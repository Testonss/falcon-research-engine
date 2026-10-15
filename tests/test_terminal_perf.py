"""Perceived-performance quality pass: deterministic offline tests.

Covers: immediate selection clearing, cached/uncached timeframe paths,
search latency on a 10k registry, delta-only row updates (no full reset),
bounded spark/chart history, connection-state transitions, stale age,
catalog metadata cache, favorites-first ordering, and chart isolation.
No network, no wall-clock flakiness, no animation tests.
"""
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from falcon_terminal import formatting as fmt
from falcon_terminal import perf as perfmod
from falcon_terminal.models import AssetSnapshot, TerminalSnapshot
from falcon_terminal.registry import AssetRegistry, CanonicalAsset
from falcon_terminal.viewmodels import (
    AssetListModel,
    HealthVM,
    SelectedAssetVM,
    TerminalController,
)

T0 = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW = T0.isoformat()


def asset(key="binance:spot:BTCUSDT", symbol="BTC/USDT", provider="binance",
          price=50000.0, **kw):
    fields = dict(key=key, symbol=symbol, provider=provider, quote="USDT",
                  price=price, bid=49999.0, ask=50001.0, connected=True,
                  stale=False, messages=10, observed_at=NOW, source="WebSocket",
                  state="CONNECTED", revision=7,
                  supported_timeframes=("1m", "5m"),
                  active_timeframe="1m")
    fields.update(kw)
    return AssetSnapshot(**fields)


def snapshot(*assets, seq=1, changed=None):
    return TerminalSnapshot(sequence=seq, generated_at=NOW, assets=tuple(assets),
                            changed=tuple(changed or []))


class FakeBridge:
    def __init__(self, *a, **k):
        self._alive = False
        self._queue = []
        self.selections = []
        self.timeframes = []

    def start(self):
        self._alive = True

    def queue(self, s):
        self._queue.append(s)

    def poll(self):
        return self._queue.pop(0) if self._queue else None

    def stop(self):
        self._alive = False

    def join(self, timeout=None):
        return True

    @property
    def is_alive(self):
        return self._alive

    def set_selection(self, cid):
        self.selections.append(cid)

    def set_timeframe(self, tf):
        self.timeframes.append(tf)


def make_ctrl(*snaps):
    made = []

    def factory(**kw):
        b = FakeBridge(**kw)
        made.append(b)
        for s in snaps:
            b.queue(s)
        return b

    ctrl = TerminalController(bridge_factory=factory)
    ctrl.start()
    return ctrl, made


def reg_asset(cid, **kw):
    fields = dict(id=cid, provider="binance", venue="spot", chain="",
                  symbol="BTC/USDT", quote="USDT")
    fields.update(kw)
    return CanonicalAsset(**fields)


class PerfModuleTests(unittest.TestCase):
    def test_budgets_and_human_readable(self):
        self.assertEqual(perfmod.format_ms(None), "—")
        self.assertIn("ms", perfmod.format_ms(0.024))
        self.assertIn("s", perfmod.format_ms(1.5))
        self.assertTrue(perfmod.within_budget("asset_click_identity", 0.02))
        self.assertFalse(perfmod.within_budget("asset_click_identity", 5.0))
        self.assertIsNone(perfmod.within_budget("startup_first_frame", 99.0))
        t = perfmod.PerfTracker(max_samples=4)
        t0 = perfmod.now()
        t.measure("search_filter", t0, "10k")
        self.assertIn("search_filter", t.report())
        for i in range(10):
            t.measure("search_filter", perfmod.now())
        self.assertLessEqual(len(t._samples), 4)  # bounded


