# Security Policy — Falcon Research Engine

## Scope

Falcon is a **simulation/paper-trading research framework**. It must never
touch real funds.

## Hard rules

- Do **not** commit API keys, secrets, tokens, or account identifiers.
  Configuration lives in `.env` (never committed); only `.env.example`
  with placeholder values belongs in the repo.
- Do **not** enable, wire, or test live-money order paths. The live
  exchange adapter requires `EXCHANGE_ADAPTER=live` **plus**
  `LIVE_TRADING_ENABLED=true` **plus**
  `LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_RISK`, and still raises
  `NotImplementedError` in this milestone.
- Do **not** deploy anything, connect to real funds, or request real
  exchange credentials in issues, examples, or docs.

## Reporting a vulnerability

Open a private report to the maintainers with: affected file/version,
reproduction steps, and impact assessment. Do not open a public issue for
credential leaks — rotate the credential first, then report.

## Dependency posture

Runtime dependencies are intentionally **stdlib-only** (see
`requirements.txt`). Proposals adding third-party runtime dependencies must
justify the need and the supply-chain risk.
