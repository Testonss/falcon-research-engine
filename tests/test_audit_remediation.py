"""Audit-remediation regression tests (B01-B09 + warnings).

Each test reproduces an original audit failure and proves the fix.
Small synthetic data only; no tuning; fast by design.
"""
import asyncio
import csv
import math
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta


def run(coro):
    return asyncio.run(coro)


def tiny_bars(n=150, start=100.0, step=0.4, prefix="ar", symbol="BTC/USDT",
              volume=5000.0, interval="5m", minutes=5):
    from market_data import Candle
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [Candle(symbol=symbol, open=start + i * step,
                   high=start + i * step + 0.2, low=start + i * step - 0.2,
                   close=start + i * step + 0.05, volume=volume,
                   timestamp=base + timedelta(minutes=minutes * i),
                   interval=interval,
                   event_id=f"{prefix}{i}") for i in range(n)]


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


class TestB01Timeframe(unittest.TestCase):
    def test_4h_resample_is_exactly_4h(self):
        # B01: datasets labelled 4h were aggregated at 6h. Time buckets must
        # give exact 4h spacing on an aligned grid.
        from research.datafiles import resample_ohlcv, INTERVAL_SECONDS
        self.assertEqual(INTERVAL_SECONDS["4h"], 14400)
        self.assertEqual(INTERVAL_SECONDS["6h"], 21600)
        bars = tiny_bars(25, interval="1h", minutes=60, prefix="b01")
        out, stats = resample_ohlcv(bars, 3600, 14400, "4h")
        self.assertGreater(len(out), 1)
        for b in out:
            self.assertEqual(int(b.timestamp.timestamp()) % 14400, 0)
            self.assertEqual(b.interval, "4h")
        for a, b in zip(out, out[1:]):
            self.assertAlmostEqual(
                (b.timestamp - a.timestamp).total_seconds(), 14400.0)
        # Rebuilt 4h dataset on disk (if present) must also be 4h-spaced.
        p = "data/falcon-BTCUSDT-4h.csv"
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                r = csv.DictReader(f)
                ts = [row["timestamp"] for _, row in zip(range(6), r)]
            dts = [datetime.fromisoformat(t) for t in ts]
            for a, b in zip(dts, dts[1:]):
                self.assertAlmostEqual(
                    (b - a).total_seconds(), 14400.0, places=3)


class TestB02EntryCommission(unittest.TestCase):
    def test_entry_fee_in_realized(self):
        # B02: realized/net PnL omitted entry commissions.
        from config import ConfigManager
        from portfolio import PortfolioManager
        cfg = ConfigManager()
        cfg.set("initial_cash", 100000.0)
        pf = PortfolioManager(cfg)
        pf.update_position("BTC/USDT", 1.0, 50000.0, is_buy=True,
                           order_id="b02e", fee=100.0)
        pf.update_position("BTC/USDT", 1.0, 55000.0, is_buy=False,
                           order_id="b02x", fee=50.0)
        self.assertAlmostEqual(pf.realized_pnl, 4850.0)
        self.assertAlmostEqual(pf.get_cash(), 104850.0)


