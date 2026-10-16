"""MT5 demo Qt view models: account banner + positions. No trading logic here.

The controller owns the DemoExecutionAdapter calls (off the GUI thread via
the bridge worker where possible); these models only hold immutable
snapshots for QML bindings. Every mutation entry point re-checks DEMO.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    Signal,
    Slot,
)

from falcon_terminal import formatting as fmt


class MT5AccountVM(QObject):
    changed = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._state = "DISCONNECTED"
        self._server = ""
        self._login = ""
        self._mode = ""
        self._demo = False
        self._exec = False
        self._message = "MT5 integration disabled"
        self._balance: Optional[float] = None
        self._equity: Optional[float] = None
        self._margin: Optional[float] = None
        self._margin_free: Optional[float] = None
        self._profit: Optional[float] = None
        self._margin_level: Optional[float] = None
        self._currency: str = ""

    @Property(str, notify=changed)
    def state(self): return self._state

    @Property(str, notify=changed)
    def server(self): return self._server

    @Property(str, notify=changed)
    def login(self): return self._login

    @Property(str, notify=changed)
    def modeName(self): return self._mode

    @Property(bool, notify=changed)
    def isDemo(self): return self._demo

    @Property(bool, notify=changed)
    def executionEnabled(self): return self._exec

    @Property(str, notify=changed)
    def message(self): return self._message

    @Property(str, notify=changed)
    def currency(self): return self._currency

    @Property(str, notify=changed)
    def executionLabel(self):
        if self._state == "CONNECTED_REAL_BLOCKED":
            return "Real account detected — demo trading disabled"
        if self._state == "CONNECTED_DEMO" and self._exec:
            return "Demo trading enabled"
        if self._state == "STALE":
            return "Demo data stale — waiting for refresh"
        if self._state == "ERROR":
            return "MT5 error — see details"
        if self._state == "CONNECTING":
            return "Connecting…"
        return "Execution disabled"

    @Property(str, notify=changed)
    def bannerText(self):
        if self._state == "CONNECTED_DEMO" and self._demo:
            srv = self._server or "MT5"
            return f"MT5 DEMO · {srv}"
        if self._state == "CONNECTED_REAL_BLOCKED":
            return "MT5 REAL ACCOUNT DETECTED — demo trading disabled"
        if self._state == "STALE":
            return f"MT5 DEMO · STALE · {self._server}"
        if self._state == "ERROR":
            return f"MT5 ERROR · {self._message}"
        if self._state == "CONNECTING":
            return "MT5 connecting…"
        return "MT5 disconnected"

    @Property(str, notify=changed)
    def balanceText(self): return fmt.format_money(self._balance, self._currency)

    @Property(str, notify=changed)
    def equityText(self): return fmt.format_money(self._equity, self._currency)

    @Property(str, notify=changed)
    def marginText(self): return fmt.format_money(self._margin, self._currency)

    @Property(str, notify=changed)
    def freeMarginText(self): return fmt.format_money(self._margin_free, self._currency)

    @Property(str, notify=changed)
    def profitText(self): return fmt.format_signed_money(self._profit, self._currency)

    @Property(str, notify=changed)
    def marginLevelText(self): return fmt.format_margin_level(self._margin_level)

    def sync(self, snap) -> None:
        if snap is None:
            return
        vals = (getattr(snap, "state", ""), getattr(snap, "server", ""),
                getattr(snap, "login", ""), getattr(snap, "mode_name", ""),
                bool(getattr(snap, "demo", False)),
                bool(getattr(snap, "execution_enabled", False)),
                getattr(snap, "message", ""),
                getattr(snap, "balance", None), getattr(snap, "equity", None),
                getattr(snap, "margin", None), getattr(snap, "margin_free", None),
                getattr(snap, "profit", None),
                getattr(snap, "margin_level", None),
                str(getattr(snap, "currency", "") or ""))
        if vals == (self._state, self._server, self._login, self._mode,
                    self._demo, self._exec, self._message, self._balance,
                    self._equity, self._margin, self._margin_free, self._profit,
                    self._margin_level, self._currency):
            return
        (self._state, self._server, self._login, self._mode, self._demo,
         self._exec, self._message, self._balance, self._equity,
         self._margin, self._margin_free, self._profit, self._margin_level,
         self._currency) = vals
        self.changed.emit()


def _price(v, digits=None) -> str:
    return fmt.format_price(v, digits=digits)


def _signed_money(v, currency="") -> str:
    return fmt.format_signed_money(v, currency)


class MT5PositionsModel(QAbstractListModel):
    KeyRole = Qt.UserRole + 1
    _ROLES = ("ticket", "key", "symbol", "side", "volume", "volumeText",
              "entryText", "currentText", "slText", "tpText", "profitText",
              "profit", "openTime", "comment", "digits", "exposureText")
    countChanged = Signal()
    aggregateChanged = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._rows: List[dict] = []
        self._by_ticket: Dict[int, int] = {}
        self._aggregate: List[dict] = []

    @Property(int, notify=countChanged)
    def count(self): return len(self._rows)

    @Property('QVariantList', notify=aggregateChanged)
    def aggregate(self): return [dict(a) for a in self._aggregate]

    @Property(str, notify=aggregateChanged)
    def aggregateSummary(self):
        if not self._aggregate:
            return ""
        parts = []
        for a in self._aggregate:
            lots = fmt.format_lots(a.get("total_lots"))
            pl = a.get("floating_pl")
            pl_t = fmt.format_signed_money(pl) if pl is not None else "—"
            parts.append(f"{a.get('symbol')}: {lots} lots · {pl_t}")
        return "  ·  ".join(parts)

    @Property(str, notify=aggregateChanged)
    def totalLotsText(self):
        total = sum(float(a.get("total_lots", 0) or 0) for a in self._aggregate)
        return fmt.format_lots(total) if self._aggregate else "—"

    @Property(str, notify=aggregateChanged)
    def totalProfitText(self):
        if not self._aggregate:
            return "—"
        if any(a.get("floating_pl") is None for a in self._aggregate):
            # Partial unknown: still sum known legs honestly.
            pass
        total = sum(float(a.get("floating_pl", 0) or 0)
                    for a in self._aggregate if a.get("floating_pl") is not None)
        has_any = any(a.get("floating_pl") is not None for a in self._aggregate)
        return fmt.format_signed_money(total) if has_any else "—"

    def roleNames(self):
        return {self.KeyRole + i: n.encode() for i, n in enumerate(self._ROLES)}

    def rowCount(self, parent=QModelIndex()):
        return len(self._rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        return self._rows[index.row()][self._ROLES[role - self.KeyRole]]

    @Slot(int, result="QVariantMap")
    def closePreview(self, ticket: int):
        """Structured close-confirmation payload for QML (no side effects)."""
        row = self.row(int(ticket) if ticket else 0)
        if not row:
            return {"ok": False, "error": "Position already closed or unknown ticket"}
        return {"ok": True, "ticket": row["ticket"], "symbol": row["symbol"],
                "side": row["side"], "volume": row["volume"],
                "volumeText": row["volumeText"], "entryText": row["entryText"],
                "currentText": row["currentText"], "profitText": row["profitText"]}

    def sync(self, positions) -> None:
        amap = {}
        for p in positions or ():
            try:
                t = int(getattr(p, "ticket", 0) or 0)
                if t <= 0:
                    continue
                digits = getattr(p, "digits", None)
                try:
                    digits = int(digits) if digits is not None else None
                    if digits is not None and not (0 <= digits <= 10):
                        digits = None
                except (TypeError, ValueError):
                    digits = None
                cs = getattr(p, "contract_size", None)
                try:
                    cs_f = float(cs) if cs is not None else None
                except (TypeError, ValueError):
                    cs_f = None
                vol = float(getattr(p, "volume", 0) or 0)
                entry = getattr(p, "entry", None)
                cur = getattr(p, "current", None)
                expo = fmt.format_notional(
                    vol, cs_f,
                    cur if cur is not None else entry)
                amap[t] = {
                    "ticket": t, "key": str(t),
                    "symbol": str(getattr(p, "symbol", "") or ""),
                    "side": str(getattr(p, "side", "") or ""),
                    "volume": vol,
                    "volumeText": fmt.format_lots(vol),
                    "entryText": _price(entry, digits),
                    "currentText": _price(cur, digits),
                    "slText": _price(getattr(p, "sl", None), digits),
                    "tpText": _price(getattr(p, "tp", None), digits),
                    "profitText": _signed_money(getattr(p, "profit", None)),
                    "profit": float(getattr(p, "profit", 0) or 0),
                    "openTime": str(getattr(p, "open_time", "") or ""),
                    "comment": str(getattr(p, "comment", "") or ""),
                    "digits": digits if digits is not None else -1,
                    "exposureText": expo,
                }
            except Exception:
                continue
        # Remove closed (terminal authoritative: manual/SL/TP closes vanish).
        doomed = sorted((self._by_ticket[t] for t in self._by_ticket if t not in amap),
                        reverse=True)
        for i in doomed:
            self.beginRemoveRows(QModelIndex(), i, i)
            self._rows.pop(i)
            self.endRemoveRows()
        if doomed:
            self._by_ticket = {r["ticket"]: n for n, r in enumerate(self._rows)}
        changed = False
        for t, row in amap.items():
            if t in self._by_ticket:
                i = self._by_ticket[t]
                if self._rows[i] != row:
                    self._rows[i] = row
                    top = self.index(i, 0)
                    self.dataChanged.emit(top, top)
            else:
                i = len(self._rows)
                self.beginInsertRows(QModelIndex(), i, i)
                self._rows.append(row)
                self._by_ticket[t] = i
                self.endInsertRows()
                changed = True
        if doomed or changed:
            # count may have changed; always safe to emit.
            try:
                self.countChanged.emit()
            except Exception:
                pass
        self._refresh_aggregate_from_positions(positions)

    def _refresh_aggregate_from_positions(self, positions) -> None:
        items = fmt.aggregate_positions(positions)
        # Format for QML consumption.
        formatted = []
        for a in items:
            # Resolve digits per symbol from member rows (first match wins).
            digits = None
            for r in self._rows:
                if r["symbol"] == a["symbol"] and r["digits"] != -1:
                    digits = int(r["digits"])
                    break
            formatted.append({
                "symbol": a["symbol"],
                "count": a["count"],
                "longLots": a["long_lots"],
                "shortLots": a["short_lots"],
                "netLots": a["net_lots"],
                "total_lots": a["total_lots"],
                "longLotsText": fmt.format_lots(a["long_lots"]),
                "shortLotsText": fmt.format_lots(a["short_lots"]),
                "netLotsText": fmt.format_lots(a["net_lots"]),
                "totalLotsText": fmt.format_lots(a["total_lots"]),
                "avgEntryText": fmt.format_price(a["avg_entry"], digits=digits),
                "currentText": fmt.format_price(a["current"], digits=digits),
                "floating_pl": a["floating_pl"],
                "floatingPlText": (fmt.format_signed_money(a["floating_pl"])
                                   if a["floating_pl"] is not None else "—"),
                "exposureText": (fmt.format_money(a["exposure"])
                                 if a["exposure"] is not None else "unavailable"),
            })
        if formatted != self._aggregate:
            self._aggregate = formatted
            try:
                self.aggregateChanged.emit()
            except Exception:
                pass

    def row(self, ticket: int) -> Optional[dict]:
        i = self._by_ticket.get(int(ticket))
        return dict(self._rows[i]) if i is not None else None


def _parse_price_text(text: str) -> Optional[float]:
    try:
        if text is None or text == "—" or text == "unavailable":
            return None
        return float(str(text).replace(",", ""))
    except (TypeError, ValueError):
        return None
