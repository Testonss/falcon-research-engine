"""Live market-data tests (v0.2). All network-independent: mocks/fakes only.

Covers: Binance symbol normalization, REST/WS parsing, forming vs closed
kline correctness, dedup, out-of-order, reconnect lifecycle, stale health,
shutdown, credential-free safety, Solana mint identity, pool selection,
price integrity, backend failures, abstraction consistency, recording,
and backward compatibility of the historical MarketDataHandler/Candle.
"""
import asyncio
import json
import os
import unittest
from datetime import datetime, timezone

from config import ConfigManager
from market_data import Candle, MarketDataHandler, Ticker
from market_data.assets import (
    OnChainAsset,
    is_valid_solana_mint,
    validate_solana_mint,
)
from market_data.health import ConnectionState, ConnectionStateMachine
from market_data.normalization import (
    MarketLiquidityEvent,
    check_ohlc,
    is_valid_price,
)
from market_data.providers.base import LiveMarketDataProvider
from market_data.providers.binance import (
    BinanceLiveMarketDataProvider,
    WsClosed,
    combined_stream_url,
    kline_stream_name,
    parse_book_ticker,
    parse_depth,
    parse_klines,
    parse_ticker_24hr,
    parse_ws_message,
    to_binance_symbol,
    to_falcon_symbol,
)
from market_data.providers.solana import (
    BackendError,
    DexScreenerBackend,
    NoMarketError,
    SolanaDataBackend,
    SolanaDexMarketDataProvider,
    apply_decimals,
    normalize_pair,
    select_pool,
)
from market_data.recording import LiveEventRecorder, read_records

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB4Y"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLM = "So11111111111111111111111111111111111111112"

T0 = 1710000000000


def run(coro):
    return asyncio.run(coro)


def kline_row(ot, o="67000", h="67300", l="66900", c="67200", v="100.5", ct=None):
    return [ot, o, h, l, c, v, ct if ct is not None else ot + 59999,
            "0", 0, "0", "0", "0"]


def ws_kline(open_ms, close="67200", closed=False, symbol="BTCUSDT", interval="1m"):
    return json.dumps({
        "stream": f"btcusdt@kline_{interval}",
        "data": {"e": "kline", "E": open_ms + 1000, "s": symbol,
                 "k": {"t": open_ms, "T": open_ms + 59999, "s": symbol, "i": interval,
                       "o": "67000", "c": close, "h": "67300", "l": "66900",
                       "v": "100.5", "x": closed}}})




def bonk_pair(pool="POOL1", dex="raydium", liq=850000.0, vol24=1200000.0,
              chain="solana", base=BONK, quote=SOLM, qsym="SOL",
              price_usd="0.0000215", price_native="0.00000123"):
    return {
        "chainId": chain, "dexId": dex, "pairAddress": pool,
        "baseToken": {"address": base, "name": "Bonk", "symbol": "BONK"},
        "quoteToken": {"address": quote, "name": "Wrapped SOL", "symbol": qsym},
        "priceNative": price_native, "priceUsd": price_usd,
        "txns": {"m5": {"buys": 120, "sells": 95}, "h1": {"buys": 1, "sells": 2}},
        "volume": {"m5": 45210.5, "h24": vol24},
        "liquidity": {"usd": liq, "base": 1000, "quote": 200},
        "fdv": 1500000000.0, "marketCap": 1400000000.0,
    }


class FakeBackend(SolanaDataBackend):
    def __init__(self, pairs=None, exc=None, meta=None):
        self._pairs = pairs if pairs is not None else []
        self.exc = exc
        self.calls = 0
        self._meta = meta or {"symbol": "BONK", "name": "Bonk", "decimals": 5}

    async def fetch_pairs(self, mint):
        self.calls += 1
        if self.exc:
            raise self.exc
        return [dict(p) for p in self._pairs]

    async def fetch_token_meta(self, mint):
        return dict(self._meta)


def fake_http(mapping):
    """mapping: path-substring -> (status, body)."""
    def _get(url, timeout):
        for key, val in mapping.items():
            if key in url:
                return val
        raise IOError(f"unexpected url {url}")
    return _get