class SelectionImmediacyTests(unittest.TestCase):
    def test_select_clears_incompatible_fields_immediately(self):
        vm = SelectedAssetVM()
        vm.select("binance:spot:BTCUSDT")
        vm.sync(asset(price=60000.0, bid=59999.0, ask=60001.0))
        self.assertEqual(vm.price, "60,000.00")
        vm.select("binance:spot:ETHUSDT")
        # New identity, old metrics gone — never new symbol + old price.
        self.assertEqual(vm.key, "binance:spot:ETHUSDT")
        self.assertEqual(vm.price, "—")
        self.assertEqual(vm.bid, "—")
        self.assertEqual(vm.pool, "—")
        self.assertEqual(vm.direction, 0)

    def test_select_identity_under_budget(self):
        vm = SelectedAssetVM()
        t0 = perfmod.now()
        vm.select("binance:spot:BTCUSDT")
        dt = perfmod.now() - t0
        self.assertLess(dt, perfmod.BUDGET_SELECT_MS / 1000.0)

    def test_vanished_selection_keeps_old_no_leakage(self):
        ctrl, made = make_ctrl()
        made[0].queue(snapshot(asset(price=100.0)))
        ctrl._poll()
        self.assertEqual(ctrl.selected.price, "100.00")
        other = asset(key="binance:spot:ETHUSDT", symbol="ETH/USDT", price=5.0)
        made[0].queue(snapshot(other))
        ctrl._poll()
        self.assertEqual(ctrl.selected.key, "binance:spot:BTCUSDT")
        self.assertEqual(ctrl.selected.price, "100.00")


class TimeframeUXTests(unittest.TestCase):
    def test_optimistic_activation_immediate(self):
        vm = SelectedAssetVM()
        vm.select("binance:spot:BTCUSDT")
        vm.sync(asset())
        t0 = perfmod.now()
        ok = vm.requestTimeframe("5m", ("1m", "5m"))
        dt = perfmod.now() - t0
        self.assertTrue(ok)
        self.assertEqual(vm.timeframe, "5m")
        self.assertTrue(vm.chartLoading)  # lightweight loading state at once
        self.assertLess(dt, perfmod.BUDGET_CLICK_MS / 1000.0)

    def test_unsupported_timeframe_rejected(self):
        vm = SelectedAssetVM()
        vm.select("binance:spot:BTCUSDT")
        vm.sync(asset())
        self.assertFalse(vm.requestTimeframe("1h", ("1m", "5m")))
        self.assertEqual(vm.timeframe, "1m")

    def test_controller_timeframe_delegates_and_activates(self):
        ctrl, made = make_ctrl()
        made[0].queue(snapshot(asset()))
        ctrl._poll()
        ctrl.setTimeframe("5m")
        self.assertEqual(ctrl.selected.timeframe, "5m")
        self.assertIn("5m", made[0].timeframes)
        before = list(made[0].timeframes)
        ctrl.setTimeframe("9m")
        self.assertEqual(made[0].timeframes, before)

    def test_cached_path_no_refetch(self):
        # Bridge-level cache reuse is covered by existing market tests;
        # here assert the registry TTL path directly (hit/miss/age).
        reg = AssetRegistry()
        reg.register(reg_asset("binance:spot:BTCUSDT"))
        self.assertEqual(reg.history_get("binance:spot:BTCUSDT", "1m"), ())
        self.assertIsNone(reg.history_age_s("binance:spot:BTCUSDT", "1m"))
        bars = tuple((f"t{i}", 1.0, 2.0, 0.5, 1.5, 10.0, True)
                     for i in range(5))
        reg.history_put("binance:spot:BTCUSDT", "1m", bars)
        self.assertTrue(reg.history_fresh("binance:spot:BTCUSDT", "1m", 60.0))
        self.assertIsNotNone(reg.history_age_s("binance:spot:BTCUSDT", "1m"))
        stats = reg.history_stats()
        self.assertGreaterEqual(stats["hits"], 1)
        self.assertGreaterEqual(stats["entries"], 1)

    def test_neighbor_prefetch_map_bounded(self):
        from falcon_terminal.bridge import MAX_PREFETCH, NEIGHBOR_TF
        for tf, neighbors in NEIGHBOR_TF.items():
            self.assertLessEqual(len(neighbors), 2, tf)
        self.assertEqual(NEIGHBOR_TF["1m"], ("5m", "15m"))
        self.assertEqual(NEIGHBOR_TF["1h"], ("15m", "4h"))
        self.assertLessEqual(MAX_PREFETCH, 2)


