"""Evaluation-framework tests: CSV, execution models, splits, metrics, no-leakage.

No strategy tuning here — fixed params, synthetic/sample data only.
"""
import asyncio
import csv
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from config import ConfigManager
from market_data import MarketDataHandler, Candle, load_ohlcv_csv, load_ohlcv_dataset
from exchange import PaperExchange
from strategy import StrategyManager, TradingSignal, SignalType
from execution import ExecutionEngine
from risk import RiskManager
from portfolio import PortfolioManager
from backtest import BacktestEngine, BacktestResult
from evaluation import (
    temporal_split, walk_forward_splits, walk_forward_folds,
    verify_fold_isolation, compute_report_metrics,
    run_walk_forward, summarize_regimes, run_cost_sweep,
    compare_execution_models, export_report_json, export_folds_csv,
    assert_no_time_overlap, sharpe_from_equity,
)


def run(coro):
    return asyncio.run(coro)


def make_candles(prices, opens=None, start_ts=None, symbol="BTC/USDT", prefix="e"):
    if start_ts is None:
        start_ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    out = []
    for i, p in enumerate(prices):
        o = opens[i] if opens is not None else p
        out.append(Candle(symbol=symbol, open=o, high=max(o, p) + 0.1,
                          low=min(o, p) - 0.1, close=p, volume=5000.0,
                          timestamp=start_ts + timedelta(minutes=5 * i),
                          interval="5m", event_id=f"{prefix}{i}"))
    return out


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


class ProbeLong:
    """Always-LONG probe recording windows for leakage checks."""

    def __init__(self):
        self.windows = []

    def generate_signal(self, symbol, market_data):
        candles = market_data.get('candles', [])
        if len(candles) < 6:
            return None
        self.windows.append(list(candles))
        return TradingSignal(symbol=symbol, signal_type=SignalType.LONG, confidence=1.0,
                             timestamp=datetime.now(timezone.utc), price=candles[-1].close,
                             signal_id=f"probe_{len(self.windows)}")


class TestCSVLoader(unittest.TestCase):
    def test_loads_sample_csv(self):
        candles, stats = load_ohlcv_csv('data/sample_ohlcv.csv')
        self.assertEqual(stats['loaded'], 240)
        self.assertEqual(stats['skipped_bad'], 0)
        self.assertEqual(len(candles), 240)
        ts = [c.timestamp for c in candles]
        self.assertEqual(ts, sorted(ts))
        self.assertTrue(all(c.open > 0 and c.close > 0 and c.volume >= 0 for c in candles))

    def test_sorts_out_of_order_and_counts_duplicates(self):
        candles, _ = load_ohlcv_csv('data/sample_ohlcv.csv')
        rev = list(reversed(candles))
        with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False, encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            for c in rev:
                w.writerow([c.timestamp.isoformat(), c.open, c.high, c.low, c.close, c.volume])
            # duplicate first row
            c0 = rev[0]
            w.writerow([c0.timestamp.isoformat(), c0.open, c0.high, c0.low, c0.close, c0.volume])
            path = f.name
        try:
            loaded, stats = load_ohlcv_csv(path)
            self.assertEqual(stats['loaded'], len(candles))
            self.assertEqual(stats['duplicates'], 1)
            self.assertEqual(stats['out_of_order_sorted'], 1)
            self.assertEqual([c.timestamp for c in loaded],
                             sorted(c.timestamp for c in loaded))
        finally:
            os.unlink(path)

    def test_skips_bad_rows(self):
        with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False, encoding='utf-8') as f:
            f.write('timestamp,open,high,low,close,volume\n')
            f.write('2024-01-01T00:00:00+00:00,100,101,99,100.5,1000\n')   # good
            f.write('2024-01-01T00:05:00+00:00,100,101,99,,1000\n')        # bad close
            f.write('2024-01-01T00:10:00+00:00,100,101,99,101,-5\n')       # bad volume
            f.write('not-a-date,100,101,99,100,1000\n')                    # bad ts
            f.write('2024-01-01T00:15:00+00:00,100,101,99,102,500\n')      # good
            path = f.name
        try:
            loaded, stats = load_ohlcv_csv(path)
            self.assertEqual(stats['loaded'], 2)
            self.assertEqual(stats['skipped_bad'], 3)
        finally:
            os.unlink(path)

    def test_alt_headers_accepted(self):
        with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False, encoding='utf-8') as f:
            f.write('time,o,h,l,c,vol\n')
            f.write('2024-01-01T00:00:00Z,100,101,99,100.5,1000\n')
            path = f.name
        try:
            loaded, stats = load_ohlcv_csv(path, symbol='ETH/USDT', interval='1m')
            self.assertEqual(stats['loaded'], 1)
            self.assertEqual(loaded[0].symbol, 'ETH/USDT')
        finally:
            os.unlink(path)


