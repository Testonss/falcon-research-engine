"""Realtime architecture boundary (documentation as code).

Future live/testnet feeds plug in here WITHOUT changing strategies or risk:

- MarketDataProvider: async market-data source interface.
- ExecutionAdapter: async order-execution interface.

Replay/backtest behaviour is unchanged. Stubs below fail safely and loudly
instead of pretending to execute. Conformance is structural (duck-typed)
so existing classes need no modification.
"""
from abc import ABC, abstractmethod
from typing import Optional


class MarketDataProvider(ABC):
    """Async market-data source (replay, paper feed, or future live feed)."""

    @abstractmethod
    async def connect(self): ...
    @abstractmethod
    async def disconnect(self): ...
    @abstractmethod
    async def get_candles(self, symbol: str, interval: str, limit: int = 100): ...
    @abstractmethod
    async def get_ticker(self, symbol: str): ...
    @abstractmethod
    async def get_order_book(self, symbol: str, depth: int = 10): ...


class ExecutionAdapter(ABC):
    """Async order-execution sink (paper, backtest exchange, future testnet)."""

    @abstractmethod
    async def connect(self): ...
    @abstractmethod
    async def disconnect(self): ...
    @abstractmethod
    async def submit_order(self, symbol: str, side, order_type, quantity: float,
                           price: Optional[float] = None,
                           client_order_id: Optional[str] = None,
                           reference_price: Optional[float] = None): ...
    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool: ...
    @abstractmethod
    async def get_order_status(self, symbol: str, order_id: str): ...


def provides_market_data(obj) -> bool:
    """Structural check: can obj serve as a MarketDataProvider?"""
    return all(callable(getattr(obj, m, None)) for m in
               ("connect", "disconnect", "get_candles", "get_ticker", "get_order_book"))


def provides_execution(obj) -> bool:
    """Structural check: can obj serve as an ExecutionAdapter?"""
    return all(callable(getattr(obj, m, None)) for m in
               ("connect", "disconnect", "submit_order", "cancel_order",
                "get_order_status"))


class TestnetAdapterStub:
    """Placeholder for a future exchange-testnet adapter.

    Deliberately NOT wired to anything: every operation raises loudly so a
    misconfiguration can never silently pretend to trade.
    """

    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "Testnet adapter is not implemented in this milestone. "
            "Use the paper exchange for simulation.")

    async def connect(self):
        raise RuntimeError("Testnet adapter is not implemented in this milestone.")

    async def disconnect(self):
        raise RuntimeError("Testnet adapter is not implemented in this milestone.")

    async def submit_order(self, *args, **kwargs):
        raise RuntimeError("Testnet adapter is not implemented in this milestone.")

    async def cancel_order(self, *args, **kwargs):
        raise RuntimeError("Testnet adapter is not implemented in this milestone.")

    async def get_order_status(self, *args, **kwargs):
        raise RuntimeError("Testnet adapter is not implemented in this milestone.")