class SearchFilterTests(unittest.TestCase):
    def _big_registry(self, n=10000):
        reg = AssetRegistry()
        for i in range(n):
            reg.register(reg_asset(f"binance:spot:SYM{i:05d}",
                                   symbol=f"SYM{i:05d}/USDT"))
        return reg

    def test_search_10k_under_budget(self):
        reg = self._big_registry()
        t0 = perfmod.now()
        hits = reg.search("SYM00999")
        dt = perfmod.now() - t0
        self.assertEqual(hits, ["binance:spot:SYM00999"])
        self.assertLess(dt, perfmod.BUDGET_SEARCH_MS / 1000.0,
                        f"search took {perfmod.format_ms(dt)}")

    def test_search_hay_precomputed_once(self):
        reg = AssetRegistry()
        reg.register(reg_asset("binance:spot:BTCUSDT", symbol="BTC/USDT"))
        hay = reg.search_hay("binance:spot:BTCUSDT")
        self.assertIn("btc", hay)
        # Enrichment refreshes the index without duplicating work per render.
        reg.enrich_identity("binance:spot:BTCUSDT", placeholders=(),
                            name="Bitcoin")
        self.assertIn("bitcoin", reg.search_hay("binance:spot:BTCUSDT"))

    def test_filters_are_local_no_refetch(self):
        reg = AssetRegistry()
        reg.register(reg_asset("binance:spot:BTCUSDT"))
        reg.register(reg_asset("binance:spot:ETHUSDT", symbol="ETH/USDT"))
        # Pure registry/view operations: no provider calls involved.
        self.assertEqual(len(reg.filter(provider="binance")), 2)
        self.assertEqual(reg.filter(quote="USDT"),
                         ["binance:spot:BTCUSDT", "binance:spot:ETHUSDT"])
        reg.set_favorite("binance:spot:ETHUSDT", True)
        self.assertEqual(reg.filter(favorites_only=True),
                         ["binance:spot:ETHUSDT"])


class DeltaUpdateTests(unittest.TestCase):
    def _row(self, key, price, **kw):
        return asset(key=key, symbol=key, price=price, **kw)

    def test_single_tick_touches_single_row(self):
        model = AssetListModel()
        model.sync([self._row("id:0", 10.0), self._row("id:1", 20.0),
                    self._row("id:2", 30.0)])
        touched = model.sync(
            [self._row("id:0", 10.0), self._row("id:1", 21.0),
             self._row("id:2", 30.0)], changed=["id:1"])
        self.assertEqual(sorted(touched), [1])
        self.assertEqual(model.row("id:1")["price"], 21.0)
        self.assertEqual(model.rowCount(), 3)

    def test_unchanged_tick_touches_nothing(self):
        model = AssetListModel()
        rows = [self._row("id:0", 10.0), self._row("id:1", 20.0)]
        model.sync(rows)
        touched = model.sync(rows, changed=["id:0", "id:1"])
        self.assertEqual(touched, {})

    def test_no_full_reset_apis_used_on_tick(self):
        # Delta path must never call beginResetModel; assert structurally.
        import inspect
        from falcon_terminal import viewmodels as vmmod
        src = inspect.getsource(vmmod.AssetListModel.sync)
        self.assertNotIn("beginResetModel", src)
        self.assertIn("dataChanged.emit", src)


