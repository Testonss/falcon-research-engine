"""Multiple-testing / data-snooping defense (honest, no fake statistics).

With many candidates, point estimates overstate the selected best. This
module reports only what the data supports:
- trials count (how many candidates were evaluated),
- a one-sided 95% lower confidence bound (LCB) on expectancy,
- an explicit "insufficient evidence" state when the sample is too small,
- a heuristic snooping warning when many trials meet marginal significance.

No Sharpe-ratio deflation is faked: where a metric cannot be validly
computed, the report says so.
"""
import math
from typing import Dict, Any, List, Optional

Z95 = 1.64
MIN_CLOSES = 10


def expectancy_lcb(trade_pnls: List[float], z: float = Z95,
                   min_n: int = MIN_CLOSES) -> Dict[str, Any]:
    """Lower confidence bound on mean trade PnL. Pure function."""
    n = len(trade_pnls)
    if n < min_n:
        return {"n": n, "sufficient": False, "lcb": None,
                "note": f"insufficient evidence: {n} closes < {min_n} minimum"}
    mean = sum(trade_pnls) / n
    if n < 2:
        return {"n": n, "sufficient": False, "lcb": None,
                "note": "insufficient evidence: need >= 2 closes for variance"}
    var = sum((x - mean) ** 2 for x in trade_pnls) / (n - 1)
    se = math.sqrt(max(var, 0.0)) / math.sqrt(n)
    return {"n": n, "sufficient": True, "mean": mean, "se": se,
            "lcb": mean - z * se,
            "note": f"one-sided {z}-sigma lower bound"}


def snooping_report(candidate_id: str, trade_pnls: List[float], n_trials: int,
                    min_n: int = MIN_CLOSES) -> Dict[str, Any]:
    """Trials-aware selection-bias report for one candidate."""
    lcb = expectancy_lcb(trade_pnls, min_n=min_n)
    mean = lcb.get("mean", 0.0) or 0.0
    warning = bool(n_trials > 50 and lcb.get("sufficient")
                   and mean > 0 and (lcb.get("lcb") or 0.0) <= 0)
    return {
        "candidate_id": candidate_id,
        "n_trials": int(n_trials),
        "n_closed": lcb["n"],
        "sufficient": lcb["sufficient"],
        "mean_expectancy": mean,
        "lcb_95": lcb.get("lcb"),
        "snoop_warning": warning,
        "note": lcb.get("note", ""),
    }
