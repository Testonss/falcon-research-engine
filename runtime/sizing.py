"""Deterministic risk-based position sizing (pure, no I/O).

Sizing rule (base units, identical to RiskManager economics):
    risk_budget = min(risk_per_trade, equity * risk_per_trade_pct)
    qty = risk_budget / (price * stop_distance_pct)
then capped by ``max_position_size`` and by ``equity / price`` (no
leverage), floored to the broker volume step and clamped to
``[volume_min, volume_max]``. Returns 0.0 when sizing is impossible
instead of raising, so the decision engine maps it to HOLD/SKIP.
"""

from __future__ import annotations

import math


def calculate_quantity(
    price: float,
    equity: float,
    risk_per_trade: float = 10.0,
    risk_per_trade_pct: float = 0.01,
    stop_distance_pct: float = 0.01,
    max_position_size: float = 1.0,
    volume_min: float = 0.0,
    volume_max: float = 0.0,
    volume_step: float = 0.0,
) -> float:
    if price is None or equity is None:
        return 0.0
    try:
        price = float(price)
        equity = float(equity)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(price) or not math.isfinite(equity):
        return 0.0
    if price <= 0 or equity <= 0:
        return 0.0
    stop_pct = float(stop_distance_pct or 0.0)
    if stop_pct <= 0:
        return 0.0
    risk_budget = min(float(risk_per_trade), equity * float(risk_per_trade_pct))
    if risk_budget <= 0:
        return 0.0
    qty = risk_budget / (price * stop_pct)
    qty = min(qty, float(max_position_size), equity / price)
    # Broker step flooring (floor, never round up into oversize).
    step = float(volume_step or 0.0)
    if step > 0:
        qty = math.floor(qty / step) * step
        # Kill float dust to step precision.
        try:
            import decimal
            q = decimal.Decimal(str(step))
            qty = float(decimal.Decimal(str(qty)).quantize(q))
        except Exception:
            qty = round(qty, 8)
    if volume_max and volume_max > 0 and qty > volume_max:
        qty = float(volume_max)
    if volume_min and volume_min > 0 and 0 < qty < volume_min:
        return 0.0  # below broker minimum: cannot trade this size
    if qty <= 0 or not math.isfinite(qty):
        return 0.0
    return float(qty)
