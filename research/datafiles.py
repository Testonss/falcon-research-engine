"""Versioned historical datasets: validation, build, verification.

Pipeline: raw exchange dump (JSON from scripts/fetch_data.py)
  -> validate (explicit counts, nothing silently repaired)
  -> canonical CSV + manifest JSON (identity + provenance + sha256).

`load_versioned_dataset` re-hashes the CSV and refuses mismatches, so
history is never silently replaced under an existing version.
"""
import csv
import hashlib
import json
import logging
import math
import os
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800,
}

# Binance kline array positions.
K_OPEN_TIME, K_OPEN, K_HIGH, K_LOW, K_CLOSE, K_VOLUME = 0, 1, 2, 3, 4, 5

MAX_EXAMPLES = 5


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def validate_klines(rows: List[list], interval: str) -> Dict[str, Any]:
    """Validate raw kline rows. Returns a report; quarantines nothing here.

    Report keys: n_rows, n_parseable, bad_rows (+examples), duplicates,
    out_of_order, impossible_ohlc, negative_volume, gaps, missing_bars,
    largest_gap_bars, interval_ok_ratio, expected_step_s.
    """
    step = INTERVAL_SECONDS.get(interval)
    if step is None:
        raise ValueError(f"Unsupported interval: {interval!r}")
    rep: Dict[str, Any] = {
        "n_rows": len(rows), "n_parseable": 0, "bad_rows": 0,
        "bad_examples": [], "duplicates": 0, "out_of_order": 0,
        "impossible_ohlc": 0, "negative_volume": 0,
        "gaps": 0, "missing_bars": 0, "largest_gap_bars": 0,
        "interval_ok_ratio": 0.0, "expected_step_s": step,
    }
    parsed: List[Tuple[int, float, float, float, float, float]] = []
    ok_steps = 0
    total_steps = 0
    for i, r in enumerate(rows):
        try:
            ts = int(r[K_OPEN_TIME])
            o, h, low, c = (float(r[K_OPEN]), float(r[K_HIGH]),
                            float(r[K_LOW]), float(r[K_CLOSE]))
            v = float(r[K_VOLUME])
        except (ValueError, TypeError, IndexError):
            rep["bad_rows"] += 1
            if len(rep["bad_examples"]) < MAX_EXAMPLES:
                rep["bad_examples"].append({"index": i, "row": str(r)[:160]})
            continue
        rep["n_parseable"] += 1
        if not (o > 0 and h > 0 and low > 0 and c > 0):
            rep["bad_rows"] += 1
            continue
        if not (math.isfinite(o) and math.isfinite(h) and math.isfinite(low)
                and math.isfinite(c) and math.isfinite(v)):
            rep["bad_rows"] += 1
            continue
        if v < 0:
            rep["negative_volume"] += 1
        if h < max(o, c) - 1e-9 or low > min(o, c) + 1e-9:
            rep["impossible_ohlc"] += 1
        parsed.append((ts, o, h, low, c, v))
    # Order / duplicates / gaps, in received order (then sorted for gaps).
    seen = set()
    last_max = None
    for t in [x[0] for x in parsed]:
        if t in seen:
            rep["duplicates"] += 1
            continue
        seen.add(t)
        if last_max is not None and t < last_max:
            rep["out_of_order"] += 1
        last_max = t if last_max is None or t > last_max else last_max
    ordered_ts = sorted(seen)
    prev = None
    for t in ordered_ts:
        if prev is not None:
            diff = (t - prev) // 1000
            total_steps += 1
            if diff == step:
                ok_steps += 1
            elif diff > step:
                gap_bars = diff // step - 1
                rep["gaps"] += 1
                rep["missing_bars"] += gap_bars
                rep["largest_gap_bars"] = max(rep["largest_gap_bars"], gap_bars)
        prev = t
    rep["interval_ok_ratio"] = (ok_steps / total_steps) if total_steps else 0.0
    return rep


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _to_symbol_pair(symbol: str) -> str:
    """BINANCE 'BTCUSDT' -> research 'BTC/USDT' (known quote assets only)."""
    for quote in ("USDT", "USDC", "BTC", "ETH", "USD"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[:-len(quote)]}/{quote}"
    return symbol


