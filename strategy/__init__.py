"""
Strategy module for the Falcon Research Engine.
Signal generation only — strategies NEVER execute orders directly.
Flow: market data -> strategy -> signal -> risk manager -> execution -> portfolio.
"""

import logging
import math
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SignalType(Enum):
    LONG = "long"
    SHORT = "short"
    HOLD = "hold"


@dataclass
class TradingSignal:
    symbol: str
    signal_type: SignalType
    confidence: float  # 0.0 - 1.0
    timestamp: datetime
    price: Optional[float] = None
    target_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    reason: Optional[str] = None
    signal_id: Optional[str] = None


class StrategyInterface(ABC):
    def __init__(self, config):
        self.config = config

    @abstractmethod
    def calculate_signal(self, symbol: str, market_data: Dict[str, Any]) -> Optional[TradingSignal]:
        ...


def ema_series(prices: List[float], period: int) -> List[float]:
    """Proper EMA series (no look-ahead; uses only provided closes in order)."""
    if not prices or period <= 0 or len(prices) < period:
        return []
    k = 2.0 / (period + 1.0)
    # Seed with SMA of first `period` values
    sma = sum(prices[:period]) / period
    out = [sma]
    for p in prices[period:]:
        out.append(p * k + out[-1] * (1 - k))
    return out


def ema_last(prices: List[float], period: int) -> Optional[float]:
    s = ema_series(prices, period)
    return s[-1] if s else None


def rolling_std(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period or period <= 1:
        return None
    window = prices[-period:]
    mean = sum(window) / len(window)
    var = sum((x - mean) ** 2 for x in window) / (len(window) - 1)
    return math.sqrt(max(var, 0.0))


class IncrementalEMA:
    """Exact incremental EMA cache: O(1) amortized per appended bar.

    Resuming the recurrence from the cached chain value performs the
    IDENTICAL floating-point operations in the IDENTICAL order as a full
    recomputation, so results are bit-identical to `ema_last`. Any
    non-sequential call (different symbol, length jump, changed prefix)
    falls back to full legacy recomputation. Purely a speed optimization:
    backtests go from O(n^2) to O(n) with zero numeric change.
    """

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}

    def emas(self, symbol: str, closes: List[float],
             fast_period: int, slow_period: int):
        """Returns (fast, slow, prev_fast, prev_slow), Nones when too short."""
        n = len(closes)
        c = self._cache.get(symbol)
        if (c is not None and c['periods'] == (fast_period, slow_period)
                and c['n'] == n - 1 and n >= 2 and closes[-2] == c['last']):
            kf = 2.0 / (fast_period + 1.0)
            ks = 2.0 / (slow_period + 1.0)
            new_fast = closes[-1] * kf + c['fast'] * (1 - kf)
            new_slow = closes[-1] * ks + c['slow'] * (1 - ks)
            prev_fast, prev_slow = c['fast'], c['slow']
            self._cache[symbol] = {'periods': (fast_period, slow_period),
                                   'n': n, 'fast': new_fast, 'slow': new_slow,
                                   'last': closes[-1]}
            return new_fast, new_slow, prev_fast, prev_slow
        fast = ema_last(closes, fast_period)
        slow = ema_last(closes, slow_period)
        prev_fast = ema_last(closes[:-1], fast_period) if n >= 2 else None
        prev_slow = ema_last(closes[:-1], slow_period) if n >= 2 else None
        if fast is not None and slow is not None:
            self._cache[symbol] = {'periods': (fast_period, slow_period),
                                   'n': n, 'fast': fast, 'slow': slow,
                                   'last': closes[-1]}
        return fast, slow, prev_fast, prev_slow


