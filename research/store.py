"""Local SQLite persistence for research state (stdlib only).

Stores campaigns, candidates (+ evaluations inline), rejection reasons,
paper sessions and idempotency keys. Every write commits immediately, so a
killed process loses nothing committed; reopening the same path recovers
all history. No failed experiment is ever deleted.
"""
import json
import logging
import math
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
  candidate_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  status TEXT NOT NULL,
  rejection_reason TEXT DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  passed INTEGER NOT NULL,
  reason TEXT DEFAULT '',
  metrics TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_candidate ON evaluations(candidate_id);
CREATE TABLE IF NOT EXISTS experiments (
  campaign_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  stage_reached TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'pending',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (campaign_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_exp_campaign ON experiments(campaign_id);
CREATE TABLE IF NOT EXISTS campaigns (
  campaign_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_sessions (
  paper_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key TEXT PRIMARY KEY,
  order_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(obj):
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float):
        if math.isinf(obj):
            return "inf" if obj > 0 else "-inf"
        if math.isnan(obj):
            return "nan"
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def dumps(payload: Any) -> str:
    return json.dumps(_json_safe(payload), sort_keys=True)


def loads(text: str) -> Any:
    return json.loads(text)


class ResearchStore:
    """SQLite-backed research store. Open with a file path for durability."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        with self._conn:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Additive migrations for databases created by older releases."""
        cols = {row[1] for row in
                self._conn.execute("PRAGMA table_info(evaluations)").fetchall()}
        if "campaign_id" not in cols:
            self._conn.execute(
                "ALTER TABLE evaluations ADD COLUMN campaign_id TEXT DEFAULT ''")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_eval_campaign "
                "ON evaluations(campaign_id)")

    def close(self) -> None:
        try:
            self._conn.commit()
            self._conn.close()
        except Exception:
            pass

    def transaction(self):
        """Context manager for a single atomic transaction.

        Usage: with store.transaction() as conn: ... (commits on clean exit,
        rolls back on exception). Correlated research writes that must not
        be split by a crash go through one of these blocks.
        """
        import contextlib

        @contextlib.contextmanager
        def _tx():
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise
        return _tx()

    # -- candidates ------------------------------------------------------
    def save_candidate(self, candidate) -> None:
        d = candidate.to_dict() if hasattr(candidate, "to_dict") else dict(candidate)
        with self._conn:
            self._conn.execute(
                "INSERT INTO candidates(candidate_id, payload, status, rejection_reason, updated_at)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(candidate_id) DO UPDATE SET payload=excluded.payload,"
                " status=excluded.status, rejection_reason=excluded.rejection_reason,"
                " updated_at=excluded.updated_at",
                (d["candidate_id"], dumps(d), d.get("status", ""),
                 d.get("rejection_reason", ""), _utcnow()))

    def load_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute("SELECT payload FROM candidates WHERE candidate_id=?",
                                 (candidate_id,))
        row = cur.fetchone()
        return loads(row[0]) if row else None

    def list_candidates(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            cur = self._conn.execute("SELECT payload FROM candidates WHERE status=? "
                                     "ORDER BY candidate_id", (status,))
        else:
            cur = self._conn.execute("SELECT payload FROM candidates ORDER BY candidate_id")
        return [loads(r[0]) for r in cur.fetchall()]

    def list_campaign_candidates(self, campaign_id: str) -> List[Dict[str, Any]]:
        """Candidates evaluated in one campaign (report isolation).

        A candidate belongs to a campaign iff it has at least one evaluation
        row tagged with that campaign_id. Legacy rows without a tag are
        visible only to unfiltered `list_candidates` calls, never here.
        """
        cur = self._conn.execute(
            "SELECT DISTINCT candidate_id FROM evaluations WHERE campaign_id=?",
            (campaign_id,))
        ids = sorted(r[0] for r in cur.fetchall())
        out = []
        for cid in ids:
            d = self.load_candidate(cid)
            if d is not None:
                out.append(d)
        return out

    # -- evaluations -----------------------------------------------------
    def save_evaluation(self, candidate_id: str, stage: str, passed: bool,
                        reason: str, metrics: Dict[str, Any],
                        campaign_id: str = "") -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO evaluations(candidate_id, stage, passed, reason, metrics,"
                " created_at, campaign_id)"
                " VALUES(?,?,?,?,?,?,?)",
                (candidate_id, stage, int(bool(passed)), reason or "",
                 dumps(metrics), _utcnow(), campaign_id or ""))

    def evaluations_for(self, candidate_id: str,
                        campaign_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if campaign_id is None:
            cur = self._conn.execute(
                "SELECT stage, passed, reason, metrics, created_at FROM evaluations"
                " WHERE candidate_id=? ORDER BY id", (candidate_id,))
        else:
            cur = self._conn.execute(
                "SELECT stage, passed, reason, metrics, created_at FROM evaluations"
                " WHERE candidate_id=? AND campaign_id=? ORDER BY id",
                (candidate_id, campaign_id))
        return [{"stage": s, "passed": bool(p), "reason": r,
                 "metrics": loads(m), "created_at": t}
                for s, p, r, m, t in cur.fetchall()]

    def delete_evaluations(self, candidate_id: str, campaign_id: str) -> int:
        """Remove one candidate's evaluations for a campaign (clean resume)."""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM evaluations WHERE candidate_id=? AND campaign_id=?",
                (candidate_id, campaign_id))
            return cur.rowcount

    def save_evaluation_bundle(self, candidate, stage: str, passed: bool,
                               reason: str, metrics: Dict[str, Any],
                               campaign_id: str = "",
                               exp_stage: str = "",
                               exp_state: str = "",
                               exp_fingerprint: str = "") -> None:
        """Atomically persist candidate + evaluation (+ experiment progress).

        A crash can never leave an evaluation without its candidate row, nor
        progress without its evaluation. Use for every campaign stage write.

        `exp_fingerprint` is the EXPERIMENT fingerprint (strategy x dataset
        x costs x gates x splits x seed x protocol). It defaults to the
        candidate (strategy) fingerprint only for backward compatibility;
        campaign code always passes the experiment fingerprint so the
        experiments table stays keyed by experiment identity.
        """
        d = candidate.to_dict() if hasattr(candidate, "to_dict") else dict(candidate)
        fp = exp_fingerprint or d.get("fingerprint", "")
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO candidates(candidate_id, payload, status, rejection_reason, updated_at)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(candidate_id) DO UPDATE SET payload=excluded.payload,"
                " status=excluded.status, rejection_reason=excluded.rejection_reason,"
                " updated_at=excluded.updated_at",
                (d["candidate_id"], dumps(d), d.get("status", ""),
                 d.get("rejection_reason", ""), _utcnow()))
            conn.execute(
                "INSERT INTO evaluations(candidate_id, stage, passed, reason, metrics,"
                " created_at, campaign_id)"
                " VALUES(?,?,?,?,?,?,?)",
                (d["candidate_id"], stage, int(bool(passed)), reason or "",
                 dumps(metrics), _utcnow(), campaign_id or ""))
            if exp_stage:
                conn.execute(
                    "INSERT INTO experiments(campaign_id, candidate_id, fingerprint,"
                    " stage_reached, state, updated_at)"
                    " VALUES(?,?,?,?,?,?)"
                    " ON CONFLICT(campaign_id, candidate_id) DO UPDATE SET"
                    " stage_reached=excluded.stage_reached, state=excluded.state,"
                    " updated_at=excluded.updated_at",
                    (campaign_id or "", d["candidate_id"],
                     fp, exp_stage, exp_state or "completed",
                     _utcnow()))

    # -- experiments (crash-recovery progress) -------------------------------
    def upsert_experiment(self, campaign_id: str, candidate_id: str,
                          fingerprint: str, stage_reached: str,
                          state: str) -> None:
        if state not in ("pending", "in_progress", "completed", "failed"):
            raise ValueError(f"invalid experiment state: {state!r}")
        with self._conn:
            self._conn.execute(
                "INSERT INTO experiments(campaign_id, candidate_id, fingerprint,"
                " stage_reached, state, updated_at)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(campaign_id, candidate_id) DO UPDATE SET"
                " stage_reached=excluded.stage_reached, state=excluded.state,"
                " updated_at=excluded.updated_at",
                (campaign_id, candidate_id, fingerprint, stage_reached,
                 state, _utcnow()))

    def get_experiment(self, campaign_id: str,
                       candidate_id: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT fingerprint, stage_reached, state, updated_at FROM experiments"
            " WHERE campaign_id=? AND candidate_id=?", (campaign_id, candidate_id))
        row = cur.fetchone()
        if not row:
            return None
        fp, stage, state, ts = row
        return {"campaign_id": campaign_id, "candidate_id": candidate_id,
                "fingerprint": fp, "stage_reached": stage, "state": state,
                "updated_at": ts}

    def find_completed_experiment_by_fingerprint(
            self, fingerprint: str,
            exclude_campaign_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Locate a COMPLETED experiment with this experiment fingerprint.

        Strategy identity and experiment identity differ: the same strategy
        (same strategy fingerprint / candidate) may legitimately take part
        in many experiments (different dataset, costs, gates, splits, seed).
        Only an identical experiment fingerprint means the work was already
        done. Used for cross-campaign dedup; the current campaign is
        excluded so same-campaign resume logic stays authoritative.
        Returns the matching row or None.
        """
        if exclude_campaign_id is None:
            cur = self._conn.execute(
                "SELECT campaign_id, candidate_id, fingerprint, stage_reached,"
                " state, updated_at FROM experiments"
                " WHERE fingerprint=? AND state='completed' LIMIT 1",
                (fingerprint,))
        else:
            cur = self._conn.execute(
                "SELECT campaign_id, candidate_id, fingerprint, stage_reached,"
                " state, updated_at FROM experiments"
                " WHERE fingerprint=? AND state='completed'"
                " AND campaign_id!=? LIMIT 1",
                (fingerprint, exclude_campaign_id))
        row = cur.fetchone()
        if not row:
            return None
        cid, cand_id, fp, stage, state, ts = row
        return {"campaign_id": cid, "candidate_id": cand_id,
                "fingerprint": fp, "stage_reached": stage, "state": state,
                "updated_at": ts}

    def list_experiments(self, campaign_id: str) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT candidate_id, fingerprint, stage_reached, state, updated_at"
            " FROM experiments WHERE campaign_id=? ORDER BY candidate_id",
            (campaign_id,))
        return [{"campaign_id": campaign_id, "candidate_id": cid, "fingerprint": fp,
                 "stage_reached": stage, "state": state, "updated_at": ts}
                for cid, fp, stage, state, ts in cur.fetchall()]

    # -- campaigns -------------------------------------------------------
    def save_campaign(self, campaign: Dict[str, Any]) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO campaigns(campaign_id, payload, updated_at) VALUES(?,?,?)"
                " ON CONFLICT(campaign_id) DO UPDATE SET payload=excluded.payload,"
                " updated_at=excluded.updated_at",
                (campaign["campaign_id"], dumps(campaign), _utcnow()))

    def load_campaign(self, campaign_id: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute("SELECT payload FROM campaigns WHERE campaign_id=?",
                                 (campaign_id,))
        row = cur.fetchone()
        return loads(row[0]) if row else None

    # -- paper sessions --------------------------------------------------
    def save_paper_session(self, session: Dict[str, Any]) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO paper_sessions(paper_id, payload, updated_at) VALUES(?,?,?)"
                " ON CONFLICT(paper_id) DO UPDATE SET payload=excluded.payload,"
                " updated_at=excluded.updated_at",
                (session["paper_id"], dumps(session), _utcnow()))

    def load_paper_session(self, paper_id: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute("SELECT payload FROM paper_sessions WHERE paper_id=?",
                                 (paper_id,))
        row = cur.fetchone()
        return loads(row[0]) if row else None

    # -- idempotency -----------------------------------------------------
    def claim_idempotency(self, key: str, order_id: str) -> bool:
        """Atomically claim key->order_id. True if first claim, False if seen."""
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO idempotency_keys(key, order_id, created_at)"
                    " VALUES(?,?,?)", (key, order_id, _utcnow()))
            return True
        except sqlite3.IntegrityError:
            return False

    def lookup_idempotency(self, key: str) -> Optional[str]:
        cur = self._conn.execute("SELECT order_id FROM idempotency_keys WHERE key=?",
                                 (key,))
        row = cur.fetchone()
        return row[0] if row else None

    def delete_idempotency_like(self, prefix: str) -> int:
        """Delete idempotency keys starting with `prefix` (resume cleanup).

        LIKE wildcards in the prefix are escaped so only the literal prefix
        matches. Returns the number of keys removed.
        """
        escaped = (prefix.replace("\\", "\\\\").replace("%", "\\%")
                   .replace("_", "\\_"))
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM idempotency_keys WHERE key LIKE ? ESCAPE '\\'",
                (escaped + "%",))
            return cur.rowcount
