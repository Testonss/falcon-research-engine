"""Thread-owned Falcon providers behind a bounded snapshot event bridge.

The desktop consumes immutable messages and never calls providers directly.

Two modes share one worker architecture:
- legacy (injected providers, or the fixed symbol+mint pair): the exact
  v0.2 behavior — provider reads flow through the registry into snapshots;
- universe (enable_universe=True): a canonical AssetRegistry becomes the
  single source of truth. A Binance spot catalog + all-tickers poll feeds
  every listed instrument, a full provider tracks the selected symbol,
  per-mint providers track the Solana set, and a timeframe service serves
  cached history. Snapshots are built from ONE registry revision (Radar,
  Selected, Chart and future engines all see the same state) with delta
  keys so the UI only touches moved rows.
"""
from __future__ import annotations

import asyncio
import logging
import math
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Mapping, Optional, Set, Tuple

from falcon_terminal import discovery as disc
from falcon_terminal.models import AssetSnapshot, TerminalSnapshot
from falcon_terminal.registry import (
    AssetRegistry,
    CanonicalAsset,
    binance_id,
    parse_canonical_id,
    solana_mint_id,
)
from falcon_terminal.timeframes import (
    TF_TTL,
    TIMEFRAMES,
    TAIL_REFRESH_S,
    aggregate_ohlcv,
    merge_candle_series,
)
from market_data.providers.base import LiveMarketDataProvider

logger = logging.getLogger(__name__)

DEFAULT_SOLANA_MINT = "So11111111111111111111111111111111111111112"
PRICE_HISTORY_MAX = 240
FINER_THAN = {
    "5m": ("1m",),
    "15m": ("1m", "5m"),
    "1h": ("1m", "5m", "15m"),
    "4h": ("1m", "5m", "15m", "1h"),
    "1D": ("1m", "5m", "15m", "1h", "4h"),
}
CATALOG_REFRESH_S = 3600.0


def _valid_price(value) -> Optional[float]:
    try:
        if value is None:
            return None
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return v


def _chart_tuples(closed: List[dict],
                  forming: Optional[dict]) -> Tuple[tuple, ...]:
    """Chart-tuple encoding: (iso, o, h, l, c, v, closed_flag)."""
    out = []
    for b in closed:
        try:
            ts = datetime.fromtimestamp(int(b["open_ms"]) / 1000.0,
                                        tz=timezone.utc).isoformat()
            out.append((ts, float(b["o"]), float(b["h"]), float(b["l"]),
                        float(b["c"]), float(b["v"]), True))
        except (KeyError, TypeError, ValueError):
            continue
    if forming is not None:
        try:
            ts = datetime.fromtimestamp(int(forming["open_ms"]) / 1000.0,
                                        tz=timezone.utc).isoformat()
            out.append((ts, float(forming["o"]), float(forming["h"]),
                        float(forming["l"]), float(forming["c"]),
                        float(forming["v"]), False))
        except (KeyError, TypeError, ValueError):
            pass
    return tuple(out)


def _tuples_to_dicts(bars: Tuple[tuple, ...]) -> List[dict]:
    """Chart tuples back to bucket dicts for merge/aggregation."""
    out = []
    for t in bars:
        try:
            iso, o, h, l, c, v, closed = t
            ms = int(datetime.fromisoformat(str(iso)).timestamp() * 1000)
            out.append({"open_ms": ms, "o": float(o), "h": float(h),
                        "l": float(l), "c": float(c), "v": float(v),
                        "closed": bool(closed)})
        except (TypeError, ValueError):
            continue
    return out


