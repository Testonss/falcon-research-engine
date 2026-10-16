"""Deterministic closed-bar feature calculation (pure, no I/O, no clock).

All functions operate on plain ``closes``/``highs``/``lows``/``volumes``
lists in oldest-first order and use only closed bars supplied by the
caller. The runtime (and backtests) must exclude the forming bar before
calling in, so no look-ahead can occur. Identical inputs always produce
identical outputs (no randomness, no wall-clock, no iteration-order
dependence).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional


def ema_series(prices: List[float], period: int) -> List[float]:
    if not prices or period <= 0 or len(prices) < period:
        return []
    k = 2.0 / (period + 1.0)
    sma = sum(prices[:period]) / period
    out = [sma]
    for p in prices[period:]:
        out.append(p * k + out[-1] * (1.0 - k))
    return out


def ema_last(prices: List[float], period: int) -> Optional[float]:
    s = ema_series(prices, period)
    return s[-1] if s else None


def rolling_std(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 1:
        return None
    window = values[-period:]
    mean = sum(window) / len(window)
    var = sum((x - mean) ** 2 for x in window) / (len(window) - 1)
    return math.sqrt(max(var, 0.0))


@dataclass(frozen=True)
class FeatureVector:
    close: float
    fast_ema: Optional[float]
    slow_ema: Optional[float]
    prev_fast_ema: Optional[float]
    prev_slow_ema: Optional[float]
    momentum: Optional[float]
    rel_volatility: Optional[float]
    volume: float
    crossed_up: bool = False
    crossed_down: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "close": self.close,
            "fast_ema": self.fast_ema,
            "slow_ema": self.slow_ema,
            "prev_fast_ema": self.prev_fast_ema,
            "prev_slow_ema": self.prev_slow_ema,
            "momentum": self.momentum,
            "rel_volatility": self.rel_volatility,
            "volume": self.volume,
            "crossed_up": self.crossed_up,
            "crossed_down": self.crossed_down,
        }


def compute_features(
    closes: List[float],
    volumes: Optional[List[float]] = None,
    fast_period: int = 10,
    slow_period: int = 20,
    momentum_period: int = 14,
    volatility_period: int = 14,
) -> FeatureVector:
    """Compute one deterministic feature vector from closed-bar history.

    Raises ``ValueError`` on empty/non-positive closes so callers fail
    closed (never trade on garbage). Short histories yield ``None``
    indicator legs instead of exceptions.
    """
    if not closes:
        raise ValueError("compute_features: empty closes")
    close = float(closes[-1])
    if not math.isfinite(close) or close <= 0:
        raise ValueError(f"compute_features: bad close {close!r}")
    vols = list(volumes) if volumes else [0.0] * len(closes)
    volume = float(vols[-1]) if vols else 0.0

    fast = ema_last(closes, fast_period)
    slow = ema_last(closes, slow_period)
    prev_fast = ema_last(closes[:-1], fast_period) if len(closes) >= 2 else None
    prev_slow = ema_last(closes[:-1], slow_period) if len(closes) >= 2 else None

    momentum: Optional[float] = None
    if len(closes) > momentum_period:
        ago = closes[-1 - momentum_period]
        if ago and math.isfinite(ago) and ago > 0:
            momentum = (close - ago) / ago

    rel_vol: Optional[float] = None
    sd = rolling_std(closes, volatility_period)
    if sd is not None and close > 0:
        rel_vol = sd / close

    crossed_up = (
        prev_fast is not None and prev_slow is not None
        and fast is not None and slow is not None
        and prev_fast <= prev_slow and fast > slow
    )
    crossed_down = (
        prev_fast is not None and prev_slow is not None
        and fast is not None and slow is not None
        and prev_fast >= prev_slow and fast < slow
    )
    return FeatureVector(
        close=close, fast_ema=fast, slow_ema=slow,
        prev_fast_ema=prev_fast, prev_slow_ema=prev_slow,
        momentum=momentum, rel_volatility=rel_vol, volume=volume,
        crossed_up=crossed_up, crossed_down=crossed_down,
    )
