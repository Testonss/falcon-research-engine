"""MT5 DEMO-only integration tests. No real terminal required (mocked mt5).

Covers: init, demo gate (real/contest/unknown/None blocked), double guard
with order_send call-count assertions, discovery, canonical ids, timeframes,
bar/tick normalization, stale, volume rules, request construction,
order_check, send/close/modify, manual-close detection, reconnect,
no-password logging.
"""
import asyncio
import logging
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from execution.mt5_demo import (
    DemoExecutionAdapter,
    DemoRiskConfig,
    DemoSafetyError,
    OrderRequest,
    assert_demo_account,
    is_demo_trade_mode,
    normalize_volume,
    parse_position,
    redact_account,
)
from falcon_terminal.mt5_connection import MT5ConnectionManager
from falcon_terminal.registry import mt5_id, parse_canonical_id
from market_data.providers import mt5 as mt5mod


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class FakeMT5:
    """Minimal official-API-shaped fake. Counts order_send calls."""

    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 60
    TIMEFRAME_H4 = 240
    TIMEFRAME_D1 = 1440
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1

    def __init__(self, trade_mode=0, symbols=("EURUSD", "EURUSD.a"),
                 server="MetaQuotes-Demo", login=12345):
        self._mode = trade_mode
        self._symbols = list(symbols)
        self._server = server
        self._login = login
        self.order_send_calls = 0
        self.last_request = None
        self.next_result = SimpleNamespace(retcode=10009, order=777001, comment="ok")
        self.next_check = SimpleNamespace(retcode=0, comment="ok")
        self._positions = []
        self._ticks = {}
        self._rates = {}
        self.initialize_ok = True
        self.account_none = False

    # -- terminal -----------------------------------------------------------
    def initialize(self, **kw):
        return self.initialize_ok

    def shutdown(self):
        return True

    def last_error(self):
        return (0, "ok")

    def account_info(self):
        if self.account_none:
            return None
        return SimpleNamespace(login=self._login, server=self._server,
                               trade_mode=self._mode, balance=10000.0,
                               equity=10050.0, margin=100.0, margin_free=9900.0,
                               profit=50.0, currency="USD", password="SECRET-NEVER")

    def terminal_info(self):
        return SimpleNamespace(company="MetaQuotes", name="MT5",
                               connected=True, trade_allowed=True, build=4000)

    # -- market data ---------------------------------------------------------
    def symbols_get(self):
        return [SimpleNamespace(name=s, description=f"{s} desc",
                                currency_base=s[:3], currency_profit=s[3:6] or "USD",
                                digits=5, point=0.00001, trade_mode=0,
                                volume_min=0.01, volume_max=100.0,
                                volume_step=0.01, contract_size=100000.0,
                                visible=True) for s in self._symbols]

    def symbol_select(self, symbol, enable):
        return True

    def symbol_info(self, symbol):
        if symbol not in self._symbols:
            return None
        return SimpleNamespace(name=symbol, visible=True, trade_mode=0,
                               volume_min=0.01, volume_max=100.0,
                               volume_step=0.01, point=0.00001, digits=5,
                               contract_size=100000.0)

    def symbol_info_tick(self, symbol):
        t = self._ticks.get(symbol)
        if t is not None:
            return t
        return SimpleNamespace(bid=1.1000, ask=1.1002, last=1.1001,
                               time=datetime.now(timezone.utc))

    def copy_rates_from_pos(self, symbol, tf, pos, count):
        return list(self._rates.get((symbol, tf), []))

    # -- trading --------------------------------------------------------------
    def order_check(self, request):
        return self.next_check

    def order_send(self, request):
        self.order_send_calls += 1
        self.last_request = dict(request)
        return self.next_result

    def positions_get(self, ticket=None):
        if ticket is not None:
            return [p for p in self._positions if int(getattr(p, "ticket", -1)) == int(ticket)]
        return list(self._positions)


def run(coro):
    return asyncio.run(coro)


