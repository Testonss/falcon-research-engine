"""Reproducible candidate generation: random search, grids, perturbation.

Deterministic for a given seed. Search spaces come from
`research.strategies.param_space` (explicit + inspectable).
A minimal `Optimizer` abstraction leaves room for future Bayesian
optimization without adding dependencies today.
"""
import itertools
import random
from abc import ABC, abstractmethod
from datetime import timezone
from typing import Dict, Any, List, Optional

from research.candidates import (
    StrategyCandidate, candidate_fingerprint, candidate_id, _utcnow,
)
from research.strategies import param_space, complexity_of, family_names


def make_candidate(family: str, params: Dict[str, Any], seed: int,
                   parent_id: Optional[str] = None,
                   dataset_version: str = "") -> StrategyCandidate:
    fp = candidate_fingerprint(family, params)
    return StrategyCandidate(
        candidate_id=candidate_id(fp, seed),
        fingerprint=fp,
        family=family,
        params=dict(params),
        seed=seed,
        created_at=_utcnow().isoformat(),
        parent_id=parent_id,
        complexity=complexity_of(family, params),
        dataset_version=dataset_version,
    )


def random_search(families: List[str], n: int, seed: int,
                  dataset_version: str = "") -> List[StrategyCandidate]:
    """Sample n candidates (family uniform, params uniform in space)."""
    rng = random.Random(seed)
    fams = sorted(families)
    out = []
    for i in range(n):
        family = rng.choice(fams)
        space = param_space(family)
        params = {k: rng.choice(list(v)) for k, v in space.items()}
        out.append(make_candidate(family, params, seed * 100003 + i,
                                  dataset_version=dataset_version))
    return out


def constrained_grid(family: str, seed: int,
                     limits: Optional[Dict[str, List[Any]]] = None,
                     max_candidates: int = 64,
                     dataset_version: str = "") -> List[StrategyCandidate]:
    """Cartesian grid over space (optionally restricted), capped deterministically."""
    space = param_space(family)
    if limits:
        for k, vals in limits.items():
            if k not in space:
                raise ValueError(f"Param {k!r} not in space for {family!r}")
            unknown = [v for v in vals if v not in space[k]]
            if unknown:
                raise ValueError(f"Values {unknown} outside space for {k!r}")
            space[k] = sorted(vals)
    keys = sorted(space.keys())
    combos = list(itertools.product(*[space[k] for k in keys]))
    combos = combos[:max_candidates]
    return [make_candidate(family, dict(zip(keys, c)), seed * 100003 + i,
                           dataset_version=dataset_version)
            for i, c in enumerate(combos)]


def perturb(candidate: StrategyCandidate, seed: int,
            n: int = 4) -> List[StrategyCandidate]:
    """One-step neighbours: each varies exactly one param to an adjacent value."""
    rng = random.Random(seed)
    space = param_space(candidate.family)
    neighbours = []
    keys = sorted(space.keys())
    for key in keys:
        values = list(space[key])
        try:
            idx = values.index(candidate.params[key])
        except (ValueError, KeyError):
            continue
        for step in (-1, 1):
            j = idx + step
            if 0 <= j < len(values):
                params = dict(candidate.params)
                params[key] = values[j]
                neighbours.append(make_candidate(
                    candidate.family, params, seed * 100003 + len(neighbours),
                    parent_id=candidate.candidate_id,
                    dataset_version=candidate.dataset_version))
    rng.shuffle(neighbours)
    return neighbours[:n]


class Optimizer(ABC):
    """Extension point for future sample-efficient search (e.g. Bayesian)."""

    @abstractmethod
    def propose(self, history: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
        """Propose n param dicts given past {params, score} history."""

    @abstractmethod
    def name(self) -> str:
        ...


class RandomOptimizer(Optimizer):
    """Baseline optimizer: seeded uniform proposals (reproducible)."""

    def __init__(self, family: str, seed: int):
        self.family = family
        self._rng = random.Random(seed)
        self._seed = seed

    def name(self) -> str:
        return f"random:{self.family}:{self._seed}"

    def propose(self, history: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
        space = param_space(self.family)
        return [{k: self._rng.choice(list(v)) for k, v in space.items()}
                for _ in range(n)]
