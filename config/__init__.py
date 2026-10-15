"""
Configuration module for the Falcon Research Engine.
Handles application configuration and environment validation.
Paper trading is the default; live trading requires explicit opt-in.
"""

from typing import Optional, Dict, Any
import os
import logging

logger = logging.getLogger(__name__)


_DEFAULTS: Dict[str, Any] = {
    # General
    'market_data_source': 'binance',
    'exchange_adapter': 'paper',  # paper | sandbox | live (live disabled by default)
    'log_level': 'INFO',
    # Risk
    'max_position_size': 1000.0,
    'risk_per_trade': 10.0,          # max notional risk per trade in quote currency
    'risk_per_trade_pct': 0.01,      # 1% of equity per trade
    'stop_loss_pct': 0.01,           # 1%
    'take_profit_pct': 0.01,         # 1%
    'max_daily_loss': 500.0,
    'max_consecutive_losses': 3,
    'stop_trading_cooldown_minutes': 30,
    'kill_switch_enabled': False,
    'max_drawdown_percentage': 0.10,  # 10% session max drawdown
    # Strategy V1
    'strategy_type': 'ema_crossover',
    'fast_ema_length': 10,
    'slow_ema_length': 20,
    'min_volume': 1000.0,
    'momentum_period': 14,
    'momentum_threshold': 0.01,
    'volatility_period': 14,
    'volatility_min': 0.0,
    'volatility_max': 0.10,
    'max_spread_pct': 0.002,  # 0.2% max spread to trade
    # Simulation costs
    'maker_fee': 0.001,
    'taker_fee': 0.002,
    'spread_pct': 0.0005,
    'slippage_pct': 0.0005,
    'simulated_latency_ms': 50,
    'partial_fill_probability': 0.0,
    'reject_probability': 0.0,
    # Liquidity-aware execution (PaperExchange).
    # Bar volume is read in base-asset units (same unit as order quantity).
    'max_participation_rate': 0.10,  # max fraction of one bar's volume per order
    'min_fill_notional': 0.0,        # quote currency; fills below -> insufficient liquidity (0 = disabled)
    'market_impact_coeff': 0.0,      # sqrt-impact coeff on participation (0 = off)
    'dynamic_spread_coeff': 0.0,     # extra spread per unit of bar range (0 = off)
    # Execution model for backtest/replay: 'next_open' | 'next_close'.
    # Signal on bars [0..i-1], fill at bar i open (next_open) or close (next_close).
    'execution_model': 'next_close',
    # Evaluation splits (temporal, no shuffling). Ratios must sum to <= 1.0.
    'train_ratio': 0.6,
    'validation_ratio': 0.2,
    # Walk-forward evaluation (all in bars; embargo gap prevents leakage).
    # Sized for long multi-period datasets; test folds must exceed the
    # strategy warmup (~22 bars for default EMA20/momentum14 config).
    'walk_forward_n_splits': 3,
    'walk_forward_train_bars': 200,
    'walk_forward_test_bars': 100,
    'embargo_bars': 5,
    # Indicator warmup: tail bars used as context (never scored, no leakage).
    'warmup_bars': 30,
    # Historical data replay
    'data_csv_path': 'data/sample_ohlcv.csv',
    'data_csv_symbol': 'BTC/USDT',
    'data_csv_interval': '5m',
    # Portfolio / engine
    'initial_cash': 10000.0,
    'paper_trading_enabled': True,
    'backtest_enabled': False,
    'trade_interval_seconds': 5,
    'random_seed': 42,
    # Live safety: live requires BOTH adapter=live AND explicit enable flag
    'live_trading_enabled': False,
    'live_trading_confirm': '',
}

_INT_KEYS = {
    'max_consecutive_losses', 'stop_trading_cooldown_minutes',
    'fast_ema_length', 'slow_ema_length', 'momentum_period',
    'volatility_period', 'trade_interval_seconds', 'random_seed',
    'simulated_latency_ms',
    'walk_forward_n_splits', 'walk_forward_train_bars',
    'walk_forward_test_bars', 'embargo_bars', 'warmup_bars',
}
_FLOAT_KEYS = {
    'max_position_size', 'risk_per_trade', 'risk_per_trade_pct',
    'stop_loss_pct', 'take_profit_pct', 'max_daily_loss',
    'max_drawdown_percentage', 'min_volume', 'momentum_threshold',
    'volatility_min', 'volatility_max', 'max_spread_pct',
    'maker_fee', 'taker_fee', 'spread_pct', 'slippage_pct',
    'partial_fill_probability', 'reject_probability', 'initial_cash',
    'train_ratio', 'validation_ratio',
    'max_participation_rate', 'min_fill_notional',
    'market_impact_coeff', 'dynamic_spread_coeff',
}
_BOOL_KEYS = {
    'kill_switch_enabled', 'paper_trading_enabled',
    'backtest_enabled', 'live_trading_enabled',
}

