"""Deterministic exit engine (no broker calls, no clock).

Evaluates one open position against the current (closed-bar) price and the
strategy context. At minimum supports: hard SL, hard TP, strategy
invalidation (opposite signal), signal expiry (bars since entry), time stop
(max bars held), risk emergency exit (caller-flagged daily halt). Broker-side
SL/TP fills are NOT duplicated here: when the terminal no longer shows the
ticket, the caller reconciles it as externally closed instead of sending
another close. Trailing / partial exits are out of scope for this milestone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

EXIT_REASONS = (
    "hard_sl", "hard_tp", "strategy_invalidation", "signal_expiry",
    "time_stop", "risk_emergency",
)


@dataclass(frozen=True)
class ExitDecision:
    should_close: bool
    reason: str = ""  # "" when no exit; else one of EXIT_REASONS
    detail: str = ""


def _side_sign(side: str) -> int:
    return 1 if str(side or "").upper() == "BUY" else -1


class ExitEngine:
    """Pure exit evaluation over plain position dicts."""

    def __init__(self, stop_loss_pct: float = 0.01,
                 take_profit_pct: float = 0.01,
                 signal_expiry_bars: int = 20,
                 time_stop_bars: int = 100):
        self.stop_loss_pct = float(stop_loss_pct or 0.0)
        self.take_profit_pct = float(take_profit_pct or 0.0)
        self.signal_expiry_bars = max(0, int(signal_expiry_bars or 0))
        self.time_stop_bars = max(0, int(time_stop_bars or 0))

    def evaluate(self, position: Dict[str, Any], current_price: float,
                 bars_held: int = 0,
                 opposite_signal: bool = False,
                 risk_emergency: bool = False) -> ExitDecision:
        try:
            price = float(current_price or 0)
        except (TypeError, ValueError):
            return ExitDecision(False)
        if price <= 0:
            return ExitDecision(False)
        entry = float(position.get("entry_price", 0) or 0)
        side = str(position.get("side", "") or "").upper()
        if entry <= 0 or side not in ("BUY", "SELL"):
            return ExitDecision(False)
        # 1. Risk emergency first (deterministic, caller-flagged).
        if risk_emergency:
            return ExitDecision(True, "risk_emergency", "daily halt flagged")
        # 2. Hard SL / hard TP from entry economics.
        if side == "BUY":
            if self.stop_loss_pct > 0 and price <= entry * (1 - self.stop_loss_pct):
                return ExitDecision(True, "hard_sl", f"{price}<=SL")
            if self.take_profit_pct > 0 and price >= entry * (1 + self.take_profit_pct):
                return ExitDecision(True, "hard_tp", f"{price}>=TP")
        else:
            if self.stop_loss_pct > 0 and price >= entry * (1 + self.stop_loss_pct):
                return ExitDecision(True, "hard_sl", f"{price}>=SL")
            if self.take_profit_pct > 0 and price <= entry * (1 - self.take_profit_pct):
                return ExitDecision(True, "hard_tp", f"{price}<=TP")
        # 3. Strategy invalidation (explicit opposite signal).
        if opposite_signal:
            return ExitDecision(True, "strategy_invalidation", "opposite signal")
        # 4. Signal expiry / time stop (bar-count based, deterministic).
        try:
            held = max(0, int(bars_held or 0))
        except (TypeError, ValueError):
            held = 0
        if self.signal_expiry_bars > 0 and held >= self.signal_expiry_bars:
            # Expiry and time-stop share bar semantics; expiry wins when both
            # configured and held exceeds the shorter horizon.
            if self.time_stop_bars <= 0 or self.signal_expiry_bars <= self.time_stop_bars:
                return ExitDecision(True, "signal_expiry", f"held={held}")
        if self.time_stop_bars > 0 and held >= self.time_stop_bars:
            return ExitDecision(True, "time_stop", f"held={held}")
        return ExitDecision(False)
