"""
Risk management module for the Falcon Research Engine.
The RiskManager has FINAL authority over every trade: execution must call it.
"""

import logging
from typing import Optional, Dict, List, Tuple
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RiskParameters:
    max_position_size: float = 1000.0
    risk_per_trade: float = 10.0
    risk_per_trade_pct: float = 0.01
    stop_loss_pct: float = 0.01
    take_profit_pct: float = 0.01
    max_daily_loss: float = 500.0
    max_consecutive_losses: int = 3
    stop_trading_cooldown_minutes: int = 30
    kill_switch_enabled: bool = False
    max_drawdown_percentage: float = 0.10


@dataclass
class RiskMetrics:
    daily_loss: float = 0.0
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    last_trade_time: Optional[datetime] = None
    is_in_cooldown: bool = False
    cooldown_end_time: Optional[datetime] = None
    current_drawdown: float = 0.0
    peak_equity: float = 0.0
    kill_switch_tripped: bool = False
    current_day: Optional[str] = None


class RiskManager:
    """Enforces all risk limits. Pure checks never mutate state."""

    def __init__(self, config):
        self.config = config
        self.risk_params = RiskParameters(
            max_position_size=float(config.get('max_position_size', 1000.0)),
            risk_per_trade=float(config.get('risk_per_trade', 10.0)),
            risk_per_trade_pct=float(config.get('risk_per_trade_pct', 0.01)),
            stop_loss_pct=float(config.get('stop_loss_pct', 0.01)),
            take_profit_pct=float(config.get('take_profit_pct', 0.01)),
            max_daily_loss=float(config.get('max_daily_loss', 500.0)),
            max_consecutive_losses=int(config.get('max_consecutive_losses', 3)),
            stop_trading_cooldown_minutes=int(config.get('stop_trading_cooldown_minutes', 30)),
            kill_switch_enabled=bool(config.get('kill_switch_enabled', False)),
            max_drawdown_percentage=float(config.get('max_drawdown_percentage', 0.10)),
        )
        self.metrics = RiskMetrics()
        self.trade_history: List[Dict] = []

    # -- internal helpers ------------------------------------------------------
    def _today_str(self, now: datetime) -> str:
        return now.date().isoformat()

    def _maybe_roll_day(self, now: datetime):
        today = self._today_str(now)
        if self.metrics.current_day is None:
            self.metrics.current_day = today
        elif self.metrics.current_day != today:
            self.metrics.daily_loss = 0.0
            self.metrics.current_day = today
            logger.debug("New trading day — daily loss reset")

    def _refresh_cooldown(self, now: datetime):
        if self.metrics.is_in_cooldown and self.metrics.cooldown_end_time and now >= self.metrics.cooldown_end_time:
            self.metrics.is_in_cooldown = False
            self.metrics.cooldown_end_time = None
            logger.debug("Cooldown period ended — trading resumed")

    # -- pure checks (no mutation) ----------------------------------------------
    def check_kill_switch(self) -> bool:
        if self.risk_params.kill_switch_enabled or self.metrics.kill_switch_tripped:
            logger.debug("Kill switch enabled — no trading allowed")
            return False
        return True

    def check_cooldown(self, now: Optional[datetime] = None) -> bool:
        now = now or _utcnow()
        # Evaluate without mutating (mutation happens in evaluate/record paths)
        if self.metrics.is_in_cooldown:
            if self.metrics.cooldown_end_time and now < self.metrics.cooldown_end_time:
                return False
        return True

    def check_position_limits(self, symbol: str, quantity: float) -> bool:
        if quantity is None or quantity <= 0:
            return False
        if quantity > self.risk_params.max_position_size:
            logger.debug("Order exceeds max position size for %s: %s > %s",
                           symbol, quantity, self.risk_params.max_position_size)
            return False
        return True

    def check_daily_loss_limit(self, additional_loss: float = 0.0,
                               now: Optional[datetime] = None) -> bool:
        """Pure check: would `daily_loss + additional_loss` breach the limit?"""
        now = now or _utcnow()
        # Do not mutate here; day-roll is handled in record/evaluate.
        daily = self.metrics.daily_loss
        # If day rolled over, effective daily is 0
        if self.metrics.current_day is not None and self.metrics.current_day != self._today_str(now):
            daily = 0.0
        if daily + max(0.0, additional_loss) > self.risk_params.max_daily_loss:
            logger.debug("Daily loss limit would be exceeded: %s + %s > %s",
                           daily, additional_loss, self.risk_params.max_daily_loss)
            return False
        return True

    def check_consecutive_losses(self) -> bool:
        if self.metrics.consecutive_losses >= self.risk_params.max_consecutive_losses:
            logger.debug("Consecutive loss limit reached: %s", self.metrics.consecutive_losses)
            return False
        return True

    def check_drawdown(self, equity: Optional[float] = None) -> bool:
        if equity is None:
            equity = self.metrics.peak_equity
        if self.metrics.peak_equity and self.metrics.peak_equity > 0 and equity is not None:
            dd = (self.metrics.peak_equity - equity) / self.metrics.peak_equity
            if dd > self.risk_params.max_drawdown_percentage:
                logger.debug("Max drawdown exceeded: %.4f > %.4f", dd, self.risk_params.max_drawdown_percentage)
                return False
        return True

    def check_stop_take(self, entry_price: float, current_price: float, side: str) -> Tuple[bool, str]:
        """Returns (should_close, reason). Pure helper for SL/TP evaluation."""
        if entry_price <= 0 or current_price <= 0:
            return False, ""
        sl = self.risk_params.stop_loss_pct
        tp = self.risk_params.take_profit_pct
        if side == 'long':
            if current_price <= entry_price * (1 - sl):
                return True, "stop_loss"
            if current_price >= entry_price * (1 + tp):
                return True, "take_profit"
        elif side == 'short':
            if current_price >= entry_price * (1 + sl):
                return True, "stop_loss"
            if current_price <= entry_price * (1 - tp):
                return True, "take_profit"
        return False, ""

    # -- sizing ------------------------------------------------------------------
    def calculate_position_size(self, price: float, equity: float,
                                stop_distance_pct: Optional[float] = None) -> float:
        """Position size (in base units) limited by notional, risk% and max size."""
        if price is None or price <= 0 or equity is None or equity <= 0:
            return 0.0
        stop_pct = stop_distance_pct if stop_distance_pct and stop_distance_pct > 0 else self.risk_params.stop_loss_pct
        risk_budget = min(self.risk_params.risk_per_trade, equity * self.risk_params.risk_per_trade_pct)
        if risk_budget <= 0 or stop_pct <= 0:
            return 0.0
        # qty such that stop-out loss ~= risk_budget: qty*price*stop_pct = risk_budget
        qty_by_risk = risk_budget / (price * stop_pct)
        qty = min(qty_by_risk, self.risk_params.max_position_size)
        # Also cap by available equity (no leverage in spot paper mode)
        qty = min(qty, equity / price)
        return max(0.0, float(qty))

    # -- unified gate --------------------------------------------------------------
    def evaluate_trade(self, symbol: str, quantity: float, price: float,
                       equity: float, now: Optional[datetime] = None) -> Tuple[bool, str]:
        """Single entry point used by the execution engine.

        Returns (allowed, reason). Has final authority; never raises.
        """
        now = now or _utcnow()
        try:
            self._maybe_roll_day(now)
            self._refresh_cooldown(now)
            if not self.check_kill_switch():
                return False, "kill_switch"
            if not self.check_cooldown(now):
                return False, "cooldown"
            if not self.check_consecutive_losses():
                return False, "consecutive_losses"
            if not self.check_position_limits(symbol, quantity):
                return False, "position_size"
            if not self.check_daily_loss_limit(0.0, now):
                return False, "daily_loss"
            if not self.check_drawdown(equity):
                return False, "max_drawdown"
            # Affordability: notional must not exceed equity (spot, no leverage)
            if price and price > 0 and quantity * price > equity:
                return False, "insufficient_equity"
            return True, "ok"
        except Exception as e:  # pragma: no cover - defensive
            logger.error("Risk evaluation failed closed: %s", e)
            return False, "error"

    # Backwards-compatible aliases used by older call sites / tests
    def check_trade_allowance(self, is_loss: bool = False) -> bool:
        if not self.check_kill_switch():
            return False
        if not self.check_cooldown():
            return False
        if not self.check_consecutive_losses():
            return False
        return True

    def apply_trade_limits(self, symbol: str, quantity: float, price: float,
                           equity: Optional[float] = None) -> bool:
        if equity is None:
            equity = quantity * price if price else 0.0
        allowed, _ = self.evaluate_trade(symbol, quantity, price, equity)
        return allowed

    # -- recording (mutating) -------------------------------------------------------
    def record_trade_result(self, success: bool, pnl: float = 0.0,
                            equity: Optional[float] = None,
                            now: Optional[datetime] = None):
        """Record a closed-trade outcome.

        Classification is PnL-based with epsilon (authoritative over `success` flag):
        - pnl > eps        -> win (resets consecutive losses)
        - pnl < -eps       -> loss (daily_loss += |pnl|, consecutive_losses += 1)
        - otherwise        -> breakeven: counts toward total_trades but neither
                              win nor loss; consecutive counters untouched.
        """
        EPS = 1e-9
        now = now or _utcnow()
        self._maybe_roll_day(now)
        self.metrics.total_trades += 1
        self.metrics.last_trade_time = now
        if equity is not None and equity > 0:
            if self.metrics.peak_equity <= 0:
                self.metrics.peak_equity = equity
            else:
                self.metrics.peak_equity = max(self.metrics.peak_equity, equity)
            if self.metrics.peak_equity > 0:
                self.metrics.current_drawdown = max(
                    0.0, (self.metrics.peak_equity - equity) / self.metrics.peak_equity)

        pnl = float(pnl or 0.0)
        entry = {'success': bool(success), 'pnl': pnl, 'timestamp': now, 'equity': equity}
        self.trade_history.append(entry)

        if pnl > EPS:
            self.metrics.consecutive_losses = 0
            self.metrics.consecutive_wins += 1
            self.metrics.winning_trades += 1
            logger.debug("Trade win pnl=%.4f — consecutive losses reset", pnl)
        elif pnl < -EPS:
            loss = abs(pnl)
            self.metrics.daily_loss += loss
            self.metrics.consecutive_losses += 1
            self.metrics.consecutive_wins = 0
            self.metrics.losing_trades += 1
            if self.metrics.consecutive_losses >= self.risk_params.max_consecutive_losses:
                self.metrics.is_in_cooldown = True
                self.metrics.cooldown_end_time = now + timedelta(
                    minutes=self.risk_params.stop_trading_cooldown_minutes)
                logger.info("Consecutive-loss limit hit — cooldown %s min",
                            self.risk_params.stop_trading_cooldown_minutes)
            if self.metrics.daily_loss >= self.risk_params.max_daily_loss:
                logger.warning("Daily loss limit reached — blocking further trades today")
            if equity is not None and not self.check_drawdown(equity):
                self.trip_kill_switch("max_drawdown")
        else:
            # Breakeven: neither win nor loss.
            logger.debug("Trade breakeven — counters untouched")

    def execute_loss_action(self, loss_amount: float, is_consecutive_loss: bool = False,
                            now: Optional[datetime] = None):
        """Legacy entry: records a losing trade of `loss_amount`."""
        self.record_trade_result(False, pnl=-abs(loss_amount), now=now)

    def trip_kill_switch(self, reason: str = ""):
        self.metrics.kill_switch_tripped = True
        logger.warning("KILL SWITCH TRIPPED (%s) — all trading halted", reason)

    def reset_kill_switch(self):
        self.metrics.kill_switch_tripped = False
        self.risk_params.kill_switch_enabled = False

    def reset_daily_limits(self, now: Optional[datetime] = None):
        self.metrics.daily_loss = 0.0
        self.metrics.current_day = self._today_str(now or _utcnow())

    def set_kill_switch(self, enabled: bool):
        self.risk_params.kill_switch_enabled = bool(enabled)

    def update_peak(self, equity: float):
        if equity and equity > self.metrics.peak_equity:
            self.metrics.peak_equity = equity

    def get_risk_metrics(self) -> RiskMetrics:
        return self.metrics

    def get_remaining_daily_balance(self) -> float:
        return max(0.0, self.risk_params.max_daily_loss - self.metrics.daily_loss)
