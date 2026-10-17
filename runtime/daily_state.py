"""Deterministic daily risk state (timezone-explicit, SQLite-backed).

Trading-day identity is the calendar date in ``trading_day_tz`` (IANA name or
UTC offset; unknown names fall back to UTC — never naive). Restart reloads
the current day's row so limits survive crashes. Day rollover is deterministic
given the supplied ``now`` (tests drive it explicitly).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def resolve_tz(name: str):
    n = (name or "UTC").strip() or "UTC"
    if n.upper() == "UTC":
        return timezone.utc
    # Fixed-offset form: UTC+14, UTC-5, UTC+5:30 (no tzdata needed).
    import re as _re
    m = _re.fullmatch(r"UTC\s*([+-])\s*(\d{1,2})(?::?\s*(\d{1,2}))?",
                      n.strip(), _re.IGNORECASE)
    if m:
        from datetime import timedelta as _td
        sign = 1 if m.group(1) == "+" else -1
        hrs = int(m.group(2))
        mins = int(m.group(3) or 0)
        if hrs <= 14 and mins < 60:
            return timezone(sign * _td(hours=hrs, minutes=mins),
                            name=f"UTC{m.group(1)}{hrs}")
    if ZoneInfo is not None:
        try:
            return ZoneInfo(n)
        except Exception:
            return timezone.utc
    return timezone.utc


def trading_day(now: datetime, tz_name: str = "UTC") -> str:
    tz = resolve_tz(tz_name)
    d = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return d.astimezone(tz).date().isoformat()


@dataclass
class DailyRiskState:
    day: str
    tz: str = "UTC"
    starting_equity: float = 0.0
    peak_equity: float = 0.0
    realized_pnl: float = 0.0
    drawdown: float = 0.0
    trade_count: int = 0
    loss_count: int = 0
    consecutive_losses: int = 0

    def to_dict(self) -> dict:
        return {
            "day": self.day, "tz": self.tz,
            "starting_equity": self.starting_equity,
            "peak_equity": self.peak_equity,
            "realized_pnl": self.realized_pnl,
            "drawdown": self.drawdown, "trade_count": self.trade_count,
            "loss_count": self.loss_count,
            "consecutive_losses": self.consecutive_losses,
        }


class DailyRiskTracker:
    """Owns the current day's row; deterministic reset on day boundary."""

    def __init__(self, store: Any, tz_name: str = "UTC",
                 starting_equity: float = 0.0):
        self.store = store
        self.tz_name = (tz_name or "UTC").strip() or "UTC"
        self.state: Optional[DailyRiskState] = None
        self._seed_equity = float(starting_equity or 0.0)

    def ensure_day(self, now: datetime, equity: float) -> DailyRiskState:
        day = trading_day(now, self.tz_name)
        eq = max(0.0, float(equity or 0.0))
        existing = None
        try:
            existing = self.store.get_daily(day)
        except Exception:
            existing = None
        if existing is None:
            st = DailyRiskState(day=day, tz=self.tz_name,
                                starting_equity=eq, peak_equity=eq)
            try:
                self.store.put_daily(st.to_dict())
            except Exception:
                pass
            self.state = st
            return st
        st = DailyRiskState(
            day=str(existing.get("day", day)), tz=str(existing.get("tz", self.tz_name)),
            starting_equity=float(existing.get("starting_equity", eq) or 0),
            peak_equity=float(existing.get("peak_equity", eq) or 0),
            realized_pnl=float(existing.get("realized_pnl", 0) or 0),
            drawdown=float(existing.get("drawdown", 0) or 0),
            trade_count=int(existing.get("trade_count", 0) or 0),
            loss_count=int(existing.get("loss_count", 0) or 0),
            consecutive_losses=int(existing.get("consecutive_losses", 0) or 0))
        # Peak ratchets up; drawdown derived deterministically.
        if eq > st.peak_equity:
            st.peak_equity = eq
        if st.peak_equity > 0:
            st.drawdown = max(0.0, (st.peak_equity - eq) / st.peak_equity)
        # First touch of the day with zero start adopts current equity.
        if st.starting_equity <= 0 and eq > 0:
            st.starting_equity = eq
            if st.peak_equity <= 0:
                st.peak_equity = eq
        try:
            self.store.put_daily(st.to_dict())
        except Exception:
            pass
        self.state = st
        return st

    def record_close(self, now: datetime, equity: float,
                     realized_pnl: float) -> DailyRiskState:
        st = self.ensure_day(now, equity)
        pnl = float(realized_pnl or 0.0)
        st.trade_count += 1
        st.realized_pnl += pnl
        if pnl < 0:
            st.loss_count += 1
            st.consecutive_losses += 1
        elif pnl > 0:
            st.consecutive_losses = 0
        try:
            self.store.put_daily(st.to_dict())
        except Exception:
            pass
        self.state = st
        return st
