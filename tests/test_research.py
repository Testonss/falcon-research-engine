"""Falcon research-engine tests: generation, seals, gates, scoring, regimes,
stability, persistence, paper idempotency, provenance, campaigns, CLI.

Small synthetic data only; strategy params are fixed (no tuning).
"""
import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from research import strategies as fam
from research.generate import random_search, constrained_grid, perturb, make_candidate
from research.candidates import CandidateRegistry, CandidateStatus
from research.datasets import build_research_dataset, FINAL_MODE
from research.gates import (GateConfig, gate_validation, gate_walk_forward,
                            gate_cross_market)
from research.scoring import (research_score, SCORE_WEIGHTS,
                              cross_market_score)
from research.regimes import (label_series, attribute_trades,
                              regime_concentration, regime_report_table,
                              regime_drawdowns)
from research.stability import neighbourhood_report
from research.store import ResearchStore
from research.campaign import CampaignConfig, run_campaign, campaign_symbols
from research.report import build_campaign_report, terminal_summary
from research.mtesting import expectancy_lcb, snooping_report
from research.datafiles import (validate_klines, build_versioned_dataset,
                                load_versioned_dataset, resample_ohlcv)
from research.interfaces import (provides_market_data, provides_execution,
                                 TestnetAdapterStub)


def run(coro):
    return asyncio.run(coro)


def tiny_bars(n=120, start=100.0, step=0.3, prefix="t", volume=5000.0):
    from market_data import Candle
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [Candle(symbol='BTC/USDT', open=start + i * step,
                   high=start + i * step + 0.2, low=start + i * step - 0.2,
                   close=start + i * step + 0.05, volume=volume,
                   timestamp=base + timedelta(minutes=5 * i), interval='5m',
                   event_id=f'{prefix}{i}') for i in range(n)]


def metrics(exp=1.0, pf=1.5, dd=0.05, n=20, wr=0.6, sharpe=1.0):
    return {'expectancy': exp, 'profit_factor': pf, 'max_drawdown': dd,
            'n_closed': n, 'win_rate': wr, 'sharpe_5m_annualized': sharpe,
            'gross_win': 100.0, 'gross_loss': -50.0, 'max_participation': 0.01}


class TestGeneration(unittest.TestCase):
    def test_deterministic(self):
        a = random_search(['momentum', 'breakout'], 8, seed=11)
        b = random_search(['momentum', 'breakout'], 8, seed=11)
        self.assertEqual([c.candidate_id for c in a], [c.candidate_id for c in b])
        self.assertEqual([c.params for c in a], [c.params for c in b])

    def test_seed_changes_output(self):
        a = random_search(['momentum'], 4, seed=1)
        b = random_search(['momentum'], 4, seed=2)
        self.assertNotEqual([c.candidate_id for c in a], [c.candidate_id for c in b])

    def test_grid_respects_limits_and_cap(self):
        grid = constrained_grid('momentum', seed=5,
                                limits={'momentum_period': [7, 14]}, max_candidates=3)
        self.assertEqual(len(grid), 3)
        for c in grid:
            self.assertIn(c.params['momentum_period'], [7, 14])

    def test_grid_rejects_out_of_space(self):
        with self.assertRaises(ValueError):
            constrained_grid('momentum', seed=5, limits={'momentum_period': [999]})
        with self.assertRaises(ValueError):
            constrained_grid('nope', seed=5)

    def test_perturb_adjacent_and_parent(self):
        c = make_candidate('momentum', {'momentum_period': 14, 'momentum_threshold': 0.01},
                           seed=3)
        nbs = perturb(c, seed=4, n=6)
        self.assertGreater(len(nbs), 0)
        for nb in nbs:
            self.assertEqual(nb.parent_id, c.candidate_id)
            diffs = [k for k in nb.params if nb.params[k] != c.params[k]]
            self.assertEqual(len(diffs), 1)
            space = fam.param_space('momentum')[diffs[0]]
            self.assertLessEqual(abs(space.index(nb.params[diffs[0]])
                                     - space.index(c.params[diffs[0]])), 1)

    def test_build_rejects_unknown(self):
        with self.assertRaises(ValueError):
            fam.build_strategy('nope', {})
        with self.assertRaises(ValueError):
            fam.build_strategy('momentum', {'momentum_period': 999,
                                            'momentum_threshold': 0.01})

    def test_spaces_inspectable(self):
        for f in fam.family_names():
            space = fam.param_space(f)
            self.assertGreater(len(space), 0)
            for vals in space.values():
                self.assertEqual(sorted(vals), vals)


class TestRegistry(unittest.TestCase):
    def test_duplicate_returns_existing(self):
        reg = CandidateRegistry()
        a = make_candidate('breakout', {'lookback': 20}, seed=1)
        b = make_candidate('breakout', {'lookback': 20}, seed=1)
        self.assertIs(reg.register(a), a)
        self.assertIs(reg.register(b), a)
        self.assertEqual(len(reg), 1)

    def test_survivors_rejected_filters(self):
        reg = CandidateRegistry()
        a = make_candidate('breakout', {'lookback': 20}, seed=1)
        b = make_candidate('breakout', {'lookback': 10}, seed=1)
        reg.register(a)
        reg.register(b)
        b.record_evaluation('validated', {}, False, 'negative_expectancy:x')
        self.assertEqual(reg.survivors(), [a])
        self.assertEqual(reg.rejected(), [b])
        self.assertTrue(b.is_rejected)


class TestSealedHoldout(unittest.TestCase):
    def test_wrong_mode_denied(self):
        ds = build_research_dataset(tiny_bars(60), 'BTC/USDT', holdout_ratio=0.2)
        with self.assertRaises(PermissionError):
            ds.holdout.open_holdout('research', 'camp', 'sneaky')
        with self.assertRaises(ValueError):
            ds.holdout.open_holdout(FINAL_MODE, 'camp', '')

    def test_final_mode_logs_once(self):
        ds = build_research_dataset(tiny_bars(60), 'BTC/USDT', holdout_ratio=0.2)
        hold = ds.holdout.open_holdout(FINAL_MODE, 'camp1', 'final check')
        self.assertEqual(len(hold), len(ds.holdout))
        self.assertEqual(len(ds.holdout.access_log), 1)
        self.assertEqual(ds.holdout.access_log[0].campaign_id, 'camp1')

    def test_research_inputs_reject_holdout_bars(self):
        ds = build_research_dataset(tiny_bars(60), 'BTC/USDT', holdout_ratio=0.2)
        hold = ds.holdout.open_holdout(FINAL_MODE, 'camp1', 'test')
        with self.assertRaises(AssertionError):
            ds.assert_not_holdout(hold, 'sneaky-input')
        ds.assert_not_holdout(ds.research_bars, 'clean-input')