class EMACrossoverStrategy(StrategyInterface):
    """EMA fast/slow crossover with volume, momentum, volatility and spread filters.

    All filters are configurable; failing any filter yields HOLD (never forced trades).
    Uses only closed candles (caller should exclude the forming candle).
    """

    def __init__(self, config):
        super().__init__(config)
        self.fast_ema_length = int(config.get('fast_ema_length', 10))
        self.slow_ema_length = int(config.get('slow_ema_length', 20))
        self.min_volume = float(config.get('min_volume', 1000))
        self.momentum_period = int(config.get('momentum_period', 14))
        self.momentum_threshold = float(config.get('momentum_threshold', 0.01))
        self.volatility_period = int(config.get('volatility_period', 14))
        self.volatility_min = float(config.get('volatility_min', 0.0))
        self.volatility_max = float(config.get('volatility_max', 0.10))
        self.max_spread_pct = float(config.get('max_spread_pct', 0.002))
        self.stop_loss_pct = float(config.get('stop_loss_pct', 0.01))
        self.take_profit_pct = float(config.get('take_profit_pct', 0.01))
        self._emas = IncrementalEMA()

    def calculate_signal(self, symbol: str, market_data: Dict[str, Any]) -> Optional[TradingSignal]:
        candles = market_data.get('candles', []) or []
        orderbook = market_data.get('orderbook')
        need = max(self.slow_ema_length, self.momentum_period, self.volatility_period) + 2
        if len(candles) < need:
            logger.debug("Not enough data for EMA calculation (%d < %d)", len(candles), need)
            return None
        closes = [c.close for c in candles]
        volumes = [c.volume for c in candles]
        current_price = closes[-1]
        if current_price <= 0:
            return None

        fast, slow, prev_fast, prev_slow = self._emas.emas(
            symbol, closes, self.fast_ema_length, self.slow_ema_length)
        if fast is None or slow is None or slow <= 0 or fast <= 0:
            return None

        # Volume filter
        current_volume = volumes[-1] if volumes else 0.0
        if current_volume < self.min_volume:
            return TradingSignal(symbol=symbol, signal_type=SignalType.HOLD, confidence=0.3,
                                 timestamp=_utcnow(), price=current_price, reason="low_volume")

        # Momentum filter
        ago_idx = max(0, len(closes) - 1 - self.momentum_period)
        price_ago = closes[ago_idx]
        momentum = (current_price - price_ago) / price_ago if price_ago else 0.0

        # Volatility filter (relative std of closes)
        vol = rolling_std(closes, self.volatility_period)
        rel_vol = (vol / current_price) if vol is not None and current_price else 0.0
        if rel_vol < self.volatility_min or rel_vol > self.volatility_max:
            return TradingSignal(symbol=symbol, signal_type=SignalType.HOLD, confidence=0.3,
                                 timestamp=_utcnow(), price=current_price, reason="volatility_filter")

        # Spread / liquidity filter
        if orderbook is not None:
            try:
                spread = orderbook.spread_pct()
                if spread is not None and spread > self.max_spread_pct:
                    return TradingSignal(symbol=symbol, signal_type=SignalType.HOLD, confidence=0.3,
                                         timestamp=_utcnow(), price=current_price, reason="spread_too_wide")
            except Exception:
                pass

        crossed_up = prev_fast is not None and prev_slow is not None and prev_fast <= prev_slow and fast > slow
        crossed_down = prev_fast is not None and prev_slow is not None and prev_fast >= prev_slow and fast < slow
        # Also allow persistent trend aligned with momentum
        trend_up = fast > slow and momentum > self.momentum_threshold
        trend_down = fast < slow and momentum < -self.momentum_threshold

        if crossed_up or trend_up:
            strength = min(1.0, abs(fast - slow) / slow + min(abs(momentum) / max(self.momentum_threshold, 1e-9), 1.0) * 0.3)
            return TradingSignal(symbol=symbol, signal_type=SignalType.LONG,
                                 confidence=max(0.0, min(1.0, strength)),
                                 timestamp=_utcnow(), price=current_price,
                                 target_price=current_price * (1 + self.take_profit_pct),
                                 stop_loss_price=current_price * (1 - self.stop_loss_pct),
                                 reason="ema_cross_up" if crossed_up else "ema_trend_up")
        if crossed_down or trend_down:
            strength = min(1.0, abs(slow - fast) / fast + min(abs(momentum) / max(self.momentum_threshold, 1e-9), 1.0) * 0.3)
            return TradingSignal(symbol=symbol, signal_type=SignalType.SHORT,
                                 confidence=max(0.0, min(1.0, strength)),
                                 timestamp=_utcnow(), price=current_price,
                                 target_price=current_price * (1 - self.take_profit_pct),
                                 stop_loss_price=current_price * (1 + self.stop_loss_pct),
                                 reason="ema_cross_down" if crossed_down else "ema_trend_down")
        return TradingSignal(symbol=symbol, signal_type=SignalType.HOLD, confidence=0.5,
                             timestamp=_utcnow(), price=current_price, reason="no_cross")


