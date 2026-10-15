"""Local catalog-metadata cache for the Binance spot universe.

Caches CATALOG METADATA ONLY (canonical ids, symbols, quotes) with an
honest TTL. Never caches prices, quotes, or liveness: every startup uses
a valid cached catalog immediately for an instant registry, then refreshes
in the background. Stale prices are never served from this cache.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

CATALOG_TTL_S = 24 * 3600.0
CATALOG_VERSION = 1


def cache_path() -> Path:
    """Catalog cache location. FALCON_STATE_DIR overrides (tests)."""
    override = os.getenv("FALCON_STATE_DIR", "").strip()
    if override:
        return Path(override) / "binance_catalog.json"
    return Path.home() / ".falcon" / "binance_catalog.json"


def to_cache_entries(assets) -> List[Dict[str, str]]:
    """CanonicalAsset list -> JSON-safe metadata rows (no prices)."""
    out = []
    for a in assets:
        try:
            out.append({
                "id": str(a.id),
                "provider": str(a.provider),
                "venue": str(a.venue),
                "chain": str(a.chain),
                "symbol": str(a.symbol),
                "quote": str(a.quote),
            })
        except AttributeError:
            continue
    return out


def save(assets, path: Path | None = None) -> bool:
    """Persist catalog metadata. Returns success (never raises)."""
    dest = Path(path) if path else cache_path()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CATALOG_VERSION,
            "saved_at": time.time(),
            "assets": to_cache_entries(assets),
        }
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, dest)
        return True
    except OSError as e:
        logger.warning("catalog cache save failed (%s)", e)
        return False


def load(path: Path | None = None,
         ttl_s: float = CATALOG_TTL_S) -> Tuple[list, bool]:
    """Load cached metadata as CanonicalAsset list + freshness flag.

    Returns ([CanonicalAsset...], fresh_bool). Empty list when missing,
    corrupt, version-mismatched, or stale (caller decides whether stale
    rows are still useful as an instant placeholder).
    """
    from falcon_terminal.registry import CanonicalAsset
    from falcon_terminal.timeframes import TIMEFRAMES

    dest = Path(path) if path else cache_path()
    try:
        raw = dest.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != CATALOG_VERSION:
            return [], False
        saved_at = float(data.get("saved_at", 0.0))
        fresh = (time.time() - saved_at) < float(ttl_s)
        assets = []
        rows = data.get("assets", [])
        if not isinstance(rows, list):
            return [], False
        for r in rows[:5000]:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            try:
                assets.append(CanonicalAsset(
                    id=str(r["id"]), provider=str(r.get("provider", "binance")),
                    venue=str(r.get("venue", "spot")),
                    chain=str(r.get("chain", "")),
                    symbol=str(r.get("symbol", r["id"])),
                    quote=str(r.get("quote", "")),
                    supported=TIMEFRAMES if str(r.get("provider")) == "binance"
                    else ()))
            except (ValueError, TypeError):
                continue
        return assets, fresh
    except FileNotFoundError:
        return [], False
    except (OSError, ValueError) as e:
        logger.warning("catalog cache unreadable (%s); ignoring", e)
        return [], False


def cache_age_s(path: Path | None = None) -> float | None:
    """Seconds since the cache was saved, or None when unknown."""
    dest = Path(path) if path else cache_path()
    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
        return max(0.0, time.time() - float(data.get("saved_at", 0.0)))
    except (OSError, ValueError, TypeError):
        return None