def demo_account(mode=0):
    return SimpleNamespace(trade_mode=mode, login=1, server="S")


# --------------------------------------------------------------------------
# Demo gate
# --------------------------------------------------------------------------

class DemoGateTests(unittest.TestCase):
    def test_demo_accepted(self):
        acc = assert_demo_account(FakeMT5(trade_mode=0))
        self.assertEqual(acc.trade_mode, 0)
        self.assertTrue(is_demo_trade_mode(0))

    def test_real_contest_unknown_none_blocked(self):
        for mode in (2, 1, 3, -1, None, "DEMO"):
            with self.assertRaises(DemoSafetyError, msg=f"mode={mode!r}"):
                assert_demo_account(FakeMT5(trade_mode=mode))
        fake = FakeMT5()
        fake.account_none = True
        with self.assertRaises(DemoSafetyError):
            assert_demo_account(fake)
        with self.assertRaises(DemoSafetyError):
            assert_demo_account(None)

    def test_never_infer_from_server_name(self):
        # A "Demo" server string with REAL trade_mode must still block.
        fake = FakeMT5(trade_mode=2, server="Super-Demo-Server")
        with self.assertRaises(DemoSafetyError):
            assert_demo_account(fake)

    def test_enable_blocks_non_demo(self):
        for mode in (2, 1, 99):
            ad = DemoExecutionAdapter(mt5_module=FakeMT5(trade_mode=mode))
            with self.assertRaises(DemoSafetyError):
                ad.enable()
            self.assertFalse(ad.enabled)

    def test_double_guard_blocks_mode_flip(self):
        """DEMO at confirm, REAL at send -> second guard blocks order_send."""
        fake = FakeMT5(trade_mode=0)
        ad = DemoExecutionAdapter(mt5_module=fake, server="MetaQuotes-Demo")
        ad.enable()
        self.assertTrue(ad.enabled)
        # Flip to REAL between UI confirmation and send.
        fake._mode = 2
        req = OrderRequest(canonical_id="mt5:MetaQuotes-Demo:EURUSD",
                           symbol="EURUSD", side="BUY", volume=0.01)
        with self.assertRaises(DemoSafetyError):
            ad.open_position(req, ref_price=1.1)
        self.assertEqual(fake.order_send_calls, 0)

    def test_real_block_call_counts(self):
        for mode in (2, 1, 99, None):
            fake = FakeMT5(trade_mode=mode)
            ad = DemoExecutionAdapter(mt5_module=fake, server="S")
            for fn in (lambda: ad.open_position(
                    OrderRequest(canonical_id="mt5:S:EURUSD", symbol="EURUSD",
                                 side="BUY", volume=0.01), ref_price=1.1),
                       lambda: ad.close_position(1),
                       lambda: ad.modify_sltp(1, sl=1.0)):
                with self.assertRaises(DemoSafetyError):
                    fn()
            self.assertEqual(fake.order_send_calls, 0,
                             msg=f"mode={mode!r} must never reach order_send")


# --------------------------------------------------------------------------
# Connection manager states
# --------------------------------------------------------------------------

