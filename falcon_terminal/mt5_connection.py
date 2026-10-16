"""MT5 connection manager: explicit states, terminal health, demo gating.

States: DISCONNECTED / CONNECTING / CONNECTED_DEMO / CONNECTED_REAL_BLOCKED /
STALE / ERROR. MT5 errors are never silently swallowed: last_error() and a
human-readable message are always retained.

The trade-mode gate is shared with execution/mt5_demo (single authoritative
definition): only trade_mode == 0 (DEMO) enables execution. The manager
itself never sends orders.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from execution.mt5_demo import (
    DemoSafetyError,
    assert_demo_account,
    is_demo_trade_mode,
    redact_account,
    trade_mode_name,
)

logger = logging.getLogger(__name__)

STATES = ("DISCONNECTED", "CONNECTING", "CONNECTED_DEMO",
          "CONNECTED_REAL_BLOCKED", "STALE", "ERROR")

_HUMAN = {
    "MT5 terminal not installed": "MT5 terminal not installed",
    "MT5 terminal not running": "MT5 terminal not running",
    "No account connected": "No account connected",
}


@dataclass
class MT5ConnectionSnapshot:
    state: str = "DISCONNECTED"
    server: str = ""
    login: str = ""
    mode_name: str = ""
    demo: bool = False
    execution_enabled: bool = False
    message: str = "MT5 integration disabled"
    balance: Optional[float] = None
    equity: Optional[float] = None
    margin: Optional[float] = None
    margin_free: Optional[float] = None
    profit: Optional[float] = None
    margin_level: Optional[float] = None
    currency: str = ""


class MT5ConnectionManager:
    """Owns initialize()/account_info() polling. Injectable mt5 for tests."""

    def __init__(self, mt5_module: Any = None, terminal_path: str = "",
                 stale_after_s: float = 30.0,
                 now_fn: Optional[Callable[[], float]] = None):
        self._mt5 = mt5_module
        self.terminal_path = terminal_path or ""
        self.stale_after_s = max(5.0, float(stale_after_s))
        self._now = now_fn or time.monotonic
        self._lock = threading.Lock()
        self._state = "DISCONNECTED"
        self._message = "MT5 integration disabled"
        self._account: Dict[str, Any] = {}
        self._last_ok = 0.0
        self._last_error = ""

    # -- state --------------------------------------------------------------
    @property
    def state(self) -> str:
        with self._lock:
            st = self._state
        # Stale is time-derived: a demo connection gone quiet reports STALE.
        if st == "CONNECTED_DEMO" and self._last_ok:
            if (self._now() - self._last_ok) > self.stale_after_s:
                return "STALE"
        return st

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def _set(self, state: str, message: str = "", account: Optional[Dict] = None,
             error: str = "") -> None:
        assert state in STATES, state
        with self._lock:
            self._state = state
            if message:
                self._message = message
            if account is not None:
                self._account = dict(account)
            if error:
                self._last_error = error

    def _require_mt5(self) -> Any:
        if self._mt5 is not None:
            return self._mt5
        try:
            import MetaTrader5 as mt5
            self._mt5 = mt5
            return mt5
        except Exception as exc:
            self._set("ERROR", "MT5 terminal not installed",
                      error=f"MetaTrader5 import failed: {exc}")
            raise DemoSafetyError("MT5 terminal not installed")

    # -- public ---------------------------------------------------------------
    def connect(self) -> MT5ConnectionSnapshot:
        """Attach to the running terminal; classify DEMO vs REAL-blocked."""
        self._set("CONNECTING", "Connecting to MT5 terminal…")
        try:
            mt5 = self._require_mt5()
        except DemoSafetyError:
            return self.snapshot()
        try:
            kwargs = {"path": self.terminal_path} if self.terminal_path else {}
            ok = mt5.initialize(**kwargs)
        except Exception as exc:
            self._set("ERROR", "MT5 terminal not running", error=str(exc))
            return self.snapshot()
        if not ok:
            try:
                code, msg = mt5.last_error()
                err = f"initialize failed ({code}: {msg})"
            except Exception:
                err = "initialize failed"
            text = "MT5 terminal not running" if "not" in err.lower() else err
            self._set("ERROR", text, error=err)
            return self.snapshot()
        return self.refresh()

    def refresh(self) -> MT5ConnectionSnapshot:
        """Re-read account_info(); never swallows errors silently."""
        if self._mt5 is None:
            try:
                self._require_mt5()
            except DemoSafetyError:
                return self.snapshot()
        mt5 = self._mt5
        try:
            acc = mt5.account_info()
        except Exception as exc:
            self._set("ERROR", "No account connected", error=str(exc))
            return self.snapshot()
        if acc is None:
            try:
                code, msg = mt5.last_error()
                err = f"account_info None ({code}: {msg})"
            except Exception:
                err = "account_info returned None"
            self._set("ERROR", "No account connected", error=err)
            return self.snapshot()
        info = redact_account(acc)
        mode = info.get("trade_mode")
        server = str(info.get("server", "") or "")
        if is_demo_trade_mode(mode):
            self._last_ok = self._now()
            self._set("CONNECTED_DEMO",
                      f"MT5 DEMO connected · {server}",
                      account=info)
        else:
            name = trade_mode_name(mode)
            if mode == 2:
                msg = "MT5 REAL ACCOUNT DETECTED — EXECUTION DISABLED"
            elif mode == 1:
                msg = "MT5 CONTEST account — demo execution disabled"
            else:
                msg = f"MT5 account mode {name} — execution disabled"
            self._set("CONNECTED_REAL_BLOCKED", msg, account=info,
                      error=f"non-demo trade_mode={mode!r}")
        return self.snapshot()

    def disconnect(self) -> None:
        mt5 = self._mt5
        if mt5 is not None:
            try:
                mt5.shutdown()
            except Exception as e:
                logger.debug("mt5 shutdown: %s", e)
        self._set("DISCONNECTED", "MT5 disconnected", account={})

    def assert_demo(self) -> Any:
        """Shared double-guard entry point (raises on non-demo)."""
        if self._mt5 is None:
            self._require_mt5()
        return assert_demo_account(self._mt5)

    def snapshot(self) -> MT5ConnectionSnapshot:
        with self._lock:
            st, msg, acc, err = (self._state, self._message,
                                 dict(self._account), self._last_error)
        # Time-derived staleness without mutating stored state.
        if st == "CONNECTED_DEMO" and self._last_ok and \
                (self._now() - self._last_ok) > self.stale_after_s:
            st = "STALE"
            msg = f"{msg} · STALE (no fresh account read)"
        mode = acc.get("trade_mode")
        demo = is_demo_trade_mode(mode) and st in ("CONNECTED_DEMO", "STALE")
        return MT5ConnectionSnapshot(
            state=st, server=str(acc.get("server", "") or ""),
            login=str(acc.get("login", "") or ""),
            mode_name=trade_mode_name(mode) if mode is not None or acc else "",
            demo=demo, execution_enabled=(st == "CONNECTED_DEMO" and demo),
            message=(f"{msg} · {err}" if st == "ERROR" and err else msg),
            balance=acc.get("balance"), equity=acc.get("equity"),
            margin=acc.get("margin"), margin_free=acc.get("margin_free"),
            profit=acc.get("profit"),
            margin_level=acc.get("margin_level"),
            currency=str(acc.get("currency", "") or ""))

    def terminal_health(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"state": self.state, "error": self.last_error}
        mt5 = self._mt5
        if mt5 is None:
            out.update({"terminal": None, "account": None})
            return out
        try:
            ti = mt5.terminal_info()
            out["terminal"] = ({k: getattr(ti, k) for k in
                                ("company", "name", "connected", "trade_allowed",
                                 "build") if hasattr(ti, k)}
                               if ti is not None else None)
        except Exception as e:
            out["terminal"] = None
            out["terminal_error"] = str(e)
        try:
            out["last_error"] = mt5.last_error()
        except Exception:
            out["last_error"] = None
        out["account"] = dict(self._account)
        return out
