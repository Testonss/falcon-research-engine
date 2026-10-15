#!/usr/bin/env python3
"""Build versioned Falcon datasets from raw exchange dumps (deterministic).

Steps per symbol:
  1. raw JSON (scripts/fetch_data.py) -> validate -> canonical 1h CSV + manifest
  2. canonical 1h candles -> time-bucket resample -> 4h CSV + manifest

Nothing is forward-filled; partial edge buckets are dropped and counted.
Re-running on the same raw files yields byte-identical CSVs and manifests
(modulo the manifest `created_at` field, which is provenance metadata).

Example:
  python scripts/build_dataset.py --raw data/raw/BINANCE_BTCUSDT_1h.json
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from research.datafiles import (
    build_versioned_dataset, load_versioned_dataset, resample_ohlcv,
    sha256_file, SCHEMA_VERSION, INTERVAL_SECONDS,
)


def write_canonical_csv(candles, symbol, interval, out_csv):
    os.makedirs(os.path.dirname(os.path.abspath(out_csv)), exist_ok=True)
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume",
                    "symbol", "interval"])
        for c in candles:
            w.writerow([c.timestamp.isoformat(), c.open, c.high, c.low,
                        c.close, c.volume, symbol, interval])


def build_resampled_manifest(csv_path, symbol, interval, source_manifest,
                             factor, dropped_stats):
    digest = sha256_file(csv_path)
    manifest = {
        "dataset_id": f"falcon-{symbol.replace('/', '')}-{interval}-{digest[:12]}",
        "symbol": symbol,
        "interval": interval,
        "source": f"resampled {factor}x from {source_manifest['dataset_id']}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sha256": digest,
        "schema_version": SCHEMA_VERSION,
        "validation": {"resampled_from_rows": dropped_stats["from_rows"],
                       "factor": factor,
                       "dropped_partial": dropped_stats["dropped_partial"],
                       "dropped_bars": dropped_stats["dropped_bars"]},
    }
    # Range/rows are filled in by the caller after writing the CSV.
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build versioned 1h + resampled datasets.")
    ap.add_argument('--raw', required=True, help="raw JSON from fetch_data.py")
    ap.add_argument('--symbol', default=None, help="override pair symbol (default: from raw)")
    ap.add_argument('--interval', default='1h', help="canonical interval of the raw dump")
    ap.add_argument('--resample-to', nargs='*', default=['4h'],
                    help="extra timeframes built by time-bucketing (default: 4h)")
    ap.add_argument('--out-dir', default='data')
    args = ap.parse_args(argv)

    base = os.path.splitext(os.path.basename(args.raw))[0]
    tag = base.replace('BINANCE_', '')
    csv1 = os.path.join(args.out_dir, f"falcon-{tag}.csv")
    man1 = os.path.join(args.out_dir, f"falcon-{tag}.manifest.json")
    manifest = build_versioned_dataset(args.raw, csv1, man1,
                                       symbol=args.symbol, interval=args.interval)
    print(f"built {manifest['dataset_id']}: {manifest['rows']} rows "
          f"{manifest['start'][:10]} -> {manifest['end'][:10]}")
    candles, manifest = load_versioned_dataset(man1)
    src_step = INTERVAL_SECONDS[manifest['interval']]
    made = [(csv1, man1)]
    for target in (args.resample_to or []):
        if target not in INTERVAL_SECONDS:
            print(f"falcon: error: unsupported interval {target!r}", file=sys.stderr)
            return 2
        tgt_step = INTERVAL_SECONDS[target]
        bars, stats = resample_ohlcv(candles, src_step, tgt_step, target)
        # Name resampled files falcon-<SYM>-<target>.csv regardless of raw name.
        sym_noslash = manifest['symbol'].replace('/', '')
        out_csv = os.path.join(args.out_dir, f"falcon-{sym_noslash}-{target}.csv")
        man_path = out_csv.replace('.csv', '.manifest.json')
        write_canonical_csv(bars, manifest['symbol'], target, out_csv)
        man2 = build_resampled_manifest(out_csv, manifest['symbol'], target, manifest,
                                        tgt_step // src_step,
                                        {"from_rows": len(candles),
                                         "dropped_partial": stats["dropped_partial"],
                                         "dropped_bars": stats["dropped_bars"]})
        man2["start"] = bars[0].timestamp.isoformat() if bars else None
        man2["end"] = bars[-1].timestamp.isoformat() if bars else None
        man2["rows"] = len(bars)
        with open(man_path, "w", encoding="utf-8") as f:
            json.dump(man2, f, indent=2, sort_keys=True)
        print(f"built {man2['dataset_id']}: {len(bars)} rows "
              f"(dropped_partial={stats['dropped_partial']})")
        made.append((out_csv, man_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
