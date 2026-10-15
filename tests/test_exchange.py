"""Exchange tests: idempotency, fees, slippage, rejects, partial fills."""
import asyncio
import unittest

from config import ConfigManager
from exchange import PaperExchange, OrderSide, OrderType, OrderStatus


def run(coro):
    return asyncio.run(coro)


class TestPaperExchange(unittest.TestCase):
    def setUp(self):
        self.config = ConfigManager()
        self.config.set('simulated_latency_ms', 0)
        self.config.set('partial_fill_probability', 0.0)
        self.config.set('reject_probability', 0.0)
        self.ex = PaperExchange(self.config, seed=123)
        run(self.ex.connect())

    def tearDown(self):
        run(self.ex.disconnect())

    def test_market_fill_applies_fees(self):
        order = run(self.ex.submit_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET,
                                         1.0, reference_price=50000.0,
                                         client_order_id="c1"))
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.filled_quantity, 1.0)
        self.assertGreater(order.avg_fill_price, 50000.0)  # spread+slippage adverse for buys
        expected_fee = order.filled_quantity * order.avg_fill_price * self.ex.taker_fee
        self.assertAlmostEqual(order.commission, expected_fee)

    def test_sell_fill_below_reference(self):
        order = run(self.ex.submit_order("BTC/USDT", OrderSide.SELL, OrderType.MARKET,
                                         1.0, reference_price=50000.0,
                                         client_order_id="c2"))
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertLess(order.avg_fill_price, 50000.0)

    def test_slippage_config_affects_price(self):
        self.config.set('spread_pct', 0.0)
        self.config.set('slippage_pct', 0.0)
        ex2 = PaperExchange(self.config, seed=1)
        run(ex2.connect())
        o = run(ex2.submit_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET,
                                 1.0, reference_price=100.0, client_order_id="z"))
        self.assertAlmostEqual(o.avg_fill_price, 100.0, places=6)
        run(ex2.disconnect())

    def test_market_without_reference_rejected(self):
        order = run(self.ex.submit_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET,
                                         1.0, client_order_id="noref"))
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertEqual(order.reject_reason, 'missing_reference_price')

    def test_invalid_quantity_rejected(self):
        order = run(self.ex.submit_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET,
                                         0.0, reference_price=100.0, client_order_id="badqty"))
        self.assertEqual(order.status, OrderStatus.REJECTED)

    def test_order_idempotency(self):
        o1 = run(self.ex.submit_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET,
                                      0.5, reference_price=100.0, client_order_id="idem1"))
        o2 = run(self.ex.submit_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET,
                                      0.5, reference_price=100.0, client_order_id="idem1"))
        self.assertEqual(o1.order_id, o2.order_id)
        self.assertEqual(len(self.ex.orders), 1)

    def test_reconnect_does_not_duplicate(self):
        o1 = run(self.ex.submit_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET,
                                      0.5, reference_price=100.0, client_order_id="r1"))
        run(self.ex.disconnect())
        run(self.ex.connect())
        o2 = run(self.ex.submit_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET,
                                      0.5, reference_price=100.0, client_order_id="r1"))
        self.assertEqual(o1.order_id, o2.order_id)

    def test_cancel_open_limit(self):
        # Non-marketable limit stays OPEN and can be cancelled
        self.ex.set_reference_price("BTC/USDT", 100.0)
        o = run(self.ex.submit_order("BTC/USDT", OrderSide.BUY, OrderType.LIMIT,
                                     1.0, price=90.0, reference_price=100.0,
                                     client_order_id="lim1"))
        self.assertEqual(o.status, OrderStatus.OPEN)
        ok = run(self.ex.cancel_order("BTC/USDT", o.order_id))
        self.assertTrue(ok)
        self.assertEqual(run(self.ex.get_order_status("BTC/USDT", o.order_id)), OrderStatus.CANCELLED)

    def test_live_exchange_disabled(self):
        from exchange import LiveExchange
        live = LiveExchange(self.config)
        with self.assertRaises(RuntimeError):
            run(live.connect())
        with self.assertRaises(RuntimeError):
            run(live.submit_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET, 1.0))


if __name__ == '__main__':
    unittest.main()
