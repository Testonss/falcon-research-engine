"""SQLite persistence + crash recovery for the autonomous demo runtime.

Schema (created idempotently, stdlib ``sqlite3`` only):

  decisions   decision_id PK, symbol, action, volume, price, reason,
              blocked_by, bar_time, created_at, executed (0/1), ticket
  fills       id PK, decision_id, ticket, symbol, side, volume, price, at
  heartbeats  id PK, run_id, cycle, at, state_json
  meta        key PK, value  (run_id, paused, cycle, updated_at, ...)

Anti-duplicate guarantee: ``decisions.decision_id`` is the primary key and
all inserts use ``INSERT OR IGNORE``; a restarted runtime replays history
from this store and never re-sends a decision already marked executed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  action TEXT NOT NULL,
  volume REAL NOT NULL DEFAULT 0,
  price REAL NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  blocked_by TEXT,
  bar_time TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  executed INTEGER NOT NULL DEFAULT 0,
  ticket INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id TEXT NOT NULL,
  ticket INTEGER NOT NULL DEFAULT 0,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  volume REAL NOT NULL,
  price REAL NOT NULL,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS heartbeats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  cycle INTEGER NOT NULL,
  at TEXT NOT NULL,
  state_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);
"""


class RuntimeStore:
    """Thread-safe SQLite store. One instance per runtime process."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # -- decisions ------------------------------------------------------
    def record_decision(self, decision_id: str, symbol: str, action: str,
                        volume: float, price: float, reason: str,
                        blocked_by: Optional[str], bar_time: str) -> bool:
        """Insert; returns False when the id already existed (duplicate)."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO decisions "
                "(decision_id, symbol, action, volume, price, reason,"
                " blocked_by, bar_time, created_at, executed, ticket)"
                " VALUES (?,?,?,?,?,?,?,?,?,0,0)",
                (decision_id, symbol, action, float(volume), float(price),
                 reason, blocked_by, bar_time, _utcnow_iso()))
            self._db.commit()
            return cur.rowcount == 1

    def mark_executed(self, decision_id: str, ticket: int = 0) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE decisions SET executed=1, ticket=? WHERE decision_id=?",
                (int(ticket or 0), decision_id))
            self._db.commit()

    def is_executed(self, decision_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT executed FROM decisions WHERE decision_id=?",
                (decision_id,)).fetchone()
            return bool(row and row["executed"])

    def has_decision(self, decision_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM decisions WHERE decision_id=?",
                (decision_id,)).fetchone()
            return row is not None

    def executed_decision_ids(self) -> List[str]:
        with self._lock:
            return [r["decision_id"] for r in self._db.execute(
                "SELECT decision_id FROM decisions WHERE executed=1")]

    def pending_decisions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM decisions WHERE executed=0 ORDER BY created_at")]

    def decision_counts(self) -> Dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT action, COUNT(*) c FROM decisions GROUP BY action").fetchall()
            out = {r["action"]: r["c"] for r in rows}
            out["executed"] = self._db.execute(
                "SELECT COUNT(*) c FROM decisions WHERE executed=1").fetchone()["c"]
            out["total"] = self._db.execute(
                "SELECT COUNT(*) c FROM decisions").fetchone()["c"]
            return out

    # -- fills ----------------------------------------------------------
    def record_fill(self, decision_id: str, ticket: int, symbol: str,
                    side: str, volume: float, price: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO fills (decision_id, ticket, symbol, side, volume, price, at)"
                " VALUES (?,?,?,?,?,?,?)",
                (decision_id, int(ticket or 0), symbol, side,
                 float(volume), float(price), _utcnow_iso()))
            self._db.commit()

    def list_fills(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM fills ORDER BY id DESC LIMIT ?", (int(limit),))]

    # -- heartbeats -----------------------------------------------------
    def write_heartbeat(self, run_id: str, cycle: int, state: Dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO heartbeats (run_id, cycle, at, state_json)"
                " VALUES (?,?,?,?)",
                (run_id, int(cycle), _utcnow_iso(), json.dumps(state)))
            # Bound growth: keep the newest 500 heartbeats.
            self._db.execute(
                "DELETE FROM heartbeats WHERE id NOT IN "
                "(SELECT id FROM heartbeats ORDER BY id DESC LIMIT 500)")
            self._db.commit()

    def last_heartbeat(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM heartbeats ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                return None
            d = dict(row)
            try:
                d["state"] = json.loads(d.get("state_json") or "{}")
            except (TypeError, ValueError):
                d["state"] = {}
            return d

    # -- meta (pause flag, run/cycle cursors) ---------------------------
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
            self._db.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_paused(self, paused: bool, reason: str = "") -> None:
        self.set_meta("paused", "1" if paused else "0")
        if reason:
            self.set_meta("paused_reason", reason)
        self.set_meta("paused_at", _utcnow_iso() if paused else "")

    def is_paused(self) -> bool:
        return self.get_meta("paused", "0") == "1"

    def close(self) -> None:
        try:
            with self._lock:
                self._db.close()
        except Exception:
            pass
