# AGENTS.md

## Project Requirements

This document outlines the core architectural principles for the Falcon
Research Engine, an open-source autonomous quantitative research engine
(research-first; simulation/paper trading only, no profit promises).

### Architecture Principles
- Modular design with clear separation of concerns
- Extensible architecture (live trading adapter can be added later)
- Paper trading as default mode (no real orders without approval)

### Core Modules
1. config - Application configuration, environment validation
2. market_data - Market data interface & WebSocket handling
3. exchange - Exchange interface with paper trading adapter
4. strategy - Strategy interface for signal generation
5. execution - Order execution engine with simulation capabilities
6. risk - Risk management system with configurable limits
7. portfolio - Portfolio tracking and PnL calculation
8. backtest - Backtesting engine for historical data replay
9. paper - Paper trading engine for real-time simulation
10. logging - Structured logging and metrics tracking
11. tests - Unit and integration test suite

### Research Engine (Falcon)
12. research - Strategy candidates, families, generation, gates, scoring,
    regimes, stability, campaigns, paper promotion, SQLite store, reports
13. scripts/falcon.py - Research CLI (research/backtest/validate/report/paper/status)
14. evaluation - Temporal splits, walk-forward, cost sweeps, capacity, metrics

### Key Constraints
- No real orders by default (paper trading sandbox)
- All API keys in .env.example, none in repository
- Risk manager has final authority over all trades
- Idempotency required for all operations
- Simulated slippage, spread, fees included 
- Deterministic, reproducible results for backtesting

### Implementation Rules
1. Start with paper trading only (no live adapter)
2. All adapters are configurable and can be enabled/disabled
3. Future live adapter must require explicit configuration
4. All parameters configured via environment variables or config files
5. No hard-coded credentials or API keys
6. Tests must cover all critical logic paths