"""Structured runtime event log (persisted, secret-free).

Event types: SIGNAL RISK_REJECT DECISION ORDER_ATTEMPT ORDER_ACCEPTED
ORDER_REJECTED POSITION_OPEN POSITION_RECONCILE POSITION_CLOSE EXIT PAUSE
RESUME RECONNECT ERROR. Each event links signal_id / decision_id /
execution_id / ticket where known.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

EVENT_TYPES = (
    "SIGNAL", "RISK_REJECT", "DECISION", "ORDER_ATTEMPT", "ORDER_ACCEPTED",
    "ORDER_REJECTED", "POSITION_OPEN", "POSITION_RECONCILE", "POSITION_CLOSE",
    "EXIT", "PAUSE", "RESUME", "RECONNECT", "ERROR",
)


def log_event(store: Any, etype: str, run_id: str = "", cycle: int = 0,
              signal_id: str = "", decision_id: str = "",
              execution_id: str = "", ticket: int = 0, symbol: str = "",
              detail: Optional[Dict[str, Any]] = None) -> int:
    t = (etype or "").strip().upper()
    if t not in EVENT_TYPES:
        t = "ERROR"
        detail = dict(detail or {})
        detail.setdefault("bad_event_type", etype)
    try:
        return int(store.record_event(
            t, run_id=run_id, cycle=int(cycle or 0),
            signal_id=signal_id or "", decision_id=decision_id or "",
            execution_id=execution_id or "", ticket=int(ticket or 0),
            symbol=symbol or "", detail=dict(detail or {})))
    except Exception:
        return 0
