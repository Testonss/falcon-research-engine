"""Runtime configuration (env + ConfigManager overlay).

``RuntimeConfig`` carries everything the headless loop needs beyond the
shared research config. Environment overrides (all optional, ``FALCON_DEMO_``
prefix) let operators run the runtime without editing files:

  FALCON_DEMO_SYMBOLS="EURUSD,GBPUSD"  FALCON_DEMO_TIMEFRAME=5m
  FALCON_DEMO_POLL_S=5  FALCON_DEMO_STATE_DB=runtime_demo.db
  FALCON_DEMO_PAUSE_FILE=PAUSE  FALCON_DEMO_DRY_RUN=1
  FALCON_DEMO_MAX_CYCLES=100  FALCON_DEMO_SERVER=MetaQuotes-Demo
  FALCON_DEMO_MAX_POSITIONS=3  FALCON_DEMO_MIN_CONFIDENCE=0.0
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class RuntimeConfig:
    symbols: List[str] = field(default_factory=lambda: ["EURUSD"])
    timeframe: str = "5m"
    poll_interval_s: float = 5.0
    state_db: str = "runtime_demo.db"
    pause_file: str = ""
    dry_run: bool = False
    max_cycles: int = 0            # 0 = run until stopped
    server: str = ""
    max_concurrent_positions: int = 5
    min_confidence: float = 0.0
    # Strategy/feature knobs (mirror strategy defaults; explicit here so the
    # headless runtime never depends on GUI state).
    fast_ema: int = 10
    slow_ema: int = 20
    momentum_period: int = 14
    momentum_threshold: float = 0.01
    volatility_period: int = 14
    volatility_min: float = 0.0
    volatility_max: float = 0.10
    min_volume: float = 0.0
    stop_loss_pct: float = 0.01
    take_profit_pct: float = 0.01
    risk_per_trade: float = 10.0
    risk_per_trade_pct: float = 0.01
    max_position_size: float = 1.0
    max_daily_loss: float = 500.0
    stale_after_s: float = 30.0
    # --- v0.2 acceptance closure: full risk engine knobs -----------------
    max_total_lots: float = 5.0
    max_exposure_per_symbol: float = 100000.0  # quote-notional cap per symbol
    max_total_exposure: float = 200000.0       # quote-notional cap portfolio
    max_daily_drawdown_pct: float = 0.05       # intraday peak->trough halt
    max_drawdown_pct: float = 0.10             # session max drawdown halt
    max_consecutive_losses: int = 3
    cooldown_between_trades_s: float = 0.0     # min seconds between entries
    stop_trading_cooldown_minutes: int = 30
    max_trades_per_symbol: int = 10            # per rolling window
    trades_window_s: float = 3600.0            # rolling window seconds
    max_spread_pct: float = 0.002              # 0.2% default
    max_spread_absolute: float = 0.0           # price units; 0 = pct only
    min_history_bars: int = 0                  # 0 = derived from indicators
    trading_day_tz: str = "UTC"                # trading-day identity tz
    # --- exit engine ------------------------------------------------------
    signal_expiry_bars: int = 20               # signal stale after N bars
    time_stop_bars: int = 100                  # flat time stop after N bars
    # --- market-closed backoff -------------------------------------------
    market_closed_backoff_s: float = 60.0      # initial cooldown
    market_closed_max_backoff_s: float = 3600.0
    # --- kill switch / pause ----------------------------------------------
    pause_mode_default: str = "NONE"           # NONE | NEW_ENTRIES | ALL

    @classmethod
    def from_env(cls, base: "RuntimeConfig | None" = None) -> "RuntimeConfig":
        cfg = base or cls()
        g = os.getenv
        if g("FALCON_DEMO_SYMBOLS"):
            syms = [s.strip() for s in g("FALCON_DEMO_SYMBOLS", "").split(",") if s.strip()]
            if syms:
                cfg.symbols = syms
        if g("FALCON_DEMO_TIMEFRAME"):
            cfg.timeframe = g("FALCON_DEMO_TIMEFRAME", "").strip() or cfg.timeframe
        try:
            if g("FALCON_DEMO_POLL_S"):
                cfg.poll_interval_s = max(0.0, float(g("FALCON_DEMO_POLL_S", "0") or 0))
        except ValueError:
            pass
        if g("FALCON_DEMO_STATE_DB"):
            cfg.state_db = g("FALCON_DEMO_STATE_DB", "").strip() or cfg.state_db
        if g("FALCON_DEMO_PAUSE_FILE") is not None and g("FALCON_DEMO_PAUSE_FILE") != "":
            cfg.pause_file = g("FALCON_DEMO_PAUSE_FILE", "")
        if g("FALCON_DEMO_DRY_RUN") is not None:
            cfg.dry_run = (g("FALCON_DEMO_DRY_RUN", "").strip().lower()
                           in ("1", "true", "yes", "on"))
        try:
            if g("FALCON_DEMO_MAX_CYCLES"):
                cfg.max_cycles = max(0, int(float(g("FALCON_DEMO_MAX_CYCLES", "0") or 0)))
        except ValueError:
            pass
        if g("FALCON_DEMO_SERVER") is not None:
            cfg.server = g("FALCON_DEMO_SERVER", "")
        return cfg

    @classmethod
    def from_config_manager(cls, cm) -> "RuntimeConfig":
        """Overlay shared ConfigManager values (risk/strategy) onto defaults."""
        cfg = cls()
        try:
            cfg.fast_ema = int(cm.get("fast_ema_length", cfg.fast_ema))
            cfg.slow_ema = int(cm.get("slow_ema_length", cfg.slow_ema))
            cfg.momentum_period = int(cm.get("momentum_period", cfg.momentum_period))
            cfg.momentum_threshold = float(cm.get("momentum_threshold", cfg.momentum_threshold))
            cfg.volatility_period = int(cm.get("volatility_period", cfg.volatility_period))
            cfg.volatility_min = float(cm.get("volatility_min", cfg.volatility_min))
            cfg.volatility_max = float(cm.get("volatility_max", cfg.volatility_max))
            cfg.min_volume = float(cm.get("min_volume", cfg.min_volume))
            cfg.stop_loss_pct = float(cm.get("stop_loss_pct", cfg.stop_loss_pct))
            cfg.take_profit_pct = float(cm.get("take_profit_pct", cfg.take_profit_pct))
            cfg.risk_per_trade = float(cm.get("risk_per_trade", cfg.risk_per_trade))
            cfg.risk_per_trade_pct = float(cm.get("risk_per_trade_pct", cfg.risk_per_trade_pct))
            cfg.max_position_size = float(cm.get("max_position_size", cfg.max_position_size))
            cfg.max_daily_loss = float(cm.get("max_daily_loss", cfg.max_daily_loss))
            # Optional overlays when present in shared config.
            for _k in ("max_total_lots", "max_exposure_per_symbol",
                       "max_total_exposure", "max_daily_drawdown_pct",
                       "max_drawdown_pct", "max_consecutive_losses",
                       "cooldown_between_trades_s",
                       "stop_trading_cooldown_minutes",
                       "max_trades_per_symbol", "trades_window_s",
                       "max_spread_pct", "max_spread_absolute",
                       "min_history_bars", "trading_day_tz",
                       "signal_expiry_bars", "time_stop_bars",
                       "market_closed_backoff_s",
                       "market_closed_max_backoff_s"):
                try:
                    _v = cm.get(_k, None)
                except Exception:
                    _v = None
                if _v is not None:
                    try:
                        setattr(cfg, _k, type(getattr(cfg, _k))(_v)
                                if not isinstance(getattr(cfg, _k), str) else str(_v))
                    except (TypeError, ValueError):
                        pass
        except (TypeError, ValueError):
            pass
        return cls.from_env(cfg)