class TestGates(unittest.TestCase):
    def test_reject_negative_expectancy(self):
        c = make_candidate('momentum', {'momentum_period': 14, 'momentum_threshold': 0.01}, 1)
        ok, reason = gate_validation(c, metrics(exp=1.0), metrics(exp=-1.0), GateConfig())
        self.assertFalse(ok)
        self.assertTrue(reason.startswith('negative_expectancy'))

    def test_reject_poor_pf_and_few_trades(self):
        c = make_candidate('momentum', {'momentum_period': 14, 'momentum_threshold': 0.01}, 1)
        ok, _ = gate_validation(c, metrics(), metrics(pf=0.8), GateConfig())
        self.assertFalse(ok)
        ok, reason = gate_validation(c, metrics(), metrics(n=2), GateConfig())
        self.assertFalse(ok)
        self.assertTrue(reason.startswith('insufficient_trades'))

    def test_reject_train_valid_gap_and_drawdown(self):
        c = make_candidate('momentum', {'momentum_period': 14, 'momentum_threshold': 0.01}, 1)
        ok, reason = gate_validation(c, metrics(exp=10.0), metrics(exp=1.0), GateConfig())
        self.assertFalse(ok)
        self.assertTrue(reason.startswith('train_validation_gap'))
        ok, _ = gate_validation(c, metrics(), metrics(dd=0.9), GateConfig())
        self.assertFalse(ok)

    def test_reject_unrealistic(self):
        c = make_candidate('momentum', {'momentum_period': 14, 'momentum_threshold': 0.01}, 1)
        ok, reason = gate_validation(c, metrics(), metrics(sharpe=99.0), GateConfig())
        self.assertFalse(ok)
        self.assertTrue(reason.startswith('unrealistic_performance'))

    def test_reject_unstable_walk_forward(self):
        c = make_candidate('momentum', {'momentum_period': 14, 'momentum_threshold': 0.01}, 1)
        folds = [metrics(exp=-1.0), metrics(exp=-2.0)]
        ok, reason = gate_walk_forward(c, folds, GateConfig())
        self.assertFalse(ok)
        self.assertIn('unstable_walk_forward', reason)

    def test_pass_sane(self):
        c = make_candidate('momentum', {'momentum_period': 14, 'momentum_threshold': 0.01}, 1)
        ok, reason = gate_validation(c, metrics(exp=1.0), metrics(exp=0.8), GateConfig())
        self.assertTrue(ok, reason)


class TestScoring(unittest.TestCase):
    def test_components_visible_and_weighted(self):
        self.assertAlmostEqual(sum(SCORE_WEIGHTS.values()), 1.0, places=9)
        score, comps = research_score(
            metrics(exp=2.0, pf=2.0), metrics(exp=2.2), [{'expectancy': 1.0}],
            1.0, 100.0, 80.0, 0.3, 50000.0, 3)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)
        self.assertEqual(set(comps.keys()), set(SCORE_WEIGHTS.keys()))

    def test_better_candidate_scores_higher(self):
        kw = dict(train_m=metrics(exp=1.0), wf_folds=[{'expectancy': 1.0}],
                  stability_rate=1.0, base_cost_net=100.0, stressed_net=80.0,
                  regime_concentration=0.3, survivable_capital=50000.0, complexity=3)
        good, _ = research_score(metrics(exp=2.0, pf=2.0), **kw)
        bad, _ = research_score(metrics(exp=0.1, pf=1.05), **kw)
        self.assertGreater(good, bad)


class TestRegimes(unittest.TestCase):
    def test_labels_trailing_only(self):
        bars = tiny_bars(60)
        full = label_series(bars)
        trunc = label_series(bars[:30])
        self.assertEqual([l['trend'] for l in trunc],
                         [l['trend'] for l in full[:30]])
        self.assertEqual([l['vol'] for l in trunc],
                         [l['vol'] for l in full[:30]])

    def test_attribution_sums_to_total(self):
        from backtest import BacktestResult
        bars = tiny_bars(40)
        res = BacktestResult(trade_pnls=[5.0, -2.0, 3.0],
                             trade_exit_stamps=[b.timestamp.isoformat() for b in bars[10:13]])
        from research.regimes import attribute_trades
        attr = attribute_trades(res, bars)
        tot = sum(v['pnl'] for k, v in attr.items()
                  if k in ('trending_up', 'trending_down', 'sideways'))
        self.assertAlmostEqual(tot, 6.0, places=9)
        from research.regimes import regime_concentration
        conc = regime_concentration(attr)
        self.assertGreaterEqual(conc, 0.0)
        self.assertLessEqual(conc, 1.0)


class TestStability(unittest.TestCase):
    def test_narrow_peak_gets_low_pass_rate(self):
        c = make_candidate('momentum', {'momentum_period': 14, 'momentum_threshold': 0.01},
                           seed=5)

        async def eval_fn(params):
            good = params == {'momentum_period': 14, 'momentum_threshold': 0.01}
            return {'expectancy': 1.0 if good else -1.0, 'n_closed': 20}

        rep = run(neighbourhood_report(c, eval_fn, seed=6, n=6))
        self.assertLess(rep['pass_rate'], 0.5)
        self.assertEqual(rep['candidate_id'], c.candidate_id)


class TestPersistence(unittest.TestCase):
    def test_roundtrip_and_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'falcon.db')
            store = ResearchStore(path)
            c = make_candidate('breakout', {'lookback': 20}, seed=1)
            store.save_candidate(c)
            store.save_evaluation(c.candidate_id, 'validated', True, '',
                                  {'expectancy': 1.0})
            store.save_campaign({'campaign_id': 'camp1', 'funnel': {}})
            store.save_paper_session({'paper_id': 'p1', 'fills': [1, 2]})
            self.assertTrue(store.claim_idempotency('k1', 'o1'))
            store.close()
            # Simulate process restart: reopen the same file.
            store2 = ResearchStore(path)
            try:
                back = store2.load_candidate(c.candidate_id)
                self.assertEqual(back['family'], 'breakout')
                self.assertEqual(len(store2.evaluations_for(c.candidate_id)), 1)
                self.assertEqual(store2.load_campaign('camp1')['campaign_id'], 'camp1')
                self.assertEqual(store2.load_paper_session('p1')['fills'], [1, 2])
                self.assertFalse(store2.claim_idempotency('k1', 'o1'))
                self.assertEqual(store2.lookup_idempotency('k1'), 'o1')
            finally:
                store2.close()

    def test_rejections_preserved(self):
        store = ResearchStore(":memory:")
        c = make_candidate('breakout', {'lookback': 20}, seed=1)
        c.record_evaluation('validated', {'expectancy': -1.0}, False,
                            'negative_expectancy:x')
        store.save_candidate(c)
        back = store.load_candidate(c.candidate_id)
        self.assertEqual(back['rejection_reason'], 'negative_expectancy:x')
        self.assertEqual(back['status'], 'rejected')
        store.close()


