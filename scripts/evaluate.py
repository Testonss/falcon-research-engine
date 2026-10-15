#!/usr/bin/env python3
"""Quantitative research evaluation (paper data only — never live).

Runs, on historical OHLCV CSV(s):
  1. temporal train/validation/test regimes (fresh stack each, warmup context);
  2. walk-forward folds with warmup (per execution model);
  3. cost-sensitivity sweep (fee/spread/slippage multipliers on the test regime);
  4. next_open vs next_close comparison (test regime);
  5. capacity scan (scaled capital on the test regime, liquidity-aware);
exports one JSON report + one CSV table.

Example:
  python scripts/evaluate.py --data "data/sample_ohlcv*.csv" --out reports/eval
"""
import argparse
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import ConfigManager
from market_data import MarketDataHandler, load_ohlcv_dataset
from exchange import PaperExchange
from strategy import StrategyManager
from execution import ExecutionEngine
from risk import RiskManager
from portfolio import PortfolioManager
from backtest import BacktestEngine
from evaluation import (
    summarize_regimes, run_walk_forward, run_cost_sweep,
    compare_execution_models, estimate_capacity,
    export_report_json, export_folds_csv,
)


def make_factory(cost_overrides=None, liquidity_overrides=None, min_notional=10.0):
    def build(costs=None):
        cfg = ConfigManager()
        cfg.set('simulated_latency_ms', 0)
        cfg.set('min_fill_notional', min_notional)  # realistic dust filter for research
        merged = dict(cost_overrides or {})
        merged.update(costs or {})
        merged.update(liquidity_overrides or {})
        for k, v in merged.items():
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


def fmt_breakdown(rep):
    return (f"gross={rep['gross_pnl']:.2f} fees={rep['fees_paid']:.2f} "
            f"spread={rep['spread_paid']:.2f} slip={rep['slippage_paid']:.2f} "
            f"impact={rep['impact_paid']:.2f}")