class FakeWs:
    """Scripted WS connection: yields/raises script items, then idles."""

    def __init__(self, script):
        self.script = list(script)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for item in self.script:
            if isinstance(item, BaseException):
                raise item
            yield item
        await asyncio.sleep(3600)


class TestBinanceSymbolNormalization(unittest.TestCase):
    def test_conversions(self):
        self.assertEqual(to_binance_symbol("BTC/USDT"), "BTCUSDT")
        self.assertEqual(to_binance_symbol("BTCUSDT"), "BTCUSDT")
        self.assertEqual(to_binance_symbol(" btc/usdt "), "BTCUSDT")
        self.assertEqual(to_falcon_symbol("BTCUSDT"), "BTC/USDT")
        self.assertEqual(to_falcon_symbol("BTC/USDT"), "BTC/USDT")
        self.assertEqual(to_falcon_symbol("ETHUSDC"), "ETH/USDC")

    def test_invalid_rejected(self):
        for bad in ["", "   ", "BTC", "BTC/", "/USDT", "BTC/USDT!", "a" * 30, None, 123]:
            with self.assertRaises(ValueError, msg=f"{bad!r}"):
                to_binance_symbol(bad)
        with self.assertRaises(ValueError):
            to_falcon_symbol("ZZZUNKNOWNQQQ")
        with self.assertRaises(ValueError):
            kline_stream_name("BTCUSDT", "9m")

    def test_stream_names(self):
        self.assertEqual(kline_stream_name("BTC/USDT", "1m"), "btcusdt@kline_1m")
        url = combined_stream_url(["btcusdt@kline_1m", "btcusdt@bookTicker"])
        self.assertTrue(url.startswith("wss://stream.binance.com"))


class TestBinanceRestParsing(unittest.TestCase):
    def test_ticker_parsing(self):
        t = parse_ticker_24hr({"symbol": "BTCUSDT", "lastPrice": "67234.50",
                               "volume": "1234.5", "closeTime": T0})
        self.assertIsNotNone(t)
        self.assertEqual(t.symbol, "BTC/USDT")
        self.assertAlmostEqual(t.price, 67234.50)
        self.assertAlmostEqual(t.volume, 1234.5)

    def test_ticker_malformed_rejected(self):
        bad_payloads = [
            {}, {"symbol": "BTCUSDT"},
            {"symbol": "BTCUSDT", "lastPrice": "NaN", "volume": "1", "closeTime": T0},
            {"symbol": "BTCUSDT", "lastPrice": "Infinity", "volume": "1", "closeTime": T0},
            {"symbol": "BTCUSDT", "lastPrice": "-5", "volume": "1", "closeTime": T0},
            {"symbol": "BTCUSDT", "lastPrice": "0", "volume": "1", "closeTime": T0},
            {"symbol": "BTCUSDT", "lastPrice": "abc", "volume": "1", "closeTime": T0},
            {"symbol": "BTCUSDT", "lastPrice": "5", "volume": "-1", "closeTime": T0},
            {"symbol": "BTCUSDT", "lastPrice": "5", "volume": "1"},  # no time
            {"symbol": "???", "lastPrice": "5", "volume": "1", "closeTime": T0},
            "not-a-dict",
        ]
        for p in bad_payloads:
            self.assertIsNone(parse_ticker_24hr(p), msg=f"{p!r}")

    def test_candle_rest_parsing(self):
        rows = [kline_row(T0), kline_row(T0 + 60000, c="67300")]
        candles, rejected = parse_klines(rows, "BTC/USDT", "1m")
        self.assertEqual(len(candles), 2)
        self.assertEqual(rejected, 0)
        self.assertTrue(all(c.is_closed for c in candles))
        self.assertLess(candles[0].timestamp, candles[1].timestamp)

    def test_candle_rest_rejects_bad_rows(self):
        rows = [kline_row(T0),
                ["bad"],
                kline_row(T0 + 60000, h="66000"),  # impossible: high < open/close
                kline_row(T0 + 120000, c="NaN"),
                kline_row(T0 + 180000, v="-3")]
        candles, rejected = parse_klines(rows, "BTC/USDT", "1m")
        self.assertEqual(len(candles), 1)
        self.assertEqual(rejected, 4)
        self.assertFalse(isinstance(rows, dict))
        c2, r2 = parse_klines({"nope": 1}, "BTC/USDT", "1m")
        self.assertEqual((c2, r2), ([], 1))

    def test_orderbook_parsing(self):
        ob = parse_depth({"bids": [["67200", "1.5"], ["67190", "2.0"]],
                          "asks": [["67210", "1.2"], ["67220", "0.8"]]},
                         "BTC/USDT", 10)
        self.assertIsNotNone(ob)
        self.assertAlmostEqual(ob.best_bid(), 67200.0)
        self.assertAlmostEqual(ob.best_ask(), 67210.0)
        self.assertIsNotNone(ob.mid_price())
        self.assertGreater(ob.spread_pct(), 0)
        parsed = parse_book_ticker({"symbol": "BTCUSDT", "bidPrice": "67200",
                                    "askPrice": "67210"})
        self.assertEqual(parsed, ("BTC/USDT", 67200.0, 67210.0))
        # crossed book rejected
        self.assertIsNone(parse_book_ticker({"symbol": "BTCUSDT", "bidPrice": "10",
                                             "askPrice": "9"}))
        self.assertIsNone(parse_depth({"bids": "x", "asks": []}, "BTC/USDT"))
        empty = parse_depth({}, "BTC/USDT")
        self.assertIsNotNone(empty)
        self.assertEqual(empty.bids, [])
        self.assertIsNone(parse_book_ticker({"symbol": "BTCUSDT", "bidPrice": "NaN",
                                             "askPrice": "9"}))