class TestPaperIdempotency(unittest.TestCase):
    def _stack(self, costs=None):
        from config import ConfigManager
        from market_data import MarketDataHandler
        from exchange import PaperExchange
        from execution import ExecutionEngine
        from risk import RiskManager
        from portfolio import PortfolioManager
        from backtest import BacktestEngine
        from strategy import StrategyManager
        cfg = ConfigManager()
        cfg.set('simulated_latency_ms', 0)
        cfg.set('trade_interval_seconds', 0)
        for k, v in (costs or {}).items():
            cfg.set(k, v)
        md = MarketDataHandler(cfg)
        ex = PaperExchange(cfg, seed=11)
        risk = RiskManager(cfg)
        pf = PortfolioManager(cfg)
        eng = ExecutionEngine(cfg, ex, risk)
        strat = StrategyManager(cfg)
        bt = BacktestEngine(cfg, md, eng, pf, risk, strat)
        return cfg, md, ex, risk, pf, eng, strat, bt

    def test_divergence_reported_and_session_persisted(self):
        from research.paper import paper_with_divergence
        bars = tiny_bars(70, step=0.5)
        c = make_candidate('momentum', {'momentum_period': 7, 'momentum_threshold': 0.005},
                           seed=2)
        with tempfile.TemporaryDirectory() as d:
            store = ResearchStore(os.path.join(d, 'f.db'))
            try:
                sess = run(paper_with_divergence(c, bars, self._stack, 'BTC/USDT', store))
                self.assertIn('divergence', sess)
                self.assertIn('backtest_net', sess)
                self.assertGreater(sess['n_fills'], 0)
                back = store.load_paper_session(sess['paper_id'])
                self.assertEqual(back['paper_net'], sess['paper_net'])
                self.assertEqual(len(back['fills']), sess['n_fills'])
            finally:
                store.close()

    def test_restart_reconstructs_state(self):
        # B07 regression: a restart must NOT reset equity or overwrite results.
        # The completed checkpoint is returned as-is: identical fills, net,
        # equity and no new orders.
        from research.paper import run_paper_replay
        bars = tiny_bars(70, step=0.5)
        c = make_candidate('momentum', {'momentum_period': 7, 'momentum_threshold': 0.005},
                           seed=2)
        with tempfile.TemporaryDirectory() as d:
            store = ResearchStore(os.path.join(d, 'f.db'))
            try:
                s1 = run(run_paper_replay(c, bars, self._stack, 'BTC/USDT', store))
                self.assertGreater(s1['n_fills'], 0)
                s2 = run(run_paper_replay(c, bars, self._stack, 'BTC/USDT', store))
                self.assertEqual(s2['n_fills'], s1['n_fills'])
                self.assertEqual(s2['paper_net'], s1['paper_net'])
                self.assertEqual(s2['final_equity'], s1['final_equity'])
                self.assertEqual(s2['fills'], s1['fills'])
                self.assertEqual(s2['bar_stamps'], s1['bar_stamps'])
            finally:
                store.close()

    def test_crash_resume_matches_uninterrupted(self):
        # B07 regression: crash mid-replay (incl. orphaned claim keys from a
        # crash-after-claim-before-fill) then resume must equal an
        # uninterrupted run exactly: same fills, net, equity, series.
        from research.paper import run_paper_replay
        bars = tiny_bars(70, step=0.5)
        c = make_candidate('momentum', {'momentum_period': 7, 'momentum_threshold': 0.005},
                           seed=2)
        with tempfile.TemporaryDirectory() as d:
            ref_store = ResearchStore(os.path.join(d, 'ref.db'))
            try:
                ref = run(run_paper_replay(c, bars, self._stack, 'BTC/USDT',
                                           ref_store, checkpoint_every=7))
            finally:
                ref_store.close()
            crash_store = ResearchStore(os.path.join(d, 'crash.db'))
            try:
                with self.assertRaises(RuntimeError):
                    run(run_paper_replay(c, bars, self._stack, 'BTC/USDT',
                                         crash_store, checkpoint_every=7,
                                         fault_after_bars=10))
                # A checkpoint must exist despite the crash.
                resumed = run(run_paper_replay(c, bars, self._stack, 'BTC/USDT',
                                               crash_store, checkpoint_every=7))
            finally:
                crash_store.close()
        self.assertEqual(resumed['n_fills'], ref['n_fills'])
        self.assertEqual(resumed['fills'], ref['fills'])
        self.assertEqual(resumed['paper_net'], ref['paper_net'])
        self.assertEqual(resumed['final_equity'], ref['final_equity'])
        self.assertEqual(resumed['equity_per_bar'], ref['equity_per_bar'])
        self.assertEqual(resumed['bar_stamps'], ref['bar_stamps'])

    def test_crash_before_first_checkpoint_resumes_cleanly(self):
        # Crash before ANY periodic checkpoint: only the initial checkpoint
        # exists, so resume replays from the start with orphaned claims
        # dropped — still exactly equal to the uninterrupted run.
        from research.paper import run_paper_replay
        bars = tiny_bars(70, step=0.5)
        c = make_candidate('momentum', {'momentum_period': 7, 'momentum_threshold': 0.005},
                           seed=2)
        with tempfile.TemporaryDirectory() as d:
            ref_store = ResearchStore(os.path.join(d, 'ref2.db'))
            try:
                ref = run(run_paper_replay(c, bars, self._stack, 'BTC/USDT',
                                           ref_store, checkpoint_every=1000))
            finally:
                ref_store.close()
            crash_store = ResearchStore(os.path.join(d, 'crash2.db'))
            try:
                with self.assertRaises(RuntimeError):
                    run(run_paper_replay(c, bars, self._stack, 'BTC/USDT',
                                         crash_store, checkpoint_every=1000,
                                         fault_after_bars=5))
                resumed = run(run_paper_replay(c, bars, self._stack, 'BTC/USDT',
                                               crash_store, checkpoint_every=1000))
            finally:
                crash_store.close()
        self.assertEqual(resumed['fills'], ref['fills'])
        self.assertEqual(resumed['paper_net'], ref['paper_net'])
        self.assertEqual(resumed['final_equity'], ref['final_equity'])


