"""
Execution module for the Falcon Research Engine.
Order execution with risk gating, idempotency, timeouts and retries.
Flow enforced: signal -> risk manager -> execution -> portfolio.
"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass
from enum import Enum

from strategy import SignalType
from exchange import OrderSide, OrderType, OrderStatus

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionStatus(Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class ExecutionResult:
    success: bool
    order_id: str
    message: str
    timestamp: datetime
    filled_quantity: float = 0.0
    avg_fill_price: Optional[float] = None
    commission: float = 0.0
    blocked_by: Optional[str] = None
    # Cost-breakdown attribution (quote currency, vs reference price).
    spread_cost: float = 0.0
    slippage_cost: float = 0.0
    impact_cost: float = 0.0
    # Liquidity diagnostics.
    liquidity_capped: bool = False
    participation_rate: float = 0.0
    reject_reason: Optional[str] = None


class ExecutionEngine:
    """Executes signals after risk approval; updates portfolio on fills."""

    def __init__(self, config, exchange, risk_manager=None, max_retries: int = 2,
                 timeout_s: float = 10.0):
        self.config = config
        self.exchange = exchange
        self.risk_manager = risk_manager
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self.executed_orders: List[Tuple[Any, ExecutionResult]] = []
        self._seen_signals: Dict[str, ExecutionResult] = {}  # signal_id -> result
        self._last_submit_ts = 0.0
        self._min_submit_interval = 0.05  # simple rate limit (20/s max)

    def set_risk_manager(self, risk_manager):
        self.risk_manager = risk_manager

    async def execute_signal(self, signal, portfolio_manager,
                             current_prices: Optional[Dict[str, float]] = None,
                             liquidity: Optional[Dict[str, float]] = None,
                             sim_now=None) -> ExecutionResult:
        """Execute a signal. Optional `liquidity` ({bar_volume, bar_high,
        bar_low}) enables liquidity-aware taker execution at the exchange.

        Optional `sim_now` is the simulated bar timestamp driving risk
        decisions (cooldown, day boundaries). Historical research callers
        (backtest, paper replay) pass the execution-bar timestamp so
        quantitative behavior never depends on wall-clock time; live paths
        leave it None (risk falls back to real time). `Result.timestamp`
        metadata always uses wall-clock time.
        """
        if signal is None or getattr(signal, 'signal_type', None) == SignalType.HOLD:
            return ExecutionResult(success=False, order_id="", message="No signal to execute",
                                   timestamp=_utcnow(), blocked_by="hold")
        # Idempotency on signal_id: never execute the same signal twice.
        signal_id = getattr(signal, 'signal_id', None)
        if signal_id and signal_id in self._seen_signals:
            prev = self._seen_signals[signal_id]
            logger.debug("Duplicate signal %s ignored (order %s)", signal_id, prev.order_id)
            return prev

        try:
            prices = dict(current_prices or {})
            ref_price = signal.price or prices.get(signal.symbol, 0.0)
            if ref_price is None or ref_price <= 0:
                return ExecutionResult(success=False, order_id="", message="Missing reference price",
                                       timestamp=_utcnow(), blocked_by="no_price")
            prices.setdefault(signal.symbol, ref_price)

            equity = portfolio_manager.get_equity(prices) if hasattr(portfolio_manager, 'get_equity') \
                else portfolio_manager.get_portfolio_value(prices)

            quantity = self._calculate_order_quantity(signal, portfolio_manager, ref_price, equity)
            if quantity <= 0:
                return ExecutionResult(success=False, order_id="", message="Position size is zero",
                                       timestamp=_utcnow(), blocked_by="sizing")

            # Risk gate — FINAL authority (simulated bar time when provided).
            if self.risk_manager is not None:
                allowed, reason = self.risk_manager.evaluate_trade(
                    signal.symbol, quantity, ref_price, equity, now=sim_now)
                if not allowed:
                    logger.debug("Risk blocked %s %s: %s", signal.symbol, signal.signal_type, reason)
                    result = ExecutionResult(success=False, order_id="", message=f"Blocked by risk: {reason}",
                                             timestamp=_utcnow(), blocked_by=reason)
                    if signal_id:
                        self._seen_signals[signal_id] = result
                    return result
            else:
                logger.warning("No risk manager attached — proceeding (tests only)")

            side = OrderSide.BUY if signal.signal_type == SignalType.LONG else OrderSide.SELL
            client_id = f"sig_{signal_id}" if signal_id else None

            # Rate limit (wall-clock pacing only: affects replay speed, never
            # trading decisions — no simulated-time equivalent needed).
            now = time.time()
            wait = self._min_submit_interval - (now - self._last_submit_ts)
            if wait > 0:
                await asyncio.sleep(wait)

            order = None
            last_err: Optional[Exception] = None
            liq = dict(liquidity or {})
            for attempt in range(self.max_retries + 1):
                try:
                    self.exchange.set_reference_price(signal.symbol, ref_price)
                    order = await asyncio.wait_for(
                        self.exchange.submit_order(
                            symbol=signal.symbol, side=side, order_type=OrderType.MARKET,
                            quantity=quantity, client_order_id=client_id,
                            reference_price=ref_price,
                            bar_volume=liq.get('bar_volume'),
                            bar_high=liq.get('bar_high'),
                            bar_low=liq.get('bar_low')),
                        timeout=self.timeout_s)
                    last_err = None
                    break
                except asyncio.TimeoutError as e:
                    last_err = e
                    logger.warning("Order submit timeout (attempt %d)", attempt + 1)
                    # Retry is safe: same client_order_id dedups on exchange.
                    continue
                except Exception as e:
                    last_err = e
                    logger.error("Order submit failed: %s", e)
                    break
            self._last_submit_ts = time.time()

            if order is None:
                result = ExecutionResult(success=False, order_id="",
                                         message=f"Execution failed: {last_err}",
                                         timestamp=_utcnow(), blocked_by="exchange_error")
                if signal_id:
                    self._seen_signals[signal_id] = result
                return result

            if order.status == OrderStatus.REJECTED:
                result = ExecutionResult(success=False, order_id=order.order_id,
                                         message=f"Order rejected: {order.reject_reason}",
                                         timestamp=_utcnow(), blocked_by="rejected",
                                         reject_reason=order.reject_reason,
                                         participation_rate=order.participation_rate)
                self.executed_orders.append((order, result))
                if signal_id:
                    self._seen_signals[signal_id] = result
                return result

            # Apply fill to portfolio (idempotent via order_id).
            realized_before = getattr(portfolio_manager, 'realized_pnl', 0.0)
            if order.filled_quantity and order.filled_quantity > 0:
                is_buy = (side == OrderSide.BUY)
                portfolio_manager.update_position(
                    signal.symbol, order.filled_quantity,
                    order.avg_fill_price or ref_price,
                    is_buy=is_buy, order_id=order.order_id,
                    fee=order.commission)

            # Feed closed PnL back to risk (wins/losses, daily loss, drawdown).
            try:
                if self.risk_manager is not None:
                    realized_after = getattr(portfolio_manager, 'realized_pnl', realized_before)
                    closed_pnl = realized_after - realized_before
                    equity_after = portfolio_manager.get_equity(prices) \
                        if hasattr(portfolio_manager, 'get_equity') \
                        else portfolio_manager.get_portfolio_value(prices)
                    if abs(closed_pnl) > 1e-9:
                        self.risk_manager.record_trade_result(
                            closed_pnl > 0, pnl=closed_pnl, equity=equity_after,
                            now=sim_now)
                    else:
                        self.risk_manager.update_peak(equity_after)
            except Exception as e:
                logger.debug("Risk recording skipped: %s", e)

            result = ExecutionResult(
                success=order.status in (OrderStatus.FILLED, OrderStatus.PARTIAL),
                order_id=order.order_id, message=f"Order {order.order_id} {order.status.value}",
                timestamp=_utcnow(), filled_quantity=order.filled_quantity,
                avg_fill_price=order.avg_fill_price, commission=order.commission,
                spread_cost=order.spread_cost, slippage_cost=order.slippage_cost,
                impact_cost=order.impact_cost, liquidity_capped=order.liquidity_capped,
                participation_rate=order.participation_rate)
            self.executed_orders.append((order, result))
            if signal_id:
                self._seen_signals[signal_id] = result
            logger.info("Executed %s %s qty=%.6f @ %s (order %s)",
                        signal.symbol, side.value, order.filled_quantity,
                        order.avg_fill_price, order.order_id)
            return result

        except Exception as e:
            logger.error("Error executing signal: %s", e)
            return ExecutionResult(success=False, order_id="",
                                   message=f"Execution failed: {e}",
                                   timestamp=_utcnow(), blocked_by="exception")

    def _calculate_order_quantity(self, signal, portfolio_manager,
                                  ref_price: float, equity: float) -> float:
        max_position_size = float(self.config.get('max_position_size', 1000))
        if self.risk_manager is not None and hasattr(self.risk_manager, 'calculate_position_size'):
            qty = self.risk_manager.calculate_position_size(ref_price, equity)
            return min(qty, max_position_size)
        # Fallback (no risk manager): fixed small size capped by equity.
        if ref_price <= 0 or equity <= 0:
            return 0.0
        return min(max_position_size, 10.0, equity / ref_price)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            return await self.exchange.cancel_order(symbol, order_id)
        except Exception as e:
            logger.error("Error cancelling order %s: %s", order_id, e)
            return False

    def get_execution_history(self) -> list:
        return list(self.executed_orders)