class TestBinanceWsParsing(unittest.TestCase):
    def test_kline_forming_and_finalized(self):
        f = parse_ws_message(ws_kline(T0, closed=False))
        self.assertEqual(f["kind"], "kline")
        self.assertFalse(f["is_closed"])
        c = parse_ws_message(ws_kline(T0, closed=True))
        self.assertTrue(c["is_closed"])
        self.assertIsNotNone(c["open_time"])

    def test_bookticker_miniticker_trade(self):
        bt = parse_ws_message(json.dumps(
            {"data": {"e": "bookTicker", "s": "BTCUSDT", "b": "67200", "a": "67210"}}))
        self.assertEqual(bt["kind"], "bookTicker")
        # Real Binance bookTicker frames omit the "e" marker entirely.
        bt2 = parse_ws_message(json.dumps(
            {"stream": "btcusdt@bookTicker",
             "data": {"u": 1, "s": "BTCUSDT", "b": "67200", "B": "1.5",
                      "a": "67210", "A": "1.2"}}))
        self.assertIsNotNone(bt2)
        self.assertEqual(bt2["kind"], "bookTicker")
        mt = parse_ws_message(json.dumps(
            {"data": {"e": "24hrMiniTicker", "s": "BTCUSDT", "c": "67234.5"}}))
        self.assertEqual(mt["kind"], "miniTicker")
        tr = parse_ws_message(json.dumps(
            {"data": {"e": "trade", "s": "BTCUSDT", "p": "67200", "q": "0.1", "T": T0}}))
        self.assertEqual(tr["kind"], "trade")

    def test_hostile_ws_input_never_raises(self):
        hostile = ["", "{bad json", "[1,2]", "null", "42",
                   json.dumps({"data": {"e": "kline", "k": {}}}),
                   json.dumps({"data": {"e": "kline", "s": "BTCUSDT",
                                         "k": {"t": T0, "o": "1", "h": "0.5",
                                               "l": "0.5", "c": "1", "v": "1"}}}),
                   json.dumps({"data": {"e": "bookTicker", "s": "BTCUSDT",
                                         "b": "10", "a": "9"}}),
                   json.dumps({"data": {"e": "bookTicker", "s": "BTCUSDT",
                                         "b": "NaN", "a": "9"}}),
                   json.dumps({"data": {"e": "weird", "s": "BTCUSDT"}})]
        for h in hostile:
            try:
                r = parse_ws_message(h)
            except Exception as e:  # noqa: BLE001
                self.fail(f"parse_ws_message raised on {h!r}: {e}")
            if h == hostile[-1]:
                self.assertIsNone(r)


