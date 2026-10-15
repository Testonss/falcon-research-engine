"""Internal normalized market-event model + validation.

Provider-specific behavior stops at the normalization boundary: research /
strategy / paper code consumes these events and must never see a raw
Binance payload or a raw DEX API blob.

Conventions:
- Every event carries provider, instrument/asset key, event timestamp,
  receive timestamp, and a deterministic event id where derivable.
- Unavailable metrics are None, never zero (zero is a measured value).
- Forming candles are NEVER hashed as if they were finalized candles.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_valid_price(x: object) -> bool:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0.0


def is_valid_volume(x: object) -> bool:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v >= 0.0


def check_ohlc(o: float, h: float, l: float, c: float) -> bool:
    """Candle invariant: low <= min(o,c), high >= max(o,c), high >= low."""
    try:
        o, h, l, c = float(o), float(h), float(l), float(c)
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(v) for v in (o, h, l, c)):
        return False
    if not (o > 0 and h > 0 and l > 0 and c > 0):
        return False
    eps = 1e-9
    return (l <= min(o, c) + eps) and (h >= max(o, c) - eps) and (h + eps >= l)


def parse_float_strict(raw: object) -> Optional[float]:
    """Parse a number that may arrive as str/float/int; None if malformed.

    Rejects NaN/Infinity/empty strings. Never raises.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def deterministic_id(*parts: str) -> str:
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return h


@dataclass
class MarketTickerEvent:
    provider: str
    instrument: str  # canonical key, e.g. "binance:BTC/USDT" or "solana:<mint>"
    price: float
    event_time: datetime
    receive_time: datetime = field(default_factory=_utcnow)
    event_id: Optional[str] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    volume: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketTradeEvent:
    provider: str
    instrument: str
    price: float
    quantity: float
    side: str  # 'buy' | 'sell' | 'unknown'
    event_time: datetime
    receive_time: datetime = field(default_factory=_utcnow)
    event_id: Optional[str] = None


@dataclass
class MarketCandleEvent:
    provider: str
    instrument: str
    interval: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    open_time: datetime
    close_time: Optional[datetime] = None
    is_closed: bool = False
    receive_time: datetime = field(default_factory=_utcnow)
    event_id: Optional[str] = None


@dataclass
class MarketBestBookEvent:
    provider: str
    instrument: str
    bid: Optional[float]
    ask: Optional[float]
    event_time: datetime
    receive_time: datetime = field(default_factory=_utcnow)
    event_id: Optional[str] = None

    def spread_bps(self) -> Optional[float]:
        if self.bid and self.ask and self.bid > 0 and self.ask >= self.bid:
            return (self.ask - self.bid) / self.bid * 10000.0
        return None


@dataclass
class MarketLiquidityEvent:
    provider: str
    instrument: str
    event_time: datetime
    receive_time: datetime = field(default_factory=_utcnow)
    event_id: Optional[str] = None
    price_usd: Optional[float] = None
    price_native: Optional[float] = None
    quote_symbol: Optional[str] = None
    liquidity_usd: Optional[float] = None
    market_cap: Optional[float] = None
    fdv: Optional[float] = None
    volume_5m: Optional[float] = None
    buys_5m: Optional[int] = None
    sells_5m: Optional[int] = None
    txns_5m: Optional[int] = None
    dex: Optional[str] = None
    pool_address: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