class TestExecutionModel(unittest.TestCase):
    def _gap_candles(self):
        # Opens differ from closes by a fixed gap (+2): models must diverge.
        closes = [100.0 + i for i in range(40)]
        opens = [c + 2.0 for c in closes]
        return make_candles(closes, opens=opens, prefix="gap")

    def test_next_open_differs_from_next_close(self):
        candles = self._gap_candles()
        _, _, _, _, _, _, _, bt_o = make_stack(execution_model='next_open',
                                               spread_pct=0.0, slippage_pct=0.0,
                                               maker_fee=0.0, taker_fee=0.0,
                                               stop_loss_pct=10.0, take_profit_pct=10.0)
        probe_o = ProbeLong()
        bt_o.strategy_manager = probe_o  # type: ignore
        r_o = run(bt_o.run('BTC/USDT', list(candles), execution_model='next_open'))

        _, _, _, _, _, _, _, bt_c = make_stack(execution_model='next_close',
                                               spread_pct=0.0, slippage_pct=0.0,
                                               maker_fee=0.0, taker_fee=0.0,
                                               stop_loss_pct=10.0, take_profit_pct=10.0)
        probe_c = ProbeLong()
        bt_c.strategy_manager = probe_c  # type: ignore
        r_c = run(bt_c.run('BTC/USDT', list(candles), execution_model='next_close'))

        self.assertEqual(r_o.execution_model, 'next_open')
        self.assertEqual(r_c.execution_model, 'next_close')
        # With zero costs, fill == execution-bar price. Probe needs >= 6 bars,
        # so first signal window is candles[:6] executed at bar 6:
        # open=108, close=106.
        fills_o = [o.avg_fill_price for o, _ in bt_o.execution_engine.get_execution_history()]
        fills_c = [o.avg_fill_price for o, _ in bt_c.execution_engine.get_execution_history()]
        self.assertGreater(len(fills_o), 0)
        self.assertGreater(len(fills_c), 0)
        self.assertAlmostEqual(fills_o[0], 108.0, places=6)
        self.assertAlmostEqual(fills_c[0], 106.0, places=6)

    def test_probe_never_sees_execution_bar(self):
        candles = self._gap_candles()
        _, _, _, _, _, _, _, bt = make_stack(execution_model='next_open',
                                             spread_pct=0.0, slippage_pct=0.0,
                                             maker_fee=0.0, taker_fee=0.0,
                                             stop_loss_pct=10.0, take_profit_pct=10.0)
        probe = ProbeLong()
        bt.strategy_manager = probe  # type: ignore
        run(bt.run('BTC/USDT', list(candles), execution_model='next_open'))
        # Window of length L is candles[:L], executed at candles[L].
        for window in probe.windows:
            exec_bar = candles[len(window)]
            self.assertLess(window[-1].timestamp, exec_bar.timestamp)


