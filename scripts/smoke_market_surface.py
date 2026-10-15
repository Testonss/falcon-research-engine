"""Live market-surface smoke: universe + timeframes + favorites + 1:1 sync.

Runs the REAL stack (Binance public REST/WS, DexScreener polling) headless
via offscreen Qt. Drives the milestone acceptance flow programmatically:

  catalog -> select BTC -> all 6 timeframes -> select SOL -> back to BTC
  -> favorite -> restart -> favorite persists -> 1:1 revision checks.

Exits 0 only if every check passes. Requires public internet.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--mint",
                        default="So11111111111111111111111111111111111111112")
    parser.add_argument("--artifacts", type=Path,
                        default=Path("artifacts") / "ui")
    parser.add_argument("--step-timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine

    from falcon_terminal.qml_main import ensure_style, qml_dir
    ensure_style()
    from falcon_terminal.theme import Theme
    from falcon_terminal.viewmodels import TerminalController

    state_dir = tempfile.mkdtemp(prefix="falcon-smoke-state-")
    store_path = str(Path(state_dir) / "terminal.json")

    app = QGuiApplication([])
    engine = QQmlApplicationEngine()
    controller = TerminalController(
        symbol=args.symbol, solana_mint=args.mint, poll_interval=15.0,
        universe=True, track_mints=(args.mint,), store_path=store_path)
    errors = []
    engine.warnings.connect(lambda ws: errors.extend(w.toString() for w in ws))
    engine.rootContext().setContextProperty("FalconTheme", Theme(engine))
    engine.rootContext().setContextProperty("assets", controller.assets)
    engine.rootContext().setContextProperty("selected", controller.selected)
    engine.rootContext().setContextProperty("health", controller.health)
    engine.rootContext().setContextProperty("nav", controller.nav)
    engine.rootContext().setContextProperty("ctrl", controller)
    engine.load(QUrl.fromLocalFile(str(qml_dir() / "main.qml")))
    if not engine.rootObjects():
        print("smoke: QML failed to load", file=sys.stderr)
        return 1
    window = engine.rootObjects()[0]
    window.setProperty("width", 1600)
    window.setProperty("height", 900)

    btc = f"binance:spot:{args.symbol}"
    sol = f"solana:{args.mint}"
    tfs = ["1m", "5m", "15m", "1h", "4h", "1D"]
    log = {"checks": {}, "one_to_one": []}
    started = time.monotonic()

    def elapsed():
        return round(time.monotonic() - started, 1)

    def row(key):
        return controller.assets.row(key)

    def selected_snapshot():
        sel = controller.selected
        table = controller.assets.row(sel.key) or {}
        return {"key": sel.key, "revision": sel.revision,
                "table_price": table.get("priceText"),
                "selected_price": sel.price,
                "chart_last": (sel.candles[-1][4] if sel.candles else None)}

    def check(name, cond, extra=""):
        log["checks"][name] = bool(cond)
        print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
        return bool(cond)

    def shot(name):
        try:
            args.artifacts.mkdir(parents=True, exist_ok=True)
            path = args.artifacts / name
            window.grabWindow().save(str(path))
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"screenshot failed: {exc}", file=sys.stderr)
            return False

    # Scripted flow runs on the GUI thread via chained single-shots so the
    # 100ms controller poll loop keeps pumping underneath each step.
    plan = []

    def wait_for(predicate, what, timeout=None):
        deadline = time.monotonic() + (timeout or args.step_timeout)
        while time.monotonic() < deadline:
            app.processEvents()
            try:
                if predicate():
                    return True
            except Exception:
                pass
            time.sleep(0.05)
        print(f"TIMEOUT waiting for: {what}", file=sys.stderr)
        return False

    def step_catalog():
        controller.start()
        ok = wait_for(
            lambda: row(btc) is not None and row(btc)["priceText"] != "—",
            "catalog BTC row with price")
        check("catalog_btc_row", ok)
        # The 17MB spot catalog takes several seconds; wait for it.
        ok2 = wait_for(lambda: controller.assets.rowCount() > 2,
                       "registry populated from catalog", timeout=120.0)
        n = controller.assets.rowCount()
        check("registry_count_gt_2", ok2, f"(n={n})")
        check("providers_derived",
              set(controller.assetProviders) >= {"binance"}, "")
        plan_next()

    def step_select_btc():
        controller.selectAsset(btc)
        ok = wait_for(lambda: controller.selected.key == btc
                      and controller.selected.price != "—", "BTC selected")
        check("select_btc", ok)
        plan_next()

    def step_timeframes(idx=0):
        if idx >= len(tfs):
            plan_next()
            return
        tf = tfs[idx]
        controller.setTimeframe(tf)
        if tf == "1m":
            # 1m is always servable from the live provider stream.
            ok = wait_for(lambda: controller.selected.timeframe == "1m",
                          "tf 1m active", timeout=10.0)
        else:
            ok = wait_for(
                lambda: (controller.selected.timeframe == tf)
                and (len(controller.selected.candles) > 0
                     or controller.selected.chartNote != ""),
                f"tf {tf} chart-or-note", timeout=args.step_timeout)
        check(f"timeframe_{tf}", ok)
        entry = selected_snapshot()
        entry["timeframe"] = tf
        log["one_to_one"].append(entry)
        # 1:1: table price and selected price agree at the same revision.
        table = controller.assets.row(btc) or {}
        check(f"sync_1to1_{tf}",
              table.get("priceText") == controller.selected.price
              and table.get("revision") == controller.selected.revision,
              f"(rev={controller.selected.revision})")
        QTimer.singleShot(0, lambda: step_timeframes(idx + 1))

    def step_sol():
        controller.selectAsset(sol)
        ok = wait_for(lambda: controller.selected.key == sol
                      and controller.selected.price != "—", "SOL selected")
        check("select_sol", ok)
        # Old BTC-specific fields must clear: no order book on Solana rows.
        check("sol_no_stale_book",
              controller.selected.bid == "—" and controller.selected.ask == "—")
        check("sol_pool_visible", controller.selected.dex != "—"
              or controller.selected.pool != "—")
        plan_next()

    def step_back_btc_fav():
        controller.selectAsset(btc)
        ok = wait_for(lambda: controller.selected.key == btc
                      and controller.selected.price != "—", "back to BTC")
        check("return_btc", ok)
        fav = controller.toggleFavorite(btc)
        check("favorite_btc", fav is True
              and controller.favoritesCount == 1)
        check("no_qml_warnings", not errors)
        shot("radar_market_surface.png")
        controller.nav.go("assets")
        app.processEvents()
        app.processEvents()
        shot("assets_market_surface.png")
        controller.stop()
        plan_next()

    def step_restart():
        # Favorites load from disk without any network round-trip.
        from falcon_terminal.store import LocalStore
        restored = LocalStore(store_path).favorites
        check("favorite_persists_restart", restored == [btc], str(restored))
        plan_next()

    def plan_next():
        if plan:
            QTimer.singleShot(0, plan.pop(0))

    plan = [step_sol, step_back_btc_fav, step_restart]

    def step_timeframes_entry():
        step_timeframes(0)

    # plan order: catalog -> btc -> timeframes -> sol -> back/fav -> restart
    ordered = [step_catalog, step_select_btc, step_timeframes_entry]
    plan = ordered + plan

    def finish():
        ok = all(log["checks"].values()) and bool(log["checks"])
        print("SMOKE-MARKET", {"ok": ok, "checks": log["checks"],
                               "elapsed_s": elapsed()})
        print("ONE_TO_ONE", json.dumps(log["one_to_one"])[:2000])
        if errors:
            print("qml warnings:", errors[:5], file=sys.stderr)
        app.exit(0 if ok else 1)

    QTimer.singleShot(0, plan_next)
    # Watchdog: bound total runtime (catalog + 6 timeframes + SOL + restart).
    QTimer.singleShot(int(1000 * 60 * 12), lambda: (
        print("WATCHDOG timeout", file=sys.stderr), app.exit(2)))
    # Chain finish after the plan drains: poll for plan emptiness.
    watch = QTimer()
    started_plan = {"n": 0}

    def _watch():
        started_plan["n"] += 1
        if not plan and started_plan["n"] > 20:
            watch.stop()
            finish()

    watch.timeout.connect(_watch)
    watch.start(500)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