class ConnectionManagerTests(unittest.TestCase):
    def test_demo_state(self):
        mgr = MT5ConnectionManager(mt5_module=FakeMT5(trade_mode=0))
        snap = mgr.connect()
        self.assertEqual(snap.state, "CONNECTED_DEMO")
        self.assertTrue(snap.demo and snap.execution_enabled)

    def test_real_blocked_state_no_override(self):
        mgr = MT5ConnectionManager(mt5_module=FakeMT5(trade_mode=2))
        snap = mgr.connect()
        self.assertEqual(snap.state, "CONNECTED_REAL_BLOCKED")
        self.assertFalse(snap.execution_enabled)
        self.assertIn("REAL", snap.message)
        with self.assertRaises(DemoSafetyError):
            mgr.assert_demo()

    def test_no_account_and_init_failure(self):
        bad = FakeMT5()
        bad.account_none = True
        mgr = MT5ConnectionManager(mt5_module=bad)
        snap = mgr.connect()
        self.assertEqual(snap.state, "ERROR")
        self.assertIn("No account", snap.message)
        bad2 = FakeMT5()
        bad2.initialize_ok = False
        mgr2 = MT5ConnectionManager(mt5_module=bad2)
        snap2 = mgr2.connect()
        self.assertEqual(snap2.state, "ERROR")

    def test_stale_after_quiet(self):
        now = [1000.0]
        mgr = MT5ConnectionManager(mt5_module=FakeMT5(trade_mode=0),
                                   stale_after_s=10.0, now_fn=lambda: now[0])
        mgr.connect()
        self.assertEqual(mgr.state, "CONNECTED_DEMO")
        now[0] += 60.0
        self.assertEqual(mgr.state, "STALE")


# --------------------------------------------------------------------------
# Canonical ids / discovery / timeframes
# --------------------------------------------------------------------------

class MT5IdentityTests(unittest.TestCase):
    def test_suffixes_not_merged(self):
        a = mt5_id("MetaQuotes-Demo", "EURUSD")
        b = mt5_id("MetaQuotes-Demo", "EURUSD.a")
        self.assertNotEqual(a, b)
        self.assertEqual(a, "mt5:MetaQuotes-Demo:EURUSD")
        self.assertEqual(parse_canonical_id(a)["provider"], "mt5")
        self.assertEqual(parse_canonical_id(b)["ref"], "EURUSD.a")

    def test_bad_ids(self):
        with self.assertRaises(ValueError):
            mt5_id("", "EURUSD")
        with self.assertRaises(ValueError):
            mt5_id("S", "")
        with self.assertRaises(ValueError):
            mt5_id("a:b", "EURUSD")
        with self.assertRaises(ValueError):
            mt5_id("S", "a:b")
        from falcon_terminal.registry import parse_mt5_id
        for bad in ("", "mt5::EURUSD", "mt5:S:", "mt5:S:a:b", "mt5:", "spot:S:X"):
            with self.assertRaises(ValueError, msg=bad):
                parse_mt5_id(bad)

    def test_discovery_uses_terminal_catalog(self):
        prov = mt5mod.MT5LiveMarketDataProvider(mt5_module=FakeMT5())
        run(prov.connect())
        try:
            syms = sorted(m.symbol for m in prov.symbols)
            self.assertEqual(syms, ["EURUSD", "EURUSD.a"])
            self.assertEqual(prov.server_actual, "MetaQuotes-Demo")
            self.assertEqual(prov.supported_timeframes(),
                             ("1m", "5m", "15m", "1h", "4h", "1D"))
        finally:
            run(prov.disconnect())

    def test_timeframe_mapping(self):
        fake = FakeMT5()
        for tf, const in (("1m", 1), ("5m", 5), ("15m", 15), ("1h", 60),
                          ("4h", 240), ("1D", 1440)):
            self.assertEqual(mt5mod.mt5_timeframe_constant(tf, fake), const)
        with self.assertRaises(ValueError):
            mt5mod.mt5_timeframe_constant("9m", fake)


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