class TestSplits(unittest.TestCase):
    def test_temporal_split_no_overlap_embargo(self):
        candles, _ = load_ohlcv_csv('data/sample_ohlcv.csv')
        train, valid, test = temporal_split(candles, 0.6, 0.2, embargo_bars=5)
        self.assertEqual(len(train), 144)
        self.assertEqual(len(valid), 48)
        # 240 - 144 - 5 - 48 - 5 = 38 test bars
        self.assertEqual(len(test), 38)
        assert_no_time_overlap(train, valid)
        assert_no_time_overlap(valid, test)
        # Embargo gap: 6 intervals of 5m between train end and valid start.
        gap = (valid[0].timestamp - train[-1].timestamp).total_seconds() / 60
        self.assertAlmostEqual(gap, 30.0)

    def test_temporal_split_rejects_bad_ratios(self):
        candles = make_candles([100.0] * 30, prefix="ratio")
        with self.assertRaises(ValueError):
            temporal_split(candles, 0.7, 0.4)
        with self.assertRaises(ValueError):
            temporal_split(candles[:5], 0.6, 0.2)

    def test_walk_forward_ordered_with_embargo(self):
        candles = make_candles([100.0 + i * 0.1 for i in range(120)], prefix="wf")
        folds = walk_forward_splits(candles, n_splits=3, train_bars=30,
                                    test_bars=10, embargo_bars=5)
        self.assertEqual(len(folds), 3)
        for k, (train, test) in enumerate(folds):
            self.assertEqual(len(train), 30)
            self.assertEqual(len(test), 10)
            assert_no_time_overlap(train, test)
            gap = (test[0].timestamp - train[-1].timestamp).total_seconds() / 60
            self.assertAlmostEqual(gap, 30.0)  # (embargo+1)*5m
        # Tests are sequential and disjoint.
        for a in range(3):
            for b in range(a + 1, 3):
                assert_no_time_overlap(folds[a][1], folds[b][1])
                self.assertLess(folds[a][1][-1].timestamp, folds[b][1][0].timestamp)

    def test_walk_forward_rejects_short_history(self):
        candles = make_candles([100.0] * 20, prefix="short")
        with self.assertRaises(ValueError):
            walk_forward_splits(candles, n_splits=3, train_bars=30,
                                test_bars=10, embargo_bars=5)


class TestMetrics(unittest.TestCase):
    def test_known_pnls(self):
        from backtest import BacktestResult
        r = BacktestResult(trade_pnls=[10.0, -5.0, 20.0, -5.0], realized_pnl=20.0,
                           max_drawdown=0.05, turnover=1000.0, fees_paid=4.0,
                           final_equity=10020.0, exposures=[0.1, 0.2],
                           equity_curve=[10000, 10010, 10005, 10025, 10020])
        m = compute_report_metrics(r, 10000.0)
        self.assertAlmostEqual(m['gross_win'], 30.0)
        self.assertAlmostEqual(m['gross_loss'], -10.0)
        self.assertAlmostEqual(m['profit_factor'], 3.0)
        self.assertAlmostEqual(m['expectancy'], 5.0)
        self.assertAlmostEqual(m['avg_win'], 15.0)
        self.assertAlmostEqual(m['avg_loss'], -5.0)
        self.assertEqual(m['max_consecutive_wins'], 1)
        self.assertEqual(m['max_consecutive_losses'], 1)
        self.assertAlmostEqual(m['win_rate'], 0.5)
        self.assertAlmostEqual(m['turnover_ratio'], 0.1)
        self.assertGreater(m['sharpe_5m_annualized'], -1000)  # finite

    def test_profit_factor_edge_cases(self):
        from backtest import BacktestResult
        m = compute_report_metrics(BacktestResult(trade_pnls=[5.0, 5.0], realized_pnl=10.0), 10000.0)
        self.assertEqual(m['profit_factor'], float('inf'))
        m = compute_report_metrics(BacktestResult(trade_pnls=[], realized_pnl=0.0), 10000.0)
        self.assertEqual(m['profit_factor'], 0.0)
        self.assertEqual(m['expectancy'], 0.0)
        self.assertEqual(m['win_rate'], 0.0)

    def test_sharpe_flat_is_zero(self):
        self.assertEqual(sharpe_from_equity([10000.0] * 10), 0.0)
        self.assertEqual(sharpe_from_equity([10000.0]), 0.0)

    def test_sharpe_uses_actual_timeframe(self):
        # B08 regression: hardcoded 5m annualization overstated 1h/4h Sharpe
        # by sqrt(12)/sqrt(48). Same returns, different grids.
        from evaluation import sharpe_annualized, bars_per_year_from_stamps
        from datetime import datetime, timezone, timedelta
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        curve = [10000.0 + i * 3.0 + (10.0 if i % 2 else -4.0) for i in range(40)]
        stamps_5m = [(base + timedelta(minutes=5 * i)).isoformat() for i in range(40)]
        stamps_1h = [(base + timedelta(hours=i)).isoformat() for i in range(40)]
        stamps_4h = [(base + timedelta(hours=4 * i)).isoformat() for i in range(40)]
        s5 = sharpe_annualized(curve, equity_stamps=stamps_5m)
        s1 = sharpe_annualized(curve, equity_stamps=stamps_1h)
        s4 = sharpe_annualized(curve, equity_stamps=stamps_4h)
        import math
        self.assertAlmostEqual(s5 / s1, math.sqrt(12.0), places=6)
        self.assertAlmostEqual(s1 / s4, math.sqrt(4.0), places=6)
        # Legacy default (no stamps) is still the 5-minute grid.
        self.assertAlmostEqual(sharpe_annualized(curve), s5, places=9)

    def test_sharpe_ignores_gaps_uses_median(self):
        from evaluation import sharpe_annualized, bars_per_year_from_stamps
        from datetime import datetime, timezone, timedelta
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # Hourly grid with one 30-day gap in the middle.
        stamps = [(base + timedelta(hours=i)).isoformat() for i in range(10)]
        stamps += [(base + timedelta(hours=10 + 24 * 30 + i)).isoformat()
                   for i in range(10)]
        self.assertAlmostEqual(bars_per_year_from_stamps(stamps), 8760.0)
        curve = [10000.0 + i for i in range(20)]
        self.assertAlmostEqual(
            sharpe_annualized(curve, equity_stamps=stamps),
            sharpe_annualized(curve, bars_per_year=8760.0), places=9)

    def test_sharpe_undefined_inputs_fall_back(self):
        from evaluation import sharpe_annualized, bars_per_year_from_stamps
        self.assertIsNone(bars_per_year_from_stamps([]))
        self.assertIsNone(bars_per_year_from_stamps(["not-a-date"]))
        self.assertEqual(sharpe_annualized([1.0, 2.0]), 0.0)  # too short
        self.assertEqual(sharpe_annualized([100.0] * 10, equity_stamps=[]), 0.0)


