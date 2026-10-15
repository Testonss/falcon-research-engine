"""Liquidity-aware execution tests: participation, impact, dynamic spread.

All tests deterministic (seeded exchange, fixed inputs). Strategy params
are never tuned here — synthetic data only.
"""
import asyncio
import unittest
from datetime import datetime, timezone, timedelta

from config import ConfigManager
from market_data import MarketDataHandler, Candle
from exchange import PaperExchange, OrderSide, OrderType, OrderStatus
from strategy import StrategyManager
from execution import ExecutionEngine
from risk import RiskManager
from portfolio import PortfolioManager
from backtest import BacktestEngine
from evaluation import estimate_capacity, compute_report_metrics


def run(coro):
    return asyncio.run(coro)


def make_exchange(**overrides):
    config = ConfigManager()
    config.set('simulated_latency_ms', 0)
    for k, v in overrides.items():
        config.set(k, v)
    ex = PaperExchange(config, seed=7)
    run(ex.connect())
    return config, ex


def trend_candles(n=60, volume=3000.0, prefix="liq"):
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [Candle(symbol='BTC/USDT', open=100 + i, high=100 + i + 0.2,
                   low=100 + i - 0.2, close=100 + i, volume=volume,
                   timestamp=base + timedelta(minutes=5 * i), interval='5m',
                   event_id=f'{prefix}{i}') for i in range(n)]


def make_stack(**overrides):
    config = ConfigManager()
    config.set('simulated_latency_ms', 0)
    config.set('min_volume', 100.0)
    config.set('volatility_max', 1.0)
    config.set('initial_cash', 10000.0)
    for k, v in overrides.items():
        config.set(k, v)
    md = MarketDataHandler(config)
    ex = PaperExchange(config, seed=11)
    run(ex.connect())
    risk = RiskManager(config)
    pf = PortfolioManager(config)
    eng = ExecutionEngine(config, ex, risk)
    strat = StrategyManager(config)
    bt = BacktestEngine(config, md, eng, pf, risk, strat)
    return config, md, ex, risk, pf, eng, strat, bt


class TestParticipationCap(unittest.TestCase):
    def test_order_clipped_to_max_participation(self):
        _, ex = make_exchange(max_participation_rate=0.10)
        try:
            o = run(ex.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                    10000.0, reference_price=100.0,
                                    client_order_id='cap1', bar_volume=5000.0,
                                    bar_high=101.0, bar_low=99.0))
            self.assertEqual(o.status, OrderStatus.PARTIAL)
            self.assertTrue(o.liquidity_capped)
            self.assertAlmostEqual(o.filled_quantity, 500.0)  # 10% of 5000
            self.assertAlmostEqual(o.participation_rate, 2.0)  # requested intent
            # Deterministic rerun on a fresh exchange with same seed.
            _, ex2 = make_exchange(max_participation_rate=0.10)
            try:
                o2 = run(ex2.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                          10000.0, reference_price=100.0,
                                          client_order_id='cap1', bar_volume=5000.0,
                                          bar_high=101.0, bar_low=99.0))
                self.assertAlmostEqual(o2.avg_fill_price, o.avg_fill_price, places=9)
                self.assertAlmostEqual(o2.filled_quantity, o.filled_quantity, places=9)
            finally:
                run(ex2.disconnect())
        finally:
            run(ex.disconnect())

    def test_fills_never_exceed_cap_across_sizes(self):
        _, ex = make_exchange(max_participation_rate=0.10)
        try:
            for i, qty in enumerate([10.0, 400.0, 500.0, 501.0, 5000.0]):
                o = run(ex.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                        qty, reference_price=100.0,
                                        client_order_id=f'prop{i}', bar_volume=5000.0,
                                        bar_high=100.5, bar_low=99.5))
                self.assertLessEqual(o.filled_quantity, 500.0 + 1e-9)
                self.assertEqual(o.liquidity_capped, qty > 500.0)
        finally:
            run(ex.disconnect())

    def test_insufficient_liquidity_rejects_dust(self):
        _, ex = make_exchange(min_fill_notional=10.0)
        try:
            o = run(ex.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                    0.001, reference_price=100.0,
                                    client_order_id='dust1', bar_volume=5000.0))
            self.assertEqual(o.status, OrderStatus.REJECTED)
            self.assertEqual(o.reject_reason, 'insufficient_liquidity')
        finally:
            run(ex.disconnect())

    def test_zero_volume_falls_back_to_legacy(self):
        _, ex = make_exchange()
        try:
            o = run(ex.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                    100.0, reference_price=100.0,
                                    client_order_id='novol', bar_volume=0.0))
            self.assertEqual(o.status, OrderStatus.FILLED)
            self.assertFalse(o.liquidity_capped)
            self.assertEqual(o.impact_cost, 0.0)
            self.assertEqual(o.participation_rate, 0.0)
        finally:
            run(ex.disconnect())

    def test_legacy_path_without_volume_unchanged(self):
        _, ex = make_exchange()
        try:
            o = run(ex.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                    100.0, reference_price=100.0,
                                    client_order_id='leg1'))
            self.assertEqual(o.status, OrderStatus.FILLED)
            self.assertEqual(o.filled_quantity, 100.0)
            self.assertFalse(o.liquidity_capped)
            self.assertEqual(o.impact_cost, 0.0)
        finally:
            run(ex.disconnect())


