# Contributing to Falcon Research Engine

## Ground rules

- **Research first.** The framework rejects fragile strategies; a rejected
  candidate with a clear reason is a good outcome.
- **No live trading.** Never add real-money order paths, credentials, or
  exchange keys. The live adapter stays explicitly gated and disabled.
- **Determinism.** Research code must be deterministic given a seed.
  No wall-clock randomness, no unordered iteration over sets in scoring.
- **No look-ahead.** Strategies see trailing data only; tests must prove it.
- **Small, verified iterations.** Inspect, implement minimally, run targeted
  tests, then the broader suite.

## Workflow

1. Fork and create a feature branch.
2. Add or update tests **before or with** the change
   (`python -m unittest discover -s tests`).
3. Keep modules small and typed; prefer dataclasses and pure functions for
   research math. Avoid giant classes, magic constants, circular imports.
4. Run the full suite: `python -m unittest discover -s tests`.
5. Verify no secrets: `Select-String -Pattern 'api[_-]?key|secret' -Recurse`.
6. Open a PR describing behaviour change + test evidence. Do not claim
   profitability or production readiness without verification.

## Test instructions

```bash
python -m unittest discover -s tests -v   # full suite (~12 min, 230 tests)
python -m unittest tests.test_research -v # research engine only
python scripts/falcon.py --help           # CLI smoke
```

## Reporting issues

Include: command run, config/seed, dataset version, expected vs actual
behaviour, and full traceback. Never paste API keys or account data.
