"""
Evaluation framework for reliable backtest assessment (no live trading).

Contents:
- temporal train/validation/test splits (strictly chronological + embargo);
- walk-forward folds with indicator warmup (rolling/anchored, embargo gap);
- pure report-metrics from a BacktestResult (no strategy tuning here);
- cost-sensitivity sweeps, execution-model comparison, CSV/JSON export.

Anti-leakage rules enforced by construction:
- splits are contiguous time slices, never shuffled;
- embargo bars between splits are discarded;
- warmup bars are indicator context only (never scored);
- every fold uses a FRESH stack via a caller-provided factory
  (no shared portfolio/risk/exchange state between train and test).
"""

import asyncio
import csv
import json
import logging
import math
from typing import List, Dict, Any, Tuple, Callable, Optional

logger = logging.getLogger(__name__)

EPS = 1e-9
# Annualization for 5m bars (365d * 24h * 12 bars/h). Used only for Sharpe scaling.
BARS_PER_YEAR_5M = 365 * 24 * 12


def _timestamps(candles) -> List:
    return [c.timestamp for c in candles]


def assert_no_time_overlap(a, b, label_a: str = "a", label_b: str = "b"):
    """Raise AssertionError if two candle slices share timestamps or interleave."""
    if not a or not b:
        return
    ta = sorted(c.timestamp for c in a)
    tb = sorted(c.timestamp for c in b)
    overlap = set(ta) & set(tb)
    if overlap:
        raise AssertionError(f"{label_a}/{label_b} share {len(overlap)} timestamps (leakage)")
    # Strict ordering: all of one side before the other.
    if not (max(ta) < min(tb) or max(tb) < min(ta)):
        raise AssertionError(f"{label_a}/{label_b} interleave in time (leakage)")


def temporal_split(candles, train_ratio: float = 0.6, validation_ratio: float = 0.2,
                   embargo_bars: int = 0) -> Tuple[List, List, List]:
    """Split sorted candles into (train, validation, test) — strictly temporal.

    Embargo bars at each boundary are discarded (not assigned to any split).
    Raises ValueError on invalid ratios or too few bars.
    """
    candles = sorted(candles, key=lambda c: c.timestamp)
    n = len(candles)
    if n < 10:
        raise ValueError(f"Need >= 10 bars for a temporal split, got {n}")
    if not (0 < train_ratio < 1) or not (0 <= validation_ratio < 1):
        raise ValueError(f"Invalid ratios train={train_ratio} valid={validation_ratio}")
    if train_ratio + validation_ratio > 1.0:
        raise ValueError("train_ratio + validation_ratio must be <= 1.0")
    if embargo_bars < 0:
        raise ValueError("embargo_bars must be >= 0")

    n_train = int(n * train_ratio)
    n_valid = int(n * validation_ratio)
    if n_train < 5:
        raise ValueError("Train slice too small for these ratios/bars")
    train = candles[:n_train]
    rest = candles[n_train + embargo_bars:]
    valid = rest[:n_valid]
    rest2 = rest[n_valid + embargo_bars:] if n_valid else rest
    test = rest2  # remainder goes to test (may be empty if ratios sum to 1)
    assert_no_time_overlap(train, valid, "train", "validation")
    assert_no_time_overlap(train, test, "train", "test")
    assert_no_time_overlap(valid, test, "validation", "test")
    logger.debug("Temporal split: n=%d -> train=%d valid=%d test=%d embargo=%d",
                n, len(train), len(valid), len(test), embargo_bars)
    return train, valid, test


def walk_forward_splits(candles, n_splits: int = 3, train_bars: int = 60,
                        test_bars: int = 20, embargo_bars: int = 5,
                        anchored: bool = False) -> List[Tuple[List, List]]:
    """Rolling (default) or anchored walk-forward folds (legacy pair form).

    Kept for backward compatibility; prefer :func:`walk_forward_folds` which
    also returns indicator warmup with explicit isolation checks.
    """
    folds = walk_forward_folds(candles, n_splits, train_bars, test_bars,
                               embargo_bars, anchored=anchored, warmup_bars=0)
    return [(f['train'], f['test']) for f in folds]


