"""Parameter-neighbourhood stability: does the edge survive nearby params?

A candidate that only works at one exact parameter combination receives a
stability penalty (or rejection). Neighbours vary exactly one param to an
adjacent allowed value (see research.generate.perturb).
"""
import asyncio
from typing import Callable, Dict, Any, List

from research.generate import perturb


async def neighbourhood_report(candidate, eval_fn: Callable, seed: int,
                               n: int = 4) -> Dict[str, Any]:
    """Evaluate up to n neighbours with eval_fn(params)->metrics.

    eval_fn must be deterministic and side-effect free w.r.t. research
    inputs. Returns pass_rate, expectancy range, and neighbour table.
    """
    neighbours = perturb(candidate, seed, n=n)
    rows = []
    for nb in neighbours:
        metrics = await eval_fn(nb.params)
        rows.append({"params": dict(nb.params),
                     "expectancy": float(metrics.get('expectancy', 0.0)),
                     "n_closed": int(metrics.get('n_closed', 0)),
                     "passed": bool(metrics.get('expectancy', 0.0) > 0
                                    and int(metrics.get('n_closed', 0)) > 0)})
    tested = len(rows)
    passed = sum(1 for r in rows if r["passed"])
    exps = [r["expectancy"] for r in rows]
    scores = [r["expectancy"] for r in rows]
    median_score = sorted(scores)[len(scores) // 2] if scores else 0.0
    if len(scores) > 1:
        mean = sum(scores) / len(scores)
        dispersion = (sum((x - mean) ** 2 for x in scores) / (len(scores) - 1)) ** 0.5
    else:
        dispersion = 0.0
    return {
        "candidate_id": candidate.candidate_id,
        "tested": tested,
        "passed": passed,
        "pass_rate": (passed / tested) if tested else 0.0,
        "pct_acceptable": (passed / tested) if tested else 0.0,
        "median_score": median_score,
        "worst_score": min(scores) if scores else 0.0,
        "best_score": max(scores) if scores else 0.0,
        "dispersion": dispersion,
        "min_expectancy": min(exps) if exps else 0.0,
        "max_expectancy": max(exps) if exps else 0.0,
        "neighbours": rows,
    }


def run_neighbourhood(candidate, eval_fn, seed: int, n: int = 4) -> Dict[str, Any]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        raise RuntimeError("run_neighbourhood needs a non-running loop; "
                           "await neighbourhood_report() instead")
    return asyncio.run(neighbourhood_report(candidate, eval_fn, seed, n))