def make_binance_provider(http_map=None, ws_factory=None, **kw):
    ticker = {"symbol": "BTCUSDT", "lastPrice": "67234.50",
              "volume": "1234.5", "closeTime": T0}
    book = {"symbol": "BTCUSDT", "bidPrice": "67200", "askPrice": "67210"}
    depth = {"bids": [["67200", "1.5"]], "asks": [["67210", "1.2"]]}
    klines = [kline_row(T0 - 60000), kline_row(T0)]
    mapping = {"/api/v3/klines": (200, json.dumps(klines)),
               "/api/v3/ticker/24hr": (200, json.dumps(ticker)),
               "/api/v3/ticker/bookTicker": (200, json.dumps(book)),
               "/api/v3/depth": (200, json.dumps(depth))}
    if http_map:
        mapping.update(http_map)
    kw.setdefault("http_get", fake_http(mapping))
    if ws_factory is not None:
        kw["ws_factory"] = ws_factory
    return BinanceLiveMarketDataProvider("BTCUSDT", "1m", **kw)


class TestKlineCorrectness(unittest.TestCase):
    def test_forming_updates_do_not_multiply_bars(self):
        p = make_binance_provider()
        run(p.connect())
        try:
            p._handle_ws_text(ws_kline(T0, close="67200", closed=False))
            p._handle_ws_text(ws_kline(T0, close="67250", closed=False))
            p._handle_ws_text(ws_kline(T0, close="67280", closed=False))
            bars = run(p.get_candles(limit=100))
            # history unchanged (bootstrap 2 bars); exactly one forming state
            self.assertEqual(len(bars), 2 + 1)  # +1 forming appended for readers
            forming = p.forming_candle()
            self.assertIsNotNone(forming)
            self.assertFalse(forming.is_closed)
            self.assertAlmostEqual(forming.close, 67280.0)
            closed = [b for b in p._candles if b.is_closed]
            self.assertEqual(len(closed), 2)
        finally:
            run(p.disconnect())

    def test_finalized_replaces_forming(self):
        p = make_binance_provider()
        run(p.connect())
        try:
            n0 = len(p._candles)
            p._handle_ws_text(ws_kline(T0, close="67280", closed=False))
            self.assertIsNotNone(p.forming_candle())
            p._handle_ws_text(ws_kline(T0, close="67300", closed=True))
            self.assertIsNone(p.forming_candle())
            self.assertEqual(len(p._candles), n0 + 1)
            self.assertTrue(p._candles[-1].is_closed)
            self.assertAlmostEqual(p._candles[-1].close, 67300.0)
        finally:
            run(p.disconnect())

    def test_duplicate_closed_kline_accepted_once(self):
        p = make_binance_provider()
        run(p.connect())
        try:
            n0 = len(p._candles)
            msg = ws_kline(T0 + 120000, close="67250", closed=True)
            p._handle_ws_text(msg)
            p._handle_ws_text(msg)  # redelivery (reconnect replay)
            self.assertEqual(len(p._candles), n0 + 1)
            self.assertGreaterEqual(p._sm.duplicates_dropped, 1)
        finally:
            run(p.disconnect())

    def test_out_of_order_kline_dropped(self):
        p = make_binance_provider()
        run(p.connect())
        try:
            n0 = len(p._candles)
            p._handle_ws_text(ws_kline(T0 + 600000, close="67290", closed=True))
            n1 = len(p._candles)
            self.assertEqual(n1, n0 + 1)
            p._handle_ws_text(ws_kline(T0 + 120000, close="67260", closed=True))
            self.assertEqual(len(p._candles), n1)
            self.assertGreaterEqual(p._sm.out_of_order_dropped, 1)
        finally:
            run(p.disconnect())

    def test_old_candle_cannot_overwrite_newer(self):
        p = make_binance_provider()
        run(p.connect())
        try:
            p._handle_ws_text(ws_kline(T0 + 600000, close="67290", closed=False))
            first = p.forming_candle()
            self.assertIsNotNone(first)
            p._handle_ws_text(ws_kline(T0 + 60000, close="67100", closed=False))
            second = p.forming_candle()
            self.assertIs(first, second)
            self.assertAlmostEqual(second.close, 67290.0)
        finally:
            run(p.disconnect())

    def test_historical_candle_defaults_closed(self):
        c = Candle(symbol="BTC/USDT", open=1, high=1, low=1, close=1,
                   volume=1, timestamp=datetime.now(timezone.utc), interval="5m")
        self.assertTrue(c.is_closed)


