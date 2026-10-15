"""Portfolio tests: PnL, fees, idempotency, valuation."""
import unittest
from datetime import datetime, timezone

from config import ConfigManager
from portfolio import PortfolioManager


def _utcnow():
    return datetime.now(timezone.utc)


class TestPortfolioManager(unittest.TestCase):
    def setUp(self):
        self.config = ConfigManager()
        self.config.set('initial_cash', 100000.0)
        self.portfolio = PortfolioManager(self.config)

    def test_initialization(self):
        self.assertEqual(self.portfolio.get_cash(), 100000.0)
        self.assertEqual(len(self.portfolio.get_positions()), 0)

    def test_update_position_buy(self):
        self.portfolio.update_position("BTC/USDT", 1.0, 50000.0, is_buy=True, order_id="o1")
        position = self.portfolio.get_position("BTC/USDT")
        self.assertIsNotNone(position)
        self.assertEqual(position.quantity, 1.0)
        self.assertEqual(position.entry_price, 50000.0)
        self.assertEqual(self.portfolio.get_cash(), 50000.0)

    def test_update_position_sell_partial_pnl(self):
        self.portfolio.update_position("BTC/USDT", 1.0, 50000.0, is_buy=True, order_id="o1")
        self.portfolio.update_position("BTC/USDT", 0.5, 55000.0, is_buy=False, order_id="o2")
        position = self.portfolio.get_position("BTC/USDT")
        self.assertIsNotNone(position)
        self.assertAlmostEqual(position.quantity, 0.5)
        # cash: 100000-50000 + 0.5*55000 = 77500
        self.assertAlmostEqual(self.portfolio.get_cash(), 77500.0)
        # realized PnL only on closed portion? partial close realizes immediately in our impl
        # (price-entry)*0.5 = 2500
        self.assertAlmostEqual(self.portfolio.realized_pnl, 2500.0)

    def test_pnl_calculation(self):
        self.portfolio.update_position("BTC/USDT", 1.0, 50000.0, is_buy=True, order_id="o1")
        info = self.portfolio.calculate_pnl({"BTC/USDT": 55000.0})
        self.assertAlmostEqual(info.unrealized_pnl, 5000.0)
        self.assertAlmostEqual(info.total_value, 50000.0 + 55000.0)
        self.assertAlmostEqual(info.equity, info.total_value)

    def test_portfolio_value_includes_positions(self):
        self.portfolio.update_position("BTC/USDT", 1.0, 50000.0, is_buy=True, order_id="o1")
        # cash 50000 + 1*55000 = 105000
        self.assertAlmostEqual(self.portfolio.get_portfolio_value({"BTC/USDT": 55000.0}), 105000.0)

    def test_fee_deducted(self):
        self.portfolio.update_position("BTC/USDT", 1.0, 50000.0, is_buy=True, order_id="o1", fee=100.0)
        self.assertAlmostEqual(self.portfolio.get_cash(), 100000.0 - 50000.0 - 100.0)
        self.assertAlmostEqual(self.portfolio.total_fees_paid, 100.0)

    def test_order_idempotency(self):
        self.portfolio.update_position("BTC/USDT", 1.0, 50000.0, is_buy=True, order_id="dup")
        cash_after_first = self.portfolio.get_cash()
        # Duplicate order_id must be ignored
        applied = self.portfolio.update_position("BTC/USDT", 1.0, 50000.0, is_buy=True, order_id="dup")
        self.assertFalse(applied)
        self.assertEqual(self.portfolio.get_cash(), cash_after_first)
        self.assertEqual(self.portfolio.get_position("BTC/USDT").quantity, 1.0)

    def _assert_invariant(self, prices, initial=100000.0):
        """cash + open value == initial + realized + unrealized, always."""
        info = self.portfolio.calculate_pnl(prices)
        lhs = self.portfolio.get_cash() + sum(
            p.quantity * prices.get(s, p.entry_price)
            for s, p in self.portfolio.get_positions().items())
        rhs = initial + info.realized_pnl + info.unrealized_pnl
        self.assertAlmostEqual(lhs, rhs, places=6)
        return info

    def test_entry_fee_in_realized_and_invariant(self):
        # B02 regression: entry commission used to vanish from realized PnL.
        self.portfolio.update_position("BTC/USDT", 1.0, 50000.0, is_buy=True,
                                       order_id="e1", fee=100.0)
        # Fee-loaded entry price.
        self.assertAlmostEqual(self.portfolio.get_position("BTC/USDT").entry_price,
                               50100.0)
        self._assert_invariant({"BTC/USDT": 55000.0})
        self.portfolio.update_position("BTC/USDT", 1.0, 55000.0, is_buy=False,
                                       order_id="e2", fee=50.0)
        # (55000-50100)*1 - 50 = 4850, includes BOTH entry and exit fees.
        self.assertAlmostEqual(self.portfolio.realized_pnl, 4850.0)
        self.assertIsNone(self.portfolio.get_position("BTC/USDT"))
        # Flat: cash == initial + realized exactly.
        self.assertAlmostEqual(self.portfolio.get_cash(), 100000.0 + 4850.0)
        self._assert_invariant({"BTC/USDT": 55000.0})

    def test_partial_close_with_fees_invariant(self):
        self.portfolio.update_position("BTC/USDT", 2.0, 50000.0, is_buy=True,
                                       order_id="p1", fee=200.0)
        self.portfolio.update_position("BTC/USDT", 0.5, 55000.0, is_buy=False,
                                       order_id="p2", fee=50.0)
        # Entry 50100; close 0.5: (55000-50100)*0.5 - 50 = 2400.
        self.assertAlmostEqual(self.portfolio.realized_pnl, 2400.0)
        self.assertAlmostEqual(self.portfolio.get_position("BTC/USDT").quantity, 1.5)
        self._assert_invariant({"BTC/USDT": 55000.0})

    def test_adds_blend_fee_loaded_entry(self):
        self.portfolio.update_position("BTC/USDT", 1.0, 50000.0, is_buy=True,
                                       order_id="a1", fee=100.0)
        self.portfolio.update_position("BTC/USDT", 1.0, 52000.0, is_buy=True,
                                       order_id="a2", fee=100.0)
        # (50100 + 52100) / 2
        self.assertAlmostEqual(self.portfolio.get_position("BTC/USDT").entry_price,
                               51100.0)
        self._assert_invariant({"BTC/USDT": 53000.0})

    def test_short_open_close_invariant(self):
        self.portfolio.update_position("BTC/USDT", 1.0, 50000.0, is_buy=False,
                                       order_id="s1", fee=100.0)
        # Short entry fee-loaded downward: (50000-100)/1.
        self.assertAlmostEqual(self.portfolio.get_position("BTC/USDT").entry_price,
                               49900.0)
        self._assert_invariant({"BTC/USDT": 48000.0})
        self.portfolio.update_position("BTC/USDT", 1.0, 48000.0, is_buy=True,
                                       order_id="s2", fee=50.0)
        # (49900-48000)*1 - 50 = 1850.
        self.assertAlmostEqual(self.portfolio.realized_pnl, 1850.0)
        self.assertAlmostEqual(self.portfolio.get_cash(), 100000.0 + 1850.0)
        self._assert_invariant({"BTC/USDT": 48000.0})

    def test_flip_with_leftover_invariant(self):
        self.portfolio.update_position("BTC/USDT", 1.0, 50000.0, is_buy=True,
                                       order_id="f1", fee=100.0)
        # Sell 2.5: closes 1.0 long, flips 1.5 short (fee share pro-rata).
        self.portfolio.update_position("BTC/USDT", 2.5, 55000.0, is_buy=False,
                                       order_id="f2", fee=250.0)
        # Closed: (55000-50100)*1 - 250*(1/2.5)=4900-100=4800.
        self.assertAlmostEqual(self.portfolio.realized_pnl, 4800.0)
        pos = self.portfolio.get_position("BTC/USDT")
        self.assertAlmostEqual(pos.quantity, -1.5)
        # Leftover short entry: 55000 - 250*(1.5/2.5)/1.5 = 55000-100.
        self.assertAlmostEqual(pos.entry_price, 54900.0)
        self._assert_invariant({"BTC/USDT": 56000.0})


if __name__ == '__main__':
    unittest.main()