class TestCampaignTiny(unittest.TestCase):
    def _stack(self, costs=None):
        from config import ConfigManager
        from market_data import MarketDataHandler
        from exchange import PaperExchange
        from execution import ExecutionEngine
        from risk import RiskManager
        from portfolio import PortfolioManager
        from backtest import BacktestEngine
        from strategy import StrategyManager
        cfg = ConfigManager()
        cfg.set('simulated_latency_ms', 0)
        cfg.set('min_volume', 0.0)
        cfg.set('volatility_max', 1.0)
        for k, v in (costs or {}).items():
            cfg.set(k, v)
        md = MarketDataHandler(cfg)
        ex = PaperExchange(cfg, seed=11)
        risk = RiskManager(cfg)
        pf = PortfolioManager(cfg)
        eng = ExecutionEngine(cfg, ex, risk)
        strat = StrategyManager(cfg)
        bt = BacktestEngine(cfg, md, eng, pf, risk, strat)
        return cfg, md, ex, risk, pf, eng, strat, bt

    def test_funnel_accounts_every_candidate(self):
        from research.datasets import build_research_dataset
        from research.campaign import CampaignConfig, run_campaign
        bars = tiny_bars(150, step=0.4)
        ds = build_research_dataset(bars, 'BTC/USDT', holdout_ratio=0.15)
        cfg = CampaignConfig(campaign_id='test_camp', symbol='BTC/USDT',
                             families=['momentum', 'breakout'], candidate_limit=4,
                             seed=7, n_splits=1, train_bars=40, test_bars=25,
                             warmup_bars=15, stability_neighbours=2,
                             capacity_multipliers=[1, 5], allow_holdout=False)
        store = ResearchStore(":memory:")
        try:
            record = run(run_campaign(cfg, ds, self._stack, store))
        finally:
            store.close()
        f = record['funnel']
        self.assertEqual(f['generated'], 4)
        # Every non-duplicate candidate ends promoted or rejected somewhere.
        accounted = f['paper_candidate']['passed'] + sum(
            f[s]['rejected'] for s in f if isinstance(f[s], dict) and 'rejected' in f[s])
        self.assertEqual(accounted + f.get('duplicates_skipped', 0), 4)
        # No holdout access without explicit permission.
        self.assertEqual(len(ds.holdout.access_log), 0)

    def test_campaign_deterministic_rerun(self):
        from research.datasets import build_research_dataset
        from research.campaign import CampaignConfig, run_campaign

        def once(cid):
            bars = tiny_bars(150, step=0.4)
            ds = build_research_dataset(bars, 'BTC/USDT', holdout_ratio=0.15)
            cfg = CampaignConfig(campaign_id=cid, symbol='BTC/USDT',
                                 families=['momentum', 'breakout'], candidate_limit=4,
                                 seed=7, n_splits=1, train_bars=40, test_bars=25,
                                 warmup_bars=15, stability_neighbours=2,
                                 capacity_multipliers=[1, 5], allow_holdout=False)
            store = ResearchStore(":memory:")
            try:
                record = run(run_campaign(cfg, ds, self._stack, store))
                ids = sorted(c['candidate_id'] for c in store.list_candidates())
                accesses = len(ds.holdout.access_log)
                return record['funnel'], ids, accesses
            finally:
                store.close()

        f1, ids1, acc1 = once('det_a')
        f2, ids2, acc2 = once('det_b')
        self.assertEqual(f1, f2)
        self.assertEqual(ids1, ids2)
        self.assertEqual(acc1, 0)
        self.assertEqual(acc2, 0)

    def test_provenance_and_report(self):
        from research.datasets import build_research_dataset
        from research.campaign import CampaignConfig, run_campaign
        bars = tiny_bars(150, step=0.4)
        ds = build_research_dataset(bars, 'BTC/USDT', holdout_ratio=0.15)
        cfg = CampaignConfig(campaign_id='test_rep', symbol='BTC/USDT',
                             families=['breakout'], candidate_limit=2,
                             seed=7, n_splits=1, train_bars=40, test_bars=25,
                             warmup_bars=15, stability_neighbours=2,
                             capacity_multipliers=[1, 5], allow_holdout=False)
        store = ResearchStore(":memory:")
        try:
            record = run(run_campaign(cfg, ds, self._stack, store))
            rep = build_campaign_report(record, store)
            text = terminal_summary(rep)
            self.assertIn('test_rep', text)
            self.assertIn('funnel:', text)
            payload = json.loads(json.dumps(rep))
            self.assertEqual(payload['campaign_id'], 'test_rep')
            for c in payload['candidates']:
                self.assertIn('family', c)
                self.assertIn('rejection_reason', c)
        finally:
            store.close()


class TestCLISmoke(unittest.TestCase):
    def test_help_and_status(self):
        import scripts.falcon as falcon
        with self.assertRaises(SystemExit) as cm:
            falcon.main(['--help'])
        self.assertEqual(cm.exception.code, 0)
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, 's.db')
            ResearchStore(db).close()
            code = falcon.main(['status', '--db', db])
            self.assertEqual(code, 0)

    def test_backtest_example_strategy(self):
        import scripts.falcon as falcon
        with tempfile.TemporaryDirectory() as d:
            spec = os.path.join(d, 'strat.json')
            with open(spec, 'w', encoding='utf-8') as f:
                json.dump({'family': 'momentum',
                           'params': {'momentum_period': 7, 'momentum_threshold': 0.005}}, f)
            code = falcon.main(['backtest', '--strategy-json', spec,
                                '--data', 'data/sample_ohlcv.csv'])
            self.assertEqual(code, 0)

    def test_validate_unknown_candidate_fails_cleanly(self):
        import scripts.falcon as falcon
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, 'v.db')
            ResearchStore(db).close()
            with self.assertRaises(SystemExit) as cm:
                falcon.main(['validate', '--db', db, '--candidate-id', 'cand_nope',
                             '--data', 'data/sample_ohlcv.csv'])
            self.assertNotEqual(cm.exception.code, 0)


def _research_stack(costs=None):
    from config import ConfigManager
    from market_data import MarketDataHandler
    from exchange import PaperExchange
    from execution import ExecutionEngine
    from risk import RiskManager
    from portfolio import PortfolioManager
    from backtest import BacktestEngine
    from strategy import StrategyManager
    cfg = ConfigManager()
    cfg.set('simulated_latency_ms', 0)
    cfg.set('min_volume', 0.0)
    cfg.set('volatility_max', 1.0)
    for k, v in (costs or {}).items():
        cfg.set(k, v)
    md = MarketDataHandler(cfg)
    ex = PaperExchange(cfg, seed=11)
    risk = RiskManager(cfg)
    pf = PortfolioManager(cfg)
    eng = ExecutionEngine(cfg, ex, risk)
    strat = StrategyManager(cfg)
    bt = BacktestEngine(cfg, md, eng, pf, risk, strat)
    return cfg, md, ex, risk, pf, eng, strat, bt


