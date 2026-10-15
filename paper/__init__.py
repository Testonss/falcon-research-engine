"""
Paper trading engine for the Falcon Research Engine.
Real-time simulation: market data -> strategy -> risk -> execution -> portfolio.
Deterministic when a seed is provided; reconnect-safe (no duplicate orders).
"""

import asyncio
import logging
import random
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta, timezone

from strategy import StrategyManager, SignalType
from market_data import Candle

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaperTradingEngine:
    def __init__(self, config, market_data_handler, execution_engine,
                 portfolio_manager, risk_manager=None, strategy_manager=None):
        self.config = config
        self.market_data = market_data_handler
        self.execution_engine = execution_engine
        self.portfolio_manager = portfolio_manager
        self.risk_manager = risk_manager
        if risk_manager is not None and getattr(execution_engine, 'risk_manager', None) is None:
            execution_engine.set_risk_manager(risk_manager)
        self.strategy_manager = strategy_manager or StrategyManager(config)
        self.is_running = False
        self.trade_interval_seconds = int(config.get('trade_interval_seconds', 5))
        seed = config.get('random_seed', 42)
        self._rng = random.Random(seed)
        self.symbols: List[str] = ["BTC/USDT"]
        self.cycles = 0

    def set_symbols(self, symbols: List[str]):
        self.symbols = list(symbols)

    async def run(self, max_cycles: Optional[int] = None):
        logger.info("Starting paper trading engine...")
        self.is_running = True
        try:
            await self.market_data.connect()
            for s in self.symbols:
                try:
                    self.market_data.subscribe_to_ticker(s)
                except Exception as e:
                    logger.warning("Subscribe failed for %s: %s", s, e)
            while self.is_running:
                for symbol in self.symbols:
                    await self._simulate_trade_cycle(symbol)
                    if not self.is_running:
                        break
                self.cycles += 1
                if max_cycles is not None and self.cycles >= max_cycles:
                    break
                await asyncio.sleep(max(0.1, self.trade_interval_seconds))
        except Exception as e:
            logger.error("Error in paper trading engine: %s", e)
            raise
        finally:
            try:
                await self.market_data.disconnect()
            except Exception:
                pass
            self.is_running = False
            logger.info("Paper trading engine stopped")

    async def stop(self):
        self.is_running = False
        logger.info("Stopping paper trading engine...")

    async def _simulate_trade_cycle(self, symbol: str):
        try:
            # Prefer ingested candles; fall back to deterministic mock for demo.
            candles = await self.market_data.get_candles(symbol, "5m", 100)
            if len(candles) < 30:
                candles = self._generate_mock_candles(symbol, 50)
                self.market_data.ingest_candles(symbol, "5m", candles)
            ticker = await self.market_data.get_ticker(symbol)
            orderbook = await self.market_data.get_order_book(symbol)
            ref_price = ticker.price if ticker and ticker.price > 0 else (candles[-1].close if candles else 0.0)

            market_data = {'candles': candles, 'ticker': ticker, 'orderbook': orderbook}
            signal = self.strategy_manager.generate_signal(symbol, market_data)
            if signal is None or signal.signal_type == SignalType.HOLD:
                logger.debug("No trading signal for %s", symbol)
                # Still enforce SL/TP on open positions
                await self._enforce_stop_take(symbol, ref_price)
                return

            logger.info("Generated signal: %s for %s (conf=%.2f)",
                        signal.signal_type.value, symbol, signal.confidence)
            prices = {symbol: ref_price}
            result = await self.execution_engine.execute_signal(signal, self.portfolio_manager, prices)
            if result.success:
                logger.info("Order executed: %s filled=%.6f @ %s",
                            result.order_id, result.filled_quantity, result.avg_fill_price)
                if self.risk_manager is not None:
                    equity = self.portfolio_manager.get_equity(prices)
                    self.risk_manager.update_peak(equity)
            else:
                logger.warning("Order execution failed/blocked: %s", result.message)
        except Exception as e:
            logger.error("Error in trade cycle: %s", e)

    async def _enforce_stop_take(self, symbol: str, current_price: float):
        """Close positions breaching SL/TP (risk-owned thresholds)."""
        if self.risk_manager is None or current_price <= 0:
            return
        pos = self.portfolio_manager.get_position(symbol)
        if pos is None:
            return
        side = 'long' if pos.quantity > 0 else 'short'
        should_close, reason = self.risk_manager.check_stop_take(pos.entry_price, current_price, side)
        if should_close:
            logger.info("SL/TP triggered for %s (%s) — closing", symbol, reason)
            from strategy import TradingSignal
            sig = TradingSignal(symbol=symbol,
                                signal_type=(SignalType.SHORT if pos.quantity > 0 else SignalType.LONG),
                                confidence=1.0, timestamp=_utcnow(), price=current_price,
                                reason=reason)
            import hashlib
            sig.signal_id = "sl_tp_" + hashlib.sha256(
                f"{symbol}{reason}{current_price}{_utcnow().isoformat()}".encode()).hexdigest()[:12]
            prices = {symbol: current_price}
            result = await self.execution_engine.execute_signal(sig, self.portfolio_manager, prices)
            # No extra risk.record here: ExecutionEngine already records the
            # closed PnL (win/loss/breakeven) from the portfolio delta.

    def _generate_mock_candles(self, symbol: str, count: int) -> list:
        """Deterministic mock candles (seeded RNG) for demos/tests."""
        candles = []
        base_price = 50000.0 + (self._rng.random() - 0.5) * 500
        ts_base = _utcnow()
        for i in range(count):
            change = (self._rng.random() - 0.5) * 400
            base_price = max(1000.0, base_price + change)
            open_price = base_price + (self._rng.random() - 0.5) * 100
            high = max(open_price, base_price) + self._rng.random() * 50
            low = min(open_price, base_price) - self._rng.random() * 50
            close = (open_price + high + low + base_price) / 4
            volume = 500 + self._rng.random() * 5000
            candles.append(Candle(symbol=symbol, open=open_price, high=high, low=low,
                                  close=close, volume=volume,
                                  timestamp=ts_base - timedelta(minutes=(count - i) * 5),
                                  interval="5m",
                                  event_id=f"mock:{symbol}:{i}:{self.cycles}"))
        return candles

    def get_engine_status(self) -> Dict[str, Any]:
        return {
            'is_running': self.is_running,
            'cycles': self.cycles,
            'trade_interval_seconds': self.trade_interval_seconds,
            'portfolio_summary': self.portfolio_manager.get_portfolio_summary(),
        }