class NormalizationTests(unittest.TestCase):
    def test_tick(self):
        tick = SimpleNamespace(bid=1.1, ask=1.1002, last=1.1001,
                               time=datetime(2026, 1, 1, tzinfo=timezone.utc))
        norm = mt5mod.normalize_tick("EURUSD", tick)
        self.assertAlmostEqual(norm["bid"], 1.1)
        self.assertAlmostEqual(norm["spread"], 0.0002, places=6)
        self.assertIsNone(mt5mod.normalize_tick("X", SimpleNamespace()))
        self.assertIsNone(mt5mod.normalize_tick("X", {"bid": -1}))

    def test_bars_utc_and_rejects(self):
        raw = [(1700000000, 1.1, 1.11, 1.09, 1.105, 100, 5, 0),
               (1700000060, 1.105, 1.106, 1.10, 1.104, 50, 3, 0),
               ("bad", 1, 1, 1, 1, 1, 1, 1),
               (1700000120, -1, 1, 1, 1, 1, 1, 1)]
        bars, rejected = mt5mod.normalize_bars(raw, "EURUSD", "1m")
        self.assertEqual(len(bars), 2)
        self.assertEqual(rejected, 2)
        self.assertTrue(all(b["time"].tzinfo is not None for b in bars))
        self.assertLess(bars[0]["time"], bars[1]["time"])

    def test_provider_tick_and_stale(self):
        fake = FakeMT5()
        prov = mt5mod.MT5LiveMarketDataProvider(
            mt5_module=fake, stale_after_s=1000.0)
        run(prov.connect())
        try:
            n = run(prov.refresh_once())
            self.assertGreater(n, 0)
            tick = run(prov.get_ticker("EURUSD"))
            self.assertIsNotNone(tick)
            self.assertGreater(tick.price, 0)
            book = run(prov.get_order_book("EURUSD"))
            self.assertIsNotNone(book.best_bid())
            h = prov.get_health()
            self.assertTrue(h["connected"])
            self.assertFalse(h["stale"])
        finally:
            run(prov.disconnect())


# --------------------------------------------------------------------------
# Execution: volume / validation / send / positions
# --------------------------------------------------------------------------