async def main_async(args):
    candles, ds_stats = load_ohlcv_dataset(args.data, symbol=args.symbol,
                                           interval=args.interval)
    print(f"dataset: {ds_stats['loaded']} bars from {ds_stats['files']} file(s) "
          f"(rows={ds_stats['rows']} bad={ds_stats['skipped_bad']} "
          f"dup_in={ds_stats['duplicates']} dup_cross={ds_stats['cross_file_duplicates']})")

    factory = make_factory(liquidity_overrides={
        'max_participation_rate': args.max_participation,
        'market_impact_coeff': args.impact_coeff,
        'dynamic_spread_coeff': args.dyn_spread_coeff,
    }, min_notional=args.min_notional)
    regimes = await summarize_regimes(
        args.symbol, candles, factory,
        train_ratio=args.train_ratio, validation_ratio=args.validation_ratio,
        embargo_bars=args.embargo, execution_model=args.models[0],
        warmup_bars=args.warmup)
    for name, rep in regimes['regimes'].items():
        if rep.get('skipped'):
            print(f"regime {name}: skipped (empty)")
            continue
        print(f"regime {name}: bars={rep['n_bars']} net={rep['net_pnl']:.2f} "
              f"PF={rep['profit_factor']:.3f} DD={rep['max_drawdown']:.4f} "
              f"Sharpe={rep['sharpe_5m_annualized']:.2f} turnover={rep['turnover']:.0f} "
              f"max_part={rep['max_participation']:.4f} | {fmt_breakdown(rep)}")

    wf_by_model = {}
    for model in args.models:
        wf = await run_walk_forward(
            args.symbol, candles, factory, n_splits=args.splits,
            train_bars=args.train_bars, test_bars=args.test_bars,
            embargo_bars=args.embargo, execution_model=model,
            warmup_bars=args.warmup)
        agg = wf['aggregate']
        print(f"walk-forward [{model}]: folds={agg['folds']} net={agg['net_pnl']:.2f} "
              f"pooled_winrate={agg['win_rate_pooled']:.3f}")
        wf_by_model[model] = wf

    # Cost sweep on the test regime only (train/valid untouched).
    base_costs = {'taker_fee': 0.002, 'maker_fee': 0.001,
                  'spread_pct': 0.0005, 'slippage_pct': 0.0005}
    scenarios = [{'name': 'base', **base_costs}]
    for mult in args.cost_mult:
        if mult == 1:
            continue
        scenarios.append({'name': f"x{mult}",
                          **{k: v * mult for k, v in base_costs.items()}})

    def cost_factory(costs):
        merged = {'max_participation_rate': args.max_participation,
                  'market_impact_coeff': args.impact_coeff,
                  'dynamic_spread_coeff': args.dyn_spread_coeff,
                  'min_fill_notional': args.min_notional}
        merged.update(costs or {})
        return make_factory(merged)()

    # Re-derive the test slice deterministically for the sweep/comparison,
    # with the same warmup context the regime evaluation used
    # (tail of train+valid, embargo bars excluded).
    from evaluation import temporal_split
    tr, va, test = temporal_split(candles, args.train_ratio,
                                  args.validation_ratio, embargo_bars=args.embargo)
    hist = sorted(tr + va, key=lambda c: c.timestamp)
    warmup = hist[-args.warmup:] if args.warmup and hist else []
    sweep = await run_cost_sweep(args.symbol, test, cost_factory, scenarios,
                                 execution_model=args.models[0],
                                 warmup_candles=list(warmup) or None)
    for s in sweep['scenarios']:
        print(f"costs {s['scenario']}: net={s['net_pnl']:.2f} fees={s['fees_paid']:.2f} "
              f"fills={s['fills']}")

    comparison = await compare_execution_models(args.symbol, test, factory,
                                                models=tuple(args.models),
                                                warmup_candles=list(warmup) or None)
    for m, rep in comparison['models'].items():
        print(f"model {m}: net={rep['net_pnl']:.2f} fills={rep['fills']} | "
              f"{fmt_breakdown(rep)}")

    # Capacity scan on the test regime (same warmup): scale initial_cash and
    # risk_per_trade linearly so sizing grows while bar volumes stay fixed.
    def capacity_factory(scale):
        base_cash = 10000.0
        base_risk = 10.0
        cfg = ConfigManager()
        cfg.set('simulated_latency_ms', 0)
        cfg.set('initial_cash', base_cash * scale)
        cfg.set('risk_per_trade', base_risk * scale)
        cfg.set('max_participation_rate', args.max_participation)
        cfg.set('market_impact_coeff', args.impact_coeff)
        cfg.set('dynamic_spread_coeff', args.dyn_spread_coeff)
        cfg.set('min_fill_notional', args.min_notional)
        md = MarketDataHandler(cfg)
        ex = PaperExchange(cfg, seed=int(cfg.get('random_seed', 42)))
        risk = RiskManager(cfg)
        pf = PortfolioManager(cfg)
        eng = ExecutionEngine(cfg, ex, risk)
        strat = StrategyManager(cfg)
        bt = BacktestEngine(cfg, md, eng, pf, risk, strat)
        return cfg, md, ex, risk, pf, eng, strat, bt

    capacity = await estimate_capacity(args.symbol, test, capacity_factory,
                                       args.capacity,
                                       execution_model=args.models[0],
                                       warmup_candles=list(warmup) or None)
    for lv in capacity['levels']:
        print(f"capacity x{lv['multiplier']:g} (capital={lv['capital']:.0f}): "
              f"net={lv['net_pnl']:.2f} fills={lv['fills']} "
              f"max_part={lv['max_participation']:.3f} "
              f"capped={lv['liquidity_capped_fills']} impact={lv['impact_paid']:.2f}")
    summ = capacity['summary']
    print(f"capacity: max_survivable={summ['max_survivable_multiplier']} "
          f"capital={summ['max_survivable_capital']}")

    report = {
        'symbol': args.symbol,
        'dataset': ds_stats,
        'params': vars(args),
        'regimes': regimes['regimes'],
        'walk_forward': {m: wf_by_model[m] for m in args.models},
        'cost_sweep': sweep['scenarios'],
        'model_comparison': comparison['models'],
        'capacity': capacity,
    }
    os.makedirs(args.out if os.path.splitext(args.out)[1] == '' else
                os.path.dirname(args.out) or '.', exist_ok=True)
    prefix = args.out
    json_path = prefix + '.json' if not prefix.endswith('.json') else prefix
    csv_path = (prefix + '.csv' if not prefix.endswith('.csv')
                else prefix.replace('.json', '.csv'))
    export_report_json(json_path, report)
    rows = []
    for name, rep in regimes['regimes'].items():
        if not rep.get('skipped'):
            rows.append(dict(rep))
    for m in args.models:
        rows.extend(wf_by_model[m]['folds'])
    for s in sweep['scenarios']:
        row = dict(s)
        row['scope'] = 'cost_sweep'
        row['fold_or_regime'] = s['scenario']
        rows.append(row)
    for m, rep in comparison['models'].items():
        row = dict(rep)
        row['scope'] = 'model_compare'
        row['fold_or_regime'] = m
        rows.append(row)
    for lv in capacity['levels']:
        row = dict(lv)
        row['scope'] = 'capacity'
        row['fold_or_regime'] = f"x{lv['multiplier']:g}"
        rows.append(row)
    export_folds_csv(csv_path, rows)
    print(f"wrote {json_path} + {csv_path}")


def main():
    ap = argparse.ArgumentParser(description="Quant research evaluation (no live trading)")
    ap.add_argument('--data', nargs='+', default=['data/sample_ohlcv*.csv'])
    ap.add_argument('--symbol', default='BTC/USDT')
    ap.add_argument('--interval', default='5m')
    ap.add_argument('--train-ratio', type=float, default=0.6)
    ap.add_argument('--validation-ratio', type=float, default=0.2)
    ap.add_argument('--embargo', type=int, default=5)
    ap.add_argument('--warmup', type=int, default=30)
    ap.add_argument('--splits', type=int, default=3)
    ap.add_argument('--train-bars', type=int, default=200)
    ap.add_argument('--test-bars', type=int, default=100)
    ap.add_argument('--models', nargs='+', default=['next_close', 'next_open'])
    ap.add_argument('--cost-mult', nargs='+', type=int, default=[1, 2, 5])
    ap.add_argument('--max-participation', type=float, default=0.10)
    ap.add_argument('--impact-coeff', type=float, default=0.0)
    ap.add_argument('--dyn-spread-coeff', type=float, default=0.0)
    ap.add_argument('--min-notional', type=float, default=10.0)
    ap.add_argument('--capacity', nargs='+', type=float, default=[1, 2, 5, 10, 20, 50])
    ap.add_argument('--out', default='reports/eval')
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == '__main__':
    main()