class TestImpactModel(unittest.TestCase):
    def test_slippage_grows_with_participation(self):
        # slippage_pct=0 isolates the sqrt-impact leg.
        _, ex = make_exchange(slippage_pct=0.0, market_impact_coeff=0.01)
        try:
            small = run(ex.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                        100.0, reference_price=100.0,
                                        client_order_id='imp_s', bar_volume=100000.0,
                                        bar_high=100.1, bar_low=99.9))
            large = run(ex.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                        9000.0, reference_price=100.0,
                                        client_order_id='imp_l', bar_volume=100000.0,
                                        bar_high=100.1, bar_low=99.9))
            self.assertEqual(small.status, OrderStatus.FILLED)
            self.assertEqual(large.status, OrderStatus.FILLED)
            # sqrt(0.09)/sqrt(0.001) = ~9.5x impact -> strictly worse fill.
            self.assertGreater(large.avg_fill_price, small.avg_fill_price)
            self.assertGreater(large.impact_cost / 9000.0, small.impact_cost / 100.0)
            # Determinism across fresh instances.
            _, ex2 = make_exchange(slippage_pct=0.0, market_impact_coeff=0.01)
            try:
                r = run(ex2.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                         9000.0, reference_price=100.0,
                                         client_order_id='imp_l', bar_volume=100000.0,
                                         bar_high=100.1, bar_low=99.9))
                self.assertAlmostEqual(r.avg_fill_price, large.avg_fill_price, places=9)
            finally:
                run(ex2.disconnect())
        finally:
            run(ex.disconnect())

    def test_zero_impact_coeff_matches_legacy(self):
        cfg_a, ex_a = make_exchange(slippage_pct=0.0, market_impact_coeff=0.0)
        cfg_b, ex_b = make_exchange(slippage_pct=0.0, market_impact_coeff=0.0)
        try:
            a = run(ex_a.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                      100.0, reference_price=100.0,
                                      client_order_id='z', bar_volume=5000.0,
                                      bar_high=100.1, bar_low=99.9))
            b = run(ex_b.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                      100.0, reference_price=100.0,
                                      client_order_id='z'))
            self.assertAlmostEqual(a.avg_fill_price, b.avg_fill_price, places=9)
            self.assertEqual(a.impact_cost, 0.0)
        finally:
            run(ex_a.disconnect())
            run(ex_b.disconnect())


class TestDynamicSpread(unittest.TestCase):
    def test_wide_range_worsens_fill(self):
        _, ex = make_exchange(slippage_pct=0.0, market_impact_coeff=0.0,
                              spread_pct=0.0005, dynamic_spread_coeff=0.5)
        try:
            narrow = run(ex.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                         10.0, reference_price=100.0,
                                         client_order_id='sp_n', bar_volume=5000.0,
                                         bar_high=100.1, bar_low=99.9))
            wide = run(ex.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                       10.0, reference_price=100.0,
                                       client_order_id='sp_w', bar_volume=5000.0,
                                       bar_high=102.0, bar_low=98.0))
            self.assertGreater(wide.avg_fill_price, narrow.avg_fill_price)
            self.assertGreater(wide.spread_cost, narrow.spread_cost)
        finally:
            run(ex.disconnect())

    def test_zero_dyn_coeff_ignores_range(self):
        _, ex = make_exchange(slippage_pct=0.0, market_impact_coeff=0.0,
                              spread_pct=0.0005, dynamic_spread_coeff=0.0)
        try:
            a = run(ex.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                    10.0, reference_price=100.0,
                                    client_order_id='q1', bar_volume=5000.0,
                                    bar_high=100.1, bar_low=99.9))
            b = run(ex.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                    10.0, reference_price=100.0,
                                    client_order_id='q2', bar_volume=5000.0,
                                    bar_high=110.0, bar_low=90.0))
            self.assertAlmostEqual(a.avg_fill_price, b.avg_fill_price, places=9)
        finally:
            run(ex.disconnect())


