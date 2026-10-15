"""Immutable messages crossing the backend / desktop boundary."""
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class AssetSnapshot:
    key: str
    symbol: str
    provider: str
    quote: str = ""
    price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread_bps: Optional[float] = None
    change_pct_24h: Optional[float] = None
    liquidity_usd: Optional[float] = None
    volume_5m: Optional[float] = None
    buys_5m: Optional[int] = None
    sells_5m: Optional[int] = None
    pool: Optional[str] = None
    dex: Optional[str] = None
    market_cap_usd: Optional[float] = None
    fdv_usd: Optional[float] = None
    observed_at: Optional[str] = None
    state: str = "CONNECTING"
    connected: bool = False
    stale: bool = False
    age_s: Optional[float] = None
    messages: int = 0
    error: Optional[str] = None
    source: str = ""
    # Live candle (Binance only): forming preferred, else last closed.
    # is_closed=False means provisional — never finalized history.
    candle_open: Optional[float] = None
    candle_high: Optional[float] = None
    candle_low: Optional[float] = None
    candle_close: Optional[float] = None
    candle_volume: Optional[float] = None
    candle_interval: str = ""
    candle_closed: Optional[bool] = None
    # Bounded recent price samples (oldest first) for the sparkline.
    # Only ever contains observed prices; empty when nothing observed.
    price_history: Tuple[float, ...] = ()
    # Chart context: recent real candles as (iso_time, o, h, l, c, v, closed).
    # Empty when the feed offers no candle history. Forming bars are flagged
    # and must never render as finalized history.
    chart_candles: Tuple[tuple, ...] = ()


@dataclass(frozen=True)
class TerminalSnapshot:
    sequence: int
    generated_at: str
    assets: Tuple[AssetSnapshot, ...]
    stopping: bool = False
