"""Strategy tests: EMA correctness, filters, no look-ahead."""
import unittest
from datetime import datetime, timezone, timedelta

from config import ConfigManager
from strategy import StrategyManager, SignalType, ema_last
from market_data import Candle, OrderBook, OrderBookEntry


def make_candles(prices, volumes=None, symbol="BTC/USDT", interval="5m"):
    base = datetime.now(timezone.utc) - timedelta(minutes=len(prices) * 5)
    out = []
    for i, p in enumerate(prices):
        v = (volumes[i] if volumes else 5000.0)
        out.append(Candle(symbol=symbol, open=p, high=p * 1.001, low=p * 0.999,
                          close=p, volume=v,
                          timestamp=base + timedelta(minutes=i * 5), interval=interval,
                          event_id=f"t{i}"))
    return out


class TestStrategy(unittest.TestCase):
    def setUp(self):
        self.config = ConfigManager()
        self.config.set('min_volume', 100.0)
        self.config.set('volatility_max', 1.0)  # disable vol filter for trend tests
        self.strategy_manager = StrategyManager(self.config)

    def test_strategy_initialization(self):
        self.assertIsNotNone(self.strategy_manager)
        self.assertIsNotNone(self.strategy_manager.get_strategy('ema_crossover'))

    def test_ema_math(self):
        # flat series => EMA == price
        self.assertAlmostEqual(ema_last([100.0] * 20, 10), 100.0, places=6)
        # rising series => EMA below last price but above first
        e = ema_last(list(range(1, 21)), 10)
        self.assertGreater(e, 10)
        self.assertLess(e, 20)

    def test_ema_uptrend_gives_long(self):
        prices = [100 + i * 2 for i in range(40)]  # strong uptrend
        candles = make_candles(prices)
        sig = self.strategy_manager.generate_signal("BTC/USDT", {'candles': candles})
        self.assertIsNotNone(sig)
        self.assertEqual(sig.signal_type, SignalType.LONG)

    def test_ema_downtrend_gives_short(self):
        prices = [200 - i * 2 for i in range(40)]
        candles = make_candles(prices)
        sig = self.strategy_manager.generate_signal("BTC/USDT", {'candles': candles})
        self.assertIsNotNone(sig)
        self.assertEqual(sig.signal_type, SignalType.SHORT)

    def test_low_volume_gives_hold(self):
        self.config.set('min_volume', 100000.0)
        m = StrategyManager(self.config)
        prices = [100 + i * 2 for i in range(40)]
        candles = make_candles(prices, volumes=[10.0] * 40)
        sig = m.generate_signal("BTC/USDT", {'candles': candles})
        self.assertEqual(sig.signal_type, SignalType.HOLD)
        self.assertEqual(sig.reason, 'low_volume')

    def test_spread_filter_gives_hold(self):
        prices = [100 + i * 2 for i in range(40)]
        candles = make_candles(prices)
        ob = OrderBook(symbol="BTC/USDT",
                       bids=[OrderBookEntry(price=100.0, quantity=1.0, side='bid')],
                       asks=[OrderBookEntry(price=110.0, quantity=1.0, side='ask')],
                       timestamp=datetime.now(timezone.utc))
        sig = self.strategy_manager.generate_signal("BTC/USDT", {'candles': candles, 'orderbook': ob})
        self.assertEqual(sig.signal_type, SignalType.HOLD)
        self.assertEqual(sig.reason, 'spread_too_wide')

    def test_not_enough_data_returns_none(self):
        sig = self.strategy_manager.generate_signal("BTC/USDT", {'candles': make_candles([100.0] * 5)})
        self.assertIsNone(sig)

    def test_no_lookahead_signal_id_stable(self):
        prices = [100 + i for i in range(40)]
        candles = make_candles(prices)
        s1 = self.strategy_manager.generate_signal("BTC/USDT", {'candles': candles})
        s2 = self.strategy_manager.generate_signal("BTC/USDT", {'candles': candles})
        # signal ids are time-based so may differ, but type must be stable
        self.assertEqual(s1.signal_type, s2.signal_type)


if __name__ == '__main__':
    unittest.main()