def build_versioned_dataset(raw_path: str, out_csv: str, manifest_path: str,
                            symbol: Optional[str] = None,
                            interval: Optional[str] = None) -> Dict[str, Any]:
    """Validate raw dump -> canonical CSV + manifest. Returns the manifest.

    Quarantine policy (explicit, never silent): unparseable/non-positive rows
    are dropped and counted; duplicate timestamps keep the first and are
    counted; gaps are recorded, never filled.
    """
    with open(raw_path, encoding="utf-8") as f:
        raw = json.load(f)
    rows = raw.get("rows", [])
    symbol = symbol or _to_symbol_pair(raw.get("symbol", "UNKNOWN"))
    interval = interval or raw.get("interval", "1h")
    report = validate_klines(rows, interval)

    # Canonical rows: parseable, positive OHLC, first occurrence per timestamp.
    canonical: List[Tuple[int, float, float, float, float, float]] = []
    seen = set()
    for r in rows:
        try:
            ts = int(r[K_OPEN_TIME])
            o, h, low, c = (float(r[K_OPEN]), float(r[K_HIGH]),
                            float(r[K_LOW]), float(r[K_CLOSE]))
            v = float(r[K_VOLUME])
        except (ValueError, TypeError, IndexError):
            continue
        if not (o > 0 and h > 0 and low > 0 and c > 0) or v < 0:
            continue
        if not (math.isfinite(o) and math.isfinite(h) and math.isfinite(low)
                and math.isfinite(c) and math.isfinite(v)):
            continue
        if ts in seen:
            continue
        seen.add(ts)
        canonical.append((ts, o, h, low, c, max(v, 0.0)))
    canonical.sort(key=lambda t: t[0])

    os.makedirs(os.path.dirname(os.path.abspath(out_csv)), exist_ok=True)
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume",
                    "symbol", "interval"])
        for ts, o, h, low, c, v in canonical:
            w.writerow([_iso(ts), o, h, low, c, v, symbol, interval])
    digest = sha256_file(out_csv)
    manifest = {
        "dataset_id": f"falcon-{symbol.replace('/', '')}-{interval}-{digest[:12]}",
        "symbol": symbol,
        "interval": interval,
        "start": _iso(canonical[0][0]) if canonical else None,
        "end": _iso(canonical[-1][0]) if canonical else None,
        "rows": len(canonical),
        "source": raw.get("source", "unknown"),
        "raw_file": os.path.basename(raw_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sha256": digest,
        "schema_version": SCHEMA_VERSION,
        "validation": report,
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    logger.info("Built %s: %d rows (%s %s)", manifest["dataset_id"],
                len(canonical), symbol, interval)
    return manifest


def load_versioned_dataset(manifest_path: str, csv_path: Optional[str] = None):
    """Load canonical CSV after verifying its sha256 against the manifest.

    Raises ValueError on any mismatch — history is never silently replaced.
    Returns (candles, manifest).
    """
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported schema_version {manifest.get('schema_version')}")
    if csv_path is None:
        csv_path = os.path.join(os.path.dirname(os.path.abspath(manifest_path)),
                                os.path.basename(manifest_path).replace(".manifest.json", ".csv"))
    actual = sha256_file(csv_path)
    if actual != manifest.get("sha256"):
        raise ValueError(f"sha256 mismatch for {csv_path}: manifest "
                         f"{manifest.get('sha256')} != file {actual}")
    from market_data import load_ohlcv_csv
    candles, stats = load_ohlcv_csv(csv_path, symbol=manifest.get("symbol", "BTC/USDT"),
                                    interval=manifest.get("interval", "1h"))
    if len(candles) != manifest.get("rows"):
        raise ValueError(f"Row count changed for {csv_path}: manifest "
                         f"{manifest.get('rows')} != file {len(candles)}")
    return candles, manifest


def resample_ohlcv(candles, source_step_s: int, target_step_s: int, interval: str):
    """Aggregate bars into exact elapsed-time buckets (causal-safe).

    Bars are floored onto the UTC grid ``floor(ts/target)*target``; each
    bucket spans exactly ``target_step_s`` seconds. Only buckets containing
    exactly ``target_step_s // source_step_s`` contiguous constituents are
    emitted — partial buckets (edge gaps) are dropped and counted, never
    padded or forward-filled.

    Returns ``(bars, stats)`` with stats {buckets, dropped_partial,
    dropped_bars, out_of_order_sorted}. Raises ValueError on bad steps
    (non-positive, or target not an integer multiple of source).

    O=max(open), H=max(high), L=min(low), C=last close, V=sum(volume).
    Output timestamp is the bucket START (grid-aligned). Event ids derive
    from constituent bars only.
    """
    from market_data import Candle
    try:
        source_step_s = int(source_step_s)
        target_step_s = int(target_step_s)
    except (TypeError, ValueError):
        raise ValueError(f"steps must be integers, got {source_step_s!r}/{target_step_s!r}")
    if source_step_s <= 0 or target_step_s <= 0:
        raise ValueError(f"steps must be positive, got {source_step_s}/{target_step_s}")
    if target_step_s % source_step_s != 0:
        raise ValueError(f"target step {target_step_s}s is not a multiple of "
                         f"source step {source_step_s}s")
    factor = target_step_s // source_step_s
    bars = sorted(candles, key=lambda c: c.timestamp)
    out_of_order = 0
    seen = set()
    buckets: Dict[int, list] = {}
    for c in candles:
        key = (c.symbol, c.timestamp.isoformat())
        if key in seen:
            continue
        seen.add(key)
        epoch = int(c.timestamp.timestamp())
        start = (epoch // target_step_s) * target_step_s
        buckets.setdefault((c.symbol, start), []).append(c)
    # Detect raw out-of-order delivery (pre-sort).
    last_ts = None
    for c in candles:
        if last_ts is not None and c.timestamp < last_ts:
            out_of_order = 1
            break
        last_ts = c.timestamp
    out = []
    dropped_partial = 0
    dropped_bars = 0
    for (symbol, start) in sorted(buckets.keys(), key=lambda k: (k[0], k[1])):
        chunk = sorted(buckets[(symbol, start)], key=lambda c: c.timestamp)
        span_ok = (int(chunk[-1].timestamp.timestamp())
                   - int(chunk[0].timestamp.timestamp()) == target_step_s - source_step_s)
        if len(chunk) != factor or not span_ok:
            dropped_partial += 1
            dropped_bars += len(chunk)
            continue
        first, last = chunk[0], chunk[-1]
        bar_ts = datetime.fromtimestamp(start, tz=timezone.utc)
        out.append(Candle(
            symbol=symbol,
            open=first.open,
            high=max(c.high for c in chunk),
            low=min(c.low for c in chunk),
            close=last.close,
            volume=sum(c.volume for c in chunk),
            timestamp=bar_ts,
            interval=interval,
            event_id=f"resampled:{symbol}:{interval}:{bar_ts.isoformat()}",
        ))
    stats = {"buckets": len(out), "dropped_partial": dropped_partial,
             "dropped_bars": dropped_bars, "out_of_order_sorted": out_of_order,
             "factor": factor, "source_step_s": source_step_s,
             "target_step_s": target_step_s}
    return out, stats
