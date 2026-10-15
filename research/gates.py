"""Configurable promotion gates. Candidates are rejected early and honestly.

Each gate is a pure function of (candidate, metrics, config) returning
(passed, reason). Thresholds live in GateConfig (inspectable, reportable).
"""
from dataclasses import dataclass
from typing import Dict, Any, Tuple
import math

from research.candidates import (
    REJECT_NEGATIVE_EXPECTANCY, REJECT_POOR_PROFIT_FACTOR,
    REJECT_EXCESS_DRAWDOWN, REJECT_TOO_FEW_TRADES, REJECT_TRAIN_VALID_GAP,
    REJECT_UNSTABLE_WALK_FORWARD, REJECT_REGIME_DEPENDENT,
    REJECT_COST_SENSITIVE, REJECT_CAPACITY_FAIL, REJECT_TOO_COMPLEX,
    REJECT_UNREALISTIC, REJECT_PARAM_UNSTABLE, REJECT_SINGLE_MARKET,
)


@dataclass
class GateConfig:
    min_trades: int = 10
    min_expectancy: float = 0.0
    min_profit_factor: float = 1.0
    max_drawdown: float = 0.30
    max_train_valid_gap: float = 0.75  # relative expectancy drop train->valid
    min_wf_positive_share: float = 0.5  # folds with expectancy > 0
    max_wf_fold_concentration: float = 0.8  # best fold share of gross
    max_regime_concentration: float = 0.8  # best regime share of gross
    max_regime_loss: float = 0.0  # extra tolerance below the worst-regime bar
    min_stressed_profit_factor: float = 0.7  # stressed (max-multiplier) scenario
    min_capacity_capital: float = 20000.0
    max_participation: float = 0.25
    max_complexity: int = 9
    max_sharpe: float = 6.0  # above = suspiciously unrealistic
    max_win_rate: float = 0.95  # above (with n>=30) = suspicious
    min_stability_rate: float = 0.5  # neighbour pass share
    min_profitable_share: float = 1.0  # symbols with expectancy > 0
    turnover_edge_ratio: float = 0.0  # reserved: min net/turnover (0 = off)


def _num(m: Dict[str, Any], key: str, default: float = 0.0) -> float:
    """Finite-float accessor: NaN/Inf/'inf'/'-inf'/None -> default (reject-safe).

    Non-finite metrics must never pass a gate by comparison accident
    (e.g. inf expectancy is not an edge; NaN drawdown is not safety).
    Exception: `profit_factor == +inf` is legitimate (wins, zero losses)
    and is preserved as +inf so perfect-win candidates are not penalized.
    """
    try:
        v = m.get(key, default)
        if v in (None, "nan", "NaN"):
            return default
        if key == "profit_factor" and v in ("inf", "+inf", "Infinity", "+Infinity"):
            return float("inf")
        if v in ("inf", "-inf", "+inf", "-Infinity", "Infinity"):
            return default
        f = float(v)
        if not math.isfinite(f):
            if key == "profit_factor" and f == float("inf"):
                return float("inf")
            return default
        return f
    except (TypeError, ValueError):
        return default


def _is_finite_value(v, *, allow_pf_inf: bool = False) -> bool:
    try:
        if v in (None, "nan", "NaN"):
            return False
        if allow_pf_inf and v in ("inf", "+inf", "Infinity", "+Infinity"):
            return True
        if v in ("inf", "-inf", "+inf", "-Infinity", "Infinity"):
            return False
        f = float(v)
        if not math.isfinite(f):
            return bool(allow_pf_inf and f == float("inf"))
        return True
    except (TypeError, ValueError):
        return False


def _reject_nonfinite(m: Dict[str, Any], keys, where: str):
    for k in keys:
        if k in m and not _is_finite_value(m[k], allow_pf_inf=(k == "profit_factor")):
            return f"{REJECT_UNREALISTIC}:{where}_{k}={m.get(k)}"
    return ""


