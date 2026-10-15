# Changelog — Falcon Research Engine

All notable changes are recorded here. Format follows "Keep a Changelog"
(loosely). Dates are UTC.

## [Unreleased] — final release-candidate milestone

### Added
- Extended real history: BTC/ETH from 2017-08-17, BNB from 2017-11-06,
  SOL from 2020-08-11 (all to 2025-01-01), 1h canonical + 4h resampled,
  all hash-manifested. True listing dates recorded, no pre-listing data.
- Expanded parameter spaces (additive only, still interpretable) to support
  500+ unique candidates; total space now 723 combinations.
- Bayesian optimizer wired into campaigns behind `--optimizer bayes`
  (OFF by default): per-family GP+EI rounds on TRAIN only, top-k by
  multi-metric train objective enter the funnel; all trials persisted.
- Cross-run duplicate defense: fingerprints evaluated in prior campaigns in
  the same store are skipped (`previously_evaluated`), never re-evaluated.
- Holdout access log persisted in the campaign record (`holdout_accesses`).
- Campaign `started_at` timestamp; `falcon dataset inspect` shows regime mix.
- Regime report tables (per-regime net/PF/expectancy) and per-regime
  drawdowns from contiguous equity runs; `regime_distribution` diagnostics.
- Stability surfaces: median/worst/dispersion neighbour scores.
- `pyproject.toml` (verified clean-venv `pip install` + external-dir imports).

### Added (RC3 audit remediation)
- Experiment identity is the dedup key (strategy x dataset x costs x
  gates x splits x seed x protocol v2); same-strategy reruns under a
  different dataset, cost model, gates, splits or seed evaluate as new
  experiments. Cost/capacity multiplier schedules folded into the
  experiment fingerprint; campaign records now carry `protocol_version`
  and `effective_config`.
- Paper sessions keyed by stable paper fingerprint (candidate, symbol,
  interval, dataset version/hash, full bar history, preingest, paper
  config); BTC/ETH replays isolated; completed sessions verified before
  reuse; crash resume preserved.
- Secondary-market regime hard gate: every market must obey the
  concentration policy, failures name the market (`failed_market`).
- Cost stress compares against the maximum configured multiplier
  (`stressed_scenario`/`stressed_multiplier` persisted; no x5 hardcode).
- CLI expands quoted manifest globs in Python (Windows-safe).
- Holdout access persisted to the campaign record at access time
  (crash-safe); policy A documented (multi-open in final mode, all
  logged).
- Historical research replays pass the execution-bar timestamp into risk
  decisions (`sim_now`); quantitative behavior no longer depends on
  wall-clock time (result metadata timestamps still do).

### Fixed
- Campaign wrapper/details mix-up that would have crashed multi-symbol
  scoring paths (caught by smoke test before release).
- `falcon dataset inspect` no longer mislabels resampled-manifest quality
  fields; shows resample provenance instead.
- Final campaign record was missing `protocol_version`/`effective_config`
  (report isolation fell back to legacy view); now recorded.

### Changed (release polish, no quantitative behavior change)
- Default CLI is quiet (WARNING): routine simulation events demoted to
  debug/info; `--verbose`/`--log-level` opt into detail. Errors unaffected.
- Legacy "scalping bot" naming removed from docs/docstrings; remaining
  `ScalpingBot*` symbols kept as documented legacy aliases only.
- Removed 4 byte-identical legacy `_1h` dataset duplicates (8 versioned
  datasets shipped); added `data/README.md` provenance/licensing note.

## [v0.1.0] - 2026-10-14 (draft, unreleased)

First public milestone: autonomous quantitative research framework.

### Added
- Strategy candidate lifecycle (generated → … → paper_candidate / rejected)
  with immutable IDs, fingerprints, parentage and persisted rejection reasons.
- Strategy families: ema_v1 (historical baseline), momentum, mean_reversion,
  breakout, ema_trend, ema_momentum_combo; explicit inspectable param spaces.
- Deterministic candidate generation: random search, constrained grids,
  perturbation; optional GP+EI Bayesian optimizer (`research/optimizer.py`).
- Sealed dataset protocol: TRAIN / VALIDATION / WALK-FORWARD / FINAL_HOLDOUT
  with explicit, logged holdout access (`research/datasets.py`).
- Real historical data pipeline: Binance public klines downloader
  (`scripts/fetch_data.py`), validation + versioned manifests with sha256
  (`research/datafiles.py`); 1h canonical + 4h resampled for BTC/ETH (from
  2017-08-17), BNB (from 2017-11-06), SOL (from 2020-08-11), all to
  2025-01-01 (shipped as 8 versioned datasets).
- Liquidity-aware paper execution: participation caps, partial fills,
  insufficient-liquidity rejects, sqrt market impact, dynamic spread,
  exact cost attribution (fees/spread/slippage/impact).
- Promotion gates (12 machine-readable rejection reasons), transparent
  multi-metric scoring, regime analysis (trailing-only), parameter
  neighbourhood stability, cost-stress matrix (1x–5x), capacity scans,
  multiple-testing LCB/snooping report.
- Multi-symbol campaigns with cross-market gate and weakest-link scoring.
- Paper promotion replay reusing the identical stack + divergence tracking.
- SQLite ResearchStore with crash-safe persistence and idempotency keys.
- CLI: `falcon research|backtest|validate|report|paper|status`,
  `falcon dataset inspect`.
- Reports: JSON + CSV + terminal funnel; rejection database preserved.
- Exact incremental EMA (bit-identical, O(n) instead of O(n²)).

### Verified
- 239 automated tests passing (full suite green, exact count).
- Sample campaign 16→0 and 100-candidate real-data campaign executed with
  real funnels; zero survivors accepted as valid outcomes.
- Live-money paths remain disabled by architecture (verified by tests).
