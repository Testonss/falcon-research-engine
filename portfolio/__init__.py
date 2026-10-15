"""
Portfolio management module for the Falcon Research Engine.
Cash tracking, position management and PnL (fees included, idempotent updates).
"""

import logging
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Side(Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class Position:
    symbol: str
    quantity: float  # signed: +long, -short
    entry_price: float
    last_update: datetime
    unrealized_pnl: float = 0.0
    side: str = "long"


@dataclass
class PortfolioInfo:
    cash: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)
    total_value: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    equity: float = 0.0


class PortfolioManager:
    """Spot + short-capable paper portfolio with idempotent trade application."""

    def __init__(self, config):
        self.config = config
        self.cash = float(config.get('initial_cash', 10000.0))
        self.positions: Dict[str, Position] = {}
        self.realized_pnl = 0.0
        self.total_fees_paid = 0.0
        self.trades_history: List[Dict] = []
        self._applied_order_ids: set = set()

    # -- accessors -----------------------------------------------------------------
    def get_cash(self) -> float:
        return self.cash

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.positions.get(symbol)

    def get_positions(self) -> Dict[str, Position]:
        return dict(self.positions)

    # -- core update -----------------------------------------------------------------
    def update_position(self, symbol: str, quantity: float, price: float,
                        is_buy: bool = True, order_id: str = "", fee: float = 0.0) -> bool:
        """Apply a fill idempotently. Returns False if duplicate (ignored).

        Entry prices are fee-loaded (fee amortized per unit), so realized PnL
        on close includes BOTH entry and exit commissions and the invariant
        cash + positions == initial + realized + unrealized holds exactly.
        With fee=0 behaviour is identical to plain-price accounting.
        """
        if order_id and order_id in self._applied_order_ids:
            logger.debug("Duplicate portfolio update ignored for order %s", order_id)
            return False
        if quantity is None or quantity <= 0 or price is None or price <= 0:
            logger.warning("Invalid fill ignored: qty=%s price=%s", quantity, price)
            return False
        fee = max(0.0, float(fee or 0.0))
        delta = float(quantity) if is_buy else -float(quantity)

        pos = self.positions.get(symbol)
        if pos is None:
            # Opening a new long (buy) or short (sell with no position).
            if is_buy:
                cost = quantity * price + fee
                self.cash -= cost
                entry = (quantity * price + fee) / quantity
            else:
                proceeds = quantity * price - fee
                self.cash += proceeds
                entry = (quantity * price - fee) / quantity
            self.positions[symbol] = Position(
                symbol=symbol, quantity=delta, entry_price=float(entry),
                last_update=_utcnow(), side="long" if is_buy else "short")
        else:
            # Adding to same-direction position -> re-average (fee-loaded).
            same_dir = (pos.quantity > 0 and delta > 0) or (pos.quantity < 0 and delta < 0)
            if same_dir:
                total_qty = abs(pos.quantity) + quantity
                if is_buy:
                    blended = (abs(pos.quantity) * pos.entry_price
                               + quantity * price + fee) / total_qty
                else:
                    blended = (abs(pos.quantity) * pos.entry_price
                               + quantity * price - fee) / total_qty
                pos.entry_price = blended
                pos.quantity += delta
                pos.last_update = _utcnow()
                if is_buy:
                    self.cash -= quantity * price + fee
                else:
                    self.cash += quantity * price - fee
            else:
                # Reducing / flipping: realize PnL on the closed portion.
                # Entry leg already carries its fee share via entry_price;
                # only the exit fee share is deducted here.
                close_qty = min(abs(pos.quantity), quantity)
                if pos.quantity > 0:  # long closed by sell
                    pnl = (price - pos.entry_price) * close_qty - fee * (close_qty / quantity)
                    self.cash += close_qty * price - fee * (close_qty / quantity)
                else:  # short closed by buy
                    pnl = (pos.entry_price - price) * close_qty - fee * (close_qty / quantity)
                    self.cash -= close_qty * price + fee * (close_qty / quantity)
                self.realized_pnl += pnl
                logger.debug("Partial close %s: qty=%.8f pnl=%.4f", symbol, close_qty, pnl)
                remaining = abs(pos.quantity) - close_qty
                leftover = quantity - close_qty
                if remaining <= 1e-12:
                    del self.positions[symbol]
                    if leftover > 1e-12:
                        # Flip to opposite side with remainder (fee-loaded).
                        leftover_fee = fee * (leftover / quantity)
                        if pos.quantity > 0:  # was long, leftover sell -> short
                            self.cash += leftover * price - leftover_fee
                            entry = (leftover * price - leftover_fee) / leftover
                            self.positions[symbol] = Position(
                                symbol=symbol, quantity=-leftover, entry_price=float(entry),
                                last_update=_utcnow(), side="short")
                        else:
                            self.cash -= leftover * price + leftover_fee
                            entry = (leftover * price + leftover_fee) / leftover
                            self.positions[symbol] = Position(
                                symbol=symbol, quantity=leftover, entry_price=float(entry),
                                last_update=_utcnow(), side="long")
                else:
                    pos.quantity += delta
                    pos.last_update = _utcnow()

        self.total_fees_paid += fee
        if order_id:
            self._applied_order_ids.add(order_id)
        self.trades_history.append({
            'symbol': symbol, 'quantity': quantity, 'price': price,
            'is_buy': is_buy, 'timestamp': _utcnow(), 'order_id': order_id,
            'fee': fee, 'remaining_cash': self.cash,
        })
        logger.debug("Portfolio updated for %s: cash=%.2f positions=%d", symbol, self.cash, len(self.positions))
        return True

    # -- valuation ----------------------------------------------------------------------
    def get_portfolio_value(self, current_prices: Optional[Dict[str, float]] = None) -> float:
        """Cash + market value of positions (shorts subtract)."""
        value = self.cash
        prices = current_prices or {}
        for symbol, pos in self.positions.items():
            px = prices.get(symbol, pos.entry_price)
            value += pos.quantity * px
        return value

    def calculate_pnl(self, current_prices: Dict[str, float]) -> PortfolioInfo:
        total_value = self.cash
        unrealized = 0.0
        for symbol, pos in self.positions.items():
            px = current_prices.get(symbol, pos.entry_price)
            if pos.quantity > 0:
                pnl = (px - pos.entry_price) * pos.quantity
            else:
                pnl = (pos.entry_price - px) * abs(pos.quantity)
            pos.unrealized_pnl = pnl
            unrealized += pnl
            total_value += pos.quantity * px
        return PortfolioInfo(
            cash=self.cash, positions=dict(self.positions),
            total_value=total_value, realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized, equity=total_value)

    def get_equity(self, current_prices: Optional[Dict[str, float]] = None) -> float:
        return self.get_portfolio_value(current_prices)

    def get_portfolio_summary(self) -> str:
        total_value = self.get_portfolio_value()
        positions_str = ", ".join(f"{s}:{p.quantity:.4f}@{p.entry_price:.2f}" for s, p in self.positions.items())
        return (f"Cash: {self.cash:.2f}, Positions: [{positions_str}], "
                f"Equity: {total_value:.2f}, RealizedPnL: {self.realized_pnl:.2f}, Fees: {self.total_fees_paid:.2f}")

    def get_trade_history(self) -> List[Dict]:
        return list(self.trades_history)

    def reset_portfolio(self):
        self.cash = float(self.config.get('initial_cash', 10000.0))
        self.positions.clear()
        self.realized_pnl = 0.0
        self.total_fees_paid = 0.0
        self.trades_history.clear()
        self._applied_order_ids.clear()

    def set_cash(self, amount: float):
        self.cash = float(amount)

    def set_position(self, symbol: str, quantity: float, entry_price: float):
        self.positions[symbol] = Position(
            symbol=symbol, quantity=float(quantity), entry_price=float(entry_price),
            last_update=_utcnow(), side="long" if quantity >= 0 else "short")
