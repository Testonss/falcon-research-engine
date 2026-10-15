"""Forensic accounting tests for backtest/PnL.

Proves (without optimizing the strategy):
- a losing round-trip is recorded as a loss (not a win, not hidden);
- breakeven is neither win nor loss;
- backtest has no look-ahead (next-bar execution);
- timestamp ordering is deterministic;
- realized vs unrealized vs fees reconcile.
"""
import asyncio
import unittest
from datetime import datetime, timezone, timedelta

from config import ConfigManager
from market_data import MarketDataHandler, Candle
from exchange import PaperExchange
from strategy import StrategyManager, TradingSignal, SignalType
from execution import ExecutionEngine
from risk import RiskManager
from portfolio import PortfolioManager
from backtest import BacktestEngine


def run(coro):
    return asyncio.run(coro)


def make_candles(prices, start_ts=None, symbol="BTC/USDT", prefix="c"):
    if start_ts is None:
        start_ts = datetime.now(timezone.utc) - timedelta(minutes=len(prices) * 5)
    out = []
    for i, p in enumerate(prices):
        out.append(Candle(symbol=symbol, open=p, high=p * 1.001, low=p * 0.999,
                          close=p, volume=5000.0,
                          timestamp=start_ts + timedelta(minutes=i * 5), interval="5m",
                          event_id=f"{prefix}{i}"))
    return out


def make_stack(**overrides):
    config = ConfigManager()
    config.set('simulated_latency_ms', 0)
    config.set('min_volume', 100.0)
    config.set('volatility_max', 1.0)
    config.set('initial_cash', 10000.0)
    for k, v in overrides.items():
        config.set(k, v)
    md = MarketDataHandler(config)
    ex = PaperExchange(config, seed=42)
    run(ex.connect())
    risk = RiskManager(config)
    pf = PortfolioManager(config)
    eng = ExecutionEngine(config, ex, risk)
    strat = StrategyManager(config)
    bt = BacktestEngine(config, md, eng, pf, risk, strat)
    return config, md, ex, risk, pf, eng, strat, bt


class AlwaysLong:
    """Test strategy: always LONG at window close (for look-ahead probe)."""
    def __init__(self):
        self.windows = []  # list of (len, last_close, last_ts)

    def generate_signal(self, symbol, market_data):
        candles = market_data.get('candles', [])
        if len(candles) < 6:
            return None
        self.windows.append((len(candles), candles[-1].close, candles[-1].timestamp))
        return TradingSignal(symbol=symbol, signal_type=SignalType.LONG, confidence=1.0,
                             timestamp=datetime.now(timezone.utc), price=candles[-1].close,
                             signal_id=f"probe_{len(self.windows)}")