class TestReconnectLifecycle(unittest.TestCase):
    def test_ws_connection_iteration_protocol(self):
        import inspect
        from market_data.providers.binance import StdlibWsConnection
        conn = StdlibWsConnection("wss://stream.binance.com:9443/stream?streams=x")
        self.assertFalse(inspect.iscoroutinefunction(conn.__aiter__))
        self.assertIs(conn.__aiter__(), conn)
        self.assertTrue(inspect.iscoroutinefunction(conn.__anext__))

    def test_reconnect_resubscribes_without_duplicate_tasks(self):
        conns = {"n": 0}

        def factory(url):
            conns["n"] += 1
            if conns["n"] == 1:
                return FakeWs([ws_kline(T0, closed=True), WsClosed("drop")])
            return FakeWs([ws_kline(T0 + 60000, closed=True)])

        async def scenario():
            p = make_binance_provider(ws_factory=factory)
            p._sm.backoff_delay = lambda attempt, **k: 0.01
            await p.connect()
            await asyncio.sleep(0.5)
            result = (dict(conns=conns["n"], reconnects=p._sm.reconnects,
                           generations=p.reader_generations,
                           running=p._reader_running(),
                           candles=len(p._candles),
                           state=p.get_health()["state"],
                           tick=(await p.get_ticker())))
            await p.disconnect()
            result["running_after"] = p._reader_running()
            return result

        r = run(scenario())
        self.assertGreaterEqual(r["conns"], 2)
        self.assertGreaterEqual(r["reconnects"], 1)
        self.assertEqual(r["generations"], 1)
        self.assertTrue(r["running"])
        self.assertFalse(r["running_after"])
        self.assertIsNotNone(r["tick"])
        self.assertGreater(r["tick"].price, 0)
        # post-reconnect messages recover the state machine to CONNECTED
        self.assertEqual(r["state"], "CONNECTED")
    def test_clean_disconnect_idempotent(self):
        p = make_binance_provider(ws_factory=lambda url: FakeWs([]))
        run(p.connect())
        run(p.disconnect())
        run(p.disconnect())  # must not raise
        from market_data.health import ConnectionState as CS
        self.assertEqual(p._sm.state, CS.STOPPED)
        self.assertFalse(p._reader_running())

    def test_ctrl_c_safe_shutdown_path(self):
        p = make_binance_provider(ws_factory=lambda url: FakeWs([]))
        run(p.connect())
        run(p.disconnect())
        self.assertFalse(p._reader_running())
        # second disconnect + health after stop never raise
        run(p.disconnect())
        h = p.get_health()
        self.assertEqual(h["state"], "STOPPED")


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class TestHealthAndStale(unittest.TestCase):
    def test_stale_transition_and_recovery(self):
        clock = FakeClock()
        p = make_binance_provider(ws_factory=lambda url: FakeWs([]), now_fn=clock)
        run(p.connect())
        try:
            h0 = p.get_health()
            self.assertFalse(h0["stale"])
            clock.advance(120.0)
            h1 = p.get_health()
            self.assertTrue(h1["stale"])
            self.assertEqual(h1["state"], "STALE")
            # new event recovers
            p._handle_ws_text(ws_kline(T0 + 900000, closed=True))
            h2 = p.get_health()
            self.assertFalse(h2["stale"])
            self.assertEqual(h2["state"], "CONNECTED")
        finally:
            run(p.disconnect())

    def test_health_schema(self):
        p = make_binance_provider()
        h = p.get_health()
        for key in ("provider", "connected", "stale", "last_message_at",
                    "seconds_since_message", "reconnects", "messages_received",
                    "duplicates_dropped", "out_of_order_dropped"):
            self.assertIn(key, h)
        self.assertEqual(h["provider"], "binance")

    def test_state_machine_rejects_illegal_jump(self):
        sm = ConnectionStateMachine("t")
        self.assertTrue(sm.transition(ConnectionState.CONNECTING))
        # CONNECTING -> STALE is illegal (must go through CONNECTED)
        self.assertFalse(sm.transition(ConnectionState.STALE))
        self.assertEqual(sm.state, ConnectionState.CONNECTING)
        # CONNECTING -> CONNECTED is legal
        self.assertTrue(sm.transition(ConnectionState.CONNECTED))