class TestB03RegimeGate(unittest.TestCase):
    def test_regime_gate_uses_real_concentration(self):
        # B03: regime promotion gate must use real attribution values.
        from research import gates
        from research.gates import GateConfig
        from research.generate import make_candidate
        c = make_candidate("momentum", {"momentum_period": 14,
                                        "momentum_threshold": 0.01}, 1)
        good = {"validation": {"expectancy": 1.0, "gross_win": 50.0,
                               "gross_loss": -50.0},
                "trending_up": {"expectancy": 1.0, "gross_win": 50.0,
                                "gross_loss": -50.0},
                "sideways": {"expectancy": 1.0, "gross_win": 50.0,
                             "gross_loss": -50.0}}
        ok, _ = gates.gate_regime(c, good, GateConfig())
        self.assertTrue(ok)
        bad = {"validation": {"expectancy": 1.0, "gross_win": 10.0,
                              "gross_loss": -10.0},
               "trending_up": {"expectancy": 5.0, "gross_win": 100.0,
                               "gross_loss": 0.0},
               "sideways": {"expectancy": -5.0, "gross_win": 0.0,
                            "gross_loss": -90.0}}
        ok2, reason = gates.gate_regime(c, bad, GateConfig())
        self.assertFalse(ok2)
        self.assertTrue(reason.startswith("regime_dependent"))

    def test_campaign_regime_stage_persists_and_counts(self):
        # Promotion-path integration: _gate_regime writes an evaluation and
        # bumps the funnel (no silent pass/fail).
        from research.campaign import _gate_regime, CampaignConfig
        from research.generate import make_candidate
        from research.store import ResearchStore
        from research.datasets import build_research_dataset
        bars = tiny_bars(80, prefix="b03")
        ds = build_research_dataset(bars, "BTC/USDT", holdout_ratio=0.15)
        research = sorted(ds.research_bars, key=lambda c: c.timestamp)
        n = len(research)
        train, valid = research[: int(n * 0.6)], research[int(n * 0.6): int(n * 0.8)]
        cand = make_candidate("momentum", {"momentum_period": 7,
                                           "momentum_threshold": 0.005}, 9)
        cfg = CampaignConfig(campaign_id="b03c", symbol="BTC/USDT",
                             families=["momentum"], candidate_limit=1, seed=9)
        store = ResearchStore(":memory:")
        try:
            from research import strategies as fam
            from research.campaign import _run_backtest, _stack_factory_with
            strategy = fam.build_strategy(cand.family, cand.params)
            res, m, _ = run(_run_backtest("BTC/USDT", list(train), strategy,
                                          _stack_factory_with(research_stack),
                                          "next_close", warmup=None))
            warmup = train[-cfg.warmup_bars:] if train else []
            res_v, m_v, _ = run(_run_backtest(
                "BTC/USDT", list(valid), strategy,
                _stack_factory_with(research_stack), "next_close",
                warmup=warmup))
            funnel = {"regime_passed": {"passed": 0, "rejected": 0}}
            out = run(_gate_regime(cfg, cand, store, funnel, valid, res_v,
                                   m_v, secondary=None))
            evs = store.evaluations_for(cand.candidate_id, "b03c")
            self.assertTrue(any(e["stage"] == "regime_passed" for e in evs))
            self.assertEqual(
                funnel["regime_passed"]["passed"]
                + funnel["regime_passed"]["rejected"], 1)
            self.assertEqual(out is not None,
                             funnel["regime_passed"]["passed"] == 1)
        finally:
            store.close()


class TestB04CostCapacity(unittest.TestCase):
    def test_cost_scenarios_use_candidate_and_effective_costs(self):
        # B04: cost stress must run the candidate (not the default strategy)
        # with pre-construction cost overrides; fees must rise with multiplier.
        from research.campaign import _gate_costs, CampaignConfig
        from research.generate import make_candidate
        from research import strategies as fam
        from research.store import ResearchStore
        bars = tiny_bars(60, step=0.5, prefix="b04")
        cand = make_candidate("momentum", {"momentum_period": 7,
                                           "momentum_threshold": 0.005}, 3)
        strategy = fam.build_strategy(cand.family, cand.params)
        cfg = CampaignConfig(campaign_id="b04c", symbol="BTC/USDT",
                             families=["momentum"], candidate_limit=1, seed=3,
                             cost_multipliers=[1, 2])
        store = ResearchStore(":memory:")
        try:
            funnel = {"cost_stress_passed": {"passed": 0, "rejected": 0}}
            run(_gate_costs(cfg, None, cand, strategy, research_stack,
                            bars, store, funnel, None))
            matrix = cand.evaluations[-1]["metrics"]["matrix"]
            by_name = {m["scenario"]: m for m in matrix}
            self.assertGreater(by_name["base"]["fills"], 0)
            self.assertGreater(by_name["x2"]["fees"], by_name["base"]["fees"])
        finally:
            store.close()

    def test_capacity_scales_capital(self):
        from research.campaign import _gate_capacity, CampaignConfig
        from research.generate import make_candidate
        from research.store import ResearchStore
        bars = tiny_bars(60, step=0.5, prefix="b04cap")
        cand = make_candidate("momentum", {"momentum_period": 7,
                                           "momentum_threshold": 0.005}, 13)
        cfg = CampaignConfig(campaign_id="b04cap", symbol="BTC/USDT",
                             families=["momentum"], candidate_limit=1, seed=13,
                             capacity_multipliers=[1, 2])
        store = ResearchStore(":memory:")
        try:
            funnel = {"capacity_passed": {"passed": 0, "rejected": 0}}
            run(_gate_capacity(cfg, cand, research_stack, bars, store,
                               funnel, None))
            levels = cand.evaluations[-1]["metrics"]["levels"]
            by_mult = {lv["multiplier"]: lv for lv in levels}
            self.assertAlmostEqual(by_mult[2.0]["capital"],
                                   2.0 * by_mult[1.0]["capital"])
            self.assertGreater(by_mult[2.0]["turnover"],
                               by_mult[1.0]["turnover"])
        finally:
            store.close()


