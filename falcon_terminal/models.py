"""Immutable messages crossing the backend / desktop boundary."""
from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass(frozen=True)
class MT5AccountSnapshot:
    """MT5 demo account display state. Never carries secrets."""
    state: str = "DISCONNECTED"      # DISCONNECTED/CONNECTING/CONNECTED_DEMO/...
    server: str = ""
    login: str = ""
    mode_name: str = ""              # DEMO / REAL / CONTEST / UNKNOWN
    demo: bool = False
    execution_enabled: bool = False
    message: str = "MT5 integration disabled"
    balance: Optional[float] = None
    equity: Optional[float] = None
    margin: Optional[float] = None
    margin_free: Optional[float] = None
    profit: Optional[float] = None


@dataclass(frozen=True)
class MT5PositionSnapshot:
    """One actual MT5 terminal position (terminal is authoritative)."""
    ticket: int = 0
    symbol: str = ""
    canonical_id: str = ""
    side: str = ""                   # BUY | SELL
    volume: float = 0.0
    entry: float = 0.0
    current: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    profit: Optional[float] = None
    swap: Optional[float] = None
    comment: str = ""
    open_time: Optional[str] = None


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
    # Registry identity + view state (functional market surface). All
    # defaulted so older construction sites keep working unchanged.
    name: str = ""
    mint: str = ""
    chain: str = ""
    venue: str = ""
    favorite: bool = False
    revision: int = 0
    supported_timeframes: Tuple[str, ...] = ()
    active_timeframe: str = ""
    chart_loading: bool = False
    chart_note: str = ""


@dataclass(frozen=True)
class TerminalSnapshot:
    sequence: int
    generated_at: str
    assets: Tuple[AssetSnapshot, ...]
    stopping: bool = False
    # Ids whose content changed since the previous published snapshot.
    # Lets consumers touch only moved rows instead of rebuilding tables.
    changed: Tuple[str, ...] = ()
    # MT5 demo surface (defaulted so older construction sites keep working).
    mt5_account: MT5AccountSnapshot = field(default_factory=MT5AccountSnapshot)
    mt5_positions: Tuple[MT5PositionSnapshot, ...] = ()