class TestWalkForwardRunner(unittest.TestCase):
    def test_fresh_state_per_fold(self):
        candles = make_candles([100.0 + i * 0.5 for i in range(120)], prefix="fresh")
        calls = []

        def factory():
            # NOTE: no I/O here — run_walk_forward connects the exchange itself.
            # Building without awaiting keeps the factory usable inside a running loop.
            cfg = ConfigManager()
            cfg.set('simulated_latency_ms', 0)
            cfg.set('min_volume', 100.0)
            cfg.set('volatility_max', 1.0)
            cfg.set('initial_cash', 10000.0)
            cfg.set('stop_loss_pct', 10.0)
            cfg.set('take_profit_pct', 10.0)
            md = MarketDataHandler(cfg)
            ex = PaperExchange(cfg, seed=11)
            risk = RiskManager(cfg)
            pf = PortfolioManager(cfg)
            eng = ExecutionEngine(cfg, ex, risk)
            strat = StrategyManager(cfg)
            bt = BacktestEngine(cfg, md, eng, pf, risk, strat)
            calls.append((pf, bt))
            return cfg, md, ex, risk, pf, eng, strat, bt

        out = run(run_walk_forward('BTC/USDT', candles, factory, n_splits=2,
                                   train_bars=40, test_bars=20, embargo_bars=5))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(out['folds']), 2)
        # Portfolios are distinct objects (no shared state).
        self.assertIsNot(calls[0][0], calls[1][0])
        for f in out['folds']:
            self.assertEqual(f['test_bars'], 20)
            self.assertIn('net_pnl', f)
        self.assertIn('net_pnl', out['aggregate'])

    def test_end_to_end_csv_split_backtest(self):
        candles, _ = load_ohlcv_csv('data/sample_ohlcv.csv')
        train, valid, test = temporal_split(candles, 0.6, 0.2, embargo_bars=5)
        _, _, ex, _, pf, eng, strat, bt = make_stack()
        res = run(bt.run('BTC/USDT', test))
        self.assertEqual(res.n_bars, len(test))
        m = compute_report_metrics(res, 10000.0)
        for key in ('net_pnl', 'profit_factor', 'expectancy', 'max_drawdown',
                    'sharpe_5m_annualized', 'avg_win', 'avg_loss',
                    'exposure_avg', 'max_consecutive_wins',
                    'max_consecutive_losses', 'turnover'):
            self.assertIn(key, m)
        run(ex.disconnect())


