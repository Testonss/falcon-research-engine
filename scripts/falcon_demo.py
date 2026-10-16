#!/usr/bin/env python3
"""Falcon Autonomous MT5 DEMO runtime — headless CLI (v0.2).

DEMO-ONLY. Every order path goes through DemoExecutionAdapter's double
DEMO guard (trade_mode == 0 at enable AND before each order_send).
REAL / CONTEST / unknown accounts are fail-closed: the loop keeps
heartbeating but never sends.

Commands:
  falcon-demo run        run the headless loop (H24 capable)
  falcon-demo status     health/heartbeat snapshot from the state DB
  falcon-demo pause      pause trading (DB flag; heartbeats continue)
  falcon-demo resume     resume trading
  falcon-demo reconcile  one-shot terminal-vs-local reconciliation
  falcon-demo soak       accelerated deterministic soak (no MT5 needed)

Live soak (human, MT5 terminal + DEMO account required):
  python scripts/falcon_demo.py run --symbols EURUSD --timeframe 5m ^
      --poll-s 5 --state-db runtime_demo.db

Accelerated offline soak (CI-safe, no broker):
  python scripts/falcon_demo.py soak --cycles 50 --symbols EURUSD
"""

import argparse
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import ConfigManager
from risk import RiskManager
from runtime.config import RuntimeConfig
from runtime.execution import RuntimeDemoExecutor, build_adapter_for_config
from runtime.health import health_snapshot
from runtime.reconcile import reconcile_positions
from runtime.runtime import AutonomousDemoRuntime
from runtime.store import RuntimeStore


def build_stack(args, mt5_module=None):
    cm = ConfigManager()
    rcfg = RuntimeConfig.from_config_manager(cm)
    if getattr(args, "symbols", None):
        rcfg.symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    for attr, val in (("timeframe", getattr(args, "timeframe", None)),
                      ("server", getattr(args, "server", None)),
                      ("state_db", getattr(args, "state_db", None)),
                      ("pause_file", getattr(args, "pause_file", None))):
        if val:
            setattr(rcfg, attr, val)
    if getattr(args, "poll_s", None) is not None:
        rcfg.poll_interval_s = max(0.0, float(args.poll_s))
    if getattr(args, "max_cycles", None):
        rcfg.max_cycles = int(args.max_cycles)
    if getattr(args, "dry_run", False):
        rcfg.dry_run = True
    rcfg = RuntimeConfig.from_env(rcfg)
    risk = RiskManager(cm)
    store = RuntimeStore(rcfg.state_db)
    adapter = build_adapter_for_config(
        mt5_module, server=rcfg.server or "demo",
        risk_kwargs={"max_position_size": rcfg.max_position_size,
                     "max_concurrent_positions": rcfg.max_concurrent_positions,
                     "max_daily_loss": rcfg.max_daily_loss})
    executor = RuntimeDemoExecutor(adapter, store=store, dry_run=rcfg.dry_run)
    return cm, rcfg, risk, store, executor


def cmd_run(args):
    try:
        from market_data.providers.mt5 import MT5LiveMarketDataProvider
    except ImportError as e:
        print(f"falcon-demo: error: MT5 provider unavailable: {e}", file=sys.stderr)
        return 2
    cm, rcfg, risk, store, executor = build_stack(args)
    provider = MT5LiveMarketDataProvider(
        symbols=tuple(rcfg.symbols), server=rcfg.server,
        poll_interval_s=max(1.0, rcfg.poll_interval_s or 5.0),
        stale_after_s=rcfg.stale_after_s)
    rt = AutonomousDemoRuntime(rcfg, store, executor, risk_manager=risk,
                               market_provider=provider)
    try:
        out = asyncio.run(_run_with_provider(rt, provider))
    except KeyboardInterrupt:
        print("falcon-demo: stopped cleanly (Ctrl+C)")
        return 0
    finally:
        store.close()
    print(f"falcon-demo run: run_id={out['run_id']} cycles={out['cycles']} sent={out['sent']}")
    return 0


async def _run_with_provider(rt, provider):
    try:
        await provider.connect()
    except Exception as e:
        print(f"falcon-demo: warning: provider connect failed ({e}) — "
              f"running send path only where bars exist")
    try:
        return await rt.run()
    finally:
        try:
            await provider.disconnect()
        except Exception:
            pass


def cmd_status(args):
    store = RuntimeStore(args.state_db)
    try:
        snap = health_snapshot(store)
    finally:
        store.close()
    print(f"run_id={snap.get('run_id') or '-'} cycle={snap.get('cycle') or '0'} "
          f"paused={snap.get('paused')} decisions={snap.get('decisions')}")
    hb = snap.get("last_heartbeat")
    if hb:
        print(f"last_heartbeat cycle={hb.get('cycle')} at={hb.get('at')}")
        st = hb.get("state") or {}
        if isinstance(st, dict):
            print(f"  equity={st.get('equity')} open_expected={st.get('open_expected')} "
                  f"demo={st.get('demo')}")
    else:
        print("no heartbeat yet")
    return 0


