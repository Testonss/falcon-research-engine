"""Optional Bayesian optimizer: exact GP + Expected Improvement, stdlib only.

Operates on the research SCORE (never raw PnL), over explicit bounded
spaces from `research.strategies.param_space`. Deterministic given a seed.
Random/grid search remain supported and are the default; this optimizer is
opt-in (`research/generate.py::Optimizer` interface). It never touches the
sealed holdout — it only sees (params, score) history handed to it.
"""
import math
import random
from typing import Dict, Any, List

from research.generate import Optimizer
from research.strategies import param_space


def _cholesky(matrix):
    """Cholesky decomposition (lower). Raises ValueError if not PD."""
    n = len(matrix)
    lower = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = matrix[i][j] - sum(lower[i][k] * lower[j][k] for k in range(j))
            if i == j:
                if s <= 0:
                    raise ValueError("non-PD kernel matrix")
                lower[i][j] = math.sqrt(s)
            else:
                lower[i][j] = s / lower[j][j]
    return lower


def _forward(lower, vec):
    n = len(lower)
    out = [0.0] * n
    for i in range(n):
        out[i] = (vec[i] - sum(lower[i][k] * out[k] for k in range(i))) / lower[i][i]
    return out


def _backward(lower, vec):
    n = len(lower)
    out = [0.0] * n
    for i in reversed(range(n)):
        out[i] = (vec[i] - sum(lower[k][i] * out[k] for k in range(i + 1, n))) / lower[i][i]
    return out


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


class BayesianOptimizer(Optimizer):
    """GP-EI optimizer over one family's bounded space.

    Categorical params are encoded as normalized indices. Deterministic:
    acquisition is maximized over a seeded random candidate pool.
    """

    def __init__(self, family: str, seed: int, lengthscale: float = 0.25,
                 noise: float = 1e-3, pool_size: int = 512):
        self.family = family
        self._seed = seed
        self._rng = random.Random(seed)
        self._lengthscale = lengthscale
        self._noise = noise
        self._pool_size = pool_size
        space = param_space(family)
        self._keys = sorted(space.keys())
        self._values = [list(space[k]) for k in self._keys]

    def name(self) -> str:
        return f"bayes:{self.family}:{self._seed}"

    def _encode(self, params: Dict[str, Any]) -> List[float]:
        vec = []
        for k, vals in zip(self._keys, self._values):
            idx = vals.index(params[k])
            vec.append(idx / max(1, len(vals) - 1))
        return vec

    def _decode(self, vec: List[float]) -> Dict[str, Any]:
        out = {}
        for k, vals, v in zip(self._keys, self._values, vec):
            idx = min(len(vals) - 1, max(0, int(round(v * (len(vals) - 1)))))
            out[k] = vals[idx]
        return out

    def _kernel(self, a: List[float], b: List[float]) -> float:
        d2 = sum((x - y) ** 2 for x, y in zip(a, b))
        return math.exp(-d2 / (2.0 * self._lengthscale ** 2))

    def _predict(self, xs: List[List[float]], ys: List[float],
                 cand: List[float]):
        n = len(xs)
        if n == 0:
            return 0.0, 1.0
        gram = [[self._kernel(a, b) + (self._noise if i == j else 0.0)
                 for j, b in enumerate(xs)] for i, a in enumerate(xs)]
        try:
            lower = _cholesky(gram)
        except ValueError:
            return sum(ys) / n, 1.0
        alpha = _backward(lower, _forward(lower, list(ys)))
        k_star = [self._kernel(a, cand) for a in xs]
        mean = sum(k * al for k, al in zip(k_star, alpha))
        v = _forward(lower, k_star)
        var = max(1.0 + self._noise - sum(x * x for x in v), 1e-9)
        return mean, math.sqrt(var)

    def propose(self, history: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
        """EI proposals; empty history -> seeded uniform (exploration first)."""
        if not history:
            return [{k: self._rng.choice(list(v))
                     for k, v in zip(self._keys, self._values)} for _ in range(n)]
        xs = [self._encode(h["params"]) for h in history]
        ys = [float(h["score"]) for h in history]
        best = max(ys)
        scored = []
        for _ in range(self._pool_size):
            vec = [self._rng.random() for _ in self._keys]
            mean, std = self._predict(xs, ys, vec)
            z = (mean - best) / std if std > 0 else 0.0
            ei = (mean - best) * _norm_cdf(z) + std * _norm_pdf(z)
            scored.append((ei, vec))
        scored.sort(key=lambda t: t[0], reverse=True)
        out, seen = [], set()
        for _, vec in scored:
            params = self._decode(vec)
            key = tuple(sorted(params.items()))
            if key in seen:
                continue
            seen.add(key)
            out.append(params)
            if len(out) >= n:
                break
        return out


def train_objective(m: Dict[str, Any]) -> float:
    """Multi-metric train objective for search (NOT raw PnL).

    Combines expectancy, profit factor and drawdown; returns -1.0 when the
    sample is too small to mean anything. Pure and deterministic.
    """
    import math
    n = int(m.get('n_closed', 0) or 0)
    if n < 3:
        return -1.0
    try:
        pf = float(m.get('profit_factor', 0.0) or 0.0)
    except (TypeError, ValueError):
        pf = 0.0
    if pf != pf:  # NaN guard
        pf = 0.0
    if not math.isfinite(pf):
        pf = 0.0
    try:
        exp = float(m.get('expectancy', 0.0) or 0.0)
    except (TypeError, ValueError):
        exp = 0.0
    if not math.isfinite(exp):
        exp = 0.0
    try:
        dd = float(m.get('max_drawdown', 0.0) or 0.0)
    except (TypeError, ValueError):
        dd = 0.0
    if not math.isfinite(dd):
        dd = 0.0
    return (0.5 * math.tanh(exp / 5.0)
            + 0.3 * min(max(pf, 0.0), 3.0) / 3.0
            + 0.2 * (1.0 - min(1.0, max(dd, 0.0) / 0.30)))
