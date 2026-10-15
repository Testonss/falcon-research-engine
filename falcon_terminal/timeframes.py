"""Deterministic OHLCV timeframe math. Pure functions, no I/O.

Rules:
- bucket boundaries are epoch-aligned (UTC), never interpolated;
- only CLOSED input bars aggregate into closed output bars; a provisional
  (forming) input bar may contribute to an explicitly flagged forming
  output bucket, never to a closed one;
- closed output bars are immutable: re-aggregating a longer prefix yields
  the same closed bars plus (at most) a new tail;
- merging never mutates a cached closed bar (keyed by open time).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1D")

TF_SECONDS: Dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1D": 86400,
}

# Freshness: full refetch TTL per timeframe (seconds).
TF_TTL: Dict[str, float] = {
    "1m": 60.0,
    "5m": 180.0,
    "15m": 600.0,
    "1h": 1800.0,
    "4h": 3600.0,
    "1D": 7200.0,
}

# Minimum seconds between live tail refreshes of the active view.
TAIL_REFRESH_S = 20.0


def bucket_start(open_ms: int, timeframe: str) -> int:
    """Epoch-aligned bucket open (ms) for a bar open time. UTC, no guessing."""
    if timeframe not in TF_SECONDS:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    try:
        ms = int(open_ms)
    except (TypeError, ValueError):
        raise ValueError(f"invalid open_ms: {open_ms!r}")
    span = TF_SECONDS[timeframe] * 1000
    return (ms // span) * span


def _valid_ohlcv(o: float, h: float, l: float, c: float, v: float) -> bool:
    try:
        o, h, l, c, v = float(o), float(h), float(l), float(c), float(v)
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(x) for x in (o, h, l, c, v)):
        return False
    if not (o > 0 and h > 0 and l > 0 and c > 0) or v < 0:
        return False
    return l <= min(o, c) and h >= max(o, c) and h >= l


def aggregate_ohlcv(base: List[dict], target: str,
                    now_ms: Optional[int] = None) -> Tuple[List[dict], Optional[dict]]:
    """Aggregate finer bars into `target` buckets.

    Input bars: dicts with open_ms, o/h/l/c/v and optional closed flag
    (default True; bars explicitly marked closed=False are forming).
    Returns (closed_bars, forming_bar_or_None), oldest first. Each bar is a
    dict {open_ms, o, h, l, c, v, closed}. Buckets with no bars are SKIPPED
    (never interpolated). A bucket still open at `now_ms` stays forming.
    Malformed input bars are skipped, never crash.
    """
    if target not in TF_SECONDS:
        raise ValueError(f"unsupported timeframe: {target!r}")
    span = TF_SECONDS[target] * 1000
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    buckets: Dict[int, dict] = {}
    order: List[int] = []
    for b in base:
        try:
            if not isinstance(b, dict):
                continue
            oms = int(b["open_ms"])
            o, h, l, c, v = (float(b["o"]), float(b["h"]), float(b["l"]),
                             float(b["c"]), float(b["v"]))
            forming_in = b.get("closed", True) is False
        except (KeyError, TypeError, ValueError):
            continue
        if not _valid_ohlcv(o, h, l, c, v):
            continue
        key = (oms // span) * span
        slot = buckets.get(key)
        if slot is None:
            slot = {"open_ms": key, "o": o, "h": h, "l": l, "c": c,
                    "v": v, "_forming": forming_in}
            buckets[key] = slot
            order.append(key)
        else:
            # Bar order within a bucket follows input order for open/close;
            # high/low/volume accumulate regardless of order.
            slot["h"] = max(slot["h"], h)
            slot["l"] = min(slot["l"], l)
            slot["v"] = slot["v"] + v
            slot["c"] = c
            slot["_forming"] = slot["_forming"] or forming_in
    closed: List[dict] = []
    forming: Optional[dict] = None
    for key in sorted(order):
        slot = buckets[key]
        is_open_bucket = (key + span) > now_ms
        out = {"open_ms": key, "o": slot["o"], "h": slot["h"], "l": slot["l"],
               "c": slot["c"], "v": slot["v"]}
        if slot["_forming"] or is_open_bucket:
            out["closed"] = False
            # Only the latest forming bucket survives; earlier provisional
            # buckets are dropped rather than presented as history.
            if forming is None or key >= forming["open_ms"]:
                forming = out
        else:
            out["closed"] = True
            closed.append(out)
    return closed, forming


def merge_candle_series(cached: List[dict],
                        fresh: List[dict]) -> Tuple[List[dict], Optional[dict]]:
    """Merge a fresh fetch into cached bars without mutating closed history.

    Both lists: dicts {open_ms, o/h/l/c/v, closed}. Cached closed bars win
    key conflicts (immutability); fresh bars extend the tail; the fresh
    forming bar (if any) becomes the forming output. Returns
    (closed_sorted, forming_or_None).
    """
    known: Dict[int, dict] = {}
    for b in cached:
        try:
            if isinstance(b, dict) and b.get("closed", True):
                known[int(b["open_ms"])] = dict(b)
        except (KeyError, TypeError, ValueError):
            continue
    fresh_forming: Optional[dict] = None
    for b in fresh:
        try:
            if not isinstance(b, dict):
                continue
            key = int(b["open_ms"])
            if b.get("closed", True) is False:
                if fresh_forming is None or key >= fresh_forming["open_ms"]:
                    fresh_forming = dict(b)
                continue
            if key not in known:
                known[key] = dict(b)
        except (KeyError, TypeError, ValueError):
            continue
    closed = [known[k] for k in sorted(known)]
    return closed, fresh_forming
