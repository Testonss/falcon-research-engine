"""Strategy candidates: versioned, reproducible, with a full lifecycle.

A candidate that fails honestly is a successful result: every rejection is
recorded with a machine-readable reason and never deleted.
"""
import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Any, List, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CandidateStatus(str, Enum):
    GENERATED = "generated"
    TRAIN_EVALUATED = "train_evaluated"
    VALIDATED = "validated"
    WALK_FORWARD_PASSED = "walk_forward_passed"
    COST_STRESS_PASSED = "cost_stress_passed"
    REGIME_PASSED = "regime_passed"
    CAPACITY_PASSED = "capacity_passed"
    PAPER_CANDIDATE = "paper_candidate"
    REJECTED = "rejected"


# Machine-readable rejection reasons (stable strings for reports/tests).
REJECT_NEGATIVE_EXPECTANCY = "negative_expectancy"
REJECT_POOR_PROFIT_FACTOR = "poor_profit_factor"
REJECT_EXCESS_DRAWDOWN = "excess_drawdown"
REJECT_TOO_FEW_TRADES = "insufficient_trades"
REJECT_TRAIN_VALID_GAP = "train_validation_gap"
REJECT_UNSTABLE_WALK_FORWARD = "unstable_walk_forward"
REJECT_REGIME_DEPENDENT = "regime_dependent"
REJECT_COST_SENSITIVE = "cost_sensitive"
REJECT_CAPACITY_FAIL = "capacity_fail"
REJECT_TOO_COMPLEX = "excess_complexity"
REJECT_UNREALISTIC = "unrealistic_performance"
REJECT_PARAM_UNSTABLE = "param_unstable"
REJECT_DUPLICATE = "duplicate_candidate"
REJECT_SINGLE_MARKET = "single_market_only"
REJECT_HOLDOUT_FAIL = "holdout_fail"


def candidate_fingerprint(family: str, params: Dict[str, Any]) -> str:
    """Stable identity for (family, params): blocks exact duplicates."""
    payload = json.dumps({"family": family, "params": params}, sort_keys=True,
                         default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def candidate_id(fingerprint: str, seed: int) -> str:
    payload = f"{fingerprint}|{seed}"
    return "cand_" + hashlib.sha256(payload.encode()).hexdigest()[:12]


def experiment_fingerprint(strategy_fingerprint: str,
                           dataset_hashes: Dict[str, str],
                           split_protocol: Dict[str, Any],
                           holdout_ratio: float,
                           execution_model: str,
                           cost_model: Dict[str, Any],
                           gates: Dict[str, Any],
                           protocol_version: str,
                           seed: Any) -> str:
    """Stable identity for one experiment: strategy × data × protocol × seed.

    Two runs produce the same fingerprint only if the strategy, the exact
    dataset content hashes, the split/embargo/warmup protocol, the holdout
    ratio, the execution model, the full cost model, the complete gate
    thresholds, the research protocol version and the seed all match.
    Anything else is a different experiment by construction.
    """
    payload = json.dumps({
        "strategy": strategy_fingerprint,
        "datasets": dict(sorted(dataset_hashes.items())),
        "splits": split_protocol,
        "holdout_ratio": holdout_ratio,
        "execution_model": execution_model,
        "costs": cost_model,
        "gates": gates,
        "protocol": protocol_version,
        "seed": seed,
    }, sort_keys=True, default=str)
    return "exp_" + hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class EvaluationRecord:
    stage: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    passed: bool = False
    reason: str = ""
    timestamp: str = ""


@dataclass
class StrategyCandidate:
    candidate_id: str
    fingerprint: str
    family: str
    params: Dict[str, Any]
    seed: int
    created_at: str
    parent_id: Optional[str]
    complexity: int
    status: str = CandidateStatus.GENERATED.value
    stage: str = CandidateStatus.GENERATED.value
    evaluations: List[Dict[str, Any]] = field(default_factory=list)
    rejection_reason: str = ""
    rejection_stage: str = ""
    research_score: Optional[float] = None
    score_components: Dict[str, float] = field(default_factory=dict)
    dataset_version: str = ""
    promoted_paper_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "StrategyCandidate":
        return StrategyCandidate(**d)

    def record_evaluation(self, stage: str, metrics: Dict[str, Any],
                          passed: bool, reason: str = "") -> None:
        self.evaluations.append({
            "stage": stage, "metrics": dict(metrics), "passed": passed,
            "reason": reason, "timestamp": _utcnow().isoformat(),
        })
        if passed:
            self.status = stage
            self.stage = stage
        else:
            self.status = CandidateStatus.REJECTED.value
            self.stage = stage
            self.rejection_reason = reason
            self.rejection_stage = stage

    @property
    def is_rejected(self) -> bool:
        return self.status == CandidateStatus.REJECTED.value


class CandidateRegistry:
    """In-memory registry with exact-duplicate prevention."""

    def __init__(self):
        self._by_id: Dict[str, StrategyCandidate] = {}
        self._by_fingerprint: Dict[str, str] = {}

    def register(self, candidate: StrategyCandidate) -> StrategyCandidate:
        """Register; returns the EXISTING candidate on fingerprint duplicate."""
        existing_id = self._by_fingerprint.get(candidate.fingerprint)
        if existing_id is not None:
            return self._by_id[existing_id]
        self._by_id[candidate.candidate_id] = candidate
        self._by_fingerprint[candidate.fingerprint] = candidate.candidate_id
        return candidate

    def get(self, candidate_id: str) -> Optional[StrategyCandidate]:
        return self._by_id.get(candidate_id)

    def all(self) -> List[StrategyCandidate]:
        return list(self._by_id.values())

    def survivors(self) -> List[StrategyCandidate]:
        return [c for c in self._by_id.values() if not c.is_rejected]

    def rejected(self) -> List[StrategyCandidate]:
        return [c for c in self._by_id.values() if c.is_rejected]

    def __len__(self) -> int:
        return len(self._by_id)