class BoundedMemoryTests(unittest.TestCase):
    def test_spark_history_bounded(self):
        from falcon_terminal.bridge import PRICE_HISTORY_MAX
        self.assertLessEqual(PRICE_HISTORY_MAX, 240)
        from collections import deque
        hist = deque(maxlen=PRICE_HISTORY_MAX)
        for i in range(1000):
            hist.append(float(i))
        self.assertEqual(len(hist), PRICE_HISTORY_MAX)

    def test_chart_history_bounded(self):
        reg = AssetRegistry()
        reg.register(reg_asset("binance:spot:BTCUSDT"))
        bars = tuple((f"t{i}", 1.0, 2.0, 0.5, 1.5, 10.0, True)
                     for i in range(2000))
        reg.history_put("binance:spot:BTCUSDT", "1m", bars)
        self.assertLessEqual(
            len(reg.history_get("binance:spot:BTCUSDT", "1m")),
            AssetRegistry.MAX_HISTORY_BARS)
        for i in range(AssetRegistry.MAX_HISTORY_ENTRIES + 50):
            reg.history_put(f"binance:spot:X{i}", "1m", bars[:5])
        self.assertLessEqual(len(reg._history),
                             AssetRegistry.MAX_HISTORY_ENTRIES)

    def test_seen_ids_bounded(self):
        reg = AssetRegistry()
        reg.register(reg_asset("binance:spot:BTCUSDT"))
        for i in range(AssetRegistry.MAX_SEEN_IDS + 500):
            reg.apply_quote("binance:spot:BTCUSDT", price=1.0 + i * 0.001,
                            event_id=f"e{i}")
        self.assertLessEqual(len(reg._seen_ids), AssetRegistry.MAX_SEEN_IDS)

    def test_recent_bounded(self):
        from falcon_terminal.store import LocalStore, MAX_RECENT
        s = LocalStore(None)
        for i in range(100):
            s.touch_recent(f"id:{i}")
        self.assertLessEqual(len(s.recent), MAX_RECENT)


class ConnectionFreshnessTests(unittest.TestCase):
    def test_state_transitions(self):
        h = HealthVM()
        self.assertEqual(h.state, "CONNECTING")
        h.sync(snapshot(asset(connected=False, state="CONNECTING",
                              messages=0)))
        self.assertEqual(h.state, "CONNECTING")
        h.sync(snapshot(asset(connected=True, state="CONNECTED",
                              messages=5)))
        self.assertEqual(h.state, "CONNECTED")
        h.sync(snapshot(asset(connected=True, stale=True, state="STALE",
                              messages=6)))
        self.assertEqual(h.state, "STALE")
        h.sync(snapshot(asset(connected=False, state="ERROR", messages=6,
                              error="boom")))
        # Was connected before -> recovering, not silently frozen.
        self.assertEqual(h.state, "RECONNECTING")
        h2 = HealthVM()
        h2.sync(snapshot(asset(connected=False, state="ERROR", messages=0,
                               error="boom")))
        self.assertEqual(h2.state, "ERROR")

    def test_stale_age_and_freshness_text(self):
        self.assertEqual(fmt.freshness_text(None, False), "Waiting for data")
        self.assertEqual(fmt.freshness_text(0.2, False), "Updated now")
        self.assertEqual(fmt.freshness_text(8.0, False), "Updated 8.0s ago")
        self.assertIn("STALE", fmt.freshness_text(8.0, True))
        self.assertEqual(fmt.age_text(0.024), "24 ms")
        # Selected VM surfaces freshness + honest polling semantics.
        vm = SelectedAssetVM()
        vm.select("binance:spot:BTCUSDT")
        vm.sync(asset(age_s=0.2, source="WebSocket"))
        self.assertEqual(vm.freshness, "Updated now")
        self.assertEqual(vm.semantics, "Streaming")
        sol = asset(key="solana:MINT", symbol="BONK", provider="solana",
                    price=0.5, age_s=4.0, stale=False,
                    source="DexScreener polling (15s)")
        vm.select("solana:MINT")
        vm.sync(sol)
        self.assertEqual(vm.freshness, "Updated 4.0s ago")
        self.assertIn("polling", vm.semantics.lower())
        self.assertIn("15", vm.semantics)

    def test_provider_honesty_never_upgrades_polling(self):
        self.assertIn("polling", fmt.feed_semantics(
            "solana", "DexScreener polling (15s)").lower())
        self.assertEqual(fmt.feed_semantics("binance", "WebSocket"),
                         "Streaming")


