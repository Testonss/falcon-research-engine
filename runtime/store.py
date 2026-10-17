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
-- v0.2 acceptance closure (additive; never alters the four tables above)
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  type TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  cycle INTEGER NOT NULL DEFAULT 0,
  signal_id TEXT NOT NULL DEFAULT '',
  decision_id TEXT NOT NULL DEFAULT '',
  execution_id TEXT NOT NULL DEFAULT '',
  ticket INTEGER NOT NULL DEFAULT 0,
  symbol TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_decision ON events(decision_id);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket);
CREATE TABLE IF NOT EXISTS order_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id TEXT NOT NULL,
  symbol TEXT NOT NULL DEFAULT '',
  side TEXT NOT NULL DEFAULT '',
  volume REAL NOT NULL DEFAULT 0,
  price REAL NOT NULL DEFAULT 0,
  attempt INTEGER NOT NULL DEFAULT 1,
  outcome TEXT NOT NULL DEFAULT '',
  ticket INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
  ticket INTEGER PRIMARY KEY,
  symbol TEXT NOT NULL DEFAULT '',
  side TEXT NOT NULL DEFAULT '',
  volume REAL NOT NULL DEFAULT 0,
  entry_price REAL NOT NULL DEFAULT 0,
  stop_loss REAL NOT NULL DEFAULT 0,
  take_profit REAL NOT NULL DEFAULT 0,
  origin_decision_id TEXT NOT NULL DEFAULT '',
  origin_signal_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'OPEN',
  opened_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  closed_at TEXT NOT NULL DEFAULT '',
  close_reason TEXT NOT NULL DEFAULT '',
  realized_pnl REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS closed_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket INTEGER NOT NULL DEFAULT 0,
  symbol TEXT NOT NULL DEFAULT '',
  side TEXT NOT NULL DEFAULT '',
  volume REAL NOT NULL DEFAULT 0,
  entry_price REAL NOT NULL DEFAULT 0,
  exit_price REAL NOT NULL DEFAULT 0,
  realized_pnl REAL NOT NULL DEFAULT 0,
  exit_reason TEXT NOT NULL DEFAULT '',
  origin_decision_id TEXT NOT NULL DEFAULT '',
  opened_at TEXT NOT NULL DEFAULT '',
  closed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_state (
  day TEXT PRIMARY KEY,
  tz TEXT NOT NULL DEFAULT 'UTC',
  starting_equity REAL NOT NULL DEFAULT 0,
  peak_equity REAL NOT NULL DEFAULT 0,
  realized_pnl REAL NOT NULL DEFAULT 0,
  drawdown REAL NOT NULL DEFAULT 0,
  trade_count INTEGER NOT NULL DEFAULT 0,
  loss_count INTEGER NOT NULL DEFAULT 0,
  consecutive_losses INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cooldowns (
  key TEXT PRIMARY KEY,
  until_iso TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS trade_tape (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  symbol TEXT NOT NULL DEFAULT '',
  decision_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tape_symbol_at ON trade_tape(symbol, at);
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

    # -- pause modes (kill switch: NONE | NEW_ENTRIES | ALL) -------------
    def set_pause_mode(self, mode: str, reason: str = "") -> None:
        m = (mode or "NONE").strip().upper()
        if m not in ("NONE", "NEW_ENTRIES", "ALL"):
            raise ValueError(f"bad pause mode: {mode!r}")
        with self._lock:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES ('pause_mode',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (m,))
            if reason:
                self._db.execute(
                    "INSERT INTO meta (key, value) VALUES ('paused_reason',?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (reason,))
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES ('paused_at',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_utcnow_iso() if m != "NONE" else "",))
            # Legacy boolean flag mirrors mode for old callers.
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES ('paused',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if m != "NONE" else "0",))
            self._db.commit()

    def get_pause_mode(self) -> str:
        m = self.get_meta("pause_mode", "")
        if m in ("NONE", "NEW_ENTRIES", "ALL"):
            return m
        # Back-compat: legacy boolean flag.
        return "NEW_ENTRIES" if self.is_paused() else "NONE"

    # -- structured events ------------------------------------------------
    def record_event(self, etype: str, run_id: str = "", cycle: int = 0,
                     signal_id: str = "", decision_id: str = "",
                     execution_id: str = "", ticket: int = 0,
                     symbol: str = "", detail: Optional[Dict[str, Any]] = None) -> int:
        # Never persist secrets: drop known secret keys.
        det = dict(detail or {})
        for _k in ("password", "passwd", "token", "secret", "key"):
            det.pop(_k, None)
        try:
            blob = json.dumps(det)[:8000]
        except (TypeError, ValueError):
            blob = "{}"
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events (at, type, run_id, cycle, signal_id,"
                " decision_id, execution_id, ticket, symbol, detail)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_utcnow_iso(), str(etype), str(run_id), int(cycle),
                 str(signal_id or ""), str(decision_id or ""),
                 str(execution_id or ""), int(ticket or 0),
                 str(symbol or ""), blob))
            self._db.execute(
                "DELETE FROM events WHERE id NOT IN "
                "(SELECT id FROM events ORDER BY id DESC LIMIT 5000)")
            self._db.commit()
            return int(cur.lastrowid or 0)

    def list_events(self, limit: int = 200,
                    etype: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            if etype:
                rows = self._db.execute(
                    "SELECT * FROM events WHERE type=? ORDER BY id DESC LIMIT ?",
                    (etype, int(limit)))
            else:
                rows = self._db.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),))
            return [dict(r) for r in rows]

    def event_counts(self) -> Dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT type, COUNT(*) c FROM events GROUP BY type").fetchall()
            return {r["type"]: r["c"] for r in rows}

    # -- order attempts ---------------------------------------------------
    def record_attempt(self, decision_id: str, symbol: str, side: str,
                       volume: float, price: float, outcome: str,
                       ticket: int = 0, error: str = "") -> None:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) c FROM order_attempts WHERE decision_id=?",
                (decision_id,)).fetchone()
            n = int((row["c"] if row else 0) or 0) + 1
            self._db.execute(
                "INSERT INTO order_attempts (decision_id, symbol, side, volume,"
                " price, attempt, outcome, ticket, error, at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (decision_id, symbol, side, float(volume), float(price),
                 n, str(outcome), int(ticket or 0), str(error or "")[:1000],
                 _utcnow_iso()))
            self._db.commit()

    def list_attempts(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM order_attempts ORDER BY id DESC LIMIT ?",
                (int(limit),))]

    # -- positions / closed trades ----------------------------------------
    def upsert_position(self, ticket: int, symbol: str, side: str,
                        volume: float, entry_price: float,
                        stop_loss: float = 0.0, take_profit: float = 0.0,
                        origin_decision_id: str = "",
                        origin_signal_id: str = "",
                        status: str = "OPEN") -> None:
        now = _utcnow_iso()
        with self._lock:
            self._db.execute(
                "INSERT INTO positions (ticket, symbol, side, volume,"
                " entry_price, stop_loss, take_profit, origin_decision_id,"
                " origin_signal_id, status, opened_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(ticket) DO UPDATE SET symbol=excluded.symbol,"
                " side=excluded.side, volume=excluded.volume,"
                " entry_price=excluded.entry_price, stop_loss=excluded.stop_loss,"
                " take_profit=excluded.take_profit, status=excluded.status,"
                " updated_at=excluded.updated_at",
                (int(ticket), symbol, side, float(volume),
                 float(entry_price), float(stop_loss or 0.0),
                 float(take_profit or 0.0), str(origin_decision_id or ""),
                 str(origin_signal_id or ""), str(status or "OPEN"),
                 now, now))
            self._db.commit()

    def mark_position_closed(self, ticket: int, reason: str,
                             exit_price: float = 0.0,
                             realized_pnl: float = 0.0) -> None:
        now = _utcnow_iso()
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM positions WHERE ticket=?", (int(ticket),)).fetchone()
            pos = dict(row) if row else {}
            self._db.execute(
                "UPDATE positions SET status='CLOSED', closed_at=?,"
                " close_reason=?, realized_pnl=?, updated_at=? WHERE ticket=?",
                (now, str(reason or ""), float(realized_pnl or 0.0),
                 now, int(ticket)))
            self._db.execute(
                "INSERT INTO closed_trades (ticket, symbol, side, volume,"
                " entry_price, exit_price, realized_pnl, exit_reason,"
                " origin_decision_id, opened_at, closed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (int(ticket), str(pos.get("symbol", "")),
                 str(pos.get("side", "")), float(pos.get("volume", 0) or 0),
                 float(pos.get("entry_price", 0) or 0),
                 float(exit_price or 0), float(realized_pnl or 0.0),
                 str(reason or ""),
                 str(pos.get("origin_decision_id", "")),
                 str(pos.get("opened_at", "")), now))
            self._db.commit()

    def list_positions(self, status: str = "",
                       limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._db.execute(
                    "SELECT * FROM positions WHERE status=? ORDER BY ticket DESC"
                    " LIMIT ?", (status, int(limit)))
            else:
                rows = self._db.execute(
                    "SELECT * FROM positions ORDER BY ticket DESC LIMIT ?",
                    (int(limit),))
            return [dict(r) for r in rows]

    def list_closed_trades(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM closed_trades ORDER BY id DESC LIMIT ?",
                (int(limit),))]

    # -- daily risk state ---------------------------------------------------
    def get_daily(self, day: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM daily_state WHERE day=?", (day,)).fetchone()
            return dict(row) if row else None

    def put_daily(self, row: Dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO daily_state (day, tz, starting_equity, peak_equity,"
                " realized_pnl, drawdown, trade_count, loss_count,"
                " consecutive_losses) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(day) DO UPDATE SET tz=excluded.tz,"
                " starting_equity=excluded.starting_equity,"
                " peak_equity=excluded.peak_equity,"
                " realized_pnl=excluded.realized_pnl,"
                " drawdown=excluded.drawdown,"
                " trade_count=excluded.trade_count,"
                " loss_count=excluded.loss_count,"
                " consecutive_losses=excluded.consecutive_losses",
                (str(row.get("day", "")), str(row.get("tz", "UTC")),
                 float(row.get("starting_equity", 0) or 0),
                 float(row.get("peak_equity", 0) or 0),
                 float(row.get("realized_pnl", 0) or 0),
                 float(row.get("drawdown", 0) or 0),
                 int(row.get("trade_count", 0) or 0),
                 int(row.get("loss_count", 0) or 0),
                 int(row.get("consecutive_losses", 0) or 0)))
            self._db.commit()

    # -- cooldowns / backoff / tape ----------------------------------------
    def set_cooldown(self, key: str, until_iso: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO cooldowns (key, until_iso, updated_at)"
                " VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET until_iso=excluded.until_iso,"
                " updated_at=excluded.updated_at",
                (str(key), str(until_iso), _utcnow_iso()))
            self._db.commit()

    def get_cooldown(self, key: str) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT until_iso FROM cooldowns WHERE key=?", (str(key),)).fetchone()
            return str(row["until_iso"]) if row else ""

    def record_trade_tape(self, symbol: str, decision_id: str,
                          at_iso: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO trade_tape (at, symbol, decision_id) VALUES (?,?,?)",
                (at_iso or _utcnow_iso(), str(symbol), str(decision_id)))
            self._db.execute(
                "DELETE FROM trade_tape WHERE id NOT IN "
                "(SELECT id FROM trade_tape ORDER BY id DESC LIMIT 2000)")
            self._db.commit()

    def count_tape(self, symbol: str, since_iso: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) c FROM trade_tape WHERE symbol=? AND at>=?",
                (str(symbol), str(since_iso))).fetchone()
            return int((row["c"] if row else 0) or 0)

    def last_trade_at(self) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT at FROM trade_tape ORDER BY id DESC LIMIT 1").fetchone()
            return str(row["at"]) if row else ""

    def close(self) -> None:
        try:
            with self._lock:
                self._db.close()
        except Exception:
            pass
