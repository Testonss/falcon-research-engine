"""Warmup gate: closed-bar history + feed freshness + spread + metadata.

No orders during WARMING_UP. Returns (ready, reason); reason is one of
ok / warming_up / insufficient_history / stale_feed / spread_too_high /
symbol_untradable / bad_bars. Pure apart from explicit inputs — the runtime
supplies feed age, spread and symbol metadata; tests drive all branches.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def required_bars(fast_ema: int, slow_ema: int, momentum_period: int,
                  volatility_period: int, min_history_bars: int = 0) -> int:
    need = max(int(fast_ema or 0), int(slow_ema or 0),
               int(momentum_period or 0), int(volatility_period or 0)) + 2
    try:
        floor = int(min_history_bars or 0)
    except (TypeError, ValueError):
        floor = 0
    return max(need, floor)


def check_warmup(n_closed_bars: int, feed_age_s: Optional[float],
                 spread_pct: Optional[float], symbol_meta: Optional[Dict[str, Any]],
                 fast_ema: int = 10, slow_ema: int = 20,
                 momentum_period: int = 14, volatility_period: int = 14,
                 min_history_bars: int = 0, stale_after_s: float = 30.0,
                 max_spread_pct: float = 0.002) -> Tuple[bool, str]:
    need = required_bars(fast_ema, slow_ema, momentum_period,
                         volatility_period, min_history_bars)
    if int(n_closed_bars or 0) < need:
        return False, "insufficient_history"
    # Symbol metadata/tradability (when supplied).
    if symbol_meta is not None:
        try:
            tm = symbol_meta.get("trade_mode", 0)
            if tm not in (0, None):
                return False, "symbol_untradable"
            if symbol_meta.get("visible") is False:
                return False, "symbol_untradable"
        except AttributeError:
            pass
    # Feed freshness.
    try:
        age = float(feed_age_s) if feed_age_s is not None else 0.0
    except (TypeError, ValueError):
        age = 0.0
    if age > float(stale_after_s or 0.0):
        return False, "stale_feed"
    # Spread gate.
    try:
        sp = float(spread_pct) if spread_pct is not None else 0.0
    except (TypeError, ValueError):
        sp = 0.0
    if sp < 0:
        return False, "bad_bars"
    if float(max_spread_pct or 0.0) > 0 and sp > float(max_spread_pct):
        return False, "spread_too_high"
    return True, "ok"
