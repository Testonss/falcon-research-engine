# Roadmap — Falcon Research Engine

## v0.1.0 (this milestone — release candidate)
- [x] Research engine: candidates, gates, scoring, campaigns
- [x] Real versioned multi-symbol data (BTC/ETH/SOL/BNB, 1h+4h, 2017–2025)
- [x] Liquidity-aware execution + capacity + cost stress
- [x] Sealed holdout, regime analysis, stability, paper promotion
- [x] Bayesian optimizer available behind explicit flag (random default)
- [x] CLI, reports, SQLite persistence, docs, CI config

## v0.1.x — stabilization, bugs, additional datasets
- More spot symbols where clean multi-year history exists
- Funnel/report polish from real campaign experience
- No threshold changes without methodological justification

## v0.2 — richer optimizers, larger market coverage
- Bayesian optimizer as a first-class campaign strategy with tuning reports
- Regime-conditional strategies (declared regime-specific execution)
- Asymmetric long/short cost modelling

## v0.3 — realtime paper feeds, exchange testnets
- `MarketDataProvider` live implementations (read-only first)
- Testnet execution adapter behind explicit flags (never default)
- Rolling paper-divergence alerting over live paper sessions

## Later — LOB-level simulation, additional strategy research systems,
## UI/dashboard, plugin ecosystem

## Explicitly out of scope (safety)
- Real-money order execution, funded keys, live adapters by default.
