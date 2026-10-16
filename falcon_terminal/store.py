"""Local terminal state: favorites, recently viewed, selected asset.

Plain JSON file, atomic writes (tmp + replace), never crashes the app:
corrupt files fall back to empty state and are rewritten on next save.
Favorites reference canonical asset ids (no duplicated asset state).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_RECENT = 20


def state_path() -> Path:
    """State file location. FALCON_STATE_DIR overrides (tests)."""
    override = os.getenv("FALCON_STATE_DIR", "").strip()
    if override:
        return Path(override) / "terminal.json"
    return Path.home() / ".falcon" / "terminal.json"


class LocalStore:
    """In-memory state with optional JSON persistence.

    path=None keeps everything in memory (tests, no disk side effects).
    Pass an explicit path (production) to persist across restarts.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        self.favorites: List[str] = []
        self.recent: List[str] = []
        self.selected: str = ""
        # MT5 demo settings (no passwords ever persisted here).
        self.mt5_enabled: bool = False
        self.mt5_terminal_path: str = ""
        self.mt5_sync_interval: float = 5.0
        self.mt5_default_volume: float = 0.01
        self.mt5_max_risk: float = 100.0
        self.load()

    # -- persistence ---------------------------------------------------------
    def load(self) -> None:
        if self.path is None:
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not a dict")
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.warning("terminal state unreadable (%s); starting fresh", e)
            return
        favs = data.get("favorites", [])
        if isinstance(favs, list):
            self.favorites = [str(x) for x in favs if isinstance(x, str)][:500]
        rec = data.get("recent", [])
        if isinstance(rec, list):
            self.recent = [str(x) for x in rec if isinstance(x, str)][:MAX_RECENT]
        sel = data.get("selected", "")
        self.selected = sel if isinstance(sel, str) else ""
        # MT5 demo settings (validated ranges; passwords never accepted).
        try:
            self.mt5_enabled = bool(data.get("mt5_enabled", False))
        except Exception:
            self.mt5_enabled = False
        tp = data.get("mt5_terminal_path", "")
        self.mt5_terminal_path = tp if isinstance(tp, str) else ""
        for key, lo, hi, default in (
                ("mt5_sync_interval", 2.0, 60.0, 5.0),
                ("mt5_default_volume", 0.01, 100.0, 0.01),
                ("mt5_max_risk", 1.0, 100000.0, 100.0)):
            try:
                v = float(data.get(key, default))
                if not (lo <= v <= hi):
                    raise ValueError("range")
                setattr(self, key, v)
            except (TypeError, ValueError):
                setattr(self, key, default)

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "favorites": self.favorites,
                "recent": self.recent,
                "selected": self.selected,
                "mt5_enabled": self.mt5_enabled,
                "mt5_terminal_path": self.mt5_terminal_path,
                "mt5_sync_interval": self.mt5_sync_interval,
                "mt5_default_volume": self.mt5_default_volume,
                "mt5_max_risk": self.mt5_max_risk,
            }, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("terminal state save failed (%s)", e)

    # -- mutations (caller saves) ----------------------------------------------
    def toggle_favorite(self, cid: str) -> bool:
        """Toggle; returns new favorite state. Unknown ids still tracked? No:
        only non-empty ids; registry membership is the caller's concern."""
        if not isinstance(cid, str) or not cid:
            return False
        if cid in self.favorites:
            self.favorites.remove(cid)
            return False
        self.favorites.append(cid)
        return True

    def is_favorite(self, cid: str) -> bool:
        return cid in self.favorites

    def touch_recent(self, cid: str) -> None:
        if not isinstance(cid, str) or not cid:
            return
        if cid in self.recent:
            self.recent.remove(cid)
        self.recent.insert(0, cid)
        del self.recent[MAX_RECENT:]

    def set_selected(self, cid: str) -> None:
        self.selected = cid if isinstance(cid, str) else ""

    def to_dict(self) -> Dict[str, object]:
        return {"favorites": list(self.favorites), "recent": list(self.recent),
                "selected": self.selected,
                "mt5_enabled": self.mt5_enabled,
                "mt5_terminal_path": self.mt5_terminal_path,
                "mt5_sync_interval": self.mt5_sync_interval,
                "mt5_default_volume": self.mt5_default_volume,
                "mt5_max_risk": self.mt5_max_risk}
