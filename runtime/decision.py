"""Decision engine: signal -> risk gate -> sizing -> executable action.

Pure apart from the injected risk manager (whose ``evaluate_trade`` is a
pure check) and the read-only open-position count. Deterministic given
identical (signal, equity, positions) inputs: no wall-clock except the
``now`` explicitly passed through for risk day/cooldown boundaries (live
paths pass ``None`` -> risk falls back to real time; replay/tests pass the
bar timestamp).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

from runtime.signals import RuntimeSignal
from runtime.sizing import calculate_quantity


@dataclass(frozen=True)
class Decision:
    decision_id: str       # == signal.signal_id (one decision per signal)
    symbol: str
    canonical_id: str
    action: str            # "BUY" | "SELL" | "HOLD" | "SKIP"
    volume: float
    price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    reason: str
    blocked_by: Optional[str] = None


def compute_sltp(price: float, side: str, stop_pct: float, tp_pct: float):
    if side == "BUY":
        return price * (1 - stop_pct), price * (1 + tp_pct)
    if side == "SELL":
        return price * (1 + stop_pct), price * (1 - tp_pct)
    return None, None


class DecisionEngine:
    """Turns deterministic signals into sized, risk-gated decisions."""

    def __init__(
        self,
        risk_manager=None,
        stop_loss_pct: float = 0.01,
        take_profit_pct: float = 0.01,
        risk_per_trade: float = 10.0,
        risk_per_trade_pct: float = 0.01,
        max_position_size: float = 1.0,
        max_concurrent_positions: int = 5,
        min_confidence: float = 0.0,
    ):
        self.risk = risk_manager
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.risk_per_trade = risk_per_trade
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_position_size = max_position_size
        self.max_concurrent_positions = max_concurrent_positions
        self.min_confidence = min_confidence

    def decide(
        self,
        signal: RuntimeSignal,
        equity: float,
        open_positions: int = 0,
        already_positioned_same_side: bool = False,
        broker_rules: Optional[Dict[str, float]] = None,
        now: Optional[datetime] = None,
    ) -> Decision:
        cid = signal.symbol  # caller overwrites with canonical id when known
        if signal.side == "HOLD":
            return Decision(signal.signal_id, signal.symbol, cid, "HOLD", 0.0,
                            signal.price, None, None, f"signal_hold:{signal.reason}")
        if signal.confidence < self.min_confidence:
            return Decision(signal.signal_id, signal.symbol, cid, "SKIP", 0.0,
                            signal.price, None, None, "low_confidence",
                            blocked_by="confidence")
        if open_positions >= self.max_concurrent_positions:
            return Decision(signal.signal_id, signal.symbol, cid, "SKIP", 0.0,
                            signal.price, None, None, "max_positions",
                            blocked_by="max_concurrent_positions")
        if already_positioned_same_side:
            return Decision(signal.signal_id, signal.symbol, cid, "HOLD", 0.0,
                            signal.price, None, None, "already_positioned")
        rules = broker_rules or {}
        qty = calculate_quantity(
            signal.price, equity,
            risk_per_trade=self.risk_per_trade,
            risk_per_trade_pct=self.risk_per_trade_pct,
            stop_distance_pct=self.stop_loss_pct,
            max_position_size=self.max_position_size,
            volume_min=float(rules.get("volume_min", 0.0) or 0.0),
            volume_max=float(rules.get("volume_max", 0.0) or 0.0),
            volume_step=float(rules.get("volume_step", 0.0) or 0.0),
        )
        if qty <= 0:
            return Decision(signal.signal_id, signal.symbol, cid, "SKIP", 0.0,
                            signal.price, None, None, "sizing_zero",
                            blocked_by="sizing")
        if self.risk is not None:
            try:
                allowed, reason = self.risk.evaluate_trade(
                    signal.symbol, qty, signal.price, equity, now=now)
            except TypeError:
                allowed, reason = self.risk.evaluate_trade(
                    signal.symbol, qty, signal.price, equity)
            if not allowed:
                return Decision(signal.signal_id, signal.symbol, cid, "SKIP", 0.0,
                                signal.price, None, None, f"risk:{reason}",
                                blocked_by=reason)
        sl, tp = compute_sltp(signal.price, signal.side,
                              self.stop_loss_pct, self.take_profit_pct)
        return Decision(signal.signal_id, signal.symbol, cid, signal.side,
                        qty, signal.price, sl, tp, f"signal:{signal.reason}")
