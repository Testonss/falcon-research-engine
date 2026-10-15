#!/usr/bin/env python3
"""
Main entry point for the Falcon Research Engine (paper-trading bot mode).
Paper trading is the default. Live trading is blocked unless explicitly enabled.
"""

import asyncio
import logging
import sys
from typing import Optional

from config import ConfigManager
from market_data import MarketDataHandler
from exchange import PaperExchange, LiveExchange
from strategy import StrategyManager
from execution import ExecutionEngine
from risk import RiskManager
from portfolio import PortfolioManager
from paper import PaperTradingEngine
from backtest import BacktestEngine

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class FalconPaperApp:
    """Falcon paper-trading runner (legacy standalone entry point).

    Enforced flow: market data -> strategy -> signal -> risk -> execution -> portfolio.
    Prefer the `falcon` CLI (`scripts/falcon.py`) for research workflows.
    """

    def __init__(self, config_file: Optional[str] = None):
        self.config = ConfigManager(config_file)
        level = self.config.get('log_level', 'INFO')
        try:
            logging.getLogger().setLevel(getattr(logging, str(level).upper(), logging.INFO))
        except Exception:
            pass

        if self.config.is_live_trading_enabled():
            logger.warning("LIVE mode requested — LiveExchange selected (separate adapter)")
            self.market_data = MarketDataHandler(self.config)
            self.exchange = LiveExchange(self.config)
        else:
            if str(self.config.get('exchange_adapter', 'paper')) == 'live':
                logger.warning("Live adapter requested without confirmation — staying in PAPER mode")
            self.market_data = MarketDataHandler(self.config)
            self.exchange = PaperExchange(self.config)

        self.strategy_manager = StrategyManager(self.config)
        self.risk_manager = RiskManager(self.config)
        self.portfolio_manager = PortfolioManager(self.config)
        self.execution_engine = ExecutionEngine(self.config, self.exchange, self.risk_manager)
        self.paper_trading = PaperTradingEngine(
            self.config, self.market_data, self.execution_engine,
            self.portfolio_manager, self.risk_manager, self.strategy_manager)
        self.backtest_engine = BacktestEngine(
            self.config, self.market_data, self.execution_engine,
            self.portfolio_manager, self.risk_manager, self.strategy_manager)

        logger.info("Falcon paper app initialized in %s mode",
                    "LIVE" if self.config.is_live_trading_enabled() else "paper")

    async def run_async(self, max_cycles: Optional[int] = None):
        if self.config.is_live_trading_enabled():
            raise RuntimeError("Live trading is not enabled in this milestone (paper/backtest only).")
        await self.paper_trading.run(max_cycles=max_cycles)

    def run(self, max_cycles: Optional[int] = None):
        asyncio.run(self.run_async(max_cycles=max_cycles))


if __name__ == "__main__":
    max_cycles = None
    if len(sys.argv) > 1 and sys.argv[1].startswith("--cycles="):
        try:
            max_cycles = int(sys.argv[1].split("=", 1)[1])
        except ValueError:
            max_cycles = None
    bot = FalconPaperApp()
    try:
        bot.run(max_cycles=max_cycles)
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, stopping bot...")
    except Exception as e:
        logger.error("Error running bot: %s", e)
        raise


# Legacy alias (pre-v0.1.0 name; kept for compatibility, do not use in new code).
ScalpingBot = FalconPaperApp
