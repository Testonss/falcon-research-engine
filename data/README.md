# Falcon data directory

## Committed (public v0.1.0)

- `sample_ohlcv.csv`, `sample_ohlcv_2024-02.csv` — small demo samples.
  `sample_ohlcv.csv` powers the README Quick start.
- `falcon-<SYM>-1h.csv` + `.manifest.json` — canonical 1h datasets:
  BTC/ETH from 2017-08-17, BNB from 2017-11-06, SOL from 2020-08-11,
  all to 2025-01-01.
- `falcon-<SYM>-4h.csv` + `.manifest.json` — deterministic 4h resamples
  (exact elapsed-time buckets; partial edge buckets dropped and counted
  in the manifest).

Every manifest pins a sha256 verified on each load (`research/datafiles.py`).

## Never committed

- `data/raw/` — reproducible Binance kline dumps via
  `scripts/fetch_data.py` (no key required). Regenerate any time;
  `scripts/build_dataset.py` rebuilds the canonical files deterministically.

## Licensing note

Apache-2.0 covers Falcon's code, not third-party market data. These CSVs
derive from public Binance klines; check Binance's terms before any use
beyond local research. Recorded gaps are never filled.
