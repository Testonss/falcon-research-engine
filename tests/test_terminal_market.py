"""Functional market surface tests: registry, discovery, timeframes, sync.

Deterministic and offline: provider/network seams are faked at the
http_get / factory / fetcher boundaries. No real network required.
"""
import asyncio
import json
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from falcon_terminal import discovery as disc
from falcon_terminal.models import AssetSnapshot, TerminalSnapshot
from falcon_terminal.registry import (
    AssetRegistry,
    CanonicalAsset,
    binance_id,
    parse_canonical_id,
    solana_mint_id,
    solana_pool_id,
)
from falcon_terminal.store import LocalStore
from falcon_terminal.timeframes import (
    TIMEFRAMES,
    TF_SECONDS,
    aggregate_ohlcv,
    bucket_start,
    merge_candle_series,
)
from falcon_terminal.viewmodels import (
    AssetListModel,
    TerminalController,
)
from market_data import Candle, OrderBook, OrderBookEntry, Ticker
from market_data.providers.binance import BinanceLiveMarketDataProvider
from market_data.providers.solana import rank_pools

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB4Y"
WSOL = "So11111111111111111111111111111111111111112"
T0 = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def candle(open_ms, o, h, l, c, v, closed=True):
    from datetime import timedelta
    return Candle(symbol="BTC/USDT", open=o, high=h, low=l, close=c,
                  volume=v,
                  timestamp=datetime.fromtimestamp(open_ms / 1000.0,
                                                   tz=timezone.utc),
                  interval="1m", is_closed=closed)


def kline_row(open_ms, o="100.0", h="101.0", l="99.0", c="100.5", v="10.0"):
    return [open_ms, o, h, l, c, v, open_ms + 59999, "0", 0, "0", "0", "0"]


# --------------------------------------------------------------------------
# Canonical ids
# --------------------------------------------------------------------------

class CanonicalIdTests(unittest.TestCase):
    def test_binance_ids(self):
        self.assertEqual(binance_id("BTCUSDT"), "binance:spot:BTCUSDT")
        self.assertEqual(binance_id("BTC/USDT"), "binance:spot:BTCUSDT")
        with self.assertRaises(ValueError):
            binance_id("")
        with self.assertRaises(ValueError):
            binance_id("   ")

    def test_solana_ids(self):
        self.assertEqual(solana_mint_id(BONK), f"solana:{BONK}")
        self.assertEqual(solana_pool_id(BONK, "raydium", "POOL1"),
                         f"solana:{BONK}:raydium:POOL1")
        with self.assertRaises(ValueError):
            solana_mint_id("")
        with self.assertRaises(ValueError):
            solana_pool_id(BONK, "", "POOL1")
        # Same display symbol, different mint => different identity.
        self.assertNotEqual(solana_mint_id(BONK), solana_mint_id(WSOL))

    def test_parse_ids(self):
        self.assertEqual(parse_canonical_id("binance:spot:BTCUSDT"),
                         {"provider": "binance", "venue": "spot",
                          "ref": "BTCUSDT", "pool": None})
        self.assertEqual(parse_canonical_id(f"solana:{BONK}"),
                         {"provider": "solana", "venue": "",
                          "ref": BONK, "pool": None})
        self.assertEqual(parse_canonical_id(f"solana:{BONK}:raydium:POOL1"),
                         {"provider": "solana", "venue": "raydium",
                          "ref": BONK, "pool": "POOL1"})
        for bad in ("", "binance", "binance:BTCUSDT", "cex:spot:X",
                    "solana", "solana:a:b", "solana:a:b:c:d", None, 42):
            with self.assertRaises(ValueError, msg=f"{bad!r}"):
                parse_canonical_id(bad)

    def test_timeframe_sets_match(self):
        self.assertEqual(tuple(BinanceLiveMarketDataProvider
                               .supported_timeframes(object.__new__(
                                   BinanceLiveMarketDataProvider))),
                         TIMEFRAMES)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def _asset(cid, **kw):
    fields = dict(id=cid, provider="binance", venue="spot", chain="",
                  symbol="BTC/USDT", quote="USDT")
    fields.update(kw)
    return CanonicalAsset(**fields)


