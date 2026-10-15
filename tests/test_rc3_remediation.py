"""RC3 audit-remediation regression tests (R01-R06, R08, R10).

Small synthetic data only; no strategy tuning; no production-gate changes.
The full-promotion test (R03) is a MECHANICS FIXTURE: market simulation is
stubbed with controlled metrics so one candidate deterministically
traverses every stage. It proves pipeline plumbing (gates, persistence,
funnel, promotion) — it is NOT a profitable strategy and its numbers must
never be reported as research evidence.
"""
import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest import mock


def run(coro):
    return asyncio.run(coro)


def tiny_bars(n=150, start=100.0, step=0.4, prefix="rc3", symbol="BTC/USDT",
              volume=5000.0, interval="5m", minutes=5):
    from market_data import Candle
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [Candle(symbol=symbol, open=start + i * step,
                   high=start + i * step + 0.2, low=start + i * step - 0.2,
                   close=start + i * step + 0.05, volume=volume,
                   timestamp=base + timedelta(minutes=minutes * i),
                   interval=interval,
                   event_id=f"{prefix}{i}") for i in range(n)]


def cycling_bars(n=200, prefix="cyc", symbol="BTC/USDT"):
    """Deterministic up/down/flat cycles so every trend regime is present
    in any ~35-bar window (used by regime fixtures). Eight-bar phases keep
    trailing-20-bar labels mixed across regimes."""
    from market_data import Candle
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    price = 100.0
    out = []
    for i in range(n):
        phase = (i // 8) % 3
        step = 0.6 if phase == 0 else (-0.6 if phase == 1 else 0.0)
        price += step
        out.append(Candle(symbol=symbol, open=price - step,
                          high=price + 0.2, low=price - 0.2, close=price,
                          volume=5000.0,
                          timestamp=base + timedelta(minutes=5 * i),
                          interval="5m", event_id=f"{prefix}{i}"))
    return out


def research_stack(costs=None):
    from config import ConfigManager
    from market_data import MarketDataHandler
    from exchange import PaperExchange
    from execution import ExecutionEngine
    from risk import RiskManager
    from portfolio import PortfolioManager
    from backtest import BacktestEngine
    from strategy import StrategyManager
    cfg = ConfigManager()
    cfg.set("simulated_latency_ms", 0)
    cfg.set("min_volume", 0.0)
    cfg.set("volatility_max", 1.0)
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


def eval_count(store):
    return sum(len(store.evaluations_for(c["candidate_id"]))
               for c in store.list_candidates())


class TestR01ExperimentDedup(unittest.TestCase):
    """Strategy identity != experiment identity.

    Same strategy + same experiment config may reuse completed work; any
    change the experiment fingerprint covers (dataset hash, gates, costs,
    splits, seed, protocol) MUST evaluate as a new experiment.
    """

    def _run(self, store, campaign_id, content_hash="H", gates=None,
             cost_multipliers=None, seed=7, bars=None, limit=2):
        from research.campaign import CampaignConfig, run_campaign
        from research.datasets import build_research_dataset
        from research.gates import GateConfig
        bars = bars if bars is not None else tiny_bars(120, prefix=f"r01{seed}")
        ds = build_research_dataset(bars, "BTC/USDT", holdout_ratio=0.15,
                                    content_hash=content_hash)
        cfg = CampaignConfig(
            campaign_id=campaign_id, symbol="BTC/USDT",
            families=["momentum"], candidate_limit=limit, seed=seed,
            n_splits=1, train_bars=40, test_bars=25, warmup_bars=15,
            stability_neighbours=2, capacity_multipliers=[1, 5],
            allow_holdout=False,
            gates=gates or GateConfig(),
            cost_multipliers=(list(cost_multipliers)
                              if cost_multipliers is not None else [1, 5]))
        return run(run_campaign(cfg, ds, research_stack, store))

    def test_A_same_experiment_reuses(self):
        from research.store import ResearchStore
        with tempfile.TemporaryDirectory() as d:
            store = ResearchStore(os.path.join(d, "a.db"))
            try:
                bars = tiny_bars(120, prefix="r01a")
                r1 = self._run(store, "a1", bars=bars)
                n1 = eval_count(store)
                self.assertGreater(n1, 0)
                r2 = self._run(store, "a2", bars=bars)
                # Genuine reuse: nothing re-evaluated, everything accounted.
                self.assertEqual(r2["funnel"]["previously_evaluated"],
                                 r2["funnel"]["generated"])
                self.assertEqual(eval_count(store), n1)
                # Reporting stays isolated: skipped work leaves no rows for a2.
                from research.report import build_campaign_report
                self.assertEqual(
                    len(build_campaign_report(r2, store)["candidates"]), 0)
                self.assertEqual(
                    len(build_campaign_report(r1, store)["candidates"]),
                    r1["funnel"]["generated"]
                    - r1["funnel"].get("duplicates_skipped", 0))
            finally:
                store.close()

    def test_B_different_dataset_hash_is_new_experiment(self):
        from research.store import ResearchStore
        with tempfile.TemporaryDirectory() as d:
            store = ResearchStore(os.path.join(d, "b.db"))
            try:
                bars = tiny_bars(120, prefix="r01b")
                self._run(store, "b1", content_hash="HASH-1", bars=bars)
                n1 = eval_count(store)
                r2 = self._run(store, "b2", content_hash="HASH-2", bars=bars)
                self.assertEqual(r2["funnel"]["previously_evaluated"], 0)
                self.assertGreater(eval_count(store), n1)
            finally:
                store.close()

    def test_C_different_gates_is_new_experiment(self):
        from research.gates import GateConfig
        from research.store import ResearchStore
        with tempfile.TemporaryDirectory() as d:
            store = ResearchStore(os.path.join(d, "c.db"))
            try:
                bars = tiny_bars(120, prefix="r01c")
                self._run(store, "c1", bars=bars,
                          gates=GateConfig(min_trades=10))
                n1 = eval_count(store)
                r2 = self._run(store, "c2", bars=bars,
                               gates=GateConfig(min_trades=5))
                self.assertEqual(r2["funnel"]["previously_evaluated"], 0)
                self.assertGreater(eval_count(store), n1)
            finally:
                store.close()

    def test_D_different_cost_model_is_new_experiment(self):
        from research.store import ResearchStore
        with tempfile.TemporaryDirectory() as d:
            store = ResearchStore(os.path.join(d, "ddb.db"))
            try:
                bars = tiny_bars(120, prefix="r01d")
                self._run(store, "d1", bars=bars, cost_multipliers=[1, 5])
                n1 = eval_count(store)
                r2 = self._run(store, "d2", bars=bars, cost_multipliers=[1, 2])
                self.assertEqual(r2["funnel"]["previously_evaluated"], 0)
                self.assertGreater(eval_count(store), n1)
            finally:
                store.close()

    def test_E_different_seed_is_new_experiment(self):
        from research.store import ResearchStore
        with tempfile.TemporaryDirectory() as d:
            store = ResearchStore(os.path.join(d, "e.db"))
            try:
                self._run(store, "e1", seed=7,
                          bars=tiny_bars(120, prefix="r01e"))
                n1 = eval_count(store)
                r2 = self._run(store, "e2", seed=8,
                               bars=tiny_bars(120, prefix="r01e"))
                self.assertEqual(r2["funnel"]["previously_evaluated"], 0)
                self.assertGreater(eval_count(store), n1)
            finally:
                store.close()

    def test_fp_differs_on_each_axis(self):
        from research.campaign import effective_config, _experiment_fp
        from research.campaign import CampaignConfig
        from research.datasets import build_research_dataset
        from research.gates import GateConfig
        bars = tiny_bars(120, prefix="r01f")
        ds = build_research_dataset(bars, "BTC/USDT", holdout_ratio=0.15,
                                    content_hash="H")
        ds2 = build_research_dataset(bars, "BTC/USDT", holdout_ratio=0.15,
                                     content_hash="H2")

        def fp(dsx, gates, mults, seed):
            cfg = CampaignConfig(campaign_id="x", symbol="BTC/USDT",
                                 families=["momentum"], seed=seed,
                                 gates=gates, cost_multipliers=mults)
            return _experiment_fp(
                effective_config(cfg, {"BTC/USDT": dsx}, ["BTC/USDT"]),
                "stratfp", seed)

        base = fp(ds, GateConfig(), [1, 5], 7)
        self.assertEqual(base, fp(ds, GateConfig(), [1, 5], 7))
        self.assertNotEqual(base, fp(ds2, GateConfig(), [1, 5], 7))
        self.assertNotEqual(base, fp(ds, GateConfig(min_trades=5), [1, 5], 7))
        self.assertNotEqual(base, fp(ds, GateConfig(), [1, 2], 7))
        self.assertNotEqual(base, fp(ds, GateConfig(), [1, 5], 8))


class TestR02PaperIsolation(unittest.TestCase):
    """Paper session identity: BTC vs ETH with identical timestamps MUST
    be isolated; dataset changes MUST not reuse; identical replays stay
    idempotent; crash recovery still works."""

    def _cand(self, seed=2):
        from research.generate import make_candidate
        return make_candidate("momentum", {"momentum_period": 7,
                                           "momentum_threshold": 0.005}, seed)

    def test_btc_eth_isolated(self):
        from research.paper import run_paper_replay
        from research.store import ResearchStore
        c = self._cand()
        btc = tiny_bars(70, step=0.5, prefix="btc", symbol="BTC/USDT")
        eth = tiny_bars(70, step=0.5, prefix="eth", symbol="ETH/USDT")
        # Identical timestamps by construction (same base + grid).
        self.assertEqual([b.timestamp for b in btc],
                         [b.timestamp for b in eth])
        with tempfile.TemporaryDirectory() as d:
            store = ResearchStore(os.path.join(d, "iso.db"))
            try:
                s_btc = run(run_paper_replay(c, btc, research_stack,
                                             "BTC/USDT", store,
                                             dataset_version="ds1",
                                             dataset_hash="h1"))
                s_eth = run(run_paper_replay(c, eth, research_stack,
                                             "ETH/USDT", store,
                                             dataset_version="ds1",
                                             dataset_hash="h1"))
                self.assertNotEqual(s_btc["paper_id"], s_eth["paper_id"])
                self.assertEqual(s_btc["symbol"], "BTC/USDT")
                self.assertEqual(s_eth["symbol"], "ETH/USDT")
                # Both sessions retrievable and unmixed.
                back_btc = store.load_paper_session(s_btc["paper_id"])
                back_eth = store.load_paper_session(s_eth["paper_id"])
                self.assertEqual(back_btc["symbol"], "BTC/USDT")
                self.assertEqual(back_eth["symbol"], "ETH/USDT")
                self.assertEqual(back_btc["fills"], s_btc["fills"])
                self.assertEqual(back_eth["fills"], s_eth["fills"])
            finally:
                store.close()

    def test_dataset_change_no_reuse(self):
        from research.paper import run_paper_replay
        from research.store import ResearchStore
        c = self._cand()
        bars = tiny_bars(70, step=0.5, prefix="dsv")
        with tempfile.TemporaryDirectory() as d:
            store = ResearchStore(os.path.join(d, "dsv.db"))
            try:
                s1 = run(run_paper_replay(c, bars, research_stack, "BTC/USDT",
                                          store, dataset_version="ds1",
                                          dataset_hash="h1"))
                s2 = run(run_paper_replay(c, bars, research_stack, "BTC/USDT",
                                          store, dataset_version="ds2",
                                          dataset_hash="h2"))
                self.assertNotEqual(s1["paper_id"], s2["paper_id"])
                self.assertEqual(s2["dataset_version"], "ds2")
                # Identical request replays idempotently.
                s3 = run(run_paper_replay(c, bars, research_stack, "BTC/USDT",
                                          store, dataset_version="ds1",
                                          dataset_hash="h1"))
                self.assertEqual(s3["paper_id"], s1["paper_id"])
                self.assertEqual(s3["fills"], s1["fills"])
                self.assertEqual(s3["paper_net"], s1["paper_net"])
            finally:
                store.close()

    def test_crash_resume_with_identity(self):
        from research.paper import run_paper_replay
        from research.store import ResearchStore
        c = self._cand()
        bars = tiny_bars(70, step=0.5, prefix="crsh")
        with tempfile.TemporaryDirectory() as d:
            ref_store = ResearchStore(os.path.join(d, "ref.db"))
            try:
                ref = run(run_paper_replay(c, bars, research_stack, "BTC/USDT",
                                           ref_store, checkpoint_every=7,
                                           dataset_version="ds1",
                                           dataset_hash="h1"))
            finally:
                ref_store.close()
            crash_store = ResearchStore(os.path.join(d, "crash.db"))
            try:
                with self.assertRaises(RuntimeError):
                    run(run_paper_replay(c, bars, research_stack, "BTC/USDT",
                                         crash_store, checkpoint_every=7,
                                         fault_after_bars=10,
                                         dataset_version="ds1",
                                         dataset_hash="h1"))
                resumed = run(run_paper_replay(c, bars, research_stack,
                                               "BTC/USDT", crash_store,
                                               checkpoint_every=7,
                                               dataset_version="ds1",
                                               dataset_hash="h1"))
            finally:
                crash_store.close()
        self.assertEqual(resumed["fills"], ref["fills"])
        self.assertEqual(resumed["paper_net"], ref["paper_net"])
        self.assertEqual(resumed["equity_per_bar"], ref["equity_per_bar"])


# ---------------------------------------------------------------------------
# R03: full-promotion MECHANICS FIXTURE (not a real strategy).
# ---------------------------------------------------------------------------

PASS_METRICS = {"expectancy": 2.0, "profit_factor": 2.0, "max_drawdown": 0.05,
                "n_closed": 12, "win_rate": 0.67,
                "sharpe_5m_annualized": 1.0, "sharpe_annualized": 1.0,
                "gross_win": 24.0, "gross_loss": -12.0,
                "max_participation": 0.01, "net_pnl": 24.0,
                "final_equity": 10024.0, "turnover": 5000.0}


def _balanced_res(candles):
    """Fake backtest result with PnL spread across the trend regimes
    actually present in `candles`.

    Up to 4 trades of +2.0 per regime (12 total when all regimes are well
    represented, concentration ~1/3); thin regimes contribute what they
    have and the largest regime tops up to 10 trades so train/validation
    minimums hold on every slice. Regime concentration only gates the
    validation slice, which the fixture keeps balanced.
    """
    from research.regimes import label_series
    labels = label_series(candles)
    by_trend = {}
    for c, lbl in zip(candles, labels):
        by_trend.setdefault(lbl["trend"], []).append(c.timestamp.isoformat())
    if not by_trend:
        raise AssertionError("fixture needs non-empty candles")
    pnls, stamps = [], []
    for trend in sorted(by_trend):
        for ts in by_trend[trend][:4]:
            pnls.append(2.0)
            stamps.append(ts)
    if len(pnls) < 10:
        # Top up from the largest regime (train/WF slices only; the
        # validation slice is always balanced — see below).
        biggest = max(sorted(by_trend), key=lambda t: len(by_trend[t]))
        pool = by_trend[biggest]
        i = 0
        while len(pnls) < 10:
            pnls.append(2.0)
            stamps.append(pool[i % len(pool)])
            i += 1
    share = max(sum(1 for s, t in zip(stamps, _trend_of(stamps, by_trend))
                    if t == trend) for trend in by_trend) / len(stamps)
    if len(candles) >= 30 and share > 0.75:
        raise AssertionError(
            f"fixture validation slice too concentrated ({share:.2f})")
    return SimpleNamespace(trade_pnls=pnls, trade_exit_stamps=stamps)


def _trend_of(stamps, by_trend):
    rev = {}
    for trend, ts_list in by_trend.items():
        for ts in ts_list:
            rev.setdefault(ts, trend)
    return [rev[s] for s in stamps]


async def _fake_run_backtest(symbol, candles, strategy, stack_factory,
                             execution_model, warmup=None, costs=None):
    return _balanced_res(candles), dict(PASS_METRICS), 10000.0


async def _fake_cost_sweep(symbol, candles, stack_factory, scenarios,
                           execution_model=None, warmup_candles=None):
    out = []
    for sc in scenarios:
        stressed = sc.get("name", "") != "base"
        out.append({"scenario": sc.get("name", "?"),
                    "net_pnl": 30.0 if stressed else 40.0,
                    "profit_factor": 1.5 if stressed else 2.0,
                    "fees_paid": 5.0, "spread_paid": 1.0,
                    "slippage_paid": 1.0, "impact_paid": 0.0,
                    "fills": 12, "n_closed": 12,
                    "expectancy": 2.0, "max_drawdown": 0.05})
    return {"scenarios": out}


async def _fake_capacity(symbol, candles, stack_factory, mults,
                         execution_model=None, warmup_candles=None):
    levels = [{"multiplier": float(m), "capital": 10000.0 * float(m),
               "max_participation": 0.01, "net_pnl": 10.0,
               "turnover": 1000.0} for m in mults]
    return {"levels": levels,
            "summary": {"max_survivable_capital": 50000.0,
                        "max_survivable_multiplier": 5.0,
                        "levels_evaluated": len(levels)}}


def _fixture_campaign(cid, limit=2):
    from research.campaign import CampaignConfig
    from research.gates import GateConfig
    return CampaignConfig(
        campaign_id=cid, symbol="BTC/USDT", families=["momentum"],
        candidate_limit=limit, seed=7, n_splits=2, train_bars=40,
        test_bars=25, embargo_bars=5, warmup_bars=15,
        stability_neighbours=2, capacity_multipliers=[1, 5],
        allow_holdout=False, gates=GateConfig())


class TestR03FullPromotionFixture(unittest.TestCase):
    """MECHANICS FIXTURE. Controlled backtest/cost/capacity stand-ins let
    one candidate traverse GENERATED -> ... -> PAPER_CANDIDATE through the
    REAL gates, persistence, funnel and scoring. Not a real strategy."""

    def test_full_promotion_path(self):
        import research.campaign as camp
        from research.datasets import build_research_dataset
        from research.report import build_campaign_report
        from research.store import ResearchStore
        bars = cycling_bars(200, prefix="r03")
        ds = build_research_dataset(bars, "BTC/USDT", holdout_ratio=0.15)
        store = ResearchStore(":memory:")
        try:
            with mock.patch.object(camp, "_run_backtest",
                                   new=_fake_run_backtest), \
                 mock.patch("evaluation.run_cost_sweep",
                            new=_fake_cost_sweep), \
                 mock.patch("evaluation.estimate_capacity",
                            new=_fake_capacity):
                record = run(camp.run_campaign(
                    _fixture_campaign("r03pass"), ds, research_stack, store))
            rep = build_campaign_report(record, store)
        finally:
            store.close()
        f = record["funnel"]
        self.assertEqual(f["generated"], 2)
        for stage in ("train_evaluated", "validated", "cross_market",
                      "walk_forward_passed", "cost_stress_passed",
                      "regime_passed", "capacity_passed", "paper_candidate"):
            self.assertEqual(f[stage]["passed"], 2, stage)
            self.assertEqual(f[stage]["rejected"], 0, stage)
        self.assertEqual(len(record["promoted"]), 2)
        # Holdout respected: never opened without permission.
        self.assertEqual(record["holdout_accesses"], [])
        self.assertEqual(len(ds.holdout.access_log), 0)
        # Every evaluation persisted in stage order with correct transitions.
        from research.candidates import StrategyCandidate
        self.assertEqual(len(rep["candidates"]), 2)
        expected = ["train_evaluated", "validated", "walk_forward_passed",
                    "cost_stress_passed", "regime_passed", "capacity_passed",
                    "param_stability", "paper_candidate"]
        for c in rep["candidates"]:
            self.assertEqual(c["status"], "paper_candidate")
            self.assertEqual([e["stage"] for e in c["evaluations"]], expected)
            self.assertTrue(all(e["passed"] for e in c["evaluations"]))
            self.assertIsNotNone(c["research_score"])
        # Funnel reconciles exactly.
        accounted = f["paper_candidate"]["passed"] + sum(
            f[s]["rejected"] for s in f
            if isinstance(f[s], dict) and "rejected" in f[s])
        self.assertEqual(accounted + f.get("duplicates_skipped", 0)
                         + f.get("previously_evaluated", 0), f["generated"])

    def test_rejection_path_separately(self):
        import research.campaign as camp
        from research.datasets import build_research_dataset
        from research.store import ResearchStore

        async def _fail(symbol, candles, strategy, stack_factory,
                        execution_model, warmup=None, costs=None):
            m = dict(PASS_METRICS)
            m.update({"expectancy": 0.0, "profit_factor": 0.0,
                      "n_closed": 0, "net_pnl": 0.0})
            return SimpleNamespace(trade_pnls=[], trade_exit_stamps=[]), \
                m, 10000.0

        bars = cycling_bars(200, prefix="r03r")
        ds = build_research_dataset(bars, "BTC/USDT", holdout_ratio=0.15)
        store = ResearchStore(":memory:")
        try:
            with mock.patch.object(camp, "_run_backtest", new=_fail):
                record = run(camp.run_campaign(
                    _fixture_campaign("r03rej", limit=1), ds,
                    research_stack, store))
        finally:
            store.close()
        f = record["funnel"]
        self.assertEqual(f["train_evaluated"]["rejected"], 1)
        self.assertEqual(record["promoted"], [])
        self.assertEqual(len(ds.holdout.access_log), 0)


class TestR04SecondaryRegimeGate(unittest.TestCase):
    """Cross-market regime robustness: EVERY relevant market must obey the
    concentration policy; failures name the offending market."""

    def _case(self, primary_trends, secondary_trends):
        """Build (valid, res) with PnL distributed per trend regime.

        trend -> n trades of +2.0. Balanced across 3 trends ~= 1/3
        concentration; single-trend == 1.0 concentration.
        """
        from research.regimes import label_series
        valid = cycling_bars(60, prefix=f"rg{len(primary_trends)}")
        labels = label_series(valid)
        by_trend = {}
        for c, lbl in zip(valid, labels):
            by_trend.setdefault(lbl["trend"], []).append(
                c.timestamp.isoformat())

        def res_for(plan):
            pnls, stamps = [], []
            for trend, n in plan.items():
                for ts in by_trend[trend][:n]:
                    pnls.append(2.0)
                    stamps.append(ts)
            return SimpleNamespace(trade_pnls=pnls, trade_exit_stamps=stamps)

        return valid, res_for(primary_trends), res_for(secondary_trends)

    def _run_gate(self, primary_plan, secondary_plan):
        from research.campaign import _gate_regime, CampaignConfig
        from research.gates import GateConfig
        from research.generate import make_candidate
        from research.store import ResearchStore
        valid, res_p, res_s = self._case(primary_plan, secondary_plan)
        eth_valid = cycling_bars(60, prefix="rge")
        cand = make_candidate("momentum", {"momentum_period": 7,
                                           "momentum_threshold": 0.005}, 21)
        cfg = CampaignConfig(campaign_id="rg", symbol="BTC/USDT",
                             families=["momentum"], candidate_limit=1, seed=21,
                             gates=GateConfig())
        store = ResearchStore(":memory:")
        try:
            funnel = {"regime_passed": {"passed": 0, "rejected": 0}}
            out = run(_gate_regime(
                cfg, cand, store, funnel, valid, res_p, dict(PASS_METRICS),
                secondary={"ETH/USDT": {"valid_res": res_s,
                                        "valid": eth_valid}}))
            return out, cand, funnel, store.evaluations_for(
                cand.candidate_id, "rg")
        finally:
            store.close()

    def test_secondary_concentrated_rejects(self):
        out, cand, funnel, evs = self._run_gate(
            {"trending_up": 4, "trending_down": 4, "sideways": 4},
            {"trending_up": 12})
        self.assertIsNone(out)
        self.assertEqual(funnel["regime_passed"]["rejected"], 1)
        reason = cand.evaluations[-1]["reason"]
        self.assertIn("secondary:ETH/USDT", reason)
        self.assertTrue(reason.startswith("regime_dependent"))
        self.assertEqual(
            cand.evaluations[-1]["metrics"]["failed_market"], "ETH/USDT")

    def test_primary_concentrated_rejects(self):
        out, cand, funnel, evs = self._run_gate(
            {"trending_up": 12},
            {"trending_up": 4, "trending_down": 4, "sideways": 4})
        self.assertIsNone(out)
        self.assertEqual(funnel["regime_passed"]["rejected"], 1)
        self.assertIn("primary", cand.evaluations[-1]["reason"])
        self.assertEqual(
            cand.evaluations[-1]["metrics"]["failed_market"], "primary")

    def test_all_compliant_passes(self):
        out, cand, funnel, evs = self._run_gate(
            {"trending_up": 4, "trending_down": 4, "sideways": 4},
            {"trending_up": 4, "trending_down": 4, "sideways": 4})
        self.assertIsNotNone(out)
        self.assertEqual(funnel["regime_passed"]["passed"], 1)
        self.assertTrue(any(e["stage"] == "regime_passed" for e in evs))


class TestR05StressSelection(unittest.TestCase):
    """The stressed gate scenario is the MAXIMUM configured multiplier —
    never a hardcoded x5."""

    def _run_costs(self, multipliers):
        from research.campaign import _gate_costs, CampaignConfig
        from research.generate import make_candidate
        from research import strategies as fam
        from research.store import ResearchStore
        bars = tiny_bars(60, step=0.5, prefix=f"c{len(multipliers)}")
        cand = make_candidate("momentum", {"momentum_period": 7,
                                           "momentum_threshold": 0.005}, 3)
        strategy = fam.build_strategy(cand.family, cand.params)
        cfg = CampaignConfig(campaign_id=f"cs{len(multipliers)}",
                             symbol="BTC/USDT", families=["momentum"],
                             candidate_limit=1, seed=3,
                             cost_multipliers=list(multipliers))
        store = ResearchStore(":memory:")
        try:
            funnel = {"cost_stress_passed": {"passed": 0, "rejected": 0}}
            run(_gate_costs(cfg, None, cand, strategy, research_stack,
                            bars, store, funnel, None))
            return cand.evaluations[-1]["metrics"]
        finally:
            store.close()

    def test_pair_uses_x2(self):
        m = self._run_costs([1, 2])
        self.assertEqual(m["stressed_scenario"], "x2")
        self.assertEqual(m["stressed_multiplier"], 2.0)
        by_name = {r["scenario"]: r for r in m["matrix"]}
        self.assertAlmostEqual(m["stressed_net"], by_name["x2"]["net"])

    def test_triple_uses_x3(self):
        m = self._run_costs([1, 1.5, 3])
        self.assertEqual(m["stressed_scenario"], "x3")
        self.assertEqual(m["stressed_multiplier"], 3.0)
        by_name = {r["scenario"]: r for r in m["matrix"]}
        self.assertAlmostEqual(m["stressed_net"], by_name["x3"]["net"])

    def test_full_matrix_still_uses_x5(self):
        m = self._run_costs([1, 1.5, 2, 3, 5])
        self.assertEqual(m["stressed_scenario"], "x5")
        self.assertEqual(m["stressed_multiplier"], 5.0)


class TestR06ManifestGlob(unittest.TestCase):
    """Quoted manifest globs expand in Python (Windows-safe)."""

    def _write_raw(self, path, symbol, start_ms, n=30):
        rows = [[start_ms + i * 3600000,
                 str(100 + i), str(101 + i), str(99 + i),
                 str(100.5 + i), "10"] for i in range(n)]
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"symbol": symbol, "interval": "1h",
                       "source": "test", "rows": rows}, f)

    def test_manifest_glob_expands(self):
        import argparse
        from scripts.falcon import load_dataset
        from research.datafiles import build_versioned_dataset
        with tempfile.TemporaryDirectory() as d:
            for sym in ("BTCUSDT", "ETHUSDT"):
                raw = os.path.join(d, f"BINANCE_{sym}_1h.json")
                self._write_raw(raw, sym, 1704067200000)
                build_versioned_dataset(
                    raw, os.path.join(d, f"v-{sym}-1h.csv"),
                    os.path.join(d, f"v-{sym}-1h.manifest.json"))
            args = SimpleNamespace(
                data=[os.path.join(d, "v-*-1h.manifest.json")],
                symbol="BTC/USDT", interval="1h")
            candles, stats = load_dataset(args)
            self.assertEqual(len(stats["manifests"]), 2)
            self.assertEqual(len(candles), 60)

    def test_manifest_glob_no_match_dies(self):
        from scripts.falcon import load_dataset
        args = SimpleNamespace(
            data=["data/does-not-exist-*.manifest.json"],
            symbol="BTC/USDT", interval="5m")
        with self.assertRaises(SystemExit) as cm:
            load_dataset(args)
        self.assertNotEqual(cm.exception.code, 0)


