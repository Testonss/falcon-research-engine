"""Transparent multi-metric research score. Every component is visible.

Ranked by score — never by net PnL alone. Components in [0,1] (penalty may
be negative); weights sum to 1. All components are returned for reports.
"""
import math
from typing import Dict, Any, Tuple

SCORE_WEIGHTS = {
    'expectancy': 0.18,
    'profit_factor': 0.14,
    'oos_stability': 0.12,
    'wf_consistency': 0.12,
    'drawdown': 0.08,
    'cost_robustness': 0.10,
    'regime_balance': 0.08,
    'sample_size': 0.06,
    'param_stability': 0.06,
    'capacity': 0.04,
    'complexity_penalty': 0.02,
}


def _clip01(x: float) -> float:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(f):
        return 0.0
    return max(0.0, min(1.0, f))


def research_score(valid_m: Dict[str, Any], train_m: Dict[str, Any],
                   wf_folds: list, stability_rate: float,
                   base_cost_net: float, stressed_net: float,
                   regime_concentration: float,
                   survivable_capital: Any,
                   complexity: int) -> Tuple[float, Dict[str, float]]:
    """Compute (score, components). Pure function, fully inspectable."""
    def num(m, k, d=0.0):
        try:
            v = m.get(k, d)
            if v in (None, "nan", "NaN"):
                return float(d)
            if k == "profit_factor" and v in ("inf", "+inf", "Infinity", "+Infinity"):
                return float("inf")
            if v in ("inf", "-inf", "+inf", "-Infinity", "Infinity"):
                return float(d)
            f = float(v)
            if not math.isfinite(f):
                if k == "profit_factor" and f == float("inf"):
                    return float("inf")
                return float(d)
            return f
        except (TypeError, ValueError):
            return float(d)

    exp = num(valid_m, 'expectancy')
    pf = num(valid_m, 'profit_factor')
    te = num(train_m, 'expectancy')
    gap = ((te - exp) / te) if te > 0 else (0.0 if exp >= 0 else 1.0)
    pos_share = (sum(1 for f in wf_folds if num(f, 'expectancy') > 0)
                 / len(wf_folds)) if wf_folds else 0.0
    dd = num(valid_m, 'max_drawdown')
    n = int(valid_m.get('n_closed', 0))
    if base_cost_net > 0:
        retention = stressed_net / base_cost_net
    else:
        retention = 0.0
    try:
        cap = float(survivable_capital) if survivable_capital else 0.0
    except (TypeError, ValueError):
        cap = 0.0

    components = {
        'expectancy': _clip01(math.tanh(exp / 5.0)),
        'profit_factor': _clip01(min(pf, 3.0) / 3.0),
        'oos_stability': _clip01(1.0 - max(0.0, gap)),
        'wf_consistency': _clip01(pos_share),
        'drawdown': _clip01(1.0 - min(1.0, dd / 0.30)),
        'cost_robustness': _clip01(max(0.0, retention)),
        'regime_balance': _clip01(1.0 - regime_concentration),
        'sample_size': _clip01(n / 50.0),
        'param_stability': _clip01(stability_rate),
        'capacity': _clip01(math.log10(max(cap, 1.0) / 10000.0) / 3.0)
        if cap > 0 else 0.0,
        'complexity_penalty': -0.02 * complexity,
    }
    score = sum(SCORE_WEIGHTS[k] * components[k] for k in SCORE_WEIGHTS)
    return max(0.0, min(1.0, score)), components


def cross_market_score(per_symbol_valid: Dict[str, Dict[str, Any]]) -> Tuple[float, Dict[str, float]]:
    """Weakest-link consistency across markets. Pure, inspectable.

    Single market -> (1.0, neutral components): ranking then coincides with
    the primary score and verified single-symbol behaviour is preserved.
    """
    syms = sorted(per_symbol_valid.keys())
    if len(syms) <= 1:
        return 1.0, {'profitable_share': 1.0, 'median_expectancy': 1.0,
                     'median_pf': 1.0, 'worst_dd': 1.0}

    def num(m, k, d=0.0):
        try:
            v = m.get(k, d)
            if v in (None, "nan", "NaN"):
                return float(d)
            if k in ("profit_factor", "median_pf") and v in ("inf", "+inf", "Infinity", "+Infinity"):
                return float("inf")
            if v in ("inf", "-inf", "+inf", "-Infinity", "Infinity"):
                return float(d)
            f = float(v)
            if not math.isfinite(f):
                if k in ("profit_factor", "median_pf") and f == float("inf"):
                    return float("inf")
                return float(d)
            return f
        except (TypeError, ValueError):
            return float(d)

    exps = [num(per_symbol_valid[s], 'expectancy') for s in syms]
    pfs = [num(per_symbol_valid[s], 'profit_factor') for s in syms]
    dds = [num(per_symbol_valid[s], 'max_drawdown') for s in syms]
    share = sum(1 for e in exps if e > 0) / len(syms)
    med_exp = sorted(exps)[len(exps) // 2]
    med_pf = sorted(pfs)[len(pfs) // 2]
    worst_dd = max(dds) if dds else 0.0
    components = {
        'profitable_share': _clip01(share),
        'median_expectancy': _clip01(math.tanh(med_exp / 5.0)),
        'median_pf': _clip01(min(med_pf, 3.0) / 3.0),
        'worst_dd': _clip01(1.0 - min(1.0, worst_dd / 0.30)),
    }
    score = sum(components.values()) / len(components)
    return max(0.0, min(1.0, score)), components
