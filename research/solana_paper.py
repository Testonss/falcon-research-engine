"""Deterministic, research-only Solana round-trip paper execution.

Never places orders, signs transactions, or accesses wallets.
Prices and liquidity must come from point-in-time observations.
"""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class PaperCosts:
    buy_fee_bps: float = 30
    sell_fee_bps: float = 30
    buy_slippage_bps: float = 100
    sell_slippage_bps: float = 100
    priority_fee_usd: float = 0.02
    fixed_network_fee_usd: float = 0.01

    def validate(self):
        values = vars(self).values()
        if any(not isfinite(v) or v < 0 for v in values):
            raise ValueError("invalid costs")
        if self.buy_fee_bps + self.buy_slippage_bps >= 10000:
            raise ValueError("buy costs exceed principal")
        if self.sell_fee_bps + self.sell_slippage_bps >= 10000:
            raise ValueError("sell costs exceed proceeds")


@dataclass(frozen=True)
class PaperResult:
    initial_usd: float
    final_usd: float
    net_pnl_usd: float
    net_return_pct: float
    units_bought: float
    entry_price: float
    exit_price: float


def replay_round_trip(capital_usd, entry_price, exit_price, costs=PaperCosts()):
    """Conservative market buy and sell with explicit bps and fixed costs.

    Does not model route failures, MEV, liquidity changes or inability to exit;
    these must be accounted for before any realistic performance claims.
    """
    costs.validate()
    if any(not isfinite(v) or v <= 0 for v in (capital_usd, entry_price, exit_price)):
        raise ValueError("capital and prices must be positive and finite")
    buy_fixed = costs.priority_fee_usd + costs.fixed_network_fee_usd
    sell_fixed = costs.priority_fee_usd + costs.fixed_network_fee_usd
    if capital_usd <= buy_fixed:
        raise ValueError("insufficient capital for fixed fees")
    effective_entry = entry_price * (1 + costs.buy_slippage_bps / 10000)
    units = (capital_usd - buy_fixed) * (1 - costs.buy_fee_bps / 10000) / effective_entry
    effective_exit = exit_price * (1 - costs.sell_slippage_bps / 10000)
    gross_exit = units * effective_exit
    final = max(0.0, gross_exit * (1 - costs.sell_fee_bps / 10000) - sell_fixed)
    pnl = final - capital_usd
    return PaperResult(capital_usd, final, pnl, 100 * pnl / capital_usd,
                       units, entry_price, exit_price)