def walk_forward_folds(candles, n_splits: int = 3, train_bars: int = 60,
                       test_bars: int = 20, embargo_bars: int = 5,
                       anchored: bool = False,
                       warmup_bars: int = 0) -> List[Dict[str, Any]]:
    """Rolling (default) or anchored walk-forward folds with warmup.

    Test segments are the last `n_splits * test_bars` bars, non-overlapping.
    Train for fold k ends `embargo_bars` before its test starts:
    - rolling:  last `train_bars` bars before the embargo;
    - anchored: all bars from 0 to embargo boundary (expanding).
    `warmup` is the last `warmup_bars` of train (indicator context only;
    never scored — pass it as `warmup_candles` to `BacktestEngine.run`).
    Raises ValueError when history is insufficient.
    NOTE: test folds shorter than the strategy indicator warmup
    (~22 bars for default EMA20/momentum14) score zero fills by design
    unless warmup is provided — size test_bars/warmup_bars accordingly.
    """
    candles = sorted(candles, key=lambda c: c.timestamp)
    n = len(candles)
    if n_splits < 1 or train_bars < 5 or test_bars < 1 or embargo_bars < 0:
        raise ValueError("Invalid walk-forward parameters")
    if warmup_bars < 0:
        raise ValueError("warmup_bars must be >= 0")
    if warmup_bars > train_bars:
        raise ValueError(f"warmup_bars ({warmup_bars}) exceeds train_bars ({train_bars})")
    need = n_splits * test_bars + embargo_bars + train_bars
    # Anchored needs less (first fold train may start at 0 with fewer bars? No:
    # require full train_bars for comparability).
    if n < need:
        raise ValueError(f"Need >= {need} bars for {n_splits} folds "
                         f"(train={train_bars} test={test_bars} embargo={embargo_bars}), got {n}")
    folds: List[Dict[str, Any]] = []
    test_region_start = n - n_splits * test_bars
    for k in range(n_splits):
        test_start = test_region_start + k * test_bars
        test_end = test_start + test_bars
        test = candles[test_start:test_end]
        train_end = test_start - embargo_bars
        if anchored:
            train = candles[:train_end]
            # Bound anchored train to keep runtime sane? No — keep full history.
            if len(train) < train_bars:
                raise ValueError(f"Fold {k}: anchored train too short ({len(train)})")
        else:
            train_start = train_end - train_bars
            train = candles[train_start:train_end]
        warmup = train[-warmup_bars:] if warmup_bars else []
        verify_fold_isolation(train, warmup, test, embargo_bars)
        # Test folds must not overlap each other.
        for j, prev in enumerate(folds):
            assert_no_time_overlap(prev['test'], test, f"test[{j}]", f"test[{k}]")
        folds.append({'train': train, 'warmup': warmup, 'test': test})
    return folds


def verify_fold_isolation(train, warmup, test, embargo_bars: int) -> Dict[str, Any]:
    """Explicit leakage control for one fold. Raises on any violation.

    Checks: non-empty train/test, pairwise no-overlap (train/test,
    warmup/test), warmup strictly before test, warmup within train range,
    and embargo gap between train end and test start.
    Returns {'gap_bars', 'embargo_bars', 'warmup_bars'}.
    """
    if not train:
        raise ValueError("Empty train slice (leakage check)")
    if not test:
        raise ValueError("Empty test slice (leakage check)")
    warmup = list(warmup or [])
    assert_no_time_overlap(train, test, "train", "test")
    if warmup:
        assert_no_time_overlap(warmup, test, "warmup", "test")
        if max(c.timestamp for c in warmup) >= min(c.timestamp for c in test):
            raise AssertionError("warmup is not strictly before test (leakage)")
        if min(c.timestamp for c in warmup) < min(c.timestamp for c in train):
            raise AssertionError("warmup starts before train (leakage)")
        if max(c.timestamp for c in warmup) > max(c.timestamp for c in train):
            raise AssertionError("warmup extends past train end (leakage)")
    # Embargo gap measured in bar steps using the test grid.
    train_end = max(c.timestamp for c in train)
    test_start = min(c.timestamp for c in test)
    if test_start <= train_end:
        raise AssertionError("test starts before train ends (leakage)")
    step = _median_step(test)
    gap_bars = int(round((test_start - train_end).total_seconds() / step)) - 1 if step else 0
    if gap_bars < embargo_bars:
        raise AssertionError(
            f"embargo violated: {gap_bars} gap bars < {embargo_bars} required")
    return {'gap_bars': gap_bars, 'embargo_bars': embargo_bars,
            'warmup_bars': len(warmup)}


