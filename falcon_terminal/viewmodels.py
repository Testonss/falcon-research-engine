"""Qt ViewModels: the only layer QML talks to.

Backend -> TerminalBridge (worker thread) -> TerminalController.poll (GUI
thread, QTimer) -> AssetListModel / SelectedAssetVM / HealthVM / NavVM ->
QML bindings. No provider, research, strategy or execution imports here.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    QTimer,
    Signal,
    Slot,
)

from falcon_terminal import formatting as fmt
from falcon_terminal.bridge import TerminalBridge
from falcon_terminal.models import AssetSnapshot, TerminalSnapshot

SPARK_POINTS = 30


def _feed_text(asset: AssetSnapshot) -> str:
    if asset.stale:
        return "STALE" if asset.state == "STALE" else f"{asset.state} / STALE"
    if asset.connected and asset.messages == 0:
        return "WAITING"
    return asset.state


def _row_dict(asset: AssetSnapshot) -> dict:
    up = None
    if asset.change_pct_24h is not None:
        up = asset.change_pct_24h >= 0
    return {
        "key": asset.key,
        "symbol": asset.symbol,
        "quote": asset.quote,
        "provider": asset.provider,
        "price": asset.price if asset.price is not None else 0.0,
        "priceText": fmt.number(asset.price),
        "changeText": fmt.change_text(asset.change_pct_24h),
        "changeUp": up,
        "feed": _feed_text(asset),
        "connected": asset.connected,
        "stale": asset.stale,
        "messages": asset.messages,
        "spark": list(asset.price_history[-SPARK_POINTS:]),
        "scoreText": "—",
        "alertsText": "—",
    }


class AssetListModel(QAbstractListModel):
    """Append/update in place; rows are never rebuilt on ticks."""

    KeyRole = Qt.UserRole + 1
    _ROLES = ("key", "symbol", "quote", "provider", "price", "priceText",
              "changeText", "changeUp", "feed", "connected", "stale",
              "messages", "spark", "scoreText", "alertsText")
    countChanged = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._rows: List[dict] = []
        self._by_key: Dict[str, int] = {}

    @Property(int, notify=countChanged)
    def count(self):
        return len(self._rows)

    def roleNames(self):
        return {self.KeyRole + i: name.encode() for i, name in enumerate(self._ROLES)}

    def rowCount(self, parent=QModelIndex()):
        return len(self._rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        name = self._ROLES[role - self.KeyRole]
        return row[name]

    def sync(self, assets) -> dict:
        """Reconcile rows with fresh DTOs. Returns changed row indices."""
        changed = {}
        seen = set()
        before = len(self._rows)
        for asset in assets:
            seen.add(asset.key)
            row = _row_dict(asset)
            if asset.key in self._by_key:
                i = self._by_key[asset.key]
                if self._rows[i] != row:
                    self._rows[i] = row
                    changed[i] = row
                    top = self.index(i, 0)
                    self.dataChanged.emit(top, top)
            else:
                i = len(self._rows)
                self.beginInsertRows(QModelIndex(), i, i)
                self._rows.append(row)
                self._by_key[asset.key] = i
                self.endInsertRows()
        for key in [k for k in self._by_key if k not in seen]:
            i = self._by_key.pop(key)
            self.beginRemoveRows(QModelIndex(), i, i)
            self._rows.pop(i)
            self.endRemoveRows()
            self._by_key = {k: n for n, k in enumerate(r["key"] for r in self._rows)}
        if len(self._rows) != before:
            self.countChanged.emit()
        return changed

    def clear(self):
        if not self._rows:
            return
        self.beginRemoveRows(QModelIndex(), 0, len(self._rows) - 1)
        self._rows = []
        self._by_key = {}
        self.endRemoveRows()
        self.countChanged.emit()

    def row(self, key: str) -> Optional[dict]:
        i = self._by_key.get(key)
        return dict(self._rows[i]) if i is not None else None


class SelectedAssetVM(QObject):
    changed = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._key = ""
        self._d: dict = {}
        self._direction = 0
        self._last_price: Optional[float] = None

    # -- identity ----------------------------------------------------------
    @Property(str, notify=changed)
    def key(self): return self._key

    @Property(str, notify=changed)
    def symbol(self): return self._d.get("symbol", "—")

    @Property(str, notify=changed)
    def provider(self): return self._d.get("provider", "—")

    @Property(str, notify=changed)
    def source(self): return self._d.get("source", "—")

    @Property(str, notify=changed)
    def feed(self): return self._d.get("feed", "—")

    @Property(bool, notify=changed)
    def live(self): return bool(self._d.get("live", False))

    # -- market ------------------------------------------------------------
    @Property(str, notify=changed)
    def price(self): return self._d.get("priceText", "—")

    @Property(str, notify=changed)
    def quote(self): return self._d.get("quote", "")

    @Property(str, notify=changed)
    def change(self): return self._d.get("changeText", "—")

    @Property(bool, notify=changed)
    def changeUp(self): return bool(self._d.get("changeUp", True))

    @Property(bool, notify=changed)
    def hasChange(self): return self._d.get("hasChange", False)

    @Property(int, notify=changed)
    def direction(self): return self._direction

    @Property(str, notify=changed)
    def bid(self): return self._d.get("bid", "—")

    @Property(str, notify=changed)
    def ask(self): return self._d.get("ask", "—")

    @Property(str, notify=changed)
    def spread(self): return self._d.get("spread", "—")

    @Property(str, notify=changed)
    def liquidity(self): return self._d.get("liquidity", "—")

    @Property(str, notify=changed)
    def volume(self): return self._d.get("volume", "—")

    @Property(str, notify=changed)
    def buys(self): return self._d.get("buys", "—")

    @Property(str, notify=changed)
    def sells(self): return self._d.get("sells", "—")

    @Property(str, notify=changed)
    def marketCap(self): return self._d.get("marketCap", "—")

    @Property(str, notify=changed)
    def pool(self): return self._d.get("pool", "—")

    @Property(str, notify=changed)
    def dex(self): return self._d.get("dex", "—")

    @Property(str, notify=changed)
    def candleLine(self): return self._d.get("candleLine", "—")

    @Property(str, notify=changed)
    def candleState(self): return self._d.get("candleState", "—")

    @Property(str, notify=changed)
    def age(self): return self._d.get("age", "—")

    @Property('QVariantList', notify=changed)
    def history(self): return self._d.get("history", [])

    @Property('QVariantList', notify=changed)
    def candles(self): return self._d.get("candles", [])

    @Slot(str)
    def select(self, key: str):
        if key != self._key:
            self._key = key
            self._last_price = None
            self._direction = 0
            self.changed.emit()

    def sync(self, asset: Optional[AssetSnapshot]):
        if asset is None or asset.key != self._key:
            return
        price = asset.price
        if price is not None and self._last_price is not None and price != self._last_price:
            self._direction = 1 if price > self._last_price else -1
        elif price is None:
            self._direction = 0
        if price is not None:
            self._last_price = price
        d = {
            "symbol": asset.symbol,
            "provider": asset.provider,
            "source": asset.source,
            "feed": _feed_text(asset),
            "live": bool(asset.connected and not asset.stale and asset.messages),
            "priceText": fmt.number(price),
            "quote": asset.quote,
            "changeText": fmt.change_text(asset.change_pct_24h),
            "changeUp": (asset.change_pct_24h or 0) >= 0,
            "hasChange": asset.change_pct_24h is not None,
            "bid": fmt.number(asset.bid),
            "ask": fmt.number(asset.ask),
            "spread": fmt.spread_text(asset.spread_bps),
            "liquidity": fmt.compact_money(asset.liquidity_usd),
            "volume": fmt.compact_money(asset.volume_5m),
            "buys": "—" if asset.buys_5m is None else str(asset.buys_5m),
            "sells": "—" if asset.sells_5m is None else str(asset.sells_5m),
            "marketCap": fmt.compact_money(asset.market_cap_usd),
            "pool": asset.pool or "—",
            "dex": asset.dex or "—",
            "age": fmt.age_text(asset.age_s),
            "history": [float(v) for v in asset.price_history[-240:]],
            "candles": [list(c) for c in asset.chart_candles[-120:]],
        }
        if asset.candle_close is None:
            d["candleLine"] = "—"
            d["candleState"] = "Candle unavailable for this feed"
        else:
            d["candleLine"] = (f"O {fmt.number(asset.candle_open)}  "
                               f"H {fmt.number(asset.candle_high)}  "
                               f"L {fmt.number(asset.candle_low)}  "
                               f"C {fmt.number(asset.candle_close)}  "
                               f"V {fmt.number(asset.candle_volume)}")
            iv = asset.candle_interval or "—"
            d["candleState"] = (f"{iv} · CLOSED" if asset.candle_closed
                                else f"{iv} · FORMING — provisional, not history")
        if d != self._d:
            self._d = d
            self.changed.emit()

    def clear(self):
        self._key = ""
        self._d = {}
        self._direction = 0
        self._last_price = None
        self.changed.emit()


class HealthVM(QObject):
    changed = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._text = "Opening live feeds…"
        self._connected = False
        self._stale = False
        self._messages = 0
        self._detail = ""

    @Property(str, notify=changed)
    def text(self): return self._text

    @Property(bool, notify=changed)
    def connected(self): return self._connected

    @Property(bool, notify=changed)
    def stale(self): return self._stale

    @Property(int, notify=changed)
    def messages(self): return self._messages

    @Property(str, notify=changed)
    def detail(self): return self._detail

    def sync(self, snapshot: TerminalSnapshot):
        connected = any(a.connected for a in snapshot.assets)
        stale = any(a.stale for a in snapshot.assets if a.connected)
        messages = sum(a.messages for a in snapshot.assets)
        parts = []
        for a in snapshot.assets:
            label = f"{a.provider}: {_feed_text(a)}"
            if a.error:
                label += f" · {a.error}"
            parts.append(label)
        text = " · ".join(parts) if parts else "Opening live feeds…"
        detail = "  |  ".join(
            f"{a.symbol}: {fmt.age_text(a.age_s)}" for a in snapshot.assets)
        if (text, connected, stale, messages, detail) != (
                self._text, self._connected, self._stale, self._messages, self._detail):
            self._text, self._connected, self._stale = text, connected, stale
            self._messages, self._detail = messages, detail
            self.changed.emit()

    def mark_switching(self, symbol: str, mint: str):
        self._text = f"Switching to {symbol} / {mint[:8]}… — stopping feeds."
        self._connected = False
        self.changed.emit()


class NavVM(QObject):
    changed = Signal()
    PAGES = ("radar", "signals", "meme", "assets", "paper", "reports", "settings")

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._page = "radar"

    @Property(str, notify=changed)
    def page(self): return self._page

    @Slot(str)
    def go(self, page: str):
        if page in self.PAGES and page != self._page:
            self._page = page
            self.changed.emit()


class TerminalController(QObject):
    errorRaised = Signal(str)

    def __init__(self, symbol: str = "BTCUSDT", solana_mint: str = "",
                 poll_interval: float = 15.0, parent: Optional[QObject] = None,
                 bridge_factory=None):
        super().__init__(parent)
        self._targets = (symbol, solana_mint or "")
        self._poll_interval = poll_interval
        self._bridge_factory = bridge_factory or TerminalBridge
        self._bridge = None
        self._timer: Optional[QTimer] = None
        self._switching = False
        self.assets = AssetListModel(self)
        self.selected = SelectedAssetVM(self)
        self.health = HealthVM(self)
        self.nav = NavVM(self)

    @Slot()
    def start(self):
        if self._bridge is not None:
            return
        symbol, mint = self._targets
        try:
            self._bridge = self._bridge_factory(symbol=symbol, solana_mint=mint,
                                                poll_interval=self._poll_interval)
        except TypeError:
            self._bridge = self._bridge_factory()
        self._bridge.start()
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.setInterval(100)
            self._timer.timeout.connect(self._poll)
        self._timer.start()

    @Slot()
    def stop(self):
        if self._timer is not None:
            self._timer.stop()
        bridge, self._bridge = self._bridge, None
        if bridge is not None:
            bridge.stop()
            bridge.join(timeout=10)

    @Slot(str, str)
    def applyTargets(self, symbol: str, mint: str):
        symbol = (symbol or "").strip().upper() or self._targets[0]
        mint = (mint or "").strip() or self._targets[1]
        if (symbol, mint) == self._targets or self._switching:
            return
        self._switching = True
        self._targets = (symbol, mint)
        self.health.mark_switching(symbol, mint)
        old, self._bridge = self._bridge, None
        if old is not None:
            old.stop()
        self.assets.clear()
        self.selected.clear()
        if old is not None:
            old.join(timeout=10)
        self._switching = False
        self.start()

    def _poll(self):
        bridge = self._bridge
        if bridge is None:
            return
        try:
            snapshot = bridge.poll()
        except Exception as exc:  # never let a poll kill the UI timer
            self.errorRaised.emit(str(exc))
            return
        if snapshot is None:
            return
        self.assets.sync(snapshot.assets)
        if not self.selected.key and snapshot.assets:
            self.selected.select(snapshot.assets[0].key)
        current = next((a for a in snapshot.assets if a.key == self.selected.key), None)
        self.selected.sync(current)
        self.health.sync(snapshot)
