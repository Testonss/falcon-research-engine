"""Solana token candidate scoring. Research-only; no transaction execution.

All features must be observable at decision time. Scores are heuristics,
NOT calibrated probabilities or evidence of positive expected value.
"""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class TokenSnapshot:
    mint: str
    observed_at: int
    liquidity_usd: float
    volume_5m_usd: float
    age_minutes: float
    top10_holder_pct: float
    price_impact_pct: float
    mint_authority_enabled: bool
    freeze_authority_enabled: bool


@dataclass(frozen=True)
class HunterDecision:
    mint: str
    eligible: bool
    score: float
    reasons: tuple[str, ...]


def evaluate_token(s: TokenSnapshot) -> HunterDecision:
    """Conservative screening; missing/unreliable values must not be fabricated."""
    values = (s.liquidity_usd, s.volume_5m_usd, s.age_minutes,
              s.top10_holder_pct, s.price_impact_pct)
    reasons = []
    if not s.mint or s.observed_at <= 0 or not all(isfinite(v) for v in values):
        return HunterDecision(s.mint, False, 0.0, ("invalid_snapshot",))
    if s.liquidity_usd < 25000:
        reasons.append("low_liquidity")
    if s.volume_5m_usd < 5000:
        reasons.append("low_volume")
    if s.age_minutes < 10:
        reasons.append("too_new")
    if not 0 <= s.top10_holder_pct <= 100 or s.top10_holder_pct > 60:
        reasons.append("holder_concentration")
    if not 0 <= s.price_impact_pct <= 100 or s.price_impact_pct > 2:
        reasons.append("price_impact")
    if s.mint_authority_enabled:
        reasons.append("mint_authority")
    if s.freeze_authority_enabled:
        reasons.append("freeze_authority")
    if reasons:
        return HunterDecision(s.mint, False, 0.0, tuple(reasons))
    # Ranking only; never interpret as a win probability.
    liquidity_score = min(s.liquidity_usd / 150000, 1.0)
    volume_score = min(s.volume_5m_usd / 50000, 1.0)
    concentration_score = 1 - s.top10_holder_pct / 100
    impact_score = 1 - s.price_impact_pct / 2
    score = round(0.30 * liquidity_score + 0.30 * volume_score
                  + 0.20 * concentration_score + 0.20 * impact_score, 6)
    return HunterDecision(s.mint, True, score, ())