def _sym_bars(symbol, n, start, step, tag):
    from market_data import Candle
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [Candle(symbol=symbol, open=start + i * step,
                   high=start + i * step + 0.2, low=start + i * step - 0.2,
                   close=start + i * step + 0.05, volume=5000.0,
                   timestamp=base + timedelta(minutes=5 * i), interval='5m',
                   event_id=f'{tag}{i}') for i in range(n)]


class TestIncrementalEMA(unittest.TestCase):
    def test_bit_identical_sequential(self):
        from strategy import ema_last, IncrementalEMA
        import random
        rng = random.Random(0)
        closes, px = [], 100.0
        for _ in range(200):
            px += rng.uniform(-1, 1.05)
            closes.append(px)
        inc = IncrementalEMA()
        for n in range(1, len(closes) + 1):
            w = closes[:n]
            f1, s1, pf1, ps1 = inc.emas('X', w, 9, 27)
            f0, s0 = ema_last(w, 9), ema_last(w, 27)
            p0f = ema_last(w[:-1], 9) if n >= 2 else None
            p0s = ema_last(w[:-1], 27) if n >= 2 else None
            self.assertEqual(f1, f0)
            self.assertEqual(s1, s0)
            self.assertEqual(pf1, p0f)
            self.assertEqual(ps1, p0s)

    def test_fallback_and_symbol_isolation(self):
        from strategy import ema_last, IncrementalEMA
        inc = IncrementalEMA()
        a = [100.0 + i * 0.1 for i in range(50)]
        b = [200.0 - i * 0.1 for i in range(50)]
        fa, _, _, _ = inc.emas('A', a, 9, 27)
        fb, _, _, _ = inc.emas('B', b, 9, 27)
        self.assertEqual(fa, ema_last(a, 9))
        self.assertEqual(fb, ema_last(b, 9))
        # Non-sequential jump falls back to exact legacy values.
        f2, _, _, _ = inc.emas('A', a[:20], 9, 27)
        self.assertEqual(f2, ema_last(a[:20], 9))


class TestDatafiles(unittest.TestCase):
    def test_validate_report_counts(self):
        rows = [
            [1704067200000, '100', '101', '99', '100.5', '10'],
            [1704070800000, '100.5', '102', '100', '101.5', '12'],
            [1704070800000, '100.5', '102', '100', '101.5', '12'],
            [1704074400000, '101.5', '101', '102', '101.8', '-3'],
            ['bad'],
        ]
        rep = validate_klines(rows, '1h')
        self.assertEqual(rep['n_rows'], 5)
        self.assertEqual(rep['bad_rows'], 1)
        self.assertEqual(rep['duplicates'], 1)
        self.assertEqual(rep['impossible_ohlc'], 1)
        self.assertEqual(rep['negative_volume'], 1)
        self.assertGreaterEqual(rep['interval_ok_ratio'], 0.0)

    def test_build_load_roundtrip_and_tamper(self):
        import json as _json
        with tempfile.TemporaryDirectory() as d:
            raw = os.path.join(d, 'raw.json')
            with open(raw, 'w', encoding='utf-8') as f:
                _json.dump({"symbol": "BTCUSDT", "interval": "1h",
                            "source": "test",
                            "rows": [[1704067200000 + i * 3600000,
                                      str(100 + i), str(101 + i), str(99 + i),
                                      str(100.5 + i), '10'] for i in range(10)]}, f)
            csv_p = os.path.join(d, 'ds.csv')
            man_p = os.path.join(d, 'ds.manifest.json')
            man = build_versioned_dataset(raw, csv_p, man_p)
            for k in ('dataset_id', 'symbol', 'interval', 'start', 'end', 'rows',
                      'source', 'created_at', 'sha256', 'schema_version'):
                self.assertIn(k, man)
            self.assertEqual(man['rows'], 10)
            candles, back = load_versioned_dataset(man_p)
            self.assertEqual(len(candles), 10)
            self.assertEqual(back['dataset_id'], man['dataset_id'])
            with open(csv_p, 'a', encoding='utf-8') as f:
                f.write('2024-01-02T00:00:00+00:00,1,1,1,1,1,X,1h\n')
            with self.assertRaises(ValueError):
                load_versioned_dataset(man_p)

    def test_resample_math(self):
        bars = _sym_bars('BTC/USDT', 10, 100.0, 1.0, 'rs')
        out, stats = resample_ohlcv(bars, 300, 900, '15m')
        self.assertEqual(len(out), 3)  # trailing partial bucket dropped
        self.assertEqual(stats['dropped_partial'], 1)
        self.assertAlmostEqual(out[0].open, 100.0)
        self.assertAlmostEqual(out[0].close, 102.05)
        self.assertAlmostEqual(out[0].high, 102.2)
        self.assertAlmostEqual(out[0].low, 99.8)
        self.assertAlmostEqual(out[0].volume, 15000.0)
        # Bucket START on the aligned 15m grid (not last constituent).
        self.assertEqual(out[0].timestamp.isoformat(), '2024-01-01T00:00:00+00:00')
        self.assertEqual(out[1].timestamp.isoformat(), '2024-01-01T00:15:00+00:00')
        self.assertEqual(out[2].timestamp.isoformat(), '2024-01-01T00:30:00+00:00')
        with self.assertRaises(ValueError):
            resample_ohlcv(bars, 0, 900, '15m')
        with self.assertRaises(ValueError):
            resample_ohlcv(bars, 300, 1000, '15m')  # not a multiple

    def test_resample_gap_drops_partial_buckets(self):
        # B01 regression: row-grouping used to relabel gapped bars as full
        # 15m bars. Time buckets must drop them and count the loss instead.
        bars = _sym_bars('BTC/USDT', 10, 100.0, 1.0, 'rg')
        gapped = [b for i, b in enumerate(bars) if i != 4]  # hole at 00:20
        out, stats = resample_ohlcv(gapped, 300, 900, '15m')
        # Buckets: [00:00 full] [00:15 partial: 00:15,00:25] [00:30 full] [00:45 partial]
        self.assertEqual(stats['dropped_partial'], 2)
        self.assertEqual(stats['dropped_bars'], 3)
        self.assertEqual(len(out), 2)
        # Grid alignment preserved across gaps: spacing stays an exact
        # multiple of the target step (never a relabeled partial bucket).
        for a, b in zip(out, out[1:]):
            gap = (b.timestamp - a.timestamp).total_seconds()
            self.assertEqual(gap % 900.0, 0.0)
            self.assertGreaterEqual(gap, 900.0)

    def test_resample_output_spacing_is_exact(self):
        bars = _sym_bars('BTC/USDT', 25, 100.0, 0.5, 'sp')
        out, _ = resample_ohlcv(bars, 300, 3600, '1h')
        self.assertGreater(len(out), 1)
        for b in out:
            epoch = int(b.timestamp.timestamp())
            self.assertEqual(epoch % 3600, 0)  # grid-aligned
        for a, b in zip(out, out[1:]):
            self.assertEqual((b.timestamp - a.timestamp).total_seconds(), 3600.0)

    def test_resample_dedupes_identical_timestamps(self):
        bars = _sym_bars('BTC/USDT', 6, 100.0, 1.0, 'dd')
        doubled = bars + [bars[0]]
        out, stats = resample_ohlcv(doubled, 300, 900, '15m')
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[0].volume, 15000.0)  # no double count