class TestCostBreakdown(unittest.TestCase):
    def test_legs_telescope_to_fill_deviation(self):
        _, ex = make_exchange(spread_pct=0.001, slippage_pct=0.001,
                              market_impact_coeff=0.005)
        try:
            o = run(ex.submit_order('BTC/USDT', OrderSide.BUY, OrderType.MARKET,
                                    200.0, reference_price=100.0,
                                    client_order_id='bd1', bar_volume=5000.0,
                                    bar_high=100.5, bar_low=99.5))
            self.assertEqual(o.status, OrderStatus.FILLED)
            legs = o.spread_cost + o.slippage_cost + o.impact_cost
            self.assertAlmostEqual(legs, abs(o.avg_fill_price - 100.0) * 200.0, places=6)
            self.assertAlmostEqual(o.commission, 200.0 * o.avg_fill_price * 0.002, places=6)
            self.assertGreater(o.spread_cost, 0.0)
            self.assertGreater(o.impact_cost, 0.0)
        finally:
            run(ex.disconnect())

    def test_backtest_breakdown_reconciles(self):
        _, _, ex, _, _, _, _, bt = make_stack(spread_pct=0.001, slippage_pct=0.001,
                                              market_impact_coeff=0.005,
                                              dynamic_spread_coeff=0.25)
        try:
            res = run(bt.run('BTC/USDT', trend_candles(60)))
            if res.trades:
                recon = (res.realized_pnl + res.fees_paid + res.spread_paid
                         + res.slippage_paid + res.impact_paid)
                self.assertAlmostEqual(res.gross_pnl, recon, places=4)
                self.assertGreater(res.spread_paid + res.slippage_paid + res.impact_paid, 0.0)
        finally:
            run(ex.disconnect())

    def test_turnover_split_and_participation_stats(self):
        _, _, ex, _, _, _, _, bt = make_stack()
        try:
            res = run(bt.run('BTC/USDT', trend_candles(60)))
            self.assertAlmostEqual(res.turnover,
                                   res.turnover_long + res.turnover_short, places=6)
            if res.trades:
                self.assertGreater(res.participation_n, 0)
                avg = res.participation_sum / res.participation_n
                self.assertGreaterEqual(avg, 0.0)
                self.assertLessEqual(avg, res.max_participation + 1e-12)
        finally:
            run(ex.disconnect())

    def test_insufficient_liquidity_counted_in_backtest(self):
        # Dust-level bar volume + enabled dust filter: sizing still wants ~10
        # quote units, so every fill is capped below min_fill_notional.
        _, _, ex, _, _, _, _, bt = make_stack(min_volume=0.0, min_fill_notional=10.0)
        try:
            res = run(bt.run('BTC/USDT', trend_candles(40, volume=0.000001)))
            self.assertEqual(res.trades, 0)
            self.assertGreater(res.insufficient_liquidity_rejects, 0)
            self.assertGreater(res.rejected_orders, 0)
        finally:
            run(ex.disconnect())


def make_stack(**overrides):
    config = ConfigManager()
    config.set('simulated_latency_ms', 0)
    config.set('min_volume', 100.0)
    config.set('volatility_max', 1.0)
    config.set('initial_cash', 10000.0)
    for k, v in overrides.items():
        config.set(k, v)
    from market_data import MarketDataHandler
    from strategy import StrategyManager
    md = MarketDataHandler(config)
    ex = PaperExchange(config, seed=11)
    run(ex.connect())
    risk = RiskManager(config)
    pf = PortfolioManager(config)
    eng = ExecutionEngine(config, ex, risk)
    strat = StrategyManager(config)
    bt = BacktestEngine(config, md, eng, pf, risk, strat)
    return config, md, ex, risk, pf, eng, strat, bt


class TestCapacity(unittest.TestCase):
    def _factory(self, scale, impact=0.02):
        config = ConfigManager()
        config.set('simulated_latency_ms', 0)
        config.set('initial_cash', 10000.0 * scale)
        config.set('risk_per_trade', 10.0 * scale)
        config.set('min_volume', 100.0)
        config.set('volatility_max', 1.0)
        config.set('market_impact_coeff', impact)
        from market_data import MarketDataHandler
        from strategy import StrategyManager
        md = MarketDataHandler(config)
        ex = PaperExchange(config, seed=11)
        risk = RiskManager(config)
        pf = PortfolioManager(config)
        eng = ExecutionEngine(config, ex, risk)
        strat = StrategyManager(config)
        bt = BacktestEngine(config, md, eng, pf, risk, strat)
        return config, md, ex, risk, pf, eng, strat, bt

    def test_scan_ordered_deterministic(self):
        candles = trend_candles(80)
        r1 = run(estimate_capacity('BTC/USDT', candles, self._factory, [1, 5, 20]))
        r2 = run(estimate_capacity('BTC/USDT', candles, self._factory, [1, 5, 20]))
        self.assertEqual([lv['multiplier'] for lv in r1['levels']], [1.0, 5.0, 20.0])
        for a, b in zip(r1['levels'], r2['levels']):
            self.assertAlmostEqual(a['net_pnl'], b['net_pnl'], places=9)
            self.assertEqual(a['fills'], b['fills'])
        self.assertIn('max_survivable_multiplier', r1['summary'])

    def test_participation_and_capping_rise_with_scale(self):
        candles = trend_candles(80)
        out = run(estimate_capacity('BTC/USDT', candles, self._factory, [1, 100]))
        small, big = out['levels']
        self.assertGreater(big['max_participation'], small['max_participation'])
        self.assertGreaterEqual(big['liquidity_capped_fills'], small['liquidity_capped_fills'])
        self.assertGreater(big['liquidity_capped_fills'], 0)
        self.assertGreater(big['impact_paid'], small['impact_paid'])

    def test_rejects_empty_multipliers(self):
        candles = trend_candles(20)
        with self.assertRaises(ValueError):
            run(estimate_capacity('BTC/USDT', candles, self._factory, []))


if __name__ == '__main__':
    unittest.main()
