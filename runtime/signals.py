"""Deterministic signal generation (pure function of features + config).

No wall-clock, no randomness, no provider access. The caller supplies the
closed-bar timestamp explicitly, and the signal id is derived
deterministically from ``(symbol, side, bar timestamp, feature hash)`` so
replays and restarts produce identical ids (idempotent execution).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from runtime.features import FeatureVector


@dataclass(frozen=True)
class RuntimeSignal:
    symbol: str
    side: str          # "BUY" | "SELL" | "HOLD"
    confidence: float  # 0.0 - 1.0
    price: float
    bar_time: str      # closed-bar ISO timestamp (signal basis, never wall-clock)
    reason: str
    signal_id: str


def _feature_hash(f: FeatureVector) -> str:
    parts = [
        f"{f.close:.8f}", f"{f.fast_ema}", f"{f.slow_ema}",
        f"{f.momentum}", f"{f.rel_volatility}", f"{f.volume}",
        str(f.crossed_up), str(f.crossed_down),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def make_signal_id(symbol: str, side: str, bar_time: str, f: FeatureVector) -> str:
    base = f"{symbol}|{side}|{bar_time}|{_feature_hash(f)}"
    return "rt_" + hashlib.sha256(base.encode()).hexdigest()[:16]


def generate_signal(
    symbol: str,
    features: FeatureVector,
    bar_time: str,
    min_volume: float = 0.0,
    momentum_threshold: float = 0.01,
    volatility_min: float = 0.0,
    volatility_max: float = 0.10,
) -> RuntimeSignal:
    """Pure deterministic signal from one closed-bar feature vector."""
    price = features.close
    if features.fast_ema is None or features.slow_ema is None:
        return RuntimeSignal(symbol, "HOLD", 0.3, price, bar_time,
                             "warming_up",
                             make_signal_id(symbol, "HOLD", bar_time, features))
    if features.volume < min_volume:
        return RuntimeSignal(symbol, "HOLD", 0.3, price, bar_time,
                             "low_volume",
                             make_signal_id(symbol, "HOLD", bar_time, features))
    rv = features.rel_volatility
    if rv is not None and (rv < volatility_min or rv > volatility_max):
        return RuntimeSignal(symbol, "HOLD", 0.3, price, bar_time,
                             "volatility_filter",
                             make_signal_id(symbol, "HOLD", bar_time, features))
    mom = features.momentum if features.momentum is not None else 0.0
    trend_up = features.fast_ema > features.slow_ema and mom > momentum_threshold
    trend_down = features.fast_ema < features.slow_ema and mom < -momentum_threshold
    if features.crossed_up or trend_up:
        denom = max(features.slow_ema, 1e-12)
        strength = min(1.0, abs(features.fast_ema - features.slow_ema) / denom
                       + min(abs(mom) / max(momentum_threshold, 1e-9), 1.0) * 0.3)
        reason = "ema_cross_up" if features.crossed_up else "ema_trend_up"
        return RuntimeSignal(symbol, "BUY", max(0.0, min(1.0, strength)),
                             price, bar_time, reason,
                             make_signal_id(symbol, "BUY", bar_time, features))
    if features.crossed_down or trend_down:
        denom = max(features.fast_ema, 1e-12)
        strength = min(1.0, abs(features.slow_ema - features.fast_ema) / denom
                       + min(abs(mom) / max(momentum_threshold, 1e-9), 1.0) * 0.3)
        reason = "ema_cross_down" if features.crossed_down else "ema_trend_down"
        return RuntimeSignal(symbol, "SELL", max(0.0, min(1.0, strength)),
                             price, bar_time, reason,
                             make_signal_id(symbol, "SELL", bar_time, features))
    return RuntimeSignal(symbol, "HOLD", 0.5, price, bar_time, "no_cross",
                         make_signal_id(symbol, "HOLD", bar_time, features))


def utc_iso(dt: Optional[datetime] = None) -> str:
    d = dt or datetime.now(timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat()
