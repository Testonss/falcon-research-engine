"""Terminal event-bridge acceptance tests; no display or network required."""

import ast
import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
import queue
import sys
import threading
import time
import unittest

from market_data import OrderBook, OrderBookEntry, Ticker
from market_data.health import ConnectionState
from market_data.normalization import MarketLiquidityEvent
from market_data.providers.base import LiveMarketDataProvider
from falcon_terminal.bridge import TerminalBridge
from falcon_terminal.models import AssetSnapshot, TerminalSnapshot


EVENT_TIME = datetime(2020, 1, 1, tzinfo=timezone.utc)


class FakeClock:
    """Provider freshness can change without waiting for a real stale timeout."""

    def __init__(self):
        self.value = 100.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += seconds


class FakeLiveProvider(LiveMarketDataProvider):
    """A controllable feed that owns a genuine cancellable asyncio reader."""

    provider_name = "test-feed"

    def __init__(self, symbol="BTC/USDT", *, clock=None, fail=False,
                 block_connect=False):
        self.clock = clock or FakeClock()
        super().__init__(stale_after_s=5.0, now_fn=self.clock)
        self.symbol = symbol
        self.allow_connect = threading.Event()
        if not fail:
            self.allow_connect.set()
        self.block_connect = block_connect
        self.connect_entered = threading.Event()
        self.connect_cancelled = threading.Event()
        self.reader_stopped = threading.Event()
        self.updates = queue.Queue()
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.call_threads = set()
        self.ticker = None
        self.book = None
        self.liquidity = None

    def feed(self, price, *, bid=None, ask=None, liquidity=None):
        """Queue a received event; the worker, not the test thread, accepts it."""
        accepted = threading.Event()
        self.updates.put((price, bid, ask, liquidity, accepted))
        return accepted

    async def connect(self):
        self.call_threads.add(threading.get_ident())
        self.connect_calls += 1
        self.connect_entered.set()
        self._sm.force(ConnectionState.CONNECTING)
        if self.block_connect:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.connect_cancelled.set()
                raise
        if not self.allow_connect.is_set():
            raise OSError("test feed temporarily unavailable")
        self._sm.force(ConnectionState.CONNECTED)
        await self._ensure_reader(self._read)

    async def _read(self):
        try:
            while True:
                self._accept_updates()
                await asyncio.sleep(0.001)
        finally:
            self.reader_stopped.set()

    async def disconnect(self):
        self.call_threads.add(threading.get_ident())
        self.disconnect_calls += 1
        await self._stop_reader()
        self._sm.force(ConnectionState.STOPPED)

    def _accept_updates(self):
        while True:
            try:
                price, bid, ask, liquidity, accepted = self.updates.get_nowait()
            except queue.Empty:
                break
            self.ticker = Ticker(self.symbol, price, 0.0, EVENT_TIME)
            self.book = OrderBook(
                self.symbol,
                [] if bid is None else [OrderBookEntry(bid, 1.0, "bid")],
                [] if ask is None else [OrderBookEntry(ask, 1.0, "ask")],
                EVENT_TIME,
            )
            self.liquidity = liquidity
            self._note_accepted()
            accepted.set()
    async def get_ticker(self, symbol=""):
        self.call_threads.add(threading.get_ident())
        return self.ticker

    async def get_order_book(self, symbol="", depth=10):
        self.call_threads.add(threading.get_ident())
        return self.book

    async def get_candles(self, symbol, interval, limit=100):
        raise AssertionError("The terminal must not fetch historical candles")

    def get_liquidity_event(self, symbol=""):
        return self.liquidity

    def get_health(self):
        health = super().get_health()
        if self.liquidity is not None:
            health.update(pool=self.liquidity.pool_address, dex=self.liquidity.dex)
        return health


class TerminalBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridges = []

    def tearDown(self):
        for bridge in self.bridges:
            bridge.stop()
        for bridge in self.bridges:
            self.assertTrue(bridge.join(2.0), "Terminal worker did not shut down")

    def bridge(self, providers):
        bridge = TerminalBridge(providers=providers, refresh_interval=0.01,
                                retry_interval=0.02)
        self.bridges.append(bridge)
        bridge.start()
        return bridge

    def wait_snapshot(self, bridge, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            snapshot = bridge.poll()
            if snapshot is not None:
                latest = snapshot
                if predicate(snapshot):
                    return snapshot
            threading.Event().wait(0.005)
        self.fail("No matching terminal snapshot; latest={!r}".format(latest))

    def wait_asset(self, bridge, key, predicate):
        def matches(snapshot):
            return any(asset.key == key and predicate(asset)
                       for asset in snapshot.assets)
        snapshot = self.wait_snapshot(bridge, matches)
        return snapshot, next(a for a in snapshot.assets if a.key == key)

    def test_market_updates_cross_bridge_without_ui_refresh_or_core_objects(self):
        provider = FakeLiveProvider()
        provider.feed(60000.0, bid=59999.0, ask=60001.0)
        bridge = self.bridge({"exchange:BTCUSDT": provider})
        first, asset = self.wait_asset(bridge, "exchange:BTCUSDT",
                                       lambda a: a.price == 60000.0)
        self.assertEqual((asset.bid, asset.ask), (59999.0, 60001.0))
        self.assertTrue(asset.connected)
        self.assertFalse(asset.stale)
        self.assertEqual(asset.messages, 1)
        self.assertIsInstance(asset.observed_at, str)
        self.assertNotEqual(asset.observed_at, EVENT_TIME.isoformat())
        self.assertIsInstance(first.assets, tuple)
        self.assertIsInstance(asset, AssetSnapshot)

        provider.feed(60010.0, bid=60009.0, ask=60011.0)
        second, updated = self.wait_asset(bridge, "exchange:BTCUSDT",
                                          lambda a: a.price == 60010.0)
        self.assertGreater(second.sequence, first.sequence)
        self.assertEqual(updated.messages, 2)
        self.assertEqual((updated.bid, updated.ask), (60009.0, 60011.0))
        self.assertNotIn(threading.get_ident(), provider.call_threads)

    def test_slow_consumer_gets_latest_value_without_backlog(self):
        provider = FakeLiveProvider()
        bridge = self.bridge({"feed:BTCUSDT": provider})
        for value in range(20):
            self.assertTrue(provider.feed(60000.0 + value).wait(1.0))
        # Stop joins the producer so the following two reads cannot race it.
        bridge.stop()
        self.assertTrue(bridge.join(2.0))
        latest = bridge.poll()
        self.assertIsNotNone(latest)
        self.assertEqual(latest.assets[0].price, 60019.0)
        self.assertIsNone(bridge.poll(), "Old snapshots accumulated behind latest")

    def test_failed_provider_does_not_block_other_market_and_retries(self):
        healthy = FakeLiveProvider()
        healthy.feed(60000.0)
        recovering = FakeLiveProvider("SOL/USD", fail=True)
        recovering.feed(150.0)
        bridge = self.bridge({"healthy:BTCUSDT": healthy,
                              "recovering:SOL": recovering})
        snapshot = self.wait_snapshot(
            bridge,
            lambda s: any(a.key == "healthy:BTCUSDT" and a.price == 60000.0
                          for a in s.assets)
            and any(a.key == "recovering:SOL" and a.error for a in s.assets),
        )
        failed = next(a for a in snapshot.assets if a.key == "recovering:SOL")
        self.assertIsNone(failed.price)
        self.assertFalse(failed.connected)
        self.assertIn("temporarily unavailable", failed.error)
        self.assertTrue(bridge.is_alive)

        recovering.allow_connect.set()
        _, asset = self.wait_asset(bridge, "recovering:SOL",
                                    lambda a: a.price == 150.0 and a.connected)
        self.assertIsNone(asset.error)
        self.assertGreaterEqual(recovering.connect_calls, 2)
        self.assertEqual(healthy.connect_calls, 1)

    def test_stale_health_changes_without_new_trade_then_recovers(self):
        clock = FakeClock()
        provider = FakeLiveProvider(clock=clock)
        provider.feed(60000.0)
        bridge = self.bridge({"feed:BTCUSDT": provider})
        _, initial = self.wait_asset(bridge, "feed:BTCUSDT",
                                     lambda a: a.price is not None)
        self.assertFalse(initial.stale)
        clock.advance(6.0)
        _, stale = self.wait_asset(bridge, "feed:BTCUSDT", lambda a: a.stale)
        self.assertEqual(stale.state, "STALE")
        self.assertEqual(stale.age_s, 6.0)
        self.assertEqual(stale.observed_at, initial.observed_at)
        self.assertEqual(stale.price, initial.price)

        provider.feed(60001.0)
        _, fresh = self.wait_asset(bridge, "feed:BTCUSDT",
                                   lambda a: a.price == 60001.0 and not a.stale)
        self.assertEqual(fresh.state, "CONNECTED")
        self.assertEqual(fresh.age_s, 0.0)

    def test_solana_optional_data_stays_missing_and_measured_zero_is_preserved(self):
        provider = FakeLiveProvider("SOL/USD")
        liquidity = MarketLiquidityEvent(
            provider="test-dex", instrument="solana:mint", event_time=EVENT_TIME,
            price_usd=150.0, liquidity_usd=1500000.0, buys_5m=0,
            sells_5m=12, pool_address="pool-1", dex="test-dex",
        )
        provider.feed(150.0, liquidity=liquidity)
        bridge = self.bridge({"solana:mint": provider})
        _, asset = self.wait_asset(bridge, "solana:mint", lambda a: a.price == 150.0)
        self.assertIsNone(asset.bid)
        self.assertIsNone(asset.ask)
        self.assertIsNone(asset.volume_5m)
        self.assertEqual(asset.buys_5m, 0)
        self.assertEqual(asset.sells_5m, 12)
        self.assertEqual(asset.liquidity_usd, 1500000.0)
        self.assertEqual((asset.pool, asset.dex), ("pool-1", "test-dex"))

    def test_connected_without_market_data_does_not_invent_price(self):
        provider = FakeLiveProvider()
        bridge = self.bridge({"feed:BTCUSDT": provider})
        _, asset = self.wait_asset(bridge, "feed:BTCUSDT", lambda a: a.connected)
        self.assertIsNone(asset.price)
        self.assertIsNone(asset.bid)
        self.assertIsNone(asset.ask)
        self.assertIsNone(asset.observed_at)
        self.assertIsNone(asset.age_s)
        self.assertEqual(asset.messages, 0)

    def test_close_during_bootstrap_cancels_connect_and_cleans_up(self):
        provider = FakeLiveProvider(block_connect=True)
        bridge = self.bridge({"blocked:BTCUSDT": provider})
        self.assertTrue(provider.connect_entered.wait(1.0))
        bridge.start()
        self.wait_asset(bridge, "blocked:BTCUSDT", lambda a: not a.connected)
        bridge.stop()
        bridge.stop()
        self.assertTrue(bridge.join(2.0))
        self.assertFalse(bridge.is_alive)
        self.assertTrue(provider.connect_cancelled.is_set())
        self.assertGreaterEqual(provider.disconnect_calls, 1)
        self.assertEqual(provider.connect_calls, 1)

    def test_stop_is_idempotent_and_leaves_no_reader_or_worker(self):
        provider = FakeLiveProvider()
        provider.feed(60000.0)
        bridge = self.bridge({"feed:BTCUSDT": provider})
        self.wait_asset(bridge, "feed:BTCUSDT", lambda a: a.price is not None)
        bridge.start()
        bridge.stop()
        self.assertTrue(bridge.join(2.0))
        bridge.stop()
        self.assertTrue(bridge.join(0.0))
        self.assertFalse(bridge.is_alive)
        self.assertTrue(provider.reader_stopped.is_set())
        self.assertEqual(provider.reader_generations, 1)
        self.assertEqual(provider.connect_calls, 1)
        self.assertEqual(provider.disconnect_calls, 1)

    def test_stop_before_start_is_safe(self):
        bridge = TerminalBridge(providers={})
        bridge.stop()
        bridge.stop()
        self.assertTrue(bridge.join(0.0))
        self.assertFalse(bridge.is_alive)


class TerminalBoundaryTests(unittest.TestCase):
    def test_snapshots_are_immutable_plain_data(self):
        asset = AssetSnapshot(key="feed:asset", symbol="ASSET/USD", provider="feed")
        snapshot = TerminalSnapshot(sequence=1, generated_at="now", assets=(asset,))
        with self.assertRaises(FrozenInstanceError):
            asset.price = 1.0
        with self.assertRaises(FrozenInstanceError):
            snapshot.sequence = 2
        for field in ("price", "bid", "ask", "liquidity_usd", "volume_5m",
                      "buys_5m", "sells_5m"):
            self.assertIsNone(getattr(asset, field))

    def test_frontend_imports_no_provider_strategy_or_execution_modules(self):
        source = Path(__file__).resolve().parents[1] / "falcon_terminal" / "app.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        forbidden = {"market_data", "execution", "exchange", "research",
                     "strategy", "risk", "portfolio", "paper"}
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertFalse(forbidden.intersection(name.split(".")[0]
                                                for name in imported), imported)