class TestMultisymbol(unittest.TestCase):
    def test_cross_gate_rejects_single_market(self):
        from research.gates import gate_cross_market, GateConfig
        c = make_candidate('momentum', {'momentum_period': 14, 'momentum_threshold': 0.01}, 1)
        per = {'BTC/USDT': {'valid': {'expectancy': 1.0, 'profit_factor': 1.5,
                                      'max_drawdown': 0.05}},
               'ETH/USDT': {'valid': {'expectancy': -1.0, 'profit_factor': 0.5,
                                      'max_drawdown': 0.05}}}
        ok, reason = gate_cross_market(c, per, GateConfig())
        self.assertFalse(ok)
        self.assertTrue(reason.startswith('single_market_only'))

    def test_two_symbol_campaign_and_funnel(self):
        from research.datasets import build_research_dataset
        from research.campaign import CampaignConfig, run_campaign
        ds_btc = build_research_dataset(_sym_bars('BTC/USDT', 150, 100.0, 0.4, 'b'),
                                        'BTC/USDT', holdout_ratio=0.15)
        ds_eth = build_research_dataset(_sym_bars('ETH/USDT', 150, 50.0, 0.3, 'e'),
                                        'ETH/USDT', holdout_ratio=0.15)
        cfg = CampaignConfig(campaign_id='multi2', symbol='BTC/USDT',
                             symbols=['BTC/USDT', 'ETH/USDT'],
                             families=['momentum'], candidate_limit=2,
                             seed=7, n_splits=1, train_bars=40, test_bars=25,
                             warmup_bars=15, stability_neighbours=2,
                             capacity_multipliers=[1, 5], allow_holdout=False)
        store = ResearchStore(":memory:")
        try:
            record = run(run_campaign(cfg, ds_btc, _research_stack, store,
                                      extra_datasets={'ETH/USDT': ds_eth}))
        finally:
            store.close()
        self.assertEqual(record['symbols'], ['BTC/USDT', 'ETH/USDT'])
        self.assertIn('cross_market', record['funnel'])
        f = record['funnel']
        accounted = f['paper_candidate']['passed'] + sum(
            f[s]['rejected'] for s in f if isinstance(f[s], dict) and 'rejected' in f[s])
        self.assertEqual(accounted + f.get('duplicates_skipped', 0), 2)
        self.assertEqual(len(ds_btc.holdout.access_log), 0)
        self.assertEqual(len(ds_eth.holdout.access_log), 0)

    def test_missing_dataset_raises(self):
        from research.datasets import build_research_dataset
        from research.campaign import CampaignConfig, run_campaign
        ds = build_research_dataset(_sym_bars('BTC/USDT', 150, 100.0, 0.4, 'b2'),
                                    'BTC/USDT', holdout_ratio=0.15)
        cfg = CampaignConfig(campaign_id='miss', symbol='BTC/USDT',
                             symbols=['BTC/USDT', 'ETH/USDT'],
                             families=['momentum'], candidate_limit=1, seed=7)
        store = ResearchStore(":memory:")
        try:
            with self.assertRaises(ValueError):
                run(run_campaign(cfg, ds, _research_stack, store))
        finally:
            store.close()

    def test_rerun_skips_previously_evaluated(self):
        from research.datasets import build_research_dataset
        from research.campaign import CampaignConfig, run_campaign
        bars = _sym_bars('BTC/USDT', 150, 100.0, 0.4, 're')
        ds = build_research_dataset(bars, 'BTC/USDT', holdout_ratio=0.15)

        def make_cfg(cid):
            return CampaignConfig(campaign_id=cid, symbol='BTC/USDT',
                                  families=['momentum'], candidate_limit=6,
                                  seed=7, n_splits=1, train_bars=40, test_bars=25,
                                  warmup_bars=15, stability_neighbours=2,
                                  capacity_multipliers=[1, 5], allow_holdout=False)

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'resume.db')
            store = ResearchStore(path)
            try:
                r1 = run(run_campaign(make_cfg('run1'), ds, _research_stack, store))
                n_eval_1 = sum(len(store.evaluations_for(c['candidate_id']))
                               for c in store.list_candidates())
                r2 = run(run_campaign(make_cfg('run2'), ds, _research_stack, store))
                n_eval_2 = sum(len(store.evaluations_for(c['candidate_id']))
                               for c in store.list_candidates())
            finally:
                store.close()
            # Second identical run re-evaluates nothing: no new evaluations,
            # everything accounted as previously-evaluated or in-list dupes.
            self.assertEqual(n_eval_2, n_eval_1)
            self.assertEqual(r2['funnel']['generated'], r1['funnel']['generated'])
            accounted = (r2['funnel'].get('previously_evaluated', 0)
                         + r2['funnel'].get('duplicates_skipped', 0)
                         + r2['funnel']['paper_candidate']['passed']
                         + sum(r2['funnel'][s]['rejected'] for s in r2['funnel']
                               if isinstance(r2['funnel'][s], dict)
                               and 'rejected' in r2['funnel'][s]))
            self.assertEqual(accounted, r2['funnel']['generated'])


class TestCostMatrix(unittest.TestCase):
    def test_matrix_covers_configured_multipliers(self):
        from research.campaign import _gate_costs, CampaignConfig
        bars = _sym_bars('BTC/USDT', 60, 100.0, 0.5, 'cm')
        cand = make_candidate('momentum', {'momentum_period': 7, 'momentum_threshold': 0.005},
                              seed=3)
        strategy = fam.build_strategy(cand.family, cand.params)
        cfg = CampaignConfig(campaign_id='mx', symbol='BTC/USDT', families=['momentum'],
                             candidate_limit=1, seed=3,
                             cost_multipliers=[1, 1.5, 2, 3, 5])
        store = ResearchStore(":memory:")
        try:
            funnel = {'cost_stress_passed': {'passed': 0, 'rejected': 0}}
            ok = run(_gate_costs(cfg, None, cand, strategy, _research_stack,
                                 bars, store, funnel, None))
            payload = cand.evaluations[-1]['metrics']
            names = [m['scenario'] for m in payload['matrix']]
            self.assertEqual(names, ['base', 'x1.5', 'x2', 'x3', 'x5'])
            self.assertTrue(ok or cand.rejection_reason.startswith('cost_sensitive'))
        finally:
            store.close()


