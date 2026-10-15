"""Binance public live market-data provider (READ-ONLY).

Public market data only. This module must never:
- place/cancel orders, access accounts, sign requests, or use API secrets.
Allowed REST paths (allowlisted, enforced):
- /api/v3/ticker/24hr, /api/v3/ticker/bookTicker, /api/v3/depth,
  /api/v3/klines, /api/v3/exchangeInfo (optional symbol validation).
Allowed WS streams: kline, bookTicker, miniTicker/trade (public).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from market_data import Candle, OrderBook, OrderBookEntry, Ticker
from market_data.health import ConnectionState
from market_data.normalization import (
    check_ohlc,
    is_valid_price,
    is_valid_volume,
    parse_float_strict,
)
from market_data.providers.base import LiveMarketDataProvider

logger = logging.getLogger(__name__)

REST_BASE = "https://api.binance.com"
WS_HOST = "stream.binance.com"
WS_PORT = 443

_ALLOWED_REST_PATHS = {
    "/api/v3/ticker/24hr",
    "/api/v3/ticker/bookTicker",
    "/api/v3/depth",
    "/api/v3/klines",
    "/api/v3/exchangeInfo",
}

# Binance order/account paths are NEVER allowed here (safety invariant).
_FORBIDDEN_SUBSTRINGS = ("order", "account", "position", "balance", "apikey",
                          "api-key", "signature", "secret", "privatekey",
                          "recvwindow")

QUOTE_ASSETS = ["USDT", "USDC", "BTC", "ETH", "BNB", "TRY", "EUR", "BRL",
                "FDUSD", "TUSD", "DAI", "SOL", "XRP", "DOGE", "TRX"]

VALID_INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h",
                   "8h", "12h", "1d", "3d", "1w", "1M"}

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{4,20}$")
_FALCON_SYMBOL_RE = re.compile(r"^([A-Z0-9]{1,12})/([A-Z0-9]{2,15})$")


# --------------------------------------------------------------------------
# Symbol normalization (single home for all conversions)
# --------------------------------------------------------------------------

def to_binance_symbol(sym: str) -> str:
    """BTC/USDT -> BTCUSDT ; BTCUSDT -> BTCUSDT (validated)."""
    if not isinstance(sym, str) or not sym.strip():
        raise ValueError(f"invalid symbol: {sym!r}")
    s = sym.strip().upper().replace(" ", "").replace("-", "").replace("_", "")
    if "/" in s:
        m = _FALCON_SYMBOL_RE.match(s)
        if not m:
            raise ValueError(f"invalid falcon symbol: {sym!r}")
        s = m.group(1) + m.group(2)
    if not _SYMBOL_RE.match(s):
        raise ValueError(f"invalid binance symbol: {sym!r}")
    return s


def to_falcon_symbol(sym: str) -> str:
    """BTCUSDT -> BTC/USDT (longest known-quote match); BTC/USDT -> itself."""
    if not isinstance(sym, str) or not sym.strip():
        raise ValueError(f"invalid symbol: {sym!r}")
    s = sym.strip().upper().replace(" ", "").replace("-", "").replace("_", "")
    if "/" in s:
        m = _FALCON_SYMBOL_RE.match(s)
        if not m:
            raise ValueError(f"invalid falcon symbol: {sym!r}")
        return s
    for q in sorted(QUOTE_ASSETS, key=len, reverse=True):
        if s.endswith(q) and len(s) > len(q):
            return f"{s[:-len(q)]}/{q}"
    raise ValueError(f"cannot resolve quote asset in {sym!r}")


def kline_stream_name(symbol: str, interval: str) -> str:
    if interval not in VALID_INTERVALS:
        raise ValueError(f"invalid interval: {interval!r}")
    return f"{to_binance_symbol(symbol).lower()}@kline_{interval}"


def combined_stream_url(streams: List[str]) -> str:
    return f"wss://{WS_HOST}:{WS_PORT}/stream?streams=" + "/".join(streams)


# --------------------------------------------------------------------------
# REST parsing (pure functions — unit tested with fixtures, no network)
# --------------------------------------------------------------------------

def _ms_to_dt(ms: Any) -> Optional[datetime]:
    try:
        v = float(ms)
    except (TypeError, ValueError):
        return None
    import math
    if not math.isfinite(v) or v <= 0:
        return None
    return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc)


def parse_ticker_24hr(payload: Dict[str, Any]) -> Optional[Ticker]:
    """Parse /api/v3/ticker/24hr. Returns None (rejected) on malformed data."""
    try:
        raw_sym = payload.get("symbol")
        price = parse_float_strict(payload.get("lastPrice"))
        vol = parse_float_strict(payload.get("volume", 0))
        ts = _ms_to_dt(payload.get("closeTime", payload.get("openTime")))
        if raw_sym is None or price is None or vol is None or ts is None:
            return None
        if not is_valid_price(price) or not is_valid_volume(vol):
            return None
        try:
            falcon = to_falcon_symbol(str(raw_sym))
        except ValueError:
            return None
        return Ticker(symbol=falcon, price=price, volume=vol, timestamp=ts,
                      event_id=f"binance:24hr:{raw_sym}:{payload.get('closeTime')}")
    except (AttributeError, TypeError):
        return None


def parse_book_ticker(payload: Dict[str, Any]) -> Optional[Tuple[str, float, float]]:
    """Parse /api/v3/ticker/bookTicker -> (falcon_symbol, bid, ask)."""
    try:
        raw_sym = payload.get("symbol")
        bid = parse_float_strict(payload.get("bidPrice"))
        ask = parse_float_strict(payload.get("askPrice"))
        if raw_sym is None or bid is None or ask is None:
            return None
        if not (is_valid_price(bid) and is_valid_price(ask) and ask >= bid):
            return None
        return to_falcon_symbol(str(raw_sym)), bid, ask
    except (AttributeError, TypeError, ValueError):
        return None


def parse_depth(payload: Dict[str, Any], falcon_symbol: str,
                depth: int = 10) -> Optional[OrderBook]:
    """Parse /api/v3/depth -> OrderBook (best-book snapshot at minimum)."""
    try:
        bids_raw = payload.get("bids", [])
        asks_raw = payload.get("asks", [])
        if not isinstance(bids_raw, list) or not isinstance(asks_raw, list):
            return None
        bids, asks = [], []
        for px, qty in bids_raw[:depth]:
            p, q = parse_float_strict(px), parse_float_strict(qty)
            if p is None or q is None or not is_valid_price(p) or not is_valid_volume(q):
                continue
            bids.append(OrderBookEntry(price=p, quantity=q, side="bid"))
        for px, qty in asks_raw[:depth]:
            p, q = parse_float_strict(px), parse_float_strict(qty)
            if p is None or q is None or not is_valid_price(p) or not is_valid_volume(q):
                continue
            asks.append(OrderBookEntry(price=p, quantity=q, side="ask"))
        ts = datetime.now(timezone.utc)
        return OrderBook(symbol=falcon_symbol, bids=bids, asks=asks, timestamp=ts)
    except (AttributeError, TypeError, ValueError):
        return None


def parse_klines(rows: Any, falcon_symbol: str, interval: str) -> Tuple[List[Candle], int]:
    """Parse /api/v3/klines array -> closed Candle list + rejected count."""
    accepted: List[Candle] = []
    rejected = 0
    if not isinstance(rows, list):
        return [], 1
    for r in rows:
        try:
            if not isinstance(r, (list, tuple)) or len(r) < 6:
                rejected += 1
                continue
            ot = _ms_to_dt(r[0])
            o = parse_float_strict(r[1])
            h = parse_float_strict(r[2])
            l = parse_float_strict(r[3])
            c = parse_float_strict(r[4])
            v = parse_float_strict(r[5])
            ct = _ms_to_dt(r[6]) if len(r) > 6 else None
            if ot is None or o is None or h is None or l is None or c is None or v is None:
                rejected += 1
                continue
            if not check_ohlc(o, h, l, c) or not is_valid_volume(v):
                rejected += 1
                continue
            accepted.append(Candle(symbol=falcon_symbol, open=o, high=h, low=l,
                                   close=c, volume=v, timestamp=ot, interval=interval,
                                   event_id=f"binance:kline:{falcon_symbol}:{interval}:{int(r[0])}:closed"))
        except (TypeError, ValueError):
            rejected += 1
    accepted.sort(key=lambda c: c.timestamp)
    return accepted, rejected


# --------------------------------------------------------------------------
# WS message parsing (pure functions)
# --------------------------------------------------------------------------

def parse_ws_message(text: str) -> Optional[Dict[str, Any]]:
    """Parse one combined-stream WS text frame -> normalized dict or None.

    Returns dicts of shape:
      {"kind": "kline", ...} | {"kind": "bookTicker", ...} |
      {"kind": "miniTicker"/"trade", ...}
    Never raises on hostile input.
    """
    try:
        msg = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(msg, dict):
        return None
    data = msg.get("data", msg)
    if not isinstance(data, dict):
        return None
    etype = str(data.get("e", "") or "")
    if not etype and "b" in data and "a" in data and "s" in data:
        # Public bookTicker frames carry no event-type marker; the
        # bid/ask/symbol triple is their signature (kline payloads nest
        # under "k" and never carry "b"/"a").
        etype = "bookTicker"
    try:
        if etype == "kline":
            k = data.get("k", {})
            if not isinstance(k, dict):
                return None
            close_px = parse_float_strict(k.get("c"))
            if close_px is None or not is_valid_price(close_px):
                return None
            o = parse_float_strict(k.get("o"))
            h = parse_float_strict(k.get("h"))
            l = parse_float_strict(k.get("l"))
            v = parse_float_strict(k.get("v", 0))
            ot = _ms_to_dt(k.get("t"))
            ct = _ms_to_dt(k.get("T"))
            if o is None or h is None or l is None or v is None or ot is None:
                return None
            if not check_ohlc(o, h, l, close_px) or not is_valid_volume(v):
                return None
            is_closed = bool(k.get("x", False))
            try:
                falcon = to_falcon_symbol(str(data.get("s") or k.get("s", "")))
            except ValueError:
                return None
            return {"kind": "kline", "symbol": falcon, "interval": str(k.get("i", "")),
                    "open": o, "high": h, "low": l, "close": close_px, "volume": v,
                    "open_time": ot, "close_time": ct, "is_closed": is_closed,
                    "event_time": ct if (is_closed and ct) else ot}
        if etype == "bookTicker":
            bid = parse_float_strict(data.get("b"))
            ask = parse_float_strict(data.get("a"))
            if bid is None or ask is None:
                return None
            if not (is_valid_price(bid) and is_valid_price(ask) and ask >= bid):
                return None
            try:
                falcon = to_falcon_symbol(str(data.get("s", "")))
            except ValueError:
                return None
            return {"kind": "bookTicker", "symbol": falcon, "bid": bid, "ask": ask,
                    "event_time": datetime.now(timezone.utc)}
        if etype in ("24hrMiniTicker", "miniTicker"):
            px = parse_float_strict(data.get("c"))
            if px is None or not is_valid_price(px):
                return None
            try:
                falcon = to_falcon_symbol(str(data.get("s", "")))
            except ValueError:
                return None
            return {"kind": "miniTicker", "symbol": falcon, "price": px,
                    "event_time": datetime.now(timezone.utc)}
        if etype in ("trade", "aggTrade"):
            px = parse_float_strict(data.get("p"))
            qty = parse_float_strict(data.get("q", 0))
            ts = _ms_to_dt(data.get("T", data.get("E")))
            if px is None or qty is None or ts is None:
                return None
            if not is_valid_price(px) or not is_valid_volume(qty):
                return None
            try:
                falcon = to_falcon_symbol(str(data.get("s", "")))
            except ValueError:
                return None
            return {"kind": "trade", "symbol": falcon, "price": px, "quantity": qty,
                    "event_time": ts}
    except (AttributeError, TypeError, ValueError):
        return None
    return None  # unknown stream payload: ignored, not an error


# --------------------------------------------------------------------------
# Minimal stdlib WS client (RFC 6455, client side)
# --------------------------------------------------------------------------

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_WS_FRAME = 2 * 1024 * 1024  # 2MB: bound memory against hostile frames


class WsClosed(Exception):
    pass


async def _ws_handshake(host: str, port: int, path_qs: str,
                        timeout: float = 10.0) -> Tuple[Any, Any]:
    ctx = ssl.create_default_context()
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=ctx), timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {path_qs} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
           f"Sec-WebSocket-Version: 13\r\nUser-Agent: falcon-research/0.2\r\n\r\n")
    writer.write(req.encode())
    await writer.drain()
    header = b""
    try:
        while b"\r\n\r\n" not in header:
            chunk = await asyncio.wait_for(reader.read(4096), timeout)
            if not chunk:
                break
            header += chunk
            if len(header) > 65536:
                break
    except asyncio.TimeoutError:
        writer.close()
        raise ConnectionError("websocket handshake timeout")
    head = header.decode("latin1", errors="replace")
    if " 101 " not in head:
        writer.close()
        raise ConnectionError(f"websocket handshake failed: {head[:120]!r}")
    accept = hashlib.sha1((key + _WS_GUID).encode()).digest()
    _ = base64.b64encode(accept)  # computed for completeness; server validated by 101
    return reader, writer


def _mask_payload(payload: bytes, mask: bytes) -> bytes:
    return bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


async def _ws_send(writer, opcode: int, payload: bytes = b""):
    import struct
    mask = os.urandom(4)
    n = len(payload)
    b1 = 0x80 | (opcode & 0x0F)
    if n < 126:
        header = struct.pack("!BB", b1, 0x80 | n)
    elif n < (1 << 16):
        header = struct.pack("!BBH", b1, 0x80 | 126, n)
    else:
        header = struct.pack("!BBQ", b1, 0x80 | 127, n)
    writer.write(header + mask + _mask_payload(payload, mask))
    await writer.drain()


async def _ws_read_frame(reader) -> Tuple[int, bytes]:
    import struct
    hdr = await reader.readexactly(2)
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    if length > _MAX_WS_FRAME:
        raise ConnectionError(f"ws frame too large ({length} bytes)")
    mask = await reader.readexactly(4) if masked else None
    payload = await reader.readexactly(length) if length else b""
    if mask:
        payload = _mask_payload(payload, mask)
    return opcode, payload


class StdlibWsConnection:
    """Async iterator of text messages over a real Binance WS stream."""

    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout
        self._reader = None
        self._writer = None
        self._closed = False

    async def __aenter__(self):
        u = urlparse(self.url)
        path = u.path + (("?" + u.query) if u.query else "")
        self._reader, self._writer = await _ws_handshake(
            u.hostname or WS_HOST, u.port or 443, path, self.timeout)
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._writer is not None:
                try:
                    await _ws_send(self._writer, 0x8, b"")
                except Exception:
                    pass
                self._writer.close()
        except Exception:
            pass

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        while True:
            try:
                opcode, payload = await _ws_read_frame(self._reader)
            except asyncio.IncompleteReadError:
                raise WsClosed("websocket closed by peer")
            if opcode == 0x8:
                raise WsClosed("websocket close frame")
            if opcode == 0x9:  # ping -> pong
                try:
                    await _ws_send(self._writer, 0xA, payload)
                except Exception:
                    pass
                continue
            if opcode == 0xA:  # pong: ignore
                continue
            if opcode == 0x1:
                try:
                    return payload.decode("utf-8")
                except UnicodeDecodeError:
                    raise ValueError("non-utf8 ws frame")
                    continue
            if opcode == 0x0:  # unexpected continuation: drop
                continue
            continue


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------

def _default_http_get(url: str, timeout: float) -> Tuple[int, str]:
    """Synchronous stdlib GET. Runs in a thread via asyncio.to_thread."""
    if "://" in url:
        path = urlparse(url).path
    else:
        path = url
    if path not in _ALLOWED_REST_PATHS:
        raise ValueError(f"blocked non-market-data path: {path!r}")
    for bad in _FORBIDDEN_SUBSTRINGS:
        if bad in url.lower():
            raise ValueError(f"blocked trading/credential URL pattern: {bad!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "falcon-research/0.2"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8")


@dataclass
class _FormingState:
    candle: Candle
    updates: int = 0


class BinanceLiveMarketDataProvider(LiveMarketDataProvider):
    """Production public-data provider for Binance spot.

    No credentials. No trading endpoints. Read-only by construction:
    every outbound URL is allowlisted and any trading/credential pattern
    raises instead of sending.
    """

    provider_name = "binance"

    def __init__(self, symbol: str = "BTCUSDT", interval: str = "1m",
                 stale_after_s: float = 30.0,
                 http_timeout: float = 10.0, http_retries: int = 3,
                 http_get: Optional[Callable[[str, float], Tuple[int, str]]] = None,
                 ws_factory: Optional[Callable[[str], Any]] = None,
                 max_seen_ids: int = 20000,
                 now_fn: Optional[Callable[[], float]] = None):
        super().__init__(stale_after_s=stale_after_s, max_seen_ids=max_seen_ids,
                         now_fn=now_fn)
        self.binance_symbol = to_binance_symbol(symbol)
        try:
            self.falcon_symbol = to_falcon_symbol(self.binance_symbol)
        except ValueError:
            self.falcon_symbol = symbol
        if interval not in VALID_INTERVALS:
            raise ValueError(f"invalid interval: {interval!r}")
        self.interval = interval
        self.http_timeout = float(http_timeout)
        self.http_retries = int(http_retries)
        self._http_get = http_get or _default_http_get
        self._ws_factory = ws_factory
        self._last_ticker: Optional[Ticker] = None
        self._last_bid: Optional[float] = None
        self._last_ask: Optional[float] = None
        self._last_book: Optional[OrderBook] = None
        self._candles: List[Candle] = []
        self._forming: Dict[Tuple[str, str], _FormingState] = {}
        self._last_trade: Optional[Dict[str, Any]] = None
        self._connect_lock = asyncio.Lock()

    # -- safety surface ------------------------------------------------------
    def describe_sources(self) -> Dict[str, Any]:
        return {"provider": "binance", "rest_base": REST_BASE,
                "streams": [kline_stream_name(self.binance_symbol, self.interval),
                            f"{self.binance_symbol.lower()}@bookTicker"],
                "credentials_required": False}

    # -- HTTP ------------------------------------------------------------------
    async def _rest_get(self, path: str, params: Dict[str, Any]) -> Any:
        qs = urllib.parse.urlencode(params)
        url = REST_BASE + path + ("?" + qs if qs else "")
        last_err: Optional[Exception] = None
        delay = 0.5
        for attempt in range(1, max(1, self.http_retries) + 1):
            try:
                status, body = await asyncio.to_thread(self._http_get, url, self.http_timeout)
                if status != 200:
                    raise IOError(f"HTTP {status} for {path}")
                if len(body) > 4 * 1024 * 1024:
                    raise IOError("response too large (>4MB)")
                return json.loads(body)
            except (IOError, ValueError, OSError) as e:
                last_err = e
                logger.warning("binance REST %s attempt %d failed: %s", path, attempt, e)
                if attempt < self.http_retries:
                    await asyncio.sleep(min(delay, 8.0))
                    delay *= 2
        raise IOError(f"binance REST {path} failed after {self.http_retries}: {last_err}")

    async def bootstrap(self) -> Dict[str, Any]:
        """Fetch ticker + book + recent candles. Bounded retries, no looping."""
        raw_klines = await self._rest_get("/api/v3/klines",
                                          {"symbol": self.binance_symbol,
                                           "interval": self.interval, "limit": 100})
        candles, rejected = parse_klines(raw_klines, self.falcon_symbol, self.interval)
        for _ in range(rejected):
            self._note_rejected()
        raw_ticker = await self._rest_get("/api/v3/ticker/24hr",
                                          {"symbol": self.binance_symbol})
        ticker = parse_ticker_24hr(raw_ticker if isinstance(raw_ticker, dict) else {})
        if ticker is None:
            self._note_rejected()
        else:
            self._last_ticker = ticker
            self._note_accepted()
        try:
            raw_book = await self._rest_get("/api/v3/ticker/bookTicker",
                                             {"symbol": self.binance_symbol})
            parsed = parse_book_ticker(raw_book if isinstance(raw_book, dict) else {})
            if parsed is None:
                self._note_rejected()
            else:
                _, bid, ask = parsed
                self._last_bid, self._last_ask = bid, ask
                self._note_accepted()
        except IOError as e:
            logger.warning("binance bookTicker bootstrap degraded: %s", e)
        try:
            raw_depth = await self._rest_get("/api/v3/depth",
                                             {"symbol": self.binance_symbol, "limit": 10})
            book = parse_depth(raw_depth if isinstance(raw_depth, dict) else {},
                               self.falcon_symbol, 10)
            if book is None:
                self._note_rejected()
            else:
                if self._last_bid and self._last_ask:
                    book.bids = [OrderBookEntry(price=self._last_bid, quantity=1.0, side="bid")]
                    book.asks = [OrderBookEntry(price=self._last_ask, quantity=1.0, side="ask")]
                self._last_book = book
        except IOError as e:
            logger.warning("binance depth bootstrap degraded: %s", e)
        self._candles = candles[-5000:]
        return {"candles": len(self._candles), "rejected": rejected,
                "ticker": self._last_ticker.price if self._last_ticker else None}

    # -- lifecycle ---------------------------------------------------------------
    async def connect(self, max_retries: int = 3):
        async with self._connect_lock:
            if self._sm.state in (ConnectionState.CONNECTED, ConnectionState.STALE):
                return
            if self._sm.state == ConnectionState.STOPPED:
                self._sm = ConnectionStateMachine(
                    self.provider_name, stale_after_s=self._sm.stale_after_s)
            self._sm.transition(ConnectionState.CONNECTING)
            delay = 0.5
            last_err: Optional[Exception] = None
            for attempt in range(1, max(1, max_retries) + 1):
                try:
                    await self.bootstrap()
                    break
                except (IOError, OSError) as e:
                    last_err = e
                    if attempt >= max_retries:
                        self._sm.transition(ConnectionState.DISCONNECTED)
                        raise
                    await asyncio.sleep(min(delay, 8.0))
                    delay *= 2
            self._sm.mark_connected()
            self._sm.transition(ConnectionState.CONNECTED) if self._sm.state != ConnectionState.CONNECTED else None
            logger.info("binance live connected: %s %s", self.falcon_symbol, self.interval)
            await self._ensure_reader(self._read_loop)

    async def disconnect(self):
        """Idempotent shutdown: stop reader, mark STOPPED."""
        if self._sm.state == ConnectionState.STOPPED:
            return
        self._sm.transition(ConnectionState.STOPPING)
        await self._stop_reader()
        self._sm.transition(ConnectionState.STOPPED)
        logger.debug("binance live disconnected")

    # -- streaming -----------------------------------------------------------------
    def _stream_url(self) -> str:
        streams = [kline_stream_name(self.binance_symbol, self.interval),
                   f"{self.binance_symbol.lower()}@bookTicker",
                   f"{self.binance_symbol.lower()}@miniTicker"]
        return combined_stream_url(streams)

    def _ws_connection(self, url: str):
        if self._ws_factory is not None:
            return self._ws_factory(url)
        return StdlibWsConnection(url)

    async def _read_loop(self):
        """Single owned reader: stream -> normalize -> ingest, reconnect bounded."""
        attempt = 0
        while not self._stop_event.is_set():
            url = self._stream_url()
            try:
                conn = self._ws_connection(url)
                async with conn if hasattr(conn, "__aexit__") else _NoCtx(conn):
                    attempt = 0
                    async for text in conn:
                        if self._stop_event.is_set():
                            return
                        self._handle_ws_text(text)
            except asyncio.CancelledError:
                return
            except (WsClosed, ConnectionError, OSError, IOError) as e:
                if self._stop_event.is_set():
                    return
                attempt += 1
                self._sm.reconnects += 1
                if not self._sm.transition(ConnectionState.RECONNECTING):
                    self._sm.force(ConnectionState.RECONNECTING)
                logger.warning("binance stream interrupted (%s); reconnect #%d",
                               e, self._sm.reconnects)
                delay = self._sm.backoff_delay(attempt)
                if not await self._sleep_cancellable(delay):
                    return
                # loop re-subscribes automatically with the same stream URL;
                # dedup + ordering state is preserved.
            except Exception as e:  # never let one bad frame kill the loop
                logger.warning("binance reader error (continuing): %s", e)
                await asyncio.sleep(0.1)

    def _handle_ws_text(self, text: str):
        """Normalize one WS frame. Never raises on hostile input."""
        try:
            if len(text) > _MAX_WS_FRAME:
                self._note_rejected()
                return
            parsed = parse_ws_message(text)
            if parsed is None:
                # Unknown stream payloads are ignored silently; malformed JSON
                # from a known envelope counts as rejected below via kind check.
                try:
                    json.loads(text)
                except (ValueError, TypeError):
                    self._note_rejected()
                    logger.warning("binance malformed ws payload rejected")
                return
            kind = parsed["kind"]
            if kind == "kline":
                self._apply_kline(parsed)
            elif kind == "bookTicker":
                self._apply_book_ticker(parsed)
            elif kind == "miniTicker":
                self._apply_mini_ticker(parsed)
            elif kind == "trade":
                self._apply_trade(parsed)
        except Exception as e:
            self._note_rejected()
            logger.warning("binance ws handle failed: %s", e)

    # -- ingest ----------------------------------------------------------------------
    def _apply_kline(self, p: Dict[str, Any]):
        sym, iv = p["symbol"], p["interval"] or self.interval
        ot: datetime = p["open_time"]
        key = self._ordering_key("candle", sym, iv)
        if self._is_out_of_order(key, ot):
            return
        if p["is_closed"]:
            event_id = (f"binance:kline:{self.binance_symbol}:{iv}:"
                        f"{int(ot.timestamp() * 1000)}:closed")
            if self._is_duplicate(event_id):
                return
            candle = Candle(symbol=sym, open=p["open"], high=p["high"], low=p["low"],
                            close=p["close"], volume=p["volume"], timestamp=ot,
                            interval=iv, event_id=event_id, is_closed=True)
            # Finalized candle replaces forming state for the same open time.
            self._forming.pop((sym, iv), None)
            self._candles.append(candle)
            if len(self._candles) > 5000:
                self._candles = self._candles[-5000:]
            self._last_ticker = Ticker(symbol=sym, price=p["close"], volume=p["volume"],
                                       timestamp=p.get("close_time") or ot, event_id=event_id)
            self._note_accepted()
        else:
            # Forming update: mutate in place, never append a new bar.
            fkey = (sym, iv)
            state = self._forming.get(fkey)
            if state is not None and state.candle.timestamp != ot:
                # A newer open time supersedes the old forming candle; an
                # older open time arriving after a newer one is stale.
                if ot < state.candle.timestamp:
                    self._sm.out_of_order_dropped += 1
                    return
                self._forming.pop(fkey, None)
                state = None
            if state is None:
                state = _FormingState(
                    Candle(symbol=sym, open=p["open"], high=p["high"], low=p["low"],
                           close=p["close"], volume=p["volume"], timestamp=ot,
                           interval=iv, event_id=None, is_closed=False))
                self._forming[fkey] = state
            else:
                c = state.candle
                c.open, c.high, c.low, c.close, c.volume = (
                    p["open"], p["high"], p["low"], p["close"], p["volume"])
            state.updates += 1
            self._last_ticker = Ticker(symbol=sym, price=p["close"], volume=p["volume"],
                                       timestamp=ot, event_id=None)
            self._note_accepted()

    def _apply_book_ticker(self, p: Dict[str, Any]):
        sym = p["symbol"]
        key = self._ordering_key("book", sym)
        now = p["event_time"]
        if self._is_out_of_order(key, now):
            return
        self._last_bid, self._last_ask = p["bid"], p["ask"]
        self._last_book = OrderBook(
            symbol=sym,
            bids=[OrderBookEntry(price=p["bid"], quantity=1.0, side="bid")],
            asks=[OrderBookEntry(price=p["ask"], quantity=1.0, side="ask")],
            timestamp=now)
        self._note_accepted()

    def _apply_mini_ticker(self, p: Dict[str, Any]):
        sym = p["symbol"]
        key = self._ordering_key("ticker", sym)
        if self._is_out_of_order(key, p["event_time"]):
            return
        self._last_ticker = Ticker(symbol=sym, price=p["price"], volume=0.0,
                                   timestamp=p["event_time"], event_id=None)
        self._note_accepted()

    def _apply_trade(self, p: Dict[str, Any]):
        key = self._ordering_key("trade", p["symbol"])
        if self._is_out_of_order(key, p["event_time"]):
            return
        self._last_trade = dict(p)
        self._last_ticker = Ticker(symbol=p["symbol"], price=p["price"],
                                   volume=p["quantity"], timestamp=p["event_time"],
                                   event_id=None)
        self._note_accepted()

    # -- MarketDataSource surface -------------------------------------------------------
    async def get_ticker(self, symbol: str = "") -> Optional[Ticker]:
        return self._last_ticker

    async def get_candles(self, symbol: str = "", interval: str = "",
                          limit: int = 100) -> List[Candle]:
        bars = list(self._candles[-max(0, limit):])
        fkey = (self.falcon_symbol, self.interval)
        state = self._forming.get(fkey)
        if state is not None and (not interval or interval == self.interval):
            bars = bars + [state.candle]
        return bars

    async def get_order_book(self, symbol: str = "", depth: int = 10) -> Optional[OrderBook]:
        return self._last_book

    def forming_candle(self) -> Optional[Candle]:
        state = self._forming.get((self.falcon_symbol, self.interval))
        return state.candle if state else None

    def get_health(self) -> Dict[str, Any]:
        self._sm.refresh_stale()
        h = self._sm.health().to_dict()
        h["symbol"] = self.falcon_symbol
        h["interval"] = self.interval
        return h


class _NoCtx:
    """Wrap a bare async-iterable factory result so `async with` still works."""

    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        enter = getattr(self.conn, "__aenter__", None)
        if enter is not None:
            return await enter()
        return self.conn

    async def __aexit__(self, *exc):
        ex = getattr(self.conn, "__aexit__", None)
        if ex is not None:
            await ex(*exc)
        else:
            close = getattr(self.conn, "close", None)
            if close is not None:
                r = close()
                if asyncio.iscoroutine(r):
                    await r
        return False

    def __aiter__(self):
        return self.conn.__aiter__()