_ENV_MAPPING = {
    'MARKET_DATA_SOURCE': 'market_data_source',
    'EXCHANGE_ADAPTER': 'exchange_adapter',
    'LOG_LEVEL': 'log_level',
    'MAX_POSITION_SIZE': 'max_position_size',
    'RISK_PER_TRADE': 'risk_per_trade',
    'RISK_PER_TRADE_PCT': 'risk_per_trade_pct',
    'STOP_LOSS_PCT': 'stop_loss_pct',
    'TAKE_PROFIT_PCT': 'take_profit_pct',
    'MAX_DAILY_LOSS': 'max_daily_loss',
    'MAX_CONSECUTIVE_LOSSES': 'max_consecutive_losses',
    'STOP_TRADING_COOLDOWN_MINUTES': 'stop_trading_cooldown_minutes',
    'KILL_SWITCH_ENABLED': 'kill_switch_enabled',
    'MAX_DRAWDOWN_PERCENTAGE': 'max_drawdown_percentage',
    'STRATEGY_TYPE': 'strategy_type',
    'FAST_EMA_LENGTH': 'fast_ema_length',
    'SLOW_EMA_LENGTH': 'slow_ema_length',
    'MIN_VOLUME': 'min_volume',
    'MOMENTUM_PERIOD': 'momentum_period',
    'MOMENTUM_THRESHOLD': 'momentum_threshold',
    'VOLATILITY_PERIOD': 'volatility_period',
    'VOLATILITY_MIN': 'volatility_min',
    'VOLATILITY_MAX': 'volatility_max',
    'MAX_SPREAD_PCT': 'max_spread_pct',
    'MAKER_FEE': 'maker_fee',
    'TAKER_FEE': 'taker_fee',
    'SPREAD_PCT': 'spread_pct',
    'SLIPPAGE_PCT': 'slippage_pct',
    'MAX_PARTICIPATION_RATE': 'max_participation_rate',
    'MIN_FILL_NOTIONAL': 'min_fill_notional',
    'MARKET_IMPACT_COEFF': 'market_impact_coeff',
    'DYNAMIC_SPREAD_COEFF': 'dynamic_spread_coeff',
    'EXECUTION_MODEL': 'execution_model',
    'TRAIN_RATIO': 'train_ratio',
    'VALIDATION_RATIO': 'validation_ratio',
    'WALK_FORWARD_N_SPLITS': 'walk_forward_n_splits',
    'WALK_FORWARD_TRAIN_BARS': 'walk_forward_train_bars',
    'WALK_FORWARD_TEST_BARS': 'walk_forward_test_bars',
    'EMBARGO_BARS': 'embargo_bars',
    'WARMUP_BARS': 'warmup_bars',
    'DATA_CSV_PATH': 'data_csv_path',
    'DATA_CSV_SYMBOL': 'data_csv_symbol',
    'DATA_CSV_INTERVAL': 'data_csv_interval',
    'SIMULATED_LATENCY_MS': 'simulated_latency_ms',
    'PARTIAL_FILL_PROBABILITY': 'partial_fill_probability',
    'REJECT_PROBABILITY': 'reject_probability',
    'INITIAL_CASH': 'initial_cash',
    'PAPER_TRADING_ENABLED': 'paper_trading_enabled',
    'BACKTEST_ENABLED': 'backtest_enabled',
    'TRADE_INTERVAL_SECONDS': 'trade_interval_seconds',
    'RANDOM_SEED': 'random_seed',
    'LIVE_TRADING_ENABLED': 'live_trading_enabled',
    'LIVE_TRADING_CONFIRM': 'live_trading_confirm',
}


