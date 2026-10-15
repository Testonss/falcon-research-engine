"""
Backtesting engine for the Falcon Research Engine.
Deterministic historical replay sharing the paper-trading flow:
market data -> strategy -> risk -> execution -> portfolio.

No look-ahead: at execution bar `i`, the strategy only sees bars [0..i-1].
Fill price is configurable via `execution_model`:
- 'next_close': fill at bar i close (default, preserves legacy behavior);
- 'next_open':  fill at bar i open (more conservative when open != close).
"""

import asyncio
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone

from strategy import SignalType
from market_data import Candle

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BacktestResult:
    trades: int = 0            # all successful fills (strategy + SL/TP)
    strategy_trades: int = 0   # fills from strategy signals only
    sltp_trades: int = 0       # fills from SL/TP enforcement
    wins: int = 0              # closed trades with pnl > eps
    losses: int = 0            # closed trades with pnl < -eps
    breakevens: int = 0        # closed trades within eps of zero
    realized_pnl: float = 0.0  # net of fees (open positions excluded)
    fees_paid: float = 0.0
    final_equity: float = 0.0  # cash + open positions at last close
    max_drawdown: float = 0.0
    win_rate: float = 0.0      # wins / (wins + losses), 0 when no closes
    equity_curve: List[float] = field(default_factory=list)
    blocked_by_risk: int = 0
    rejected_orders: int = 0
    # Quantitative-evaluation extras (raw series for the evaluation module)
    execution_model: str = 'next_close'
    trade_pnls: List[float] = field(default_factory=list)  # per-close net PnL
    trade_exit_stamps: List[str] = field(default_factory=list)  # exec-bar ISO ts per close
    equity_stamps: List[str] = field(default_factory=list)  # exec-bar ISO ts per equity point
    turnover: float = 0.0       # sum(|fill qty * fill price|)
    turnover_long: float = 0.0  # buy-fill notional
    turnover_short: float = 0.0  # sell-fill notional
    exposures: List[float] = field(default_factory=list)  # per-bar |pos|/equity
    n_bars: int = 0
    warmup_bars: int = 0        # context bars used for indicators, never scored
    # Cost-breakdown attribution (quote currency; telescope to |fill-ref|*qty).
    spread_paid: float = 0.0
    slippage_paid: float = 0.0
    impact_paid: float = 0.0
    gross_pnl: float = 0.0      # realized + fees + spread + slippage + impact
    # Liquidity diagnostics.
    max_participation: float = 0.0
    participation_sum: float = 0.0
    participation_n: int = 0
    liquidity_capped_fills: int = 0
    insufficient_liquidity_rejects: int = 0


