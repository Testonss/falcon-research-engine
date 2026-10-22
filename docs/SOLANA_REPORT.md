# Falcon Quant Hunter — offline report CLI

Input CSV format: `mint,observed_at,price_usd` (UTC Unix timestamps; observed prices only).

```sh
python -m research.solana_report --csv prices.csv --out report.json --capital 500 --hold-seconds 300
python -m unittest tests.test_solana_report -v
```

Output contains win rate, profit factor, average return, individual round-trip costs and unresolved exits. **No live-money integration, no wallet signing, no network dependency.**

**Important:** The entry rule is *first observation*, not an alpha signal. Trades are independent per-token experiments, not a shared-capital strategy. Unresolved exits can hide catastrophic losses. Metrics do not establish predictive performance. Do not use this report to allocate real funds without realistic event-level historical data, a shared portfolio simulator, failed-exit handling, out-of-sample tests and live paper monitoring.
