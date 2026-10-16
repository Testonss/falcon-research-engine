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
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from runtime.config import RuntimeConfig
from runtime.decision import DecisionEngine
from runtime.execution import RuntimeDemoExecutor
from runtime.features import compute_features
from runtime.health import write_heartbeat
from runtime.reconcile import reconcile_positions
from runtime.signals import generate_signal
from runtime.store import RuntimeStore

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

    # -- control ------------------------------------------------------
    def request_stop(self) -> None:
        self._stop = True

    def pause(self, reason: str = "operator") -> None:
        self.store.set_paused(True, reason)

    def resume(self) -> None:
        self.store.set_paused(False)

    def paused(self) -> bool:
        if self.store.is_paused():
            return True
        pf = (self.config.pause_file or "").strip()
        if pf and os.path.exists(pf):
            return True
        return False

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

    # -- one cycle ----------------------------------------------------
    async def run_cycle(self) -> Dict[str, Any]:
        self.cycle += 1
        cycle_paused = self.paused()
        decisions: List[Dict[str, Any]] = []
        equity = self._equity()
        n_open = self._open_position_count()
        for symbol in self.config.symbols:
            try:
                bars = await self._closed_bars(symbol)
            except Exception as e:
                logger.debug("cycle bars %s: %s", symbol, e)
                bars = []
            need = max(self.config.slow_ema, self.config.momentum_period,
                       self.config.volatility_period) + 2
            if len(bars) < need:
                decisions.append({"symbol": symbol, "action": "SKIP",
                                  "reason": "warming_up", "sent": False})
                try:
                    self.store.record_decision(
                        f"rt_skip_{self.run_id}_{self.cycle}_{symbol}",
                        symbol, "SKIP", 0.0, 0.0, "warming_up", "warmup",
                        _utcnow_iso())
                except Exception:
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
            sig = generate_signal(
                symbol, feats, bar_time,
                min_volume=self.config.min_volume,
                momentum_threshold=self.config.momentum_threshold,
                volatility_min=self.config.volatility_min,
                volatility_max=self.config.volatility_max)
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
            if cycle_paused:
                try:
                    self.store.record_decision(
                        dec.decision_id, symbol, "SKIP", 0.0, dec.price,
                        "paused", "paused", bar_time)
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
                decisions.append({"symbol": symbol, "action": dec.action,
                                  "reason": dec.reason, "sent": False,
                                  "decision_id": dec.decision_id})
                continue
            res = self.executor.execute(dec)
            if res.sent and res.ticket:
                self.expected[res.ticket] = {
                    "symbol": symbol, "volume": dec.volume, "side": dec.action}
                n_open += 1
            decisions.append({"symbol": symbol, "action": dec.action,
                              "reason": dec.reason, "sent": res.sent,
                              "duplicate": res.duplicate, "ticket": res.ticket,
                              "error": res.error, "decision_id": dec.decision_id})
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
                    self.expected.pop(int(t), None)
            except Exception as e:
                recon = {"ok": False, "error": str(e)}
        state = {
            "run_id": self.run_id, "cycle": self.cycle,
            "paused": cycle_paused, "equity": equity,
            "open_expected": len(self.expected),
            "decisions": decisions, "reconcile": recon,
            "demo": self._demo_flag(),
        }
        try:
            write_heartbeat(self.store, self.run_id, self.cycle, state)
            self.store.set_meta("cycle", str(self.cycle))
        except Exception as e:
            logger.debug("heartbeat: %s", e)
        return state

    def _demo_flag(self) -> Dict[str, Any]:
        try:
            snap = self.executor.account()
            if isinstance(snap, dict) and "demo" in snap:
                return {"demo": bool(snap.get("demo")),
                        "mode": snap.get("mode_name", "")}
        except Exception:
            pass
        return {"demo": None, "mode": ""}

    # -- main loop ----------------------------------------------------
    async def run(self, max_cycles: Optional[int] = None) -> Dict[str, Any]:
        limit = max_cycles if max_cycles is not None else self.config.max_cycles
        limit = int(limit or 0)
        # DEMO enable gate runs once up front (fail-closed: loop continues
        # heartbeating but never sends when unsafe).
        try:
            self.executor.enable(self.mt5)
        except Exception as e:
            logger.warning("DEMO enable failed — running send-disabled: %s", e)
        sent = 0
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
        return {"run_id": self.run_id, "cycles": self.cycle, "sent": sent}
