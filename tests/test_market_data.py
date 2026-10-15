"""Market data tests: duplicates, out-of-order, reconnect."""
import asyncio
import unittest
from datetime import datetime, timezone, timedelta

from config import ConfigManager
from market_data import MarketDataHandler, Ticker, Candle


def run(coro):
    return asyncio.run(coro)


class TestMarketData(unittest.TestCase):
    def setUp(self):
        self.md = MarketDataHandler(ConfigManager())
        run(self.md.connect())

    def tearDown(self):
        run(self.md.disconnect())

    def test_duplicate_ticker_dropped(self):
        ts = datetime.now(timezone.utc)
        t = Ticker(symbol="BTC/USDT", price=50000.0, volume=100.0, timestamp=ts, event_id="e1")
        self.assertTrue(self.md.ingest_ticker(t))
        # Same event_id again -> duplicate
        t2 = Ticker(symbol="BTC/USDT", price=50001.0, volume=100.0, timestamp=ts, event_id="e1")
        self.assertFalse(self.md.ingest_ticker(t2))
        self.assertEqual(self.md.duplicates_dropped, 1)
        # Price must remain first value
        self.assertEqual(run(self.md.get_ticker("BTC/USDT")).price, 50000.0)

    def test_out_of_order_dropped(self):
        now = datetime.now(timezone.utc)
        t1 = Ticker(symbol="BTC/USDT", price=100.0, volume=1.0,
                    timestamp=now, event_id="a")
        t2 = Ticker(symbol="BTC/USDT", price=200.0, volume=1.0,
                    timestamp=now - timedelta(seconds=60), event_id="b")
        self.assertTrue(self.md.ingest_ticker(t1))
        self.assertFalse(self.md.ingest_ticker(t2))
        self.assertEqual(self.md.out_of_order_dropped, 1)

    def test_reconnect_keeps_dedup(self):
        ts = datetime.now(timezone.utc)
        t = Ticker(symbol="BTC/USDT", price=100.0, volume=1.0, timestamp=ts, event_id="keep")
        self.assertTrue(self.md.ingest_ticker(t))
        run(self.md.reconnect())
        # Replay after reconnect must still be seen as duplicate
        t2 = Ticker(symbol="BTC/USDT", price=100.0, volume=1.0, timestamp=ts, event_id="keep")
        self.assertFalse(self.md.ingest_ticker(t2))
        self.assertEqual(self.md.reconnects, 1)

    def test_candle_ingest_sorted_and_deduped(self):
        base = datetime.now(timezone.utc)
        c1 = Candle(symbol="BTC/USDT", open=1, high=1, low=1, close=100, volume=10,
                    timestamp=base, interval="5m", event_id="c1")
        c2 = Candle(symbol="BTC/USDT", open=1, high=1, low=1, close=101, volume=10,
                    timestamp=base + timedelta(minutes=5), interval="5m", event_id="c2")
        # out of order input + duplicate
        accepted = self.md.ingest_candles("BTC/USDT", "5m", [c2, c1, c1])
        self.assertEqual(len(accepted), 2)
        stored = run(self.md.get_candles("BTC/USDT", "5m", 10))
        self.assertEqual([c.close for c in stored], [100, 101])

    def test_handle_message_never_raises(self):
        self.md.register_callback(lambda m: (_ for _ in ()).throw(ValueError("cb fail")))
        try:
            self.md.handle_message({'type': 'x', 'event_id': 'm1'})
        except Exception:
            self.fail("handle_message raised")


if __name__ == '__main__':
    unittest.main()
