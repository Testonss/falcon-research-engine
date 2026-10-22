# Falcon Solana Quant Hunter — research prototype

Status: **offline screening only**, no Solana RPC, Axiom, Jupiter, wallet, signing, order routing or live trading. Existing Falcon live-money safety architecture remains untouched.

## Purpose
Evaluate point-in-time token snapshots with explicit conservative screening and a transparent ranking heuristic. A high score **is not** a calibrated probability, trading signal, or proof of profitability.

## Data contract
Provide mint, observation Unix timestamp, liquidity USD, five-minute volume USD, age in minutes, top-ten holder concentration %, expected round-trip price impact proxy %, and mint/freeze authority booleans. Never substitute zeros for missing values. Verify token metadata and snapshot provenance before evaluation.

## Run tests
`python -m unittest tests.test_solana_hunter -v`
`python -m unittest discover -s tests`

## Next research milestones
1. Add an independently verified historical token universe **including dead and delisted tokens**; record ingestion and observation timestamps to prevent look-ahead and survivorship bias.
2. Validate token safety data and route quotes from documented provider endpoints; account for rate limits and missing data.
3. Replay decisions with latency, swap fees, priority fees, MEV exposure, price impact, failed swaps, and exits that cannot fill.
4. Use chronological walk-forward and sealed holdout; report uncertainty and compare against a no-trade baseline.
5. Only after verified paper results, separately review whether live execution is appropriate. Never commit secrets or enable real-money paths by default.

Thresholds are **illustrative defaults** and must be validated rather than optimized on the holdout.