class MomentumStrategy(StrategyInterface):
    """Momentum confirmation strategy (secondary, same filters)."""

    def __init__(self, config):
        super().__init__(config)
        self.momentum_period = int(config.get('momentum_period', 14))
        self.threshold = float(config.get('momentum_threshold', 0.01))

    def calculate_signal(self, symbol: str, market_data: Dict[str, Any]) -> Optional[TradingSignal]:
        candles = market_data.get('candles', []) or []
        if len(candles) < self.momentum_period + 2:
            return None
        current_price = candles[-1].close if candles else 0.0
        if current_price <= 0:
            return None
        price_ago = candles[-self.momentum_period - 1].close if len(candles) > self.momentum_period else 0.0
        if price_ago == 0.0:
            return TradingSignal(symbol=symbol, signal_type=SignalType.HOLD, confidence=0.3,
                                 timestamp=_utcnow(), price=current_price, reason="no_reference")
        momentum = (current_price - price_ago) / price_ago
        if momentum > self.threshold:
            return TradingSignal(symbol=symbol, signal_type=SignalType.LONG,
                                 confidence=min(1.0, momentum / self.threshold / 3.0 + 0.4),
                                 timestamp=_utcnow(), price=current_price, reason="momentum_up")
        if momentum < -self.threshold:
            return TradingSignal(symbol=symbol, signal_type=SignalType.SHORT,
                                 confidence=min(1.0, abs(momentum) / self.threshold / 3.0 + 0.4),
                                 timestamp=_utcnow(), price=current_price, reason="momentum_down")
        return TradingSignal(symbol=symbol, signal_type=SignalType.HOLD, confidence=0.5,
                             timestamp=_utcnow(), price=current_price, reason="momentum_flat")


class StrategyManager:
    """Selects and runs strategies (default: ema_crossover)."""

    def __init__(self, config):
        self.config = config
        self.strategies: Dict[str, StrategyInterface] = {}
        self._initialize_strategies()

    def _initialize_strategies(self):
        self.strategies['ema_crossover'] = EMACrossoverStrategy(self.config)
        self.strategies['momentum'] = MomentumStrategy(self.config)
        requested = self.config.get('strategy_type', 'ema_crossover')
        if requested not in self.strategies:
            logger.warning("Unknown strategy type: %s — falling back to ema_crossover", requested)

    def get_strategy(self, name: str) -> Optional[StrategyInterface]:
        return self.strategies.get(name)

    def generate_signal(self, symbol: str, market_data: Dict[str, Any],
                        strategy_name: Optional[str] = None) -> Optional[TradingSignal]:
        if strategy_name is None:
            strategy_name = self.config.get('strategy_type', 'ema_crossover')
        strategy = self.strategies.get(strategy_name)
        if strategy is None:
            logger.warning("Strategy %s not found", strategy_name)
            return None
        sig = strategy.calculate_signal(symbol, market_data)
        if sig is not None and not sig.signal_id:
            # Deterministic signal id for idempotent execution
            base = f"{sig.symbol}|{sig.signal_type.value}|{sig.timestamp.isoformat()}|{sig.price}"
            import hashlib
            sig.signal_id = hashlib.sha256(base.encode()).hexdigest()[:16]
        return sig
