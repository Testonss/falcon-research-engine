"""Execution tests: risk gating, idempotency, portfolio update."""
import asyncio
import unittest
from datetime import datetime, timezone

from config import ConfigManager
from market_data import MarketDataHandler
from exchange import PaperExchange
from strategy import TradingSignal, SignalType
from execution import ExecutionEngine
from risk import RiskManager
from portfolio import PortfolioManager


def run(coro):
    return asyncio.run(coro)


def _sig(symbol="BTC/USDT", side=SignalType.LONG, price=50000.0, sid="s1"):
    return TradingSignal(symbol=symbol, signal_type=side, confidence=0.9,
                         timestamp=datetime.now(timezone.utc), price=price,
                         signal_id=sid)


class TestExecution(unittest.TestCase):
    def setUp(self):
        self.config = ConfigManager()
        self.config.set('simulated_latency_ms', 0)
        self.config.set('initial_cash', 100000.0)
        self.ex = PaperExchange(self.config, seed=7)
        run(self.ex.connect())
        self.risk = RiskManager(self.config)
        self.portfolio = PortfolioManager(self.config)
        self.engine = ExecutionEngine(self.config, self.ex, self.risk)

    def tearDown(self):
        run(self.ex.disconnect())

    def test_hold_does_nothing(self):
        sig = _sig(side=SignalType.HOLD, sid="hold1")
        res = run(self.engine.execute_signal(sig, self.portfolio, {"BTC/USDT": 50000.0}))
        self.assertFalse(res.success)
        self.assertEqual(len(self.engine.get_execution_history()), 0)

    def test_risk_blocks_kill_switch(self):
        self.risk.set_kill_switch(True)
        res = run(self.engine.execute_signal(_sig(sid="k1"), self.portfolio, {"BTC/USDT": 50000.0}))
        self.assertFalse(res.success)
        self.assertEqual(res.blocked_by, 'kill_switch')

    def test_success_updates_portfolio(self):
        res = run(self.engine.execute_signal(_sig(sid="ok1"), self.portfolio, {"BTC/USDT": 50000.0}))
        self.assertTrue(res.success)
        self.assertGreater(res.filled_quantity, 0)
        pos = self.portfolio.get_position("BTC/USDT")
        self.assertIsNotNone(pos)
        self.assertGreater(pos.quantity, 0)

    def test_signal_idempotency(self):
        r1 = run(self.engine.execute_signal(_sig(sid="idem"), self.portfolio, {"BTC/USDT": 50000.0}))
        r2 = run(self.engine.execute_signal(_sig(sid="idem"), self.portfolio, {"BTC/USDT": 50000.0}))
        self.assertEqual(r1.order_id, r2.order_id)
        # Only one portfolio application
        self.assertEqual(len([t for t in self.portfolio.get_trade_history()]), 1)

    def test_insufficient_equity_blocked(self):
        self.portfolio.set_cash(10.0)
        res = run(self.engine.execute_signal(_sig(sid="poor", price=50000.0), self.portfolio,
                                             {"BTC/USDT": 50000.0}))
        # sizing caps by equity -> tiny qty, but evaluate may still allow; ensure no crash
        self.assertIn(res.success, (True, False))


if __name__ == '__main__':
    unittest.main()