class TestStressWiring(unittest.TestCase):
    """B04 regression: stress/capacity scenarios must instantiate the real
    candidate strategy with effective (pre-construction) config overrides."""

    def test_cost_factory_applies_effective_values(self):
        from research.campaign import _stack_factory_with, SingleStrategyManager
        strategy = fam.build_strategy(
            'momentum', {'momentum_period': 7, 'momentum_threshold': 0.005})
        build = _stack_factory_with(_research_stack, strategy,
                                    extra={'taker_fee': 0.008})
        out = build({})
        self.assertAlmostEqual(out[2].taker_fee, 0.008)
        self.assertIsInstance(out[-1].strategy_manager, SingleStrategyManager)

    def test_cost_matrix_runs_candidate_not_default(self):
        # Reproduces the old failure: every scenario ran the default
        # strategy at base costs, so two families gave identical matrices.
        from research.campaign import _gate_costs, CampaignConfig
        bars = _sym_bars('BTC/USDT', 60, 100.0, 0.5, 'cw')
        nets = {}
        for family, params in (
                ('momentum', {'momentum_period': 7, 'momentum_threshold': 0.005}),
                ('breakout', {'lookback': 10})):
            cand = make_candidate(family, params, seed=11)
            strategy = fam.build_strategy(family, params)
            cfg = CampaignConfig(campaign_id=f'mx-{family}', symbol='BTC/USDT',
                                 families=[family], candidate_limit=1, seed=11,
                                 cost_multipliers=[1, 2])
            store = ResearchStore(":memory:")
            try:
                funnel = {'cost_stress_passed': {'passed': 0, 'rejected': 0}}
                run(_gate_costs(cfg, None, cand, strategy, _research_stack,
                                bars, store, funnel, None))
                matrix = cand.evaluations[-1]['metrics']['matrix']
                by_name = {m['scenario']: m for m in matrix}
                self.assertGreater(by_name['base']['fills'], 0)
                # 2x fees must bite: strictly higher fee bill than base.
                self.assertGreater(by_name['x2']['fees'], by_name['base']['fees'])
                nets[family] = by_name['base']['net']
            finally:
                store.close()
        self.assertNotEqual(nets['momentum'], nets['breakout'])

    def test_capacity_scales_capital_and_strategy(self):
        # Reproduces the old failure: every level ran base capital with the
        # default strategy, so turnover was identical across levels.
        from research.campaign import _gate_capacity, CampaignConfig
        bars = _sym_bars('BTC/USDT', 60, 100.0, 0.5, 'cap')
        cand = make_candidate('momentum', {'momentum_period': 7, 'momentum_threshold': 0.005},
                              seed=13)
        cfg = CampaignConfig(campaign_id='capx', symbol='BTC/USDT', families=['momentum'],
                             candidate_limit=1, seed=13,
                             capacity_multipliers=[1, 2])
        store = ResearchStore(":memory:")
        try:
            funnel = {'capacity_passed': {'passed': 0, 'rejected': 0}}
            run(_gate_capacity(cfg, cand, _research_stack, bars, store, funnel, None))
            levels = cand.evaluations[-1]['metrics']['levels']
            by_mult = {lv['multiplier']: lv for lv in levels}
            self.assertAlmostEqual(by_mult[2.0]['capital'], 2.0 * by_mult[1.0]['capital'])
            self.assertGreater(by_mult[2.0]['turnover'], by_mult[1.0]['turnover'])
            self.assertGreater(by_mult[1.0]['turnover'], 0.0)
        finally:
            store.close()


class TestMtesting(unittest.TestCase):
    def test_lcb_math(self):
        rep = expectancy_lcb([2.0] * 25)
        self.assertTrue(rep['sufficient'])
        self.assertAlmostEqual(rep['mean'], 2.0, places=9)
        self.assertAlmostEqual(rep['lcb'], 2.0, places=9)  # zero variance

    def test_insufficient_state(self):
        rep = expectancy_lcb([5.0, -1.0, 2.0])
        self.assertFalse(rep['sufficient'])
        self.assertIsNone(rep['lcb'])
        self.assertIn('insufficient', rep['note'])

    def test_snoop_flag(self):
        good = snooping_report('c1', [0.5] * 60, n_trials=200)
        self.assertFalse(good['snoop_warning'])  # tight variance -> LCB still > 0
        spread = [2.0 if i % 2 == 0 else -1.8 for i in range(60)]
        rep2 = snooping_report('c3', spread, n_trials=200)
        self.assertTrue(rep2['snoop_warning'])
        self.assertEqual(rep2['n_trials'], 200)


class TestInterfaces(unittest.TestCase):
    def test_conformance(self):
        from config import ConfigManager
        from market_data import MarketDataHandler
        from exchange import PaperExchange
        cfg = ConfigManager()
        self.assertTrue(provides_market_data(MarketDataHandler(cfg)))
        ex = PaperExchange(cfg, seed=1)
        self.assertTrue(provides_execution(ex))
        run(ex.connect())
        run(ex.disconnect())

    def test_stubs_fail_loudly(self):
        with self.assertRaises(RuntimeError):
            TestnetAdapterStub()
        stub = TestnetAdapterStub.__new__(TestnetAdapterStub)
        with self.assertRaises(RuntimeError):
            run(stub.submit_order())
        with self.assertRaises(RuntimeError):
            run(stub.connect())


class TestPaperDivergence(unittest.TestCase):
    def test_identical_series_no_warnings(self):
        from research.paper import rolling_divergence
        eq = [10000.0 + i for i in range(60)]
        out = rolling_divergence({'net': 59.0, 'fills': 60, 'fees': 6.0, 'n_bars': 60},
                                 eq, [1] * 60, [0.1 * i for i in range(60)])
        self.assertEqual(out['n_warnings'], 0)

    def test_flipped_series_warns(self):
        from research.paper import rolling_divergence
        eq = [10000.0 - i * 2 for i in range(60)]
        out = rolling_divergence({'net': 59.0, 'fills': 30, 'fees': 6.0, 'n_bars': 60},
                                 eq, [1] * 60, [0.1 * i for i in range(60)])
        kinds = {w.split(':')[0] for w in out['warnings']}
        self.assertIn('net_sign_flip', kinds)

    def test_empty_safe(self):
        from research.paper import rolling_divergence
        out = rolling_divergence({'net': 0.0, 'fills': 0, 'fees': 0.0, 'n_bars': 1},
                                 [], [], [])
        self.assertEqual(out['windows'], [])
        self.assertEqual(out['n_warnings'], 0)


