"""Falcon candidate strategy families.

Signal-only strategies (never execute orders). The V1 baseline
(`ema_v1`, `momentum`) reuses the battle-tested classes in `strategy/`
unchanged; all other families are implemented here from trailing data only.

Each family declares an explicit, inspectable PARAM_SPACE
(param -> sorted list of allowed values). Complexity stays controlled:
at most 3 indicator components per candidate (enforced in generation).
"""
import logging
import math
from typing import Dict, List, Any, Optional, Callable

from strategy import (
    StrategyInterface, TradingSignal, SignalType,
    EMACrossoverStrategy, MomentumStrategy, ema_last, rolling_std,
    IncrementalEMA, _utcnow,
)

logger = logging.getLogger(__name__)


class _ParamsConfig:
    """Minimal config shim exposing .get() for param-driven strategies."""

    def __init__(self, params: Dict[str, Any]):
        self._p = dict(params)

    def get(self, key: str, default: Any = None) -> Any:
        return self._p.get(key, default)


def _hold(symbol: str, price: float, reason: str) -> TradingSignal:
    return TradingSignal(symbol=symbol, signal_type=SignalType.HOLD,
                         confidence=0.5, timestamp=_utcnow(),
                         price=price, reason=reason)


class MeanReversionStrategy(StrategyInterface):
    """Z-score vs trailing SMA. LONG oversold, SHORT overbought."""

    def __init__(self, config):
        super().__init__(config)
        self.sma_period = int(config.get('sma_period', 20))
        self.z_threshold = float(config.get('z_threshold', 1.5))
        self.min_volume = float(config.get('min_volume', 0.0))

    def calculate_signal(self, symbol: str, market_data: Dict[str, Any]) -> Optional[TradingSignal]:
        candles = market_data.get('candles', []) or []
        if len(candles) < self.sma_period + 2:
            return None
        closes = [c.close for c in candles]
        price = closes[-1]
        if price <= 0:
            return None
        if candles[-1].volume < self.min_volume:
            return _hold(symbol, price, "low_volume")
        window = closes[-self.sma_period:]
        mean = sum(window) / len(window)
        var = sum((x - mean) ** 2 for x in window) / max(1, len(window) - 1)
        std = math.sqrt(max(var, 0.0))
        if std <= 0:
            return _hold(symbol, price, "no_variance")
        z = (price - mean) / std
        if z < -self.z_threshold:
            return TradingSignal(symbol=symbol, signal_type=SignalType.LONG,
                                 confidence=min(1.0, abs(z) / (self.z_threshold * 2)),
                                 timestamp=_utcnow(), price=price, reason="mr_oversold")
        if z > self.z_threshold:
            return TradingSignal(symbol=symbol, signal_type=SignalType.SHORT,
                                 confidence=min(1.0, z / (self.z_threshold * 2)),
                                 timestamp=_utcnow(), price=price, reason="mr_overbought")
        return _hold(symbol, price, "mr_neutral")


class BreakoutStrategy(StrategyInterface):
    """Donchian breakout on closed bars (never the forming bar)."""

    def __init__(self, config):
        super().__init__(config)
        self.lookback = int(config.get('lookback', 20))
        self.min_volume = float(config.get('min_volume', 0.0))

    def calculate_signal(self, symbol: str, market_data: Dict[str, Any]) -> Optional[TradingSignal]:
        candles = market_data.get('candles', []) or []
        if len(candles) < self.lookback + 2:
            return None
        price = candles[-1].close
        if price <= 0:
            return None
        if candles[-1].volume < self.min_volume:
            return _hold(symbol, price, "low_volume")
        prior = candles[-(self.lookback + 1):-1]  # closed bars only
        hi = max(c.high for c in prior)
        lo = min(c.low for c in prior)
        if price > hi:
            return TradingSignal(symbol=symbol, signal_type=SignalType.LONG,
                                 confidence=0.6, timestamp=_utcnow(),
                                 price=price, reason="breakout_up")
        if price < lo:
            return TradingSignal(symbol=symbol, signal_type=SignalType.SHORT,
                                 confidence=0.6, timestamp=_utcnow(),
                                 price=price, reason="breakout_down")
        return _hold(symbol, price, "no_breakout")


