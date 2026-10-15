"""CSV OHLCV import for historical replay (stdlib only, deterministic).

Expected columns (case-insensitive, aliases accepted):
  timestamp | time | datetime | date        -> ISO-8601 or epoch seconds/ms
  open | o, high | h, low | l, close | c   -> floats > 0
  volume | vol | v                          -> float >= 0
  symbol (optional), interval (optional)

Rules:
- rows with missing/invalid OHLC are skipped and counted (never raise);
- rows are sorted by timestamp (out-of-order files replay deterministically);
- duplicate timestamps keep the first occurrence, counted as duplicates;
- naive datetimes are assumed UTC; epoch < 1e12 is seconds, else milliseconds.
"""

import csv
import glob
import logging
import math
from typing import List, Tuple, Dict, Optional, Sequence
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(raw: str) -> Optional[datetime]:
    s = (raw or "").strip()
    if not s:
        return None
    # epoch numeric?
    try:
        num = float(s)
        if num > 0:
            # ms vs s heuristic
            ts = num / 1000.0 if num >= 1e12 else num
            return datetime.fromtimestamp(ts, tz=timezone.utc)
    except ValueError:
        pass
    # ISO-8601 variants
    candidate = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


_ALIASES = {
    'timestamp': 'timestamp', 'time': 'timestamp', 'datetime': 'timestamp', 'date': 'timestamp',
    'open': 'open', 'o': 'open',
    'high': 'high', 'h': 'high',
    'low': 'low', 'l': 'low',
    'close': 'close', 'c': 'close', 'price': 'close',
    'volume': 'volume', 'vol': 'volume', 'v': 'volume',
    'symbol': 'symbol', 'interval': 'interval',
}


def load_ohlcv_csv(path: str, symbol: str = "BTC/USDT",
                    interval: str = "5m") -> Tuple[List, Dict[str, int]]:
    """Load candles from CSV. Returns (candles, stats).

    stats keys: rows, loaded, skipped_bad, duplicates, out_of_order_sorted(bool as int).
    Never raises for malformed rows; raises only if the file cannot be opened.
    """
    from market_data import Candle  # local import: avoids circulars at module load

    stats = {'rows': 0, 'loaded': 0, 'skipped_bad': 0, 'duplicates': 0,
             'out_of_order_sorted': 0}
    rows: List[Candle] = []
    seen_ts = set()
    out_of_order = False
    prev_ts = None

    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return [], stats
        norm_fields = {fn: _ALIASES.get(fn.strip().lower(), fn.strip().lower())
                       for fn in reader.fieldnames}
        for raw in reader:
            stats['rows'] += 1
            try:
                rec = {(norm_fields.get(k, k)): (v.strip() if isinstance(v, str) else v)
                       for k, v in raw.items()}
                ts = _parse_timestamp(str(rec.get('timestamp', '')))
                o = float(str(rec.get('open', '')).strip())
                h = float(str(rec.get('high', '')).strip())
                l = float(str(rec.get('low', '')).strip())
                cl = float(str(rec.get('close', '')).strip())
                vol_raw = rec.get('volume', '0')
                vol = float(str(vol_raw).strip() or 0.0)
                sym = str(rec.get('symbol', symbol) or symbol).strip()
                iv = str(rec.get('interval', interval) or interval).strip()
            except (ValueError, TypeError, AttributeError):
                stats['skipped_bad'] += 1
                continue
            if ts is None or not (o > 0 and h > 0 and l > 0 and cl > 0) or vol < 0:
                stats['skipped_bad'] += 1
                continue
            # Reject non-finite market data (NaN/Inf must never enter replay).
            if not (math.isfinite(o) and math.isfinite(h) and math.isfinite(l)
                    and math.isfinite(cl) and math.isfinite(vol)):
                stats['skipped_bad'] += 1
                continue
            # Basic OHLC sanity (log only, still accept — real feeds are messy).
            if h < max(o, cl) - 1e-9 or l > min(o, cl) + 1e-9:
                logger.debug("Suspicious OHLC row %d in %s (h=%s l=%s o=%s c=%s)",
                             stats['rows'], path, h, l, o, cl)
            key = (sym, ts.isoformat())
            if key in seen_ts:
                stats['duplicates'] += 1
                continue
            seen_ts.add(key)
            if prev_ts is not None and ts < prev_ts:
                out_of_order = True
            prev_ts = ts if prev_ts is None or ts > prev_ts else prev_ts
            rows.append(Candle(symbol=sym, open=o, high=h, low=l, close=cl,
                               volume=vol, timestamp=ts, interval=iv,
                               event_id=f"csv:{sym}:{ts.isoformat()}"))
    rows.sort(key=lambda c: c.timestamp)
    stats['loaded'] = len(rows)
    stats['out_of_order_sorted'] = int(out_of_order)
    logger.info("Loaded %d candles from %s (rows=%d bad=%d dup=%d)",
                stats['loaded'], path, stats['rows'],
                stats['skipped_bad'], stats['duplicates'])
    return rows, stats


