"""Offline chronological replay of observed Solana prices; no wallet or orders.

Assumes snapshots are from the same token and already collected at observation
time. A fixed holding horizon is used; no future data is used for selection.
"""
from dataclasses import dataclass
from math import isfinite
from research.solana_paper import PaperCosts, replay_round_trip


@dataclass(frozen=True)
class PriceObservation:
    mint: str
    observed_at: int
    price_usd: float


@dataclass(frozen=True)
class ReplayTrade:
    mint: str
    entry_at: int
    exit_at: int
    entry_price: float
    exit_price: float
    pnl_usd: float
    return_pct: float


def replay_observations(observations, capital_usd=500.0, hold_seconds=300,
                        costs=PaperCosts()):
    """Buy at first observation, sell at first observation >= holding horizon.

    Uses one independent round trip per token, not a shared portfolio.
    Results must not be summed as though capital were simultaneously available.
    Missing exits are reported separately, never treated as successful fills.
    """
    if not isinstance(hold_seconds, int) or hold_seconds <= 0:
        raise ValueError("hold_seconds must be a positive integer")
    if not isfinite(capital_usd) or capital_usd <= 0:
        raise ValueError("invalid capital")
    costs.validate()
    grouped = {}
    for o in observations:
        if not isinstance(o, PriceObservation) or not o.mint or type(o.observed_at) is not int or o.observed_at <= 0 or not isfinite(o.price_usd) or o.price_usd <= 0:
            raise ValueError("invalid price observation")
        grouped.setdefault(o.mint, []).append(o)
    trades, unresolved = [], []
    for mint in sorted(grouped):
        prices = sorted(grouped[mint], key=lambda o: o.observed_at)
        if len({o.observed_at for o in prices}) != len(prices):
            raise ValueError("duplicate observation timestamp")
        entry = prices[0]
        exit_point = next((o for o in prices[1:] if o.observed_at - entry.observed_at >= hold_seconds), None)
        if exit_point is None:
            unresolved.append(mint)
            continue
        result = replay_round_trip(capital_usd, entry.price_usd, exit_point.price_usd, costs)
        trades.append(ReplayTrade(mint, entry.observed_at, exit_point.observed_at,
                                  entry.price_usd, exit_point.price_usd,
                                  result.net_pnl_usd, result.net_return_pct))
    return {"trades": trades, "unresolved_exits": unresolved,
            "trade_count": len(trades), "unresolved_count": len(unresolved)}