class EmaTrendStrategy(StrategyInterface):
    """Simple EMA trend + momentum alignment (distinct from V1 baseline)."""

    def __init__(self, config):
        super().__init__(config)
        self.fast = int(config.get('fast_ema', 9))
        self.slow = int(config.get('slow_ema', 27))
        self.mom_period = int(config.get('momentum_period', 14))
        self.mom_thr = float(config.get('momentum_threshold', 0.01))
        self.vol_max = float(config.get('volatility_max', 1.0))
        self._emas = IncrementalEMA()

    def calculate_signal(self, symbol: str, market_data: Dict[str, Any]) -> Optional[TradingSignal]:
        candles = market_data.get('candles', []) or []
        need = max(self.fast, self.slow, self.mom_period) + 2
        if len(candles) < need:
            return None
        closes = [c.close for c in candles]
        price = closes[-1]
        if price <= 0:
            return None
        fast, slow, _, _ = self._emas.emas(symbol, closes, self.fast, self.slow)
        if fast is None or slow is None:
            return None
        vol = rolling_std(closes, self.mom_period)
        if vol is not None and price > 0 and (vol / price) > self.vol_max:
            return _hold(symbol, price, "volatility_filter")
        ago = closes[-self.mom_period - 1]
        mom = (price - ago) / ago if ago else 0.0
        if fast > slow and mom > self.mom_thr:
            return TradingSignal(symbol=symbol, signal_type=SignalType.LONG,
                                 confidence=0.6, timestamp=_utcnow(),
                                 price=price, reason="trend_up")
        if fast < slow and mom < -self.mom_thr:
            return TradingSignal(symbol=symbol, signal_type=SignalType.SHORT,
                                 confidence=0.6, timestamp=_utcnow(),
                                 price=price, reason="trend_down")
        return _hold(symbol, price, "trend_flat")


class EmaMomentumCombo(StrategyInterface):
    """EMA trend AND momentum must agree (controlled 2-component combo)."""

    def __init__(self, config):
        super().__init__(config)
        self.fast = int(config.get('fast_ema', 9))
        self.slow = int(config.get('slow_ema', 27))
        self.mom_period = int(config.get('momentum_period', 14))
        self.mom_thr = float(config.get('momentum_threshold', 0.01))
        self._emas = IncrementalEMA()

    def calculate_signal(self, symbol: str, market_data: Dict[str, Any]) -> Optional[TradingSignal]:
        candles = market_data.get('candles', []) or []
        need = max(self.fast, self.slow, self.mom_period) + 2
        if len(candles) < need:
            return None
        closes = [c.close for c in candles]
        price = closes[-1]
        if price <= 0:
            return None
        fast, slow, _, _ = self._emas.emas(symbol, closes, self.fast, self.slow)
        if fast is None or slow is None:
            return None
        ago = closes[-self.mom_period - 1]
        mom = (price - ago) / ago if ago else 0.0
        up = fast > slow and mom > self.mom_thr
        dn = fast < slow and mom < -self.mom_thr
        if up:
            return TradingSignal(symbol=symbol, signal_type=SignalType.LONG,
                                 confidence=0.7, timestamp=_utcnow(),
                                 price=price, reason="combo_up")
        if dn:
            return TradingSignal(symbol=symbol, signal_type=SignalType.SHORT,
                                 confidence=0.7, timestamp=_utcnow(),
                                 price=price, reason="combo_down")
        return _hold(symbol, price, "combo_disagree")