class TestSafetyBoundaries(unittest.TestCase):
    def test_no_credentials_required(self):
        import inspect
        sig = inspect.signature(BinanceLiveMarketDataProvider.__init__)
        src = inspect.getsource(BinanceLiveMarketDataProvider)
        for token in ("api_key", "apikey", "api-secret", "secret"):
            self.assertNotIn(token, src.lower())
        p = make_binance_provider()
        run(p.connect())
        run(p.disconnect())

    def test_no_trading_endpoints(self):
        import pathlib
        for rel in ("market_data/providers/binance.py",
                    "market_data/providers/solana.py",
                    "market_data/providers/base.py"):
            src = pathlib.Path(rel).read_text(encoding="utf-8")
            for bad in ("/api/v3/order", "X-MBX-APIKEY", "recvWindow",
                        "private_key", "seed_phrase", "sign_transaction",
                        "submit_transaction", "/axiom", "axiom.trade"):
                self.assertNotIn(bad, src,
                                 msg=f"{rel} contains forbidden {bad!r}")
        # functional: trading URLs are blocked before any network use
        from market_data.providers.binance import _default_http_get
        with self.assertRaises(ValueError):
            _default_http_get("https://api.binance.com/api/v3/order?symbol=BTCUSDT", 1)
        with self.assertRaises(ValueError):
            _default_http_get("https://api.binance.com/api/v3/account", 1)


