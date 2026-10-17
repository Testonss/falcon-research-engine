"""Headless autonomous MT5 DEMO runtime loop.

Cycle (per symbol, per poll):
  bars (closed only) -> features -> signal -> decision -> DEMO execute
  -> reconcile (every N cycles) -> heartbeat.

Properties:
  * closed-bar only: the provider's forming bar (``is_closed=False``) is
    excluded before feature calculation — no look-ahead;
  * deterministic core: features/signals/sizing/decision ids are pure;
  * crash recovery: store seeds the executor's sent-set; executed decisions
    are never re-sent; pause flag + run/cycle cursors persist;
  * pause/resume: DB flag (``store.set_paused``) OR pause-file presence both
    pause trading (heartbeats continue with ``paused=true``);
  * fail-closed: REAL/CONTEST/unknown account, stale feed, risk block, or
    any safety error disables sending (loop keeps heartbeating);
  * REAL-money structurally forbidden: all sends go through
    RuntimeDemoExecutor -> DemoExecutionAdapter (double DEMO guard).

Offline/accelerated mode: pass a ``bars_provider`` mapping
symbol -> list of bar-dicts (or Candle lists) and ``accelerated=True`` to
run N deterministic cycles without sleeping or touching MT5. Used by tests
and the documented human soak alternative.

v0.2 closure additions (additive, existing behaviour preserved):
  * explicit RuntimeState machine (STARTING..ERROR), persisted in meta;
  * dual kill-switch pause modes (NEW_ENTRIES / ALL), persisted;
  * FullRiskEngine (13 limits, ALLOW/REJECT/PAUSE + persisted reason);
  * deterministic DailyRiskTracker (tz-explicit day identity, restart-safe);
  * deterministic ExitEngine (SL/TP/invalidation/expiry/time-stop/emergency);
  * warmup gate (history + freshness + spread + metadata);
  * market-closed bounded backoff (restart-safe);
  * degraded mode (disconnect/stale -> no entries; reconnect -> verify DEMO
    -> reconcile -> restore -> resume);
  * structured events + runtime metrics;
  * crash-recovery expiry for never-executed persisted decisions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from runtime.backoff import MarketClosedBackoff, is_market_closed_reason
from runtime.config import RuntimeConfig
from runtime.daily_state import DailyRiskTracker
from runtime.decision import DecisionEngine
from runtime.events import log_event
from runtime.execution import RuntimeDemoExecutor
from runtime.exits import ExitEngine
from runtime.features import compute_features
from runtime.health import write_heartbeat
from runtime.metrics import RuntimeMetrics
from runtime.reconcile import reconcile_positions
from runtime.risk_engine import FullRiskEngine
from runtime.signals import generate_signal
from runtime.state_machine import RuntimeState, can_enter, transition
from runtime.store import RuntimeStore
from runtime.warmup import check_warmup, required_bars

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bar_close(b: Any) -> float:
    if isinstance(b, dict):
        return float(b.get("close", 0) or 0)
    return float(getattr(b, "close", 0) or 0)


def _bar_time_iso(b: Any) -> str:
    if isinstance(b, dict):
        t = b.get("time")
    else:
        t = getattr(b, "timestamp", None)
    if isinstance(t, datetime):
        d = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        return d.isoformat()
    if t is not None:
        return str(t)
    return ""


def _is_closed(b: Any) -> bool:
    if isinstance(b, dict):
        return bool(b.get("is_closed", True))
    return bool(getattr(b, "is_closed", True))


def _bar_volume(b: Any) -> float:
    if isinstance(b, dict):
        for k in ("tick_volume", "volume", "real_volume"):
            v = b.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return 0.0
    for k in ("volume", "tick_volume"):
        try:
            v = getattr(b, k, None)
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def _bar_ohlc(b: Any, name: str) -> float:
    if isinstance(b, dict):
        try:
            return float(b.get(name, 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(getattr(b, name, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


class AutonomousDemoRuntime:
    """Headless H24-capable DEMO loop. Construct, then ``await run()``."""

    RECONCILE_EVERY = 10

    def __init__(
        self,
        config: RuntimeConfig,
        store: RuntimeStore,
        executor: RuntimeDemoExecutor,
        risk_manager=None,
        mt5_module: Any = None,
        market_provider: Any = None,
        bars_provider: Optional[Dict[str, List[Any]]] = None,
        equity_fn: Optional[Callable[[], float]] = None,
        expected_positions: Optional[Dict[int, Dict[str, Any]]] = None,
        accelerated: bool = False,
        run_id: Optional[str] = None,
        # --- v0.2 closure seams (all optional; tests drive them) ---
        now_fn: Optional[Callable[[], datetime]] = None,
        time_fn: Optional[Callable[[], float]] = None,
        feed_age_fn: Optional[Callable[[str], Optional[float]]] = None,
        spread_fn: Optional[Callable[[str], Optional[float]]] = None,
        symbol_meta_fn: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        connectivity_fn: Optional[Callable[[], Any]] = None,
    ):
        self.config = config
        self.store = store
        self.executor = executor
        self.risk = risk_manager
        self.mt5 = mt5_module
        self.provider = market_provider
        self.bars_provider = bars_provider or {}
        self.equity_fn = equity_fn or (lambda: 10000.0)
        self.expected: Dict[int, Dict[str, Any]] = dict(expected_positions or {})
        self.accelerated = accelerated
        self.run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        self.cycle = 0
        self._stop = False
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._time_fn = time_fn or time.monotonic
        self._t0 = self._time_fn()
        self._feed_age_fn = feed_age_fn
        self._spread_fn = spread_fn
        self._symbol_meta_fn = symbol_meta_fn
        self._connectivity_fn = connectivity_fn
        self.decider = DecisionEngine(
            risk_manager=risk_manager,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
            risk_per_trade=config.risk_per_trade,
            risk_per_trade_pct=config.risk_per_trade_pct,
            max_position_size=config.max_position_size,
            max_concurrent_positions=config.max_concurrent_positions,
            min_confidence=config.min_confidence,
        )
        # Closure engines (pure wrappers; legacy manager stays inner gate).
        self.daily = DailyRiskTracker(
            store, tz_name=getattr(config, "trading_day_tz", "UTC") or "UTC",
            starting_equity=self._equity())
        self.full_risk = FullRiskEngine(config, risk_manager=risk_manager,
                                        store=store, daily=self.daily)
        self.exits = ExitEngine(
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
            signal_expiry_bars=getattr(config, "signal_expiry_bars", 20),
            time_stop_bars=getattr(config, "time_stop_bars", 100))
        self.backoff = MarketClosedBackoff(
            store, base_s=getattr(config, "market_closed_backoff_s", 60.0),
            max_s=getattr(config, "market_closed_max_backoff_s", 3600.0))
        self.metrics = RuntimeMetrics(store, started_monotonic=self._t0,
                                      now_fn=self._time_fn)
        self.reconnect_count = 0
        self.stale_count = 0
        self._was_degraded = False
        self._bars_held: Dict[int, int] = {}
        # Seed expected from already-executed fills so restarts reconcile.
        try:
            for f in self.store.list_fills(limit=1000):
                t = int(f.get("ticket", 0) or 0)
                if t and t not in self.expected:
                    self.expected[t] = {
                        "symbol": f.get("symbol", ""),
                        "volume": float(f.get("volume", 0) or 0),
                        "side": str(f.get("side", "") or "").upper(),
                    }
        except Exception as e:
            logger.debug("expected-position recovery: %s", e)
        self.store.set_meta("run_id", self.run_id)
        self._paused_logged = False
        # Crash-recovery case D: persisted but never executed BUY/SELL
        # decisions are EXPIRED (never blindly resumed). Fail-closed.
        try:
            self._expire_stale_pending()
        except Exception as e:
            logger.debug("pending-expiry: %s", e)
        # Restore persisted state machine / pause mode.
        try:
            raw_state = self.store.get_meta("runtime_state", "")
            self._state = RuntimeState(raw_state) if raw_state else RuntimeState.STARTING
        except ValueError:
            self._state = RuntimeState.STARTING
        if self._state in (RuntimeState.STOPPED, RuntimeState.STOPPING,
                           RuntimeState.ERROR):
            self._state = RuntimeState.STARTING

    # -- state machine -------------------------------------------------
    @property
    def state(self) -> RuntimeState:
        return self._state

    def _set_state(self, nxt: RuntimeState) -> RuntimeState:
        if nxt == self._state:
            return self._state
        try:
            self._state = transition(self._state, nxt)
        except ValueError:
            # Fail-closed: illegal jumps land in ERROR, never silently.
            logger.debug("illegal state %s -> %s", self._state, nxt)
            try:
                self._state = transition(self._state, RuntimeState.ERROR)
            except ValueError:
                self._state = RuntimeState.ERROR
        try:
            self.store.set_meta("runtime_state", self._state.value)
        except Exception:
            pass
        return self._state

    def _force_state(self, nxt: RuntimeState) -> RuntimeState:
        self._state = nxt
        try:
            self.store.set_meta("runtime_state", nxt.value)
        except Exception:
            pass
        return self._state

    # -- control (dual kill switch) -------------------------------------
    def request_stop(self) -> None:
        self._stop = True

    def pause(self, reason: str = "operator") -> None:
        """Legacy pause: maps to PAUSE_NEW_ENTRIES (monitoring continues)."""
        self.pause_new_entries(reason)

    def pause_new_entries(self, reason: str = "operator") -> None:
        self.store.set_pause_mode("NEW_ENTRIES", reason)
        log_event(self.store, "PAUSE", run_id=self.run_id, cycle=self.cycle,
                  detail={"mode": "NEW_ENTRIES", "reason": reason})
        if self._state == RuntimeState.RUNNING:
            self._set_state(RuntimeState.PAUSED)

    def pause_all(self, reason: str = "operator") -> None:
        """PAUSE_ALL_STRATEGY_ACTIONS: no entries AND no strategy exits."""
        self.store.set_pause_mode("ALL", reason)
        log_event(self.store, "PAUSE", run_id=self.run_id, cycle=self.cycle,
                  detail={"mode": "ALL", "reason": reason})
        if self._state in (RuntimeState.RUNNING, RuntimeState.DEGRADED,
                           RuntimeState.WARMING_UP):
            try:
                self._set_state(RuntimeState.PAUSED)
            except Exception:
                self._force_state(RuntimeState.PAUSED)

    def resume(self) -> None:
        self.store.set_pause_mode("NONE", "")
        log_event(self.store, "RESUME", run_id=self.run_id, cycle=self.cycle,
                  detail={})
        if self._state == RuntimeState.PAUSED:
            self._force_state(RuntimeState.RUNNING)

    def pause_mode(self) -> str:
        try:
            return self.store.get_pause_mode()
        except Exception:
            return "NONE"

    def paused(self) -> bool:
        try:
            if self.store.get_pause_mode() != "NONE":
                return True
        except Exception:
            pass
        if self.store.is_paused():
            return True
        pf = (self.config.pause_file or "").strip()
        if pf and os.path.exists(pf):
            return True
        return False

    # -- crash recovery ---------------------------------------------------
    def _expire_stale_pending(self) -> int:
        """Expire persisted BUY/SELL decisions that never executed.

        Never auto-executes on restart (fail-closed). Returns expired count.
        """
        try:
            pending = self.store.pending_decisions()
        except Exception:
            return 0
        n = 0
        for p in pending:
            try:
                if str(p.get("action", "")) in ("BUY", "SELL") and not int(p.get("executed", 0)):
                    log_event(self.store, "DECISION", run_id=self.run_id,
                              cycle=self.cycle, decision_id=str(p.get("decision_id", "")),
                              symbol=str(p.get("symbol", "")),
                              detail={"expired": True,
                                      "reason": "persisted_but_never_executed"})
                    n += 1
            except Exception:
                continue
        return n

    # -- connectivity / degraded -------------------------------------------
    def _demo_flag(self) -> Dict[str, Any]:
        try:
            snap = self.executor.account()
            if isinstance(snap, dict) and "demo" in snap:
                return {"demo": bool(snap.get("demo")),
                        "mode": snap.get("mode_name", "")}
        except Exception:
            pass
        return {"demo": None, "mode": ""}

    def _demo_ok(self) -> bool:
        # Offline deterministic mode (bars_provider + dry_run) skips terminal
        # verification by construction — no orders reach a broker anyway.
        if self.accelerated or getattr(self.config, "dry_run", False) \
                or getattr(self.executor, "dry_run", False):
            if self.bars_provider:
                return True
        try:
            snap = self.executor.account()
        except Exception:
            return False
        if not isinstance(snap, dict):
            return False
        if snap.get("demo") is True:
            return True
        if "error" in snap:
            # No terminal attached and not in offline mode -> degraded.
            return False
        return bool(snap.get("demo"))

    def _connectivity(self) -> Dict[str, Any]:
        """Best-effort connectivity probe. Never raises."""
        if self._connectivity_fn is not None:
            try:
                r = self._connectivity_fn()
                if isinstance(r, dict):
                    return r
                return {"ok": bool(r)}
            except Exception as e:
                return {"ok": False, "reason": f"probe:{e}"}
        # Provider health (when a live provider is attached).
        prov = self.provider
        if prov is not None:
            try:
                h = prov.get_health() if hasattr(prov, "get_health") else {}
                if isinstance(h, dict):
                    if h.get("connected") is False:
                        return {"ok": False, "reason": "provider_disconnected"}
                    if h.get("stale") is True:
                        return {"ok": False, "reason": "provider_stale"}
            except Exception as e:
                return {"ok": False, "reason": f"provider:{e}"}
        # MT5 module presence (live mode only; offline bars skip this).
        if not self.bars_provider and self.mt5 is None and prov is None \
                and not getattr(self.executor, "dry_run", False):
            return {"ok": False, "reason": "mt5_unavailable"}
        return {"ok": True}

    def note_disconnect(self, reason: str = "disconnect") -> None:
        self._was_degraded = True
        if self._state in (RuntimeState.STARTING, RuntimeState.RUNNING,
                           RuntimeState.WARMING_UP, RuntimeState.CONNECTING):
            self._set_state(RuntimeState.DEGRADED)
        try:
            self.store.set_meta("degraded_reason", reason)
            self.store.set_meta("degraded_at", _utcnow_iso())
        except Exception:
            pass
        log_event(self.store, "ERROR", run_id=self.run_id, cycle=self.cycle,
                  detail={"degraded": True, "reason": reason})

    def note_reconnect(self) -> Dict[str, Any]:
        """Reconnect sequence: verify DEMO -> reconcile -> restore -> resume.

        Returns the reconcile report. No entries resume until this completes.
        """
        self.reconnect_count += 1
        try:
            self.metrics.reconnect_count = self.reconnect_count
        except Exception:
            pass
        # 1. Re-verify DEMO (fail-closed).
        demo_ok = self._demo_ok()
        if not demo_ok:
            self.note_disconnect("reconnect_demo_unverified")
            return {"ok": False, "reason": "demo_unverified"}
        # 2. Reconcile terminal truth before resuming evaluation.
        try:
            actual = self.executor.open_positions()
            report = reconcile_positions(self.expected, actual)
            for t in report.manually_closed:
                self.expected.pop(int(t), None)
                try:
                    self.store.mark_position_closed(int(t), "reconnect_reconcile")
                except Exception:
                    pass
            ok = True
        except Exception as e:
            report = None
            ok = False
            log_event(self.store, "ERROR", run_id=self.run_id, cycle=self.cycle,
                      detail={"reconnect_reconcile_failed": str(e)})
            return {"ok": False, "reason": str(e)}
        # 3. Restore + resume evaluation.
        log_event(self.store, "RECONNECT", run_id=self.run_id, cycle=self.cycle,
                  detail={"demo": True,
                          "report": report.to_dict() if report else {}})
        if self._state == RuntimeState.DEGRADED and not self.paused():
            self._set_state(RuntimeState.RUNNING)
        self._was_degraded = False
        return (report.to_dict() if report else {"ok": ok})

    # -- data ---------------------------------------------------------
    async def _closed_bars(self, symbol: str) -> List[Any]:
        if symbol in self.bars_provider:
            return [b for b in self.bars_provider[symbol] if _is_closed(b)]
        prov = self.provider
        if prov is None:
            return []
        try:
            if hasattr(prov, "fetch_bars"):
                bars, _rej = await prov.fetch_bars(symbol, self.config.timeframe, limit=300)
                return [b for b in bars if _is_closed(b)]
            if hasattr(prov, "get_candles"):
                bars = await prov.get_candles(symbol, self.config.timeframe, 300)
                return [b for b in bars if _is_closed(b)]
        except Exception as e:
            logger.debug("bars %s: %s", symbol, e)
        return []

    def _equity(self) -> float:
        try:
            return max(0.0, float(self.equity_fn()))
        except Exception:
            return 10000.0

    def _open_position_count(self) -> int:
        try:
            return len(self.executor.open_positions())
        except Exception:
            return len(self.expected)

    def _feed_age(self, symbol: str) -> float:
        if self._feed_age_fn is not None:
            try:
                v = self._feed_age_fn(symbol)
                return max(0.0, float(v if v is not None else 0.0))
            except (TypeError, ValueError):
                return 0.0
        prov = self.provider
        if prov is not None:
            try:
                h = prov.get_health() if hasattr(prov, "get_health") else {}
                if isinstance(h, dict) and h.get("stale"):
                    return float(getattr(self.config, "stale_after_s", 30.0)) + 1.0
            except Exception:
                pass
        return 0.0

    def _spread_pct(self, symbol: str, price: float) -> float:
        if self._spread_fn is not None:
            try:
                v = self._spread_fn(symbol)
                return max(0.0, float(v if v is not None else 0.0))
            except (TypeError, ValueError):
                return 0.0
        # Best-effort live spread from provider order book / MT5 tick.
        try:
            prov = self.provider
            if prov is not None and hasattr(prov, "get_order_book"):
                import asyncio as _aio
                try:
                    book = _aio.get_event_loop().run_until_complete(
                        prov.get_order_book(symbol)) \
                        if False else None
                except Exception:
                    book = None
                _ = book
        except Exception:
            pass
        try:
            mt5 = self.mt5 or getattr(self.executor.adapter, "_mt5", None)
            if mt5 is not None and hasattr(mt5, "symbol_info_tick"):
                tick = mt5.symbol_info_tick(symbol)
                bid = getattr(tick, "bid", None) if not isinstance(tick, dict) else tick.get("bid")
                ask = getattr(tick, "ask", None) if not isinstance(tick, dict) else tick.get("ask")
                if bid and ask and price:
                    return max(0.0, (float(ask) - float(bid)) / float(price))
        except Exception:
            pass
        return 0.0

    def _symbol_meta(self, symbol: str) -> Optional[Dict[str, Any]]:
        if self._symbol_meta_fn is not None:
            try:
                return self._symbol_meta_fn(symbol)
            except Exception:
                return None
        try:
            rules = self.executor.adapter.symbol_rules(symbol)
            return {"trade_mode": 0, "visible": True,
                    "volume_min": rules.get("volume_min", 0),
                    "volume_max": rules.get("volume_max", 0),
                    "volume_step": rules.get("volume_step", 0)}
        except Exception:
            return None

    # -- exits -----------------------------------------------------------
    def _run_exits(self, equity: float, now: datetime,
                   pause_all_active: bool) -> List[Dict[str, Any]]:
        """Evaluate deterministic exits for tracked positions.

        Broker-closed tickets reconcile (no duplicate close). Returns per-ticket
        exit records. PAUSE_ALL blocks strategy exits; risk-emergency exits
        still proceed (safety over pause).
        """
        out: List[Dict[str, Any]] = []
        # Terminal truth for close decisions.
        try:
            actual = self.executor.open_positions()
        except Exception:
            actual = []
        actual_by_ticket: Dict[int, Any] = {}
        for p in actual or []:
            try:
                t = getattr(p, "ticket", None)
                if t is None and isinstance(p, dict):
                    t = p.get("ticket")
                actual_by_ticket[int(t)] = p
            except (TypeError, ValueError):
                continue
        # Externally closed (broker SL/TP or manual): reconcile, never re-close.
        for t in sorted(set(int(k) for k in self.expected)):
            if t not in actual_by_ticket and t != 0:
                exp = self.expected.pop(t, {})
                exit_price = 0.0
                try:
                    for f in self.store.list_fills(limit=1000):
                        if int(f.get("ticket", 0) or 0) == int(t):
                            exit_price = float(f.get("price", 0) or 0)
                            break
                except Exception:
                    pass
                entry = float((exp or {}).get("entry_price",
                                             (exp or {}).get("price", 0)) or 0)
                side = str((exp or {}).get("side", "") or "").upper()
                # Realized PnL estimate needs a current mark; without one we
                # persist 0 and keep the linkage (no fabricated profit).
                try:
                    pos_row = None
                    for pr in self.store.list_positions(limit=5000):
                        if int(pr.get("ticket", 0) or 0) == int(t):
                            pos_row = pr
                            break
                    if pos_row:
                        entry = float(pos_row.get("entry_price", 0) or entry or 0)
                        side = str(pos_row.get("side", "") or side or "").upper()
                except Exception:
                    pass
                try:
                    self.store.mark_position_closed(int(t), "broker_or_external")
                    self.daily.record_close(now, equity, 0.0)
                except Exception:
                    pass
                log_event(self.store, "POSITION_RECONCILE", run_id=self.run_id,
                          cycle=self.cycle, ticket=int(t),
                          symbol=str((exp or {}).get("symbol", "")),
                          detail={"externally_closed": True})
                log_event(self.store, "POSITION_CLOSE", run_id=self.run_id,
                          cycle=self.cycle, ticket=int(t),
                          symbol=str((exp or {}).get("symbol", "")),
                          detail={"reason": "broker_or_external"})
                out.append({"ticket": int(t), "closed": True,
                            "reason": "broker_or_external", "external": True})
        # Strategy exits for still-open tickets.
        if pause_all_active:
            return out
        # Risk emergency flag: daily halt latched.
        risk_emergency = False
        try:
            dst = self.daily.ensure_day(now, equity)
            if dst.realized_pnl <= -abs(float(self.config.max_daily_loss or 0)):
                risk_emergency = True
        except Exception:
            pass
        for t, exp in sorted(list(self.expected.items()), key=lambda kv: kv[0]):
            if int(t) == 0:
                continue
            pos = actual_by_ticket.get(int(t))
            if pos is None:
                continue
            try:
                cur = getattr(pos, "current", None)
                if cur is None and isinstance(pos, dict):
                    cur = pos.get("current", pos.get("price_current"))
                cur_f = float(cur or 0)
            except (TypeError, ValueError):
                cur_f = 0.0
            if cur_f <= 0:
                # Fall back to last close for deterministic offline tests.
                try:
                    cur_f = float((exp or {}).get("last_price", 0) or 0)
                except (TypeError, ValueError):
                    cur_f = 0.0
            if cur_f <= 0:
                continue
            entry = 0.0
            side = str((exp or {}).get("side", "") or "").upper()
            try:
                for pr in self.store.list_positions(limit=5000):
                    if int(pr.get("ticket", 0) or 0) == int(t):
                        entry = float(pr.get("entry_price", 0) or 0)
                        side = str(pr.get("side", "") or side or "").upper()
                        break
            except Exception:
                pass
            if entry <= 0:
                try:
                    entry = float((exp or {}).get("entry_price", 0) or 0)
                except (TypeError, ValueError):
                    entry = 0.0
            held = int(self._bars_held.get(int(t), 0) or 0)
            dec = self.exits.evaluate(
                {"entry_price": entry, "side": side}, cur_f,
                bars_held=held, opposite_signal=False,
                risk_emergency=risk_emergency)
            if not dec.should_close:
                continue
            res = self.executor.close(int(t), reason=dec.reason)
            if res.get("already_closed"):
                self.expected.pop(int(t), None)
                try:
                    self.store.mark_position_closed(int(t), "broker_or_external")
                except Exception:
                    pass
                log_event(self.store, "POSITION_RECONCILE", run_id=self.run_id,
                          cycle=self.cycle, ticket=int(t),
                          symbol=str((exp or {}).get("symbol", "")),
                          detail={"externally_closed": True,
                                  "wanted": dec.reason})
                out.append({"ticket": int(t), "closed": True,
                            "reason": "broker_or_external", "external": True})
            elif res.get("closed"):
                self.expected.pop(int(t), None)
                self._bars_held.pop(int(t), None)
                # Deterministic realized estimate for the structured log.
                sign = 1.0 if side == "BUY" else -1.0
                try:
                    vol = float((exp or {}).get("volume", 0) or 0)
                except (TypeError, ValueError):
                    vol = 0.0
                pnl = sign * (cur_f - entry) * vol if entry > 0 else 0.0
                try:
                    self.daily.record_close(now, equity, pnl)
                    if self.risk is not None and hasattr(self.risk, "record_trade_result"):
                        try:
                            self.risk.record_trade_result(pnl > 0, pnl=pnl,
                                                         equity=equity, now=now)
                        except TypeError:
                            self.risk.record_trade_result(pnl > 0, pnl=pnl, equity=equity)
                except Exception:
                    pass
                log_event(self.store, "EXIT", run_id=self.run_id,
                          cycle=self.cycle, ticket=int(t),
                          symbol=str((exp or {}).get("symbol", "")),
                          detail={"reason": dec.reason})
                log_event(self.store, "POSITION_CLOSE", run_id=self.run_id,
                          cycle=self.cycle, ticket=int(t),
                          symbol=str((exp or {}).get("symbol", "")),
                          detail={"reason": dec.reason, "pnl": pnl})
                out.append({"ticket": int(t), "closed": True,
                            "reason": dec.reason, "pnl": pnl})
            else:
                log_event(self.store, "ERROR", run_id=self.run_id,
                          cycle=self.cycle, ticket=int(t),
                          symbol=str((exp or {}).get("symbol", "")),
                          detail={"exit_failed": res.get("error", "")[:300],
                                  "reason": dec.reason})
                out.append({"ticket": int(t), "closed": False,
                            "reason": dec.reason, "error": res.get("error", "")})
        return out

    # -- one cycle ----------------------------------------------------
    async def run_cycle(self) -> Dict[str, Any]:
        self.cycle += 1
        now = self._now_fn()
        try:
            now_s = self._time_fn()
        except Exception:
            now_s = time.monotonic()
        equity = self._equity()
        # Daily state first (restart preserves the day's limits).
        try:
            self.daily.ensure_day(now, equity)
        except Exception as e:
            logger.debug("daily ensure: %s", e)
        pause_mode = self.pause_mode()
        cycle_paused = self.paused()
        pause_all_active = (pause_mode == "ALL") or cycle_paused and pause_mode == "ALL"
        # Legacy file/boolean pause maps to NEW_ENTRIES semantics.
        if cycle_paused and pause_mode == "NONE":
            pause_mode = "NEW_ENTRIES"

        # Market-closed backoff: no entries while cooling down.
        backoff_blocked = self.backoff.is_blocked(now_s)
        # Connectivity -> DEGRADED (no new entries while degraded).
        conn = self._connectivity()
        demo_ok = self._demo_ok()
        degraded_reason = ""
        if not conn.get("ok"):
            degraded_reason = str(conn.get("reason", "disconnected"))
            self.note_disconnect(degraded_reason)
            try:
                self.metrics.stale_feed_count += 1 if "stale" in degraded_reason else 0
            except Exception:
                pass
        elif not demo_ok:
            degraded_reason = "demo_unverified"
            self.note_disconnect(degraded_reason)
        else:
            # Recovery path: reconnect -> verify DEMO -> reconcile -> resume.
            if self._state == RuntimeState.DEGRADED:
                self.note_reconnect()
        degraded = (self._state == RuntimeState.DEGRADED)

        # State baseline for this cycle.
        if self._state in (RuntimeState.STARTING, RuntimeState.CONNECTING):
            self._set_state(RuntimeState.WARMING_UP)
        if cycle_paused and self._state in (RuntimeState.RUNNING,
                                            RuntimeState.WARMING_UP,
                                            RuntimeState.DEGRADED):
            self._set_state(RuntimeState.PAUSED)
        elif not cycle_paused and self._state == RuntimeState.PAUSED:
            self._force_state(RuntimeState.RUNNING)
        # Recompute after baseline: a disconnect flagged above must hold
        # DEGRADED through the entry gate and the end-of-cycle promotion.
        degraded = (self._state == RuntimeState.DEGRADED)

        decisions: List[Dict[str, Any]] = []
        n_open = self._open_position_count()

        # Strategy exits run even while degraded/paused-new-entries (monitor
        # open risk); PAUSE_ALL blocks strategy exits (emergency still runs).
        exits: List[Dict[str, Any]] = []
        try:
            exits = self._run_exits(equity, now, pause_all_active)
            n_open = self._open_position_count()
        except Exception as e:
            logger.debug("exits: %s", e)

        entries_allowed = (
            can_enter(self._state)
            and not cycle_paused
            and not degraded
            and not backoff_blocked
            and demo_ok
            and pause_mode == "NONE"
        )

        for symbol in self.config.symbols:
            try:
                bars = await self._closed_bars(symbol)
            except Exception as e:
                logger.debug("cycle bars %s: %s", symbol, e)
                bars = []
            need = required_bars(
                self.config.fast_ema, self.config.slow_ema,
                self.config.momentum_period, self.config.volatility_period,
                getattr(self.config, "min_history_bars", 0))
            feed_age = self._feed_age(symbol)
            if feed_age > float(getattr(self.config, "stale_after_s", 30.0)):
                try:
                    self.metrics.stale_feed_count += 1
                    self.stale_count += 1
                except Exception:
                    pass
            # Spread estimate needs a price; compute after closes known.
            closes_preview = [_bar_close(b) for b in bars] if bars else []
            last_px = closes_preview[-1] if closes_preview else 0.0
            spread = self._spread_pct(symbol, last_px)
            meta = self._symbol_meta(symbol)
            ready, warm_reason = check_warmup(
                len(bars), feed_age, spread, meta,
                fast_ema=self.config.fast_ema, slow_ema=self.config.slow_ema,
                momentum_period=self.config.momentum_period,
                volatility_period=self.config.volatility_period,
                min_history_bars=getattr(self.config, "min_history_bars", 0),
                stale_after_s=getattr(self.config, "stale_after_s", 30.0),
                max_spread_pct=getattr(self.config, "max_spread_pct", 0.002))
            if not ready:
                wreason = warm_reason
                if len(bars) < need and warm_reason != "insufficient_history":
                    wreason = "warming_up"
                elif warm_reason == "insufficient_history":
                    wreason = "warming_up"
                decisions.append({"symbol": symbol, "action": "SKIP",
                                  "reason": wreason, "sent": False})
                try:
                    self.store.record_decision(
                        f"rt_skip_{self.run_id}_{self.cycle}_{symbol}",
                        symbol, "SKIP", 0.0, 0.0, wreason, "warmup",
                        _utcnow_iso())
                except Exception:
                    pass
                if self._state == RuntimeState.WARMING_UP and len(bars) < need:
                    pass
                continue
            closes = [_bar_close(b) for b in bars]
            volumes = [_bar_volume(b) for b in bars]
            try:
                feats = compute_features(
                    closes, volumes,
                    fast_period=self.config.fast_ema,
                    slow_period=self.config.slow_ema,
                    momentum_period=self.config.momentum_period,
                    volatility_period=self.config.volatility_period)
            except ValueError as e:
                decisions.append({"symbol": symbol, "action": "SKIP",
                                  "reason": f"bad_bars:{e}", "sent": False})
                continue
            bar_time = _bar_time_iso(bars[-1])
            # Persist last processed bar/revision (crash recovery cursor).
            try:
                self.store.set_meta(f"last_bar_{symbol}", bar_time)
                self.store.set_meta(f"last_bar_rev_{symbol}", str(len(bars)))
            except Exception:
                pass
            sig = generate_signal(
                symbol, feats, bar_time,
                min_volume=self.config.min_volume,
                momentum_threshold=self.config.momentum_threshold,
                volatility_min=self.config.volatility_min,
                volatility_max=self.config.volatility_max)
            try:
                log_event(self.store, "SIGNAL", run_id=self.run_id,
                          cycle=self.cycle, signal_id=sig.signal_id,
                          symbol=symbol,
                          detail={"side": sig.side, "bar_time": bar_time,
                                  "reason": sig.reason})
            except Exception:
                pass
            # Same-side position check from terminal truth (best effort).
            same_side = False
            try:
                for p in self.executor.open_positions():
                    psym = getattr(p, "symbol", "") if not isinstance(p, dict) else p.get("symbol", "")
                    pside = getattr(p, "side", "") if not isinstance(p, dict) else p.get("side", "")
                    if psym == symbol and str(pside).upper() == sig.side:
                        same_side = True
                        break
            except Exception:
                pass
            broker_rules: Dict[str, float] = {}
            try:
                rules = self.executor.adapter.symbol_rules(symbol)
                for k in ("volume_min", "volume_max", "volume_step"):
                    broker_rules[k] = float(rules.get(k, 0) or 0)
            except Exception:
                pass
            from falcon_terminal.registry import mt5_id as _mt5_id
            try:
                cid = _mt5_id(self.config.server or "demo", symbol)
            except ValueError:
                cid = symbol
            dec = self.decider.decide(
                sig, equity, open_positions=n_open,
                already_positioned_same_side=same_side,
                broker_rules=broker_rules or None)
            # Attach canonical id (frozen dataclass -> rebuild).
            from runtime.decision import Decision as _D
            dec = _D(dec.decision_id, dec.symbol, cid, dec.action,
                     dec.volume, dec.price, dec.stop_loss, dec.take_profit,
                     dec.reason, dec.blocked_by)
            if cycle_paused or pause_mode != "NONE":
                try:
                    self.store.record_decision(
                        dec.decision_id, symbol, "SKIP", 0.0, dec.price,
                        "paused", "paused", bar_time)
                except Exception:
                    pass
                try:
                    log_event(self.store, "RISK_REJECT", run_id=self.run_id,
                              cycle=self.cycle, signal_id=sig.signal_id,
                              decision_id=dec.decision_id, symbol=symbol,
                              detail={"reason": "paused", "mode": pause_mode})
                except Exception:
                    pass
                decisions.append({"symbol": symbol, "action": "SKIP",
                                  "reason": "paused", "sent": False,
                                  "decision_id": dec.decision_id})
                continue
            if dec.action in ("HOLD", "SKIP"):
                try:
                    self.store.record_decision(
                        dec.decision_id, symbol, dec.action, 0.0, dec.price,
                        dec.reason, dec.blocked_by, bar_time)
                except Exception:
                    pass
                try:
                    log_event(self.store, "DECISION", run_id=self.run_id,
                              cycle=self.cycle, signal_id=sig.signal_id,
                              decision_id=dec.decision_id, symbol=symbol,
                              detail={"action": dec.action,
                                      "reason": dec.reason,
                                      "blocked_by": dec.blocked_by or ""})
                except Exception:
                    pass
                decisions.append({"symbol": symbol, "action": dec.action,
                                  "reason": dec.reason, "sent": False,
                                  "decision_id": dec.decision_id})
                continue
            # Gating outside RUNNING: never send outside valid state.
            if not entries_allowed:
                gate_reason = "not_running" if not can_enter(self._state) else (
                    "degraded" if degraded else (
                        "backoff" if backoff_blocked else (
                            "demo_unverified" if not demo_ok else "paused")))
                try:
                    self.store.record_decision(
                        dec.decision_id, symbol, "SKIP", 0.0, dec.price,
                        gate_reason, gate_reason, bar_time)
                except Exception:
                    pass
                try:
                    log_event(self.store, "RISK_REJECT", run_id=self.run_id,
                              cycle=self.cycle, signal_id=sig.signal_id,
                              decision_id=dec.decision_id, symbol=symbol,
                              detail={"reason": gate_reason})
                except Exception:
                    pass
                decisions.append({"symbol": symbol, "action": "SKIP",
                                  "reason": gate_reason, "sent": False,
                                  "decision_id": dec.decision_id})
                continue
            # Full risk engine (13 limits, explicit ALLOW/REJECT/PAUSE).
            try:
                positions_ctx: List[Dict[str, Any]] = []
                try:
                    for t, e in self.expected.items():
                        positions_ctx.append({
                            "ticket": int(t), "symbol": e.get("symbol", ""),
                            "volume": float(e.get("volume", 0) or 0),
                            "side": str(e.get("side", "") or ""),
                            "entry_price": float(e.get("entry_price", e.get("price", 0)) or 0),
                            "price": float(e.get("entry_price", e.get("price", 0)) or 0)})
                except Exception:
                    positions_ctx = []
                verdict = self.full_risk.evaluate(
                    symbol, dec.volume, dec.price, equity,
                    positions=positions_ctx, spread_pct=spread,
                    feed_age_s=feed_age, n_bars=len(bars), need_bars=need,
                    now=now, pause_mode=pause_mode, demo_ok=demo_ok)
            except Exception as e:
                from runtime.risk_engine import RiskVerdict as _RV
                verdict = _RV("REJECT", "risk_error", str(e))
            if verdict.verdict != "ALLOW":
                try:
                    self.store.record_decision(
                        dec.decision_id, symbol, "SKIP", 0.0, dec.price,
                        f"risk:{verdict.reason}", verdict.reason, bar_time)
                except Exception:
                    pass
                try:
                    log_event(self.store, "RISK_REJECT", run_id=self.run_id,
                              cycle=self.cycle, signal_id=sig.signal_id,
                              decision_id=dec.decision_id, symbol=symbol,
                              detail={"reason": verdict.reason,
                                      "verdict": verdict.verdict,
                                      "detail": verdict.detail})
                except Exception:
                    pass
                decisions.append({"symbol": symbol, "action": "SKIP",
                                  "reason": f"risk:{verdict.reason}",
                                  "sent": False, "verdict": verdict.verdict,
                                  "decision_id": dec.decision_id})
                continue
            # Persist decision + attempt linkage before sending.
            try:
                log_event(self.store, "DECISION", run_id=self.run_id,
                          cycle=self.cycle, signal_id=sig.signal_id,
                          decision_id=dec.decision_id, symbol=symbol,
                          detail={"action": dec.action, "volume": dec.volume,
                                  "reason": dec.reason})
                log_event(self.store, "ORDER_ATTEMPT", run_id=self.run_id,
                          cycle=self.cycle, signal_id=sig.signal_id,
                          decision_id=dec.decision_id, symbol=symbol,
                          detail={"volume": dec.volume, "price": dec.price})
            except Exception:
                pass
            res = self.executor.execute(dec, tick_age_s=feed_age)
            if res.sent and (res.ticket or getattr(self.executor, "dry_run", False)):
                self.expected[res.ticket or 0] = {
                    "symbol": symbol, "volume": dec.volume, "side": dec.action,
                    "entry_price": dec.price, "last_price": dec.price}
                self._bars_held[res.ticket or 0] = 0
                n_open += 1
                # Persistent cooldown + per-symbol tape (restart-safe).
                try:
                    gap = float(getattr(self.config, "cooldown_between_trades_s", 0.0) or 0.0)
                    if gap > 0:
                        from datetime import timedelta as _td
                        self.store.set_cooldown(
                            "trade_cooldown", (now + _td(seconds=gap)).isoformat())
                    self.store.record_trade_tape(symbol, dec.decision_id, now.isoformat())
                except Exception:
                    pass
                try:
                    log_event(self.store, "ORDER_ACCEPTED", run_id=self.run_id,
                              cycle=self.cycle, signal_id=sig.signal_id,
                              decision_id=dec.decision_id, ticket=int(res.ticket or 0),
                              symbol=symbol, detail={"volume": dec.volume})
                    if res.ticket:
                        log_event(self.store, "POSITION_OPEN", run_id=self.run_id,
                                  cycle=self.cycle, signal_id=sig.signal_id,
                                  decision_id=dec.decision_id,
                                  ticket=int(res.ticket or 0), symbol=symbol,
                                  detail={"side": dec.action,
                                          "volume": dec.volume})
                except Exception:
                    pass
            else:
                # Market-closed style errors arm the bounded backoff
                # (restart persists it — no order storm).
                try:
                    err = f"{res.error or ''} {res.reason or ''}"
                    if is_market_closed_reason(err):
                        self.backoff.notify_closed(now_s)
                        log_event(self.store, "ERROR", run_id=self.run_id,
                                  cycle=self.cycle, signal_id=sig.signal_id,
                                  decision_id=dec.decision_id, symbol=symbol,
                                  detail={"market_closed_backoff": True,
                                          "error": err[:300]})
                    else:
                        log_event(self.store, "ORDER_REJECTED", run_id=self.run_id,
                                  cycle=self.cycle, signal_id=sig.signal_id,
                                  decision_id=dec.decision_id, symbol=symbol,
                                  detail={"reason": res.reason or "",
                                          "error": (res.error or "")[:300]})
                except Exception:
                    pass
            decisions.append({"symbol": symbol, "action": dec.action,
                              "reason": dec.reason, "sent": res.sent,
                              "duplicate": res.duplicate, "ticket": res.ticket,
                              "error": res.error, "decision_id": dec.decision_id})
        # Age tracked positions by one bar-cycle.
        try:
            for t in list(self._bars_held):
                self._bars_held[t] = int(self._bars_held.get(t, 0) or 0) + 1
        except Exception:
            pass
        # Periodic reconciliation (terminal is authoritative).
        recon: Dict[str, Any] = {"ok": True}
        if self.cycle % self.RECONCILE_EVERY == 0:
            try:
                actual = self.executor.open_positions()
                report = reconcile_positions(self.expected, actual)
                recon = report.to_dict()
                # Drop locally-expected tickets the terminal no longer shows
                # (manual close / SL-TP filled externally).
                for t in report.manually_closed:
                    exp = self.expected.pop(int(t), None)
                    try:
                        self.store.mark_position_closed(int(t), "broker_or_external")
                        log_event(self.store, "POSITION_RECONCILE",
                                  run_id=self.run_id, cycle=self.cycle,
                                  ticket=int(t),
                                  symbol=str((exp or {}).get("symbol", "")),
                                  detail=report.to_dict())
                    except Exception:
                        pass
            except Exception as e:
                recon = {"ok": False, "error": str(e)}
        # Promote WARMING_UP -> RUNNING once every symbol has enough history
        # and no gate blocks entries.
        try:
            if self._state == RuntimeState.WARMING_UP and not cycle_paused \
                    and not degraded and demo_ok and not backoff_blocked \
                    and bool(conn.get("ok")):
                self._set_state(RuntimeState.RUNNING)
        except Exception:
            pass
        try:
            snap_metrics = self.metrics.snapshot()
        except Exception:
            snap_metrics = {}
        try:
            daily_snap = self.daily.state.to_dict() if self.daily.state else {}
        except Exception:
            daily_snap = {}
        state = {
            "run_id": self.run_id, "cycle": self.cycle,
            "paused": cycle_paused, "pause_mode": pause_mode,
            "equity": equity,
            "open_expected": len(self.expected),
            "decisions": decisions, "reconcile": recon,
            "exits": exits,
            "demo": self._demo_flag(),
            "runtime_state": self._state.value,
            "degraded": degraded,
            "degraded_reason": degraded_reason,
            "backoff_blocked": backoff_blocked,
            "daily": daily_snap,
            "metrics": snap_metrics,
        }
        try:
            write_heartbeat(self.store, self.run_id, self.cycle, state)
            self.store.set_meta("cycle", str(self.cycle))
        except Exception as e:
            logger.debug("heartbeat: %s", e)
        return state

    # -- main loop ----------------------------------------------------
    async def run(self, max_cycles: Optional[int] = None) -> Dict[str, Any]:
        self._set_state(RuntimeState.CONNECTING)
        limit = max_cycles if max_cycles is not None else self.config.max_cycles
        limit = int(limit or 0)
        # DEMO enable gate runs once up front (fail-closed: loop continues
        # heartbeating but never sends when unsafe).
        try:
            self.executor.enable(self.mt5)
            self._set_state(RuntimeState.WARMING_UP)
        except Exception as e:
            logger.warning("DEMO enable failed — running send-disabled: %s", e)
            self.note_disconnect(f"enable:{e}")
        sent = 0
        try:
            while not self._stop:
                state = await self.run_cycle()
                sent += sum(1 for d in state.get("decisions", []) if d.get("sent"))
                if limit and self.cycle >= limit:
                    break
                if not self.accelerated and self.config.poll_interval_s > 0:
                    try:
                        await asyncio.sleep(self.config.poll_interval_s)
                    except asyncio.CancelledError:
                        break
                else:
                    await asyncio.sleep(0)
        finally:
            try:
                if self._stop:
                    self._set_state(RuntimeState.STOPPED)
                else:
                    # max_cycles reached: orderly stop, still restartable.
                    self._set_state(RuntimeState.STOPPING)
                    self._set_state(RuntimeState.STOPPED)
            except Exception:
                pass
        return {"run_id": self.run_id, "cycles": self.cycle, "sent": sent,
                "state": self._state.value}