class TerminalFormattingTests(unittest.TestCase):
    def test_price_number_formatting(self):
        from falcon_terminal.app import change_text, compact_money, number
        self.assertEqual(number(None), "—")
        self.assertEqual(number(float("nan")), "—")
        self.assertEqual(number(float("inf")), "—")
        self.assertEqual(number(81204.214489), "81,204.21")
        self.assertEqual(number(0), "0.00")
        self.assertEqual(number(150.0), "150.00")
        tiny = number(0.000000341215)
        self.assertTrue(tiny.startswith("0.00000034"))
        self.assertNotIn("e", tiny.lower())

    def test_compact_money(self):
        from falcon_terminal.app import compact_money
        self.assertEqual(compact_money(None), "—")
        self.assertEqual(compact_money(-5), "—")
        self.assertEqual(compact_money(float("nan")), "—")
        self.assertEqual(compact_money(1834921), "$1.83M")
        self.assertEqual(compact_money(73400000000), "$73.40B")
        self.assertEqual(compact_money(5007.34), "$5.01K")
        self.assertEqual(compact_money(0), "$0.00")

    def test_change_text(self):
        from falcon_terminal.app import change_text
        self.assertEqual(change_text(None), "—")
        self.assertEqual(change_text(float("nan")), "—")
        self.assertEqual(change_text(2.345), "+2.35%")
        self.assertEqual(change_text(-0.5), "-0.50%")

    def test_missing_metrics_default_to_unavailable(self):
        asset = AssetSnapshot(key="k", symbol="S", provider="p")
        self.assertIsNone(asset.price)
        self.assertIsNone(asset.bid)
        self.assertIsNone(asset.ask)
        self.assertIsNone(asset.spread_bps)
        self.assertIsNone(asset.change_pct_24h)
        self.assertIsNone(asset.liquidity_usd)
        self.assertIsNone(asset.candle_close)
        self.assertIsNone(asset.candle_closed)
        self.assertEqual(asset.price_history, ())


