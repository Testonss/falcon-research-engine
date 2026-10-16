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


def _digits_of(asset) -> Optional[int]:
    for attr in ("price_digits", "digits"):
        try:
            v = getattr(asset, attr, None)
            if v is None:
                continue
            d = int(v)
            if 0 <= d <= 10:
                # 0 digits means unknown in legacy metas; treat as unknown
                # only when price_digits is 0 and digits also unknown?
                # Keep explicit 0 (e.g. JPY pairs never 0) — pass through
                # except None; bridge already maps 0 -> None for price_digits.
                return d
        except (TypeError, ValueError):
            continue
    return None


def _row_dict(asset: AssetSnapshot, prev_price: Optional[float] = None) -> dict:
    up = None
    if asset.change_pct_24h is not None:
        up = asset.change_pct_24h >= 0
    # Tick direction vs the previously rendered price (None-safe):
    # 1 = uptick, -1 = downtick, 0 = unchanged/unknown (never flashes).
    direction = 0
    if asset.price is not None and prev_price is not None:
        try:
            if float(asset.price) > float(prev_price):
                direction = 1
            elif float(asset.price) < float(prev_price):
                direction = -1
        except (TypeError, ValueError):
            direction = 0
    digits = getattr(asset, "price_digits", None)
    if digits is None:
        digits = getattr(asset, "digits", None)
    try:
        digits_i = int(digits) if digits is not None else None
        if digits_i is not None and not (0 <= digits_i <= 10):
            digits_i = None
    except (TypeError, ValueError):
        digits_i = None
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
        "priceText": fmt.format_price(asset.price, digits=digits_i),
        "priceDigits": digits_i if digits_i is not None else -1,
        "change": asset.change_pct_24h,
        "changeText": fmt.format_percent(asset.change_pct_24h),
        "changeUp": up,
        "direction": direction,
        "feed": _feed_text(asset),
        "connected": asset.connected,
        "stale": asset.stale,
        "messages": asset.messages,
        "age_s": asset.age_s,
        "ageText": fmt.age_text(asset.age_s),
        "spread_bps": asset.spread_bps,
        "spreadText": fmt.format_spread_bps(asset.spread_bps),
        "volume": asset.volume_5m if asset.volume_5m is not None else (
            asset.volume_24h if getattr(asset, "volume_24h", None) is not None else None),
        "volumeText": (fmt.format_volume(asset.volume_5m)
                       if asset.volume_5m is not None
                       else (fmt.format_volume(getattr(asset, "volume_24h", None))
                             if getattr(asset, "volume_24h", None) is not None else "—")),
        "liquidity": asset.liquidity_usd,
        "liquidityText": fmt.format_volume(asset.liquidity_usd),
        "spark": list(asset.price_history[-SPARK_POINTS:]),
        "dex": asset.dex or "",
        "pool": asset.pool or "",
        "favorite": asset.favorite,
        "revision": asset.revision,
        "searchHay": f"{asset.symbol} {asset.name} {asset.mint}".lower(),
    }