class TestRegimeMetrics(unittest.TestCase):
    def test_report_table_math(self):
        from research.regimes import regime_report_table
        attr = {'trending_up': {'n': 3, 'pnl': 30.0, 'wins': 3, 'losses': 0,
                               'gross_win': 30.0, 'gross_loss': 0.0},
                'trending_down': {'n': 2, 'pnl': -10.0, 'wins': 0, 'losses': 2,
                                  'gross_win': 0.0, 'gross_loss': -10.0}}
        table = regime_report_table(attr)
        self.assertAlmostEqual(table['trending_up']['expectancy'], 10.0)
        self.assertEqual(table['trending_up']['profit_factor'], float('inf'))
        self.assertAlmostEqual(table['trending_down']['profit_factor'], 0.0)

    def test_drawdown_v_shape(self):
        from backtest import BacktestResult
        from research.regimes import regime_drawdowns
        bars = _sym_bars('BTC/USDT', 30, 100.0, 0.0, 'dd')
        eq = [100.0, 110.0, 105.0, 95.0, 100.0]
        res = BacktestResult(equity_curve=eq,
                             equity_stamps=[b.timestamp.isoformat() for b in bars[:5]])
        dd = regime_drawdowns(res, bars)
        # Single contiguous run (all sideways/low-vol here): (110-95)/110.
        worst = max(dd.values())
        self.assertAlmostEqual(worst, (110.0 - 95.0) / 110.0, places=9)


class TestOptimizer(unittest.TestCase):
    def test_deterministic_and_bounded(self):
        from research.optimizer import BayesianOptimizer, train_objective
        o1, o2 = BayesianOptimizer('momentum', 4), BayesianOptimizer('momentum', 4)
        p1, p2 = o1.propose([], 3), o2.propose([], 3)
        self.assertEqual(p1, p2)
        space = fam.param_space('momentum')
        for p in p1:
            for k, v in p.items():
                self.assertIn(v, space[k])
        hist = [{'params': p, 'score': 0.5} for p in p1]
        q1 = o1.propose(hist, 3)
        o3 = BayesianOptimizer('momentum', 4)
        o3.propose([], 3)
        self.assertEqual(q1, o3.propose(hist, 3))

    def test_unknown_family_raises(self):
        from research.optimizer import BayesianOptimizer
        with self.assertRaises(ValueError):
            BayesianOptimizer('nope', 1)

    def test_train_objective_ranks_and_rejects_thin(self):
        from research.optimizer import train_objective
        self.assertEqual(train_objective({'n_closed': 2, 'expectancy': 99.0}), -1.0)
        good = train_objective({'n_closed': 20, 'expectancy': 2.0,
                                'profit_factor': 2.0, 'max_drawdown': 0.05})
        bad = train_objective({'n_closed': 20, 'expectancy': -1.0,
                               'profit_factor': 0.5, 'max_drawdown': 0.05})
        self.assertGreater(good, bad)

    def test_bayes_campaign_branch(self):
        from research.datasets import build_research_dataset
        from research.campaign import CampaignConfig, run_campaign
        bars = _sym_bars('BTC/USDT', 150, 100.0, 0.4, 'bo')
        ds = build_research_dataset(bars, 'BTC/USDT', holdout_ratio=0.15)
        cfg = CampaignConfig(campaign_id='bayes1', symbol='BTC/USDT',
                             families=['momentum'], candidate_limit=4,
                             seed=7, n_splits=1, train_bars=40, test_bars=25,
                             warmup_bars=15, stability_neighbours=2,
                             capacity_multipliers=[1, 5], allow_holdout=False,
                             optimizer='bayes', optimizer_rounds=1,
                             optimizer_batch=2, optimizer_top_k=2)
        store = ResearchStore(":memory:")
        try:
            record = run(run_campaign(cfg, ds, _research_stack, store))
        finally:
            store.close()
        self.assertEqual(record['optimizer'], 'bayes')
        self.assertGreater(record.get('optimizer_trials', 0), 0)
        f = record['funnel']
        accounted = f['paper_candidate']['passed'] + sum(
            f[s]['rejected'] for s in f if isinstance(f[s], dict) and 'rejected' in f[s])
        self.assertEqual(accounted + f.get('duplicates_skipped', 0),
                         f['generated'])


class TestDatasetCLI(unittest.TestCase):
    def test_inspect_real_manifest(self):
        import scripts.falcon as falcon
        code = falcon.main(['dataset', 'inspect', 'data/falcon-BTCUSDT-1h.manifest.json'])
        self.assertEqual(code, 0)

    def test_inspect_missing_fails_cleanly(self):
        import scripts.falcon as falcon
        with self.assertRaises(SystemExit) as cm:
            falcon.main(['dataset', 'inspect', 'data/nope.manifest.json'])
        self.assertNotEqual(cm.exception.code, 0)


class TestFreshHoldoutTail(unittest.TestCase):
    def test_holdout_is_most_recent_segment(self):
        from research.datasets import build_research_dataset, FINAL_MODE
        bars = _sym_bars('BTC/USDT', 100, 100.0, 0.5, 'fh')
        ds = build_research_dataset(bars, 'BTC/USDT', holdout_ratio=0.2)
        hold = ds.holdout.open_holdout(FINAL_MODE, 'test', 'tail check')
        self.assertEqual([c.timestamp for c in hold],
                         [c.timestamp for c in bars[-20:]])
        self.assertLess(max(c.timestamp for c in ds.research_bars),
                        min(c.timestamp for c in hold))

    def test_campaign_wires_holdout_access_log(self):
        from research.datasets import build_research_dataset
        from research.campaign import CampaignConfig, run_campaign
        bars = _sym_bars('BTC/USDT', 150, 100.0, 0.4, 'hw')
        ds = build_research_dataset(bars, 'BTC/USDT', holdout_ratio=0.15)
        cfg = CampaignConfig(campaign_id='hwlog', symbol='BTC/USDT',
                             families=['breakout'], candidate_limit=2,
                             seed=7, n_splits=1, train_bars=40, test_bars=25,
                             warmup_bars=15, stability_neighbours=2,
                             capacity_multipliers=[1, 5], allow_holdout=False)
        collected = []
        store = ResearchStore(":memory:")
        try:
            record = run(run_campaign(cfg, ds, _research_stack, store,
                                      on_holdout_access=collected.append))
        finally:
            store.close()
        self.assertEqual(collected, [])
        self.assertEqual(record.get('holdout_accesses'), [])
        self.assertEqual(len(ds.holdout.access_log), 0)


if __name__ == '__main__':
    unittest.main()