class TestR08HoldoutSemantics(unittest.TestCase):
    """Policy A: sealed in research, multi-open allowed in final mode with
    every access logged AND persisted at access time."""

    def test_repeated_final_opens_all_logged(self):
        from research.datasets import build_research_dataset, FINAL_MODE
        bars = tiny_bars(60, prefix="r08")
        ds = build_research_dataset(bars, "BTC/USDT", holdout_ratio=0.2)
        with self.assertRaises(PermissionError):
            ds.holdout.open_holdout("research", "c", "sneaky")
        with self.assertRaises(ValueError):
            ds.holdout.open_holdout(FINAL_MODE, "c", "")
        ds.holdout.open_holdout(FINAL_MODE, "c1", "first")
        ds.holdout.open_holdout(FINAL_MODE, "c1", "second")
        self.assertEqual(len(ds.holdout.access_log), 2)
        self.assertEqual([r.reason for r in ds.holdout.access_log],
                         ["first", "second"])

    def test_access_persisted_across_restart(self):
        from research.campaign import _make_holdout_access_cb
        from research.datasets import build_research_dataset, FINAL_MODE
        from research.store import ResearchStore
        bars = tiny_bars(60, prefix="r08p")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "h.db")
            store = ResearchStore(path)
            try:
                store.save_campaign({"campaign_id": "hc", "state": "in_progress",
                                     "holdout_accesses": []})
                ds = build_research_dataset(bars, "BTC/USDT",
                                            holdout_ratio=0.2)
                external = []
                ds.attach_access_logger(
                    _make_holdout_access_cb(store, "hc", "BTC/USDT",
                                            external.append))
                ds.holdout.open_holdout(FINAL_MODE, "hc", "final check")
                self.assertEqual(len(external), 1)
                store.close()
            finally:
                try:
                    store.close()
                except Exception:
                    pass
            # Simulate a crash: reopen and verify the access survived.
            store2 = ResearchStore(path)
            try:
                rec = store2.load_campaign("hc")
                self.assertEqual(len(rec["holdout_accesses"]), 1)
                self.assertEqual(rec["holdout_accesses"][0]["reason"],
                                 "final check")
                self.assertEqual(rec["holdout_accesses"][0]["symbol"],
                                 "BTC/USDT")
            finally:
                store2.close()