def load_ohlcv_dataset(patterns: Sequence[str], symbol: str = "BTC/USDT",
                       interval: str = "5m") -> Tuple[List, Dict]:
    """Load and merge OHLCV candles from multiple files/periods.

    `patterns` may mix plain paths and glob patterns (stdlib `glob`).
    Files are processed in sorted order for determinism; candles are merged,
    globally sorted by timestamp, and deduplicated by (symbol, timestamp)
    keeping the first occurrence in (file_order, timestamp) order.

    Returns (candles, stats) with stats:
      files, per_file {path: {rows, loaded, skipped_bad, duplicates}},
      rows, loaded, skipped_bad, duplicates (within-file),
      cross_file_duplicates, out_of_order_sorted.
    Raises FileNotFoundError if no file matches.
    """
    from market_data import Candle  # local import: avoids circulars at module load

    resolved: List[str] = []
    for pat in patterns:
        matches = sorted(glob.glob(pat))
        if matches:
            resolved.extend(matches)
        else:
            resolved.append(pat)  # let the open() raise a clear error later
    # De-duplicate paths while preserving order.
    seen_paths = set()
    ordered_paths = []
    for p in resolved:
        if p not in seen_paths:
            seen_paths.add(p)
            ordered_paths.append(p)
    if not ordered_paths:
        raise FileNotFoundError(f"No files match patterns: {list(patterns)}")

    merged: List = []
    per_file: Dict[str, Dict[str, int]] = {}
    totals = {'rows': 0, 'skipped_bad': 0, 'duplicates': 0}
    for path in ordered_paths:
        try:
            candles, stats = load_ohlcv_csv(path, symbol=symbol, interval=interval)
        except FileNotFoundError:
            raise
        except OSError as e:
            raise FileNotFoundError(f"Cannot read dataset file {path}: {e}")
        per_file[path] = dict(stats)
        totals['rows'] += stats['rows']
        totals['skipped_bad'] += stats['skipped_bad']
        totals['duplicates'] += stats['duplicates']
        merged.extend(candles)

    out_of_order = int(any(
        b.timestamp < a.timestamp
        for a, b in zip(merged, merged[1:])  # files concatenated in order
    )) if len(merged) > 1 else 0
    # Global sort (stable) then cross-file dedup by (symbol, timestamp).
    merged.sort(key=lambda c: c.timestamp)
    deduped: List = []
    seen_ts = set()
    cross_dup = 0
    for c in merged:
        key = (c.symbol, c.timestamp.isoformat())
        if key in seen_ts:
            cross_dup += 1
            continue
        seen_ts.add(key)
        deduped.append(c)
    stats = {
        'files': len(ordered_paths),
        'per_file': per_file,
        'rows': totals['rows'],
        'loaded': len(deduped),
        'skipped_bad': totals['skipped_bad'],
        'duplicates': totals['duplicates'],
        'cross_file_duplicates': cross_dup,
        'out_of_order_sorted': out_of_order,
    }
    logger.info("Dataset merged: %d files -> %d candles "
                "(rows=%d bad=%d dup_in=%d dup_cross=%d)",
                len(ordered_paths), len(deduped), totals['rows'],
                totals['skipped_bad'], totals['duplicates'], cross_dup)
    return deduped, stats
