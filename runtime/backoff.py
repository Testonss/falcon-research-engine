"""Market-closed bounded backoff (persisted, restart-safe).

After market_closed / trade_disabled / session_unavailable, the runtime must
not hammer the broker. Backoff is exponential from ``base_s`` capped at
``max_s``, persisted via ``cooldowns`` so an immediate restart keeps the
quiet period instead of creating an order storm. Deterministic given the
explicit ``now`` timestamp (seconds since epoch).
"""

from __future__ import annotations

from typing import Any

MARKET_CLOSED_SIGNALS = (
    "market closed", "market_closed", "trade disabled", "trade_disabled",
    "session unavailable", "session_unavailable", "no quotes", "invalid stops",
)

RETRYABLE_RETCODE_HINTS = (10018, 10017, 10021, 10027)


def is_market_closed_reason(text: str) -> bool:
    t = (text or "").strip().lower().replace("-", " ").replace("_", " ")
    for sig in MARKET_CLOSED_SIGNALS:
        norm = sig.replace("-", " ").replace("_", " ")
        if norm and norm in t:
            return True
    return False


class MarketClosedBackoff:
    KEY = "market_closed"

    def __init__(self, store: Any, base_s: float = 60.0,
                 max_s: float = 3600.0):
        self.store = store
        self.base_s = max(1.0, float(base_s or 60.0))
        self.max_s = max(self.base_s, float(max_s or 3600.0))

    def _failures(self) -> int:
        try:
            return max(0, int(self.store.get_meta("market_closed_failures", "0") or 0))
        except (TypeError, ValueError):
            return 0

    def _set_failures(self, n: int) -> None:
        try:
            self.store.set_meta("market_closed_failures", str(max(0, int(n))))
        except Exception:
            pass

    def blocked_until(self) -> float:
        try:
            return float(self.store.get_meta("market_closed_until", "0") or 0)
        except (TypeError, ValueError):
            return 0.0

    def is_blocked(self, now_s: float) -> bool:
        return float(now_s or 0) < self.blocked_until()

    def notify_closed(self, now_s: float) -> float:
        """Record one market-closed hit; returns the new blocked-until time."""
        n = self._failures() + 1
        self._set_failures(n)
        wait = min(self.base_s * (2 ** (n - 1)), self.max_s)
        until = float(now_s or 0) + wait
        try:
            self.store.set_meta("market_closed_until", str(until))
        except Exception:
            pass
        try:
            self.store.set_cooldown(self.KEY, str(until))
        except Exception:
            pass
        return until

    def notify_success(self) -> None:
        self._set_failures(0)
        try:
            self.store.set_meta("market_closed_until", "0")
        except Exception:
            pass