# Family registry: name -> {description, components, param_space, build}.
# param_space maps param name -> SORTED list of allowed values (inspectable).
FAMILIES: Dict[str, Dict[str, Any]] = {
    'ema_v1': {
        'description': 'Historical V1 EMA-crossover baseline (may be rejected).',
        'components': 2,
        'param_space': {
            'fast_ema_length': [4, 5, 8, 9, 10, 12, 14, 16],
            'slow_ema_length': [20, 24, 26, 27, 30, 36, 40],
            'momentum_threshold': [0.002, 0.003, 0.005, 0.008, 0.01, 0.015, 0.02],
        },
        'build': lambda p: EMACrossoverStrategy(_ParamsConfig({
            'fast_ema_length': p['fast_ema_length'],
            'slow_ema_length': p['slow_ema_length'],
            'momentum_threshold': p['momentum_threshold'],
            'min_volume': p.get('min_volume', 0.0),
            'momentum_period': 14, 'volatility_period': 14,
            'volatility_min': 0.0, 'volatility_max': 1.0,
            'max_spread_pct': 1.0, 'stop_loss_pct': 0.01,
            'take_profit_pct': 0.01,
        })),
    },
    'momentum': {
        'description': 'Trailing momentum vs threshold.',
        'components': 1,
        'param_space': {
            'momentum_period': [4, 5, 6, 7, 10, 14, 21, 28, 35],
            'momentum_threshold': [0.001, 0.002, 0.003, 0.005, 0.008, 0.01, 0.015, 0.02],
        },
        'build': lambda p: MomentumStrategy(_ParamsConfig(dict(p))),
    },
    'mean_reversion': {
        'description': 'Z-score vs trailing SMA.',
        'components': 1,
        'param_space': {
            'sma_period': [8, 10, 15, 20, 30, 40, 50],
            'z_threshold': [0.75, 1.0, 1.5, 2.0, 2.5, 3.0],
        },
        'build': lambda p: MeanReversionStrategy(_ParamsConfig(dict(p))),
    },
    'breakout': {
        'description': 'Donchian breakout on closed bars.',
        'components': 1,
        'param_space': {
            'lookback': [5, 7, 10, 14, 20, 30, 40, 50, 60],
        },
        'build': lambda p: BreakoutStrategy(_ParamsConfig(dict(p))),
    },
    'ema_trend': {
        'description': 'EMA trend with momentum alignment + vol filter.',
        'components': 2,
        'param_space': {
            'fast_ema': [5, 8, 9, 12],
            'slow_ema': [21, 26, 27, 36],
            'momentum_threshold': [0.002, 0.003, 0.005, 0.008, 0.01, 0.015, 0.02],
        },
        'build': lambda p: EmaTrendStrategy(_ParamsConfig(dict(p))),
    },
    'ema_momentum_combo': {
        'description': 'EMA trend AND momentum agreement (2-component combo).',
        'components': 2,
        'param_space': {
            'fast_ema': [4, 5, 9, 12],
            'slow_ema': [20, 21, 27, 36],
            'momentum_threshold': [0.003, 0.005, 0.008, 0.01, 0.015, 0.02],
        },
        'build': lambda p: EmaMomentumCombo(_ParamsConfig(dict(p))),
    },
}

MAX_COMPONENTS = 3  # hard ceiling: combos stay interpretable


def family_names() -> List[str]:
    return sorted(FAMILIES.keys())


def param_space(family: str) -> Dict[str, List[Any]]:
    try:
        space = FAMILIES[family]['param_space']
    except KeyError:
        raise ValueError(f"Unknown strategy family: {family}")
    return {k: sorted(v) for k, v in space.items()}


def build_strategy(family: str, params: Dict[str, Any]):
    """Instantiate a signal-only strategy. Raises ValueError on unknown family/param."""
    if family not in FAMILIES:
        raise ValueError(f"Unknown strategy family: {family}")
    space = param_space(family)
    for k, v in params.items():
        if k not in space:
            raise ValueError(f"Param {k!r} not in space for family {family!r}")
        if v not in space[k]:
            raise ValueError(f"Value {v!r} for {k!r} outside space {space[k]}")
    if FAMILIES[family]['components'] > MAX_COMPONENTS:
        raise ValueError(f"Family {family!r} exceeds complexity ceiling")
    return FAMILIES[family]['build'](dict(params))


def complexity_of(family: str, params: Dict[str, Any]) -> int:
    """Interpretable complexity: components + tuned params + active filters."""
    base = int(FAMILIES[family]['components'])
    n_params = len(params)
    filters = sum(1 for k in ('min_volume', 'volatility_max', 'max_spread_pct')
                  if k in params)
    return base + n_params + filters