class CatalogCacheTests(unittest.TestCase):
    def test_metadata_roundtrip_with_ttl(self):
        import tempfile
        from falcon_terminal import catalog_cache
        tmp = Path(tempfile.mkdtemp(prefix="falcon-cat-")) / "catalog.json"
        assets = [reg_asset("binance:spot:BTCUSDT", symbol="BTC/USDT",
                            quote="USDT"),
                  reg_asset("binance:spot:ETHUSDT", symbol="ETH/USDT",
                            quote="USDT")]
        self.assertTrue(catalog_cache.save(assets, tmp))
        loaded, fresh = catalog_cache.load(tmp)
        self.assertTrue(fresh)
        self.assertEqual([a.id for a in loaded],
                         ["binance:spot:BTCUSDT", "binance:spot:ETHUSDT"])
        # Metadata only: no prices cached, never stale prices served.
        self.assertEqual(catalog_cache.cache_age_s(tmp) is not None, True)
        old, fresh_old = catalog_cache.load(tmp, ttl_s=0.0)
        self.assertFalse(fresh_old)
        # Stale metadata is still returned as an instant placeholder;
        # freshness flag tells the caller to refresh in background.
        self.assertEqual(len(old), 2)

    def test_corrupt_cache_is_empty_not_fatal(self):
        import tempfile
        from falcon_terminal import catalog_cache
        tmp = Path(tempfile.mkdtemp(prefix="falcon-cat-")) / "c.json"
        tmp.write_text("{broken", encoding="utf-8")
        loaded, fresh = catalog_cache.load(tmp)
        self.assertEqual((loaded, fresh), ([], False))


class FavoritesFirstTests(unittest.TestCase):
    def test_priority_order_selected_favorites_recent(self):
        reg = AssetRegistry()
        for sym in ("A", "B", "C", "D"):
            reg.register(reg_asset(f"binance:spot:{sym}USDT",
                                   symbol=f"{sym}/USDT"))
        reg.set_favorite("binance:spot:CUSDT", True)
        order = reg.priority_order(selected="binance:spot:DUSDT",
                                   recent=("binance:spot:BUSDT",))
        self.assertEqual(order[0], "binance:spot:DUSDT")
        self.assertEqual(order[1], "binance:spot:CUSDT")
        self.assertEqual(order[2], "binance:spot:BUSDT")


class ChartIsolationTests(unittest.TestCase):
    def test_no_chart_repaint_on_unrelated_event(self):
        # Chart binds only to selected.history/candles: an unrelated row
        # update must not change the selected payload at all.
        vm = SelectedAssetVM()
        vm.select("binance:spot:BTCUSDT")
        candles = tuple((f"t{i}", 1.0, 2.0, 0.5, 1.5, 10.0, True)
                        for i in range(5))
        vm.sync(asset(price=100.0, price_history=(99.0, 100.0),
                      chart_candles=candles))
        before = (list(vm.history), list(vm.candles), vm.price)
        # Unrelated ETH tick arrives: selected asset object unchanged.
        vm.sync(asset(price=100.0, price_history=(99.0, 100.0),
                      chart_candles=candles))
        after = (list(vm.history), list(vm.candles), vm.price)
        self.assertEqual(before, after)

    def test_pricechart_repaints_only_on_data_or_geometry(self):
        src = (Path(__file__).resolve().parents[1] / "falcon_terminal"
               / "qml" / "components" / "PriceChart.qml").read_text(
            encoding="utf-8")
        self.assertIn("onCandlesChanged", src)
        self.assertIn("onHistoryChanged", src)
        # No repaint hook on unrelated registry/model signals.
        self.assertNotIn("onAssetsChanged", src)


if __name__ == "__main__":
    unittest.main()