class TestSolanaProvider(unittest.TestCase):
    def test_mint_validation(self):
        self.assertEqual(validate_solana_mint(BONK), BONK)
        self.assertEqual(validate_solana_mint("  " + USDC + " "), USDC)
        for bad in ["", "   ", "XYZ", "0" * 44, "O" * 44, "l" * 44,
                    "short", "x" * 64, None, 123, "!" * 44]:
            with self.assertRaises((ValueError, TypeError), msg=f"{bad!r}"):
                validate_solana_mint(bad)  # type: ignore[arg-type]
        self.assertTrue(is_valid_solana_mint(BONK))
        self.assertFalse(is_valid_solana_mint("not-a-mint"))

    def test_token_identity_no_collision(self):
        a = OnChainAsset(chain="solana", mint=BONK, symbol="BONK")
        b = OnChainAsset(chain="solana", mint=USDC, symbol="BONK")
        self.assertNotEqual(a.key(), b.key())
        self.assertEqual(a.symbol, b.symbol)  # same display, different identity

    def test_decimals_handling(self):
        self.assertAlmostEqual(apply_decimals(1_000_000, 6), 1.0)
        self.assertAlmostEqual(apply_decimals(500, 5), 0.005)
        for bad_dec in [None, -1, 19, "x", True, 6.5 if False else "six"]:
            self.assertIsNone(apply_decimals(1000, bad_dec))
        self.assertIsNone(apply_decimals("nan", 6))
        self.assertIsNone(apply_decimals(-5, 6))

    def test_pool_selection_prefers_liquidity(self):
        pools = [bonk_pair("POOL_LOW", "orca", liq=10_000.0),
                 bonk_pair("POOL_HIGH", "raydium", liq=850_000.0)]
        winner, reason = select_pool(pools, BONK)
        self.assertEqual(winner["pairAddress"], "POOL_HIGH")
        self.assertIn("raydium", reason)

    def test_multi_pool_deterministic(self):
        pools = [bonk_pair("POOL_B", "orca", liq=50_000.0, vol24=9.0),
                 bonk_pair("POOL_A", "raydium", liq=50_000.0, vol24=9.0),
                 {"chainId": "ethereum", "dexId": "uniswap",
                  "pairAddress": "ETH_POOL",
                  "baseToken": {"address": BONK}, "quoteToken": {"address": "x"},
                  "liquidity": {"usd": 10**12}, "volume": {"h24": 10**12}}]
        w1, _ = select_pool(pools, BONK)
        w2, _ = select_pool(list(reversed(pools)), BONK)
        self.assertEqual(w1["pairAddress"], w2["pairAddress"])
        self.assertEqual(w1["pairAddress"], "POOL_A")  # tie -> address asc
        with self.assertRaises(NoMarketError):
            select_pool([], BONK)
        with self.assertRaises(NoMarketError):
            select_pool([bonk_pair(chain="ethereum")], BONK)

    def test_price_normalization(self):
        ev = normalize_pair(BONK, bonk_pair())
        self.assertIsInstance(ev, MarketLiquidityEvent)
        self.assertAlmostEqual(ev.price_usd, 0.0000215)
        self.assertEqual(ev.quote_symbol, "SOL")
        self.assertAlmostEqual(ev.liquidity_usd, 850000.0)
        self.assertEqual(ev.buys_5m, 120)
        self.assertEqual(ev.sells_5m, 95)
        self.assertEqual(ev.txns_5m, 215)
        self.assertEqual(ev.dex, "raydium")

    def test_unavailable_metrics_are_none_not_zero(self):
        thin = bonk_pair()
        thin.pop("marketCap", None)
        thin.pop("fdv", None)
        thin["liquidity"] = {}
        thin["volume"] = {}
        thin["txns"] = {}
        ev = normalize_pair(BONK, thin)
        self.assertIsNone(ev.market_cap)
        self.assertIsNone(ev.fdv)
        self.assertIsNone(ev.liquidity_usd)
        self.assertIsNone(ev.volume_5m)
        self.assertIsNone(ev.buys_5m)
        self.assertNotEqual(ev.market_cap, 0)

    def test_malformed_pair_handling(self):
        ev = normalize_pair(BONK, {"chainId": "solana"})
        self.assertIsNone(ev.price_usd)
        self.assertIsNone(ev.pool_address)
        ev2 = normalize_pair(BONK, bonk_pair(price_usd="NaN"))
        self.assertIsNone(ev2.price_usd)

    def test_resolve_and_refresh(self):
        be = FakeBackend([bonk_pair("POOL_LOW", "orca", liq=10_000.0),
                          bonk_pair("POOL_HIGH", "raydium", liq=850_000.0)])
        p = SolanaDexMarketDataProvider(BONK, backend=be, poll_interval_s=60)
        info = run(p.resolve())
        self.assertEqual(info["pool"].pool_address, "POOL_HIGH")
        self.assertEqual(info["pool"].dex, "raydium")
        ev = run(p.refresh_once())
        self.assertIsNotNone(ev)
        self.assertAlmostEqual(ev.price_usd, 0.0000215)
        tick = run(p.get_ticker())
        self.assertIsNotNone(tick)
        self.assertGreater(tick.price, 0)
        # identical re-poll dedups (accepted once)
        ev2 = run(p.refresh_once())
        self.assertIsNone(ev2)
        self.assertGreaterEqual(p._sm.duplicates_dropped, 1)
        h = p.get_health()
        self.assertEqual(h["mint"], BONK)

    def test_no_market_token_error_not_fake_price(self):
        p = SolanaDexMarketDataProvider(BONK, backend=FakeBackend([]))
        with self.assertRaises(NoMarketError):
            run(p.resolve())
        self.assertIsNone(run(p.get_ticker()))  # honest None, never 0
        self.assertEqual(run(p.get_candles()), [])
        self.assertIsNone(run(p.get_order_book()))

    def test_backend_failure_handling(self):
        p = SolanaDexMarketDataProvider(
            BONK, backend=FakeBackend(exc=BackendError("down")))
        self.assertIsNone(run(p.refresh_once()))
        self.assertEqual(p._failures, 1)
        with self.assertRaises(BackendError):
            run(p.resolve())

    def test_stale_detection(self):
        clock = FakeClock()
        p = SolanaDexMarketDataProvider(BONK, backend=FakeBackend([bonk_pair()]),
                                        now_fn=clock)
        run(p.refresh_once())
        self.assertFalse(p.get_health()["stale"])
        clock.advance(3600.0)
        self.assertTrue(p.get_health()["stale"])

    def test_dexscreener_malformed_envelope(self):
        be = DexScreenerBackend(http_get=lambda url, t: (200, "not json"))
        with self.assertRaises(BackendError):
            run(be.fetch_pairs(BONK))
        be2 = DexScreenerBackend(http_get=lambda url, t: (500, "{}"))
        with self.assertRaises(BackendError):
            run(be2.fetch_pairs(BONK))
        be3 = DexScreenerBackend(
            http_get=lambda url, t: (200, json.dumps({"pairs": None})))
        self.assertEqual(run(be3.fetch_pairs(BONK)), [])


