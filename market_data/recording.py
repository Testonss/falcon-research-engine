"""Append-safe JSONL recording of normalized live events.

Each line: {"schema": 1, "provider": ..., "instrument": ..., "type": ...,
"event_time": iso, "receive_time": iso, "event_id": ..., "payload": {...}}.
Writes are atomic per line (single write + flush); a torn process kill can
at worst leave a partial last line, which the reader skips and counts.
Normal shutdown never leaves partial records: close() flushes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

SCHEMA_VERSION = 1


class LiveEventRecorder:
    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "a", encoding="utf-8")
        self.records = 0

    def record(self, provider: str, instrument: str, etype: str,
               event_time: datetime, event_id: Optional[str],
               payload: Dict[str, Any]):
        rec = {"schema": SCHEMA_VERSION, "provider": provider,
               "instrument": instrument, "type": etype,
               "event_time": event_time.astimezone(timezone.utc).isoformat(),
               "receive_time": datetime.now(timezone.utc).isoformat(),
               "event_id": event_id, "payload": payload}
        line = json.dumps(rec, sort_keys=True)
        self._fh.write(line + "\n")
        self._fh.flush()
        self.records += 1

    def close(self):
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def read_records(path: str):
    """Read back records; skips corrupt/torn trailing lines, returns (rows, skipped)."""
    rows = []
    skipped = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                skipped += 1
    return rows, skipped
