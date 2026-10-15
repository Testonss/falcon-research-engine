"""Risk tests: limits, SL/TP, daily loss, drawdown, kill switch, sizing."""
import unittest
from datetime import datetime, timezone, timedelta

from config import ConfigManager
from risk import RiskManager


def _utcnow():
    return datetime.now(timezone.utc)


class TestRiskManager(unittest.TestCase):
    def setUp(self):
        self.config = ConfigManager()
        self.risk_manager = RiskManager(self.config)

    def test_position_limits_check(self):
        self.assertTrue(self.risk_manager.check_position_limits("BTC/USDT", 500.0))
        self.assertFalse(self.risk_manager.check_position_limits("BTC/USDT", 1500.0))

    def test_position_sizing(self):
        # equity 10000, price 50000, risk 10 @ 1% stop => qty = 10/(50000*0.01)=0.02
        qty = self.risk_manager.calculate_position_size(50000.0, 10000.0)
        self.assertGreater(qty, 0)
        self.assertLessEqual(qty, 1000.0)
        self.assertAlmostEqual(qty, 0.02, places=6)

    def test_position_sizing_capped_by_equity(self):
        qty = self.risk_manager.calculate_position_size(50000.0, 100.0)
        # equity/price = 0.002 max
        self.assertLessEqual(qty, 0.002 + 1e-9)

    def test_stop_loss_triggers(self):
        should_close, reason = self.risk_manager.check_stop_take(100.0, 98.0, 'long')
        self.assertTrue(should_close)
        self.assertEqual(reason, 'stop_loss')

    def test_take_profit_triggers(self):
        should_close, reason = self.risk_manager.check_stop_take(100.0, 102.0, 'long')
        self.assertTrue(should_close)
        self.assertEqual(reason, 'take_profit')

    def test_short_stop_take(self):
        should_close, reason = self.risk_manager.check_stop_take(100.0, 102.0, 'short')
        self.assertTrue(should_close)
        self.assertEqual(reason, 'stop_loss')
        should_close, reason = self.risk_manager.check_stop_take(100.0, 98.0, 'short')
        self.assertTrue(should_close)
        self.assertEqual(reason, 'take_profit')

    def test_no_stop_take_inside_band(self):
        should_close, _ = self.risk_manager.check_stop_take(100.0, 100.5, 'long')
        self.assertFalse(should_close)

    def test_daily_loss_check_pure_no_mutation(self):
        # Pure check must not mutate daily_loss
        self.assertTrue(self.risk_manager.check_daily_loss_limit(100.0))
        self.assertEqual(self.risk_manager.metrics.daily_loss, 0.0)
        self.assertTrue(self.risk_manager.check_daily_loss_limit(200.0))
        self.assertEqual(self.risk_manager.metrics.daily_loss, 0.0)
        self.assertFalse(self.risk_manager.check_daily_loss_limit(600.0))

    def test_max_daily_loss_blocks_after_records(self):
        self.risk_manager.record_trade_result(False, pnl=-300.0, equity=9700.0)
        self.assertTrue(self.risk_manager.check_daily_loss_limit(100.0))
        self.risk_manager.record_trade_result(False, pnl=-250.0, equity=9450.0)
        # daily_loss = 550 > 500
        self.assertFalse(self.risk_manager.check_daily_loss_limit(0.0))
        allowed, reason = self.risk_manager.evaluate_trade("BTC/USDT", 1.0, 100.0, 9450.0)
        self.assertFalse(allowed)
        self.assertEqual(reason, 'daily_loss')

    def test_consecutive_losses_cooldown(self):
        for _ in range(3):
            self.risk_manager.record_trade_result(False, pnl=-10.0, equity=9990.0)
        self.assertTrue(self.risk_manager.metrics.is_in_cooldown)
        self.assertFalse(self.risk_manager.check_cooldown())
        # evaluate_trade must block during cooldown
        allowed, reason = self.risk_manager.evaluate_trade("BTC/USDT", 1.0, 100.0, 9990.0)
        self.assertFalse(allowed)
        self.assertEqual(reason, 'cooldown')

    def test_win_resets_consecutive(self):
        self.risk_manager.record_trade_result(False, pnl=-10.0, equity=9990.0)
        self.risk_manager.record_trade_result(True, pnl=20.0, equity=10010.0)
        self.assertEqual(self.risk_manager.metrics.consecutive_losses, 0)
        self.assertTrue(self.risk_manager.check_consecutive_losses())

    def test_kill_switch_blocks_all(self):
        self.risk_manager.set_kill_switch(True)
        self.assertFalse(self.risk_manager.check_kill_switch())
        allowed, reason = self.risk_manager.evaluate_trade("BTC/USDT", 1.0, 100.0, 10000.0)
        self.assertFalse(allowed)
        self.assertEqual(reason, 'kill_switch')

    def test_trip_kill_switch_latches(self):
        self.risk_manager.trip_kill_switch("test")
        allowed, reason = self.risk_manager.evaluate_trade("BTC/USDT", 1.0, 100.0, 10000.0)
        self.assertFalse(allowed)
        self.assertEqual(reason, 'kill_switch')

    def test_drawdown_blocks(self):
        self.risk_manager.update_peak(10000.0)
        # 15% drawdown > 10% limit
        allowed, reason = self.risk_manager.evaluate_trade("BTC/USDT", 1.0, 100.0, 8500.0)
        self.assertFalse(allowed)
        self.assertEqual(reason, 'max_drawdown')

    def test_evaluate_allows_normal(self):
        self.risk_manager.update_peak(10000.0)
        allowed, reason = self.risk_manager.evaluate_trade("BTC/USDT", 0.01, 50000.0, 10000.0)
        self.assertTrue(allowed)
        self.assertEqual(reason, 'ok')


if __name__ == '__main__':
    unittest.main()