class AssetListModel(QAbstractListModel):
    """Append/update in place; rows are never rebuilt on ticks."""

    KeyRole = Qt.UserRole + 1
    _ROLES = ("key", "symbol", "quote", "provider", "price", "priceText",
              "priceDigits", "change", "changeText", "changeUp", "direction",
              "feed", "connected",
              "stale", "messages", "age_s", "ageText", "spread_bps",
              "spreadText", "volume", "volumeText", "liquidity",
              "liquidityText", "spark",
              "name", "mint", "chain", "venue", "favorite", "revision",
              "dex", "pool", "searchHay")
    countChanged = Signal()
    sortChanged = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._rows: List[dict] = []
        self._by_key: Dict[str, int] = {}
        self._sort_field = "symbol"
        self._sort_asc = True
        self._sort_active = False

    @Property(int, notify=countChanged)
    def count(self):
        return len(self._rows)

    @Property(str, notify=sortChanged)
    def sortField(self): return self._sort_field

    @Property(bool, notify=sortChanged)
    def sortAscending(self): return self._sort_asc

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
        from falcon_terminal import perf as _perf
        t0 = _perf.now()
        size_before = len(self._rows)
        self._remove_missing(amap)
        touched = {}
        for key in changed:
            asset = amap.get(key)
            if asset is None:
                continue
            prev = (self._rows[self._by_key[key]]["price"]
                    if key in self._by_key else None)
            row = _row_dict(asset, prev)
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
        if self._sort_active:
            self._apply_sort(silent=False)
        _perf.global_tracker.measure("registry_to_ui", t0,
                                     f"delta {len(touched)}/{len(amap)}")
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
            prev = (self._rows[self._by_key[key]]["price"]
                    if key in self._by_key else None)
            row = _row_dict(asset, prev)
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
        if self._sort_active:
            self._apply_sort(silent=False)
        return changed

    @Slot(str, bool)
    def sortBy(self, field: str, ascending: bool = True):
        """Local sort (no refetch). Selection + favorites preserved (per-row)."""
        f = (field or "symbol").lower()
        allowed = ("symbol", "price", "change", "provider", "freshness",
                   "volume", "spread")
        if f not in allowed:
            f = "symbol"
        if f == self._sort_field and bool(ascending) == self._sort_asc and self._sort_active:
            return
        self._sort_field = f
        self._sort_asc = bool(ascending)
        self._sort_active = True
        self._apply_sort(silent=False)
        try:
            self.sortChanged.emit()
        except Exception:
            pass

    def _apply_sort(self, silent: bool = False) -> None:
        if not self._rows:
            return
        ordered = fmt.sort_rows(self._rows, self._sort_field, self._sort_asc)
        if [r["key"] for r in ordered] == [r["key"] for r in self._rows]:
            return
        self.layoutAboutToBeChanged.emit()
        self._rows = ordered
        self._by_key = {r["key"]: n for n, r in enumerate(self._rows)}
        self.layoutChanged.emit()
        # dataChanged for all (views refresh sort-dependent highlighting).
        if self._rows:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self._rows) - 1, 0))

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
    def freshness(self): return self._d.get("freshness", "Waiting for data")

    @Property(str, notify=changed)
    def semantics(self): return self._d.get("semantics", "—")

    @Property(bool, notify=changed)
    def isStale(self): return bool(self._d.get("isStale", False))

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
    def fdv(self): return self._d.get("fdv", "—")

    @Property(str, notify=changed)
    def volume24h(self): return self._d.get("volume24h", "—")

    @Property(int, notify=changed)
    def priceDigits(self):
        try:
            v = self._d.get("priceDigits", -1)
            return int(v) if v is not None else -1
        except (TypeError, ValueError):
            return -1

    @Property(float, notify=changed)
    def volumeMin(self):
        v = self._d.get("volumeMin", None)
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @Property(float, notify=changed)
    def volumeMax(self):
        v = self._d.get("volumeMax", None)
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @Property(float, notify=changed)
    def volumeStep(self):
        v = self._d.get("volumeStep", None)
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @Property(float, notify=changed)
    def contractSize(self):
        v = self._d.get("contractSize", None)
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @Property(str, notify=changed)
    def currencyBase(self): return self._d.get("currencyBase", "")

    @Property(str, notify=changed)
    def currencyProfit(self): return self._d.get("currencyProfit", "")

    @Property(bool, notify=changed)
    def isMt5(self): return self._d.get("provider", "") == "mt5"

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
        from falcon_terminal import perf as _perf
        t0 = _perf.now()
        if key != self._key:
            self._key = key
            # Clear immediately: showing the previous asset's price/bid/pool
            # under a new identity (even for one frame) is cross-asset
            # leakage. Fields repopulate from the new asset's own state.
            self._d = {}
            self._last_price = None
            self._direction = 0
            self.changed.emit()
            _perf.global_tracker.measure("asset_click_identity", t0, key)

    def requestTimeframe(self, timeframe: str, supported=()) -> bool:
        """Optimistic pill activation: the UI goes active immediately.

        Returns True when the request was applied locally (caller still
        syncs the backend). Shows cached values when present; the loading
        flag is set so missing data shows a lightweight chart state at
        once instead of leaving the old timeframe appearing active.
        """
        from falcon_terminal import perf as _perf
        t0 = _perf.now()
        if supported and timeframe not in tuple(supported):
            return False
        d = dict(self._d)
        if d.get("timeframe") == timeframe and d.get("chartLoading") is False:
            return True
        d["timeframe"] = timeframe
        # Assume a miss until the backend confirms a cache hit: the chart
        # shows "Loading …" instantly, replaced by real data (or instantly
        # by cache on the next poll). Never fakes candle data.
        d["chartLoading"] = True
        if d != self._d:
            self._d = d
            self.changed.emit()
        _perf.global_tracker.measure("timeframe_active", t0, timeframe)
        return True

    def sync(self, asset: Optional[AssetSnapshot]):
        if asset is None or asset.key != self._key:
            return
        from falcon_terminal import perf as _perf
        t0 = _perf.now()
        had_chart = bool(self._d.get("candles"))
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
            "timeframe": asset.active_timeframe or self._d.get("timeframe", "1m"),
            "supportedTimeframes": list(asset.supported_timeframes or ("1m",)),
            "chartLoading": asset.chart_loading,
            "chartNote": asset.chart_note,
            "freshness": fmt.freshness_text(asset.age_s, asset.stale),
            "semantics": fmt.feed_semantics(asset.provider, asset.source),
            "isStale": bool(asset.stale),
            "provider": asset.provider,
            "source": asset.source,
            "feed": _feed_text(asset),
            "live": bool(asset.connected and not asset.stale and asset.messages),
            "priceDigits": _digits_of(asset),
            "volumeMin": getattr(asset, "volume_min", None),
            "volumeMax": getattr(asset, "volume_max", None),
            "volumeStep": getattr(asset, "volume_step", None),
            "contractSize": getattr(asset, "contract_size", None),
            "currencyBase": getattr(asset, "currency_base", "") or "",
            "currencyProfit": getattr(asset, "currency_profit", "") or "",
            "priceText": fmt.format_price(price, digits=_digits_of(asset)),
            "quote": asset.quote,
            "changeText": fmt.format_percent(asset.change_pct_24h),
            "changeUp": (asset.change_pct_24h or 0) >= 0,
            "hasChange": asset.change_pct_24h is not None,
            "bid": fmt.format_price(asset.bid, digits=_digits_of(asset)),
            "ask": fmt.format_price(asset.ask, digits=_digits_of(asset)),
            "spread": fmt.format_spread_bps(asset.spread_bps),
            "spreadPrice": fmt.format_spread_price(
                (asset.ask - asset.bid) if (asset.ask is not None and asset.bid is not None) else None,
                digits=_digits_of(asset)),
            "liquidity": fmt.format_volume(asset.liquidity_usd),
            "volume": (fmt.format_volume(asset.volume_5m)
                       if asset.volume_5m is not None
                       else (fmt.format_volume(getattr(asset, "volume_24h", None))
                             if getattr(asset, "volume_24h", None) is not None else "—")),
            "volume24h": fmt.format_volume(getattr(asset, "volume_24h", None)),
            "buys": "—" if asset.buys_5m is None else str(asset.buys_5m),
            "sells": "—" if asset.sells_5m is None else str(asset.sells_5m),
            "marketCap": fmt.format_market_cap(asset.market_cap_usd),
            "fdv": fmt.format_market_cap(getattr(asset, "fdv_usd", None)),
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
            _dg = _digits_of(asset)
            d["candleLine"] = (f"O {fmt.format_price(asset.candle_open, digits=_dg)}  "
                               f"H {fmt.format_price(asset.candle_high, digits=_dg)}  "
                               f"L {fmt.format_price(asset.candle_low, digits=_dg)}  "
                               f"C {fmt.format_price(asset.candle_close, digits=_dg)}  "
                               f"V {fmt.format_quantity(asset.candle_volume)}")
            iv = asset.candle_interval or "—"
            d["candleState"] = (f"{iv} · CLOSED" if asset.candle_closed
                                else f"{iv} · Live (forming)")
        pending_tf = self._d.get("timeframe")
        if (pending_tf and self._d.get("chartLoading")
                and asset.active_timeframe
                and asset.active_timeframe != pending_tf):
            # In-flight snapshot predates the user's timeframe click: keep
            # the optimistic pill + loading + previous TF-scoped candles
            # instead of flickering back to the stale timeframe. Live
            # price/candle-line fields above still update honestly.
            d["timeframe"] = pending_tf
            d["chartLoading"] = True
            d["history"] = self._d.get("history", [])
            d["candles"] = self._d.get("candles", [])
        if d != self._d:
            self._d = d
            self.changed.emit()
            if asset.chart_candles and not had_chart:
                _perf.global_tracker.measure(
                    "asset_click_chart", t0, asset.key)

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
        self._summary = "Opening live feeds…"
        self._state = "CONNECTING"
        self._was_connected = False

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

    @Property(str, notify=changed)
    def summary(self): return self._summary

    @Property(str, notify=changed)
    def state(self): return self._state

    def sync(self, snapshot: TerminalSnapshot):
        connected = any(a.connected for a in snapshot.assets)
        stale = any(a.stale for a in snapshot.assets if a.connected)
        messages = sum(a.messages for a in snapshot.assets)
        has_error = any(bool(a.error) or a.state == "ERROR"
                        for a in snapshot.assets)
        # Bounded provider summary for the top bar (no per-instrument age list).
        providers = []
        for a in snapshot.assets:
            if a.provider not in providers:
                providers.append(a.provider)
        summary = fmt.health_summary(providers, connected, stale, max_len=64)
        parts = []
        for a in snapshot.assets:
            label = f"{a.provider}: {_feed_text(a)}"
            if a.error:
                label += f" · {a.error}"
            parts.append(label)
        text = fmt.bound_text(" · ".join(parts) if parts else "Opening live feeds…", 160)
        # Per-instrument freshness lives in rows/selected asset; the top-bar
        # detail is bounded and elided (no endless age list).
        detail = fmt.bound_text(summary, 64)
        state = fmt.connection_state(
            connected, stale,
            "ERROR" if has_error else ("CONNECTED" if connected else "CONNECTING"),
            "error" if has_error else None,
            was_connected=self._was_connected)
        if connected:
            self._was_connected = True
        if (text, connected, stale, messages, detail, summary, state) != (
                self._text, self._connected, self._stale, self._messages,
                self._detail, self._summary, self._state):
            self._text, self._connected, self._stale = text, connected, stale
            self._messages, self._detail = messages, detail
            self._summary = summary
            self._state = state
            self.changed.emit()

    def mark_switching(self, symbol: str, mint: str):
        self._text = f"Switching to {symbol} / {mint[:8]}… — stopping feeds."
        self._connected = False
        self._state = "RECONNECTING"
        self.changed.emit()