class TestB05CrashRecovery(unittest.TestCase):
    def test_incomplete_experiment_redone_not_skipped(self):
        # B05: crash recovery must never permanently skip incomplete
        # candidates; a rerun reproduces the same evaluations.
        from research.campaign import CampaignConfig, run_campaign
        from research.datasets import build_research_dataset
        from research.store import ResearchStore
        bars = tiny_bars(150, prefix="b05")
        ds = build_research_dataset(bars, "BTC/USDT", holdout_ratio=0.15)

        def make_cfg(cid):
            return CampaignConfig(campaign_id=cid, symbol="BTC/USDT",
                                  families=["momentum"], candidate_limit=3,
                                  seed=7, n_splits=1, train_bars=40,
                                  test_bars=25, warmup_bars=15,
                                  stability_neighbours=2,
                                  capacity_multipliers=[1, 5],
                                  allow_holdout=False)

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b05.db")
            store = ResearchStore(path)
            try:
                r1 = run(run_campaign(make_cfg("b05a"), ds, research_stack,
                                      store))
                n1 = sum(len(store.evaluations_for(c["candidate_id"], "b05a"))
                         for c in store.list_candidates())
                # Simulate a crash: mark one completed experiment incomplete
                # with its evaluations deleted (partial attempt).
                exps = store.list_experiments("b05a")
                self.assertGreater(len(exps), 0)
                victim = exps[0]["candidate_id"]
                store.delete_evaluations(victim, "b05a")
                store.upsert_experiment("b05a", victim, exps[0]["fingerprint"],
                                        "validated", "in_progress")
                # Rerunning the SAME campaign must redo the victim (more evals
                # for this campaign than the crashed state had).
                n_crashed = sum(
                    len(store.evaluations_for(c["candidate_id"], "b05a"))
                    for c in store.list_candidates())
                self.assertLess(n_crashed, n1)
                r2 = run(run_campaign(make_cfg("b05a"), ds, research_stack,
                                      store))
                n2 = sum(len(store.evaluations_for(c["candidate_id"], "b05a"))
                         for c in store.list_candidates())
                self.assertEqual(n2, n1)
                self.assertEqual(r2["funnel"]["generated"],
                                 r1["funnel"]["generated"])
            finally:
                store.close()


class TestB06Identity(unittest.TestCase):
    def test_experiment_fp_covers_protocol(self):
        from research.candidates import experiment_fingerprint
        def fp(strategy="s", datasets=None, splits=None, holdout=0.15,
               model="next_close", costs=None, gates=None, proto="1", seed=7):
            return experiment_fingerprint(
                strategy, datasets or {"BTC/USDT": "h"},
                splits or {"train_ratio": 0.6}, holdout, model,
                costs or {"taker_fee": 0.002}, gates or {"min_trades": 10},
                proto, seed)
        a = fp()
        b = fp(costs={"taker_fee": 0.01})
        c = fp(seed=8)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a, fp())

    def test_report_isolation(self):
        from research.campaign import CampaignConfig, run_campaign
        from research.datasets import build_research_dataset
        from research.report import build_campaign_report
        from research.store import ResearchStore
        bars = tiny_bars(150, prefix="b06")
        ds = build_research_dataset(bars, "BTC/USDT", holdout_ratio=0.15)

        def make_cfg(cid):
            return CampaignConfig(campaign_id=cid, symbol="BTC/USDT",
                                  families=["momentum"], candidate_limit=2,
                                  seed=7, n_splits=1, train_bars=40,
                                  test_bars=25, warmup_bars=15,
                                  stability_neighbours=2,
                                  capacity_multipliers=[1, 5],
                                  allow_holdout=False)

        with tempfile.TemporaryDirectory() as d:
            store = ResearchStore(os.path.join(d, "b06.db"))
            try:
                r1 = run(run_campaign(make_cfg("iso1"), ds, research_stack,
                                      store))
                r2 = run(run_campaign(make_cfg("iso2"), ds, research_stack,
                                      store))
                # Same experiment configuration: run2 reuses run1's completed
                # experiments by EXPERIMENT identity (no re-evaluation).
                self.assertEqual(r2["funnel"]["previously_evaluated"], 2)
                rep1 = build_campaign_report(r1, store)
                rep2 = build_campaign_report(r2, store)
                # Report isolation: skipped work leaves no evaluation rows
                # tagged with the new campaign, so its report is empty and
                # run1's report contains only run1's evaluations.
                self.assertEqual(len(rep2["candidates"]), 0)
                self.assertEqual(len(rep1["candidates"]), 2)
                for c in rep1["candidates"]:
                    stages = [e["stage"] for e in c["evaluations"]]
                    self.assertGreater(len(stages), 0)
            finally:
                store.close()