def cmd_pause(args):
    store = RuntimeStore(args.state_db)
    try:
        store.set_paused(True, args.reason or "operator")
        print(f"paused ({args.reason or 'operator'})")
    finally:
        store.close()
    return 0


def cmd_resume(args):
    store = RuntimeStore(args.state_db)
    try:
        store.set_paused(False)
        print("resumed")
    finally:
        store.close()
    return 0


def cmd_reconcile(args):
    cm, rcfg, risk, store, executor = build_stack(args)
    try:
        mt5 = None
        try:
            import MetaTrader5 as _real  # noqa
            mt5 = _real
        except Exception:
            pass
        if mt5 is not None:
            try:
                executor.enable(mt5)
            except Exception as e:
                print(f"falcon-demo: DEMO enable failed: {e}")
                return 1
        expected = {}
        for f in store.list_fills(limit=1000):
            t = int(f.get("ticket", 0) or 0)
            if t:
                expected[t] = {"symbol": f.get("symbol", ""),
                               "volume": float(f.get("volume", 0) or 0),
                               "side": str(f.get("side", "") or "").upper()}
        actual = executor.open_positions() if mt5 is not None else []
        rep = reconcile_positions(expected, actual)
        print(f"ok={rep.ok} matched={rep.matched} "
              f"manually_closed={rep.manually_closed} unexpected={rep.unexpected} "
              f"volume_mismatch={rep.volume_mismatch}")
        return 0 if rep.ok else 1
    finally:
        store.close()


def _synthetic_bars(symbol, n=120, start=1.1000):
    from datetime import datetime, timedelta, timezone
    bars = []
    px = start
    t0 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    for i in range(n):
        # Sustained uptrend with mild noise: momentum (~1.5%/14 bars)
        # clears the default 1% threshold -> deterministic BUY signal.
        drift = 0.0012 + (0.0002 if (i % 3 == 0) else -0.0001)
        o = px
        c = px + drift
        h = max(o, c) + 0.0001
        low = min(o, c) - 0.0001
        bars.append({"close": c, "open": o, "high": h, "low": low,
                     "tick_volume": 1000.0,
                     "time": t0 + timedelta(minutes=5 * i),
                     "is_closed": True})
        px = c
    return bars


def cmd_soak(args):
    cm, rcfg, risk, store, executor = build_stack(args)
    rcfg.dry_run = True
    executor.dry_run = True
    symbols = [s.strip() for s in (args.symbols or "EURUSD").split(",") if s.strip()]
    rcfg.symbols = symbols
    bars = {s: _synthetic_bars(s, n=int(args.bars)) for s in symbols}
    rt = AutonomousDemoRuntime(rcfg, store, executor, risk_manager=risk,
                               bars_provider=bars, accelerated=True)
    try:
        out = asyncio.run(rt.run(max_cycles=int(args.cycles)))
    finally:
        store.close()
    print(f"falcon-demo soak: cycles={out['cycles']} sent={out['sent']} "
          f"(dry_run, deterministic, no broker)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="falcon-demo",
                                 description="Falcon Autonomous MT5 DEMO runtime (headless, DEMO-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the headless DEMO loop")
    r.add_argument("--symbols", default="EURUSD")
    r.add_argument("--timeframe", default="5m")
    r.add_argument("--server", default="")
    r.add_argument("--poll-s", type=float, default=5.0)
    r.add_argument("--state-db", default="runtime_demo.db")
    r.add_argument("--pause-file", default="")
    r.add_argument("--max-cycles", type=int, default=0)
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("status", help="health/heartbeat snapshot")
    s.add_argument("--state-db", default="runtime_demo.db")
    s.set_defaults(fn=cmd_status)

    p = sub.add_parser("pause", help="pause trading (heartbeats continue)")
    p.add_argument("--state-db", default="runtime_demo.db")
    p.add_argument("--reason", default="operator")
    p.set_defaults(fn=cmd_pause)

    rs = sub.add_parser("resume", help="resume trading")
    rs.add_argument("--state-db", default="runtime_demo.db")
    rs.set_defaults(fn=cmd_resume)

    rc = sub.add_parser("reconcile", help="one-shot position reconciliation")
    rc.add_argument("--state-db", default="runtime_demo.db")
    rc.add_argument("--server", default="")
    rc.set_defaults(fn=cmd_reconcile)

    sk = sub.add_parser("soak", help="accelerated deterministic soak (no MT5)")
    sk.add_argument("--symbols", default="EURUSD")
    sk.add_argument("--cycles", type=int, default=50)
    sk.add_argument("--bars", type=int, default=120)
    sk.add_argument("--state-db", default="runtime_demo_soak.db")
    sk.add_argument("--server", default="")
    sk.set_defaults(fn=cmd_soak)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except SystemExit:
        raise
    except Exception as e:
        print(f"falcon-demo: error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
