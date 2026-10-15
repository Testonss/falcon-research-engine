#!/usr/bin/env python3
"""Falcon Research Engine — autonomous quantitative research CLI.

Paper/simulation only. Real-money trading stays disabled by architecture.

  falcon research --data "data/sample_ohlcv*.csv" --limit 16
  falcon backtest --strategy-json examples/strategy.json --data data/sample_ohlcv.csv
  falcon validate --db falcon.db --candidate-id cand_xxx --data data/sample_ohlcv.csv
  falcon report --db falcon.db --campaign-id camp_xxx
  falcon paper --db falcon.db --candidate-id cand_xxx --data data/sample_ohlcv.csv
  falcon status --db falcon.db

Config files are JSON (stdlib only — no YAML dependency by design).
"""
import argparse
import asyncio
import glob
import json
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import ConfigManager
from market_data import MarketDataHandler, load_ohlcv_dataset
from exchange import PaperExchange
from execution import ExecutionEngine
from risk import RiskManager
from portfolio import PortfolioManager
from backtest import BacktestEngine
from strategy import StrategyManager
from evaluation import compute_report_metrics
from research.store import ResearchStore
from research.candidates import CandidateStatus
from research import strategies as fam
from research.campaign import CampaignConfig, run_campaign, SingleStrategyManager
from research.datasets import build_research_dataset
from research.gates import GateConfig
from research.report import build_campaign_report, terminal_summary, write_campaign_reports