class TestB07PaperRecovery(unittest.TestCase):
    def test_restart_returns_completed_as_is(self):
        from research.generate import make_candidate
        from research.paper import run_paper_replay
        from research.store import ResearchStore
        bars = tiny_bars(70, step=0.5, prefix="b07")
        c = make_candidate("momentum", {"momentum_period": 7,
                                        "momentum_threshold": 0.005}, 2)
        with tempfile.TemporaryDirectory() as d:
            store = ResearchStore(os.path.join(d, "b07.db"))
            try:
                s1 = run(run_paper_replay(c, bars, research_stack, "BTC/USDT",
                                          store))
                self.assertGreater(s1["n_fills"], 0)
                s2 = run(run_paper_replay(c, bars, research_stack, "BTC/USDT",
                                          store))
                self.assertEqual(s2["fills"], s1["fills"])
                self.assertEqual(s2["paper_net"], s1["paper_net"])
                self.assertEqual(s2["final_equity"], s1["final_equity"])
                self.assertEqual(s2["bar_stamps"], s1["bar_stamps"])
            finally:
                store.close()

    def test_crash_resume_equals_uninterrupted(self):
        from research.generate import make_candidate
        from research.paper import run_paper_replay
        from research.store import ResearchStore
        bars = tiny_bars(70, step=0.5, prefix="b07c")
        c = make_candidate("momentum", {"momentum_period": 7,
                                        "momentum_threshold": 0.005}, 2)
        with tempfile.TemporaryDirectory() as d:
            ref_store = ResearchStore(os.path.join(d, "ref.db"))
            try:
                ref = run(run_paper_replay(c, bars, research_stack, "BTC/USDT",
                                           ref_store, checkpoint_every=7))
            finally:
                ref_store.close()
            crash_store = ResearchStore(os.path.join(d, "crash.db"))
            try:
                with self.assertRaises(RuntimeError):
                    run(run_paper_replay(c, bars, research_stack, "BTC/USDT",
                                         crash_store, checkpoint_every=7,
                                         fault_after_bars=10))
                resumed = run(run_paper_replay(c, bars, research_stack,
                                               "BTC/USDT", crash_store,
                                               checkpoint_every=7))
            finally:
                crash_store.close()
        self.assertEqual(resumed["fills"], ref["fills"])
        self.assertEqual(resumed["paper_net"], ref["paper_net"])
        self.assertEqual(resumed["equity_per_bar"], ref["equity_per_bar"])


class TestB08Sharpe(unittest.TestCase):
    def test_timeframe_aware(self):
        from evaluation import sharpe_annualized
        import math
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        curve = [10000.0 + i * 3.0 + (10.0 if i % 2 else -4.0)
                 for i in range(40)]
        s5 = sharpe_annualized(
            curve, equity_stamps=[(base + timedelta(minutes=5 * i)).isoformat()
                                 for i in range(40)])
        s1 = sharpe_annualized(
            curve, equity_stamps=[(base + timedelta(hours=i)).isoformat()
                                 for i in range(40)])
        self.assertAlmostEqual(s5 / s1, math.sqrt(12.0), places=6)

    def test_report_uses_timeframe_aware_value(self):
        from backtest import BacktestResult
        from evaluation import compute_report_metrics, sharpe_annualized
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        curve = [10000.0 + i for i in range(20)]
        stamps = [(base + timedelta(hours=i)).isoformat() for i in range(20)]
        r = BacktestResult(trade_pnls=[1.0, 2.0], realized_pnl=3.0,
                           equity_curve=curve, equity_stamps=stamps)
        m = compute_report_metrics(r, 10000.0)
        self.assertAlmostEqual(m["sharpe_annualized"],
                               sharpe_annualized(curve, equity_stamps=stamps))
        self.assertAlmostEqual(m["sharpe_5m_annualized"],
                               m["sharpe_annualized"])

    def test_nonfinite_sharpe_is_zero(self):
        from evaluation import sharpe_from_equity, sharpe_annualized
        self.assertEqual(sharpe_from_equity([10000.0, float("nan"), 10001.0]), 0.0)
        self.assertEqual(sharpe_from_equity([10000.0, float("inf"), 10001.0]), 0.0)
        self.assertEqual(sharpe_annualized([10000.0, float("nan"), 10001.0],
                                           equity_stamps=[]), 0.0)


