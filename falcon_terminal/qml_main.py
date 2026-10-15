"""QML composition root: backend -> bridge -> view models -> Qt Quick.

Lazy PySide6 import: this module must only load when the QML terminal is
requested, so `falcon --help` works without GUI dependencies.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MINT = "So11111111111111111111111111111111111111112"
MIN_WIDTH, MIN_HEIGHT = 1200, 750
TARGET_WIDTH, TARGET_HEIGHT = 1600, 900

_STYLE_SET = False


def ensure_style() -> None:
    """Select the customizable Controls style exactly once per process.

    QQuickStyle.setStyle() must run before any engine loads Qt Quick
    Controls, and must not be re-applied while engines already exist.
    """
    global _STYLE_SET
    if _STYLE_SET:
        return
    from PySide6.QtQuickControls2 import QQuickStyle
    QQuickStyle.setStyle("Basic")
    _STYLE_SET = True


def startup_geometry(avail_w: int, avail_h: int):
    """Initial window size/position from available work-area geometry.

    Pure function (device-independent pixels, DPI-safe): targets 1600x900
    at 94% of the work area, never larger than the work area, never below
    the practical minimum unless the screen itself is smaller. Returns
    (width, height, x, y) with the window centered. No negative values.
    """
    try:
        aw, ah = float(avail_w), float(avail_h)
    except (TypeError, ValueError):
        aw, ah = float(MIN_WIDTH), float(MIN_HEIGHT)
    if not (aw > 0 and ah > 0):
        aw, ah = float(MIN_WIDTH), float(MIN_HEIGHT)
    w = int(min(TARGET_WIDTH, aw * 0.94))
    h = int(min(TARGET_HEIGHT, ah * 0.94))
    w = max(w, int(min(MIN_WIDTH, aw)))
    h = max(h, int(min(MIN_HEIGHT, ah)))
    w, h = min(w, int(aw)), min(h, int(ah))
    x = int((aw - w) / 2)
    y = int((ah - h) / 2)
    return max(1, w), max(1, h), max(0, x), max(0, y)


def qml_dir() -> Path:
    """Resolve bundled QML without depending on cwd or source checkout."""
    try:
        from importlib.resources import files
        ref = files("falcon_terminal") / "qml" / "main.qml"
        if ref.is_file():
            return ref.parent
    except (ImportError, OSError, ValueError) as exc:
        logger.debug("importlib.resources QML lookup failed: %s", exc)
    here = Path(__file__).resolve().parent / "qml"
    if (here / "main.qml").is_file():
        return here
    raise RuntimeError("Falcon Terminal QML resources not found")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Falcon Terminal (QML): read-only native live market terminal")
    parser.add_argument("--provider", default="both",
                        choices=("both", "binance", "solana"))
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--mint", default=None)
    parser.add_argument("--solana-mint", default=None)
    parser.add_argument("--page", default="radar",
                        choices=("radar", "signals", "meme"))
    parser.add_argument("--poll-interval", type=float, default=15.0)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not 2 <= args.poll_interval <= 3600:
        print("falcon-terminal: error: --poll-interval must be 2..3600",
              file=sys.stderr)
        return 2
    mint = args.mint or args.solana_mint or DEFAULT_MINT

    try:
        from PySide6.QtCore import Qt, QUrl, QSize
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
    except ImportError:
        print("falcon-terminal: error: PySide6 is required for the QML terminal.\n"
              'Install it with: pip install ".[terminal]"\n'
              "Fallback (no extra deps): falcon-terminal --ui tk",
              file=sys.stderr)
        return 1

    # Basic style: the only Qt Quick Controls style that honors custom
    # backgrounds/content items (native styles warn and ignore them).
    ensure_style()

    from falcon_terminal.theme import Theme
    from falcon_terminal.viewmodels import TerminalController

    if os.environ.get("FALCON_QML_OFFSCREEN", "") == "1":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")

    app = QGuiApplication(sys.argv[:1])
    engine = QQmlApplicationEngine()
    try:
        area = app.primaryScreen().availableGeometry()
        win_w, win_h, win_x, win_y = startup_geometry(area.width(), area.height())
    except Exception as exc:
        logger.warning("screen geometry unavailable (%s); using fallback", exc)
        win_w, win_h, win_x, win_y = 1280, 800, 60, 40
    controller = TerminalController(symbol=args.symbol, solana_mint=mint,
                                    poll_interval=args.poll_interval)
    controller.nav.go(args.page)
    engine.rootContext().setContextProperty("FalconTheme", Theme(engine))
    engine.rootContext().setContextProperty("assets", controller.assets)
    engine.rootContext().setContextProperty("selected", controller.selected)
    engine.rootContext().setContextProperty("health", controller.health)
    engine.rootContext().setContextProperty("nav", controller.nav)
    engine.rootContext().setContextProperty("ctrl", controller)

    errors = []

    def _on_warnings(items):
        errors.extend(w.toString() for w in items)

    engine.warnings.connect(_on_warnings)
    qml_path = qml_dir()
    engine.addImportPath(str(qml_path.parent))
    engine.load(QUrl.fromLocalFile(str(qml_path / "main.qml")))
    if not engine.rootObjects():
        print("falcon-terminal: error: QML failed to load:\n" + "\n".join(errors[:20]),
              file=sys.stderr)
        controller.stop()
        return 1
    if errors:
        logger.warning("QML warnings:\n%s", "\n".join(errors[:20]))
    window = engine.rootObjects()[0]
    try:
        window.setProperty("width", win_w)
        window.setProperty("height", win_h)
        window.setProperty("x", win_x)
        window.setProperty("y", win_y)
        window.setMinimumSize(QSize(min(MIN_WIDTH, win_w), min(MIN_HEIGHT, win_h)))
    except Exception as exc:
        logger.warning("initial geometry failed (%s); using QML defaults", exc)

    controller.start()
    app.aboutToQuit.connect(controller.stop)
    try:
        return app.exec()
    except KeyboardInterrupt:
        controller.stop()
        return 0
