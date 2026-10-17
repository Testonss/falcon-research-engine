"""Accelerated 7-day deterministic soak (no MT5, no Qt, no network).

Scenario per symbol over >= 7 trading days (5m bars by default):
  trends (entries) / ranges (no-signal) / high-spread + stale windows
  (rejects, no entries) / market-closed windows (bounded backoff, no storm) /
  disconnect windows (DEGRADED, no entries) / reconnect (verify DEMO ->
  reconcile -> resume) / mid-soak restart (crash recovery, zero duplicates) /
  exits via short signal-expiry/time-stop + reconcile of closed tickets.

Deterministic: seeded drift pattern, fixed t0, no randomness, no wall-clock
(explicit now per cycle). Reports cycles/bars/signals/orders/rejects/
positions/restarts/reconnects/duplicates. Duplicate count must be zero.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

from config import ConfigManager
from risk import RiskManager
from runtime.config import RuntimeConfig
from runtime.execution import RuntimeDemoExecutor, build_adapter_for_config
from runtime.runtime import AutonomousDemoRuntime
from runtime.store import RuntimeStore


class _SoakMT5:
    """Minimal DEMO-shaped fake with ticketed positions and close support."""

    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 60
    TIMEFRAME_H4 = 240
    TIMEFRAME_D1 = 1440
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1

    def __init__(self, server="MetaQuotes-Demo"):
        self._server = server
        self._mode = 0
        self.order_send_calls = 0
        self.last_request = None
        self._next_ticket = 700001
        self._positions: List[Any] = []
        self.market_closed = False
        self.next_check = SimpleNamespace(retcode=0, comment="ok")

    def account_info(self):
        return SimpleNamespace(login=1, server=self._server, trade_mode=self._mode,
                               balance=10000.0, equity=10000.0, margin=0.0,
                               margin_free=10000.0, profit=0.0, currency="USD")

    def symbol_info(self, symbol):
        return SimpleNamespace(name=symbol, visible=True, trade_mode=0,
                               volume_min=0.01, volume_max=5.0, volume_step=0.01,
                               point=0.00001, digits=5, contract_size=100000.0)

    def symbol_select(self, s, b):
        return True

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=1.1, ask=1.1002, last=1.1001,
                               time=datetime.now(timezone.utc))

    def order_check(self, request):
        return self.next_check

    def order_send(self, request):
        from execution.mt5_demo import DemoSafetyError
        if self.market_closed:
            return SimpleNamespace(retcode=10018, order=0, comment="market closed")
        self.order_send_calls += 1
        self.last_request = dict(request)
        action = request.get("action", 1)
        if action == 6:  # SLTP modify
            return SimpleNamespace(retcode=10009, order=request.get("position", 0),
                                   comment="ok")
        if "position_id" in request:  # close
            pid = int(request["position_id"])
            self._positions = [p for p in self._positions if int(p.ticket) != pid]
            return SimpleNamespace(retcode=10009, order=pid, comment="ok")
        t = self._next_ticket
        self._next_ticket += 1
        side = "BUY" if request.get("type", 0) == 0 else "SELL"
        pos = SimpleNamespace(ticket=t, symbol=request.get("symbol", ""),
                              type=0 if side == "BUY" else 1,
                              volume=float(request.get("volume", 0.01)),
                              price_open=float(request.get("price", 1.1)),
                              price_current=float(request.get("price", 1.1)),
                              sl=float(request.get("sl", 0) or 0),
                              tp=float(request.get("tp", 0) or 0),
                              profit=0.0, swap=0.0, magic=20261016,
                              comment="falcon-demo", time=1700000000,
                              side=side)
        self._positions.append(pos)
        return SimpleNamespace(retcode=10009, order=t, comment="ok")

    def positions_get(self, ticket=None):
        if ticket is not None:
            return [p for p in self._positions if int(p.ticket) == int(ticket)]
        return list(self._positions)


def _build_bars(symbol: str, total: int, start: float = 1.1000):
    t0 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    bars = []
    px = start
    for i in range(total):
        day = i // 288
        within = i % 288
        phase = (day % 7, within)
        # Deterministic regime mix per day-of-week cycle:
        d = day % 7
        if d in (0, 3, 5):  # trend days -> entries
            drift = 0.0012 if d != 3 else -0.0012
            drift += 0.0002 if (i % 3 == 0) else -0.0001
            vol = 1000.0
        elif d in (1, 4):  # range days -> mostly no-signal
            drift = 0.0001 * (1 if (i % 2 == 0) else -1)
            vol = 800.0
        elif d == 2:  # choppy, some signals some rejects
            drift = 0.0006 if (within // 24) % 2 == 0 else -0.0006
            vol = 1200.0
        else:  # d == 6: flat no-signal gaps + low volume pockets
            drift = 0.0
            vol = 0.0 if (within // 48) % 2 == 0 else 500.0
        o = px
        c = px + drift
        if c <= 0:
            c = px
        bars.append({"close": c, "open": o, "high": max(o, c) + 0.0001,
                     "low": min(o, c) - 0.0001, "tick_volume": vol,
                     "time": t0 + timedelta(minutes=5 * i),
                     "is_closed": True, "symbol": symbol})
        px = c
    return bars


def run_7d_soak(state_db: str = "runtime_demo_soak.db",
                symbols=("EURUSD",), days: int = 7,
                bars_per_day: int = 288, cycles: int | None = None,
                server: str = "MetaQuotes-Demo") -> Dict[str, Any]:
    symbols = list(symbols or ["EURUSD"])
    days = max(7, int(days or 7))
    bars_per_day = max(50, int(bars_per_day or 288))
    total_bars = days * bars_per_day
    # Cycle budget: enough to replay the whole window with step>1 for speed.
    step = max(1, total_bars // 220)
    warm0 = 40
    plan_cycles = (total_bars - warm0) // step
    if cycles:
        plan_cycles = max(10, int(cycles))
    else:
        cycles = plan_cycles
    cycles = int(cycles)

    try:
        os.unlink(state_db)
    except OSError:
        pass

    full = {s: _build_bars(s, total_bars) for s in symbols}

    def make_stack():
        cm = ConfigManager()
        rcfg = RuntimeConfig.from_config_manager(cm)
        rcfg.symbols = list(symbols)
        rcfg.poll_interval_s = 0.0
        rcfg.state_db = state_db
        rcfg.dry_run = False
        rcfg.max_concurrent_positions = 2
        rcfg.max_total_lots = 5.0
        rcfg.max_exposure_per_symbol = 50000.0
        rcfg.max_total_exposure = 100000.0
        rcfg.max_trades_per_symbol = 6
        rcfg.trades_window_s = 6 * 3600.0
        rcfg.cooldown_between_trades_s = 300.0
        rcfg.max_spread_pct = 0.002
        rcfg.stale_after_s = 30.0
        rcfg.signal_expiry_bars = 12
        rcfg.time_stop_bars = 25
        rcfg.market_closed_backoff_s = 60.0
        rcfg.market_closed_max_backoff_s = 900.0
        risk = RiskManager(cm)
        store = RuntimeStore(state_db)
        fake = _SoakMT5(server=server or "MetaQuotes-Demo")
        adapter = build_adapter_for_config(
            fake, server=server or "demo",
            risk_kwargs={"max_position_size": rcfg.max_position_size,
                         "max_concurrent_positions": rcfg.max_concurrent_positions,
                         "max_daily_loss": rcfg.max_daily_loss,
                         "max_risk_per_trade": 1e9})
        ex = RuntimeDemoExecutor(adapter, store=store, dry_run=False)
        try:
            ex.enable()
        except Exception:
            pass
        return rcfg, risk, store, fake, ex

    rcfg, risk, store, fake, ex = make_stack()

    # Deterministic scenario windows (fractions of the cycle plan).
    stale_win = set(range(int(cycles * 0.30), int(cycles * 0.36)))
    closed_win = set(range(int(cycles * 0.50), int(cycles * 0.56)))
    disc_win = set(range(int(cycles * 0.66), int(cycles * 0.72)))
    restart_at = int(cycles * 0.55)

    flags = {"stale": False, "closed": False, "disc": False}

    def feed_age(symbol: str):
        return 999.0 if flags["stale"] else 0.0

    def spread(symbol: str):
        # High-spread pocket inside the stale window forces spread rejects.
        return 0.01 if flags["stale"] else 0.0

    def connectivity():
        if flags["disc"]:
            return {"ok": False, "reason": "ipc_disconnect"}
        return {"ok": True}

    t0 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    clock = {"now": t0}

    rt = AutonomousDemoRuntime(
        rcfg, store, ex, risk_manager=risk, mt5_module=fake,
        bars_provider={s: [] for s in symbols}, accelerated=True,
        feed_age_fn=feed_age, spread_fn=spread, connectivity_fn=connectivity,
        now_fn=lambda: clock["now"],
        time_fn=lambda: clock["now"].timestamp())

    sent_ids: List[str] = []
    duplicates = 0
    restarts = 0
    reconnects = 0
    max_open = 0

    async def _drive():
        nonlocal restarts, reconnects, duplicates, rt, store, ex, fake, rcfg, risk
        for i in range(cycles):
            clock["now"] = t0 + timedelta(minutes=5 * i)
            flags["stale"] = i in stale_win
            flags["closed"] = i in closed_win
            flags["disc"] = i in disc_win
            fake.market_closed = flags["closed"]
            # Expanding replay window (deterministic new bar_time each cycle).
            upto = min(total_bars, warm0 + (i + 1) * step)
            rt.bars_provider = {s: full[s][:upto] for s in symbols}
            # Update terminal marks to current closes (exit engine input).
            try:
                for p in fake._positions:
                    for s in symbols:
                        if p.symbol == s:
                            mark = float(full[s][upto - 1]["close"])
                            p.price_current = mark
                            try:
                                p.current = mark
                            except Exception:
                                pass
            except Exception:
                pass
            # Mid-soak crash restart: new store/executor/runtime, same DB +
            # same terminal (positions survive). Pending never-executed must
            # expire, executed must never resend.
            if i == restart_at:
                try:
                    store.close()
                except Exception:
                    pass
                store2 = RuntimeStore(state_db)
                adapter2 = build_adapter_for_config(
                    fake, server=server or "demo",
                    risk_kwargs={"max_position_size": rcfg.max_position_size,
                                 "max_concurrent_positions": rcfg.max_concurrent_positions,
                                 "max_daily_loss": rcfg.max_daily_loss,
                                 "max_risk_per_trade": 1e9})
                ex2 = RuntimeDemoExecutor(adapter2, store=store2, dry_run=False)
                try:
                    ex2.enable()
                except Exception:
                    pass
                rt2 = AutonomousDemoRuntime(
                    rcfg, store2, ex2, risk_manager=risk, mt5_module=fake,
                    bars_provider=dict(rt.bars_provider), accelerated=True,
                    feed_age_fn=feed_age, spread_fn=spread,
                    connectivity_fn=connectivity,
                    now_fn=lambda: clock["now"],
                    time_fn=lambda: clock["now"].timestamp())
                # Carry held-age + expected intent (terminal truth re-seeds).
                rt2.expected = dict(rt.expected)
                rt2._bars_held = dict(rt._bars_held)
                rt2.reconnect_count = rt.reconnect_count
                try:
                    rt2.metrics.stale_feed_count = rt.metrics.stale_feed_count
                except Exception:
                    pass
                rt, store, ex = rt2, store2, ex2
                restarts += 1
            state = await rt.run_cycle()
            max_open_now = int(state.get("open_expected", 0) or 0)
            nonlocal_max = max_open
            for d in state.get("decisions", []):
                if d.get("sent"):
                    did = str(d.get("decision_id", ""))
                    if did in sent_ids:
                        duplicates += 1
                    else:
                        sent_ids.append(did)
            try:
                if max_open_now > max_open:
                    pass
            except Exception:
                pass
            globals().update({})
            # track max open via closure list
            _track.append(max_open_now)
            if rt._was_degraded is False and flags["disc"] is False and i > 0:
                pass
            # Count reconnect recoveries (DEGRADED -> RUNNING transitions log RECONNECT).
            try:
                ev = store.event_counts()
                reconnects = int(ev.get("RECONNECT", 0) or 0)
            except Exception:
                pass

    _track: List[int] = []
    asyncio.run(_drive())

    # Final tallies from the store (deterministic, restart-safe).
    try:
        counts = store.decision_counts()
        signals = int(counts.get("total", 0) or 0)
    except Exception:
        signals = 0
    try:
        ev = store.event_counts()
        rejects = int(ev.get("RISK_REJECT", 0) or 0) + int(ev.get("ORDER_REJECTED", 0) or 0)
        orders = int(ev.get("ORDER_ACCEPTED", 0) or 0)
        positions = int(ev.get("POSITION_OPEN", 0) or 0)
    except Exception:
        rejects, orders, positions = 0, 0, 0
    try:
        fills = store.list_fills(limit=100000)
        tickets = [int(f.get("ticket", 0) or 0) for f in fills if int(f.get("ticket", 0) or 0)]
        if len(tickets) != len(set(tickets)) and tickets:
            # Same ticket reused across fills would indicate resend; count it.
            duplicates += len(tickets) - len(set(tickets))
    except Exception:
        pass
    # Risk limits never violated (final + structural checks).
    warnings: List[str] = []
    try:
        open_pos = store.list_positions(status="OPEN", limit=5000)
        tot_lots = sum(float(p.get("volume", 0) or 0) for p in open_pos)
        if len(open_pos) > int(rcfg.max_concurrent_positions):
            warnings.append(f"concurrent breach {len(open_pos)}")
        if tot_lots > float(rcfg.max_total_lots) + 1e-9:
            warnings.append(f"lots breach {tot_lots}")
    except Exception as e:
        warnings.append(f"limit-check:{e}")
    # Bounded growth checks.
    try:
        n_hb = store._db.execute("SELECT COUNT(*) c FROM heartbeats").fetchone()["c"]
        n_ev = store._db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        if int(n_hb) > 500:
            warnings.append(f"heartbeat growth {n_hb}")
        if int(n_ev) > 5000:
            warnings.append(f"event growth {n_ev}")
    except Exception:
        pass
    max_open_v = max(_track) if _track else 0
    _ = max_open_v
    report = {
        "days": days, "cycles": cycles, "bars": total_bars * len(symbols),
        "signals": signals, "orders": orders, "rejects": rejects,
        "positions": positions, "restarts": restarts,
        "reconnects": reconnects, "duplicates": duplicates,
        "order_send_calls": int(fake.order_send_calls),
        "warnings": warnings,
    }
    try:
        store.close()
    except Exception:
        pass
    return report