def die(msg: str, code: int = 2):
    print(f"falcon: error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def configure_logging(args) -> str:
    """Set the root log level for this CLI invocation.

    Default is WARNING: routine simulation events (risk blocks, fills,
    connects) stay silent and terminal output remains concise. `--verbose`
    enables INFO (campaign progress, backtest summaries); `--log-level`
    selects any level explicitly (DEBUG shows execution internals).
    Real errors always surface (WARNING+ is never suppressed).
    Returns the effective level name.
    """
    level = getattr(args, "log_level", None) or (
        "INFO" if getattr(args, "verbose", False) else "WARNING")
    logging.basicConfig(
        level=getattr(logging, level, logging.WARNING),
        format="falcon %(levelname)s %(name)s: %(message)s")
    # basicConfig is a no-op when handlers already exist (embedding);
    # set the level explicitly so the flags always take effect.
    logging.getLogger().setLevel(getattr(logging, level, logging.WARNING))
    return level


def base_stack_factory():
    def build(costs=None):
        cfg = ConfigManager()
        cfg.set('simulated_latency_ms', 0)
        # Research harness: timeframe-agnostic volume/volatility floors so all
        # symbols are treated identically (candidate-level filters stay opt-in).
        cfg.set('min_volume', 0.0)
        cfg.set('volatility_max', 1.0)
        for k, v in (costs or {}).items():
            cfg.set(k, v)
        md = MarketDataHandler(cfg)
        ex = PaperExchange(cfg, seed=int(cfg.get('random_seed', 42)))
        risk = RiskManager(cfg)
        pf = PortfolioManager(cfg)
        eng = ExecutionEngine(cfg, ex, risk)
        strat = StrategyManager(cfg)
        bt = BacktestEngine(cfg, md, eng, pf, risk, strat)
        return cfg, md, ex, risk, pf, eng, strat, bt
    return build


def load_dataset(args):
    """Load candles from CSV globs and/or versioned manifests.

    Both CSV paths and manifest paths accept cross-platform Python glob
    patterns (expanded here with stdlib `glob`, sorted for determinism),
    so quoted patterns like "data/falcon-*1h.manifest.json" work on
    Windows, Linux and macOS without shell expansion. Manifests
    (.manifest.json) are hash-verified before use; plain CSVs go through
    the standard loader. Returns (candles, stats) where stats notes
    manifests used (with dataset ids) for provenance.
    """
    manifest_pats = [p for p in (args.data or []) if p.endswith('.manifest.json')]
    csv_pats = [p for p in (args.data or []) if not p.endswith('.manifest.json')]
    manifests = []
    for pat in manifest_pats:
        matches = sorted(glob.glob(pat))
        if matches:
            for m in matches:
                if m not in manifests:
                    manifests.append(m)
        elif os.path.exists(pat):
            manifests.append(pat)
        else:
            die(f"no manifest files match: {pat}")
    candles, manifests_used = [], []
    if manifests:
        from research.datafiles import load_versioned_dataset
        for mp in manifests:
            try:
                bars, man = load_versioned_dataset(mp)
            except (OSError, ValueError) as e:
                die(f"versioned dataset failed: {e}")
            candles.extend(bars)
            manifests_used.append({"manifest": mp, "dataset_id": man.get("dataset_id"),
                                   "symbol": man.get("symbol"),
                                   "sha256": man.get("sha256"),
                                   "rows": man.get("rows")})
    if csv_pats:
        try:
            bars, stats = load_ohlcv_dataset(csv_pats, symbol=args.symbol,
                                             interval=args.interval)
        except FileNotFoundError as e:
            die(f"no data files: {e}")
        candles.extend(bars)
    if not candles:
        die("dataset is empty")
    stats = {"loaded": len(candles), "manifests": manifests_used}
    return candles, stats


async def _backtest_once(symbol, candles, strategy, factory, model, warmup=None):
    cfg, md, ex, risk, pf, eng, strat, bt = factory()
    bt.strategy_manager = SingleStrategyManager(strategy)
    await ex.connect()
    try:
        res = await bt.run(symbol, list(candles), execution_model=model,
                           warmup_candles=list(warmup) if warmup else None)
    finally:
        try:
            await ex.disconnect()
        except Exception:
            pass
    return res, compute_report_metrics(res, float(cfg.get('initial_cash', 10000.0)))


def cmd_research(args):
    from research.datasets import build_research_dataset as build_ds
    candles, stats = load_dataset(args)
    content_hash = {}
    for m in stats.get("manifests", []):
        if m.get("symbol") and m.get("sha256"):
            content_hash.setdefault(m["symbol"], m["sha256"])
    symbols = list(args.symbols) if args.symbols else [args.symbol]
    by_symbol = {}
    for c in candles:
        by_symbol.setdefault(c.symbol, []).append(c)
    missing = [s for s in symbols if s not in by_symbol or not by_symbol[s]]
    if missing:
        die(f"no bars for symbols {missing} in --data (found: {sorted(by_symbol.keys())})")
    fams = args.families or sorted(fam.FAMILIES.keys())
    unknown = [f for f in fams if f not in fam.FAMILIES]
    if unknown:
        die(f"unknown families: {unknown} (choose from {sorted(fam.FAMILIES.keys())})")
    primary = symbols[0]
    ds = build_ds(by_symbol[primary], primary, holdout_ratio=args.holdout_ratio,
                  source=",".join(args.data),
                  content_hash=content_hash.get(primary, ""))
    extra = {s: build_ds(by_symbol[s], s, holdout_ratio=args.holdout_ratio,
                         source=",".join(args.data),
                         content_hash=content_hash.get(s, ""))
             for s in symbols[1:]}
    gates = GateConfig()
    cfg = CampaignConfig(
        campaign_id=args.campaign_id, symbol=primary, symbols=symbols,
        families=sorted(fams),
        candidate_limit=args.limit, seed=args.seed,
        train_ratio=args.train_ratio, validation_ratio=args.validation_ratio,
        embargo_bars=args.embargo, warmup_bars=args.warmup,
        n_splits=args.splits, train_bars=args.train_bars, test_bars=args.test_bars,
        execution_model=args.model, capacity_multipliers=args.capacity,
        allow_holdout=args.allow_holdout, gates=gates,
        optimizer=args.optimizer, optimizer_rounds=args.optimizer_rounds,
        optimizer_batch=args.optimizer_batch, optimizer_top_k=args.optimizer_top_k)
    store = ResearchStore(args.db)
    holdout_log = []
    try:
        record = asyncio.run(run_campaign(cfg, ds, base_stack_factory(), store,
                                          extra_datasets=extra or None,
                                          on_holdout_access=holdout_log.append))
    finally:
        store.close()
    rep = {"campaign_id": record["campaign_id"], "symbol": record["symbol"],
           "dataset_version": record["dataset_version"], "seed": record["seed"],
           "families": record["families"], "funnel": record["funnel"],
           "promoted": record["promoted"], "gates": record["gates"],
           "candidates": []}
    # Re-open briefly for the detailed report.
    store2 = ResearchStore(args.db)
    try:
        from research.report import build_campaign_report as bcr
        rep = bcr(record, store2)
    finally:
        store2.close()
    print(terminal_summary(rep))
    if args.report_out:
        jp, cp = write_campaign_reports(rep, args.report_out)
        print(f"wrote {jp} + {cp}")
    return 0


def cmd_backtest(args):
    try:
        with open(args.strategy_json, encoding="utf-8") as f:
            spec = json.load(f)
        strategy = fam.build_strategy(spec["family"], spec.get("params", {}))
    except (OSError, KeyError, ValueError) as e:
        die(f"bad strategy file: {e}")
    candles, _ = load_dataset(args)
    res, m = asyncio.run(_backtest_once(args.symbol, candles, strategy,
                                        base_stack_factory(), args.model))
    print(f"fills={res.trades} closed={m['n_closed']} net={m['net_pnl']:.2f} "
          f"PF={m['profit_factor']} exp={m['expectancy']:.4f} "
          f"DD={m['max_drawdown']:.4f}")
    if args.out:
        from evaluation import export_report_json
        export_report_json(args.out, {"strategy": spec, "metrics": m})
        print(f"wrote {args.out}")
    return 0


def cmd_validate(args):
    store = ResearchStore(args.db)
    try:
        d = store.load_candidate(args.candidate_id)
    finally:
        store.close()
    if d is None:
        die(f"candidate not found: {args.candidate_id}")
    try:
        strategy = fam.build_strategy(d["family"], d.get("params", {}))
    except ValueError as e:
        die(f"cannot build candidate strategy: {e}")
    candles, _ = load_dataset(args)
    from evaluation import temporal_split
    bars = sorted(candles, key=lambda c: c.timestamp)
    train = bars[:int(len(bars) * 0.6)]
    valid = bars[int(len(bars) * 0.6):int(len(bars) * 0.8)]
    _, tm = asyncio.run(_backtest_once(args.symbol, train, strategy,
                                       base_stack_factory(), "next_close"))
    _, vm = asyncio.run(_backtest_once(args.symbol, valid, strategy,
                                       base_stack_factory(), "next_close"))
    from research.gates import gate_validation, GateConfig
    ok, reason = gate_validation(d, tm, vm, GateConfig())
    print(f"candidate {args.candidate_id}: {'PASS' if ok else 'FAIL'} {reason}")
    return 0 if ok else 1


def cmd_report(args):
    store = ResearchStore(args.db)
    try:
        record = store.load_campaign(args.campaign_id)
        if record is None:
            die(f"campaign not found: {args.campaign_id}")
        from research.report import build_campaign_report as bcr
        rep = bcr(record, store)
    finally:
        store.close()
    print(terminal_summary(rep))
    if args.out:
        jp, cp = write_campaign_reports(rep, args.out)
        print(f"wrote {jp} + {cp}")
    return 0


def cmd_paper(args):
    store = ResearchStore(args.db)
    try:
        d = store.load_candidate(args.candidate_id)
    finally:
        store.close()
    if d is None:
        die(f"candidate not found: {args.candidate_id}")
    if d.get("status") != CandidateStatus.PAPER_CANDIDATE.value:
        die(f"candidate {args.candidate_id} is not a paper candidate "
            f"(status={d.get('status')})")
    candles, stats = load_dataset(args)
    bars = sorted(candles, key=lambda c: c.timestamp)[-args.bars:]
    from research.candidates import StrategyCandidate
    from research.paper import paper_with_divergence
    cand = StrategyCandidate.from_dict(d)
    dataset_version, dataset_hash = "", ""
    for m in stats.get("manifests", []):
        if m.get("symbol") == args.symbol:
            dataset_version = m.get("dataset_id", "")
            dataset_hash = m.get("sha256", "")
            break
    store2 = ResearchStore(args.db)
    try:
        sess = asyncio.run(paper_with_divergence(
            cand, bars, base_stack_factory(), args.symbol, store2,
            interval=args.interval,
            dataset_version=dataset_version, dataset_hash=dataset_hash))
    finally:
        store2.close()
    print(f"paper {sess['paper_id']}: fills={sess['n_fills']} "
          f"paper_net={sess['paper_net']:.2f} backtest_net={sess['backtest_net']:.2f} "
          f"divergence={sess['divergence']:.2f}")
    return 0


def cmd_status(args):
    store = ResearchStore(args.db)
    try:
        cands = store.list_candidates()
    finally:
        store.close()
    by_status = {}
    for c in cands:
        by_status[c.get("status", "?")] = by_status.get(c.get("status", "?"), 0) + 1
    print(f"db={args.db} candidates={len(cands)} " +
          " ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    return 0


def cmd_dataset_inspect(args):
    from research.datafiles import load_versioned_dataset
    from research.regimes import regime_distribution
    try:
        candles, manifest = load_versioned_dataset(args.manifest)
    except (OSError, ValueError) as e:
        die(f"dataset inspect failed: {e}")
    v = manifest.get("validation", {})
    print(f"dataset {manifest.get('dataset_id')}")
    print(f"  symbol={manifest.get('symbol')} interval={manifest.get('interval')} "
          f"rows={manifest.get('rows')}")
    print(f"  range={manifest.get('start')} -> {manifest.get('end')}")
    print(f"  source={manifest.get('source')}")
    print(f"  sha256={str(manifest.get('sha256'))[:16]}... "
          f"schema={manifest.get('schema_version')} (verified)")
    if "resampled_from_rows" in v:
        print(f"  resample: factor={v.get('factor')} from_rows={v.get('resampled_from_rows')} "
              f"dropped_trailing={v.get('dropped_trailing')}")
    else:
        print(f"  quality: bad={v.get('bad_rows', 0)} dup={v.get('duplicates', 0)} "
              f"ooo={v.get('out_of_order', 0)} impossible_ohlc={v.get('impossible_ohlc', 0)} "
              f"negvol={v.get('negative_volume', 0)} gaps={v.get('gaps', 0)} "
              f"missing_bars={v.get('missing_bars', 0)} "
              f"interval_ok={v.get('interval_ok_ratio', 0):.4f}")
    print(f"  loaded_bars={len(candles)}")
    dist = regime_distribution(candles)
    trend = ", ".join(f"{k}={v:.2f}" for k, v in sorted(dist["trend_share"].items()))
    vol = ", ".join(f"{k}={v:.2f}" for k, v in sorted(dist["vol_share"].items()))
    print(f"  regimes: {trend} | {vol} (dominant={dist['dominant_share']:.2f})")
    if dist["dominant_share"] > 0.9:
        print("  WARNING: one regime covers >90% of history — investigate classifier")
    return 0


def add_data_args(ap):
    ap.add_argument('--data', nargs='+', default=['data/sample_ohlcv*.csv'])
    ap.add_argument('--symbol', default='BTC/USDT')
    ap.add_argument('--interval', default='5m')


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="falcon",
        description="Falcon Research Engine — generate strategies. Break them. "
                    "Promote only the survivors. (Simulation/paper only.)")
    ap.add_argument('--verbose', '-v', action='store_true',
                    help="verbose output: INFO logs (progress, summaries)")
    ap.add_argument('--log-level', default=None,
                    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                    help="explicit log level (default: WARNING, or INFO with --verbose)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("research", help="run an autonomous research campaign")
    add_data_args(r)
    r.add_argument('--symbols', nargs='*', default=None,
                   help="symbol universe (default: [--symbol]); cross-market gate applies")
    r.add_argument('--families', nargs='*', default=None)
    r.add_argument('--limit', type=int, default=16)
    r.add_argument('--seed', type=int, default=7)
    r.add_argument('--campaign-id', default="camp_demo")
    r.add_argument('--db', default="falcon.db")
    r.add_argument('--train-ratio', type=float, default=0.6)
    r.add_argument('--validation-ratio', type=float, default=0.2)
    r.add_argument('--embargo', type=int, default=5)
    r.add_argument('--warmup', type=int, default=30)
    r.add_argument('--splits', type=int, default=2)
    r.add_argument('--train-bars', type=int, default=60)
    r.add_argument('--test-bars', type=int, default=40)
    r.add_argument('--model', default='next_close', choices=['next_open', 'next_close'])
    r.add_argument('--capacity', nargs='+', type=float, default=[1, 5, 20])
    r.add_argument('--allow-holdout', action='store_true',
                   help="permit sealed-holdout confirmation (logged, final stage only)")
    r.add_argument('--holdout-ratio', type=float, default=0.15)
    r.add_argument('--optimizer', default='random', choices=['random', 'bayes'],
                   help="candidate search: random (default) or bayes (GP+EI on train only)")
    r.add_argument('--optimizer-rounds', type=int, default=2)
    r.add_argument('--optimizer-batch', type=int, default=4)
    r.add_argument('--optimizer-top-k', type=int, default=8)
    r.add_argument('--report-out', default=None)
    r.set_defaults(fn=cmd_research)

    b = sub.add_parser("backtest", help="backtest one strategy JSON file")
    add_data_args(b)
    b.add_argument('--strategy-json', required=True)
    b.add_argument('--model', default='next_close', choices=['next_open', 'next_close'])
    b.add_argument('--out', default=None)
    b.set_defaults(fn=cmd_backtest)

    v = sub.add_parser("validate", help="re-run validation gates for a candidate")
    add_data_args(v)
    v.add_argument('--db', default="falcon.db")
    v.add_argument('--candidate-id', required=True)
    v.set_defaults(fn=cmd_validate)

    rp = sub.add_parser("report", help="print/write a campaign report")
    rp.add_argument('--db', default="falcon.db")
    rp.add_argument('--campaign-id', required=True)
    rp.add_argument('--out', default=None)
    rp.set_defaults(fn=cmd_report)

    p = sub.add_parser("paper", help="promote a paper candidate to paper replay")
    add_data_args(p)
    p.add_argument('--db', default="falcon.db")
    p.add_argument('--candidate-id', required=True)
    p.add_argument('--bars', type=int, default=60)
    p.set_defaults(fn=cmd_paper)

    s = sub.add_parser("status", help="show research database summary")
    s.add_argument('--db', default="falcon.db")
    s.set_defaults(fn=cmd_status)

    d = sub.add_parser("dataset", help="inspect dataset files and manifests")
    dsub = d.add_subparsers(dest="dataset_cmd", required=True)
    di = dsub.add_parser("inspect", help="validate + summarize a manifest/CSV pair")
    di.add_argument('manifest', help="path to .manifest.json (CSV resolved alongside)")
    di.set_defaults(fn=cmd_dataset_inspect)

    args = ap.parse_args(argv)
    configure_logging(args)
    try:
        return args.fn(args)
    except SystemExit:
        raise
    except Exception as e:
        die(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
