"""Qt ViewModels: the only layer QML talks to.

Backend -> TerminalBridge (worker thread) -> TerminalController.poll (GUI
thread, QTimer) -> AssetListModel / SelectedAssetVM / HealthVM / NavVM ->
QML bindings. No provider, research, strategy or execution imports here.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set

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
        "name": asset.name,
        "mint": asset.mint,
        "chain": asset.chain,
        "venue": asset.venue,
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
        "dex": asset.dex or "",
        "pool": asset.pool or "",
        "favorite": asset.favorite,
        "revision": asset.revision,
        "scoreText": "—",
        "alertsText": "—",
    }


class AssetListModel(QAbstractListModel):
    """Append/update in place; rows are never rebuilt on ticks."""

    KeyRole = Qt.UserRole + 1
    _ROLES = ("key", "symbol", "quote", "provider", "price", "priceText",
              "changeText", "changeUp", "feed", "connected", "stale",
              "messages", "spark", "scoreText", "alertsText",
              "name", "mint", "chain", "venue", "favorite", "revision",
              "dex", "pool")
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

    def sync(self, assets, changed=None) -> dict:
        """Reconcile rows with fresh DTOs. Returns changed row indices.

        When `changed` (snapshot delta keys) is provided, rows outside it
        are left untouched — O(changed) instead of O(N) — while inserts
        and removals are still fully reconciled.
        """
        amap = {a.key: a for a in assets}
        if changed is None:
            return self._sync_full(amap)
        size_before = len(self._rows)
        self._remove_missing(amap)
        touched = {}
        for key in changed:
            asset = amap.get(key)
            if asset is None:
                continue
            row = _row_dict(asset)
            if key in self._by_key:
                i = self._by_key[key]
                if self._rows[i] != row:
                    self._rows[i] = row
                    touched[i] = row
                    top = self.index(i, 0)
                    self.dataChanged.emit(top, top)
            else:
                i = len(self._rows)
                self.beginInsertRows(QModelIndex(), i, i)
                self._rows.append(row)
                self._by_key[key] = i
                self.endInsertRows()
                touched[i] = row
        # Inserts of rows the delta did not mention (e.g. first publish).
        for key, asset in amap.items():
            if key not in self._by_key:
                row = _row_dict(asset)
                i = len(self._rows)
                self.beginInsertRows(QModelIndex(), i, i)
                self._rows.append(row)
                self._by_key[key] = i
                self.endInsertRows()
                touched[i] = row
        if len(self._rows) != size_before:
            self.countChanged.emit()
        return touched

    def _remove_missing(self, amap) -> bool:
        """Drop rows absent from the snapshot, highest index first."""
        doomed = sorted(
            (self._by_key[k] for k in self._by_key if k not in amap),
            reverse=True)
        for i in doomed:
            key = self._rows[i]["key"]
            self._by_key.pop(key, None)
            self.beginRemoveRows(QModelIndex(), i, i)
            self._rows.pop(i)
            self.endRemoveRows()
        if doomed:
            self._by_key = {k: n for n, k in enumerate(r["key"] for r in self._rows)}
        return bool(doomed)

    def _sync_full(self, amap) -> dict:
        changed = {}
        before = len(self._rows)
        self._remove_missing(amap)
        for key, asset in amap.items():
            row = _row_dict(asset)
            if key in self._by_key:
                i = self._by_key[key]
                if self._rows[i] != row:
                    self._rows[i] = row
                    changed[i] = row
                    top = self.index(i, 0)
                    self.dataChanged.emit(top, top)
            else:
                i = len(self._rows)
                self.beginInsertRows(QModelIndex(), i, i)
                self._rows.append(row)
                self._by_key[key] = i
                self.endInsertRows()
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
    def name(self): return self._d.get("name", "")

    @Property(str, notify=changed)
    def mint(self): return self._d.get("mint", "")

    @Property(str, notify=changed)
    def chain(self): return self._d.get("chain", "")

    @Property(str, notify=changed)
    def venue(self): return self._d.get("venue", "")

    @Property(bool, notify=changed)
    def favorite(self): return bool(self._d.get("favorite", False))

    @Property(int, notify=changed)
    def revision(self): return int(self._d.get("revision", 0))

    @Property(str, notify=changed)
    def timeframe(self): return self._d.get("timeframe", "1m")

    @Property('QVariantList', notify=changed)
    def supportedTimeframes(self): return self._d.get("supportedTimeframes", ["1m"])

    @Property(bool, notify=changed)
    def chartLoading(self): return bool(self._d.get("chartLoading", False))

    @Property(str, notify=changed)
    def chartNote(self): return self._d.get("chartNote", "")

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
            # Clear immediately: showing the previous asset's price/bid/pool
            # under a new identity (even for one frame) is cross-asset
            # leakage. Fields repopulate from the new asset's own state.
            self._d = {}
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
            "name": asset.name,
            "mint": asset.mint,
            "chain": asset.chain,
            "venue": asset.venue,
            "favorite": asset.favorite,
            "revision": asset.revision,
            "timeframe": asset.active_timeframe or "1m",
            "supportedTimeframes": list(asset.supported_timeframes or ("1m",)),
            "chartLoading": asset.chart_loading,
            "chartNote": asset.chart_note,
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
    listsChanged = Signal()
    watchedChanged = Signal()

    def __init__(self, symbol: str = "BTCUSDT", solana_mint: str = "",
                 poll_interval: float = 15.0, parent: Optional[QObject] = None,
                 bridge_factory=None, universe: bool = False,
                 track_mints: tuple = (), store_path=None):
        super().__init__(parent)
        from falcon_terminal.store import LocalStore
        from falcon_terminal.timeframes import TIMEFRAMES
        self._TIMEFRAMES = TIMEFRAMES
        self._targets = (symbol, solana_mint or "")
        self._poll_interval = poll_interval
        self._bridge_factory = bridge_factory or TerminalBridge
        self._universe = bool(universe)
        self._track_mints = tuple(track_mints)
        self._store = LocalStore(store_path)
        self._watched: Set[str] = set()
        self._tf_by_key: Dict[str, str] = {}
        self._last_view: tuple = (None, None)
        self._last_providers: List[str] = []
        self._last_chains: List[str] = []
        self._last_quotes: List[str] = []
        self._last_dexes: List[str] = []
        self._bridge = None
        self._timer: Optional[QTimer] = None
        self._switching = False
        self._last_selected_key: str = ""
        self.assets = AssetListModel(self)
        self.selected = SelectedAssetVM(self)
        self.health = HealthVM(self)
        self.nav = NavVM(self)

    # -- discoverable metadata (drives filter combos; grows with providers) ----
    @Property('QVariantList', notify=listsChanged)
    def assetProviders(self): return self._last_providers

    @Property('QVariantList', notify=listsChanged)
    def assetChains(self):
        # Empty chain (centralized venues) displays as CEX in filters.
        return ["CEX" if c == "" else c for c in self._last_chains]

    @Property('QVariantList', notify=listsChanged)
    def assetQuotes(self): return self._last_quotes

    @Property('QVariantList', notify=listsChanged)
    def assetDexes(self): return self._last_dexes

    @Property('QVariantList', notify=listsChanged)
    def recentKeys(self): return list(self._store.recent)

    @Property(int, notify=listsChanged)
    def favoritesCount(self): return len(self._store.favorites)

    @Property('QVariantList', notify=watchedChanged)
    def watchedKeys(self): return sorted(self._watched)

    def _refresh_lists(self, snapshot) -> None:
        providers, chains, quotes, dexes = [], [], [], []
        for a in snapshot.assets:
            if a.provider not in providers:
                providers.append(a.provider)
            if a.chain not in chains:
                chains.append(a.chain)
            if a.quote and a.quote not in quotes:
                quotes.append(a.quote)
            dex = a.dex or ""
            if dex and dex not in dexes:
                dexes.append(dex)
        providers.sort()
        chains.sort()
        quotes.sort()
        dexes.sort()
        if (providers, chains, quotes, dexes) != (
                getattr(self, "_last_providers", None),
                getattr(self, "_last_chains", None),
                getattr(self, "_last_quotes", None),
                getattr(self, "_last_dexes", None)):
            self._last_providers, self._last_chains = providers, chains
            self._last_quotes, self._last_dexes = quotes, dexes
            self.listsChanged.emit()

    @Slot()
    def start(self):
        if self._bridge is not None:
            return
        symbol, mint = self._targets
        kwargs = dict(symbol=symbol, solana_mint=mint,
                      poll_interval=self._poll_interval)
        if self._universe:
            kwargs.update(universe=True, track_mints=self._track_mints)
        try:
            self._bridge = self._bridge_factory(**kwargs)
        except TypeError:
            self._bridge = self._bridge_factory()
        if hasattr(self._bridge, "set_favorites"):
            self._bridge.set_favorites(self._store.favorites)
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

    # -- functional market surface -----------------------------------------------
    @Slot(str)
    def selectAsset(self, key: str):
        """Select a canonical id everywhere (recent + persist + backend)."""
        if not key:
            return
        self.selected.select(key)
        self._on_selection(key)

    def _on_selection(self, key: str) -> None:
        self._store.touch_recent(key)
        self._store.set_selected(key)
        self._store.save()
        self._last_selected_key = key
        self._sync_bridge_view()

    @Slot(str)
    def setTimeframe(self, timeframe: str):
        """Request a chart timeframe (validated; backend fetches honestly)."""
        if timeframe not in self._TIMEFRAMES:
            return
        key = self.selected.key
        if key:
            self._tf_by_key[key] = timeframe
        self._sync_bridge_view()

    @Slot(str, result=bool)
    def toggleFavorite(self, key: str) -> bool:
        """Flip favorite (persisted). Returns the new state."""
        if not key:
            return False
        new_state = self._store.toggle_favorite(key)
        self._store.save()
        bridge = self._bridge
        if bridge is not None and hasattr(bridge, "toggle_favorite"):
            bridge.toggle_favorite(key)
        self.listsChanged.emit()
        return new_state

    @Slot(str)
    def toggleWatch(self, key: str):
        """Local-only watch flag (no backend, no persistence)."""
        if not key:
            return
        if key in self._watched:
            self._watched.discard(key)
        else:
            self._watched.add(key)
        self.watchedChanged.emit()

    @Slot(str, result=str)
    def addMint(self, mint: str) -> str:
        """Track a Solana mint. Returns "" or a human-readable error."""
        bridge = self._bridge
        if bridge is None or not hasattr(bridge, "add_mint"):
            return "market backend not ready"
        return bridge.add_mint(mint or "")

    def _sync_bridge_view(self) -> None:
        bridge = self._bridge
        if bridge is None:
            return
        key = self.selected.key
        tf = self._tf_by_key.get(key, "1m") if key else "1m"
        if (key, tf) == self._last_view:
            return  # already synced; skip redundant backend calls
        self._last_view = (key, tf)
        if hasattr(bridge, "set_selection"):
            bridge.set_selection(key)
        if hasattr(bridge, "set_timeframe"):
            bridge.set_timeframe(tf)

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
        self.assets.sync(snapshot.assets, getattr(snapshot, "changed", None))
        if not self.selected.key and snapshot.assets:
            keys = [a.key for a in snapshot.assets]
            preferred = self._store.selected if self._store.selected in keys else keys[0]
            self.selectAsset(preferred)
        current = next((a for a in snapshot.assets if a.key == self.selected.key), None)
        # Selection follows the user even across provider switches: if the
        # selected id vanished from the feed, keep the last known selection
        # (no cross-asset leakage) instead of jumping to another row.
        self.selected.sync(current)
        # Any selection change — via selectAsset() or a direct QML
        # selected.select() — syncs recent/persisted/backend state exactly once.
        if self.selected.key and self.selected.key != self._last_selected_key:
            self._on_selection(self.selected.key)
        elif getattr(bridge, "_selected_id", None) != self.selected.key:
            self._sync_bridge_view()
        self._refresh_lists(snapshot)
        self.health.sync(snapshot)