class TestR10SimulatedTime(unittest.TestCase):
    """Quantitative risk behavior follows injected bar time, never the
    wall clock. Wall clock remains allowed for result metadata only."""

    def test_backtest_threads_bar_timestamps_into_risk(self):
        from backtest import BacktestEngine
        from config import ConfigManager
        from market_data import MarketDataHandler
        from exchange import PaperExchange
        from portfolio import PortfolioManager
        from strategy import SignalType
        seen = []

        class CapRisk:
            def evaluate_trade(self, symbol, quantity, price, equity,
                               now=None):
                seen.append(("eval", now))
                return True, "ok"

            def record_trade_result(self, success, pnl=0.0, equity=None,
                                    now=None):
                seen.append(("record", now))

            def check_stop_take(self, entry, px, side):
                return False, ""

            def calculate_position_size(self, price, equity,
                                        stop_distance_pct=None):
                return 1.0

            def update_peak(self, equity):
                pass

            def get_risk_metrics(self):
                from risk import RiskMetrics
                return RiskMetrics()

        class AlwaysLong:
            def generate_signal(self, symbol, market_data):
                from strategy import TradingSignal
                from datetime import datetime, timezone
                candles = market_data.get("candles", [])
                if len(candles) < 6:
                    return None
                return TradingSignal(
                    symbol=symbol, signal_type=SignalType.LONG,
                    confidence=1.0, timestamp=datetime.now(timezone.utc),
                    price=candles[-1].close, signal_id="x")

        cfg = ConfigManager()
        cfg.set("simulated_latency_ms", 0)
        md = MarketDataHandler(cfg)
        ex = PaperExchange(cfg, seed=11)
        run(ex.connect())
        try:
            pf = PortfolioManager(cfg)
            from execution import ExecutionEngine
            eng = ExecutionEngine(cfg, ex, CapRisk())
            bt = BacktestEngine(cfg, md, eng, pf, CapRisk(), AlwaysLong())
            bars = tiny_bars(40, prefix="r10")
            run(bt.run("BTC/USDT", list(bars)))
        finally:
            run(ex.disconnect())
        self.assertGreater(len(seen), 0)
        stamps = {c.timestamp for c in bars}
        for kind, now in seen:
            self.assertIn(now, stamps, kind)

    def test_risk_day_and_cooldown_follow_injected_now(self):
        from config import ConfigManager
        from risk import RiskManager
        cfg = ConfigManager()
        cfg.set("max_daily_loss", 5.0)
        cfg.set("max_consecutive_losses", 2)
        cfg.set("stop_trading_cooldown_minutes", 30)
        risk = RiskManager(cfg)
        day1 = datetime(2024, 3, 1, 23, 59, tzinfo=timezone.utc)
        day2 = datetime(2024, 3, 2, 0, 1, tzinfo=timezone.utc)
        # Two day-1 losses trip cooldown and consume most of the daily budget.
        risk.record_trade_result(False, pnl=-4.0, equity=10000.0, now=day1)
        risk.record_trade_result(False, pnl=-1.0, equity=9995.0, now=day1)
        self.assertFalse(risk.check_cooldown(now=day1))
        self.assertFalse(risk.check_cooldown(
            now=day1 + timedelta(minutes=10)))
        self.assertTrue(risk.check_cooldown(
            now=day1 + timedelta(minutes=40)))
        # Day boundary rolls on bar time: the same-sized loss is allowed
        # on day 2 despite day 1 being exhausted.
        self.assertTrue(risk.check_daily_loss_limit(4.0, now=day2))
        risk.record_trade_result(False, pnl=-4.0, equity=9990.0, now=day2)
        self.assertFalse(risk.check_daily_loss_limit(4.0, now=day2))


if __name__ == "__main__":
    unittest.main()
