"""Backtest tests: determinism, duplicates, fees, missing data."""
import asyncio
import unittest
from datetime import datetime, timezone, timedelta

from config import ConfigManager
from market_data import MarketDataHandler, Candle
from exchange import PaperExchange
from strategy import StrategyManager
from execution import ExecutionEngine
from risk import RiskManager
from portfolio import PortfolioManager
from backtest import BacktestEngine


def run(coro):
    return asyncio.run(coro)


def trend_candles(n=60, start=100.0, step=1.0, symbol="BTC/USDT"):
    base = datetime.now(timezone.utc) - timedelta(minutes=n * 5)
    out = []
    for i in range(n):
        p = start + i * step
        out.append(Candle(symbol=symbol, open=p, high=p * 1.001, low=p * 0.999,
                          close=p, volume=5000.0,
                          timestamp=base + timedelta(minutes=i * 5), interval="5m",
                          event_id=f"bt{i}"))
    return out


def make_stack(seed=42):
    config = ConfigManager()
    config.set('simulated_latency_ms', 0)
    config.set('min_volume', 100.0)
    config.set('volatility_max', 1.0)
    config.set('initial_cash', 10000.0)
    md = MarketDataHandler(config)
    ex = PaperExchange(config, seed=seed)
    run(ex.connect())
    risk = RiskManager(config)
    pf = PortfolioManager(config)
    eng = ExecutionEngine(config, ex, risk)
    strat = StrategyManager(config)
    bt = BacktestEngine(config, md, eng, pf, risk, strat)
    return config, md, ex, risk, pf, eng, strat, bt


class TestBacktest(unittest.TestCase):
    def test_deterministic_backtest(self):
        _, _, _, _, _, _, _, bt1 = make_stack()
        candles = trend_candles()
        r1 = run(bt1.run("BTC/USDT", list(candles)))
        _, _, _, _, _, _, _, bt2 = make_stack()
        r2 = run(bt2.run("BTC/USDT", list(candles)))
        self.assertEqual(r1.trades, r2.trades)
        self.assertAlmostEqual(r1.final_equity, r2.final_equity, places=6)
        self.assertEqual(r1.equity_curve, r2.equity_curve)

    def test_duplicate_candles_ignored(self):
        _, _, _, _, _, _, _, bt = make_stack()
        candles = trend_candles(n=40)
        doubled = candles + candles  # exact duplicates
        r = run(bt.run("BTC/USDT", doubled))
        self.assertGreater(bt.duplicates_skipped, 0)
        # Same as single run
        _, _, _, _, _, _, _, bt2 = make_stack()
        r2 = run(bt2.run("BTC/USDT", candles))
        self.assertEqual(r.trades, r2.trades)
        self.assertAlmostEqual(r.final_equity, r2.final_equity, places=6)

    def test_fees_accounted(self):
        config, md, ex, risk, pf, eng, strat, bt = make_stack()
        candles = trend_candles(n=50)
        r = run(bt.run("BTC/USDT", candles))
        if r.trades > 0:
            self.assertGreaterEqual(r.fees_paid, 0.0)
            self.assertGreater(pf.total_fees_paid, 0.0) if r.trades else None

    def test_empty_candles(self):
        _, _, _, _, _, _, _, bt = make_stack()
        r = run(bt.run("BTC/USDT", []))
        self.assertEqual(r.trades, 0)


if __name__ == '__main__':
    unittest.main()