class RegistryUpdateTests(unittest.TestCase):
    def setUp(self):
        self.reg = AssetRegistry()
        self.reg.register(_asset("binance:spot:BTCUSDT"))
        self.reg.register(_asset(f"solana:{BONK}", provider="solana",
                                 venue="", chain="solana", symbol="BONK",
                                 mint=BONK, quote="SOL"))

    def test_insert_and_revision_increment(self):
        r0 = self.reg.revision
        ok, changed = self.reg.apply_quote(
            "binance:spot:BTCUSDT", price=67000.0, event_time=T0,
            event_id="e1")
        self.assertTrue(ok and changed)
        self.assertGreater(self.reg.revision, r0)
        st = self.reg.state("binance:spot:BTCUSDT")
        self.assertEqual(st.price, 67000.0)
        self.assertEqual(st.revision, self.reg.revision)

    def test_duplicate_event_rejected(self):
        self.reg.apply_quote("binance:spot:BTCUSDT", price=1.0,
                             event_time=T0, event_id="dup")
        r1 = self.reg.revision
        ok, changed = self.reg.apply_quote(
            "binance:spot:BTCUSDT", price=2.0, event_time=T0, event_id="dup")
        self.assertFalse(ok)
        self.assertFalse(changed)
        self.assertEqual(self.reg.revision, r1)
        self.assertEqual(self.reg.state("binance:spot:BTCUSDT").price, 1.0)

    def test_out_of_order_rejected(self):
        later = datetime(2024, 5, 1, 12, 5, tzinfo=timezone.utc)
        self.reg.apply_quote("binance:spot:BTCUSDT", price=200.0,
                             event_time=later, event_id="n1")
        ok, _ = self.reg.apply_quote(
            "binance:spot:BTCUSDT", price=100.0, event_time=T0, event_id="n2")
        self.assertFalse(ok)
        self.assertEqual(self.reg.state("binance:spot:BTCUSDT").price, 200.0)

    def test_unknown_id_rejected(self):
        ok, changed = self.reg.apply_quote("nope:nope", price=1.0)
        self.assertFalse(ok or changed)

    def test_stale_state_passthrough(self):
        self.assertTrue(self.reg.apply_health(
            f"solana:{BONK}", state="STALE", connected=True, stale=True))
        st = self.reg.state(f"solana:{BONK}")
        self.assertTrue(st.stale)
        self.assertEqual(st.state, "STALE")

    def test_asset_pool_field_separation(self):
        # Pool-level metrics land on pool rows; aggregates on asset rows.
        pool_id = solana_pool_id(BONK, "raydium", "POOL1")
        self.reg.register(CanonicalAsset(
            id=pool_id, provider="solana", venue="raydium", chain="solana",
            symbol="BONK", mint=BONK, quote="SOL", kind="pool",
            parent_id=f"solana:{BONK}"))
        self.reg.apply_liquidity(
            pool_id, liquidity_usd=850000.0, volume_5m=45210.5,
            buys_5m=120, sells_5m=95, dex="raydium", pool_address="POOL1",
            event_time=T0, event_id="p1")
        self.reg.apply_liquidity(
            f"solana:{BONK}", market_cap=1400000000.0, fdv=1500000000.0,
            event_time=T0, event_id="a1")
        pool, asset = self.reg.state(pool_id), self.reg.state(f"solana:{BONK}")
        self.assertEqual(pool.liquidity_usd, 850000.0)
        self.assertIsNone(pool.market_cap)
        self.assertEqual(asset.market_cap, 1400000000.0)
        self.assertIsNone(asset.liquidity_usd)

    def test_changed_since_and_drain(self):
        self.reg.apply_quote("binance:spot:BTCUSDT", price=1.0, event_id="q1")
        self.assertEqual(self.reg.changed_since(0),
                         ["binance:spot:BTCUSDT"])
        drained = self.reg.drain_dirty()
        self.assertEqual(drained, ["binance:spot:BTCUSDT"])
        self.assertEqual(self.reg.drain_dirty(), [])
        # Identical content: accepted, but the content revision does NOT
        # advance (liveness timestamps refresh; no fake movement).
        r = self.reg.revision
        ok, changed = self.reg.apply_quote(
            "binance:spot:BTCUSDT", price=1.0, event_id="q2")
        self.assertTrue(ok)
        self.assertFalse(changed)
        self.assertEqual(self.reg.revision, r)
        self.assertEqual(self.reg.drain_dirty(), [])
        # Real change advances exactly one revision.
        ok, changed = self.reg.apply_quote(
            "binance:spot:BTCUSDT", price=2.0, event_id="q3")
        self.assertTrue(changed)
        self.assertEqual(self.reg.revision, r + 1)
        self.assertEqual(self.reg.drain_dirty(), ["binance:spot:BTCUSDT"])

    def test_search_and_filters(self):
        self.reg.register(_asset("binance:spot:ETHUSDT", symbol="ETH/USDT"))
        self.assertEqual(self.reg.search("btc"), ["binance:spot:BTCUSDT"])
        self.assertEqual(self.reg.search("bonk"), [f"solana:{BONK}"])
        self.assertEqual(self.reg.search(BONK), [f"solana:{BONK}"])
        self.assertEqual(len(self.reg.search("")), 3)
        self.assertEqual(self.reg.filter(provider="binance"),
                         ["binance:spot:BTCUSDT", "binance:spot:ETHUSDT"])
        self.assertEqual(self.reg.filter(chain="solana"), [f"solana:{BONK}"])
        self.assertEqual(self.reg.filter(quote="USDT"),
                         ["binance:spot:BTCUSDT", "binance:spot:ETHUSDT"])
        self.assertEqual(self.reg.filter(provider="binance", quote="USDT",
                                         chain=""), ["binance:spot:BTCUSDT",
                                                     "binance:spot:ETHUSDT"])
        self.reg.set_favorite("binance:spot:ETHUSDT", True)
        self.assertEqual(self.reg.filter(favorites_only=True),
                         ["binance:spot:ETHUSDT"])
        self.assertEqual(self.reg.providers(), ["binance", "solana"])
        self.assertEqual(self.reg.chains(), ["", "solana"])
        self.assertEqual(self.reg.quotes(), ["USDT", "SOL"])


class RegistryScaleTests(unittest.TestCase):
    def test_10000_synthetic_assets(self):
        reg = AssetRegistry()
        n = 10000
        for i in range(n):
            reg.register(_asset(f"binance:spot:SYM{i:05d}",
                                symbol=f"SYM{i:05d}/USDT"))
        self.assertEqual(len(reg), n)
        for i in range(n):
            reg.apply_quote(f"binance:spot:SYM{i:05d}", price=1.0 + i,
                            event_id=f"bulk-{i}")
        self.assertEqual(len(reg.changed_since(0)), n)
        hits = reg.search("SYM00999")
        self.assertEqual(hits, ["binance:spot:SYM00999"])
        self.assertEqual(len(reg.filter(provider="binance")), n)


# --------------------------------------------------------------------------
# Store (favorites / recents / selection persistence)
# --------------------------------------------------------------------------

class StoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="falcon-state-")
        self.path = Path(self.dir) / "terminal.json"

    def store(self):
        return LocalStore(self.path)

    def test_favorites_persist_across_restart(self):
        s = self.store()
        self.assertTrue(s.toggle_favorite("binance:spot:BTCUSDT"))
        self.assertFalse(s.toggle_favorite("binance:spot:BTCUSDT"))
        self.assertTrue(s.toggle_favorite("binance:spot:BTCUSDT"))
        s.toggle_favorite(f"solana:{BONK}")
        s.save()
        s2 = self.store()
        self.assertEqual(s2.favorites,
                         ["binance:spot:BTCUSDT", f"solana:{BONK}"])

    def test_recent_bounded_order_and_persist(self):
        s = self.store()
        for i in range(30):
            s.touch_recent(f"id:{i}")
        self.assertEqual(len(s.recent), 20)
        self.assertEqual(s.recent[0], "id:29")
        s.touch_recent("id:5")
        self.assertEqual(s.recent[0], "id:5")
        self.assertEqual(len(s.recent), 20)
        s.set_selected("id:5")
        s.save()
        s2 = self.store()
        self.assertEqual(s2.recent[0], "id:5")
        self.assertEqual(s2.selected, "id:5")

    def test_corrupt_file_falls_back_empty(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        s = self.store()
        self.assertEqual((s.favorites, s.recent, s.selected), ([], [], ""))
        s.toggle_favorite("x")
        s.save()  # rewrites cleanly
        self.assertEqual(self.store().favorites, ["x"])

    def test_memory_only_store_never_touches_disk(self):
        s = LocalStore(None)
        s.toggle_favorite("a")
        s.touch_recent("a")
        s.set_selected("a")
        s.save()
        self.assertEqual(s.to_dict()["selected"], "a")


# --------------------------------------------------------------------------
# Discovery fixtures (offline)
# --------------------------------------------------------------------------

def _exchange_info_fixture():
    return {"symbols": [
        {"symbol": "BTCUSDT", "status": "TRADING",
         "baseAsset": "BTC", "quoteAsset": "USDT", "isSpotTradingAllowed": True},
        {"symbol": "ETHUSDC", "status": "TRADING",
         "baseAsset": "ETH", "quoteAsset": "USDC", "isSpotTradingAllowed": True},
        {"symbol": "BNBBTC", "status": "TRADING",
         "baseAsset": "BNB", "quoteAsset": "BTC", "isSpotTradingAllowed": True},
        {"symbol": "HALTUSDT", "status": "HALT",
         "baseAsset": "HALT", "quoteAsset": "USDT", "isSpotTradingAllowed": True},
        {"symbol": "PERPUSDT", "status": "TRADING",
         "baseAsset": "PERP", "quoteAsset": "USDT", "isSpotTradingAllowed": False},
        {"symbol": "WEIRDXXX", "status": "TRADING",
         "baseAsset": "WEIRD", "quoteAsset": "XXX", "isSpotTradingAllowed": True},
        {"symbol": "BROKEN", "status": "TRADING"},
        "not-a-dict",
    ]}


def _tickers_fixture():
    return [
        {"symbol": "BTCUSDT", "lastPrice": "67000.50", "volume": "123.4",
         "quoteVolume": "8000000.0", "priceChangePercent": "1.25",
         "openTime": 1714560000000, "closeTime": 1714646400000},
        {"symbol": "ETHUSDC", "lastPrice": "3500.0", "volume": "10",
         "quoteVolume": "35000", "priceChangePercent": "-0.5",
         "openTime": 1714560000000, "closeTime": 1714646400000},
        {"symbol": "BAD", "lastPrice": "NaN", "volume": "1",
         "closeTime": 1714646400000},
        {"nope": True},
    ]


def _fake_http(mapping):
    def _get(url, timeout):
        for key, value in mapping.items():
            if key in url:
                return value
        raise IOError(f"unexpected url {url}")
    return _get


class DiscoveryTests(unittest.TestCase):
    def test_exchange_info_catalog(self):
        assets, stats = disc.parse_exchange_info(_exchange_info_fixture())
        ids = sorted(a.id for a in assets)
        self.assertEqual(ids, ["binance:spot:BNBBTC", "binance:spot:BTCUSDT",
                               "binance:spot:ETHUSDC"])
        by_id = {a.id: a for a in assets}
        self.assertEqual(by_id["binance:spot:BTCUSDT"].symbol, "BTC/USDT")
        self.assertEqual(by_id["binance:spot:BTCUSDT"].quote, "USDT")
        self.assertEqual(stats, {"symbols": 8, "kept": 3, "skipped": 5})
        empty, stats2 = disc.parse_exchange_info({"nope": 1})
        self.assertEqual((empty, stats2["kept"]), ([], 0))

    def test_all_tickers_parse(self):
        out, stats = disc.parse_all_tickers(_tickers_fixture())
        self.assertEqual(stats, {"rows": 4, "kept": 2, "skipped": 2})
        self.assertAlmostEqual(out["BTCUSDT"]["price"], 67000.50)
        self.assertAlmostEqual(out["BTCUSDT"]["change"], 1.25)
        self.assertAlmostEqual(out["ETHUSDC"]["change"], -0.5)
        out2, _ = disc.parse_all_tickers({"nope": 1})
        self.assertEqual(out2, {})

    def test_fetchers_use_injected_http(self):
        http = _fake_http({
            "exchangeInfo": (200, json.dumps(_exchange_info_fixture())),
            "ticker/24hr": (200, json.dumps(_tickers_fixture())),
            "klines": (200, json.dumps([kline_row(1714560000000),
                                        kline_row(1714560060000)])),
        })
        assets, _ = run(disc.fetch_spot_catalog(http))
        self.assertEqual(len(assets), 3)
        tickers, _ = run(disc.fetch_all_tickers(http))
        self.assertIn("BTCUSDT", tickers)
        candles, rejected = run(disc.fetch_klines("BTCUSDT", "5m", http_get=http))
        self.assertEqual(len(candles), 2)
        self.assertEqual(rejected, 0)
        with self.assertRaises(ValueError):
            run(disc.fetch_klines("BTCUSDT", "9m", http_get=http))

    def test_native_interval_mapping(self):
        # Binance daily is "1d" (case-sensitive); terminal label stays "1D".
        self.assertEqual(disc.to_native_interval("1D"), "1d")
        self.assertEqual(disc.to_native_interval("1h"), "1h")
        with self.assertRaises(ValueError):
            disc.to_native_interval("9m")
        seen = {}

        def _capture(url, timeout):
            seen["url"] = url
            return 200, json.dumps([kline_row(1714521600000)])

        run(disc.fetch_klines("BTCUSDT", "1D", http_get=_capture))
        self.assertIn("interval=1d", seen["url"])
        self.assertNotIn("interval=1D", seen["url"])

    def test_fetcher_transport_failure_bounded(self):
        def _boom(url, timeout):
            return (500, "{}")
        with self.assertRaises(IOError):
            run(disc.fetch_spot_catalog(_boom))

    def test_mark_forming_split(self):
        now_ms = 1714560060000 + 30000  # 30s into the second bar: still forming
        c1 = candle(1714560000000, 100, 101, 99, 100.5, 10)
        c1.close_time = datetime.fromtimestamp(1714560059.999, tz=timezone.utc)
        c2 = candle(1714560060000, 100.5, 102, 100, 101.5, 12)
        c2.close_time = datetime.fromtimestamp(1714560119.999, tz=timezone.utc)
        closed, forming = disc.mark_forming([c1, c2], now_ms=now_ms)
        self.assertEqual(len(closed), 1)
        self.assertTrue(all(c.is_closed for c in closed))
        self.assertIsNotNone(forming)
        self.assertFalse(forming.is_closed)

    def test_rank_pools_deterministic(self):
        def pair(addr, dex, liq, vol=9.0, chain="solana", base=BONK):
            return {"chainId": chain, "dexId": dex, "pairAddress": addr,
                    "baseToken": {"address": base, "symbol": "BONK"},
                    "quoteToken": {"address": WSOL, "symbol": "SOL"},
                    "liquidity": {"usd": liq}, "volume": {"h24": vol}}
        pairs = [pair("LOW", "orca", 10_000.0), pair("HIGH", "raydium", 850_000.0),
                 pair("MID", "orca", 50_000.0)]
        ranked = rank_pools(pairs, BONK)
        self.assertEqual([p["pairAddress"] for p in ranked],
                         ["HIGH", "MID", "LOW"])
        self.assertEqual([p["pairAddress"] for p in rank_pools(
            list(reversed(pairs)), BONK)], ["HIGH", "MID", "LOW"])
        self.assertEqual(len(rank_pools(pairs, BONK, limit=2)), 2)
        self.assertEqual(rank_pools([], BONK), [])


# --------------------------------------------------------------------------
# Timeframe math
# --------------------------------------------------------------------------

def _m1(open_ms, seed=0):
    return {"open_ms": open_ms, "o": 100.0 + seed, "h": 101.0 + seed,
            "l": 99.0 + seed, "c": 100.5 + seed, "v": 10.0, "closed": True}


class BucketTests(unittest.TestCase):
    def test_bucket_alignment(self):
        self.assertEqual(bucket_start(1714560000000, "1m"), 1714560000000)
        self.assertEqual(bucket_start(1714560000000 + 61000, "1m"),
                         1714560060000)
        # Exact boundary belongs to the new bucket.
        self.assertEqual(bucket_start(1714560300000, "5m"), 1714560300000)
        # A timestamp inside a bucket floors to that bucket's open.
        self.assertEqual(bucket_start(1714560060000 + 60000, "5m"),
                         1714560000000)
        # Day boundary (UTC).
        day = 1714521600000  # 2024-05-01 00:00 UTC
        self.assertEqual(bucket_start(day + 3600000, "1D"), day)
        self.assertEqual(bucket_start(day - 1, "1D"), day - 86400000)
        with self.assertRaises(ValueError):
            bucket_start(1000, "9m")
        with self.assertRaises(ValueError):
            bucket_start("xx", "1m")

    def test_tf_seconds(self):
        self.assertEqual(TF_SECONDS, {"1m": 60, "5m": 300, "15m": 900,
                                      "1h": 3600, "4h": 14400, "1D": 86400})


class AggregationTests(unittest.TestCase):
    def _bars(self, n, start_ms=1714560000000):
        return [_m1(start_ms + i * 60000, seed=i * 0.1) for i in range(n)]

    def test_1m_passthrough(self):
        bars = self._bars(3)
        closed, forming = aggregate_ohlcv(bars, "1m", now_ms=9999999999999)
        self.assertEqual(len(closed), 3)
        self.assertIsNone(forming)
        self.assertTrue(all(b["closed"] for b in closed))

    def test_5m_aggregation(self):
        bars = self._bars(10)
        closed, forming = aggregate_ohlcv(bars, "5m", now_ms=9999999999999)
        self.assertEqual(len(closed), 2)
        first, second = closed
        self.assertEqual(first["open_ms"], 1714560000000)
        self.assertAlmostEqual(first["o"], 100.0)
        self.assertAlmostEqual(first["c"], 100.5 + 0.4)
        self.assertAlmostEqual(first["h"], 101.0 + 0.4)
        self.assertAlmostEqual(first["l"], 99.0)
        self.assertAlmostEqual(first["v"], 50.0)
        self.assertEqual(second["open_ms"], 1714560000000 + 300000)

    def test_15m_1h_4h_1d_group_counts(self):
        bars = self._bars(288, start_ms=1714521600000)  # UTC midnight start
        for tf, expect in (("15m", 20), ("1h", 5), ("4h", 2), ("1D", 1)):
            closed, _ = aggregate_ohlcv(bars, tf, now_ms=9999999999999)
            self.assertEqual(len(closed), expect, tf)

    def test_forming_excluded_from_closed(self):
        bars = self._bars(10)
        bars[-1] = dict(bars[-1], closed=False)
        closed, forming = aggregate_ohlcv(bars, "5m", now_ms=9999999999999)
        # Last bucket holds the forming bar -> flagged forming, not closed.
        self.assertEqual(len(closed), 1)
        self.assertIsNotNone(forming)
        self.assertFalse(forming["closed"])
        self.assertEqual(forming["open_ms"], 1714560000000 + 300000)

    def test_open_bucket_stays_forming(self):
        bars = self._bars(6)
        now_ms = 1714560000000 + 5 * 60000 + 1000  # inside 6th minute
        closed, forming = aggregate_ohlcv(bars, "5m", now_ms=now_ms)
        self.assertEqual(len(closed), 1)
        self.assertIsNotNone(forming)

    def test_malformed_bars_skipped(self):
        bars = self._bars(5) + ["junk", {"o": 1}, {"open_ms": 1, "o": "x",
                                                   "h": 1, "l": 1, "c": 1,
                                                   "v": 1},
                                {"open_ms": 2, "o": 5, "h": 4, "l": 4,
                                 "c": 5, "v": 1}]  # impossible OHLC
        closed, _ = aggregate_ohlcv(bars, "1m", now_ms=9999999999999)
        self.assertEqual(len(closed), 5)

    def test_closed_immutability_across_prefixes(self):
        full = self._bars(30)
        closed_full, _ = aggregate_ohlcv(full, "5m", now_ms=9999999999999)
        closed_part, _ = aggregate_ohlcv(full[:20], "5m", now_ms=9999999999999)
        self.assertEqual(closed_full[:4], closed_part)

    def test_merge_keeps_cached_closed(self):
        cached = self._bars(10)
        fresh = [dict(b, c=b["c"] + 999.0) for b in self._bars(10)]  # conflict
        fresh.append(_m1(1714560000000 + 10 * 60000, seed=5.0))
        merged, forming = merge_candle_series(cached, fresh)
        self.assertEqual(len(merged), 11)
        # Cached values win conflicts; the genuinely new bar appends.
        self.assertAlmostEqual(merged[0]["c"], 100.5)
        self.assertAlmostEqual(merged[-1]["c"], 105.5)
        self.assertIsNone(forming)

    def test_merge_forming_replace(self):
        cached = self._bars(5)
        f1 = dict(_m1(1714560000000 + 5 * 60000), closed=False)
        f2 = dict(_m1(1714560000000 + 5 * 60000, seed=9), closed=False)
        _, forming = merge_candle_series(cached, [f1])
        self.assertAlmostEqual(forming["c"], 100.5)
        _, forming2 = merge_candle_series(cached, [f2])
        self.assertAlmostEqual(forming2["c"], 109.5)


# --------------------------------------------------------------------------
# Bridge universe + history service (offline fakes)
# --------------------------------------------------------------------------

class FakeTickerProvider:
    provider_name = "test-feed"

    def __init__(self, symbol="BTC/USDT", price=None, change=None):
        self.symbol = symbol
        self._price = price
        self._change = change
        self.connect_calls = 0
        self.disconnect_calls = 0

    def supported_timeframes(self):
        return ()

    async def connect(self):
        self.connect_calls += 1

    async def disconnect(self):
        self.disconnect_calls += 1

    async def get_ticker(self, symbol=""):
        if self._price is None:
            return None
        return Ticker(self.symbol, self._price, 10.0, T0,
                      event_id=f"fake:{self.symbol}:{self._price}",
                      change_pct_24h=self._change)

    async def get_order_book(self, symbol="", depth=10):
        return None

    def get_liquidity_event(self):
        return None

    def get_health(self):
        return {"state": "CONNECTED", "connected": True, "stale": False,
                "messages_received": 1, "last_message_at": T0.isoformat(),
                "seconds_since_message": 0.0, "symbol": self.symbol}


def _catalog_fixture():
    from falcon_terminal.registry import CanonicalAsset
    from falcon_terminal.timeframes import TIMEFRAMES
    return ([CanonicalAsset(id="binance:spot:BTCUSDT", provider="binance",
                            venue="spot", chain="", symbol="BTC/USDT",
                            quote="USDT", supported=TIMEFRAMES),
             CanonicalAsset(id="binance:spot:ETHUSDT", provider="binance",
                            venue="spot", chain="", symbol="ETH/USDT",
                            quote="USDT", supported=TIMEFRAMES)],
            {"symbols": 2, "kept": 2, "skipped": 0})


def _universe_tickers(prices=None):
    prices = prices or {"BTCUSDT": 67000.0, "ETHUSDT": 3500.0}
    return ({sym: {"price": px, "change": 1.0, "quote_volume": 1000.0,
                   "event_time": T0}
             for sym, px in prices.items()},
            {"rows": len(prices), "kept": len(prices), "skipped": 0})


async def _history_fixture(symbol, tf, limit=200):
    _tf_check = {"1m": 0, "5m": 0, "15m": 0, "1h": 0, "4h": 0, "1D": 0}[tf]
    start = 1714560000000
    out = []
    for i in range(10):
        c = candle(start + i * 60000, 100 + i, 101 + i, 99 + i, 100.5 + i, 10)
        c.close_time = datetime.fromtimestamp(
            (start + i * 60000 + 59999) / 1000.0, tz=timezone.utc)
        out.append(c)
    return out, 0


async def _catalog_fixture_async():
    return _catalog_fixture()


async def _universe_tickers_async(prices=None):
    return _universe_tickers(prices)


class BridgeUniverseTests(unittest.TestCase):
    def setUp(self):
        from falcon_terminal.bridge import TerminalBridge
        self.bridges = []
        self._Bridge = TerminalBridge

    def tearDown(self):
        for b in self.bridges:
            b.stop()
        for b in self.bridges:
            self.assertTrue(b.join(5.0), "bridge worker did not stop")

    def make_bridge(self, **kw):
        kw.setdefault("refresh_interval", 0.05)
        kw.setdefault("universe_poll_s", 60.0)
        kw.setdefault("catalog_fetcher", _catalog_fixture_async)
        kw.setdefault("tickers_fetcher", _universe_tickers_async)
        kw.setdefault("history_fetcher", _history_fixture)
        b = self._Bridge(symbol="BTCUSDT", universe=True,
                         binance_provider_factory=lambda s: FakeTickerProvider(
                             "BTC/USDT" if s == "BTCUSDT" else s + "/?",
                             price=67000.0, change=1.0),
                         solana_provider_factory=lambda m: FakeTickerProvider(
                             m[:8], price=None),
                         **kw)
        self.bridges.append(b)
        return b

    def wait_row(self, bridge, key, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            snap = bridge.poll()
            if snap is not None:
                latest = snap
                row = next((a for a in snap.assets if a.key == key), None)
                if row is not None and predicate(row):
                    return snap, row
            threading.Event().wait(0.01)
        self.fail(f"no matching row {key}; latest={latest!r}")

    def test_catalog_populates_registry_rows(self):
        b = self.make_bridge()
        b.start()
        snap, _ = self.wait_row(b, "binance:spot:BTCUSDT",
                                lambda r: r.price == 67000.0)
        keys = [a.key for a in snap.assets]
        self.assertIn("binance:spot:ETHUSDT", keys)
        self.assertEqual(snap.assets[0].symbol, "BTC/USDT")
        # ASSETS counter reflects the registry, not a hardcoded pair.
        self.assertGreaterEqual(len(keys), 2)

    def test_tickers_update_all_rows_no_unknown_growth(self):
        b = self.make_bridge()
        b.start()
        self.wait_row(b, "binance:spot:ETHUSDT", lambda r: r.price == 3500.0)
        snap, row = self.wait_row(b, "binance:spot:BTCUSDT",
                                  lambda r: r.change_pct_24h == 1.0)
        self.assertEqual(len(snap.assets), len(b.registry.ids()))

    def test_timeframe_switch_fetches_and_caches(self):
        calls = []

        async def counting_hist(symbol, tf, limit=200):
            calls.append((symbol, tf))
            return await _history_fixture(symbol, tf, limit)

        b = self.make_bridge(history_fetcher=counting_hist)
        b.start()
        self.wait_row(b, "binance:spot:BTCUSDT", lambda r: r.price == 67000.0)
        b.set_selection("binance:spot:BTCUSDT")
        b.set_timeframe("1h")
        _, row = self.wait_row(b, "binance:spot:BTCUSDT",
                               lambda r: len(r.chart_candles) == 10
                               and r.active_timeframe == "1h")
        self.assertFalse(row.chart_loading)
        one_h_calls = len([c for c in calls if c[1] == "1h"])
        self.assertGreater(one_h_calls, 0)
        # Switch away and back: cache reuses, no refetch of 1h.
        b.set_timeframe("5m")
        self.wait_row(b, "binance:spot:BTCUSDT",
                      lambda r: r.active_timeframe == "5m"
                      and len(r.chart_candles) > 0)
        b.set_timeframe("1h")
        self.wait_row(b, "binance:spot:BTCUSDT",
                      lambda r: r.active_timeframe == "1h"
                      and len(r.chart_candles) == 10)
        self.assertEqual(len([c for c in calls if c[1] == "1h"]), one_h_calls)
        # Chart scoped to selection only.
        snap, _ = self.wait_row(b, "binance:spot:ETHUSDT", lambda r: True)
        eth = next(a for a in snap.assets if a.key == "binance:spot:ETHUSDT")
        self.assertEqual(eth.chart_candles, ())

    def test_solana_timeframe_unsupported_honestly(self):
        from falcon_terminal.registry import CanonicalAsset
        b = self.make_bridge()
        b.start()
        self.wait_row(b, "binance:spot:BTCUSDT", lambda r: r.price == 67000.0)
        # Track a second mint id directly (no network): registry-level check.
        b.registry.register(CanonicalAsset(
            id=f"solana:{BONK}", provider="solana", venue="", chain="solana",
            symbol="BONK", mint=BONK))
        b.set_selection(f"solana:{BONK}")
        b.set_timeframe("1h")
        _, row = self.wait_row(b, f"solana:{BONK}",
                               lambda r: r.chart_note != "")
        self.assertIn("not supported", row.chart_note)
        self.assertFalse(row.chart_loading)
        self.assertEqual(row.chart_candles, ())

    def test_selection_scoping_and_loading_flag(self):
        b = self.make_bridge()
        b.start()
        self.wait_row(b, "binance:spot:BTCUSDT", lambda r: r.price == 67000.0)
        b.set_selection("binance:spot:ETHUSDT")
        b.set_timeframe("15m")
        _, row = self.wait_row(b, "binance:spot:ETHUSDT",
                               lambda r: len(r.chart_candles) > 0)
        self.assertEqual(row.active_timeframe, "15m")


# --------------------------------------------------------------------------
# Controller market flows (offline FakeBridge with backend seams)
# --------------------------------------------------------------------------

class SeamFakeBridge:
    """Fake bridge honoring the extended backend protocol."""

    def __init__(self, *args, **kwargs):
        self.start_calls = 0
        self._alive = False
        self._queue = []
        self.selections = []
        self.timeframes = []
        self.fav_toggles = []
        self.added_mints = []
        self._favorites = set()

    def start(self):
        self.start_calls += 1
        self._alive = True

    def queue(self, snap):
        self._queue.append(snap)

    def poll(self):
        if self._queue:
            return self._queue.pop(0)
        return None

    def stop(self):
        self._alive = False

    def join(self, timeout=None):
        return True

    @property
    def is_alive(self):
        return self._alive

    # extended seams
    def set_selection(self, cid):
        self.selections.append(cid)

    def set_timeframe(self, tf):
        self.timeframes.append(tf)

    def set_favorites(self, ids):
        self._favorites = set(ids)

    def toggle_favorite(self, cid):
        self.fav_toggles.append(cid)
        return True

    def add_mint(self, mint):
        self.added_mints.append(mint)
        return ""


def _row(key="binance:spot:BTCUSDT", symbol="BTC/USDT", provider="binance",
         price=81204.21, **kw):
    from falcon_terminal.models import AssetSnapshot
    fields = dict(key=key, symbol=symbol, provider=provider, quote="USDT",
                  price=price, connected=True, stale=False, messages=5,
                  state="CONNECTED", revision=3)
    fields.update(kw)
    return AssetSnapshot(**fields)


def _snap(*rows, changed=None):
    return TerminalSnapshot(sequence=1, generated_at=T0.isoformat(),
                            assets=tuple(rows),
                            changed=tuple(changed) if changed else ())


class ControllerMarketTests(unittest.TestCase):
    def make(self, store_path=None, **kw):
        import tempfile
        from falcon_terminal.viewmodels import TerminalController
        if store_path == "tmp":
            tmp = tempfile.mkdtemp(prefix="falcon-ctrl-")
            self._tmp = tmp
            store_path = str(Path(tmp) / "terminal.json")
        made = []

        def factory(**kwargs):
            bridge = SeamFakeBridge(**kwargs)
            made.append(bridge)
            return bridge

        ctrl = TerminalController(bridge_factory=factory, store_path=store_path,
                                  **kw)
        return ctrl, made

    def test_select_asset_syncs_backend_recent_and_persist(self):
        ctrl, made = self.make(store_path="tmp")
        ctrl.start()
        made[0].queue(_snap(_row(price=1.0), _row("binance:spot:ETHUSDT",
                                                 "ETH/USDT", price=2.0)))
        ctrl._poll()
        self.assertEqual(ctrl.selected.key, "binance:spot:BTCUSDT")
        self.assertEqual(made[0].selections, ["binance:spot:BTCUSDT"])
        ctrl.selectAsset("binance:spot:ETHUSDT")
        made[0].queue(_snap(_row(price=1.0), _row("binance:spot:ETHUSDT",
                                                 "ETH/USDT", price=2.0)))
        ctrl._poll()
        self.assertEqual(ctrl.selected.price, "2.0000")
        self.assertIn("binance:spot:ETHUSDT", made[0].selections)
        # persisted + recent
        from falcon_terminal.store import LocalStore
        s2 = LocalStore(Path(self._tmp) / "terminal.json")
        self.assertEqual(s2.selected, "binance:spot:ETHUSDT")
        self.assertEqual(s2.recent[0], "binance:spot:ETHUSDT")

    def test_selection_restored_from_store(self):
        import tempfile
        from falcon_terminal.store import LocalStore
        from falcon_terminal.viewmodels import TerminalController
        tmp = tempfile.mkdtemp(prefix="falcon-restore-")
        path = str(Path(tmp) / "terminal.json")
        s = LocalStore(path)
        s.set_selected("binance:spot:ETHUSDT")
        s.save()
        made = []

        def factory(**kwargs):
            b = SeamFakeBridge(**kwargs)
            made.append(b)
            return b

        ctrl = TerminalController(bridge_factory=factory, store_path=path)
        ctrl.start()
        made[0].queue(_snap(_row(price=1.0), _row("binance:spot:ETHUSDT",
                                                 "ETH/USDT", price=2.0)))
        ctrl._poll()
        self.assertEqual(ctrl.selected.key, "binance:spot:ETHUSDT")

    def test_same_revision_selection_synchronization(self):
        ctrl, made = self.make()
        ctrl.start()
        made[0].queue(_snap(_row(price=99.5, revision=42)))
        ctrl._poll()
        table = ctrl.assets.row("binance:spot:BTCUSDT")
        self.assertIsNotNone(table)
        # Same snapshot revision -> identical price on row and detail.
        self.assertEqual(table["revision"], 42)
        self.assertEqual(ctrl.selected.revision, 42)
        self.assertEqual(table["priceText"], ctrl.selected.price)

    def test_vanished_selection_keeps_old_no_leakage(self):
        ctrl, made = self.make()
        ctrl.start()
        made[0].queue(_snap(_row(price=100.0)))
        ctrl._poll()
        self.assertEqual(ctrl.selected.key, "binance:spot:BTCUSDT")
        # Selected id disappears from the feed: keep old state, no jump.
        other = _row("binance:spot:ETHUSDT", "ETH/USDT", price=5.0)
        made[0].queue(_snap(other))
        ctrl._poll()
        self.assertEqual(ctrl.selected.key, "binance:spot:BTCUSDT")
        self.assertEqual(ctrl.selected.price, "100.00")

    def test_select_clears_stale_fields_immediately(self):
        # Switching identity must never show SOL price + BTC bid, not even
        # for one frame: the detail resets to unknown until the new asset's
        # own state arrives.
        ctrl, made = self.make()
        ctrl.start()
        made[0].queue(_snap(_row(price=100.0)))
        ctrl._poll()
        self.assertEqual(ctrl.selected.price, "100.00")
        ctrl.selectAsset("binance:spot:ETHUSDT")
        self.assertEqual(ctrl.selected.key, "binance:spot:ETHUSDT")
        self.assertEqual(ctrl.selected.price, "—")
        self.assertEqual(ctrl.selected.bid, "—")
        self.assertEqual(ctrl.selected.pool, "—")

    def test_set_timeframe_validates_and_delegates(self):
        ctrl, made = self.make()
        ctrl.start()
        made[0].queue(_snap(_row()))
        ctrl._poll()
        ctrl.setTimeframe("1h")
        self.assertIn("1h", made[0].timeframes)
        before = list(made[0].timeframes)
        ctrl.setTimeframe("9m")  # invalid: ignored
        self.assertEqual(made[0].timeframes, before)

    def test_toggle_favorite_persists(self):
        ctrl, made = self.make(store_path="tmp")
        ctrl.start()
        self.assertTrue(ctrl.toggleFavorite("binance:spot:BTCUSDT"))
        self.assertIn("binance:spot:BTCUSDT", made[0].fav_toggles)
        from falcon_terminal.store import LocalStore
        self.assertIn("binance:spot:BTCUSDT",
                      LocalStore(Path(self._tmp) / "terminal.json").favorites)
        self.assertEqual(ctrl.favoritesCount, 1)
        self.assertFalse(ctrl.toggleFavorite("binance:spot:BTCUSDT"))
        self.assertEqual(ctrl.favoritesCount, 0)

    def test_watch_is_local_only(self):
        ctrl, made = self.make(store_path="tmp")
        ctrl.start()
        ctrl.toggleWatch("binance:spot:BTCUSDT")
        self.assertEqual(ctrl.watchedKeys, ["binance:spot:BTCUSDT"])
        ctrl.toggleWatch("binance:spot:BTCUSDT")
        self.assertEqual(ctrl.watchedKeys, [])
        # Nothing persisted to disk for watch flags.
        from falcon_terminal.store import LocalStore
        s2 = LocalStore(Path(self._tmp) / "terminal.json")
        self.assertEqual(s2.to_dict()["favorites"], [])

    def test_add_mint_delegates_and_reports_errors(self):
        ctrl, made = self.make()
        ctrl.start()
        self.assertEqual(ctrl.addMint(BONK), "")
        self.assertEqual(made[0].added_mints, [BONK])

    def test_metadata_lists_derive_from_registry(self):
        ctrl, made = self.make()
        ctrl.start()
        made[0].queue(_snap(
            _row(),
            _row("binance:spot:ETHUSDT", "ETH/USDT", price=2.0),
            _row(f"solana:{BONK}", "BONK", "solana", price=0.5,
                 quote="SOL", chain="solana", **{"dex": "raydium"})))
        ctrl._poll()
        self.assertEqual(ctrl.assetProviders, ["binance", "solana"])
        self.assertIn("solana", ctrl.assetChains)
        self.assertIn("USDT", ctrl.assetQuotes)
        self.assertIn("raydium", ctrl.assetDexes)


class DeltaModelTests(unittest.TestCase):
    def _model(self):
        from falcon_terminal.viewmodels import AssetListModel
        return AssetListModel()

    def _assets(self, prices):
        return [_row(f"id:{i}", f"S{i}", "binance", price=p) for i, p in
                prices.items()]

    def test_delta_touches_only_listed_rows(self):
        model = self._model()
        base = {0: 10.0, 1: 20.0, 2: 30.0}
        model.sync(self._assets(base))
        changed = dict(base)
        changed[1] = 21.0
        touched = model.sync(self._assets(changed), changed=["id:1"])
        self.assertEqual(sorted(touched), [1])
        self.assertEqual(model.row("id:1")["price"], 21.0)
        self.assertEqual(model.row("id:0")["price"], 10.0)

    def test_delta_still_reconciles_inserts_and_removals(self):
        model = self._model()
        model.sync(self._assets({0: 1.0, 1: 2.0}))
        touched = model.sync(self._assets({1: 2.0, 2: 3.0}), changed=["id:2"])
        self.assertEqual(model.rowCount(), 2)
        self.assertIsNone(model.row("id:0"))
        self.assertEqual(model.row("id:2")["price"], 3.0)
        self.assertIn(1, touched)

    def test_multi_removal_index_correctness(self):
        model = self._model()
        model.sync(self._assets({0: 1.0, 1: 2.0, 2: 3.0, 3: 4.0}))
        model.sync(self._assets({1: 2.0}), changed=[])
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.row("id:1")["price"], 2.0)


class ResetContractTests(unittest.TestCase):
    def test_meme_reset_restores_both_selectors(self):
        import re
        src = (Path(__file__).resolve().parents[1] / "falcon_terminal" / "qml"
               / "pages" / "MemeRadarPage.qml").read_text(encoding="utf-8")
        m = re.search(r"onClicked:\s*\{([^}]*)\}", src)
        self.assertIsNotNone(m)
        # Find the Reset control body (gold-bordered Reset text nearby).
        idx = src.find('text: "Reset"')
        self.assertGreater(idx, 0)
        window = src[max(0, idx - 600):idx + 600]
        self.assertIn("venueBox.currentIndex = 0", window)
        self.assertIn("chainBox.currentIndex = 0", window)


if __name__ == "__main__":
    unittest.main()