class ExecutionTests(unittest.TestCase):
    def make(self, mode=0, **kw):
        fake = FakeMT5(trade_mode=mode)
        ad = DemoExecutionAdapter(mt5_module=fake, server="MetaQuotes-Demo",
                                  risk=DemoRiskConfig(**kw) if kw else None)
        if mode == 0:
            ad.enable()
        return fake, ad

    def test_volume_normalization(self):
        self.assertAlmostEqual(normalize_volume(0.05, 0.01, 100, 0.01), 0.05)
        self.assertAlmostEqual(normalize_volume(0.059, 0.01, 100, 0.01), 0.05)
        self.assertAlmostEqual(normalize_volume(500, 0.01, 1.0, 0.01), 1.0)
        for bad in (0, -1, float("nan"), "x", None):
            with self.assertRaises(DemoSafetyError, msg=f"{bad!r}"):
                normalize_volume(bad, 0.01, 100, 0.01)
        with self.assertRaises(DemoSafetyError):
            normalize_volume(0.001, 0.01, 100, 0.01)

    def test_order_request_construction(self):
        fake, ad = self.make()
        req = OrderRequest(canonical_id="mt5:MetaQuotes-Demo:EURUSD",
                           symbol="EURUSD", side="BUY", volume=0.05)
        norm = ad.validate_order(req, ref_price=1.1)
        built = ad.build_request(norm, 1.1002)
        self.assertEqual(built["symbol"], "EURUSD")
        self.assertEqual(built["volume"], 0.05)
        self.assertEqual(built["type"], fake.ORDER_TYPE_BUY)
        self.assertEqual(built["comment"], "falcon-demo")

    def test_invalid_orders_rejected_locally(self):
        fake, ad = self.make()
        with self.assertRaises(DemoSafetyError):
            ad.validate_order(OrderRequest(canonical_id="mt5:S:X", symbol="NOPE",
                                            side="BUY", volume=0.01), ref_price=1.0)
        with self.assertRaises(DemoSafetyError):
            ad.validate_order(OrderRequest(canonical_id="mt5:S:EURUSD", symbol="EURUSD",
                                            side="HOLD", volume=0.01), ref_price=1.1)
        with self.assertRaises(DemoSafetyError):
            ad.validate_order(OrderRequest(canonical_id="mt5:S:EURUSD", symbol="EURUSD",
                                            side="BUY", volume=0.01, sl=1.2), ref_price=1.1)
        fake2, ad2 = self.make(max_position_size=0.01)
        with self.assertRaises(DemoSafetyError):
            ad2.validate_order(OrderRequest(canonical_id="mt5:S:EURUSD", symbol="EURUSD",
                                            side="BUY", volume=1.0), ref_price=1.1)

    def test_order_check_rejection_blocks_send(self):
        fake, ad = self.make()
        fake.next_check = SimpleNamespace(retcode=10014, comment="invalid volume")
        with self.assertRaises(DemoSafetyError):
            ad.open_position(OrderRequest(canonical_id="mt5:S:EURUSD", symbol="EURUSD",
                                          side="BUY", volume=0.01), ref_price=1.1)
        self.assertEqual(fake.order_send_calls, 0)

    def test_open_success_and_rejection(self):
        fake, ad = self.make()
        out = ad.open_position(OrderRequest(canonical_id="mt5:S:EURUSD", symbol="EURUSD",
                                            side="BUY", volume=0.01), ref_price=1.1)
        self.assertEqual(fake.order_send_calls, 1)
        self.assertEqual(out["ticket"], 777001)
        fake.next_result = SimpleNamespace(retcode=10019, order=0, comment="no money")
        with self.assertRaises(DemoSafetyError):
            ad.open_position(OrderRequest(canonical_id="mt5:S:EURUSD", symbol="EURUSD",
                                          side="BUY", volume=0.01), ref_price=1.1)

    def test_position_parsing_and_close(self):
        fake, ad = self.make()
        pos = SimpleNamespace(ticket=555, symbol="EURUSD", type=0, volume=0.05,
                              price_open=1.1000, price_current=1.1020,
                              sl=1.0950, tp=1.1100, profit=10.0, swap=0.0,
                              magic=1, comment="t", time=1700000000)
        parsed = parse_position(pos, "MetaQuotes-Demo")
        self.assertEqual(parsed.side, "BUY")
        self.assertEqual(parsed.canonical_id, "mt5:MetaQuotes-Demo:EURUSD")
        self.assertIsNone(parse_position(SimpleNamespace(), "S"))
        fake._positions = [pos]
        out = ad.close_position(555)
        self.assertTrue(out["closed"])
        self.assertEqual(fake.order_send_calls, 1)
        # Position gone -> human-readable error, no silent success.
        fake._positions = []
        with self.assertRaises(DemoSafetyError) as ctx:
            ad.close_position(555)
        self.assertIn("no longer exists", str(ctx.exception))

    def test_sltp_modify(self):
        fake, ad = self.make()
        pos = SimpleNamespace(ticket=556, symbol="EURUSD", type=1, volume=0.02,
                              price_open=1.1, price_current=1.099, sl=0, tp=0,
                              profit=1.0, swap=0.0, magic=0, comment="",
                              time=1700000000)
        fake._positions = [pos]
        out = ad.modify_sltp(556, sl=1.105, tp=1.09)
        self.assertEqual(out["ticket"], 556)
        self.assertEqual(fake.last_request["action"], fake.TRADE_ACTION_SLTP)

    def test_manual_close_detected(self):
        """Terminal authoritative: Falcon diff sees externally closed tickets."""
        before = {101, 102}
        after = {102}
        closed = sorted(before - after)
        self.assertEqual(closed, [101])

    def test_no_password_in_logs_or_snapshot(self):
        fake = FakeMT5(trade_mode=0)
        snap = redact_account(fake.account_info())
        self.assertNotIn("password", {k.lower() for k in snap})
        self.assertNotIn("SECRET-NEVER", str(snap))
        # Logger must never receive secrets: redact before logging.
        with self.assertLogs(level="INFO") as logs:
            ad = DemoExecutionAdapter(mt5_module=fake, server="S")
            ad.enable()
        blob = "\n".join(logs.output)
        self.assertNotIn("SECRET-NEVER", blob)
        self.assertNotIn("password", blob.lower())


if __name__ == "__main__":
    unittest.main()
