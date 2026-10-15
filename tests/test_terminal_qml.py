"""QML terminal tests: resources, view models, boundaries. No network.

Qt object tests run headless (no QApplication needed for QObject property
logic). The engine load test uses the offscreen platform.
"""
import ast
import os
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from falcon_terminal import formatting as fmt
from falcon_terminal.models import AssetSnapshot, TerminalSnapshot
from falcon_terminal.theme import TOKENS, Theme
from falcon_terminal.viewmodels import (
    AssetListModel,
    HealthVM,
    NavVM,
    SelectedAssetVM,
    TerminalController,
)

NOW = datetime.now(timezone.utc).isoformat()


def asset(key="binance:BTCUSDT", symbol="BTC/USDT", provider="binance",
          price=81204.21, **kw):
    fields = dict(key=key, symbol=symbol, provider=provider, quote="USDT",
                  price=price, bid=81204.0, ask=81204.5, connected=True,
                  stale=False, messages=10, observed_at=NOW, source="WebSocket",
                  state="CONNECTED")
    fields.update(kw)
    return AssetSnapshot(**fields)


def snapshot(*assets, seq=1):
    return TerminalSnapshot(sequence=seq, generated_at=NOW, assets=tuple(assets))


class FakeBridge:
    """Thread-free stand-in honoring the SnapshotBridge protocol."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.start_calls = 0
        self.stop_calls = 0
        self.join_calls = 0
        self._alive = False
        self._queue = []

    def start(self):
        self.start_calls += 1
        self._alive = True

    def queue(self, snap):
        self._queue.append(snap)

    def poll(self):
        if self._queue:
            return self._queue.pop(0)
        return None

    def stop(self):
        self.stop_calls += 1
        self._alive = False

    def join(self, timeout=None):
        self.join_calls += 1
        return True

    @property
    def is_alive(self):
        return self._alive


def controller_with(*snaps):
    made = []

    def factory(**kwargs):
        bridge = FakeBridge(**kwargs)
        made.append(bridge)
        for s in snaps:
            bridge.queue(s)
        return bridge

    ctrl = TerminalController(bridge_factory=factory)
    ctrl.start()
    return ctrl, made


class ResourceTests(unittest.TestCase):
    def test_qml_resources_resolve(self):
        from falcon_terminal.qml_main import qml_dir
        base = qml_dir()
        for rel in ("main.qml",
                    "components/Panel.qml", "components/MetricCard.qml",
                    "components/MetricBlock.qml", "components/Badge.qml",
                    "components/Sparkline.qml", "components/PriceChart.qml",
                    "components/TimePills.qml", "components/EmptyState.qml",
                    "pages/RadarPage.qml", "pages/SignalsPage.qml",
                    "pages/MemeRadarPage.qml"):
            self.assertTrue((base / rel).is_file(), rel)

    def test_theme_tokens_complete_and_valid(self):
        theme = Theme()
        hex_re = re.compile(r"^#[0-9a-fA-F]{6}$")
        for name, value in TOKENS.items():
            self.assertTrue(theme.property(name) is not None
                            or getattr(theme, name, None) is not None, name)
            if isinstance(value, str) and value.startswith("#"):
                self.assertRegex(value, hex_re, name)
        for spacing in ("s4", "s8", "s12", "s16", "s20", "s24", "s32"):
            self.assertIn(spacing, TOKENS)
        for radius in ("r4", "r6", "r8", "r10"):
            self.assertIn(radius, TOKENS)

    def test_font_family_resolves_without_bundled_files(self):
        from falcon_terminal.theme import system_font_family
        family = system_font_family()
        self.assertTrue(isinstance(family, str) and family)
        self.assertTrue(Theme().fontFamily)
        fonts = [p for p in (ROOT / "falcon_terminal").rglob("*")
                 if p.suffix.lower() in (".ttf", ".otf", ".woff", ".woff2")]
        self.assertEqual(fonts, [])


class FormattingTests(unittest.TestCase):
    def test_number(self):
        self.assertEqual(fmt.number(81204.214489), "81,204.21")
        self.assertEqual(fmt.number(0.000000341215)[:10], "0.00000034")
        self.assertEqual(fmt.number(None), "—")
        self.assertEqual(fmt.number(float("nan")), "—")
        self.assertEqual(fmt.number(float("inf")), "—")

    def test_unavailable_money(self):
        self.assertEqual(fmt.compact_money(None), "—")
        self.assertEqual(fmt.compact_money(-1), "—")
        self.assertEqual(fmt.compact_money(float("nan")), "—")
        self.assertEqual(fmt.compact_money(1834921), "$1.83M")
        self.assertEqual(fmt.compact_money(73400000000), "$73.40B")

    def test_change_and_spread_and_age(self):
        self.assertEqual(fmt.change_text(None), "—")
        self.assertEqual(fmt.change_text(2.345), "+2.35%")
        self.assertEqual(fmt.change_text(-0.5), "-0.50%")
        self.assertEqual(fmt.spread_text(None), "—")
        self.assertEqual(fmt.age_text(None), "—")
        self.assertEqual(fmt.age_text(0.024), "24 ms")


class ViewModelTests(unittest.TestCase):
    def test_price_update_flows_to_selected(self):
        ctrl, _ = controller_with(snapshot(asset(price=81204.21)))
        ctrl._poll()
        self.assertEqual(ctrl.selected.price, "81,204.21")
        self.assertEqual(ctrl.assets.rowCount(), 1)

    def test_up_down_direction_state(self):
        ctrl, made = controller_with()
        ctrl._poll()
        ctrl.selected.select("binance:BTCUSDT")
        made[0].queue(snapshot(asset(price=100.0)))
        ctrl._poll()
        made[0].queue(snapshot(asset(price=101.0)))
        ctrl._poll()
        self.assertEqual(ctrl.selected.direction, 1)
        made[0].queue(snapshot(asset(price=99.0)))
        ctrl._poll()
        self.assertEqual(ctrl.selected.direction, -1)

    def test_model_updates_in_place_without_row_rebuild(self):
        model = AssetListModel()
        model.sync([asset(price=1.0)])
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.count, 1)
        changed = model.sync([asset(price=2.0)])
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.count, 1)
        self.assertIn(0, changed)
        self.assertEqual(model.row("binance:BTCUSDT")["priceText"], "2.00")
        model.clear()
        self.assertEqual(model.count, 0)

    def test_provider_switch_clears_old_state(self):
        ctrl, made = controller_with()
        made[0].queue(snapshot(asset(price=60000.0)))
        ctrl._poll()
        self.assertEqual(ctrl.selected.price, "60,000.00")
        ctrl.applyTargets("ETHUSDT", "")
        self.assertEqual(made[0].stop_calls, 1)
        self.assertEqual(made[0].join_calls, 1)
        self.assertEqual(ctrl.assets.rowCount(), 0)
        self.assertEqual(ctrl.selected.price, "—")
        self.assertEqual(len(made), 2)
        made[1].queue(snapshot(asset(key="binance:ETHUSDT", symbol="ETH/USDT",
                                     price=3000.0)))
        ctrl._poll()
        self.assertEqual(ctrl.selected.price, "3,000.00")

    def test_stale_and_reconnect_propagate(self):
        ctrl, made = controller_with()
        made[0].queue(snapshot(asset(stale=True, state="STALE")))
        ctrl._poll()
        self.assertTrue(ctrl.health.stale)
        self.assertIn("STALE", ctrl.health.text)
        made[0].queue(snapshot(asset(stale=False, state="CONNECTED")))
        ctrl._poll()
        self.assertFalse(ctrl.health.stale)

    def test_unavailable_fields_stay_unavailable(self):
        ctrl, made = controller_with()
        made[0].queue(snapshot(asset(price=None, bid=None, ask=None)))
        ctrl._poll()
        self.assertEqual(ctrl.selected.price, "—")
        self.assertEqual(ctrl.selected.bid, "—")
        self.assertEqual(ctrl.selected.liquidity, "—")
        self.assertEqual(ctrl.selected.candleLine, "—")

    def test_high_frequency_updates_coalesce(self):
        ctrl, made = controller_with()
        for i in range(500):
            made[0].queue(snapshot(asset(price=60000.0 + i), seq=i))
        for _ in range(500):
            ctrl._poll()
        self.assertEqual(ctrl.assets.rowCount(), 1)
        self.assertEqual(ctrl.selected.price, "60,499.00")

    def test_worker_single_ownership_and_clean_shutdown(self):
        ctrl, made = controller_with()
        ctrl.start()
        ctrl.start()
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].start_calls, 1)
        ctrl.stop()
        self.assertEqual(made[0].stop_calls, 1)
        self.assertEqual(made[0].join_calls, 1)
        ctrl.stop()  # idempotent

    def test_navigation_state(self):
        nav = NavVM()
        self.assertEqual(nav.page, "radar")
        nav.go("signals")
        self.assertEqual(nav.page, "signals")
        nav.go("meme")
        self.assertEqual(nav.page, "meme")
        nav.go("nope")
        self.assertEqual(nav.page, "meme")
        for page in ("assets", "paper", "reports", "settings"):
            nav.go(page)
            self.assertEqual(nav.page, page)


class BoundaryTests(unittest.TestCase):
    def qml_sources(self):
        return list((ROOT / "falcon_terminal" / "qml").rglob("*.qml"))

    def test_no_trading_controls_or_actions(self):
        word = re.compile(r"\b(buy|sell|swap|wallet|seed|private_key|apikey|"
                          r"api_key|place_order|market_order|limit_order|"
                          r"connect_wallet)\b", re.IGNORECASE)
        for path in self.qml_sources():
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(word.search(text), f"{path.name} trading word")
        for path in self.qml_sources():
            for match in re.finditer(r"text:\s*\"([^\"]+)\"", path.read_text(encoding="utf-8")):
                label = match.group(1).strip().upper()
                self.assertNotIn(label, {"BUY", "SELL", "SWAP", "ORDER", "TRADE",
                                         "CONNECT WALLET"}, path.name)

    def test_no_wallet_or_key_inputs(self):
        lowered = " ".join(p.read_text(encoding="utf-8").lower()
                           for p in self.qml_sources())
        for token in ("wallet", "seed phrase", "private key", "mnemonic",
                      "apikey", "api_key", "password"):
            self.assertNotIn(token, lowered)
        py = (ROOT / "falcon_terminal" / "viewmodels.py").read_text(encoding="utf-8")
        tree = ast.parse(py)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        roots = {n.split(".")[0] for n in imported}
        self.assertFalse(roots & {"market_data", "execution", "exchange",
                                  "research", "strategy", "risk", "portfolio",
                                  "paper"}, roots)

    def test_terminal_entry_lazy_imports_gui(self):
        source = (ROOT / "falcon_terminal" / "__main__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        top = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top.add(node.module)
        self.assertFalse({n.split(".")[0] for n in top} & {"tkinter", "PySide6"})

    def test_core_cli_without_terminal_extra(self):
        import subprocess
        probe = ("import sys\n"
                 "sys.modules['PySide6'] = None\n"
                 "sys.modules['tkinter'] = None\n"
                 "from scripts.falcon import main\n"
                 "try:\n"
                 "    main(['--help'])\n"
                 "except SystemExit as e:\n"
                 "    sys.exit(e.code)\n")
        proc = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                              text=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        probe2 = probe.replace("main(['--help'])", "main(['terminal', '--help'])")
        proc2 = subprocess.run([sys.executable, "-c", probe2], capture_output=True,
                               text=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(proc2.returncode, 0, proc2.stderr[-2000:])


_APPS = []  # keep QGuiApplication alive while engines exist
_CONTROLLERS = []  # keep view models alive while their engine exists


class EngineLoadTests(unittest.TestCase):
    def _load(self, width=1600, height=900):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtCore import QUrl
        from falcon_terminal.qml_main import ensure_style, qml_dir
        ensure_style()
        from falcon_terminal.theme import Theme
        app = QGuiApplication.instance() or QGuiApplication([])
        _APPS.append(app)  # never let the app die under a live engine
        engine = QQmlApplicationEngine()
        ctrl = TerminalController()
        _CONTROLLERS.append(ctrl)  # models must outlive the assertions
        errors = []
        engine.warnings.connect(lambda ws: errors.extend(w.toString() for w in ws))
        engine.rootContext().setContextProperty("FalconTheme", Theme(engine))
        engine.rootContext().setContextProperty("assets", ctrl.assets)
        engine.rootContext().setContextProperty("selected", ctrl.selected)
        engine.rootContext().setContextProperty("health", ctrl.health)
        engine.rootContext().setContextProperty("nav", ctrl.nav)
        engine.rootContext().setContextProperty("ctrl", ctrl)
        engine.load(QUrl.fromLocalFile(str(qml_dir() / "main.qml")))
        root = engine.rootObjects()[0] if engine.rootObjects() else None
        if root is not None:
            root.setProperty("width", width)
            root.setProperty("height", height)
            QGuiApplication.instance().processEvents()
            QGuiApplication.instance().processEvents()
        return engine, root, errors

    def test_main_qml_loads_without_warnings(self):
        engine, root, errors = self._load()
        self.assertIsNotNone(root)
        self.assertEqual(errors, [])

    def test_startup_geometry(self):
        from falcon_terminal.qml_main import startup_geometry
        w, h, x, y = startup_geometry(1920, 1080)
        self.assertEqual((w, h), (1600, 900))
        self.assertEqual((x, y), (160, 90))
        w, h, x, y = startup_geometry(1600, 900)
        self.assertEqual((w, h), (1504, 846))
        w, h, x, y = startup_geometry(1440, 900)
        self.assertEqual((w, h), (1353, 846))
        w, h, x, y = startup_geometry(1366, 768)
        self.assertEqual((w, h), (1284, 750))
        self.assertTrue(0 <= x <= 1366 - w and 0 <= y <= 768 - h)
        # Smaller than minimum: use all available, never negative/exceeding.
        w, h, x, y = startup_geometry(800, 600)
        self.assertEqual((w, h), (800, 600))
        self.assertGreaterEqual(min(w, h, x, y), 0)
        w, h, x, y = startup_geometry(0, -5)
        self.assertTrue(w > 0 and h > 0 and x >= 0 and y >= 0)
        w, h, x, y = startup_geometry("xx", None)
        self.assertTrue(w > 0 and h > 0 and x >= 0 and y >= 0)

    def test_resolutions_have_no_clipped_panels(self):
        from PySide6.QtGui import QGuiApplication
        expected = {
            "radar": ("RadarPage", ("watchlistPanel", "selectedPanel",
                                    "liveSignalsPanel", "marketNotesPanel")),
            "signals": ("SignalsPage", ("chartPanel", "tradeSetupPanel",
                                        "queuePanel", "aiNotesPanel",
                                        "perfPanel")),
            "meme": ("MemeRadarPage", ("tokenTablePanel", "tokenPanel",
                                       "whyPanel", "insightPanel")),
            "assets": ("AssetsPage", ("assetsTablePanel",)),
        }
        for width, height in ((1920, 1080), (1600, 900), (1440, 900), (1366, 768)):
            engine, root, errors = self._load(width, height)
            self.assertEqual(errors, [], f"{width}x{height}: {errors[:3]}")
            self.assertEqual((root.property("width"), root.property("height")),
                             (width, height))

            def kids(o):
                try:
                    return o.childItems()
                except AttributeError:
                    return o.contentItem().childItems()

            pages = {}

            def find_pages(item):
                name = item.metaObject().className()
                for key, (marker, _) in expected.items():
                    if name.startswith(marker):
                        pages[key] = item
                for ch in kids(item):
                    find_pages(ch)

            find_pages(root)
            self.assertEqual(set(pages), set(expected))
            nav = engine.rootContext().contextProperty("nav")
            for page_id, (_, names) in expected.items():
                nav.go(page_id)
                QGuiApplication.instance().processEvents()
                QGuiApplication.instance().processEvents()
                by_name = {}

                def collect(item):
                    obj = item.objectName()
                    if obj:
                        by_name[obj] = item
                    for ch in kids(item):
                        collect(ch)

                collect(pages[page_id])
                for name in names:
                    self.assertIn(name, by_name, f"{width}x{height}/{page_id}")
                    panel = by_name[name]
                    self.assertTrue(panel.isVisible(), f"{width}x{height}/{name}")
                    self.assertGreater(panel.width(), 0, f"{width}x{height}/{name} w")
                    self.assertGreater(panel.height(), 0, f"{width}x{height}/{name} h")
                    self.assertGreaterEqual(panel.x(), 0, f"{width}x{height}/{name} x")
                    self.assertGreaterEqual(panel.y(), 0, f"{width}x{height}/{name} y")
                    self.assertLessEqual(panel.x() + panel.width(), width,
                                         f"{width}x{height}/{name} right edge")
            self.assertIn("placeholderPanel", self._panel_names(root))
            engine.deleteLater()

    def _panel_names(self, root):
        def kids(o):
            try:
                return o.childItems()
            except AttributeError:
                return o.contentItem().childItems()

        names = []

        def collect(item):
            if item.objectName():
                names.append(item.objectName())
            for ch in kids(item):
                collect(ch)

        collect(root)
        return names


if __name__ == "__main__":
    unittest.main()
