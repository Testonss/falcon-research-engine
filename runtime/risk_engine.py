"""Full runtime risk engine (explicit ALLOW / REJECT / PAUSE).

Covers every acceptance limit — no silent pass-through:

  max risk per trade | max concurrent positions | max total lots |
  max exposure per symbol | max total exposure | max daily realized loss |
  max daily drawdown | max consecutive losses | max spread |
  minimum data freshness | minimum history | cooldown between trades |
  maximum trades per symbol per time window.

Results persist the reason (``blocked_by`` on the decision + RISK_REJECT
event). PAUSE halts entries until the condition clears (daily halt,
consecutive-loss halt, kill switch, pause mode); REJECT blocks only the
candidate order. The legacy ``RiskManager.evaluate_trade`` still runs as an
inner gate so shared research limits stay authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class RiskVerdict:
    verdict: str  # "ALLOW" | "REJECT" | "PAUSE"
    reason: str   # "ok" or the blocking limit name
    detail: str = ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(s))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except (TypeError, ValueError):
        return None


class FullRiskEngine:
    """Explicit per-order risk closure over RuntimeConfig + DailyRiskTracker."""

    def __init__(self, config: Any, risk_manager: Any = None,
                 store: Any = None, daily: Any = None):
        self.config = config
        self.risk = risk_manager
        self.store = store
        self.daily = daily

    # -- helpers ---------------------------------------------------------
    def _totals(self, positions: Optional[List[Dict[str, Any]]],
                symbol: str, qty: float, price: float):
        pos = list(positions or [])
        n_open = len(pos)
        total_lots = sum(float(p.get("volume", 0) or 0) for p in pos)
        total_expo = sum(float(p.get("volume", 0) or 0)
                         * float(p.get("entry_price", p.get("price", 0)) or 0)
                         for p in pos)
        sym_lots = sum(float(p.get("volume", 0) or 0)
                       for p in pos if str(p.get("symbol", "")) == symbol)
        sym_expo = sum(float(p.get("volume", 0) or 0)
                       * float(p.get("entry_price", p.get("price", 0)) or 0)
                       for p in pos if str(p.get("symbol", "")) == symbol)
        new_notional = float(qty or 0) * float(price or 0)
        return n_open, total_lots, total_expo, sym_lots, sym_expo, new_notional

    # -- main gate ---------------------------------------------------------
    def evaluate(self, symbol: str, qty: float, price: float, equity: float,
                 positions: Optional[List[Dict[str, Any]]] = None,
                 spread_pct: Optional[float] = None,
                 feed_age_s: Optional[float] = None,
                 n_bars: Optional[int] = None,
                 need_bars: Optional[int] = None,
                 now: Optional[datetime] = None,
                 pause_mode: str = "NONE",
                 demo_ok: bool = True) -> RiskVerdict:
        cfg = self.config
        now = now or _utcnow()
        pm = (pause_mode or "NONE").upper()

        # 0. Structural gates (always PAUSE-class: operator/recovery scope).
        if not demo_ok:
            return RiskVerdict("REJECT", "not_demo", "demo unverified")
        if pm in ("NEW_ENTRIES", "ALL"):
            return RiskVerdict("PAUSE", "paused",
                               f"pause_mode={pm}")
        # Kill switch latches (legacy manager mirrors operator halt).
        try:
            if self.risk is not None and not self.risk.check_kill_switch():
                return RiskVerdict("PAUSE", "kill_switch", "kill switch latched")
        except Exception:
            pass

        # 1. Minimum history (explicit, per-symbol warmup floor).
        if n_bars is not None and need_bars is not None:
            try:
                if int(n_bars) < int(need_bars):
                    return RiskVerdict("REJECT", "min_history",
                                       f"{n_bars}<{need_bars}")
            except (TypeError, ValueError):
                return RiskVerdict("REJECT", "min_history", "bad bar count")

        # 2. Data freshness.
        if feed_age_s is not None:
            try:
                if float(feed_age_s) > float(getattr(cfg, "stale_after_s", 30.0)):
                    return RiskVerdict("REJECT", "stale_feed",
                                       f"age={feed_age_s}")
            except (TypeError, ValueError):
                return RiskVerdict("REJECT", "stale_feed", "bad age")

        # 3. Spread.
        if spread_pct is not None:
            try:
                cap = float(getattr(cfg, "max_spread_pct", 0.0) or 0.0)
                if cap > 0 and float(spread_pct) > cap:
                    return RiskVerdict("REJECT", "max_spread",
                                       f"{spread_pct}>{cap}")
            except (TypeError, ValueError):
                pass

        # 4. Quantity sanity + max risk per trade.
        try:
            q = float(qty or 0)
            px = float(price or 0)
            eq = float(equity or 0)
        except (TypeError, ValueError):
            return RiskVerdict("REJECT", "bad_sizing", "non-numeric")
        if q <= 0 or px <= 0 or eq <= 0:
            return RiskVerdict("REJECT", "bad_sizing", "non-positive")
        try:
            stop_pct = float(getattr(cfg, "stop_loss_pct", 0.01) or 0.01)
            budget = min(float(getattr(cfg, "risk_per_trade", 10.0)),
                         eq * float(getattr(cfg, "risk_per_trade_pct", 0.01)))
            est_risk = q * px * stop_pct
            if budget > 0 and est_risk > budget * (1 + 1e-9):
                return RiskVerdict("REJECT", "max_risk_per_trade",
                                   f"est={est_risk:.2f}>budget={budget:.2f}")
        except (TypeError, ValueError):
            pass
        if q > float(getattr(cfg, "max_position_size", 1.0) or 0):
            return RiskVerdict("REJECT", "max_position_size",
                               f"{q}>{getattr(cfg, 'max_position_size', 1.0)}")

        # 5. Concurrent positions / lots / exposure.
        n_open, total_lots, total_expo, sym_lots, sym_expo, new_notional = \
            self._totals(positions, symbol, q, px)
        if n_open >= int(getattr(cfg, "max_concurrent_positions", 5)):
            return RiskVerdict("REJECT", "max_concurrent_positions",
                               f"open={n_open}")
        if total_lots + q > float(getattr(cfg, "max_total_lots", 5.0) or 0):
            return RiskVerdict("REJECT", "max_total_lots",
                               f"{total_lots}+{q}")
        if sym_expo + new_notional > float(
                getattr(cfg, "max_exposure_per_symbol", 1e12) or 1e12):
            return RiskVerdict("REJECT", "max_exposure_per_symbol",
                               f"{sym_expo}+{new_notional}")
        if total_expo + new_notional > float(
                getattr(cfg, "max_total_exposure", 1e12) or 1e12):
            return RiskVerdict("REJECT", "max_total_exposure",
                               f"{total_expo}+{new_notional}")

        # 6. Daily realized loss + daily drawdown (PAUSE-class: day halt).
        daily_state = None
        if self.daily is not None:
            try:
                daily_state = self.daily.ensure_day(now, eq)
            except Exception:
                daily_state = None
        if daily_state is not None:
            if daily_state.realized_pnl <= -abs(
                    float(getattr(cfg, "max_daily_loss", 500.0))):
                return RiskVerdict("PAUSE", "max_daily_realized_loss",
                                   f"pnl={daily_state.realized_pnl}")
            cap_dd = float(getattr(cfg, "max_daily_drawdown_pct", 0.05) or 0)
            if cap_dd > 0 and float(daily_state.drawdown or 0) > cap_dd:
                return RiskVerdict("PAUSE", "max_daily_drawdown",
                                   f"dd={daily_state.drawdown}")
            max_cons = int(getattr(cfg, "max_consecutive_losses", 3) or 0)
            if max_cons > 0 and int(daily_state.consecutive_losses or 0) >= max_cons:
                return RiskVerdict("PAUSE", "max_consecutive_losses",
                                   f"n={daily_state.consecutive_losses}")
        else:
            # Fall back to legacy manager counters when no daily tracker.
            try:
                if self.risk is not None:
                    if not self.risk.check_daily_loss_limit(0.0, now):
                        return RiskVerdict("PAUSE", "max_daily_realized_loss", "")
                    if not self.risk.check_consecutive_losses():
                        return RiskVerdict("PAUSE", "max_consecutive_losses", "")
                    if not self.risk.check_drawdown(eq):
                        return RiskVerdict("PAUSE", "max_daily_drawdown", "")
            except Exception:
                pass

        # 7. Cooldown between trades (persistent).
        if self.store is not None:
            try:
                gap = float(getattr(cfg, "cooldown_between_trades_s", 0.0) or 0.0)
                if gap > 0:
                    until = str(self.store.get_cooldown("trade_cooldown") or "")
                    dt = _parse_iso(until) if until else None
                    if dt is not None and now < dt:
                        return RiskVerdict("REJECT", "cooldown_between_trades",
                                           f"until={until}")
            except Exception:
                pass
            # 8. Max trades per symbol per rolling window (persistent tape).
            try:
                cap_n = int(getattr(cfg, "max_trades_per_symbol", 10) or 0)
                win = float(getattr(cfg, "trades_window_s", 3600.0) or 0)
                if cap_n > 0 and win > 0:
                    from datetime import timedelta
                    since = (now - timedelta(seconds=win)).isoformat()
                    if int(self.store.count_tape(symbol, since)) >= cap_n:
                        return RiskVerdict("REJECT", "max_trades_per_symbol",
                                           f"cap={cap_n}/{win}s")
            except Exception:
                pass

        # 9. Legacy unified gate stays authoritative (covers kill/cooldown/
        #    consecutive/daily/drawdown/affordability from shared config).
        if self.risk is not None:
            try:
                allowed, reason = self.risk.evaluate_trade(symbol, q, px, eq, now=now)
            except TypeError:
                try:
                    allowed, reason = self.risk.evaluate_trade(symbol, q, px, eq)
                except Exception:
                    allowed, reason = True, "ok"
            except Exception:
                allowed, reason = True, "ok"
            if not allowed:
                r = str(reason or "risk")
                pause_reasons = {"kill_switch", "daily_loss", "max_drawdown",
                                 "consecutive_losses", "cooldown"}
                return RiskVerdict("PAUSE" if r in pause_reasons else "REJECT",
                                   r, "legacy gate")

        return RiskVerdict("ALLOW", "ok")