class TestWarmup(unittest.TestCase):
    def _trend(self, n, prefix, start=0.0):
        return make_candles([100.0 + start + i for i in range(n)], prefix=prefix)

    def test_short_test_trades_only_with_warmup(self):
        # 20-bar test is below the ~22-bar indicator need: cold scores nothing.
        warmup = self._trend(30, "wu", start=0.0)
        test = self._trend(20, "wt", start=30.0)
        # Shift test timestamps after warmup.
        base = warmup[-1].timestamp + timedelta(minutes=5)
        for i, c in enumerate(test):
            c.timestamp = base + timedelta(minutes=5 * i)
            c.event_id = f"wt{i}"

        _, _, _, _, _, _, _, bt_cold = make_stack()
        r_cold = run(bt_cold.run('BTC/USDT', list(test)))
        self.assertEqual(r_cold.trades, 0)

        _, _, _, _, _, _, _, bt_warm = make_stack()
        r_warm = run(bt_warm.run('BTC/USDT', list(test),
                                 warmup_candles=list(warmup)))
        self.assertEqual(r_warm.warmup_bars, 30)
        self.assertEqual(r_warm.n_bars, len(test))
        self.assertGreater(r_warm.trades, 0)

    def test_warmup_context_never_scored(self):
        # Same full history run two ways: (a) warmup10+test8 scored,
        # (b) all 18 bars scored. Fills on the shared 8 bars must match
        # exactly; warmup-only bars add zero scored fills.
        prices = [100.0 + i for i in range(18)]
        all_bars = make_candles(prices, prefix="wctx")
        warmup, test = all_bars[:10], all_bars[10:]

        _, _, _, _, _, _, _, bt_a = make_stack(spread_pct=0.0, slippage_pct=0.0,
                                               maker_fee=0.0, taker_fee=0.0,
                                               stop_loss_pct=10.0, take_profit_pct=10.0)
        probe_a = ProbeLong()
        bt_a.strategy_manager = probe_a  # type: ignore
        r_a = run(bt_a.run('BTC/USDT', list(test),
                           warmup_candles=list(warmup)))

        _, _, _, _, _, _, _, bt_b = make_stack(spread_pct=0.0, slippage_pct=0.0,
                                               maker_fee=0.0, taker_fee=0.0,
                                               stop_loss_pct=10.0, take_profit_pct=10.0)
        probe_b = ProbeLong()
        bt_b.strategy_manager = probe_b  # type: ignore
        r_b = run(bt_b.run('BTC/USDT', list(all_bars)))

        fills_a = [o.avg_fill_price for o, _ in bt_a.execution_engine.get_execution_history()]
        fills_b = [o.avg_fill_price for o, _ in bt_b.execution_engine.get_execution_history()]
        # (b) scores bars 5..17, (a) only bars 10..17: shared suffix identical.
        self.assertEqual(fills_a, fills_b[-len(fills_a):])
        self.assertEqual(r_a.n_bars, 8)
        self.assertEqual(r_b.n_bars, 18)

    def test_warmup_after_test_raises(self):
        test = self._trend(20, "t")
        warmup = self._trend(10, "w")
        base = test[-1].timestamp + timedelta(minutes=5)
        for i, c in enumerate(warmup):
            c.timestamp = base + timedelta(minutes=5 * i)
            c.event_id = f"w{i}"
        _, _, _, _, _, _, _, bt = make_stack()
        with self.assertRaises(ValueError):
            run(bt.run('BTC/USDT', list(test), warmup_candles=list(warmup)))

    def test_warmup_sharing_ids_raises(self):
        bars = self._trend(20, "dup")
        _, _, _, _, _, _, _, bt = make_stack()
        with self.assertRaises(ValueError):
            # Overlapping slices share event_ids AND timestamps.
            run(bt.run('BTC/USDT', list(bars[10:]),
                       warmup_candles=list(bars[5:15])))

    def test_walk_forward_warmup_exceeding_train_raises(self):
        candles = make_candles([100.0] * 100, prefix="wex")
        with self.assertRaises(ValueError):
            walk_forward_folds(candles, n_splits=2, train_bars=30,
                               test_bars=20, embargo_bars=5, warmup_bars=31)

    def test_walk_forward_short_tests_trade_with_warmup(self):
        candles = make_candles([100.0 + i * 0.5 for i in range(150)], prefix="wwf")

        def factory():
            cfg = ConfigManager()
            cfg.set('simulated_latency_ms', 0)
            cfg.set('min_volume', 100.0)
            cfg.set('volatility_max', 1.0)
            cfg.set('initial_cash', 10000.0)
            md = MarketDataHandler(cfg)
            ex = PaperExchange(cfg, seed=11)
            risk = RiskManager(cfg)
            pf = PortfolioManager(cfg)
            eng = ExecutionEngine(cfg, ex, risk)
            strat = StrategyManager(cfg)
            bt = BacktestEngine(cfg, md, eng, pf, risk, strat)
            return cfg, md, ex, risk, pf, eng, strat, bt

        out = run(run_walk_forward('BTC/USDT', candles, factory, n_splits=2,
                                   train_bars=40, test_bars=20, embargo_bars=5,
                                   warmup_bars=30))
        total_fills = sum(f['fills'] for f in out['folds'])
        self.assertGreater(total_fills, 0)
        for f in out['folds']:
            self.assertEqual(f['warmup_bars'], 30)