class BacktestEngine:
    """Replays candles deterministically (no look-ahead, no randomness beyond seed)."""

    def __init__(self, config, market_data_handler, execution_engine,
                 portfolio_manager, risk_manager=None, strategy_manager=None):
        self.config = config
        self.market_data = market_data_handler
        self.execution_engine = execution_engine
        self.portfolio_manager = portfolio_manager
        self.risk_manager = risk_manager
        if risk_manager is not None and getattr(execution_engine, 'risk_manager', None) is None:
            execution_engine.set_risk_manager(risk_manager)
        if strategy_manager is None:
            from strategy import StrategyManager
            strategy_manager = StrategyManager(config)
        self.strategy_manager = strategy_manager
        self._seen_candle_ids: set = set()
        self.duplicates_skipped = 0

    def _execution_price(self, bar: Candle, model: str) -> Optional[float]:
        if model == 'next_open':
            px = bar.open
        else:
            px = bar.close
        if px is None or px <= 0:
            return None
        return float(px)

    def _dedup_candles(self, candles: List[Candle]) -> List[Candle]:
        """Drop duplicates (event_id) and sort by timestamp. Missing data is skipped."""
        unique: Dict[str, Candle] = {}
        out: List[Candle] = []
        for c in candles:
            if c is None:
                continue  # missing data point
            eid = c.event_id or f"{c.symbol}|{c.timestamp.isoformat()}|{c.close}"
            if eid in self._seen_candle_ids or eid in unique:
                self.duplicates_skipped += 1
                continue
            unique[eid] = c
        ordered = sorted(unique.values(), key=lambda x: x.timestamp)
        self._seen_candle_ids.update(unique.keys())
        return ordered

    @staticmethod
    def _liquidity_of(bar: Candle) -> Dict[str, float]:
        """Bar liquidity context for the exchange (base-unit volume + range)."""
        return {'bar_volume': bar.volume, 'bar_high': bar.high, 'bar_low': bar.low}

    def _track_fill(self, result: BacktestResult, exec_result, is_long: bool,
                    realized_before: float, exit_stamp: Optional[str] = None) -> None:
        """Aggregate turnover, costs, participation and per-close PnL from a fill."""
        if not (exec_result.success and exec_result.filled_quantity
                and exec_result.avg_fill_price):
            return
        notional = abs(exec_result.filled_quantity * exec_result.avg_fill_price)
        result.turnover += notional
        if is_long:
            result.turnover_long += notional
        else:
            result.turnover_short += notional
        result.spread_paid += exec_result.spread_cost
        result.slippage_paid += exec_result.slippage_cost
        result.impact_paid += exec_result.impact_cost
        part = exec_result.participation_rate or 0.0
        if part > 0:
            result.participation_sum += part
            result.participation_n += 1
            result.max_participation = max(result.max_participation, part)
        if exec_result.liquidity_capped:
            result.liquidity_capped_fills += 1
        realized_after = float(getattr(self.portfolio_manager, 'realized_pnl', 0.0) or 0.0)
        delta = realized_after - realized_before
        if abs(delta) > 1e-9:
            result.trade_pnls.append(delta)
            result.trade_exit_stamps.append(exit_stamp or "")

    async def _execute_and_track(self, signal, prices: Dict[str, float],
                                 result: BacktestResult,
                                 liquidity: Optional[Dict[str, float]] = None,
                                 exit_stamp: Optional[str] = None,
                                 sim_now=None) -> bool:
        """Execute one signal; on fills update turnover/costs/PnL. Returns filled?"""
        realized_before = float(getattr(self.portfolio_manager, 'realized_pnl', 0.0) or 0.0)
        exec_result = await self.execution_engine.execute_signal(
            signal, self.portfolio_manager, prices, liquidity=liquidity,
            sim_now=sim_now)
        if exec_result.blocked_by == "rejected" \
                and exec_result.reject_reason == "insufficient_liquidity":
            result.insufficient_liquidity_rejects += 1
        self._track_fill(result, exec_result,
                         is_long=(signal.signal_type == SignalType.LONG),
                         realized_before=realized_before,
                         exit_stamp=exit_stamp)
        return bool(exec_result.success)

    async def run(self, symbol: str, candles: List[Candle],
                  interval: str = "5m", execution_model: Optional[str] = None,
                  warmup_candles: Optional[List[Candle]] = None) -> BacktestResult:
        """Replay `candles` with optional indicator `warmup_candles`.

        Warmup bars provide indicator context only: the strategy may observe
        them, but NO fills, turnover, PnL, equity points or risk updates are
        ever scored from the warmup region. `warmup_candles` must be strictly
        older than `candles` with no shared timestamps/event_ids, otherwise
        ValueError (leakage guard).
        """
        result = BacktestResult()
        model = execution_model or self.config.get('execution_model', 'next_close')
        if model not in ('next_open', 'next_close'):
            logger.warning("Unknown execution_model %r — using 'next_close'", model)
            model = 'next_close'
        result.execution_model = model
        warmup = self._validate_warmup(warmup_candles, candles)
        result.warmup_bars = len(warmup)
        candles = self._dedup_candles(candles)
        result.n_bars = len(candles)
        if not candles:
            logger.warning("Backtest: no candles to replay")
            return result
        full = warmup + candles  # warmup strictly precedes test (validated)

        equity_curve: List[float] = []
        exposures: List[float] = []
        peak = self.portfolio_manager.get_equity({})
        if peak <= 0:
            peak = float(self.config.get('initial_cash', 10000.0))
        if self.risk_manager is not None:
            self.risk_manager.update_peak(peak)

        # Next-bar execution: signal on full[:j], fill at full[j]
        # (open or close depending on model). Strategy never sees bar j.
        # Bars j < len(warmup) only warm indicator state — never scored.
        W = len(warmup)
        min_bars = 5
        for j in range(min_bars, len(full)):
            window = full[:j]
            exec_bar = full[j]
            exec_price = self._execution_price(exec_bar, model)
            if exec_price is None:
                continue  # missing price — skip bar
            if j < W:
                # Indicator context only: stateful strategies observe history,
                # but nothing is executed, settled or recorded here.
                try:
                    self.strategy_manager.generate_signal(
                        symbol, {'candles': window, 'ticker': None, 'orderbook': None})
                except Exception as e:
                    logger.debug("warmup signal failed: %s", e)
                continue
            prices = {symbol: exec_price}
            liquidity = self._liquidity_of(exec_bar)
            try:
                if hasattr(self.execution_engine.exchange, 'match_open_orders'):
                    self.execution_engine.exchange.match_open_orders(symbol, exec_price)
            except Exception as e:
                logger.debug("match_open_orders failed: %s", e)

            # SL/TP enforcement first (risk-owned), counted separately.
            sltp_filled = await self._enforce_stop_take(symbol, exec_price, prices,
                                                        bar_index=j, result=result,
                                                        stamp=exec_bar.timestamp,
                                                        liquidity=liquidity)
            if sltp_filled:
                result.trades += 1
                result.sltp_trades += 1

            market_data = {'candles': window, 'ticker': None, 'orderbook': None}
            signal = self.strategy_manager.generate_signal(symbol, market_data)
            if signal is None or signal.signal_type == SignalType.HOLD:
                equity = self.portfolio_manager.get_equity(prices)
                equity_curve.append(equity)
                result.equity_stamps.append(exec_bar.timestamp.isoformat())
                exposures.append(self._exposure(symbol, exec_price, equity))
                peak = max(peak, equity)
                continue
            # Deterministic per-bar id (timestamp-based: stable across warmup
            # lengths); override price to execution-bar price.
            signal.signal_id = f"bt_{symbol}_{exec_bar.timestamp.isoformat()}_{signal.signal_type.value}"
            signal.price = exec_price
            realized_before = float(getattr(self.portfolio_manager, 'realized_pnl', 0.0) or 0.0)
            exec_result = await self.execution_engine.execute_signal(
                signal, self.portfolio_manager, prices, liquidity=liquidity,
                sim_now=exec_bar.timestamp)
            if exec_result.success:
                result.trades += 1
                result.strategy_trades += 1
                self._track_fill(result, exec_result,
                                 is_long=(signal.signal_type == SignalType.LONG),
                                 realized_before=realized_before,
                                 exit_stamp=exec_bar.timestamp.isoformat())
            elif exec_result.blocked_by and exec_result.blocked_by not in ("hold",):
                if exec_result.blocked_by == "rejected":
                    result.rejected_orders += 1
                    if exec_result.reject_reason == "insufficient_liquidity":
                        result.insufficient_liquidity_rejects += 1
                else:
                    result.blocked_by_risk += 1
            equity = self.portfolio_manager.get_equity(prices)
            equity_curve.append(equity)
            result.equity_stamps.append(exec_bar.timestamp.isoformat())
            exposures.append(self._exposure(symbol, exec_price, equity))
            peak = max(peak, equity)
            if self.risk_manager is not None:
                self.risk_manager.update_peak(equity)

        # Final settlement (mark open positions at last close; realized excludes them).
        last_px = {symbol: candles[-1].close}
        info = self.portfolio_manager.calculate_pnl(last_px)
        result.realized_pnl = info.realized_pnl
        result.fees_paid = getattr(self.portfolio_manager, 'total_fees_paid', 0.0)
        result.final_equity = info.total_value
        # Gross PnL reconstructs reference-price PnL: realized is net of every
        # adverse cost leg plus commissions (legs telescope exactly).
        result.gross_pnl = (result.realized_pnl + result.fees_paid
                            + result.spread_paid + result.slippage_paid
                            + result.impact_paid)
        result.equity_curve = equity_curve
        result.exposures = exposures
        if equity_curve:
            running_peak = equity_curve[0]
            max_dd = 0.0
            for e in equity_curve:
                running_peak = max(running_peak, e)
                if running_peak > 0:
                    max_dd = max(max_dd, (running_peak - e) / running_peak)
            result.max_drawdown = max_dd
        if self.risk_manager is not None:
            m = self.risk_manager.get_risk_metrics()
            result.wins = m.winning_trades
            result.losses = m.losing_trades
            result.breakevens = max(0, m.total_trades - m.winning_trades - m.losing_trades)
        closed = result.wins + result.losses
        result.win_rate = (result.wins / closed) if closed > 0 else 0.0
        logger.debug("Backtest done [%s]: fills=%d (strat=%d sltp=%d) wins=%d losses=%d be=%d pnl=%.2f equity=%.2f maxDD=%.4f",
                    model, result.trades, result.strategy_trades, result.sltp_trades,
                    result.wins, result.losses, result.breakevens,
                    result.realized_pnl, result.final_equity, result.max_drawdown)
        return result

    def _exposure(self, symbol: str, mark_price: float, equity: float) -> float:
        try:
            pos = self.portfolio_manager.get_position(symbol)
            if pos is None or equity is None or equity <= 0:
                return 0.0
            return abs(pos.quantity * mark_price) / equity
        except Exception:
            return 0.0

    def _validate_warmup(self, warmup_candles, candles: List[Candle]) -> List[Candle]:
        """Validate indicator warmup: strictly before test bars, no shared ids."""
        if not warmup_candles:
            return []
        warmup = sorted(warmup_candles, key=lambda c: c.timestamp)
        test = sorted(candles, key=lambda c: c.timestamp)
        if max(c.timestamp for c in warmup) >= min(c.timestamp for c in test):
            raise ValueError("Warmup must end strictly before the test period (leakage)")
        w_ids = {c.event_id for c in warmup if c.event_id}
        t_ids = {c.event_id for c in test if c.event_id}
        if w_ids & t_ids:
            raise ValueError("Warmup shares event_ids with the test period (leakage)")
        w_ts = {c.timestamp for c in warmup}
        t_ts = {c.timestamp for c in test}
        if w_ts & t_ts:
            raise ValueError("Warmup shares timestamps with the test period (leakage)")
        return warmup

    async def _enforce_stop_take(self, symbol: str, current_price: float,
                                 prices: Dict[str, float], bar_index: int = 0,
                                 result: Optional[BacktestResult] = None,
                                 stamp=None,
                                 liquidity: Optional[Dict[str, float]] = None) -> bool:
        """Returns True if an SL/TP fill occurred."""
        if self.risk_manager is None or current_price <= 0:
            return False
        pos = self.portfolio_manager.get_position(symbol)
        if pos is None:
            return False
        side = 'long' if pos.quantity > 0 else 'short'
        should_close, reason = self.risk_manager.check_stop_take(pos.entry_price, current_price, side)
        if should_close:
            from strategy import TradingSignal
            tag = stamp.isoformat() if stamp is not None else str(bar_index)
            bar_ts = stamp if stamp is not None else _utcnow()
            sig = TradingSignal(symbol=symbol,
                                signal_type=(SignalType.SHORT if pos.quantity > 0 else SignalType.LONG),
                                confidence=1.0, timestamp=bar_ts, price=current_price,
                                reason=reason,
                                signal_id=f"bt_sltp_{symbol}_{tag}_{reason}")
            if result is None:
                res = await self.execution_engine.execute_signal(sig, self.portfolio_manager, prices,
                                                                 liquidity=liquidity,
                                                                 sim_now=bar_ts)
                return bool(res.success)
            filled = await self._execute_and_track(
                sig, prices, result, liquidity=liquidity,
                exit_stamp=(stamp.isoformat() if stamp is not None else ""),
                sim_now=bar_ts)
            return filled
        return False

    def get_stats(self) -> Dict[str, Any]:
        return {'duplicates_skipped': self.duplicates_skipped,
                'seen_candles': len(self._seen_candle_ids)}