class CandleFeedProvider(FakeLiveProvider):
    provider_name = "candle-feed"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.candle = None

    def get_live_candle(self):
        return self.candle


class TerminalSnapshotFieldTests(unittest.TestCase):
    def setUp(self):
        self.bridges = []

    def tearDown(self):
        for bridge in self.bridges:
            bridge.stop()
        for bridge in self.bridges:
            self.assertTrue(bridge.join(2.0), "Terminal worker did not shut down")

    def bridge(self, providers):
        bridge = TerminalBridge(providers=providers, refresh_interval=0.01,
                                retry_interval=0.02)
        self.bridges.append(bridge)
        bridge.start()
        return bridge

    def wait_asset(self, bridge, key, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            snapshot = bridge.poll()
            if snapshot is not None:
                latest = snapshot
                match = next((a for a in snapshot.assets
                              if a.key == key and predicate(a)), None)
                if match is not None:
                    return snapshot, match
            threading.Event().wait(0.005)
        self.fail("No matching asset; latest={!r}".format(latest))

    def test_binance_style_fields_flow_to_snapshot(self):
        from market_data import Candle
        provider = CandleFeedProvider("BTC/USDT")
        provider.feed(67200.0, bid=67199.0, ask=67201.0)
        bridge = self.bridge({"binance:BTCUSDT": provider})
        self.wait_asset(bridge, "binance:BTCUSDT", lambda a: a.price == 67200.0)
        provider.candle = Candle(
            symbol="BTC/USDT", open=67000.0, high=67300.0, low=66900.0,
            close=67200.0, volume=100.5,
            timestamp=datetime.now(timezone.utc), interval="1m", is_closed=False)
        provider.ticker = Ticker("BTC/USDT", 67200.0, 10.0, EVENT_TIME,
                                 change_pct_24h=1.25)
        _, asset = self.wait_asset(
            bridge, "binance:BTCUSDT",
            lambda a: a.candle_close == 67200.0 and a.change_pct_24h == 1.25)
        self.assertEqual(
            (asset.candle_open, asset.candle_high, asset.candle_low), (67000.0, 67300.0, 66900.0))
        self.assertEqual(asset.candle_interval, "1m")
        self.assertFalse(asset.candle_closed)
        self.assertAlmostEqual(asset.spread_bps, 2.0 / 67199.0 * 10000.0)
        self.assertGreater(len(asset.price_history), 0)
        self.assertEqual(asset.price_history[-1], 67200.0)

    def test_price_history_bounded_and_latest_wins(self):
        from falcon_terminal.bridge import PRICE_HISTORY_MAX
        provider = FakeLiveProvider()
        bridge = self.bridge({"feed:X": provider})
        total = PRICE_HISTORY_MAX + 50
        for i in range(total):
            self.assertTrue(provider.feed(1.0 + i).wait(2.0))
        last_price = float(total)  # 1.0 + (total - 1)
        _, asset = self.wait_asset(
            bridge, "feed:X", lambda a: a.price == last_price)
        self.assertEqual(len(asset.price_history), PRICE_HISTORY_MAX)
        self.assertEqual(asset.price_history[-1], last_price)
        self.assertEqual(list(asset.price_history),
                         sorted(asset.price_history))

    def test_switching_bridges_clears_old_asset_state(self):
        first = FakeLiveProvider("BTC/USDT")
        first.feed(60000.0)
        bridge_a = self.bridge({"feed:A": first})
        self.wait_asset(bridge_a, "feed:A", lambda a: a.price == 60000.0)
        bridge_a.stop()
        self.assertTrue(bridge_a.join(2.0))
        second = FakeLiveProvider("ETH/USDT")
        bridge_b = self.bridge({"feed:B": second})
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            snapshot = bridge_b.poll()
            if snapshot is not None:
                for asset in snapshot.assets:
                    self.assertNotEqual(asset.price, 60000.0)
                    self.assertNotEqual(asset.key, "feed:A")
                if any(a.key == "feed:B" for a in snapshot.assets):
                    break
            threading.Event().wait(0.005)

    def test_single_worker_thread_and_bounded_backlog(self):
        provider = FakeLiveProvider()
        provider.feed(60000.0)
        bridge = self.bridge({"feed:X": provider})
        bridge.start()  # second start must not spawn another worker
        workers = [t for t in threading.enumerate() if t.name == "FalconMarketData"]
        self.assertEqual(len(workers), 1)
        deadline = time.monotonic() + 0.25
        seen = 0
        while time.monotonic() < deadline:
            if bridge.poll() is not None:
                seen += 1
            threading.Event().wait(0.005)
        # refresh_interval=0.01 allows ~25 publishes in this window; the cap
        # proves the rate stays proportional to the interval (no spin loop).
        self.assertLessEqual(seen, 32)
        bridge.stop()
        self.assertTrue(bridge.join(2.0))
        drained = 0
        while bridge.poll() is not None:
            drained += 1
        self.assertLessEqual(drained, 3, "queue must stay bounded (latest wins)")

    def test_provider_change_pct_and_candle_apis(self):
        from market_data.providers.binance import BinanceLiveMarketDataProvider
        import inspect
        self.assertIn("change_pct_24h", inspect.signature(Ticker).parameters)
        self.assertIsNone(LiveMarketDataProvider.get_live_candle(FakeLiveProvider()))
        self.assertTrue(callable(BinanceLiveMarketDataProvider.get_live_candle))


class TerminalImportBoundaryTests(unittest.TestCase):
    def test_core_cli_imports_no_gui_stack(self):
        source = Path(__file__).resolve().parents[1] / "scripts" / "falcon.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        top_level = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module)
        self.assertFalse({"tkinter", "falcon_terminal", "PySide6", "PyQt6"}
                         & {n.split(".")[0] for n in top_level})
        # Core CLI must survive a machine with no GUI stack at all.
        import subprocess
        probe = ("import sys\n"
                 "sys.modules['tkinter'] = None\n"
                 "sys.modules['PySide6'] = None\n"
                 "from scripts.falcon import main\n"
                 "try:\n"
                 "    main(['--help'])\n"
                 "except SystemExit as e:\n"
                 "    sys.exit(e.code)\n")
        proc = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parents[1]), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("terminal", proc.stdout)

    def test_no_trading_ui_actions_exist(self):
        import re
        for rel in ("falcon_terminal/app.py", "falcon_terminal/__main__.py",
                    "falcon_terminal/bridge.py"):
            source = Path(__file__).resolve().parents[1] / rel
            text = source.read_text(encoding="utf-8")
            for word in ("buy", "sell", "swap", "wallet", "seed",
                         "private_key", "apikey", "api_key", "place_order",
                         "market_order", "limit_order", "connect_wallet"):
                self.assertIsNone(
                    re.search(r"\b%s\b" % word, text, re.IGNORECASE),
                    f"{rel} contains trading word {word!r}")
        app_text = (Path(__file__).resolve().parents[1]
                    / "falcon_terminal" / "app.py").read_text(encoding="utf-8")
        buttons = re.findall(r"tk\.Button\(.*?text=\"([^\"]+)\"", app_text)
        self.assertTrue(buttons)
        for label in buttons:
            self.assertNotIn(label.strip().upper(),
                             {"BUY", "SELL", "SWAP", "ORDER", "TRADE",
                              "CONNECT WALLET"})


if __name__ == "__main__":
    unittest.main()