def gate_train(candidate, m: Dict[str, Any], cfg: GateConfig) -> Tuple[bool, str]:
    bad = _reject_nonfinite(m, ('expectancy', 'profit_factor', 'max_drawdown',
                                'sharpe_5m_annualized', 'sharpe_annualized',
                                'win_rate'), "train")
    if bad:
        return False, bad
    if int(m.get('n_closed', 0)) < cfg.min_trades:
        return False, f"{REJECT_TOO_FEW_TRADES}:train_n={m.get('n_closed', 0)}"
    if _num(m, 'max_drawdown') > cfg.max_drawdown:
        return False, f"{REJECT_EXCESS_DRAWDOWN}:train_dd={m.get('max_drawdown')}"
    if _num(m, 'expectancy') < 0:
        return False, f"{REJECT_NEGATIVE_EXPECTANCY}:train_exp={m.get('expectancy')}"
    ok, why = _realism_check(m, cfg, "train")
    if not ok:
        return False, why
    return True, ""


def gate_validation(candidate, train_m: Dict[str, Any], valid_m: Dict[str, Any],
                    cfg: GateConfig) -> Tuple[bool, str]:
    bad = _reject_nonfinite(valid_m, ('expectancy', 'profit_factor', 'max_drawdown',
                                      'sharpe_5m_annualized', 'sharpe_annualized',
                                      'win_rate'), "validation")
    if bad:
        return False, bad
    bad = _reject_nonfinite(train_m, ('expectancy',), "train")
    if bad:
        return False, bad
    if int(valid_m.get('n_closed', 0)) < cfg.min_trades:
        return False, f"{REJECT_TOO_FEW_TRADES}:valid_n={valid_m.get('n_closed', 0)}"
    if _num(valid_m, 'expectancy') <= cfg.min_expectancy:
        return False, f"{REJECT_NEGATIVE_EXPECTANCY}:valid_exp={valid_m.get('expectancy')}"
    if _num(valid_m, 'profit_factor') < cfg.min_profit_factor:
        return False, f"{REJECT_POOR_PROFIT_FACTOR}:valid_pf={valid_m.get('profit_factor')}"
    if _num(valid_m, 'max_drawdown') > cfg.max_drawdown:
        return False, f"{REJECT_EXCESS_DRAWDOWN}:valid_dd={valid_m.get('max_drawdown')}"
    te, ve = _num(train_m, 'expectancy'), _num(valid_m, 'expectancy')
    if te > 0:
        gap = (te - ve) / te
        if gap > cfg.max_train_valid_gap:
            return False, f"{REJECT_TRAIN_VALID_GAP}:gap={gap:.2f}"
    ok, why = _realism_check(valid_m, cfg, "validation")
    if not ok:
        return False, why
    if int(getattr(candidate, 'complexity', 0)) > cfg.max_complexity:
        return False, f"{REJECT_TOO_COMPLEX}:complexity={candidate.complexity}"
    return True, ""


def gate_walk_forward(candidate, fold_metrics: list, cfg: GateConfig) -> Tuple[bool, str]:
    if not fold_metrics:
        return False, f"{REJECT_TOO_FEW_TRADES}:no_folds"
    pos = sum(1 for f in fold_metrics if _num(f, 'expectancy') > 0)
    if pos / len(fold_metrics) < cfg.min_wf_positive_share:
        return False, (f"{REJECT_UNSTABLE_WALK_FORWARD}:"
                       f"positive_folds={pos}/{len(fold_metrics)}")
    gross = [_num(f, 'gross_win', 0.0) + abs(_num(f, 'gross_loss', 0.0)) for f in fold_metrics]
    tot = sum(gross)
    if tot > 0 and max(gross) / tot > cfg.max_wf_fold_concentration:
        return False, (f"{REJECT_UNSTABLE_WALK_FORWARD}:"
                       f"fold_concentration={max(gross) / tot:.2f}")
    if any(_num(f, 'max_drawdown') > cfg.max_drawdown for f in fold_metrics):
        return False, f"{REJECT_EXCESS_DRAWDOWN}:wf_fold_dd"
    return True, ""


def gate_cost_stress(candidate, base_m: Dict[str, Any], stressed_m: Dict[str, Any],
                     cfg: GateConfig) -> Tuple[bool, str]:
    spf = _num(stressed_m, 'profit_factor')
    if spf < cfg.min_stressed_profit_factor:
        return False, f"{REJECT_COST_SENSITIVE}:stressed_pf={stressed_m.get('profit_factor')}"
    return True, ""


