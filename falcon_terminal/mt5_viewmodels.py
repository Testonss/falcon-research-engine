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
    def bannerText(self):
        if self._state == "CONNECTED_DEMO" and self._demo:
            srv = self._server or "MT5"
            return f"MT5 DEMO · {srv} · DEMO mode"
        if self._state == "CONNECTED_REAL_BLOCKED":
            return "MT5 REAL ACCOUNT DETECTED — EXECUTION DISABLED"
        if self._state == "STALE":
            return f"MT5 DEMO · STALE · {self._server}"
        if self._state == "ERROR":
            return f"MT5 ERROR · {self._message}"
        if self._state == "CONNECTING":
            return "MT5 connecting…"
        return "MT5 disconnected"

    @Property(str, notify=changed)
    def balanceText(self): return _money(self._balance)

    @Property(str, notify=changed)
    def equityText(self): return _money(self._equity)

    @Property(str, notify=changed)
    def marginText(self): return _money(self._margin)

    @Property(str, notify=changed)
    def freeMarginText(self): return _money(self._margin_free)

    @Property(str, notify=changed)
    def profitText(self): return _signed(self._profit)

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
                getattr(snap, "profit", None))
        if vals == (self._state, self._server, self._login, self._mode,
                    self._demo, self._exec, self._message, self._balance,
                    self._equity, self._margin, self._margin_free, self._profit):
            return
        (self._state, self._server, self._login, self._mode, self._demo,
         self._exec, self._message, self._balance, self._equity,
         self._margin, self._margin_free, self._profit) = vals
        self.changed.emit()


def _money(v) -> str:
    if v is None:
        return "—"
    try:
        import math
        f = float(v)
        if not math.isfinite(f):
            return "—"
        return f"{f:,.2f}"
    except (TypeError, ValueError):
        return "—"


def _signed(v) -> str:
    if v is None:
        return "—"
    try:
        import math
        f = float(v)
        if not math.isfinite(f):
            return "—"
        sign = "+" if f >= 0 else ""
        return f"{sign}{f:,.2f}"
    except (TypeError, ValueError):
        return "—"


class MT5PositionsModel(QAbstractListModel):
    KeyRole = Qt.UserRole + 1
    _ROLES = ("ticket", "key", "symbol", "side", "volume", "volumeText",
              "entryText", "currentText", "slText", "tpText", "profitText",
              "profit", "openTime", "comment")
    countChanged = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._rows: List[dict] = []
        self._by_ticket: Dict[int, int] = {}

    @Property(int, notify=countChanged)
    def count(self): return len(self._rows)

    def roleNames(self):
        return {self.KeyRole + i: n.encode() for i, n in enumerate(self._ROLES)}

    def rowCount(self, parent=QModelIndex()):
        return len(self._rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        return self._rows[index.row()][self._ROLES[role - self.KeyRole]]

    def sync(self, positions) -> None:
        amap = {}
        for p in positions or ():
            try:
                t = int(getattr(p, "ticket", 0) or 0)
                if t <= 0:
                    continue
                amap[t] = {
                    "ticket": t, "key": str(t),
                    "symbol": str(getattr(p, "symbol", "") or ""),
                    "side": str(getattr(p, "side", "") or ""),
                    "volume": float(getattr(p, "volume", 0) or 0),
                    "volumeText": f"{float(getattr(p, 'volume', 0) or 0):.2f}",
                    "entryText": _money(getattr(p, "entry", None)),
                    "currentText": _money(getattr(p, "current", None)),
                    "slText": _money(getattr(p, "sl", None)),
                    "tpText": _money(getattr(p, "tp", None)),
                    "profitText": _signed(getattr(p, "profit", None)),
                    "profit": float(getattr(p, "profit", 0) or 0),
                    "openTime": str(getattr(p, "open_time", "") or ""),
                    "comment": str(getattr(p, "comment", "") or ""),
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

    def row(self, ticket: int) -> Optional[dict]:
        i = self._by_ticket.get(int(ticket))
        return dict(self._rows[i]) if i is not None else None