class TerminalBridge:
    """Own one background loop; expose thread-safe, nonblocking UI methods.

    join() returning True confirms providers AND HTTP executor work ended.
    A bridge is a single run; create a new instance to restart.
    providers is an injection seam for offline lifecycle tests.
    Selection/timeframe/favorites entry points are plain thread-safe calls;
    the worker loop actuates them (provider switches, history fetches).
    """

    def __init__(self, symbol: str = "BTCUSDT",
                 solana_mint: str = DEFAULT_SOLANA_MINT,
                 poll_interval: float = 15.0, *,
                 providers: Optional[Mapping[str, LiveMarketDataProvider]] = None,
                 refresh_interval: float = 0.25,
                 retry_interval: float = 3.0,
                 universe: bool = False,
                 track_mints: Tuple[str, ...] = (),
                 max_solana_tracked: int = 25,
                 universe_poll_s: float = 20.0,
                 catalog_fetcher=None,
                 tickers_fetcher=None,
                 history_fetcher=None,
                 binance_provider_factory=None,
                 solana_provider_factory=None):
        for name, value in (("poll_interval", poll_interval),
                            ("refresh_interval", refresh_interval),
                            ("retry_interval", retry_interval)):
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be finite and greater than zero")
        self._symbol = symbol
        self._solana_mint = solana_mint
        self._poll_interval = float(poll_interval)
        self._refresh_interval = float(refresh_interval)
        self._retry_interval = float(retry_interval)
        self._universe = bool(universe)
        self._track_mints = tuple(track_mints)
        self._max_solana = max(1, int(max_solana_tracked))
        self._universe_poll_s = max(5.0, float(universe_poll_s))
        self._catalog_fetcher = catalog_fetcher or disc.fetch_spot_catalog
        self._tickers_fetcher = tickers_fetcher or disc.fetch_all_tickers
        self._history_fetcher = history_fetcher or disc.fetch_klines
        self._binance_factory = binance_provider_factory
        self._solana_factory = solana_provider_factory
        self._injected = dict(providers) if providers is not None else None
        self._events: queue.Queue[TerminalSnapshot] = queue.Queue(maxsize=1)
        self._requested_stop = threading.Event()
        self._guard = threading.Lock()
        self._prov_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._async_stop: Optional[asyncio.Event] = None
        self._providers: Dict[str, LiveMarketDataProvider] = {}
        self._conn_tasks: Set[asyncio.Task] = set()
        self._errors: Dict[str, str] = {}
        self._history: Dict[str, Deque[float]] = {}
        self._sequence = 0
        # Registry + view state (selection/timeframe/favorites).
        self._registry = AssetRegistry()
        self._selected_id: Optional[str] = None
        self._active_tf = "1m"
        self._chart_loading: Set[Tuple[str, str]] = set()
        self._chart_notes: Dict[str, str] = {}
        self._hist_tasks: Dict[Tuple[str, str], asyncio.Task] = {}
        self._last_tail: Dict[Tuple[str, str], float] = {}
        self._full_binance_id: Optional[str] = None
        self._pending_mints: Set[str] = set()
        self._uni_polls = 0
        self._uni_msg_counts: Dict[str, int] = {}
        self._catalog_at = 0.0

    # -- public control surface (thread-safe, nonblocking) ---------------------
    def start(self) -> None:
        with self._guard:
            if self._thread is not None or self._requested_stop.is_set():
                return
            self._thread = threading.Thread(
                target=self._worker, name="FalconMarketData", daemon=False)
            self._thread.start()

    def poll(self) -> Optional[TerminalSnapshot]:
        """Take the latest available message, or None; never wait."""
        try:
            return self._events.get_nowait()
        except queue.Empty:
            return None

    def stop(self) -> None:
        self._requested_stop.set()
        with self._guard:
            loop, event = self._loop, self._async_stop
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass  # worker already completed shutdown

    def join(self, timeout: Optional[float] = None) -> bool:
        with self._guard:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not self.is_alive

    @property
    def is_alive(self) -> bool:
        with self._guard:
            return self._thread is not None and self._thread.is_alive()

    @property
    def registry(self) -> AssetRegistry:
        return self._registry

    def set_selection(self, canonical_id: Optional[str]) -> None:
        """Select a canonical id for chart scoping (plain thread-safe call)."""
        self._selected_id = canonical_id or None

    def set_timeframe(self, timeframe: str) -> None:
        """Set the active chart timeframe (validated; ignored otherwise)."""
        if timeframe in TIMEFRAMES:
            self._active_tf = timeframe

    def set_favorites(self, ids) -> None:
        favs = set(ids or [])
        for cid in list(self._registry._favorites):
            if cid not in favs:
                self._registry.set_favorite(cid, False)
        for cid in favs:
            self._registry.set_favorite(cid, True)

    def toggle_favorite(self, cid: str) -> bool:
        """Flip favorite flag. Returns the new state (False when unknown)."""
        if self._registry.get(cid) is None:
            return False
        new_state = not self._registry.is_favorite(cid)
        self._registry.set_favorite(cid, new_state)
        return new_state

    def add_mint(self, mint: str) -> str:
        """Track a Solana mint. Returns "" on success, else an error string."""
        from market_data.assets import validate_solana_mint
        try:
            clean = validate_solana_mint(mint)
        except (ValueError, TypeError) as e:
            return f"invalid mint address: {e}"
        cid = solana_mint_id(clean)
        with self._prov_lock:
            if cid in self._providers:
                return ""
            tracked = sum(1 for k in list(self._providers)
                          if k.startswith("solana:"))
        if tracked + len(self._pending_mints) >= self._max_solana:
            return f"tracking limit reached ({self._max_solana} Solana markets)"
        self._registry.register(CanonicalAsset(
            id=cid, provider="solana", venue="", chain="solana",
            symbol=clean[:8], mint=clean, supported=()))
        self._pending_mints.add(clean)
        return ""

    # -- snapshots ---------------------------------------------------------------
    def _publish(self, stopping: bool = False) -> None:
        self._sequence += 1
        rows = tuple(self._row_snapshot(cid) for cid in self._registry.ids())
        changed = tuple(self._registry.drain_dirty())
        message = TerminalSnapshot(
            self._sequence, datetime.now(timezone.utc).isoformat(),
            rows, stopping, changed)
        try:
            self._events.get_nowait()
        except queue.Empty:
            pass
        self._events.put_nowait(message)

    def _row_snapshot(self, cid: str) -> AssetSnapshot:
        asset = self._registry.get(cid)
        st = self._registry.state(cid)
        price = _valid_price(st.price) if st else None
        # Sparkline sampling: latest accepted price wins, bounded, per asset.
        history: Deque[float] = self._history.setdefault(
            cid, deque(maxlen=PRICE_HISTORY_MAX))
        if price is not None and (not history or history[-1] != price):
            history.append(price)
        chart: Tuple[tuple, ...] = ()
        loading = False
        note = ""
        if asset is not None and cid == self._selected_id and self._active_tf:
            chart = self._registry.history_get(cid, self._active_tf)
            loading = (cid, self._active_tf) in self._chart_loading
            note = self._chart_notes.get(cid, "")
        return AssetSnapshot(
            key=cid, symbol=asset.symbol if asset else cid,
            provider=asset.provider if asset else "",
            quote=asset.quote if asset else "",
            price=price,
            bid=st.bid if st else None, ask=st.ask if st else None,
            spread_bps=st.spread_bps if st else None,
            change_pct_24h=st.change_24h if st else None,
            liquidity_usd=st.liquidity_usd if st else None,
            volume_5m=st.volume_5m if st else None,
            buys_5m=st.buys_5m if st else None,
            sells_5m=st.sells_5m if st else None,
            pool=st.pool_address if st and st.pool_address else None,
            dex=st.dex if st else None,
            market_cap_usd=st.market_cap if st else None,
            fdv_usd=st.fdv if st else None,
            observed_at=st.observed_at if st else None,
            state=st.state if st else "CONNECTING",
            connected=bool(st.connected) if st else False,
            stale=bool(st.stale) if st else False,
            age_s=st.age_s if st else None,
            messages=st.messages if st else 0,
            error=self._errors.get(cid),
            source=st.source if st else "",
            candle_open=st.candle_open if st else None,
            candle_high=st.candle_high if st else None,
            candle_low=st.candle_low if st else None,
            candle_close=st.candle_close if st else None,
            candle_volume=st.candle_volume if st else None,
            candle_interval=st.candle_interval if st else "",
            candle_closed=st.candle_closed if st else None,
            price_history=tuple(history),
            chart_candles=chart,
            name=asset.name if asset else "",
            mint=asset.mint or "" if asset else "",
            chain=asset.chain if asset else "",
            venue=asset.venue if asset else "",
            favorite=self._registry.is_favorite(cid),
            revision=st.revision if st else 0,
            supported_timeframes=asset.supported if asset else (),
            active_timeframe=self._active_tf if cid == self._selected_id else "",
            chart_loading=loading,
            chart_note=note)

    # -- provider construction -----------------------------------------------------
    def _supported_of(self, provider) -> Tuple[str, ...]:
        try:
            fn = getattr(provider, "supported_timeframes", None)
            if callable(fn):
                return tuple(fn() or ())
        except Exception:
            pass
        return ()

    def _register_provider_asset(self, key, provider, display="",
                                   strict=True):
        name = getattr(provider, "provider_name", "")
        chain = "solana" if name == "solana" else ""
        venue = "spot" if name == "binance" else ""
        self._registry.register(CanonicalAsset(
            id=key, provider=name, venue=venue, chain=chain,
            symbol=display or key, supported=self._supported_of(provider)),
            strict=strict)
        if name == "solana":
            mint = getattr(provider, "mint", "") or ""
            if mint:
                self._registry.enrich_identity(key, mint=mint)

    def _spawn_connect(self, key: str,
                       provider: LiveMarketDataProvider) -> None:
        task = asyncio.ensure_future(self._connect(key, provider))
        self._conn_tasks.add(task)
        task.add_done_callback(self._conn_tasks.discard)

    def _new_binance_provider(self, symbol: str):
        if self._binance_factory is not None:
            return self._binance_factory(symbol)
        from market_data.providers.binance import BinanceLiveMarketDataProvider
        return BinanceLiveMarketDataProvider(
            symbol, http_timeout=3.0, http_retries=1)

    def _new_solana_provider(self, mint: str):
        if self._solana_factory is not None:
            return self._solana_factory(mint)
        from market_data.providers.solana import SolanaDexMarketDataProvider
        return SolanaDexMarketDataProvider(
            mint, poll_interval_s=self._poll_interval,
            stale_after_s=max(60.0, self._poll_interval * 3),
            http_timeout=3.0)

    def _make_providers(self) -> None:
        if self._injected is not None:
            self._providers = self._injected
            for key, provider in self._providers.items():
                self._register_provider_asset(key, provider, strict=False)
            return
        if not self._universe:
            definitions = (
                (binance_id(self._symbol), self._symbol, "binance", "WebSocket",
                 lambda: self._new_binance_provider(self._symbol)),
                (solana_mint_id(self._solana_mint),
                 "SOL" if self._solana_mint == DEFAULT_SOLANA_MINT
                 else self._solana_mint, "solana",
                 f"DexScreener polling ({max(2.0, self._poll_interval):g}s)",
                 lambda: self._new_solana_provider(self._solana_mint)),
            )
            for key, symbol, name, source, factory in definitions:
                self._register_provider_asset(key, self._stub_provider(name))
                self._registry.enrich_identity(key, symbol=symbol,
                                               placeholders=(key,))
                try:
                    self._providers[key] = factory()
                except Exception as exc:
                    self._registry.apply_health(
                        key, state="ERROR", connected=False, error=str(exc))
                self._registry.apply_health(key, source=source)
            self._full_binance_id = binance_id(self._symbol)
            return
        # Universe mode: full provider for the initial symbol + tracked mints.
        try:
            cid = binance_id(self._symbol)
            self._providers[cid] = self._new_binance_provider(self._symbol)
            self._full_binance_id = cid
            self._register_binance(cid, self._symbol)
            self._registry.apply_health(cid, source="WebSocket")
        except Exception as exc:
            cid = binance_id(self._symbol)
            self._register_binance(cid, self._symbol)
            self._registry.apply_health(
                cid, state="ERROR", connected=False, error=str(exc))
        for mint in [self._solana_mint, *self._track_mints]:
            self._track_mint_sync(mint)

    @staticmethod
    def _stub_provider(name: str):
        # Registration-time capability probe without constructing providers.
        class _P:
            provider_name = name

            def supported_timeframes(self):
                return TIMEFRAMES if name == "binance" else ()
        return _P()

    def _register_binance(self, cid: str, raw_symbol: str) -> None:
        try:
            from market_data.providers.binance import to_falcon_symbol
            display = to_falcon_symbol(raw_symbol)
            quote = display.rsplit("/", 1)[1] if "/" in display else ""
        except Exception:
            display, quote = raw_symbol, ""
        self._registry.register(CanonicalAsset(
            id=cid, provider="binance", venue="spot", chain="",
            symbol=display, quote=quote, supported=TIMEFRAMES))

    def _track_mint_sync(self, mint: str) -> str:
        """Validate + construct a Solana provider. Returns "" or an error."""
        from market_data.assets import validate_solana_mint
        try:
            clean = validate_solana_mint(mint)
        except (ValueError, TypeError) as e:
            return f"invalid mint address: {e}"
        cid = solana_mint_id(clean)
        if cid in self._providers:
            return ""
        with self._prov_lock:
            tracked = sum(1 for k in list(self._providers)
                          if k.startswith("solana:"))
        if tracked >= self._max_solana:
            return f"tracking limit reached ({self._max_solana} Solana markets)"
        self._registry.register(CanonicalAsset(
            id=cid, provider="solana", venue="", chain="solana",
            symbol=clean[:8], mint=clean, supported=()))
        self._registry.apply_health(
            cid, source=f"DexScreener polling ({max(2.0, self._poll_interval):g}s)")
        try:
            self._providers[cid] = self._new_solana_provider(clean)
        except Exception as exc:
            self._registry.apply_health(
                cid, state="ERROR", connected=False, error=str(exc))
            return str(exc)
        return ""

    # -- worker ----------------------------------------------------------------------
    def _worker(self) -> None:
        try:
            # asyncio.run also waits for in-flight urllib executor threads.
            asyncio.run(self._run())
        except Exception as exc:
            logger.exception("market worker died: %s", exc)
            for cid in self._registry.ids():
                self._registry.apply_health(
                    cid, state="ERROR", connected=False,
                    error=f"Backend stopped: {exc}")
            self._publish(stopping=True)
        finally:
            with self._guard:
                self._loop = None
                self._async_stop = None

    def _snapshot_providers(self) -> List[Tuple[str, LiveMarketDataProvider]]:
        with self._prov_lock:
            return list(self._providers.items())

    async def _connect(self, key: str, provider: LiveMarketDataProvider) -> None:
        while not self._async_stop.is_set():
            try:
                await provider.connect()
                self._errors.pop(key, None)
                return  # provider owns streaming and reconnects
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._errors[key] = str(exc)
                try:
                    await asyncio.wait_for(
                        self._async_stop.wait(), self._retry_interval)
                except asyncio.TimeoutError:
                    continue

    def _register_or_enrich(self, key: str, provider,
                            symbol: str, quote: str) -> None:
        if self._registry.get(key) is None:
            self._register_provider_asset(key, provider)
        self._registry.enrich_identity(
            key, symbol=symbol or None, quote=quote or None,
            placeholders=(key,),
            supported=self._supported_of(provider) or None)
        if getattr(provider, "provider_name", "") == "solana":
            asset_obj = getattr(provider, "asset", None)
            if asset_obj is not None:
                self._registry.enrich_identity(
                    key, symbol=getattr(asset_obj, "symbol", None),
                    name=getattr(asset_obj, "name", None),
                    mint=getattr(asset_obj, "mint", None),
                    placeholders=(key,))

    async def _collect(self, key: str, provider: LiveMarketDataProvider) -> None:
        try:
            health = provider.get_health()
            ticker = await provider.get_ticker()
            book = await provider.get_order_book()
            liquidity = provider.get_liquidity_event()
            try:
                live_candle = provider.get_live_candle()
            except Exception:
                live_candle = None
            symbol = (health.get("symbol") or
                      (ticker.symbol if ticker else None) or key)
            if getattr(provider, "provider_name", "") == "solana":
                quote = "USD"
            else:
                quote = symbol.rsplit("/", 1)[1] if "/" in symbol else ""
            self._register_or_enrich(key, provider, symbol, quote)
            if ticker is not None:
                self._registry.apply_quote(
                    key, price=ticker.price,
                    change_24h=getattr(ticker, "change_pct_24h", None),
                    volume_24h=getattr(ticker, "volume", None),
                    event_time=getattr(ticker, "timestamp", None),
                    event_id=getattr(ticker, "event_id", None))
            if book is not None:
                try:
                    bid, ask = book.best_bid(), book.best_ask()
                except Exception:
                    bid, ask = None, None
                self._registry.apply_book(
                    key, bid=bid, ask=ask,
                    event_time=getattr(book, "timestamp", None))
            if liquidity is not None:
                self._registry.apply_liquidity(
                    key,
                    liquidity_usd=getattr(liquidity, "liquidity_usd", None),
                    market_cap=getattr(liquidity, "market_cap", None),
                    fdv=getattr(liquidity, "fdv", None),
                    volume_5m=getattr(liquidity, "volume_5m", None),
                    buys_5m=getattr(liquidity, "buys_5m", None),
                    sells_5m=getattr(liquidity, "sells_5m", None),
                    dex=getattr(liquidity, "dex", None),
                    pool_address=getattr(liquidity, "pool_address", None),
                    price_usd=getattr(liquidity, "price_usd", None),
                    event_time=getattr(liquidity, "event_time", None),
                    event_id=getattr(liquidity, "event_id", None))
            if live_candle is not None:
                self._registry.apply_candle(
                    key, open=getattr(live_candle, "open", None),
                    high=getattr(live_candle, "high", None),
                    low=getattr(live_candle, "low", None),
                    close=getattr(live_candle, "close", None),
                    volume=getattr(live_candle, "volume", None),
                    interval=getattr(live_candle, "interval", "") or "",
                    is_closed=getattr(live_candle, "is_closed", None),
                    event_time=getattr(live_candle, "timestamp", None),
                    event_id=getattr(live_candle, "event_id", None))
            if key == self._selected_id and live_candle is not None:
                self._seed_live_bar(key, live_candle)
            self._registry.apply_health(
                key, state=str(health.get("state", "DISCONNECTED")),
                connected=bool(health.get("connected", False)),
                stale=bool(health.get("stale", False)),
                messages=int(health.get("messages_received", 0)),
                error=self._errors.get(key),
                source=health.get("source", "") if isinstance(
                    health.get("source", ""), str) else "",
                observed_at=health.get("last_message_at"),
                age_s=health.get("seconds_since_message"),
                dex=health.get("dex"), pool_address=health.get("pool"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            st = self._registry.state(key)
            self._registry.apply_health(
                key, state="ERROR", connected=False,
                stale=st.price is not None if st else False,
                error=str(exc))

    # -- timeframe service ---------------------------------------------------------------
    def _seed_live_bar(self, key: str, candle) -> None:
        """Fold the selected provider's live bar into the active-view cache."""
        try:
            interval = getattr(candle, "interval", "") or ""
            if not interval or interval != self._active_tf:
                return
            entry = self._registry._history.get((key, interval))
            if not entry or not entry.bars:
                return  # the fetch service owns initial population
            bar = {"open_ms": int(candle.timestamp.timestamp() * 1000),
                   "o": float(candle.open), "h": float(candle.high),
                   "l": float(candle.low), "c": float(candle.close),
                   "v": float(candle.volume),
                   "closed": bool(getattr(candle, "is_closed", False))}
            old_closed = [b for b in _tuples_to_dicts(entry.bars)
                          if b.get("closed", True)]
            merged, forming = merge_candle_series(old_closed, [bar])
            after = _chart_tuples(merged, forming)
            if after and after != entry.bars:
                self._registry.history_put(key, interval, after)
                self._registry.mark_changed(key)
        except (TypeError, ValueError, AttributeError):
            return

    async def _ensure_active_view(self) -> None:
        """Make sure the selected (id, timeframe) has fresh chart data."""
        cid, tf = self._selected_id, self._active_tf
        if not cid:
            return
        asset = self._registry.get(cid)
        if asset is None:
            return
        if tf not in (asset.supported or ()):
            self._chart_notes[cid] = f"{tf} history not supported by this feed"
            self._chart_loading.discard((cid, tf))
            return
        if self._registry.history_fresh(cid, tf, TF_TTL[tf]):
            return
        if (cid, tf) in self._hist_tasks:
            return
        self._chart_loading.add((cid, tf))
        task = asyncio.ensure_future(self._fetch_history(cid, tf))
        self._hist_tasks[(cid, tf)] = task
        task.add_done_callback(
            lambda t, k=(cid, tf): self._hist_tasks.pop(k, None))

    async def _maybe_tail_refresh(self) -> None:
        """Refresh the active view's tail (live forming bar) when due."""
        cid, tf = self._selected_id, self._active_tf
        if not cid:
            return
        asset = self._registry.get(cid)
        if asset is None or tf not in (asset.supported or ()):
            return
        if not self._registry.history_get(cid, tf):
            return  # initial fetch owned by _ensure_active_view
        now = time.monotonic()
        if now - self._last_tail.get((cid, tf), 0.0) < TAIL_REFRESH_S:
            return
        if (cid, tf) in self._hist_tasks:
            return
        self._last_tail[(cid, tf)] = now
        task = asyncio.ensure_future(self._tail_fetch(cid, tf))
        self._hist_tasks[(cid, tf)] = task
        task.add_done_callback(
            lambda t, k=(cid, tf): self._hist_tasks.pop(k, None))

    async def _fetch_history(self, cid: str, tf: str) -> None:
        """Full history fetch + merge. Never raises into the loop."""
        try:
            asset = self._registry.get(cid)
            if asset is None or asset.provider != "binance":
                raise IOError(f"history unsupported for {cid}")
            ref = parse_canonical_id(cid)["ref"]
            candles, _rejected = await self._history_fetcher(ref, tf, 200)
            closed, forming = disc.mark_forming(candles)
            fresh = ([{"open_ms": int(c.timestamp.timestamp() * 1000),
                       "o": float(c.open), "h": float(c.high),
                       "l": float(c.low), "c": float(c.close),
                       "v": float(c.volume), "closed": True}
                      for c in closed]
                     + ([{"open_ms": int(forming.timestamp.timestamp() * 1000),
                          "o": float(forming.open), "h": float(forming.high),
                          "l": float(forming.low), "c": float(forming.close),
                          "v": float(forming.volume), "closed": False}]
                        if forming is not None else []))
            old = _tuples_to_dicts(self._registry.history_get(cid, tf))
            old_closed = [b for b in old if b.get("closed", True)]
            merged, merged_forming = merge_candle_series(old_closed, fresh)
            self._registry.history_put(cid, tf, _chart_tuples(merged, merged_forming))
            self._registry.mark_changed(cid)
            self._chart_notes.pop(cid, None)
            self._last_tail[(cid, tf)] = time.monotonic()
        except Exception as e:
            logger.warning("history fetch %s %s failed: %s", cid, tf, e)
            if not await self._aggregation_fallback(cid, tf):
                self._chart_notes[cid] = "History unavailable (feed error)"
        finally:
            self._chart_loading.discard((cid, tf))

    async def _aggregation_fallback(self, cid: str, tf: str) -> bool:
        """Aggregate from the finest cached timeframe. Returns success."""
        try:
            for finer in FINER_THAN.get(tf, ()):
                bars = self._registry.history_get(cid, finer)
                if not bars:
                    continue
                dicts = _tuples_to_dicts(bars)
                closed_in = [b for b in dicts if b.get("closed", True)]
                if not closed_in:
                    continue
                closed, forming = aggregate_ohlcv(closed_in, tf)
                self._registry.history_put(cid, tf, _chart_tuples(closed, forming))
                self._registry.mark_changed(cid)
                self._chart_notes.pop(cid, None)
                self._last_tail[(cid, tf)] = time.monotonic()
                return True
        except Exception as e:
            logger.warning("aggregation fallback %s %s failed: %s", cid, tf, e)
        return False

    async def _tail_fetch(self, cid: str, tf: str) -> None:
        """Light tail refresh: merge the last few bars into the cache."""
        try:
            asset = self._registry.get(cid)
            if asset is None or asset.provider != "binance":
                return
            ref = parse_canonical_id(cid)["ref"]
            candles, _rejected = await self._history_fetcher(ref, tf, 5)
            closed, forming = disc.mark_forming(candles)
            fresh = ([{"open_ms": int(c.timestamp.timestamp() * 1000),
                       "o": float(c.open), "h": float(c.high),
                       "l": float(c.low), "c": float(c.close),
                       "v": float(c.volume), "closed": True}
                      for c in closed]
                     + ([{"open_ms": int(forming.timestamp.timestamp() * 1000),
                          "o": float(forming.open), "h": float(forming.high),
                          "l": float(forming.low), "c": float(forming.close),
                          "v": float(forming.volume), "closed": False}]
                        if forming is not None else []))
            if not fresh:
                return
            old = [b for b in _tuples_to_dicts(self._registry.history_get(cid, tf))
                   if b.get("closed", True)]
            merged, merged_forming = merge_candle_series(old, fresh)
            before = self._registry.history_get(cid, tf)
            after = _chart_tuples(merged, merged_forming)
            if after != before:
                self._registry.history_put(cid, tf, after)
                self._registry.mark_changed(cid)
        except Exception as e:
            logger.debug("tail refresh %s %s failed: %s", cid, tf, e)
        finally:
            self._chart_loading.discard((cid, tf))

    # -- universe (catalog + all-tickers poll) -------------------------------------------------
    async def _universe_once(self) -> None:
        """One-shot catalog bootstrap (public, bounded, never fatal)."""
        try:
            assets, stats = await self._catalog_fetcher()
            for a in assets:
                self._registry.register(a)
            self._catalog_at = time.monotonic()
            logger.info("universe catalog: %d kept, %d skipped",
                        stats.get("kept", 0), stats.get("skipped", 0))
        except Exception as e:
            logger.warning("universe catalog unavailable: %s", e)

    async def _universe_tick(self) -> None:
        """All-symbols quote refresh + hourly catalog refresh."""
        now = time.monotonic()
        if now - self._catalog_at > 3600.0:
            await self._universe_once()
            return
        try:
            tickers, stats = await self._tickers_fetcher()
        except Exception as e:
            logger.warning("universe tickers unavailable: %s", e)
            return
        self._uni_polls += 1
        now_iso = datetime.now(timezone.utc).isoformat()
        unknown = 0
        for raw, data in tickers.items():
            try:
                cid = binance_id(str(raw))
            except ValueError:
                unknown += 1
                continue
            if self._registry.get(cid) is None:
                unknown += 1
                continue
            accepted, _ = self._registry.apply_quote(
                cid, price=data.get("price"),
                change_24h=data.get("change"),
                volume_24h=data.get("quote_volume"),
                event_time=data.get("event_time"),
                event_id="falcon:uni:{}:{}".format(
                    raw, data.get("event_time").isoformat()
                    if data.get("event_time") else "na"))
            if accepted:
                n = self._uni_msg_counts.get(cid, 0) + 1
                self._uni_msg_counts[cid] = n
                self._registry.apply_health(
                    cid, messages=n, observed_at=now_iso, age_s=0.0)
        if unknown:
            logger.debug("universe tick: %d unknown symbols ignored", unknown)

    async def _universe_loop(self) -> None:
        await self._universe_once()
        while not self._async_stop.is_set():
            try:
                await self._universe_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("universe loop error (continuing): %s", e)
            try:
                await asyncio.wait_for(
                    self._async_stop.wait(), self._universe_poll_s)
            except asyncio.TimeoutError:
                pass

    # -- provider dynamics (worker side) ---------------------------------------------------------
    def _track_connect(self, key: str) -> None:
        provider = self._providers.get(key)
        if provider is not None:
            self._spawn_connect(key, provider)

    def _pump_pending_mints(self) -> None:
        """Create + connect providers for UI-added mints (worker loop only)."""
        while self._pending_mints:
            mint = self._pending_mints.pop()
            err = self._track_mint_sync(mint)
            if err:
                logger.warning("mint tracking failed for %s: %s", mint[:8], err)
                continue
            self._track_connect(solana_mint_id(mint))

    async def _maybe_tail_refresh(self) -> None:
        """Refresh the active view's tail (live forming bar) when due."""
        cid, tf = self._selected_id, self._active_tf
        if not cid:
            return
        asset = self._registry.get(cid)
        if asset is None or tf not in (asset.supported or ()):
            return
        if not self._registry.history_get(cid, tf):
            return  # initial fetch owned by _ensure_active_view
        now = time.monotonic()
        if now - self._last_tail.get((cid, tf), 0.0) < TAIL_REFRESH_S:
            return
        if (cid, tf) in self._hist_tasks:
            return
        self._last_tail[(cid, tf)] = now
        task = asyncio.ensure_future(self._tail_fetch(cid, tf))
        self._hist_tasks[(cid, tf)] = task
        task.add_done_callback(
            lambda t, k=(cid, tf): self._hist_tasks.pop(k, None))

    async def _maybe_retarget_full_provider(self) -> None:
        """Retarget the full-fidelity Binance provider to the selected symbol."""
        if not self._universe or self._injected is not None:
            return
        cid = self._selected_id
        if not cid or cid == self._full_binance_id:
            return
        try:
            parsed = parse_canonical_id(cid)
        except ValueError:
            return
        if parsed["provider"] != "binance":
            return
        if self._registry.get(cid) is None:
            return
        try:
            old_id = self._full_binance_id
            with self._prov_lock:
                old = self._providers.pop(old_id, None) if old_id else None
            if old is not None:
                try:
                    await old.disconnect()
                except Exception as e:
                    logger.debug("old provider disconnect: %s", e)
            symbol = parsed["ref"]
            provider = self._new_binance_provider(symbol)
            with self._prov_lock:
                self._providers[cid] = provider
            self._full_binance_id = cid
            self._track_connect(cid)
        except Exception as e:
            logger.warning("full provider switch to %s failed: %s", cid, e)
            self._registry.apply_health(
                cid, state="ERROR", connected=False, error=str(e))

    async def _run(self) -> None:
        with self._guard:
            self._loop = asyncio.get_running_loop()
            self._async_stop = asyncio.Event()
            if self._requested_stop.is_set():
                self._async_stop.set()
        self._make_providers()
        self._publish()
        for key, provider in self._snapshot_providers():
            self._spawn_connect(key, provider)
        uni_task: Optional[asyncio.Task] = None
        try:
            if self._universe and self._injected is None \
                    and not self._async_stop.is_set():
                uni_task = asyncio.ensure_future(self._universe_loop())
            while not self._async_stop.is_set():
                self._pump_pending_mints()
                await self._maybe_retarget_full_provider()
                await asyncio.gather(*(self._collect(key, provider)
                                       for key, provider in
                                       self._snapshot_providers()))
                await self._ensure_active_view()
                await self._maybe_tail_refresh()
                self._publish()
                try:
                    await asyncio.wait_for(
                        self._async_stop.wait(), self._refresh_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            # Cancel bootstraps first: a late connect must not spawn a reader
            # after its provider has already stopped.
            for task in list(self._conn_tasks):
                task.cancel()
            if uni_task is not None:
                uni_task.cancel()
            for task in list(self._hist_tasks.values()):
                task.cancel()
            await asyncio.gather(*list(self._conn_tasks),
                                 return_exceptions=True)
            if uni_task is not None:
                await asyncio.gather(uni_task, return_exceptions=True)
            await asyncio.gather(*list(self._hist_tasks.values()),
                                 return_exceptions=True)
            final_providers = self._snapshot_providers()
            shutdown = await asyncio.gather(
                *(provider.disconnect() for _, provider in final_providers),
                return_exceptions=True)
            for key, result in zip([k for k, _ in final_providers], shutdown):
                if isinstance(result, BaseException):
                    self._errors[key] = f"Shutdown failed: {result}"
            await asyncio.gather(*(self._collect(key, provider)
                                   for key, provider in final_providers))
            self._publish(stopping=True)