class TestDataset(unittest.TestCase):
    def test_multi_file_merge_and_cross_duplicates(self):
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        paths = []
        try:
            for name, day, extra in (("a.csv", 1, False), ("b.csv", 11, True)):
                p = os.path.join(tempfile.gettempdir(), f"ds_{name}")
                with open(p, 'w', encoding='utf-8', newline='') as f:
                    w = csv.writer(f)
                    w.writerow(['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    for i in range(10):
                        ts = base + timedelta(days=day - 1, minutes=5 * i)
                        px = 100 + i
                        w.writerow([ts.isoformat(), px, px + 0.1, px - 0.1, px, 1000])
                    if extra:
                        # Duplicate of a.csv's first bar (cross-file overlap).
                        ts = base + timedelta(days=0, minutes=0)
                        w.writerow([ts.isoformat(), 100, 100.1, 99.9, 100, 1000])
                paths.append(p)
            candles, stats = load_ohlcv_dataset(paths)
            self.assertEqual(stats['files'], 2)
            self.assertEqual(stats['loaded'], 20)
            self.assertEqual(stats['cross_file_duplicates'], 1)
            ts = [c.timestamp for c in candles]
            self.assertEqual(ts, sorted(ts))
        finally:
            for p in paths:
                if os.path.exists(p):
                    os.unlink(p)

    def test_no_match_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_ohlcv_dataset(['data/does_not_exist_*.csv'])

    def test_sample_dataset_two_periods(self):
        candles, stats = load_ohlcv_dataset(
            ['data/sample_ohlcv.csv', 'data/sample_ohlcv_2024-02.csv'])
        self.assertEqual(stats['files'], 2)
        self.assertEqual(len(candles), 240 + 1600)
        ts = [c.timestamp for c in candles]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual(len(set(ts)), len(ts))


class TestFoldIsolation(unittest.TestCase):
    def test_verify_ok(self):
        candles = make_candles([100.0] * 60, prefix="iso")
        train, warmup, test = candles[:40], candles[30:40], candles[45:]
        info = verify_fold_isolation(train, warmup, test, embargo_bars=5)
        self.assertEqual(info['warmup_bars'], 10)
        self.assertGreaterEqual(info['gap_bars'], 5)

    def test_overlap_raises(self):
        candles = make_candles([100.0] * 30, prefix="ov")
        with self.assertRaises(AssertionError):
            verify_fold_isolation(candles[:20], [], candles[10:20], 0)

    def test_warmup_after_test_raises(self):
        candles = make_candles([100.0] * 40, prefix="waf")
        with self.assertRaises(AssertionError):
            verify_fold_isolation(candles[:20], candles[25:30], candles[20:25], 0)

    def test_embargo_violation_raises(self):
        candles = make_candles([100.0] * 40, prefix="emb")
        with self.assertRaises(AssertionError):
            verify_fold_isolation(candles[:20], [], candles[21:30], embargo_bars=5)


class TestRegimes(unittest.TestCase):
    def test_summarize_regimes_disjoint_with_metrics(self):
        candles = make_candles([100.0 + i * 0.2 for i in range(120)], prefix="reg")

        def factory():
            cfg = ConfigManager()
            cfg.set('simulated_latency_ms', 0)
            cfg.set('min_volume', 100.0)
            cfg.set('volatility_max', 1.0)
            cfg.set('initial_cash', 10000.0)
            md = MarketDataHandler(cfg)
            ex = PaperExchange(cfg, seed=11)
            risk = RiskManager(cfg)
            pf = PortfolioManager(cfg)
            eng = ExecutionEngine(cfg, ex, risk)
            strat = StrategyManager(cfg)
            bt = BacktestEngine(cfg, md, eng, pf, risk, strat)
            return cfg, md, ex, risk, pf, eng, strat, bt

        out = run(summarize_regimes('BTC/USDT', candles, factory,
                                    train_ratio=0.5, validation_ratio=0.25,
                                    embargo_bars=5, warmup_bars=10))
        regs = out['regimes']
        self.assertEqual(set(regs), {'train', 'validation', 'test'})
        starts = [(n, r['start'], r['end']) for n, r in regs.items()]
        # Disjoint and ordered: train < validation < test.
        self.assertLess(regs['train']['end'], regs['validation']['start'])
        self.assertLess(regs['validation']['end'], regs['test']['start'])
        for n, r in regs.items():
            for key in ('net_pnl', 'profit_factor', 'sharpe_5m_annualized',
                        'max_drawdown', 'exposure_avg', 'turnover'):
                self.assertIn(key, r, f"{n} missing {key}")
        # Warmup comes from preceding history only: train has none.
        self.assertEqual(regs['train']['warmup_bars'], 0)
        self.assertEqual(regs['validation']['warmup_bars'], 10)
        self.assertEqual(regs['test']['warmup_bars'], 10)


class TestCostSweep(unittest.TestCase):
    def _factory(self, costs):
        cfg = ConfigManager()
        cfg.set('simulated_latency_ms', 0)
        cfg.set('min_volume', 100.0)
        cfg.set('volatility_max', 1.0)
        cfg.set('initial_cash', 10000.0)
        cfg.set('stop_loss_pct', 10.0)
        cfg.set('take_profit_pct', 10.0)
        for k, v in costs.items():
            cfg.set(k, v)
        md = MarketDataHandler(cfg)
        ex = PaperExchange(cfg, seed=11)
        risk = RiskManager(cfg)
        pf = PortfolioManager(cfg)
        eng = ExecutionEngine(cfg, ex, risk)
        strat = StrategyManager(cfg)
        bt = BacktestEngine(cfg, md, eng, pf, risk, strat)
        return cfg, md, ex, risk, pf, eng, strat, bt

    def _scenarios(self):
        base = {'taker_fee': 0.002, 'maker_fee': 0.001,
                'spread_pct': 0.0005, 'slippage_pct': 0.0005}
        out = [{'name': 'base', **base}]
        for name, m in (('x2', 2), ('x5', 5)):
            out.append({'name': name, **{k: v * m for k, v in base.items()}})
        return out

    def test_sweep_deterministic_and_fee_ordered(self):
        candles = make_candles([100.0 + i for i in range(36)], prefix="cost")
        scenarios = self._scenarios()
        r1 = run(run_cost_sweep('BTC/USDT', candles, self._factory, scenarios))
        r2 = run(run_cost_sweep('BTC/USDT', candles, self._factory, scenarios))
        self.assertEqual([s['scenario'] for s in r1['scenarios']], ['base', 'x2', 'x5'])
        for a, b in zip(r1['scenarios'], r2['scenarios']):
            self.assertEqual(a['fills'], b['fills'])
            self.assertAlmostEqual(a['net_pnl'], b['net_pnl'], places=9)
            self.assertAlmostEqual(a['fees_paid'], b['fees_paid'], places=9)
        fees = [s['fees_paid'] for s in r1['scenarios']]
        self.assertLess(fees[0], fees[1])
        self.assertLess(fees[1], fees[2])
        fills = {s['fills'] for s in r1['scenarios']}
        self.assertEqual(len(fills), 1)  # identical trajectory, costs only


class TestModelComparison(unittest.TestCase):
    def test_compare_models_deterministic(self):
        candles = make_candles([100.0 + i for i in range(40)],
                               opens=[102.0 + i for i in range(40)], prefix="cmp")

        def factory():
            cfg = ConfigManager()
            cfg.set('simulated_latency_ms', 0)
            cfg.set('min_volume', 100.0)
            cfg.set('volatility_max', 1.0)
            cfg.set('initial_cash', 10000.0)
            cfg.set('stop_loss_pct', 10.0)
            cfg.set('take_profit_pct', 10.0)
            md = MarketDataHandler(cfg)
            ex = PaperExchange(cfg, seed=11)
            risk = RiskManager(cfg)
            pf = PortfolioManager(cfg)
            eng = ExecutionEngine(cfg, ex, risk)
            strat = StrategyManager(cfg)
            bt = BacktestEngine(cfg, md, eng, pf, risk, strat)
            return cfg, md, ex, risk, pf, eng, strat, bt

        r1 = run(compare_execution_models('BTC/USDT', candles, factory))
        r2 = run(compare_execution_models('BTC/USDT', candles, factory))
        self.assertEqual(set(r1['models']), {'next_open', 'next_close'})
        for m in ('next_open', 'next_close'):
            self.assertEqual(r1['models'][m]['execution_model'], m)
            self.assertAlmostEqual(r1['models'][m]['net_pnl'],
                                   r2['models'][m]['net_pnl'], places=9)


class TestExport(unittest.TestCase):
    def test_json_csv_roundtrip(self):
        rows = [
            {'scope': 'walk_forward', 'fold_or_regime': 0, 'execution_model': 'next_close',
             'n_bars': 40, 'warmup_bars': 30, 'fills': 5, 'n_closed': 4,
             'wins': 3, 'losses': 1, 'win_rate': 0.75, 'net_pnl': 12.5,
             'profit_factor': float('inf'), 'expectancy': 3.125,
             'avg_win': 5.0, 'avg_loss': -2.5, 'max_drawdown': 0.01,
             'sharpe_5m_annualized': 1.5, 'exposure_avg': 0.1, 'exposure_max': 0.2,
             'turnover': 1000.0, 'turnover_ratio': 0.1, 'fees_paid': 2.0,
             'final_equity': 10012.5, 'max_consecutive_wins': 2,
             'max_consecutive_losses': 1, 'blocked_by_risk': 0,
             'rejected_orders': 0, 'start': '2024-01-01', 'end': '2024-01-02'},
        ]
        with tempfile.TemporaryDirectory() as d:
            jp = os.path.join(d, 'report.json')
            cp = os.path.join(d, 'folds.csv')
            export_report_json(jp, {'folds': rows, 'aggregate': {'net_pnl': 12.5}})
            export_folds_csv(cp, rows)
            import json
            with open(jp, encoding='utf-8') as f:
                back = json.load(f)
            self.assertEqual(back['aggregate']['net_pnl'], 12.5)
            self.assertEqual(back['folds'][0]['profit_factor'], 'inf')
            with open(cp, encoding='utf-8', newline='') as f:
                rd = list(csv.DictReader(f))
            self.assertEqual(len(rd), 1)
            self.assertEqual(rd[0]['scope'], 'walk_forward')
            self.assertAlmostEqual(float(rd[0]['net_pnl']), 12.5)


if __name__ == '__main__':
    unittest.main()
