"""MetaTrader 5 market-data provider (READ-ONLY market data; DEMO-gated execution lives elsewhere).

Windows-terminal based, OPTIONAL dependency. This module must import cleanly
when MetaTrader5 is NOT installed: the real package is imported lazily and
every entry point accepts an injected ``mt5_module`` fake for deterministic
offline tests.

Authoritative DEMO semantics (official MT5 account trade_mode):
    0 = DEMO, 1 = CONTEST, 2 = REAL.
Never infer DEMO from server name, balance, UI label or user assertion.

Market-data responsibilities only (no order_send here):
  terminal connection / health / account state / symbol discovery /
  tick (bid/ask/last/spread) / OHLCV bars / timeframes / stale / reconnect.

Canonical MT5 ids: ``mt5:<server>:<exact_symbol>`` (broker suffixes kept).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from market_data import Candle, OrderBook, OrderBookEntry, Ticker
from market_data.health import ConnectionState
from market_data.providers.base import LiveMarketDataProvider

logger = logging.getLogger(__name__)

# Official MT5 account trade modes (MetaTrader5.account_info().trade_mode).
TRADE_MODE_DEMO = 0
TRADE_MODE_CONTEST = 1
TRADE_MODE_REAL = 2

# Terminal timeframe labels used by Falcon charts.
MT5_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1D")

# Fallback MT5 timeframe constant names (used when the real package is absent
# or for human-readable diagnostics). Real mapping resolves against the
# injected/real mt5 module attributes when available.
_TF_ATTR = {
    "1m": "TIMEFRAME_M1",
    "5m": "TIMEFRAME_M5",
    "15m": "TIMEFRAME_M15",
    "1h": "TIMEFRAME_H1",
    "4h": "TIMEFRAME_H4",
    "1D": "TIMEFRAME_D1",
}

_MT5_IMPORT_ERROR: Optional[Exception] = None


def mt5_available() -> bool:
    """True when the official MetaTrader5 package imports."""
    try:
        import MetaTrader5  # noqa: F401
        return True
    except Exception:
        return False


def import_mt5():
    """Lazy import of the official package. Raises with install hint."""
    try:
        import MetaTrader5 as mt5
        return mt5
    except Exception as exc:
        raise ImportError(
            "MetaTrader5 package not installed. "
            'Install it with: pip install ".[terminal,mt5]" (Windows + MT5 terminal required).'
        ) from exc


def mt5_id(server: str, symbol: str) -> str:
    """Canonical id ``mt5:<server>:<exact_symbol>``. Suffixes preserved."""
    srv = (server or "").strip()
    sym = (symbol or "").strip()
    if not srv or not sym:
        raise ValueError(f"invalid mt5 identity: {(server, symbol)!r}")
    if ":" in srv or ":" in sym:
        raise ValueError(f"invalid mt5 identity (':' not allowed): {(server, symbol)!r}")
    return f"mt5:{srv}:{sym}"


def parse_mt5_id(cid: str) -> Dict[str, str]:
    if not isinstance(cid, str) or not cid.startswith("mt5:"):
        raise ValueError(f"invalid mt5 canonical id: {cid!r}")
    parts = cid.split(":")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"invalid mt5 canonical id: {cid!r}")
    return {"provider": "mt5", "server": parts[1], "symbol": parts[2]}


def mt5_timeframe_constant(timeframe: str, mt5_module: Any = None) -> Any:
    """Map a Falcon timeframe to the native MT5 constant.

    Returns the real mt5.TIMEFRAME_* value when a module is supplied and
    exposes it, else the constant's attribute name (still honest for tests).
    """
    if timeframe not in _TF_ATTR:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    attr = _TF_ATTR[timeframe]
    if mt5_module is not None:
        const = getattr(mt5_module, attr, None)
        if const is not None:
            return const
    # Real package also exposes the same names; without it return the name.
    try:
        import MetaTrader5 as _real
        const = getattr(_real, attr, None)
        if const is not None:
            return const
    except Exception:
        pass
    return attr


def normalize_tick(symbol: str, tick: Any) -> Optional[Dict[str, Any]]:
    """Normalize an MT5 tick (namespace or dict) -> bid/ask/last/spread/time.

    Returns None when nothing usable is present. Never raises.
    """
    try:
        def _g(name, *alts):
            for key in (name, *alts):
                try:
                    v = getattr(tick, key) if not isinstance(tick, dict) else tick.get(key)
                except Exception:
                    v = None
                if v is not None:
                    return v
            return None

        def _f(v):
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if math.isfinite(f) and f > 0 else None

        bid = _f(_g("bid"))
        ask = _f(_g("ask"))
        last = _f(_g("last"))
        t = _g("time", "time_msc")
        event_time: Optional[datetime] = None
        try:
            if isinstance(t, (int, float)) and t > 0:
                # MT5 time is seconds; time_msc is milliseconds.
                secs = float(t) / 1000.0 if t > 4102444800 else float(t)
                event_time = datetime.fromtimestamp(secs, tz=timezone.utc)
            elif isinstance(t, datetime):
                event_time = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        except Exception:
            event_time = None
        if bid is None and ask is None and last is None:
            return None
        spread = None
        if bid and ask and ask >= bid:
            spread = ask - bid
        return {"symbol": symbol, "bid": bid, "ask": ask, "last": last,
                "spread": spread, "event_time": event_time or datetime.now(timezone.utc)}
    except Exception:
        return None


def normalize_bars(raw: Any, symbol: str, timeframe: str) -> Tuple[List[Dict[str, Any]], int]:
    """Normalize MT5 copy_rates output -> Falcon bar dicts + rejected count.

    Accepts numpy structured arrays (real MT5), lists of tuples/dicts.
    Returns (bars_oldest_first, rejected). Never raises.
    """
    rejected = 0
    bars: List[Dict[str, Any]] = []
    try:
        rows: List[Any] = []
        try:
            import numpy as _np  # type: ignore
            if isinstance(raw, _np.ndarray):
                rows = [dict(zip(raw.dtype.names, r)) if raw.dtype.names else list(r) for r in raw.tolist()]
            else:
                rows = list(raw) if raw is not None else []
        except Exception:
            try:
                rows = list(raw) if raw is not None else []
            except Exception:
                return [], 1
        for r in rows:
            try:
                if isinstance(r, dict):
                    t = r.get("time", r.get("time_msc"))
                    o, h, l, c = r.get("open"), r.get("high"), r.get("low"), r.get("close")
                    tv = r.get("tick_volume", r.get("tick_volume_", 0))
                    sp = r.get("spread", 0)
                    rv = r.get("real_volume", 0)
                elif isinstance(r, (list, tuple)) and len(r) >= 6:
                    t, o, h, l, c = r[0], r[1], r[2], r[3], r[4]
                    tv = r[5] if len(r) > 5 else 0
                    sp = r[6] if len(r) > 6 else 0
                    rv = r[7] if len(r) > 7 else 0
                else:
                    rejected += 1
                    continue
                # time -> UTC datetime
                try:
                    secs = float(t) / 1000.0 if isinstance(t, (int, float)) and t > 4102444800 else float(t)
                    ts = datetime.fromtimestamp(secs, tz=timezone.utc)
                except (TypeError, ValueError):
                    rejected += 1
                    continue
                o, h, l, c = float(o), float(h), float(l), float(c)
                if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
                    rejected += 1
                    continue
                if not (l <= min(o, c) and h >= max(o, c)):
                    rejected += 1
                    continue
                try:
                    tv_f = float(tv or 0)
                    rv_f = float(rv or 0)
                    sp_f = float(sp or 0)
                except (TypeError, ValueError):
                    tv_f, rv_f, sp_f = 0.0, 0.0, 0.0
                bars.append({"time": ts, "open": o, "high": h, "low": l,
                             "close": c, "tick_volume": tv_f, "spread": sp_f,
                             "real_volume": rv_f, "symbol": symbol,
                             "timeframe": timeframe})
            except (TypeError, ValueError):
                rejected += 1
        bars.sort(key=lambda b: b["time"])
        return bars, rejected
    except Exception:
        return [], rejected + 1


def bars_to_candles(bars: List[Dict[str, Any]], falcon_symbol: str,
                    timeframe: str, now: Optional[datetime] = None) -> Tuple[List[Candle], Optional[Candle]]:
    """Split normalized bars into (closed, forming_or_None) Falcon Candles."""
    from falcon_terminal.timeframes import TF_SECONDS
    now = now or datetime.now(timezone.utc)
    span_s = TF_SECONDS.get(timeframe, 60)
    closed: List[Candle] = []
    forming: Optional[Candle] = None
    for b in bars:
        try:
            ts: datetime = b["time"]
            c = Candle(symbol=falcon_symbol, open=float(b["open"]), high=float(b["high"]),
                       low=float(b["low"]), close=float(b["close"]),
                       volume=float(b.get("tick_volume", 0) or 0),
                       timestamp=ts, interval=timeframe,
                       event_id=f"mt5:bar:{falcon_symbol}:{timeframe}:{int(ts.timestamp())}",
                       is_closed=True)
            # A bar whose bucket is still open at `now` is forming.
            epoch = int(ts.timestamp())
            bucket_open = (epoch // span_s) * span_s
            if (bucket_open + span_s) > int(now.timestamp()):
                c.is_closed = False
                if forming is None or c.timestamp >= forming.timestamp:
                    forming = c
            else:
                closed.append(c)
        except (KeyError, TypeError, ValueError):
            continue
    closed.sort(key=lambda c: c.timestamp)
    return closed, forming


@dataclass
class MT5SymbolMeta:
    symbol: str
    description: str = ""
    currency_base: str = ""
    currency_profit: str = ""
    digits: int = 0
    point: float = 0.0
    trade_mode: Any = None
    volume_min: float = 0.0
    volume_max: float = 0.0
    volume_step: float = 0.0
    contract_size: float = 0.0
    visible: bool = True


def normalize_symbol_info(info: Any) -> Optional[MT5SymbolMeta]:
    """Normalize mt5.symbol_info() -> MT5SymbolMeta. None on malformed."""
    try:
        def _g(name, default=None):
            try:
                v = getattr(info, name) if not isinstance(info, dict) else info.get(name, default)
            except Exception:
                return default
            return default if v is None else v
        name = str(_g("name", "") or "").strip()
        if not name:
            return None
        def _fl(v, d=0.0):
            try:
                f = float(v)
                return f if math.isfinite(f) else d
            except (TypeError, ValueError):
                return d
        return MT5SymbolMeta(
            symbol=name,
            description=str(_g("description", "") or ""),
            currency_base=str(_g("currency_base", "") or ""),
            currency_profit=str(_g("currency_profit", "") or ""),
            digits=int(_g("digits", 0) or 0),
            point=_fl(_g("point", 0.0)),
            trade_mode=_g("trade_mode"),
            volume_min=_fl(_g("volume_min", 0.0)),
            volume_max=_fl(_g("volume_max", 0.0)),
            volume_step=_fl(_g("volume_step", 0.0)),
            contract_size=_fl(_g("contract_size", 0.0)),
            visible=bool(_g("visible", True)),
        )
    except Exception:
        return None


class MT5LiveMarketDataProvider(LiveMarketDataProvider):
    """Polling MT5 market-data provider. Public data only; never trades.

    ``mt5_module`` is an injection seam: pass a fake in tests, else the real
    MetaTrader5 package is imported lazily on connect(). ``symbols`` restricts
    the tracked set (default: all discovered); discovery always comes from
    symbols_get(), never hardcoded.
    """

    provider_name = "mt5"

    def __init__(self, symbols: Tuple[str, ...] = (), server: str = "",
                 poll_interval_s: float = 5.0, stale_after_s: float = 30.0,
                 terminal_path: str = "", mt5_module: Any = None,
                 max_seen_ids: int = 20000,
                 now_fn: Optional[Callable[[], float]] = None):
        super().__init__(stale_after_s=stale_after_s, max_seen_ids=max_seen_ids,
                         now_fn=now_fn)
        self._wanted = tuple(s for s in (symbols or ()) if s)
        self.server = (server or "").strip()
        self.poll_interval = max(1.0, float(poll_interval_s))
        self.terminal_path = terminal_path or ""
        self._mt5 = mt5_module  # lazy real import on connect when None
        self._server_actual = ""
        self._symbols: List[MT5SymbolMeta] = []
        self._ticks: Dict[str, Dict[str, Any]] = {}
        self._candles: Dict[str, List[Candle]] = {}
        self._forming: Dict[str, Candle] = {}
        self._last_error: str = ""
        self._connect_lock = asyncio.Lock()
        self._poll_task: Optional[asyncio.Task] = None

    # -- introspection -----------------------------------------------------
    def describe_sources(self) -> Dict[str, Any]:
        return {"provider": "mt5", "terminal_path": self.terminal_path or "(attached)",
                "server": self._server_actual or self.server or "(unknown)",
                "symbols_tracked": len(self._symbols),
                "credentials_required": False,
                "note": "attaches to the user-authenticated MT5 terminal; no password stored"}

    @property
    def symbols(self) -> List[MT5SymbolMeta]:
        return list(self._symbols)

    @property
    def server_actual(self) -> str:
        return self._server_actual

    def canonical_id(self, symbol: str) -> str:
        return mt5_id(self._server_actual or self.server or "unknown", symbol)

    # -- lifecycle ----------------------------------------------------------
    def _require_mt5(self) -> Any:
        if self._mt5 is not None:
            return self._mt5
        self._mt5 = import_mt5()
        return self._mt5

    async def connect(self, max_retries: int = 3):
        async with self._connect_lock:
            if self._sm.state in (ConnectionState.CONNECTED, ConnectionState.STALE):
                return
            if self._sm.state == ConnectionState.STOPPED:
                from market_data.health import ConnectionStateMachine
                self._sm = ConnectionStateMachine("mt5", stale_after_s=self._sm.stale_after_s)
            self._sm.transition(ConnectionState.CONNECTING)
            try:
                mt5 = self._require_mt5()
            except ImportError as e:
                self._last_error = str(e)
                self._sm.transition(ConnectionState.DISCONNECTED)
                raise
            last_err: Optional[Exception] = None
            for attempt in range(1, max(1, max_retries) + 1):
                try:
                    ok = await asyncio.to_thread(self._initialize_terminal, mt5)
                    if not ok:
                        raise IOError(f"MT5 initialize failed: {self._last_mt5_error(mt5)}")
                    await asyncio.to_thread(self._discover_symbols, mt5)
                    break
                except (IOError, OSError, RuntimeError) as e:
                    last_err = e
                    self._last_error = str(e)
                    if attempt >= max_retries:
                        self._sm.transition(ConnectionState.DISCONNECTED)
                        raise
                    await asyncio.sleep(min(0.5 * (2 ** (attempt - 1)), 8.0))
            self._sm.mark_connected()
            if self._sm.state != ConnectionState.CONNECTED:
                self._sm.transition(ConnectionState.CONNECTED)
            logger.info("mt5 market-data connected: server=%s symbols=%d",
                        self._server_actual, len(self._symbols))
            await self._ensure_reader(self._poll_loop)

    def _initialize_terminal(self, mt5: Any) -> bool:
        try:
            kwargs = {}
            if self.terminal_path:
                kwargs["path"] = self.terminal_path
            ok = mt5.initialize(**kwargs) if kwargs else mt5.initialize()
        except Exception as e:
            self._last_error = f"MT5 terminal not running ({e})"
            return False
        if not ok:
            try:
                code, msg = mt5.last_error()
                self._last_error = f"MT5 initialize failed ({code}: {msg})"
            except Exception:
                self._last_error = "MT5 terminal not running or not installed"
            return False
        try:
            acc = mt5.account_info()
            if acc is not None:
                srv = getattr(acc, "server", "") or ""
                if srv:
                    self._server_actual = str(srv)
        except Exception as e:
            logger.debug("mt5 account probe: %s", e)
        if not self._server_actual and self.server:
            self._server_actual = self.server
        return True

    def _last_mt5_error(self, mt5: Any) -> str:
        try:
            code, msg = mt5.last_error()
            if self._last_error and "MT5" in self._last_error:
                return self._last_error
            return f"{code}: {msg}"
        except Exception:
            return self._last_error or "unknown MT5 error"

    def _discover_symbols(self, mt5: Any) -> None:
        try:
            raw = mt5.symbols_get()
        except Exception as e:
            raise IOError(f"MT5 symbol discovery failed: {e}")
        if not raw:
            raise IOError("MT5 returned no symbols (no account connected?)")
        metas: List[MT5SymbolMeta] = []
        for info in raw:
            meta = normalize_symbol_info(info)
            if meta is None:
                continue
            if self._wanted and meta.symbol not in self._wanted:
                continue
            metas.append(meta)
        if not metas:
            raise IOError("MT5 symbol discovery: none of the requested symbols available")
        # Select (make visible) each tracked symbol so ticks flow.
        for m in metas:
            try:
                mt5.symbol_select(m.symbol, True)
            except Exception as e:
                logger.debug("mt5 symbol_select %s: %s", m.symbol, e)
        self._symbols = sorted(metas, key=lambda m: m.symbol)
        if not self._server_actual and self.server:
            self._server_actual = self.server

    async def disconnect(self):
        if self._sm.state == ConnectionState.STOPPED:
            return
        self._sm.transition(ConnectionState.STOPPING)
        await self._stop_reader()
        try:
            if self._mt5 is not None:
                mt5 = self._mt5
                try:
                    await asyncio.to_thread(mt5.shutdown)
                except Exception:
                    pass
        finally:
            self._sm.transition(ConnectionState.STOPPED)

    # -- polling -------------------------------------------------------------
    async def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("mt5 poll error (continuing): %s", e)
            if not await self._sleep_cancellable(self.poll_interval):
                return

    async def refresh_once(self) -> int:
        """One poll of all tracked ticks. Returns ticks accepted."""
        mt5 = self._mt5
        if mt5 is None:
            return 0
        accepted = 0
        for meta in list(self._symbols):
            try:
                raw = await asyncio.to_thread(mt5.symbol_info_tick, meta.symbol)
            except Exception as e:
                logger.debug("mt5 tick %s: %s", meta.symbol, e)
                continue
            if raw is None:
                continue
            norm = normalize_tick(meta.symbol, raw)
            if norm is None:
                self._note_rejected()
                continue
            key = self._ordering_key("tick", meta.symbol)
            if self._is_out_of_order(key, norm["event_time"]):
                continue
            self._ticks[meta.symbol] = norm
            self._note_accepted()
            accepted += 1
        return accepted

    async def fetch_bars(self, symbol: str, timeframe: str, limit: int = 200) -> Tuple[List[Candle], int]:
        """Fetch native MT5 bars via copy_rates_from_pos. Never synthesizes."""
        if timeframe not in MT5_TIMEFRAMES:
            raise ValueError(f"unsupported timeframe: {timeframe!r}")
        mt5 = self._require_mt5()
        tf_const = mt5_timeframe_constant(timeframe, mt5)
        limit = max(2, min(int(limit), 1000))
        try:
            raw = await asyncio.to_thread(mt5.copy_rates_from_pos, symbol, tf_const, 0, limit)
        except Exception as e:
            raise IOError(f"MT5 history failed for {symbol} {timeframe}: {e}")
        if raw is None or (hasattr(raw, "__len__") and len(raw) == 0):
            return [], 0
        bars, rejected = normalize_bars(raw, symbol, timeframe)
        for _ in range(rejected):
            self._note_rejected()
        falcon_sym = symbol  # display keeps the exact broker symbol
        closed, forming = bars_to_candles(bars, falcon_sym, timeframe)
        key = f"{symbol}:{timeframe}"
        self._candles[key] = closed
        if forming is not None:
            self._forming[key] = forming
        return closed + ([forming] if forming is not None else []), rejected

    # -- MarketDataSource surface --------------------------------------------
    def _primary_symbol(self) -> str:
        if self._symbols:
            return self._symbols[0].symbol
        return ""

    async def get_ticker(self, symbol: str = "") -> Optional[Ticker]:
        sym = symbol or self._primary_symbol()
        tick = self._ticks.get(sym)
        if tick is None:
            # No tick observed yet -> None (never zero).
            return None
        price = tick.get("last") or tick.get("ask") or tick.get("bid")
        if price is None:
            return None
        return Ticker(symbol=sym, price=float(price), volume=0.0,
                      timestamp=tick.get("event_time") or datetime.now(timezone.utc),
                      event_id=f"mt5:tick:{sym}:{tick.get('event_time')}",
                      change_pct_24h=None)

    async def get_order_book(self, symbol: str = "", depth: int = 10) -> Optional[OrderBook]:
        sym = symbol or self._primary_symbol()
        tick = self._ticks.get(sym)
        if tick is None:
            return None
        bid, ask = tick.get("bid"), tick.get("ask")
        if bid is None and ask is None:
            return None
        now = tick.get("event_time") or datetime.now(timezone.utc)
        bids = [OrderBookEntry(price=float(bid), quantity=1.0, side="bid")] if bid else []
        asks = [OrderBookEntry(price=float(ask), quantity=1.0, side="ask")] if ask else []
        return OrderBook(symbol=sym, bids=bids, asks=asks, timestamp=now)

    async def get_candles(self, symbol: str = "", interval: str = "", limit: int = 100) -> List[Candle]:
        key = f"{symbol or self._primary_symbol()}:{interval or '1m'}"
        bars = list(self._candles.get(key, [])[-max(0, limit):])
        forming = self._forming.get(key)
        if forming is not None:
            bars = bars + [forming]
        return bars

    def get_live_candle(self) -> Optional[Candle]:
        if not self._candles and not self._forming:
            return None
        # Newest across tracked keys.
        best: Optional[Candle] = None
        for c in list(self._forming.values()):
            if best is None or c.timestamp > best.timestamp:
                best = c
        if best is not None:
            return best
        for bars in self._candles.values():
            if bars and (best is None or bars[-1].timestamp > best.timestamp):
                best = bars[-1]
        return best

    def supported_timeframes(self):
        return MT5_TIMEFRAMES

    def get_health(self) -> Dict[str, Any]:
        self._sm.refresh_stale()
        h = self._sm.health().to_dict()
        h["symbol"] = self._primary_symbol()
        h["server"] = self._server_actual or self.server
        h["symbols_tracked"] = len(self._symbols)
        if self._last_error:
            h["error"] = self._last_error
        return h

    def terminal_health(self) -> Dict[str, Any]:
        """terminal_info + account_info + last_error snapshot (never raises)."""
        out: Dict[str, Any] = {"available": self._mt5 is not None, "error": self._last_error}
        mt5 = self._mt5
        if mt5 is None:
            out["terminal"] = None
            out["account"] = None
            return out
        try:
            ti = mt5.terminal_info()
            out["terminal"] = {k: getattr(ti, k) for k in
                               ("company", "name", "path", "connected", "trade_allowed",
                                "build", "maxbars") if hasattr(ti, k)} if ti is not None else None
        except Exception as e:
            out["terminal"] = None
            out["terminal_error"] = str(e)
        try:
            acc = mt5.account_info()
            out["account"] = {k: getattr(acc, k) for k in
                              ("login", "server", "trade_mode", "balance", "equity",
                               "margin", "margin_free", "profit", "currency") if hasattr(acc, k)} \
                if acc is not None else None
            # Never include password/token fields even if the API ever adds them.
        except Exception as e:
            out["account"] = None
            out["account_error"] = str(e)
        try:
            out["last_error"] = mt5.last_error()
        except Exception:
            out["last_error"] = None
        return out