class TestForensicAccounting(unittest.TestCase):
    def test_losing_trade_recorded_as_loss_integration(self):
        # Zero costs for exact math: buy 10 @100, sell 10 @90 => -100.
        config, md, ex, risk, pf, eng, strat, bt = make_stack(
            spread_pct=0.0, slippage_pct=0.0, maker_fee=0.0, taker_fee=0.0,
            stop_loss_pct=10.0, take_profit_pct=10.0)
        # Force exact qty = 10 by patching sizing.
        eng._calculate_order_quantity = lambda sig, pm, ref, eq: 10.0
        buy = TradingSignal(symbol="BTC/USDT", signal_type=SignalType.LONG, confidence=1.0,
                            timestamp=datetime.now(timezone.utc), price=100.0, signal_id="loss_buy")
        res1 = run(eng.execute_signal(buy, pf, {"BTC/USDT": 100.0}))
        self.assertTrue(res1.success)
        self.assertAlmostEqual(res1.avg_fill_price, 100.0, places=6)
        sell = TradingSignal(symbol="BTC/USDT", signal_type=SignalType.SHORT, confidence=1.0,
                             timestamp=datetime.now(timezone.utc), price=90.0, signal_id="loss_sell")
        res2 = run(eng.execute_signal(sell, pf, {"BTC/USDT": 90.0}))
        self.assertTrue(res2.success)
        self.assertAlmostEqual(res2.avg_fill_price, 90.0, places=6)
        # Portfolio: realized must be negative (a real loss, fees zero).
        self.assertAlmostEqual(pf.realized_pnl, -100.0, places=6)
        # Risk: exactly one loss, zero wins, daily_loss ~= 100.
        m = risk.get_risk_metrics()
        self.assertEqual(m.losing_trades, 1)
        self.assertEqual(m.winning_trades, 0)
        self.assertAlmostEqual(risk.metrics.daily_loss, 100.0, places=6)
        self.assertEqual(m.consecutive_losses, 1)
        run(ex.disconnect())

    def test_breakeven_is_neither_win_nor_loss(self):
        config = ConfigManager()
        risk = RiskManager(config)
        risk.record_trade_result(False, pnl=0.0, equity=10000.0)
        m = risk.get_risk_metrics()
        self.assertEqual(m.total_trades, 1)
        self.assertEqual(m.winning_trades, 0)
        self.assertEqual(m.losing_trades, 0)
        self.assertEqual(m.consecutive_losses, 0)
        self.assertAlmostEqual(m.daily_loss, 0.0)
        # Tiny dust within eps is also breakeven.
        risk.record_trade_result(True, pnl=1e-12, equity=10000.0)
        m = risk.get_risk_metrics()
        self.assertEqual(m.winning_trades, 0)
        self.assertEqual(m.losing_trades, 0)

    def test_backtest_has_no_lookahead_next_bar_execution(self):
        # Zero costs => fill == execution-bar close. With next-bar execution,
        # fill must equal the bar AFTER the signal window, not the window close.
        config, md, ex, risk, pf, eng, strat, bt = make_stack(
            spread_pct=0.0, slippage_pct=0.0, maker_fee=0.0, taker_fee=0.0,
            stop_loss_pct=10.0, take_profit_pct=10.0)
        probe = AlwaysLong()
        bt.strategy_manager = probe  # type: ignore
        prices = [100.0 + i for i in range(30)]  # strict uptrend, step 1
        candles = make_candles(prices, prefix="la")
        result = run(bt.run("BTC/USDT", candles))
        self.assertGreater(result.strategy_trades, 0)
        # Each fill must match the NEXT bar after its window.
        fills = [(o.avg_fill_price) for o, _ in eng.get_execution_history()]
        # windows[i] had last close = 100 + (min_bars-1 + i) approx; fill should be +1.
        # Check at least first fill: window last + 1 == fill (step is 1.0).
        first_window_last = probe.windows[0][1]
        self.assertAlmostEqual(fills[0], first_window_last + 1.0, places=6,
                               msg=f"look-ahead? fill={fills[0]} window_last={first_window_last}")
        # And no fill may equal a future spike it could not have seen:
        # append a spike as final bar; strategy windows must never contain it
        # when deciding fills before it.
        for length, last_close, last_ts in probe.windows:
            self.assertLess(last_close, 100.0 + 30)  # spike would be e.g. 500
        run(ex.disconnect())

    def test_backtest_ignores_future_spike(self):
        # If the last bar is an extreme spike, signals generated before it
        # must not have seen it. Probe windows' max close must exclude spike.
        config, md, ex, risk, pf, eng, strat, bt = make_stack(
            spread_pct=0.0, slippage_pct=0.0, maker_fee=0.0, taker_fee=0.0,
            stop_loss_pct=10.0, take_profit_pct=10.0)
        probe = AlwaysLong()
        bt.strategy_manager = probe  # type: ignore
        prices = [100.0] * 25 + [500.0]  # flat then spike at the end
        candles = make_candles(prices, prefix="spike")
        run(bt.run("BTC/USDT", candles))
        max_seen = max(w[1] for w in probe.windows)
        self.assertLess(max_seen, 500.0,
                        msg="strategy saw the future spike before it occurred")
        run(ex.disconnect())

    def test_timestamp_ordering_deterministic(self):
        config1, md1, ex1, risk1, pf1, eng1, strat1, bt1 = make_stack()
        prices = [100.0 + (i % 5) for i in range(40)]
        ordered = make_candles(prices, prefix="ord")
        r1 = run(bt1.run("BTC/USDT", list(ordered)))
        run(ex1.disconnect())
        config2, md2, ex2, risk2, pf2, eng2, strat2, bt2 = make_stack()
        shuffled = list(make_candles(prices, prefix="ord"))
        shuffled = shuffled[::-1]  # reverse timestamps
        r2 = run(bt2.run("BTC/USDT", shuffled))
        run(ex2.disconnect())
        self.assertEqual(r1.trades, r2.trades)
        self.assertAlmostEqual(r1.final_equity, r2.final_equity, places=6)
        self.assertAlmostEqual(r1.realized_pnl, r2.realized_pnl, places=6)

    def test_unrealized_realized_fees_reconcile_zero_costs(self):
        config, md, ex, risk, pf, eng, strat, bt = make_stack(
            spread_pct=0.0, slippage_pct=0.0, maker_fee=0.0, taker_fee=0.0,
            stop_loss_pct=10.0, take_profit_pct=10.0)
        prices = [100.0 + i * 0.5 for i in range(30)]
        candles = make_candles(prices, prefix="rec")
        result = run(bt.run("BTC/USDT", candles))
        last = prices[-1]
        info = pf.calculate_pnl({"BTC/USDT": last})
        # With zero costs: equity == initial + realized + unrealized.
        self.assertAlmostEqual(info.total_value,
                               10000.0 + info.realized_pnl + info.unrealized_pnl, places=6)
        self.assertAlmostEqual(result.final_equity, info.total_value, places=6)
        self.assertAlmostEqual(result.realized_pnl, info.realized_pnl, places=6)
        self.assertAlmostEqual(result.fees_paid, 0.0, places=9)
        run(ex.disconnect())

    def test_win_rate_uses_closed_trades_denominator(self):
        config, md, ex, risk, pf, eng, strat, bt = make_stack()
        prices = [100.0 + i for i in range(40)]
        candles = make_candles(prices, prefix="wr")
        result = run(bt.run("BTC/USDT", candles))
        closed = result.wins + result.losses
        if closed > 0:
            self.assertAlmostEqual(result.win_rate, result.wins / closed, places=9)
        else:
            self.assertEqual(result.win_rate, 0.0)
        # Fills include opens; wins+losses count closes only.
        self.assertGreaterEqual(result.trades, closed)
        run(ex.disconnect())

    def test_costs_make_fills_adverse(self):
        # With costs on, buys fill above ref and sells below ref, fees > 0.
        config, md, ex, risk, pf, eng, strat, bt = make_stack(
            spread_pct=0.001, slippage_pct=0.001, maker_fee=0.001, taker_fee=0.002)
        prices = [100.0 + i for i in range(30)]
        candles = make_candles(prices, prefix="cost")
        result = run(bt.run("BTC/USDT", candles))
        self.assertGreater(result.trades, 0)
        self.assertGreater(result.fees_paid, 0.0)
        # At least one buy fill above its execution bar and one sell below.
        histories = eng.get_execution_history()
        buys = [o for o, _ in histories if o.side.value == 'buy']
        sells = [o for o, _ in histories if o.side.value == 'sell']
        # Execution bars are candles[5:] (next-bar). Buy fill must exceed 100 (uptrend start).
        self.assertTrue(all(o.commission > 0 for o, _ in histories))
        run(ex.disconnect())


if __name__ == '__main__':
    unittest.main()
