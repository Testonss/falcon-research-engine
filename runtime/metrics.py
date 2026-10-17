"""Runtime metrics (headless-readable, no profitability claims).

Exposes uptime, signals, risk rejects, orders attempted/accepted/rejected,
positions opened/closed, wins/losses, realized P/L, drawdown, reconnect and
stale-feed counts. Derived deterministically from the SQLite store plus the
in-memory run clock (start monotonic). No forward-looking or profit promises.
"""

from __future__ import annotations

import time
from typing import Any, Dict


class RuntimeMetrics:
    def __init__(self, store: Any, started_monotonic: float = 0.0,
                 now_fn=None):
        self.store = store
        self._t0 = float(started_monotonic or time.monotonic())
        self._now = now_fn or time.monotonic
        self.reconnect_count = 0
        self.stale_feed_count = 0

    def snapshot(self) -> Dict[str, Any]:
        try:
            uptime = max(0.0, float(self._now()) - self._t0)
        except Exception:
            uptime = 0.0
        out: Dict[str, Any] = {
            "uptime_s": round(uptime, 1),
            "signals": 0, "risk_rejects": 0,
            "orders_attempted": 0, "orders_accepted": 0,
            "orders_rejected": 0, "positions_opened": 0,
            "positions_closed": 0, "wins": 0, "losses": 0,
            "realized_pnl": 0.0, "drawdown": 0.0,
            "reconnect_count": int(self.reconnect_count),
            "stale_feed_count": int(self.stale_feed_count),
        }
        try:
            counts = self.store.decision_counts()
            out["signals"] = int(counts.get("total", 0) or 0)
        except Exception:
            pass
        try:
            ev = self.store.event_counts()
            out["risk_rejects"] = int(ev.get("RISK_REJECT", 0) or 0)
            out["orders_attempted"] = int(ev.get("ORDER_ATTEMPT", 0) or 0)
            out["orders_accepted"] = int(ev.get("ORDER_ACCEPTED", 0) or 0)
            out["orders_rejected"] = int(ev.get("ORDER_REJECTED", 0) or 0)
            out["positions_opened"] = int(ev.get("POSITION_OPEN", 0) or 0)
            out["positions_closed"] = int(ev.get("POSITION_CLOSE", 0) or 0)
            out["reconnect_count"] = max(
                out["reconnect_count"], int(ev.get("RECONNECT", 0) or 0))
        except Exception:
            pass
        try:
            for t in self.store.list_closed_trades(limit=5000):
                pnl = float(t.get("realized_pnl", 0) or 0)
                out["realized_pnl"] += pnl
                if pnl > 0:
                    out["wins"] += 1
                elif pnl < 0:
                    out["losses"] += 1
            out["realized_pnl"] = round(out["realized_pnl"], 2)
        except Exception:
            pass
        return out