class NavVM(QObject):
    changed = Signal()
    PAGES = ("radar", "signals", "meme", "assets", "mt5", "paper", "reports", "settings")

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._page = "radar"

    @Property(str, notify=changed)
    def page(self): return self._page

    @Slot(str)
    def go(self, page: str):
        # Immediate local state switch; backend state (registry, selection,
        # favorites, cached history) is preserved, never rebuilt.
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
                 track_mints: tuple = (), store_path=None,
                 mt5_enabled: bool = False, mt5_sync_s: float = 5.0):
        super().__init__(parent)
        from falcon_terminal.store import LocalStore
        from falcon_terminal.timeframes import TIMEFRAMES
        from falcon_terminal import perf as _perf
        self._created_at = _perf.now()
        self._first_snap = True
        self._TIMEFRAMES = TIMEFRAMES
        self._targets = (symbol, solana_mint or "")
        self._poll_interval = poll_interval
        self._bridge_factory = bridge_factory or TerminalBridge
        self._universe = bool(universe)
        self._track_mints = tuple(track_mints)
        self._store = LocalStore(store_path)
        self._mt5_enabled = bool(mt5_enabled or self._store.mt5_enabled)
        self._mt5_sync_s = max(2.0, float(mt5_sync_s))
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
        from falcon_terminal.mt5_viewmodels import MT5AccountVM as _AccVM
        from falcon_terminal.mt5_viewmodels import MT5PositionsModel as _PosModel
        self.mt5account = _AccVM(self)
        self.mt5positions = _PosModel(self)
        self._mt5_last_error: str = ""
        self._mt5_busy: bool = False

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

    mt5Changed = Signal()

    @Property(bool, notify=mt5Changed)
    def mt5Enabled(self): return self._mt5_enabled

    @Property(bool, notify=mt5Changed)
    def mt5Busy(self): return self._mt5_busy

    @Property(str, notify=mt5Changed)
    def mt5Error(self): return self._mt5_last_error

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
        if self._mt5_enabled:
            kwargs.update(mt5_enabled=True, mt5_sync_s=self._mt5_sync_s)
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
        self.selected.select(key)  # immediate identity + clearing (<100ms)
        self._on_selection(key)

    def _on_selection(self, key: str) -> None:
        self._store.touch_recent(key)
        self._store.set_selected(key)
        self._store.save()
        self._last_selected_key = key
        self._sync_bridge_view()

    @Slot(str)
    def setTimeframe(self, timeframe: str):
        """Request a chart timeframe (validated; backend fetches honestly).

        The pill activates immediately (optimistic local state + loading
        feedback); real candle data follows from cache or the fetcher.
        """
        if timeframe not in self._TIMEFRAMES:
            return
        key = self.selected.key
        if key:
            self._tf_by_key[key] = timeframe
        # Optimistic activation first so the click feels instant (<50ms);
        # supported list comes from the last synced snapshot when known.
        try:
            supported = list(self.selected.supportedTimeframes or ())
        except Exception:
            supported = ()
        if key:
            self.selected.requestTimeframe(
                timeframe, supported or tuple(self._TIMEFRAMES))
        self._sync_bridge_view()

    @Slot(result=str)
    def perfReport(self) -> str:
        """Human-readable timings for the quality pass (no guessing)."""
        from falcon_terminal import perf as _perf
        return _perf.global_tracker.report()

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
        if self._first_snap:
            # First market state after startup (window was already visible;
            # network init never blocks window creation).
            self._first_snap = False
            from falcon_terminal import perf as _perf
            if "app_start" in _perf.global_tracker._marks:
                _perf.global_tracker.measure_since(
                    "startup_first_state", "app_start",
                    f"{len(snapshot.assets)} rows")
            else:
                _perf.global_tracker.measure(
                    "startup_first_state", self._created_at,
                    f"{len(snapshot.assets)} rows")
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
        try:
            self.mt5account.sync(getattr(snapshot, "mt5_account", None))
        except Exception:
            pass
        try:
            self.mt5positions.sync(getattr(snapshot, "mt5_positions", ()))
        except Exception:
            pass

    # -- MT5 DEMO execution (human-gated; worker thread; double-guarded) ----
    def _mt5_exec_adapter(self):
        bridge = self._bridge
        if bridge is None:
            return None
        return getattr(bridge, "_mt5_exec", None)

    def _selected_snapshot(self, canonical_id: str = ""):
        """Live registry state for preview/validation (None-safe)."""
        try:
            bridge = self._bridge
            if bridge is None:
                return None, None
            cid = canonical_id or self.selected.key
            if not cid:
                return None, None
            asset = None
            try:
                # Prefer the last synced snapshot row for display consistency.
                pass
            except Exception:
                pass
            st = bridge.registry.state(cid) if hasattr(bridge, "registry") else None
            meta = bridge.registry.get(cid) if hasattr(bridge, "registry") else None
            return st, meta
        except Exception:
            return None, None

    @Slot(str, str, result=str)
    def sortAssets(self, field: str, ascending: bool = True):
        """Local radar sort (no refetch). Returns "" or error text."""
        try:
            self.assets.sortBy(field or "symbol", bool(ascending))
            return ""
        except Exception as exc:
            return str(exc)

    @Slot(str, result="QVariantMap")
    def mt5SymbolMeta(self, canonical_id: str):
        """Canonical MT5 metadata for QML (no extra symbol_info calls)."""
        try:
            bridge = self._bridge
            if bridge is None or not hasattr(bridge, "registry"):
                return {"ok": False}
            meta = bridge.registry.get(canonical_id or self.selected.key or "")
            if meta is None:
                return {"ok": False}
            return {"ok": True, "digits": meta.digits, "point": meta.point,
                    "contractSize": meta.contract_size,
                    "volumeMin": meta.volume_min, "volumeMax": meta.volume_max,
                    "volumeStep": meta.volume_step,
                    "currencyBase": meta.currency_base or "",
                    "currencyProfit": meta.currency_profit or ""}
        except Exception:
            return {"ok": False}

    @Slot(str, result=str)
    def defaultVolumeText(self, canonical_id: str):
        """Initial volume: persisted value only if valid, else volume_min."""
        from falcon_terminal import formatting as _fmt
        try:
            bridge = self._bridge
            meta = bridge.registry.get(canonical_id or "") if (
                bridge is not None and hasattr(bridge, "registry")) else None
            mn = getattr(meta, "volume_min", None) if meta else None
            mx = getattr(meta, "volume_max", None) if meta else None
            st = getattr(meta, "volume_step", None) if meta else None
            if mn is None:
                # Fall back to the selected VM (last synced snapshot).
                mn = self.selected.volumeMin or None
                mx = self.selected.volumeMax or None
                st = self.selected.volumeStep or None
            persisted = getattr(self._store, "mt5_default_volume", 0.01)
            return _fmt.default_volume_text(persisted, mn, mx, st)
        except Exception:
            return "0.01"

    @Slot(str, result=str)
    def validateVolume(self, text: str):
        """Inline volume validation for the selected MT5 symbol."""
        from falcon_terminal import formatting as _fmt
        mn = self.selected.volumeMin or None
        mx = self.selected.volumeMax or None
        st = self.selected.volumeStep or None
        # When the selected VM has no metadata yet, try the registry.
        try:
            if (not mn) and self._bridge is not None and hasattr(self._bridge, "registry"):
                meta = self._bridge.registry.get(self.selected.key or "")
                if meta is not None:
                    mn, mx, st = meta.volume_min, meta.volume_max, meta.volume_step
        except Exception:
            pass
        return _fmt.validate_volume_text(text, mn, mx, st)

    @Slot(str, str, str, result=str)
    def validateSltp(self, side: str, slText: str, tpText: str):
        """Inline SL/TP validation vs current market (broker authoritative later)."""
        from falcon_terminal import formatting as _fmt
        e1 = _fmt.sltp_text_error(slText, "SL")
        if e1:
            return e1
        e2 = _fmt.sltp_text_error(tpText, "TP")
        if e2:
            return e2
        sl = _fmt.parse_sltp_text(slText)
        tp = _fmt.parse_sltp_text(tpText)
        if sl is None and tp is None:
            return ""
        # Reference price: live registry price preferred.
        ref = None
        try:
            bridge = self._bridge
            if bridge is not None and hasattr(bridge, "registry"):
                st = bridge.registry.state(self.selected.key or "")
                ref = st.price if st else None
        except Exception:
            ref = None
        return _fmt.validate_sltp_side(side, sl, tp, ref)

    @Slot(str, str, str, str, str, result="QVariantMap")
    def orderPreviewDetails(self, canonical_id: str, side: str, volumeText: str,
                            slText: str, tpText: str):
        """Structured order preview for the confirmation dialog."""
        from falcon_terminal import formatting as _fmt
        out: dict = {"ok": False}
        try:
            from falcon_terminal.registry import parse_canonical_id
            parsed = parse_canonical_id(canonical_id)
            symbol = parsed.get("symbol") or parsed.get("ref", "")
            server = parsed.get("server") or self.mt5account.server
        except Exception:
            symbol, server = (canonical_id or "—"), self.mt5account.server
        # Market state (bid/ask/price/spread) from the selected VM when it
        # matches, else from the registry.
        bid_t = ask_t = price_t = spread_t = "—"
        bid = ask = ref = None
        digits = None
        contract = None
        vol_min = vol_max = vol_step = None
        try:
            if self.selected.key == canonical_id:
                bid_t, ask_t = self.selected.bid, self.selected.ask
                price_t = self.selected.price
                spread_t = self.selected.spread
                digits = self.selected.priceDigits
                digits = None if digits == -1 else digits
                contract = self.selected.contractSize or None
                vol_min = self.selected.volumeMin or None
                vol_max = self.selected.volumeMax or None
                vol_step = self.selected.volumeStep or None
                try:
                    bridge = self._bridge
                    if bridge is not None and hasattr(bridge, "registry"):
                        st = bridge.registry.state(canonical_id)
                        if st is not None:
                            bid, ask, ref = st.bid, st.ask, st.price
                except Exception:
                    pass
            else:
                try:
                    bridge = self._bridge
                    if bridge is not None and hasattr(bridge, "registry"):
                        st = bridge.registry.state(canonical_id)
                        meta = bridge.registry.get(canonical_id)
                        if st is not None:
                            bid, ask, ref = st.bid, st.ask, st.price
                        if meta is not None:
                            digits = meta.digits
                            contract = meta.contract_size
                            vol_min, vol_max, vol_step = (
                                meta.volume_min, meta.volume_max, meta.volume_step)
                        bid_t = _fmt.format_price(bid, digits=digits)
                        ask_t = _fmt.format_price(ask, digits=digits)
                        price_t = _fmt.format_price(ref, digits=digits)
                        spread_t = _fmt.format_spread_bps(st.spread_bps) if st else "—"
                except Exception:
                    pass
        except Exception:
            pass
        side_u = (side or "").upper()
        exec_price = ask if side_u == "BUY" else (bid if side_u == "SELL" else ref)
        exec_t = _fmt.format_price(exec_price, digits=digits)
        vol = _fmt.parse_volume_text(volumeText)
        vol_err = _fmt.validate_volume_text(volumeText, vol_min, vol_max, vol_step)
        sl = _fmt.parse_sltp_text(slText)
        tp = _fmt.parse_sltp_text(tpText)
        sl_err = _fmt.sltp_text_error(slText, "SL") if str(slText or "").strip() else ""
        tp_err = _fmt.sltp_text_error(tpText, "TP") if str(tpText or "").strip() else ""
        side_err = _fmt.validate_sltp_side(side_u, sl, tp, ref)
        notional_t = _fmt.format_notional(vol, contract, exec_price)
        out.update({
            "ok": True, "symbol": symbol, "server": server or "—",
            "side": side_u, "volumeText": _fmt.format_lots(vol) if vol else "—",
            "volumeError": vol_err, "slError": sl_err or side_err, "tpError": tp_err,
            "bidText": bid_t, "askText": ask_t, "priceText": price_t,
            "execPriceText": exec_t, "spreadText": spread_t,
            "notionalText": notional_t,
            "slText": _fmt.format_price(sl, digits=digits) if sl else "—",
            "tpText": _fmt.format_price(tp, digits=digits) if tp else "—",
            "slDistance": _fmt.sltp_distance_text(ref, sl, digits=digits),
            "tpDistance": _fmt.sltp_distance_text(ref, tp, digits=digits),
            "demo": True,
        })
        return out

    def _set_mt5_busy(self, busy: bool, error: str = ""):
        self._mt5_busy = busy
        if error:
            self._mt5_last_error = error
        elif not busy:
            pass
        try:
            self.mt5Changed.emit()
        except Exception:
            pass

    @Slot(str, str, "QVariant", "QVariant", "QVariant", result=str)
    def openDemoPosition(self, canonical_id, side, volume, sl, tp):
        """Open an MT5 DEMO position. Returns '' on submit, else error text.

        Runs order_send off the GUI thread (one-shot worker, bounded).
        Requires a DEMO account at submit time AND immediately before send
        (double guard inside the adapter). No automatic execution: QML calls
        this only from the explicit OPEN DEMO POSITION confirmation.
        Volume/SL/TP accept text or numbers; invalid text is rejected
        (never silently parsed to zero).
        """
        from execution.mt5_demo import DemoSafetyError, OrderRequest
        from falcon_terminal import formatting as _fmt
        adapter = self._mt5_exec_adapter()
        if adapter is None:
            err = "MT5 demo execution unavailable (integration disabled)"
            self._set_mt5_busy(False, err)
            return err
        if not self.mt5account.executionEnabled:
            # Fail closed on the UI state too; the adapter re-checks anyway.
            state = self.mt5account.state
            err = ("Real account detected — demo trading disabled"
                   if state == "CONNECTED_REAL_BLOCKED"
                   else "Demo trading is not enabled — connect an MT5 demo account")
            self._set_mt5_busy(False, err)
            return err
        try:
            from falcon_terminal.registry import parse_canonical_id
            parsed = parse_canonical_id(canonical_id)
            if parsed.get("provider") != "mt5":
                raise DemoSafetyError("not an MT5 instrument")
            symbol = parsed.get("symbol") or parsed.get("ref", "")
        except Exception as exc:
            err = f"Invalid MT5 instrument: {exc}"
            self._set_mt5_busy(False, err)
            return err
        if side not in ("BUY", "SELL"):
            return "Order rejected: side must be BUY or SELL"
        vol = _fmt.parse_volume_text(volume)
        if vol is None or vol <= 0:
            return "Order rejected: enter a valid volume (numbers only)"
        sl_v = _fmt.parse_sltp_text(sl) if str(sl or "").strip() not in ("", "None") else None
        tp_v = _fmt.parse_sltp_text(tp) if str(tp or "").strip() not in ("", "None") else None
        # Distinguish "optional empty" from "invalid text": non-empty text
        # that parses to None is invalid (never silently zero).
        # (Volume None already rejected above; SL/TP checked per-field below.)
        for raw, parsed_v, label in ((sl, sl_v, "SL"), (tp, tp_v, "TP")):
            s = str(raw or "").strip()
            if s not in ("", "0", "0.0", "None") and parsed_v is None:
                return f"Order rejected: invalid {label} (must be a positive price)"
        req = OrderRequest(canonical_id=canonical_id, symbol=symbol, side=side,
                           volume=vol, sl=sl_v, tp=tp_v)
        # Reference price + age from the selected snapshot (stale rejected).
        ref_price, age = None, None
        try:
            bridge = self._bridge
            st = bridge.registry.state(canonical_id) if bridge is not None else None
            ref_price = st.price if st else None
            age = st.age_s if st else None
        except Exception:
            pass
        self._set_mt5_busy(True)
        import threading as _th

        def _run():
            try:
                adapter.open_position(req, ref_price, age)
                self._mt5_last_error = ""
            except Exception as exc:
                self._mt5_last_error = str(exc)
            finally:
                self._mt5_busy = False
                try:
                    self.mt5Changed.emit()
                except Exception:
                    pass

        _th.Thread(target=_run, name="FalconMT5Order", daemon=True).start()
        return ""

    @Slot(int, result=str)
    def closeDemoPosition(self, ticket):
        adapter = self._mt5_exec_adapter()
        if adapter is None:
            return "MT5 demo execution unavailable"
        if not self.mt5account.executionEnabled:
            return "Real account detected — demo trading disabled"
        try:
            t = int(ticket)
            if t <= 0:
                return "Position already closed or unknown ticket"
        except (TypeError, ValueError):
            return "Position already closed or unknown ticket"
        self._set_mt5_busy(True)
        import threading as _th

        def _run():
            try:
                adapter.close_position(int(ticket))
                self._mt5_last_error = ""
            except Exception as exc:
                self._mt5_last_error = str(exc)
            finally:
                self._mt5_busy = False
                try:
                    self.mt5Changed.emit()
                except Exception:
                    pass

        _th.Thread(target=_run, name="FalconMT5Close", daemon=True).start()
        return ""

    @Slot(int, "QVariant", "QVariant", result=str)
    def modifyDemoSltp(self, ticket, sl, tp):
        from falcon_terminal import formatting as _fmt
        adapter = self._mt5_exec_adapter()
        if adapter is None:
            return "MT5 demo execution unavailable"
        if not self.mt5account.executionEnabled:
            return "Real account detected — demo trading disabled"
        try:
            t = int(ticket)
            if t <= 0:
                return "Position already closed or unknown ticket"
        except (TypeError, ValueError):
            return "Position already closed or unknown ticket"
        sl_s = str(sl or "").strip() if not isinstance(sl, (int, float)) else str(sl)
        tp_s = str(tp or "").strip() if not isinstance(tp, (int, float)) else str(tp)
        # Empty means "leave unchanged/clear"; non-empty invalid is rejected.
        sl_v = _fmt.parse_sltp_text(sl) if sl_s not in ("", "None") else None
        tp_v = _fmt.parse_sltp_text(tp) if tp_s not in ("", "None") else None
        if sl_s not in ("", "None", "0", "0.0") and sl_v is None and sl_s:
            # Allow explicit "0"/empty to clear; otherwise invalid text rejected.
            if str(sl).strip() not in ("", "0", "0.0"):
                return "Invalid SL (must be a positive price)"
        if tp_s not in ("", "None", "0", "0.0") and tp_v is None and tp_s:
            if str(tp).strip() not in ("", "0", "0.0"):
                return "Invalid TP (must be a positive price)"
        self._set_mt5_busy(True)
        import threading as _th

        def _run(sl_v=sl_v, tp_v=tp_v):
            try:
                adapter.modify_sltp(int(ticket), sl_v, tp_v)
                self._mt5_last_error = ""
            except Exception as exc:
                self._mt5_last_error = str(exc)
            finally:
                self._mt5_busy = False
                try:
                    self.mt5Changed.emit()
                except Exception:
                    pass

        _th.Thread(target=_run, name="FalconMT5Modify", daemon=True).start()
        return ""

    @Slot(str, result=str)
    def demoOrderPreview(self, canonical_id):
        """Human-readable confirmation text (symbol/side/price/server/DEMO)."""
        try:
            from falcon_terminal.registry import parse_canonical_id
            parsed = parse_canonical_id(canonical_id)
            symbol = parsed.get("symbol") or parsed.get("ref", "")
            server = parsed.get("server") or self.mt5account.server
        except Exception:
            symbol, server = canonical_id, self.mt5account.server
        price = self.selected.price if self.selected.key == canonical_id else "—"
        return (f"MT5 DEMO · {symbol} · {price} · server {server or '—'} · "
                f"mode {self.mt5account.modeName or '—'}")

    # -- MT5 settings (functional; passwords never accepted/stored) ---------
    @Slot(bool)
    def setMt5Enabled(self, enabled: bool):
        self._mt5_enabled = bool(enabled)
        self._store.mt5_enabled = bool(enabled)
        self._store.save()
        try:
            self.mt5Changed.emit()
        except Exception:
            pass

    @Slot(float)
    def setMt5SyncInterval(self, seconds: float):
        try:
            v = float(seconds)
            if 2.0 <= v <= 60.0:
                self._mt5_sync_s = v
                self._store.mt5_sync_interval = v
                self._store.save()
        except (TypeError, ValueError):
            pass

    @Slot(float)
    def setMt5DefaultVolume(self, volume: float):
        try:
            v = float(volume)
            if 0.01 <= v <= 100.0:
                self._store.mt5_default_volume = v
                self._store.save()
        except (TypeError, ValueError):
            pass

    @Slot(float)
    def setMt5MaxRisk(self, risk: float):
        try:
            v = float(risk)
            if 1.0 <= v <= 100000.0:
                self._store.mt5_max_risk = v
                self._store.save()
        except (TypeError, ValueError):
            pass

    @Slot(str)
    def setMt5TerminalPath(self, path: str):
        self._store.mt5_terminal_path = path if isinstance(path, str) else ""
        self._store.save()
