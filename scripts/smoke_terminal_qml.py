"""Live QML smoke: real providers -> QML window -> PNG screenshot.

Headless-friendly: QT_QPA_PLATFORM=offscreen renders in software and
QQuickWindow.grabWindow() captures real pixels. Verifies live data reached
the view models (price set, messages advanced, feed connected) and that the
window closes cleanly. No Tk dependency.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="both",
                        choices=("both", "binance", "solana"))
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--mint", default=None)
    parser.add_argument("--page", default="radar",
                        choices=("radar", "signals", "meme"))
    parser.add_argument("--poll-interval", type=float, default=15.0)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--shot", type=Path, default=None,
                        help="PNG screenshot path (parent dirs created)")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=900)
    args = parser.parse_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine

    from falcon_terminal.qml_main import ensure_style, qml_dir
    ensure_style()
    from falcon_terminal.theme import Theme
    from falcon_terminal.viewmodels import TerminalController

    mint = args.mint or "So11111111111111111111111111111111111111112"
    app = QGuiApplication([])
    engine = QQmlApplicationEngine()
    controller = TerminalController(symbol=args.symbol, solana_mint=mint,
                                    poll_interval=args.poll_interval)
    controller.nav.go(args.page)
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
    window.setProperty("width", args.width)
    window.setProperty("height", args.height)

    state = {"prices": set(), "msgs": 0, "connected": False, "shots": 0}

    def sample():
        sel = controller.selected
        if sel.price != "—":
            state["prices"].add(sel.price)
        state["msgs"] = max(state["msgs"], controller.health.messages)
        state["connected"] = state["connected"] or controller.health.connected

    sampler = QTimer()
    sampler.timeout.connect(sample)
    sampler.start(500)

    def finish():
        sample()
        ok = True
        try:
            if args.shot is not None:
                args.shot.parent.mkdir(parents=True, exist_ok=True)
                image = window.grabWindow()
                image.save(str(args.shot))
                state["shots"] = 1 if args.shot.is_file() else 0
        except Exception as exc:  # noqa: BLE001
            print(f"smoke: screenshot failed: {exc}", file=sys.stderr)
            ok = False
        checks = {
            "window_loaded": True,
            "no_qml_warnings": not errors,
            "live_price_seen": bool(state["prices"]),
            "messages_advanced": state["msgs"] > 0,
            "connected_seen": state["connected"],
            "screenshot_saved": args.shot is None or state["shots"] == 1,
        }
        print("SMOKE", args.page, checks)
        print("prices_seen:", sorted(state["prices"])[:4], "messages:", state["msgs"])
        if errors:
            print("qml warnings:", errors[:5], file=sys.stderr)
        controller.stop()
        app.exit(0 if (ok and all(checks.values())) else 1)

    QTimer.singleShot(int(args.duration * 1000), finish)
    controller.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
