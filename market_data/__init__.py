"""
Market data module for the Falcon Research Engine.
Handles market data interfaces, WebSocket-style handling with
deduplication, out-of-order protection and reconnect semantics.
"""

import asyncio
import logging
import math
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Ticker:
    symbol: str
    price: float
    volume: float
    timestamp: datetime
    event_id: Optional[str] = None


@dataclass
class Trade:
    symbol: str
    price: float
    quantity: float
    timestamp: datetime
    side: str  # 'buy' or 'sell'
    event_id: Optional[str] = None


@dataclass
class OrderBookEntry:
    price: float
    quantity: float
    side: str  # 'bid' or 'ask'


@dataclass
class OrderBook:
    symbol: str
    bids: List[OrderBookEntry]
    asks: List[OrderBookEntry]
    timestamp: datetime

    def best_bid(self) -> Optional[float]:
        bids = [b.price for b in self.bids if b.quantity > 0]
        return max(bids) if bids else None

    def best_ask(self) -> Optional[float]:
        asks = [a.price for a in self.asks if a.quantity > 0]
        return min(asks) if asks else None

    def mid_price(self) -> Optional[float]:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2.0

    def spread_pct(self) -> Optional[float]:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None or bb <= 0:
            return None
        return (ba - bb) / bb


@dataclass
class Candle:
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: datetime
    interval: str  # e.g. '1m', '5m', '1h'
    event_id: Optional[str] = None


class MarketDataSource(ABC):
    """Abstract market-data source."""

    @abstractmethod
    async def connect(self): ...
    @abstractmethod
    async def disconnect(self): ...
    @abstractmethod
    async def get_ticker(self, symbol: str) -> Optional[Ticker]: ...
    @abstractmethod
    async def get_order_book(self, symbol: str, depth: int = 10) -> Optional[OrderBook]: ...
    @abstractmethod
    async def get_candles(self, symbol: str, interval: str, limit: int = 100) -> List[Candle]: ...


