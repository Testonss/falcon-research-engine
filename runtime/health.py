"""Heartbeat + health snapshot helpers (thin wrappers over RuntimeStore)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from runtime.store import RuntimeStore


def write_heartbeat(store: RuntimeStore, run_id: str, cycle: int,
                    state: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(state)
    payload.setdefault("at", datetime.now(timezone.utc).isoformat())
    store.write_heartbeat(run_id, cycle, payload)
    return payload


def health_snapshot(store: RuntimeStore, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    hb = store.last_heartbeat()
    snap: Dict[str, Any] = {
        "paused": store.is_paused(),
        "paused_reason": store.get_meta("paused_reason", ""),
        "run_id": store.get_meta("run_id", ""),
        "cycle": store.get_meta("cycle", "0"),
        "decisions": store.decision_counts(),
        "last_heartbeat": hb,
    }
    try:
        snap["pause_mode"] = store.get_pause_mode()
    except Exception:
        snap["pause_mode"] = "NONE" if not snap["paused"] else "NEW_ENTRIES"
    try:
        snap["runtime_state"] = store.get_meta("runtime_state", "")
    except Exception:
        snap["runtime_state"] = ""
    try:
        snap["events"] = store.event_counts()
    except Exception:
        snap["events"] = {}
    try:
        snap["open_positions"] = len(store.list_positions(status="OPEN", limit=5000))
        snap["closed_trades"] = len(store.list_closed_trades(limit=5000))
    except Exception:
        pass
    if extra:
        snap.update(extra)
    return snap
