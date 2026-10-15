"""Live provider base: dedup, ordering, reconnect loop, health.

All live providers share these invariants:

- same logical event twice -> accepted once (dedup by event id);
- reconnect replay -> no duplicated downstream action (dedup survives);
- late events -> must not move state backward (per-stream ordering keys);
- disconnect is idempotent; reconnect never spawns duplicate reader tasks;
- forming-candle updates are keyed per (instrument, interval, open_time)
  and never hashed as unique finalized candles.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from market_data.health import ConnectionState, ConnectionStateMachine

logger = logging.getLogger(__name__)


class LiveMarketDataProvider(ABC):
    """Abstract live market-data source. Public market data only."""

    provider_name: str = "base"

    def __init__(self, stale_after_s: float = 30.0,
                 max_seen_ids: int = 20000,
                 now_fn: Optional[Callable[[], float]] = None):
        self._sm = ConnectionStateMachine(self.provider_name, now_fn=now_fn,
                                          stale_after_s=stale_after_s)
        self._seen_ids: set = set()
        self._seen_order: List[str] = []
        self._max_seen = max_seen_ids
        self._last_ts: Dict[str, float] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._reader_generations = 0  # live reader tasks ever started
        self._lock = asyncio.Lock()

    # -- abstract surface --------------------------------------------------
    @abstractmethod
    async def connect(self): ...
    @abstractmethod
    async def disconnect(self): ...
    @abstractmethod
    async def get_ticker(self, symbol: str):
        ...
    @abstractmethod
    async def get_candles(self, symbol: str, interval: str, limit: int = 100):
        ...
    @abstractmethod
    async def get_order_book(self, symbol: str, depth: int = 10):
        ...

    def get_health(self) -> Dict[str, Any]:
        self._sm.refresh_stale()
        return self._sm.health().to_dict()

    # -- dedup / ordering ----------------------------------------------------
    def _is_duplicate(self, event_id: Optional[str]) -> bool:
        if not event_id:
            return False
        if event_id in self._seen_ids:
            self._sm.duplicates_dropped += 1
            return True
        self._seen_ids.add(event_id)
        self._seen_order.append(event_id)
        if len(self._seen_order) > self._max_seen:
            old = self._seen_order.pop(0)
            self._seen_ids.discard(old)
        return False

    def _ordering_key(self, event_type: str, instrument: str,
                      interval: Optional[str] = None) -> str:
        # Ordering is per (provider, instrument, event type, interval).
        # Unrelated streams must never block each other.
        base = f"{self.provider_name}:{instrument}:{event_type}"
        return f"{base}:{interval}" if interval else base

    def _is_out_of_order(self, key: str, ts: datetime) -> bool:
        try:
            epoch = ts.timestamp()
        except Exception:
            return False
        last = self._last_ts.get(key)
        if last is not None and epoch < last:
            self._sm.out_of_order_dropped += 1
            return True
        if last is None or epoch > last:
            self._last_ts[key] = epoch
        return False

    def _note_accepted(self):
        self._sm.mark_message()

    def _note_rejected(self):
        self._sm.rejected += 1

    # -- reader task ownership -------------------------------------------------
    def _reader_running(self) -> bool:
        return self._reader_task is not None and not self._reader_task.done()

    async def _ensure_reader(self, coro_factory: Callable[[], Any]):
        """Start the reader exactly once; never duplicate tasks on reconnect."""
        async with self._lock:
            if self._reader_running():
                return self._reader_task
            self._stop_event.clear()
            self._reader_task = asyncio.create_task(coro_factory())
            self._reader_generations += 1
            return self._reader_task

    async def _stop_reader(self):
        task = None
        async with self._lock:
            self._stop_event.set()
            task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def reader_generations(self) -> int:
        return self._reader_generations

    # -- reconnect helper -------------------------------------------------------
    async def _sleep_cancellable(self, delay: float) -> bool:
        """Sleep that wakes early on stop. Returns False if stop requested."""
        try:
            await asyncio.wait_for(asyncio.shield(self._stop_event.wait()), delay)
            return False
        except asyncio.TimeoutError:
            return True