class MarketDataHandler(MarketDataSource):
    """Handles market data connections with reliability guards.

    Guarantees:
    - duplicate events (same event_id) are ignored;
    - out-of-order events (older than last accepted timestamp per symbol+kind)
      are dropped and counted;
    - reconnect uses exponential backoff and never replays already-seen
      event_ids (caller must persist seen ids for crash recovery);
    - all callbacks are best-effort and never raise.
    """

    def __init__(self, config, max_seen_ids: int = 10000):
        self.config = config
        self.is_connected = False
        self.subscribed_symbols: set = set()
        self._connect_attempts = 0
        self._last_connect_ts: Optional[float] = None
        # Dedup / ordering state
        self._seen_event_ids: set = set()
        self._seen_order: List[str] = []  # FIFO for eviction
        self._max_seen_ids = max_seen_ids
        self._last_ts: Dict[str, float] = {}  # key = f"{kind}:{symbol}" -> epoch seconds
        # Counters (also exposed to metrics)
        self.duplicates_dropped = 0
        self.out_of_order_dropped = 0
        self.messages_handled = 0
        self.reconnects = 0
        self._callbacks: List[Callable[[Dict[str, Any]], None]] = []
        # Last values cache (for paper/backtest convenience)
        self._last_ticker: Dict[str, Ticker] = {}
        self._last_orderbook: Dict[str, OrderBook] = {}
        self._candles: Dict[str, List[Candle]] = {}

    # -- connection lifecycle -------------------------------------------------
    async def connect(self, max_retries: int = 5, base_delay: float = 0.5):
        """Connect with exponential backoff. Safe to call multiple times."""
        if self.is_connected:
            return
        delay = base_delay
        for attempt in range(1, max_retries + 1):
            try:
                self._connect_attempts += 1
                logger.debug("Connecting to market data (attempt %d)...", attempt)
                # Real WS connection would happen here; simulation always succeeds.
                await asyncio.sleep(0)
                self.is_connected = True
                self._last_connect_ts = time.time()
                logger.debug("Market data connection established")
                return
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Market data connect attempt %d failed: %s", attempt, e)
                if attempt == max_retries:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def disconnect(self):
        logger.debug("Disconnecting from market data...")
        self.is_connected = False
        logger.debug("Market data connection closed")

    async def reconnect(self):
        """Reconnect without losing dedup state (no duplicate orders downstream)."""
        self.reconnects += 1
        logger.debug("Reconnecting market data (reconnect #%d)...", self.reconnects)
        self.is_connected = False
        await self.connect()
        # NOTE: we deliberately keep _seen_event_ids and _last_ts so that
        # replayed messages after reconnect are dropped as duplicates.

    # -- subscriptions ---------------------------------------------------------
    def subscribe_to_ticker(self, symbol: str):
        if not self.is_connected:
            raise RuntimeError("Not connected to market data")
        self.subscribed_symbols.add(symbol)
        logger.debug("Subscribed to ticker for %s", symbol)

    def unsubscribe_from_ticker(self, symbol: str):
        self.subscribed_symbols.discard(symbol)
        logger.debug("Unsubscribed from ticker for %s", symbol)

    def register_callback(self, cb: Callable[[Dict[str, Any]], None]):
        self._callbacks.append(cb)

    # -- dedup / ordering -------------------------------------------------------
    def _is_duplicate(self, event_id: Optional[str]) -> bool:
        if not event_id:
            return False
        if event_id in self._seen_event_ids:
            self.duplicates_dropped += 1
            return True
        self._seen_event_ids.add(event_id)
        self._seen_order.append(event_id)
        if len(self._seen_order) > self._max_seen_ids:
            old = self._seen_order.pop(0)
            self._seen_event_ids.discard(old)
        return False

    def _is_out_of_order(self, kind: str, symbol: str, ts: datetime) -> bool:
        try:
            epoch = ts.timestamp()
        except Exception:
            return False
        key = f"{kind}:{symbol}"
        last = self._last_ts.get(key)
        if last is not None and epoch < last:
            self.out_of_order_dropped += 1
            return True
        if last is None or epoch > last:
            self._last_ts[key] = epoch
        return False

    # -- data access -------------------------------------------------------------
    async def get_ticker(self, symbol: str) -> Optional[Ticker]:
        t = self._last_ticker.get(symbol)
        if t is not None:
            return t
        return Ticker(symbol=symbol, price=0.0, volume=0.0, timestamp=_utcnow())

    async def get_order_book(self, symbol: str, depth: int = 10) -> Optional[OrderBook]:
        ob = self._last_orderbook.get(symbol)
        if ob is not None:
            return ob
        return OrderBook(symbol=symbol, bids=[], asks=[], timestamp=_utcnow())

    async def get_candles(self, symbol: str, interval: str, limit: int = 100) -> List[Candle]:
        key = f"{symbol}:{interval}"
        return list(self._candles.get(key, [])[-limit:])

    def ingest_ticker(self, ticker: Ticker) -> bool:
        """Ingest a ticker with dedup/ordering. Returns True if accepted."""
        try:
            if not (math.isfinite(float(ticker.price))
                    and math.isfinite(float(ticker.volume))):
                logger.debug("Non-finite ticker dropped for %s", ticker.symbol)
                return False
        except (TypeError, ValueError):
            logger.debug("Invalid ticker dropped for %s", ticker.symbol)
            return False
        if self._is_duplicate(ticker.event_id):
            logger.debug("Duplicate ticker dropped: %s", ticker.event_id)
            return False
        if self._is_out_of_order('ticker', ticker.symbol, ticker.timestamp):
            logger.debug("Out-of-order ticker dropped for %s", ticker.symbol)
            return False
        self._last_ticker[ticker.symbol] = ticker
        self.messages_handled += 1
        return True

    @staticmethod
    def _candle_is_finite(c) -> bool:
        try:
            return (math.isfinite(float(c.open)) and math.isfinite(float(c.high))
                    and math.isfinite(float(c.low)) and math.isfinite(float(c.close))
                    and math.isfinite(float(c.volume))
                    and float(c.open) > 0 and float(c.high) > 0
                    and float(c.low) > 0 and float(c.close) > 0
                    and float(c.volume) >= 0)
        except (TypeError, ValueError):
            return False

    def ingest_candles(self, symbol: str, interval: str, candles: List[Candle]) -> List[Candle]:
        """Ingest candles: dedups by event_id, sorts by timestamp, drops stale.

        Non-finite (NaN/Inf) or non-positive OHLC bars are rejected outright.
        Returns the list of newly accepted candles.
        """
        accepted: List[Candle] = []
        for c in sorted(candles, key=lambda x: x.timestamp):
            if not self._candle_is_finite(c):
                continue
            if self._is_duplicate(c.event_id):
                continue
            # For candle streams, allow equal timestamps (updates) but drop older.
            if self._is_out_of_order(f'candle:{interval}', symbol, c.timestamp):
                continue
            accepted.append(c)
        if accepted:
            key = f"{symbol}:{interval}"
            existing = self._candles.setdefault(key, [])
            existing.extend(accepted)
            existing.sort(key=lambda x: x.timestamp)
            # Bound memory: keep last 5000
            if len(existing) > 5000:
                self._candles[key] = existing[-5000:]
            self.messages_handled += len(accepted)
        return accepted

    def ingest_orderbook(self, ob: OrderBook) -> bool:
        if self._is_out_of_order('orderbook', ob.symbol, ob.timestamp):
            return False
        self._last_orderbook[ob.symbol] = ob
        self.messages_handled += 1
        return True

    def handle_message(self, message: Dict[str, Any]):
        """Handle incoming raw market data messages (never raises)."""
        try:
            self.messages_handled += 1
            event_id = message.get('event_id')
            if self._is_duplicate(event_id):
                return
            for cb in self._callbacks:
                try:
                    cb(message)
                except Exception as e:
                    logger.warning("Market data callback failed: %s", e)
            logger.debug("Handling market data message: %s", message.get('type', 'unknown'))
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("handle_message failed: %s", e)

    def get_stats(self) -> Dict[str, Any]:
        return {
            'is_connected': self.is_connected,
            'subscribed_symbols': sorted(self.subscribed_symbols),
            'messages_handled': self.messages_handled,
            'duplicates_dropped': self.duplicates_dropped,
            'out_of_order_dropped': self.out_of_order_dropped,
            'reconnects': self.reconnects,
            'seen_ids': len(self._seen_event_ids),
        }


# Re-exported for convenience: from market_data import load_ohlcv_csv, ...
try:
    from market_data.csv_loader import load_ohlcv_csv, load_ohlcv_dataset  # noqa: F401
except ImportError:  # pragma: no cover
    pass
