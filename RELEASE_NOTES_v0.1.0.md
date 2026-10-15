# Falcon Research Engine v0.1.0 — DRAFT release notes (unreleased)

> Generate strategies. Break them. Promote only the survivors.

First public milestone of an autonomous quantitative research engine.
**Not a trading bot. No profitability is promised or implied.**

## What is validated in this release

- 230 automated tests passing (full suite green: 230 run, 0 failures;
  locally verified on Windows; Linux + remote CI matrix pending —
  see Limitations).
- Campaign rerun reproduces the identical funnel (determinism verified).
- Deterministic reruns (same seed + dataset + code = identical funnel).
- Sealed final holdout never opened during research (access log empty).
- Live-money execution disabled by architecture (tested).

## Research history (pre-remediation evidence, not current validation)

Campaign `big500` (2026-10-14; 1600 draws → 508 unique candidates,
BTC+ETH+SOL+BNB 4h 2017–2025, seed 7, all six families):
1600 generated → 1092 duplicates skipped → 508 evaluated →
502 rejected at train → 4 rejected at validation → 2 rejected
cross-market → **0 survivors** (accepted outcome).

Recorded here for provenance only. Gate semantics, experiment identity
and stress selection changed afterwards (protocol v2); re-run to confirm
under the current release before citing it.

## Research pipeline

Generate → train → validate → cross-market → walk-forward → cost stress
(max-multiplier scenario) → regime (every market gated) → capacity →
stability → sealed holdout (final mode only, all accesses logged) →
paper candidate, with every rejection persisted with reason + metrics in
SQLite.

## Known limitations

See README "Limitations (honest)". Backtests do not predict future returns.
Do not trade real money with this software.