def gate_regime(candidate, regime_metrics: Dict[str, Dict[str, Any]],
                cfg: GateConfig) -> Tuple[bool, str]:
    grosses = {k: _num(v, 'gross_win', 0.0) + abs(_num(v, 'gross_loss', 0.0))
               for k, v in regime_metrics.items() if not v.get('skipped')}
    tot = sum(grosses.values())
    if tot > 0 and max(grosses.values()) / tot > cfg.max_regime_concentration:
        return False, (f"{REJECT_REGIME_DEPENDENT}:"
                       f"concentration={max(grosses.values()) / tot:.2f}")
    worst = min((_num(v, 'expectancy') for v in regime_metrics.values()
                 if not v.get('skipped')), default=0.0)
    valid_exp = _num(regime_metrics.get('validation', {}), 'expectancy', 0.0)
    # A regime performing worse than validation by more than |validation|
    # (+ configured tolerance) signals regime dependence.
    if worst < valid_exp - abs(valid_exp) - cfg.max_regime_loss - 1e-9:
        return False, f"{REJECT_REGIME_DEPENDENT}:worst_regime_exp={worst:.4f}"
    return True, ""


def gate_capacity(candidate, capacity_summary: Dict[str, Any],
                  base_metrics: Dict[str, Any], cfg: GateConfig) -> Tuple[bool, str]:
    cap = capacity_summary.get('max_survivable_capital')
    if not cap or cap < cfg.min_capacity_capital:
        return False, f"{REJECT_CAPACITY_FAIL}:max_capital={cap}"
    if _num(base_metrics, 'max_participation') > cfg.max_participation:
        return False, (f"{REJECT_CAPACITY_FAIL}:"
                       f"base_participation={base_metrics.get('max_participation')}")
    return True, ""


def gate_cross_market(candidate, per_symbol: Dict[str, Dict[str, Any]],
                      cfg: GateConfig) -> Tuple[bool, str]:
    """Cross-market robustness: no single-market wonders.

    `per_symbol` maps symbol -> {'valid': metrics, 'wf': [fold metrics]}.
    Requires every market profitable (share), healthy median expectancy/PF
    and bounded worst-market drawdown.
    """
    from research.candidates import REJECT_SINGLE_MARKET
    syms = sorted(per_symbol.keys())
    if not syms:
        return False, f"{REJECT_SINGLE_MARKET}:no_markets"
    exps = [_num(per_symbol[s].get('valid', {}), 'expectancy') for s in syms]
    pfs = [_num(per_symbol[s].get('valid', {}), 'profit_factor') for s in syms]
    dds = [_num(per_symbol[s].get('valid', {}), 'max_drawdown') for s in syms]
    n = len(syms)
    share = sum(1 for e in exps if e > 0) / n
    if share < cfg.min_profitable_share - 1e-9:
        bad = [s for s in syms
               if _num(per_symbol[s].get('valid', {}), 'expectancy') <= 0]
        return False, (f"{REJECT_SINGLE_MARKET}:profitable_share={share:.2f} "
                       f"losers={bad}")
    med_exp = sorted(exps)[n // 2]
    if med_exp <= cfg.min_expectancy:
        return False, f"{REJECT_SINGLE_MARKET}:median_expectancy={med_exp:.4f}"
    med_pf = sorted(pfs)[n // 2]
    if med_pf < cfg.min_profit_factor:
        return False, f"{REJECT_SINGLE_MARKET}:median_pf={med_pf:.3f}"
    worst_dd = max(dds) if dds else 0.0
    if worst_dd > cfg.max_drawdown:
        return False, f"{REJECT_EXCESS_DRAWDOWN}:worst_market_dd={worst_dd:.4f}"
    return True, ""


def _realism_check(m: Dict[str, Any], cfg: GateConfig, where: str) -> Tuple[bool, str]:
    n = int(m.get('n_closed', 0))
    if _num(m, 'sharpe_5m_annualized') > cfg.max_sharpe:
        return False, f"{REJECT_UNREALISTIC}:{where}_sharpe={m.get('sharpe_5m_annualized')}"
    if n >= 30 and _num(m, 'win_rate') > cfg.max_win_rate:
        return False, f"{REJECT_UNREALISTIC}:{where}_winrate={m.get('win_rate')}"
    return True, ""