def _median_step(candles) -> float:
    """Median timestamp step in seconds (0.0 if fewer than 2 bars)."""
    ts = sorted(c.timestamp for c in candles)
    if len(ts) < 2:
        return 0.0
    diffs = sorted((b - a).total_seconds() for a, b in zip(ts, ts[1:]))
    mid = len(diffs) // 2
    if len(diffs) % 2:
        return diffs[mid]
    return (diffs[mid - 1] + diffs[mid]) / 2.0


def max_consecutive(seq: List[float], sign: int) -> int:
    """Longest run of strictly positive (sign=+1) or negative (sign=-1) values."""
    best = cur = 0
    for v in seq:
        if (sign > 0 and v > EPS) or (sign < 0 and v < -EPS):
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def sharpe_from_equity(equity_curve: List[float],
                       bars_per_year: int = BARS_PER_YEAR_5M) -> float:
    """Annualized Sharpe (risk-free 0) on simple per-bar returns. 0.0 if undefined."""
    if len(equity_curve) < 3:
        return 0.0
    try:
        if not math.isfinite(float(bars_per_year)) or float(bars_per_year) <= 0:
            return 0.0
    except (TypeError, ValueError):
        return 0.0
    rets = []
    for prev, cur in zip(equity_curve[:-1], equity_curve[1:]):
        try:
            pf, cf = float(prev), float(cur)
        except (TypeError, ValueError):
            return 0.0
        if not (math.isfinite(pf) and math.isfinite(cf)):
            return 0.0
        if pf and pf > 0:
            r = (cf - pf) / pf
            if not math.isfinite(r):
                return 0.0
            rets.append(r)
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    if not math.isfinite(mean):
        return 0.0
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if not math.isfinite(var) or var <= 0:
        return 0.0
    out = mean / math.sqrt(var) * math.sqrt(float(bars_per_year))
    return out if math.isfinite(out) else 0.0


SECONDS_PER_YEAR = 365 * 24 * 3600  # annualization policy: fixed 365-day year