class ConfigManager:
    """Manages application configuration from defaults, env vars and files."""

    def __init__(self, config_file: Optional[str] = None):
        self._config: Dict[str, Any] = dict(_DEFAULTS)
        self._load_from_env()
        if config_file and os.path.exists(config_file):
            self._load_from_file(config_file)
        self._validate_config()

    def _load_from_env(self):
        for env_key, config_key in _ENV_MAPPING.items():
            value = os.getenv(env_key)
            if value is None or value == '':
                continue
            if config_key in _INT_KEYS:
                try:
                    self._config[config_key] = int(float(value))
                except ValueError:
                    logger.warning("Invalid int value for %s: %s", env_key, value)
            elif config_key in _FLOAT_KEYS:
                try:
                    self._config[config_key] = float(value)
                except ValueError:
                    logger.warning("Invalid float value for %s: %s", env_key, value)
            elif config_key in _BOOL_KEYS:
                self._config[config_key] = value.strip().lower() in ('true', '1', 'yes', 'on')
            else:
                self._config[config_key] = value.strip()

    def _load_from_file(self, config_file: str):
        # Minimal .env-style key=value loader (no secrets logged).
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    k, v = line.split('=', 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    # Map env-style keys to internal keys
                    target = _ENV_MAPPING.get(k, k.lower())
                    if target in _DEFAULTS:
                        if target in _INT_KEYS:
                            try:
                                self._config[target] = int(float(v))
                            except ValueError:
                                logger.warning("Invalid int value in %s for %s", config_file, k)
                        elif target in _FLOAT_KEYS:
                            try:
                                self._config[target] = float(v)
                            except ValueError:
                                logger.warning("Invalid float value in %s for %s", config_file, k)
                        elif target in _BOOL_KEYS:
                            self._config[target] = v.lower() in ('true', '1', 'yes', 'on')
                        else:
                            self._config[target] = v
        except OSError as e:
            logger.warning("Could not load config file %s: %s", config_file, e)

    def _validate_config(self):
        adapter = self._config.get('exchange_adapter')
        if adapter not in ('paper', 'sandbox', 'testnet', 'live'):
            logger.warning("Unknown exchange adapter: %s (expected paper/sandbox/testnet/live)", adapter)
        # Live is never enabled implicitly.
        if adapter == 'live' and not self.is_live_trading_enabled():
            logger.warning("exchange_adapter=live without explicit live enable flag — forcing paper mode")
            self._config['exchange_adapter'] = 'paper'
        for key in list(_INT_KEYS) + list(_FLOAT_KEYS):
            try:
                if self._config.get(key) is not None and self._config[key] < 0:
                    logger.warning("Negative value for %s: %s", key, self._config[key])
            except TypeError:
                pass
        fast = self._config.get('fast_ema_length', 10)
        slow = self._config.get('slow_ema_length', 20)
        if isinstance(fast, int) and isinstance(slow, int) and fast >= slow:
            logger.warning("fast_ema_length (%s) should be < slow_ema_length (%s)", fast, slow)
        model = self._config.get('execution_model', 'next_close')
        if model not in ('next_open', 'next_close'):
            logger.warning("Unknown execution_model %r — forcing 'next_close'", model)
            self._config['execution_model'] = 'next_close'
        part = self._config.get('max_participation_rate', 0.10)
        try:
            if not (0.0 < float(part) <= 1.0):
                logger.warning("max_participation_rate %r outside (0,1] — clamping", part)
                self._config['max_participation_rate'] = min(max(float(part), 1e-6), 1.0)
        except (TypeError, ValueError):
            logger.warning("Invalid max_participation_rate %r — resetting to 0.10", part)
            self._config['max_participation_rate'] = 0.10
        tr = float(self._config.get('train_ratio', 0.6) or 0.0)
        vr = float(self._config.get('validation_ratio', 0.2) or 0.0)
        if tr <= 0 or vr < 0 or tr + vr > 1.0:
            logger.warning("Invalid train/validation ratios (%s/%s) — resetting to 0.6/0.2", tr, vr)
            self._config['train_ratio'] = 0.6
            self._config['validation_ratio'] = 0.2

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def set(self, key: str, value: Any):
        self._config[key] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._config)

    def is_live_trading_enabled(self) -> bool:
        """Live trading requires adapter=live AND explicit enable AND confirm token.

        The confirm token must be exactly 'I_UNDERSTAND_LIVE_RISK'.
        Default is always False (paper/sandbox only).
        """
        return (
            self._config.get('exchange_adapter') == 'live'
            and bool(self._config.get('live_trading_enabled'))
            and self._config.get('live_trading_confirm') == 'I_UNDERSTAND_LIVE_RISK'
        )
