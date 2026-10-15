#!/usr/bin/env python3
"""Download public OHLCV klines from Binance REST (no API key required).

Paginates /api/v3/klines (1000 rows/request) with retries and polite
rate-limiting, then stores RAW responses plus fetch metadata as JSON.
Validation/versioning happens separately in research/datafiles.py.

Example:
  python scripts/fetch_data.py --symbols BTCUSDT ETHUSDT --interval 1h \
      --start 2021-01-01 --end 2025-01-01 --out data/raw
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE_URL = "https://api.binance.com"
KLINES_PATH = "/api/v3/klines"
PAGE_LIMIT = 1000


def parse_date(s: str) -> dt.datetime:
    try:
        d = dt.datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"date must be YYYY-MM-DD, got {s!r}")
    return d.replace(tzinfo=dt.timezone.utc)


def to_ms(d: dt.datetime) -> int:
    return int(d.timestamp() * 1000)


def fetch_page(symbol: str, interval: str, start_ms: int, end_ms: int,
               retries: int = 5) -> list:
    params = urllib.parse.urlencode({
        "symbol": symbol, "interval": interval, "limit": PAGE_LIMIT,
        "startTime": start_ms, "endTime": end_ms,
    })
    url = BASE_URL + KLINES_PATH + "?" + params
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "falcon-research/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status != 200:
                    raise IOError(f"HTTP {r.status}")
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - retry transient network errors
            last_err = e
            time.sleep(min(2 ** attempt, 16))
    raise IOError(f"klines fetch failed for {symbol} {interval}: {last_err}")


def download(symbol: str, interval: str, start: dt.datetime, end: dt.datetime,
             sleep_s: float, verbose: bool = True) -> dict:
    start_ms, end_ms = to_ms(start), to_ms(end)
    rows: list = []
    pages = 0
    cursor = start_ms
    while True:
        page = fetch_page(symbol, interval, cursor, end_ms)
        pages += 1
        if not page:
            break
        # De-duplicate overlap at page boundaries by open time.
        seen = {r[0] for r in rows} if rows else set()
        fresh = [r for r in page if r[0] not in seen]
        rows.extend(fresh)
        last_open = page[-1][0]
        if verbose:
            print(f"  {symbol} {interval}: page {pages}, +{len(fresh)} rows "
                  f"(through {dt.datetime.fromtimestamp(last_open / 1000, dt.timezone.utc):%Y-%m-%d})")
        if len(page) < PAGE_LIMIT:
            break
        cursor = last_open + 1
        if cursor >= end_ms:
            break
        time.sleep(sleep_s)
    rows.sort(key=lambda r: r[0])
    return {
        "symbol": symbol, "interval": interval,
        "requested_start": start.isoformat(), "requested_end": end.isoformat(),
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "api.binance.com/api/v3/klines (public, no key)",
        "pages": pages, "rows": rows,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Download public Binance klines (no key).")
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--start", type=parse_date, default=parse_date("2021-01-01"))
    ap.add_argument("--end", type=parse_date, default=parse_date("2025-01-01"))
    ap.add_argument("--out", default="data/raw")
    ap.add_argument("--sleep", type=float, default=0.4)
    ap.add_argument("--force", action="store_true",
                    help="re-fetch even if the output file already exists")
    args = ap.parse_args(argv)
    if args.start >= args.end:
        print("error: --start must precede --end", file=sys.stderr)
        return 2
    os.makedirs(args.out, exist_ok=True)
    for symbol in args.symbols:
        path = os.path.join(args.out, f"BINANCE_{symbol}_{args.interval}.json")
        if os.path.exists(path) and not args.force:
            print(f"exists, skipping (delete to re-fetch, or pass --force): {path}")
            continue
        print(f"downloading {symbol} {args.interval} ...")
        try:
            payload = download(symbol, args.interval, args.start, args.end, args.sleep)
        except IOError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
        print(f"wrote {path} ({len(payload['rows'])} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
