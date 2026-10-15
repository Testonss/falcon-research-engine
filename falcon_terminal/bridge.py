"""Thread-owned Falcon providers behind a bounded snapshot event bridge.

The desktop consumes immutable messages and never calls providers directly.
"""
from __future__ import annotations

import asyncio
import math
import queue
import threading
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from typing import Deque, Dict, Mapping, Optional

from falcon_terminal.models import AssetSnapshot, TerminalSnapshot
from market_data.providers.base import LiveMarketDataProvider

DEFAULT_SOLANA_MINT = "So11111111111111111111111111111111111111112"
PRICE_HISTORY_MAX = 240


class TerminalBridge:
    """Own one background loop; expose thread-safe, nonblocking UI methods.

    join() returning True confirms providers AND HTTP executor work ended.
    A bridge is a single run; create a new instance to restart.
    providers is an injection seam for offline lifecycle tests.
    """
    def __init__(self, symbol: str = "BTCUSDT",
                 solana_mint: str = DEFAULT_SOLANA_MINT,
                 poll_interval: float = 15.0, *,
                 providers: Optional[Mapping[str, LiveMarketDataProvider]] = None,
                 refresh_interval: float = 0.25,
                 retry_interval: float = 3.0):
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
        self._injected = dict(providers) if providers is not None else None
        self._events: queue.Queue[TerminalSnapshot] = queue.Queue(maxsize=1)
        self._requested_stop = threading.Event()
        self._guard = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._async_stop: Optional[asyncio.Event] = None
        self._providers: Dict[str, LiveMarketDataProvider] = {}
        self._assets: Dict[str, AssetSnapshot] = {}
        self._errors: Dict[str, str] = {}
        self._history: Dict[str, Deque[float]] = {}
        self._sequence = 0

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

    def _publish(self, stopping: bool = False) -> None:
        self._sequence += 1
        message = TerminalSnapshot(
            self._sequence, datetime.now(timezone.utc).isoformat(),
            tuple(self._assets.values()), stopping)
        try:
            self._events.get_nowait()
        except queue.Empty:
            pass
        self._events.put_nowait(message)

    def _make_providers(self) -> None:
        if self._injected is not None:
            self._providers = self._injected
            for key, provider in self._providers.items():
                self._assets[key] = AssetSnapshot(
                    key=key, symbol=key, provider=provider.provider_name)
            return
        # Provider imports/construction stay off the GUI startup path.
        from market_data.providers.binance import BinanceLiveMarketDataProvider
        from market_data.providers.solana import SolanaDexMarketDataProvider
        definitions = (
            (f"binance:{self._symbol}", self._symbol, "binance", "WebSocket",
             lambda: BinanceLiveMarketDataProvider(
                 self._symbol, http_timeout=3.0, http_retries=1)),
            (f"solana:{self._solana_mint}", "SOL" if self._solana_mint ==
             DEFAULT_SOLANA_MINT else self._solana_mint, "solana",
             f"DexScreener polling ({max(2.0, self._poll_interval):g}s)",
             lambda: SolanaDexMarketDataProvider(
                 self._solana_mint, poll_interval_s=self._poll_interval,
                 stale_after_s=max(60.0, self._poll_interval * 3),
                 http_timeout=3.0)),
        )
        for key, symbol, name, source, factory in definitions:
            self._assets[key] = AssetSnapshot(
                key=key, symbol=symbol, provider=name, source=source)
            try:
                self._providers[key] = factory()
            except Exception as exc:
                self._assets[key] = replace(
                    self._assets[key], state="ERROR", error=str(exc))

    def _worker(self) -> None:
        try:
            # asyncio.run also waits for in-flight urllib executor threads.
            asyncio.run(self._run())
        except Exception as exc:
            self._assets = {
                key: replace(asset, state="ERROR", connected=False,
                             error=f"Backend stopped: {exc}")
                for key, asset in self._assets.items()}
            self._publish(stopping=True)
        finally:
            with self._guard:
                self._loop = None
                self._async_stop = None

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

    async def _collect(self, key: str, provider: LiveMarketDataProvider) -> None:
        previous = self._assets[key]
        try:
            health = provider.get_health()
            ticker = await provider.get_ticker()
            book = await provider.get_order_book()
            liquidity = provider.get_liquidity_event()
            try:
                live_candle = provider.get_live_candle()
            except Exception:
                live_candle = None
            price = ticker.price if ticker else None
            if price is not None and math.isfinite(price) and price > 0:
                history = self._history.setdefault(key, deque(maxlen=PRICE_HISTORY_MAX))
                if not history or history[-1] != price:
                    history.append(price)
            else:
                price = None  # never publish a non-positive/non-finite price
            bid = book.best_bid() if book else None
            ask = book.best_ask() if book else None
            spread_bps = None
            if bid and ask and bid > 0 and ask >= bid:
                spread_bps = (ask - bid) / bid * 10000.0
            symbol = (health.get("symbol") or
                      (ticker.symbol if ticker else None) or previous.symbol)
            quote = "USD" if previous.provider == "solana" else (
                symbol.rsplit("/", 1)[1] if "/" in symbol else previous.quote)
            self._assets[key] = AssetSnapshot(
                key=key, symbol=symbol, provider=previous.provider,
                quote=quote, price=price,
                bid=bid, ask=ask, spread_bps=spread_bps,
                change_pct_24h=(ticker.change_pct_24h
                                if ticker is not None else None),
                liquidity_usd=liquidity.liquidity_usd if liquidity else None,
                volume_5m=liquidity.volume_5m if liquidity else None,
                buys_5m=liquidity.buys_5m if liquidity else None,
                sells_5m=liquidity.sells_5m if liquidity else None,
                pool=health.get("pool"), dex=health.get("dex"),
                observed_at=health.get("last_message_at"),
                state=str(health.get("state", "DISCONNECTED")),
                connected=bool(health.get("connected", False)),
                stale=bool(health.get("stale", False)),
                age_s=health.get("seconds_since_message"),
                messages=int(health.get("messages_received", 0)),
                error=self._errors.get(key), source=previous.source,
                candle_open=live_candle.open if live_candle else None,
                candle_high=live_candle.high if live_candle else None,
                candle_low=live_candle.low if live_candle else None,
                candle_close=live_candle.close if live_candle else None,
                candle_volume=live_candle.volume if live_candle else None,
                candle_interval=live_candle.interval if live_candle else "",
                candle_closed=live_candle.is_closed if live_candle else None,
                price_history=tuple(self._history.get(key, ())))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._assets[key] = replace(
                previous, state="ERROR", connected=False,
                stale=previous.price is not None, error=str(exc))

    async def _run(self) -> None:
        with self._guard:
            self._loop = asyncio.get_running_loop()
            self._async_stop = asyncio.Event()
            if self._requested_stop.is_set():
                self._async_stop.set()
        self._make_providers()
        self._publish()
        connections = []
        try:
            if not self._async_stop.is_set():
                connections = [asyncio.create_task(self._connect(key, provider))
                               for key, provider in self._providers.items()]
            while not self._async_stop.is_set():
                await asyncio.gather(*(self._collect(key, provider)
                                       for key, provider in self._providers.items()))
                self._publish()
                try:
                    await asyncio.wait_for(
                        self._async_stop.wait(), self._refresh_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            # Cancel bootstraps first: a late connect must not spawn a reader
            # after its provider has already stopped.
            for task in connections:
                task.cancel()
            await asyncio.gather(*connections, return_exceptions=True)
            shutdown = await asyncio.gather(
                *(provider.disconnect() for provider in self._providers.values()),
                return_exceptions=True)
            for key, result in zip(self._providers, shutdown):
                if isinstance(result, BaseException):
                    self._errors[key] = f"Shutdown failed: {result}"
            await asyncio.gather(*(self._collect(key, provider)
                                   for key, provider in self._providers.items()))
            self._publish(stopping=True)