def bars_per_year_from_stamps(equity_stamps) -> Optional[float]:
    """Infer bars/year from the median bar interval (robust to gaps).

    Gaps and irregular spacing are NOT interpolated: the median step
    defines the grid, missing bars simply contribute no return observations.
    Returns None when fewer than 3 parseable stamps or no positive step.
    """
    from datetime import datetime
    ts = []
    for s in (equity_stamps or []):
        try:
            dt = s if isinstance(s, datetime) else datetime.fromisoformat(s)
            ts.append(dt.timestamp())
        except (ValueError, TypeError, AttributeError):
            continue
    if len(ts) < 3:
        return None
    steps = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
    if not steps:
        return None
    median_step = steps[len(steps) // 2]
    if median_step <= 0:
        return None
    return SECONDS_PER_YEAR / median_step


def sharpe_annualized(equity_curve: List[float],
                      equity_stamps=None,
                      bars_per_year: Optional[float] = None) -> float:
    """Timeframe-aware annualized Sharpe (risk-free 0).

    Uses explicit `bars_per_year` when given, else infers it from the median
    bar interval of `equity_stamps`, else falls back to the legacy 5-minute
    grid. Never raises; 0.0 when undefined. Do NOT compare values computed
    under different annualization policies as if equivalent.
    """
    bpy = bars_per_year
    if bpy is None:
        bpy = bars_per_year_from_stamps(equity_stamps)
    if bpy is None:
        bpy = BARS_PER_YEAR_5M
    if bpy <= 0:
        return 0.0
    return sharpe_from_equity(list(equity_curve or []), bars_per_year=bpy)


def compute_report_metrics(result, initial_cash: float) -> Dict[str, Any]:
    """Pure quantitative report from a BacktestResult + initial cash.

    Returns: net_pnl, gross_pnl (reference-price PnL before impact costs),
    cost breakdown (fees, spread, slippage, market impact), gross_win,
    gross_loss, profit_factor (+inf when no losses but wins exist; 0.0 when
    nothing closed), expectancy, avg_win, avg_loss, max_win, max_loss,
    n_closed, win_rate, max_drawdown, sharpe_annualized (timeframe-aware;
    legacy 'sharpe_5m_annualized' key kept as a deprecated identical alias), exposure_avg,
    exposure_max, turnover (+long/short split, ratio), participation
    (avg/max), liquidity counters, final_equity.
    """
    pnls = list(getattr(result, 'trade_pnls', []) or [])
    wins = [p for p in pnls if p > EPS]
    losses = [p for p in pnls if p < -EPS]
    n_closed = len(wins) + len(losses)
    gross_win = sum(wins)
    gross_loss = sum(losses)  # negative or zero
    net_pnl = float(getattr(result, 'realized_pnl', 0.0) or 0.0)
    fees_paid = float(getattr(result, 'fees_paid', 0.0) or 0.0)
    spread_paid = float(getattr(result, 'spread_paid', 0.0) or 0.0)
    slippage_paid = float(getattr(result, 'slippage_paid', 0.0) or 0.0)
    impact_paid = float(getattr(result, 'impact_paid', 0.0) or 0.0)
    gross_pnl = float(getattr(result, 'gross_pnl', 0.0) or 0.0)
    if gross_loss < -EPS:
        profit_factor = gross_win / abs(gross_loss)
    elif gross_win > EPS:
        profit_factor = float('inf')
    else:
        profit_factor = 0.0
    expectancy = (net_pnl / n_closed) if n_closed else 0.0
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    exposures = list(getattr(result, 'exposures', []) or [])
    turnover = float(getattr(result, 'turnover', 0.0) or 0.0)
    part_n = int(getattr(result, 'participation_n', 0) or 0)
    part_sum = float(getattr(result, 'participation_sum', 0.0) or 0.0)
    equity_curve = list(getattr(result, 'equity_curve', []) or [])
    equity_stamps = list(getattr(result, 'equity_stamps', []) or [])
    # Timeframe-aware Sharpe. The legacy 'sharpe_5m_annualized' key is kept
    # as a deprecated alias (identical value) for report compatibility.
    sharpe = sharpe_annualized(equity_curve, equity_stamps=equity_stamps)
    return {
        'execution_model': getattr(result, 'execution_model', 'next_close'),
        'n_bars': getattr(result, 'n_bars', 0),
        'fills': getattr(result, 'trades', 0),
        'strategy_trades': getattr(result, 'strategy_trades', 0),
        'sltp_trades': getattr(result, 'sltp_trades', 0),
        'n_closed': n_closed,
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': (len(wins) / n_closed) if n_closed else 0.0,
        'net_pnl': net_pnl,
        'gross_pnl': gross_pnl,
        'fees_paid': fees_paid,
        'spread_paid': spread_paid,
        'slippage_paid': slippage_paid,
        'impact_paid': impact_paid,
        'gross_win': gross_win,
        'gross_loss': gross_loss,
        'profit_factor': profit_factor,
        'expectancy': expectancy,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'max_win': max(pnls) if pnls else 0.0,
        'max_loss': min(pnls) if pnls else 0.0,
        'max_consecutive_wins': max_consecutive(pnls, +1),
        'max_consecutive_losses': max_consecutive(pnls, -1),
        'max_drawdown': float(getattr(result, 'max_drawdown', 0.0) or 0.0),
        'sharpe_5m_annualized': sharpe,
        'sharpe_annualized': sharpe,
        'exposure_avg': (sum(exposures) / len(exposures)) if exposures else 0.0,
        'exposure_max': max(exposures) if exposures else 0.0,
        'turnover': turnover,
        'turnover_long': float(getattr(result, 'turnover_long', 0.0) or 0.0),
        'turnover_short': float(getattr(result, 'turnover_short', 0.0) or 0.0),
        'turnover_ratio': (turnover / initial_cash) if initial_cash > 0 else 0.0,
        'avg_participation': (part_sum / part_n) if part_n else 0.0,
        'max_participation': float(getattr(result, 'max_participation', 0.0) or 0.0),
        'liquidity_capped_fills': int(getattr(result, 'liquidity_capped_fills', 0) or 0),
        'insufficient_liquidity_rejects': int(getattr(result, 'insufficient_liquidity_rejects', 0) or 0),
        'final_equity': float(getattr(result, 'final_equity', 0.0) or 0.0),
        'blocked_by_risk': getattr(result, 'blocked_by_risk', 0),
        'rejected_orders': getattr(result, 'rejected_orders', 0),
    }


def _iso(ts) -> str:
    try:
        return ts.isoformat()
    except Exception:
        return str(ts)


async def run_walk_forward(symbol: str, candles, stack_factory: Callable,
                           n_splits: int = 3, train_bars: int = 60,
                           test_bars: int = 20, embargo_bars: int = 5,
                           execution_model: Optional[str] = None,
                           anchored: bool = False,
                           warmup_bars: int = 0) -> Dict[str, Any]:
    """Evaluate strictly on walk-forward TEST folds with fresh stacks.

    `stack_factory()` must return (config, md, ex, risk, pf, eng, strat, bt)
    with no shared mutable state between calls. Train slices are returned
    for inspection/tuning but NEVER executed here (tuning must happen outside,
    on train only). `warmup_bars` takes the tail of each train as indicator
    context (never scored). Returns {'folds': [...], 'aggregate': {...}}.
    """
    folds = walk_forward_folds(candles, n_splits, train_bars, test_bars,
                               embargo_bars, anchored=anchored,
                               warmup_bars=warmup_bars)
    fold_reports = []
    all_pnls: List[float] = []
    for k, fold in enumerate(folds):
        train, warmup, test = fold['train'], fold['warmup'], fold['test']
        config, md, ex, risk, pf, eng, strat, bt = stack_factory()
        model = execution_model or config.get('execution_model', 'next_close')
        await ex.connect()
        try:
            res = await bt.run(symbol, list(test), execution_model=model,
                               warmup_candles=list(warmup) or None)
        finally:
            try:
                await ex.disconnect()
            except Exception:
                pass
        initial = float(config.get('initial_cash', 10000.0))
        metrics = compute_report_metrics(res, initial)
        metrics['fold'] = k
        metrics['train_bars'] = len(train)
        metrics['warmup_bars'] = len(warmup)
        metrics['test_bars'] = len(test)
        metrics['test_start'] = _iso(min(c.timestamp for c in test))
        metrics['test_end'] = _iso(max(c.timestamp for c in test))
        metrics['start'] = metrics['test_start']
        metrics['end'] = metrics['test_end']
        metrics['scope'] = 'walk_forward'
        metrics['fold_or_regime'] = k
        fold_reports.append(metrics)
        all_pnls.extend(list(res.trade_pnls or []))
    # Aggregate = concatenation semantics (sums + pooled win rate).
    agg_wins = sum(f['wins'] for f in fold_reports)
    agg_losses = sum(f['losses'] for f in fold_reports)
    aggregate = {
        'folds': n_splits,
        'net_pnl': sum(f['net_pnl'] for f in fold_reports),
        'wins': agg_wins,
        'losses': agg_losses,
        'win_rate_pooled': (agg_wins / (agg_wins + agg_losses)) if (agg_wins + agg_losses) else 0.0,
        'avg_profit_factor': sum(f['profit_factor'] for f in fold_reports
                                 if f['profit_factor'] != float('inf')) / max(1, n_splits),
        'avg_max_drawdown': sum(f['max_drawdown'] for f in fold_reports) / max(1, n_splits),
        'total_turnover': sum(f['turnover'] for f in fold_reports),
    }
    return {'folds': fold_reports, 'aggregate': aggregate}


async def summarize_regimes(symbol: str, candles, stack_factory: Callable,
                            train_ratio: float = 0.6, validation_ratio: float = 0.2,
                            embargo_bars: int = 5,
                            execution_model: Optional[str] = None,
                            warmup_bars: int = 0) -> Dict[str, Any]:
    """Per-regime (train/validation/test) evaluation with fresh stacks.

    Each regime is replayed independently; the `warmup_bars` tail preceding
    each regime provides indicator context (never scored). Isolation between
    regimes is verified explicitly. Returns {'regimes': {name: report}}.
    """
    candles = sorted(candles, key=lambda c: c.timestamp)
    train, valid, test = temporal_split(candles, train_ratio, validation_ratio,
                                        embargo_bars=embargo_bars)
    regimes = {'train': train, 'validation': valid, 'test': test}
    reports: Dict[str, Any] = {}
    # Regime order for warmup sourcing: each regime warms up from its own
    # preceding history (never from its future).
    preceding = {'train': [], 'validation': train, 'test': train + valid}
    for name, segment in regimes.items():
        if not segment:
            reports[name] = {'n_bars': 0, 'skipped': True}
            continue
        history = sorted(preceding[name], key=lambda c: c.timestamp)
        warmup = history[-warmup_bars:] if warmup_bars and history else []
        if warmup:
            verify_fold_isolation(history, warmup, segment, embargo_bars)
        else:
            assert_no_time_overlap(history, segment, f"history", name) if history else None
        config, md, ex, risk, pf, eng, strat, bt = stack_factory()
        model = execution_model or config.get('execution_model', 'next_close')
        await ex.connect()
        try:
            res = await bt.run(symbol, list(segment), execution_model=model,
                               warmup_candles=list(warmup) or None)
        finally:
            try:
                await ex.disconnect()
            except Exception:
                pass
        initial = float(config.get('initial_cash', 10000.0))
        metrics = compute_report_metrics(res, initial)
        metrics['regime'] = name
        metrics['n_bars'] = len(segment)
        metrics['warmup_bars'] = len(warmup)
        metrics['start'] = _iso(min(c.timestamp for c in segment))
        metrics['end'] = _iso(max(c.timestamp for c in segment))
        metrics['scope'] = 'regime'
        metrics['fold_or_regime'] = name
        reports[name] = metrics
    return {'regimes': reports}


async def run_cost_sweep(symbol: str, candles, stack_factory: Callable,
                         scenarios: List[Dict[str, Any]],
                         execution_model: Optional[str] = None,
                         warmup_candles: Optional[List] = None) -> Dict[str, Any]:
    """Robustness sweep over cost scenarios on identical bars.

    `stack_factory(costs)` builds a fresh stack with the given cost overrides
    ({taker_fee, maker_fee, spread_pct, slippage_pct} subset). Every scenario
    replays the same `candles` (+ optional shared warmup). Returns
    {'scenarios': [{name, costs, metrics}]}. Deterministic per scenario.
    """
    out = []
    for sc in scenarios:
        name = sc.get('name', 'scenario')
        costs = {k: sc[k] for k in ('taker_fee', 'maker_fee', 'spread_pct', 'slippage_pct')
                 if k in sc}
        config, md, ex, risk, pf, eng, strat, bt = stack_factory(costs)
        model = execution_model or config.get('execution_model', 'next_close')
        await ex.connect()
        try:
            res = await bt.run(symbol, list(candles), execution_model=model,
                               warmup_candles=(list(warmup_candles)
                                               if warmup_candles else None))
        finally:
            try:
                await ex.disconnect()
            except Exception:
                pass
        initial = float(config.get('initial_cash', 10000.0))
        metrics = compute_report_metrics(res, initial)
        metrics['scenario'] = name
        metrics['costs'] = dict(costs)
        out.append(metrics)
    return {'scenarios': out}


async def compare_execution_models(symbol: str, candles, stack_factory: Callable,
                                   models: Tuple[str, ...] = ('next_open', 'next_close'),
                                   warmup_candles: Optional[List] = None) -> Dict[str, Any]:
    """Same bars, fresh stack per model. Returns {'models': {name: metrics}}."""
    out: Dict[str, Any] = {}
    for model in models:
        config, md, ex, risk, pf, eng, strat, bt = stack_factory()
        await ex.connect()
        try:
            res = await bt.run(symbol, list(candles), execution_model=model,
                               warmup_candles=(list(warmup_candles)
                                               if warmup_candles else None))
        finally:
            try:
                await ex.disconnect()
            except Exception:
                pass
        initial = float(config.get('initial_cash', 10000.0))
        out[model] = compute_report_metrics(res, initial)
    return {'models': out}


async def estimate_capacity(symbol: str, candles, stack_factory: Callable,
                            capital_multipliers: List[float],
                            execution_model: Optional[str] = None,
                            warmup_candles: Optional[List] = None) -> Dict[str, Any]:
    """Capacity scan: replay identical bars at scaled capital levels.

    `stack_factory(scale)` must return a fresh stack with `initial_cash` and
    `risk_per_trade` scaled linearly by `scale` (all else fixed, same seed),
    so order sizes grow with capital while bar volumes stay fixed.
    Rising participation then drives capping and square-root market impact.

    Returns {'levels': [{multiplier, capital, metrics...}], 'summary': {...}}
    where summary reports the largest multiplier with net_pnl > 0
    (`max_survivable_multiplier`, None when even the base loses).
    Deterministic per (seed, inputs).
    """
    mults = sorted(float(m) for m in capital_multipliers)
    if not mults or any(m <= 0 for m in mults):
        raise ValueError("capital_multipliers must be non-empty positives")
    levels = []
    for m in mults:
        config, md, ex, risk, pf, eng, strat, bt = stack_factory(m)
        model = execution_model or config.get('execution_model', 'next_close')
        await ex.connect()
        try:
            res = await bt.run(symbol, list(candles), execution_model=model,
                               warmup_candles=(list(warmup_candles)
                                               if warmup_candles else None))
        finally:
            try:
                await ex.disconnect()
            except Exception:
                pass
        initial = float(config.get('initial_cash', 10000.0))
        metrics = compute_report_metrics(res, initial)
        metrics['multiplier'] = m
        metrics['capital'] = initial
        levels.append(metrics)
    survivors = [lv for lv in levels if lv['net_pnl'] > 0]
    summary = {
        'max_survivable_multiplier': max((lv['multiplier'] for lv in survivors),
                                         default=None),
        'max_survivable_capital': max((lv['capital'] for lv in survivors),
                                      default=None),
        'levels_evaluated': len(levels),
    }
    return {'levels': levels, 'summary': summary}


def _json_safe(value):
    if isinstance(value, float):
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        if math.isnan(value):
            return "nan"
    return value


def export_report_json(path: str, report: Dict[str, Any]) -> str:
    """Write a JSON-serializable evaluation report (inf-safe). Returns path."""
    def convert(obj):
        if isinstance(obj, dict):
            return {k: convert(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [convert(v) for v in obj]
        return _json_safe(obj)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(convert(report), f, indent=2, sort_keys=True)
    logger.info("Report written to %s", path)
    return path


REPORT_CSV_COLUMNS = [
    'scope', 'fold_or_regime', 'execution_model', 'n_bars', 'warmup_bars',
    'fills', 'n_closed', 'wins', 'losses', 'win_rate', 'net_pnl', 'gross_pnl',
    'fees_paid', 'spread_paid', 'slippage_paid', 'impact_paid',
    'profit_factor', 'expectancy', 'avg_win', 'avg_loss',
    'max_drawdown', 'sharpe_5m_annualized', 'exposure_avg', 'exposure_max',
    'turnover', 'turnover_long', 'turnover_short', 'turnover_ratio',
    'avg_participation', 'max_participation',
    'liquidity_capped_fills', 'insufficient_liquidity_rejects',
    'final_equity', 'capital', 'multiplier',
    'max_consecutive_wins', 'max_consecutive_losses',
    'blocked_by_risk', 'rejected_orders', 'start', 'end',
]


def export_folds_csv(path: str, rows: List[Dict[str, Any]]) -> str:
    """Write per-fold/per-regime metric rows to CSV. Returns path."""
    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=REPORT_CSV_COLUMNS, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow({k: _json_safe(r.get(k, '')) for k in REPORT_CSV_COLUMNS})
    logger.info("CSV report written to %s (%d rows)", path, len(rows))
    return path