class TestAbstractionConsistency(unittest.TestCase):
    def test_both_providers_share_surface(self):
        bp = make_binance_provider()
        sp = SolanaDexMarketDataProvider(BONK, backend=FakeBackend([bonk_pair()]))
        for prov in (bp, sp):
            self.assertIsInstance(prov, LiveMarketDataProvider)
            for m in ("connect", "disconnect", "get_ticker", "get_candles",
                      "get_order_book", "get_health"):
                self.assertTrue(callable(getattr(prov, m)), msg=m)
        bh, sh = bp.get_health(), sp.get_health()
        self.assertEqual(set(bh) & set(sh),
                         set(bh) & set(sh))  # both dicts; shared core keys below
        for key in ("provider", "connected", "stale", "reconnects",
                    "messages_received", "duplicates_dropped",
                    "out_of_order_dropped"):
            self.assertIn(key, bh)
            self.assertIn(key, sh)

    def test_validation_primitives(self):
        self.assertTrue(is_valid_price(1.5))
        self.assertFalse(is_valid_price(0))
        self.assertFalse(is_valid_price(float("nan")))
        self.assertFalse(is_valid_price(float("inf")))
        self.assertTrue(check_ohlc(1, 2, 0.5, 1.5))
        self.assertFalse(check_ohlc(1, 0.5, 0.5, 1))  # impossible high
        self.assertFalse(check_ohlc(0, 0, 0, 0))


class TestBackwardCompatibility(unittest.TestCase):
    def test_old_handler_behaviour_preserved(self):
        md = MarketDataHandler(ConfigManager())
        run(md.connect())
        try:
            ts = datetime.now(timezone.utc)
            t = Ticker(symbol="BTC/USDT", price=50000.0, volume=100.0,
                       timestamp=ts, event_id="e1")
            self.assertTrue(md.ingest_ticker(t))
            self.assertFalse(md.ingest_ticker(t))
            h = md.get_health()
            self.assertIn("connected", h)
        finally:
            run(md.disconnect())

    def test_recording_roundtrip_and_torn_tail(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as f:
            path = f.name
        rec = LiveEventRecorder(path)
        ts = datetime.now(timezone.utc)
        rec.record("binance", "BTC/USDT", "ticker", ts, "e1", {"price": 1.0})
        rec.close()
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"schema": 1, "torn": \n')  # simulated torn kill tail
        rows, skipped = read_records(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(skipped, 1)
        self.assertEqual(rows[0]["schema"], 1)
        os.unlink(path)


if __name__ == "__main__":
    unittest.main()
