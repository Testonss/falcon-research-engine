"""
Exchange module for the Falcon Research Engine.
Exchange abstraction with a deterministic paper-trading adapter.
Live adapter is intentionally disabled (raises on any order attempt).
"""

import asyncio
import hashlib
import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    LIMIT = "limit"
    MARKET = "market"
    STOP = "stop"


class OrderStatus(Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class Order:
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: Optional[float]  # limit price (None for market)
    status: OrderStatus
    timestamp: datetime
    filled_quantity: float = 0.0
    avg_fill_price: Optional[float] = None
    commission: float = 0.0
    client_order_id: Optional[str] = None
    reject_reason: Optional[str] = None
    # Cost-breakdown attribution (quote currency, vs reference price).
    # spread+slippage+impact legs telescope exactly to |fill-ref|*qty.
    spread_cost: float = 0.0
    slippage_cost: float = 0.0
    impact_cost: float = 0.0
    # Liquidity-aware execution diagnostics.
    liquidity_capped: bool = False  # fill clipped by participation limit
    participation_rate: float = 0.0  # requested qty / bar volume (0 when unknown)


class ExchangeInterface(ABC):
    def __init__(self, config):
        self.config = config
        self.is_connected = False

    @abstractmethod
    async def connect(self): ...
    @abstractmethod
    async def disconnect(self): ...
    @abstractmethod
    async def submit_order(self, symbol: str, side: OrderSide,
                           order_type: OrderType, quantity: float,
                           price: Optional[float] = None,
                           client_order_id: Optional[str] = None,
                           reference_price: Optional[float] = None) -> Order: ...
    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool: ...
    @abstractmethod
    async def get_order_status(self, symbol: str, order_id: str) -> OrderStatus: ...


class PaperExchange(ExchangeInterface):
    """Deterministic paper-trading exchange.

    Features:
    - idempotency via client_order_id (same id returns same order, no duplicate);
    - commissions (maker/taker), spread and slippage applied to fill price;
    - optional simulated latency, partial fills and rejections (seeded RNG
      for reproducibility);
    - market orders require a reference_price (never fill at 0.0);
    - basic rate limiting and validation (qty > 0, price > 0 for limits).
    - liquidity-aware taker execution when bar volume is provided
      (see submit_order): participation cap, liquidity-driven partial fills,
      insufficient-liquidity rejects, square-root market impact and
      range-based dynamic spread. Without bar volume the legacy fill path
      applies (fully backward compatible).
    """

    def __init__(self, config, seed: Optional[int] = None):
        super().__init__(config)
        self.orders: Dict[str, Order] = {}
        self._by_client_id: Dict[str, str] = {}  # client_order_id -> order_id
        self.next_order_id = 1
        self.maker_fee = float(config.get('maker_fee', 0.001))
        self.taker_fee = float(config.get('taker_fee', 0.002))
        self.spread_pct = float(config.get('spread_pct', 0.0005))
        self.slippage_pct = float(config.get('slippage_pct', 0.0005))
        self.latency_ms = int(config.get('simulated_latency_ms', 0) or 0)
        self.partial_fill_prob = float(config.get('partial_fill_probability', 0.0) or 0.0)
        self.reject_prob = float(config.get('reject_probability', 0.0) or 0.0)
        # Liquidity-aware execution knobs (neutral defaults = legacy numbers).
        self.max_participation_rate = float(config.get('max_participation_rate', 0.10))
        if not (0.0 < self.max_participation_rate <= 1.0):
            self.max_participation_rate = min(max(self.max_participation_rate, 1e-6), 1.0)
        self.min_fill_notional = float(config.get('min_fill_notional', 10.0) or 0.0)
        self.impact_coeff = float(config.get('market_impact_coeff', 0.0) or 0.0)
        self.dyn_spread_coeff = float(config.get('dynamic_spread_coeff', 0.0) or 0.0)
        seed = seed if seed is not None else config.get('random_seed', 42)
        self._rng = random.Random(seed)
        # Simple token-bucket rate limiter
        self._rate_window = 60.0
        self._rate_max = 600
        self._rate_hits: List[float] = []
        # Reference prices for simulation (set by engine/backtest)
        self.reference_prices: Dict[str, float] = {}

    def set_reference_price(self, symbol: str, price: float):
        if price and price > 0:
            self.reference_prices[symbol] = float(price)

    def _check_rate_limit(self) -> bool:
        now = time.time()
        self._rate_hits = [t for t in self._rate_hits if now - t < self._rate_window]
        if len(self._rate_hits) >= self._rate_max:
            return False
        self._rate_hits.append(now)
        return True

    async def connect(self):
        logger.debug("Connecting to paper exchange...")
        self.is_connected = True
        logger.debug("Paper exchange connected")

    async def disconnect(self):
        logger.debug("Disconnecting from paper exchange...")
        self.is_connected = False
        logger.debug("Paper exchange disconnected")

    def _apply_costs(self, side: OrderSide, order_type: OrderType, ref: float) -> float:
        """Apply spread + slippage to a reference price (deterministic direction).

        Legacy path (no bar volume): arithmetic is frozen for backward
        compatibility. Prefer _decompose_legacy_costs for cost attribution.
        """
        px, _, _ = self._decompose_legacy_costs(side, ref, 0.0)
        return px

    def _decompose_legacy_costs(self, side: OrderSide, ref: float,
                                qty: float) -> tuple:
        """Legacy fill decomposition. Returns (fill_px, spread_cost, slip_cost).

        Arithmetic identical to the historical _apply_costs (same RNG draws);
        fill numbers unchanged, costs attributed vs ref for reporting.
        """
        px = ref
        # Spread: buys lift the ask, sells hit the bid.
        if side == OrderSide.BUY:
            px = px * (1.0 + self.spread_pct / 2.0)
        else:
            px = px * (1.0 - self.spread_pct / 2.0)
        spread_cost = abs(px - ref) * qty
        # Slippage: adverse move, seeded jitter for realism.
        slip = self.slippage_pct
        if slip:
            jitter = (self._rng.random() - 0.5) * slip  # [-slip/2, +slip/2]
            adverse = slip / 2.0
            if side == OrderSide.BUY:
                px = px * (1.0 + adverse + jitter)
            else:
                px = px * (1.0 - adverse + jitter * -1.0)
        slip_cost = abs(px - ref) * qty - spread_cost
        # Guard microscopic negative rounding (telescoping legs are monotonic).
        slip_cost = max(slip_cost, 0.0)
        return max(px, 1e-9), spread_cost, slip_cost

    def _apply_liquidity_costs(self, side: OrderSide, ref: float, qty: float,
                               bar_volume: float, bar_high: Optional[float] = None,
                               bar_low: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Liquidity-aware taker fill. Returns fill dict or None (insufficient).

        Model (all legs adverse, telescoping exactly to |fill-ref|*fill_qty):
        - participation = requested qty / bar volume (base units);
        - fill capped at bar_volume * max_participation_rate (partial fill);
        - fills below min_fill_notional are not executed (None);
        - effective spread = base + dyn_coeff * bar_range/ref (0 when no OHLC);
        - slippage = base adverse + same seeded jitter as legacy;
        - market impact = impact_coeff * sqrt(min(participation, 1)).
        Fully deterministic given (seed, inputs).
        """
        participation = qty / bar_volume
        cap_qty = bar_volume * self.max_participation_rate
        fill_qty = min(qty, cap_qty)
        capped = fill_qty < qty - 1e-12
        if fill_qty <= 0 or fill_qty * ref < self.min_fill_notional:
            return None
        range_pct = 0.0
        if bar_high and bar_low and ref > 0 and bar_high >= bar_low:
            range_pct = max(0.0, (bar_high - bar_low) / ref)
        spread_eff = self.spread_pct + self.dyn_spread_coeff * range_pct
        px = ref
        if side == OrderSide.BUY:
            px = px * (1.0 + spread_eff / 2.0)
        else:
            px = px * (1.0 - spread_eff / 2.0)
        spread_cost = abs(px - ref) * fill_qty
        slip = self.slippage_pct
        if slip:
            jitter = (self._rng.random() - 0.5) * slip
            adverse = slip / 2.0
            if side == OrderSide.BUY:
                px = px * (1.0 + adverse + jitter)
            else:
                px = px * (1.0 - adverse + jitter * -1.0)
        slip_cost = max(abs(px - ref) * fill_qty - spread_cost, 0.0)
        impact = self.impact_coeff * (min(participation, 1.0) ** 0.5)
        if impact:
            if side == OrderSide.BUY:
                px = px * (1.0 + impact)
            else:
                px = px * (1.0 - impact)
        impact_cost = max(abs(px - ref) * fill_qty - spread_cost - slip_cost, 0.0)
        return {
            'fill_qty': fill_qty,
            'fill_px': max(px, 1e-9),
            'spread_cost': spread_cost,
            'slippage_cost': slip_cost,
            'impact_cost': impact_cost,
            'participation': participation,
            'capped': capped,
        }

    async def submit_order(self, symbol: str, side: OrderSide,
                           order_type: OrderType, quantity: float,
                           price: Optional[float] = None,
                           client_order_id: Optional[str] = None,
                           reference_price: Optional[float] = None,
                           bar_volume: Optional[float] = None,
                           bar_high: Optional[float] = None,
                           bar_low: Optional[float] = None) -> Order:
        """Submit an order.

        Optional bar context (base-unit volume + high/low) enables the
        liquidity-aware taker model for MARKET orders: participation cap,
        liquidity partial fills, insufficient-liquidity rejects, sqrt market
        impact and range-based dynamic spread. LIMIT/STOP flow is unchanged.
        """
        if not self.is_connected:
            raise RuntimeError("Not connected to paper exchange")
        # Idempotency: same client_order_id returns the original order.
        if client_order_id and client_order_id in self._by_client_id:
            existing_id = self._by_client_id[client_order_id]
            logger.debug("Idempotent replay for client_order_id=%s -> %s", client_order_id, existing_id)
            return self.orders[existing_id]
        if not self._check_rate_limit():
            return self._store_rejected(symbol, side, order_type, quantity, price,
                                        client_order_id, "rate_limit")
        # Validation
        if quantity is None or quantity <= 0:
            return self._store_rejected(symbol, side, order_type, quantity or 0, price,
                                        client_order_id, "invalid_quantity")
        if order_type in (OrderType.LIMIT, OrderType.STOP) and (price is None or price <= 0):
            return self._store_rejected(symbol, side, order_type, quantity, price,
                                        client_order_id, "invalid_price")
        # Simulated latency (capped, skippable in tests via 0)
        if self.latency_ms > 0:
            await asyncio.sleep(min(self.latency_ms / 1000.0, 0.25))
        # Random rejection (seeded; default 0)
        if self.reject_prob > 0 and self._rng.random() < self.reject_prob:
            return self._store_rejected(symbol, side, order_type, quantity, price,
                                        client_order_id, "simulated_reject")

        ref = reference_price if reference_price and reference_price > 0 else self.reference_prices.get(symbol)
        if order_type == OrderType.MARKET and (ref is None or ref <= 0):
            return self._store_rejected(symbol, side, order_type, quantity, price,
                                        client_order_id, "missing_reference_price")

        order_id = f"paper_{self.next_order_id}"
        self.next_order_id += 1
        order = Order(
            order_id=order_id, symbol=symbol, side=side, order_type=order_type,
            quantity=float(quantity), price=price, status=OrderStatus.PENDING,
            timestamp=_utcnow(), client_order_id=client_order_id,
        )
        if order_type == OrderType.MARKET:
            assert ref is not None and ref > 0
            fee_rate = self.taker_fee
            use_liquidity = bar_volume is not None and bar_volume > 0
            if use_liquidity:
                liq = self._apply_liquidity_costs(
                    side, float(ref), float(quantity),
                    float(bar_volume), bar_high, bar_low)
                if liq is None:
                    return self._store_rejected(symbol, side, order_type, quantity, price,
                                                client_order_id, "insufficient_liquidity",
                                                participation=float(quantity) / float(bar_volume))
                fill_px = liq['fill_px']
                order.spread_cost = liq['spread_cost']
                order.slippage_cost = liq['slippage_cost']
                order.impact_cost = liq['impact_cost']
                order.participation_rate = liq['participation']
                order.liquidity_capped = liq['capped']
                if liq['capped']:
                    order.status = OrderStatus.PARTIAL
                    order.filled_quantity = liq['fill_qty']
                    order.avg_fill_price = fill_px
                    order.commission = liq['fill_qty'] * fill_px * fee_rate
                elif self.partial_fill_prob > 0 and self._rng.random() < self.partial_fill_prob:
                    fill_qty = round(liq['fill_qty'] * self._rng.uniform(0.3, 0.9), 8)
                    order.status = OrderStatus.PARTIAL
                    order.filled_quantity = fill_qty
                    order.avg_fill_price = fill_px
                    order.commission = fill_qty * fill_px * fee_rate
                else:
                    order.status = OrderStatus.FILLED
                    order.filled_quantity = liq['fill_qty']
                    order.avg_fill_price = fill_px
                    order.commission = liq['fill_qty'] * fill_px * fee_rate
            else:
                fill_px, spread_cost, slip_cost = self._decompose_legacy_costs(
                    side, float(ref), float(quantity))
                fee_rate = self.taker_fee
                order.spread_cost = spread_cost
                order.slippage_cost = slip_cost
                order.impact_cost = 0.0
                # Partial fill simulation
                if self.partial_fill_prob > 0 and self._rng.random() < self.partial_fill_prob:
                    fill_qty = round(quantity * self._rng.uniform(0.3, 0.9), 8)
                    order.status = OrderStatus.PARTIAL
                    order.filled_quantity = fill_qty
                    order.avg_fill_price = fill_px
                    order.commission = fill_qty * fill_px * fee_rate
                else:
                    order.status = OrderStatus.FILLED
                    order.filled_quantity = float(quantity)
                    order.avg_fill_price = fill_px
                    order.commission = float(quantity) * fill_px * fee_rate
        elif order_type == OrderType.LIMIT and price is not None:
            # Limit: maker fee; simulate immediate fill only if marketable,
            # else leave OPEN (backtest/engine can match later).
            assert ref is not None or True
            mkt = ref if ref and ref > 0 else price
            marketable = (side == OrderSide.BUY and price >= mkt) or \
                         (side == OrderSide.SELL and price <= mkt)
            fee_rate = self.maker_fee if not marketable else self.taker_fee
            if marketable:
                fill_px, spread_cost, slip_cost = self._decompose_legacy_costs(
                    side, float(mkt), float(quantity))
                # Respect limit price (no worse fill)
                if side == OrderSide.BUY:
                    fill_px = min(fill_px, price)
                else:
                    fill_px = max(fill_px, price)
                order.status = OrderStatus.FILLED
                order.filled_quantity = float(quantity)
                order.avg_fill_price = fill_px
                order.commission = float(quantity) * fill_px * fee_rate
                order.spread_cost = spread_cost
                order.slippage_cost = slip_cost
                order.impact_cost = 0.0
            else:
                order.status = OrderStatus.OPEN
        else:
            order.status = OrderStatus.OPEN

        self.orders[order_id] = order
        if client_order_id:
            self._by_client_id[client_order_id] = order_id
        logger.debug("Paper order submitted: %s", order)
        return order

    def _store_rejected(self, symbol, side, order_type, quantity, price,
                        client_order_id, reason: str,
                        participation: float = 0.0) -> Order:
        order_id = f"paper_{self.next_order_id}"
        self.next_order_id += 1
        order = Order(
            order_id=order_id, symbol=symbol, side=side, order_type=order_type,
            quantity=float(quantity or 0), price=price, status=OrderStatus.REJECTED,
            timestamp=_utcnow(), client_order_id=client_order_id, reject_reason=reason,
            participation_rate=float(participation or 0.0),
        )
        self.orders[order_id] = order
        if client_order_id and client_order_id not in self._by_client_id:
            self._by_client_id[client_order_id] = order_id
        logger.debug("Paper order rejected (%s): %s", reason, order_id)
        return order

    def match_open_orders(self, symbol: str, market_price: float):
        """Try to fill OPEN limit orders against a new market price (backtest)."""
        for order in self.orders.values():
            if order.symbol != symbol or order.status != OrderStatus.OPEN:
                continue
            if order.price is None:
                continue
            fill = False
            if order.side == OrderSide.BUY and market_price <= order.price:
                fill = True
            elif order.side == OrderSide.SELL and market_price >= order.price:
                fill = True
            if fill:
                order.status = OrderStatus.FILLED
                order.filled_quantity = order.quantity
                order.avg_fill_price = order.price
                order.commission = order.quantity * order.price * self.maker_fee

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        if not self.is_connected:
            raise RuntimeError("Not connected to paper exchange")
        order = self.orders.get(order_id)
        if order is None:
            logger.warning("Order %s not found", order_id)
            return False
        if order.status in (OrderStatus.OPEN, OrderStatus.PARTIAL, OrderStatus.PENDING):
            order.status = OrderStatus.CANCELLED
            logger.debug("Cancelled paper order: %s", order_id)
            return True
        logger.warning("Cannot cancel order %s - status is %s", order_id, order.status)
        return False

    async def get_order_status(self, symbol: str, order_id: str) -> OrderStatus:
        order = self.orders.get(order_id)
        if order is not None:
            return order.status
        return OrderStatus.REJECTED

    def get_order(self, order_id: str) -> Optional[Order]:
        return self.orders.get(order_id)


class LiveExchange(ExchangeInterface):
    """Live exchange adapter — DISABLED by default.

    Even connect() refuses unless ConfigManager.is_live_trading_enabled()
    returns True (adapter=live + explicit flag + confirm token). This guarantees
    no real orders can be sent from paper/sandbox sessions.
    """

    def __init__(self, config):
        super().__init__(config)

    def _guard(self):
        is_live = False
        try:
            is_live = bool(self.config.is_live_trading_enabled())
        except AttributeError:
            is_live = False
        if not is_live:
            raise RuntimeError(
                "Live trading is DISABLED. Set EXCHANGE_ADAPTER=live, "
                "LIVE_TRADING_ENABLED=true and LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_RISK "
                "to enable (not recommended)."
            )

    async def connect(self):
        self._guard()
        logger.warning("Connecting to LIVE exchange (real funds at risk)")
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def submit_order(self, symbol: str, side: OrderSide,
                           order_type: OrderType, quantity: float,
                           price: Optional[float] = None,
                           client_order_id: Optional[str] = None,
                           reference_price: Optional[float] = None) -> Order:
        self._guard()
        raise NotImplementedError("Live exchange not implemented — no real orders in this milestone")

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        self._guard()
        raise NotImplementedError("Live exchange not implemented")

    async def get_order_status(self, symbol: str, order_id: str) -> OrderStatus:
        self._guard()
        raise NotImplementedError("Live exchange not implemented")