class TestB09CLI(unittest.TestCase):
    def test_entry_point_declared(self):
        with open("pyproject.toml", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("[project.scripts]", text)
        self.assertIn('falcon = "scripts.falcon:main"', text)
        self.assertIn('"scripts"', text)

    def test_help_exits_zero(self):
        import scripts.falcon as falcon
        with self.assertRaises(SystemExit) as cm:
            falcon.main(["--help"])
        self.assertEqual(cm.exception.code, 0)


class TestWarnings(unittest.TestCase):
    def test_csv_rejects_nan_inf(self):
        from market_data import load_ohlcv_csv
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         encoding="utf-8") as f:
            f.write("timestamp,open,high,low,close,volume\n")
            f.write("2024-01-01T00:00:00+00:00,100,101,99,100.5,1000\n")
            f.write("2024-01-01T00:05:00+00:00,nan,101,99,100.5,1000\n")
            f.write("2024-01-01T00:10:00+00:00,100,101,99,inf,1000\n")
            f.write("2024-01-01T00:15:00+00:00,100,101,99,101,nan\n")
            path = f.name
        try:
            loaded, stats = load_ohlcv_csv(path)
            self.assertEqual(stats["loaded"], 1)
            self.assertEqual(stats["skipped_bad"], 3)
        finally:
            os.unlink(path)

    def test_ingest_rejects_nan_inf(self):
        from config import ConfigManager
        from market_data import MarketDataHandler, Candle, Ticker
        md = MarketDataHandler(ConfigManager())
        base = datetime.now(timezone.utc)
        bad = Candle(symbol="BTC/USDT", open=float("nan"), high=1, low=1,
                     close=1, volume=1, timestamp=base, interval="5m",
                     event_id="nan1")
        self.assertEqual(md.ingest_candles("BTC/USDT", "5m", [bad]), [])
        badt = Ticker(symbol="BTC/USDT", price=float("inf"), volume=1,
                      timestamp=base, event_id="inf1")
        self.assertFalse(md.ingest_ticker(badt))

    def test_gates_reject_nan_inf(self):
        from research.gates import gate_validation, GateConfig
        from research.generate import make_candidate
        c = make_candidate("momentum", {"momentum_period": 14,
                                        "momentum_threshold": 0.01}, 1)
        good = {"expectancy": 1.0, "profit_factor": 1.5, "max_drawdown": 0.05,
                "n_closed": 20, "win_rate": 0.6, "sharpe_5m_annualized": 1.0,
                "gross_win": 100.0, "gross_loss": -50.0}
        for key, bad_val in (("expectancy", float("nan")),
                             ("expectancy", float("inf")),
                             ("max_drawdown", float("nan")),
                             ("win_rate", float("inf"))):
            m = dict(good)
            m[key] = bad_val
            ok, reason = gate_validation(c, dict(good), m, GateConfig())
            self.assertFalse(ok)
            self.assertTrue(reason.startswith("unrealistic_performance"))
        # Legit +inf profit factor (zero losses) still passes.
        m = dict(good)
        m["profit_factor"] = float("inf")
        ok, _ = gate_validation(c, dict(good), m, GateConfig())
        self.assertTrue(ok)

    def test_funnel_reconciles_with_stability(self):
        from research.campaign import CampaignConfig, run_campaign
        from research.datasets import build_research_dataset
        from research.store import ResearchStore
        bars = tiny_bars(150, prefix="wfun")
        ds = build_research_dataset(bars, "BTC/USDT", holdout_ratio=0.15)
        cfg = CampaignConfig(campaign_id="wfun", symbol="BTC/USDT",
                             families=["momentum", "breakout"],
                             candidate_limit=4, seed=7, n_splits=1,
                             train_bars=40, test_bars=25, warmup_bars=15,
                             stability_neighbours=2,
                             capacity_multipliers=[1, 5], allow_holdout=False)
        store = ResearchStore(":memory:")
        try:
            record = run(run_campaign(cfg, ds, research_stack, store))
        finally:
            store.close()
        f = record["funnel"]
        accounted = f["paper_candidate"]["passed"] + sum(
            f[s]["rejected"] for s in f
            if isinstance(f[s], dict) and "rejected" in f[s])
        self.assertEqual(
            accounted + f.get("duplicates_skipped", 0)
            + f.get("previously_evaluated", 0), f["generated"])
        self.assertEqual(len(ds.holdout.access_log), 0)

    def test_determinism(self):
        from research.generate import random_search
        a = random_search(["momentum", "breakout"], 6, seed=11)
        b = random_search(["momentum", "breakout"], 6, seed=11)
        self.assertEqual([c.candidate_id for c in a],
                         [c.candidate_id for c in b])


if __name__ == "__main__":
    unittest.main()
