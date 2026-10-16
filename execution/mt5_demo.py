"""MT5 DEMO-only execution adapter.

Dedicated execution component — never mixed into the market-data provider.

Safety contract (NON-NEGOTIABLE, fail closed):
  * mt5.account_info().trade_mode == 0 (DEMO) is authoritative.
  * REAL (2), CONTEST (1), UNKNOWN/None/unreadable -> execution disabled.
  * Double guard: demo is checked (A) when enabling the adapter AND
    (B) immediately before EVERY mt5.order_send() call.
  * No override, no "proceed anyway", no safety disable.

All mutating methods re-check demo mode. All validation rejects locally
before order_send; MT5 order_check is used as a pre-check where available.
No passwords are stored or logged.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

TRADE_MODE_DEMO = 0
TRADE_MODE_CONTEST = 1
TRADE_MODE_REAL = 2


class DemoSafetyError(RuntimeError):
    """Raised whenever demo-only execution cannot be proven safe."""


def trade_mode_name(mode: Any) -> str:
    return {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(mode, f"UNKNOWN({mode!r})")


def is_demo_trade_mode(mode: Any) -> bool:
    """Only integer 0 (official DEMO) counts. Everything else is blocked."""
    return mode is not None and mode == TRADE_MODE_DEMO


def assert_demo_account(mt5_module: Any) -> Any:
    """Fail-closed demo gate. Returns account_info on DEMO, else raises.

    Never infer DEMO from server name, balance, UI label or user assertion.
    """
    if mt5_module is None:
        raise DemoSafetyError("MT5 module unavailable — execution disabled")
    try:
        account = mt5_module.account_info()
    except Exception as exc:
        raise DemoSafetyError(f"MT5 account unreadable — execution disabled ({exc})")
    if account is None:
        raise DemoSafetyError("MT5 account unreadable (None) — execution disabled")
    mode = getattr(account, "trade_mode", None)
    if not is_demo_trade_mode(mode):
        raise DemoSafetyError(
            f"MT5 account is {trade_mode_name(mode)} — DEMO execution only; blocked")
    return account


def redact_account(account: Any) -> Dict[str, Any]:
    """Account display state without secrets. Login shown, password never."""
    if account is None:
        return {}
    out = {}
    for k in ("login", "server", "trade_mode", "balance", "equity", "margin",
              "margin_free", "margin_level", "profit", "currency", "company"):
        try:
            v = getattr(account, k, None) if not isinstance(account, dict) else account.get(k)
        except Exception:
            v = None
        if v is not None:
            out[k] = v
    out["mode_name"] = trade_mode_name(out.get("trade_mode"))
    return out


@dataclass
class MT5Position:
    ticket: int
    symbol: str
    canonical_id: str = ""
    side: str = ""          # "BUY" | "SELL"
    volume: float = 0.0
    entry: float = 0.0
    current: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    profit: Optional[float] = None
    swap: Optional[float] = None
    magic: int = 0
    comment: str = ""
    open_time: Optional[str] = None


def parse_position(pos: Any, server: str = "") -> Optional[MT5Position]:
    """Normalize mt5.positions_get() row -> MT5Position. None on malformed."""
    try:
        def _g(name, default=None):
            try:
                v = getattr(pos, name) if not isinstance(pos, dict) else pos.get(name, default)
            except Exception:
                return default
            return default if v is None else v
        ticket = _g("ticket")
        symbol = str(_g("symbol", "") or "").strip()
        if ticket is None or not symbol:
            return None
        try:
            ticket_i = int(ticket)
        except (TypeError, ValueError):
            return None
        ptype = _g("type", 0)
        # MT5 POSITION_TYPE_BUY=0, SELL=1.
        side = "BUY" if int(ptype or 0) == 0 else "SELL"
        def _f(v):
            try:
                f = float(v)
                return f if math.isfinite(f) else None
            except (TypeError, ValueError):
                return None
        vol = _f(_g("volume", 0)) or 0.0
        entry = _f(_g("price_open", _g("price_current", 0))) or 0.0
        current = _f(_g("price_current"))
        sl = _f(_g("sl", 0)) or None
        tp = _f(_g("tp", 0)) or None
        if sl == 0:
            sl = None
        if tp == 0:
            tp = None
        t = _g("time", _g("time_msc"))
        open_iso: Optional[str] = None
        try:
            if isinstance(t, (int, float)) and t > 0:
                secs = float(t) / 1000.0 if t > 4102444800 else float(t)
                open_iso = datetime.fromtimestamp(secs, tz=timezone.utc).isoformat()
            elif isinstance(t, datetime):
                open_iso = (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()
        except Exception:
            open_iso = None
        cid = f"mt5:{server}:{symbol}" if server else ""
        return MT5Position(ticket=ticket_i, symbol=symbol, canonical_id=cid, side=side,
                           volume=vol, entry=entry, current=current, sl=sl, tp=tp,
                           profit=_f(_g("profit")), swap=_f(_g("swap")),
                           magic=int(_g("magic", 0) or 0),
                           comment=str(_g("comment", "") or ""),
                           open_time=open_iso)
    except Exception:
        return None


@dataclass
class DemoRiskConfig:
    max_risk_per_trade: float = 100.0      # account-currency estimate cap
    max_position_size: float = 1.0         # lots
    max_concurrent_positions: int = 5
    max_daily_loss: float = 500.0
    min_stop_distance: float = 0.0         # price units; 0 = broker default
    reject_stale_s: float = 30.0
    default_volume: float = 0.01
    deviation_points: int = 20


@dataclass
class OrderRequest:
    canonical_id: str
    symbol: str
    side: str                 # "BUY" | "SELL"
    volume: float
    sl: Optional[float] = None
    tp: Optional[float] = None
    deviation: int = 20
    comment: str = "falcon-demo"


def normalize_volume(volume: float, vol_min: float, vol_max: float,
                      vol_step: float) -> float:
    """Floor to the broker step, clamp to [min, max]. Raises on invalid."""
    try:
        v = float(volume)
    except (TypeError, ValueError):
        raise DemoSafetyError(f"invalid volume: {volume!r}")
    if not math.isfinite(v) or v <= 0:
        raise DemoSafetyError(f"invalid volume (must be > 0): {volume!r}")
    step = float(vol_step or 0) or 0.0
    if step > 0:
        v = math.floor(v / step) * step
    if vol_max and v > float(vol_max):
        v = float(vol_max)
    if vol_min and v < float(vol_min):
        raise DemoSafetyError(
            f"volume {v} below broker minimum {vol_min}")
    # Round to step precision to avoid float dust.
    if step > 0:
        try:
            import decimal
            q = decimal.Decimal(str(step))
            v = float(decimal.Decimal(str(v)).quantize(q))
        except Exception:
            v = round(v, 8)
    if v <= 0:
        raise DemoSafetyError("volume normalized to zero — rejected")
    return v


class DemoExecutionAdapter:
    """DEMO-only MT5 execution. Every mutating method re-checks demo mode."""

    def __init__(self, mt5_module: Any = None, server: str = "",
                 risk: Optional[DemoRiskConfig] = None,
                 daily_loss_tracker: Optional[Callable[[], float]] = None):
        self._mt5 = mt5_module
        self.server = (server or "").strip()
        self.risk = risk or DemoRiskConfig()
        self._daily_loss = daily_loss_tracker or (lambda: 0.0)
        self._enabled = False
        self._last_error: str = ""
        self._open_results: List[Dict[str, Any]] = []

    # -- enablement (guard A) --------------------------------------------------
    def enable(self, mt5_module: Any = None) -> Dict[str, Any]:
        """Verify DEMO and enable. Raises DemoSafetyError otherwise."""
        if mt5_module is not None:
            self._mt5 = mt5_module
        account = assert_demo_account(self._mt5)  # guard A
        self._enabled = True
        info = redact_account(account)
        logger.info("MT5 DEMO execution enabled (server=%s login=%s)",
                    info.get("server"), info.get("login"))
        return info

    @property
    def enabled(self) -> bool:
        return self._enabled

    def disable(self, reason: str = "") -> None:
        self._enabled = False
        if reason:
            self._last_error = reason

    def _require_enabled_demo(self) -> Any:
        if not self._enabled:
            raise DemoSafetyError("MT5 demo execution not enabled")
        return assert_demo_account(self._mt5)  # guard B (every mutation)

    @property
    def last_error(self) -> str:
        return self._last_error

    # -- read paths (no guard needed, but never expose secrets) ---------------
    def account_snapshot(self) -> Dict[str, Any]:
        if self._mt5 is None:
            return {"error": "MT5 module unavailable"}
        try:
            acc = self._mt5.account_info()
        except Exception as e:
            return {"error": f"account unreadable: {e}"}
        if acc is None:
            return {"error": "no account connected"}
        snap = redact_account(acc)
        try:
            mode = getattr(acc, "trade_mode", None) if not isinstance(acc, dict) else acc.get("trade_mode")
            snap["demo"] = is_demo_trade_mode(mode)
        except Exception:
            snap["demo"] = False
        return snap

    def get_positions(self) -> List[MT5Position]:
        """Actual terminal positions (authoritative). Empty on failure."""
        if self._mt5 is None:
            return []
        try:
            raw = self._mt5.positions_get()
        except Exception as e:
            self._last_error = f"positions_get failed: {e}"
            return []
        if not raw:
            return []
        out = []
        for p in raw:
            parsed = parse_position(p, self.server)
            if parsed is not None:
                out.append(parsed)
        return out

    def symbol_rules(self, symbol: str) -> Dict[str, Any]:
        if self._mt5 is None:
            raise DemoSafetyError("MT5 module unavailable")
        try:
            info = self._mt5.symbol_info(symbol)
        except Exception as e:
            raise DemoSafetyError(f"symbol info failed for {symbol}: {e}")
        if info is None:
            raise DemoSafetyError(f"symbol not available: {symbol}")
        def _g(name, default=None):
            try:
                v = getattr(info, name) if not isinstance(info, dict) else info.get(name, default)
            except Exception:
                return default
            return default if v is None else v
        return {"symbol": symbol,
                "visible": bool(_g("visible", True)),
                "trade_mode": _g("trade_mode"),
                "volume_min": float(_g("volume_min", 0) or 0),
                "volume_max": float(_g("volume_max", 0) or 0),
                "volume_step": float(_g("volume_step", 0) or 0),
                "point": float(_g("point", 0) or 0),
                "digits": int(_g("digits", 0) or 0),
                "contract_size": float(_g("contract_size", 0) or 0)}

    # -- validation -------------------------------------------------------------
    def validate_order(self, req: OrderRequest, ref_price: Optional[float] = None,
                       tick_age_s: Optional[float] = None) -> Dict[str, Any]:
        """Local validation. Returns normalized request dict or raises."""
        if not req.symbol or not req.symbol.strip():
            raise DemoSafetyError("order rejected: empty symbol")
        if req.side not in ("BUY", "SELL"):
            raise DemoSafetyError(f"order rejected: bad side {req.side!r}")
        rules = self.symbol_rules(req.symbol)
        if not rules.get("visible", True):
            # Try to select it; some brokers hide symbols until selected.
            try:
                self._mt5.symbol_select(req.symbol, True)
            except Exception:
                pass
        volume = normalize_volume(req.volume, rules["volume_min"],
                                  rules["volume_max"], rules["volume_step"])
        if volume > self.risk.max_position_size:
            raise DemoSafetyError(
                f"order rejected: volume {volume} exceeds max demo size {self.risk.max_position_size}")
        # Concurrent-position cap.
        try:
            n_open = len(self.get_positions())
        except Exception:
            n_open = 0
        if n_open >= self.risk.max_concurrent_positions:
            raise DemoSafetyError(
                f"order rejected: {n_open} open (max {self.risk.max_concurrent_positions})")
        # Daily-loss cap.
        try:
            day_loss = float(self._daily_loss())
        except Exception:
            day_loss = 0.0
        if day_loss <= -abs(self.risk.max_daily_loss):
            raise DemoSafetyError(
                f"order rejected: daily demo loss limit reached ({day_loss})")
        # Stale feed.
        if tick_age_s is not None and tick_age_s > self.risk.reject_stale_s:
            raise DemoSafetyError(
                f"order rejected: stale market data ({tick_age_s:.1f}s > {self.risk.reject_stale_s}s)")
        # SL/TP sanity vs reference price.
        if ref_price and ref_price > 0:
            if req.sl is not None:
                if req.side == "BUY" and req.sl >= ref_price:
                    raise DemoSafetyError("order rejected: BUY SL must be below price")
                if req.side == "SELL" and req.sl <= ref_price:
                    raise DemoSafetyError("order rejected: SELL SL must be above price")
            if req.tp is not None:
                if req.side == "BUY" and req.tp <= ref_price:
                    raise DemoSafetyError("order rejected: BUY TP must be above price")
                if req.side == "SELL" and req.tp >= ref_price:
                    raise DemoSafetyError("order rejected: SELL TP must be below price")
            if req.sl is not None and self.risk.min_stop_distance > 0:
                if abs(ref_price - req.sl) < self.risk.min_stop_distance:
                    raise DemoSafetyError("order rejected: stop too close to price")
        # Estimated risk cap (crude: |price - SL| * volume * contract).
        est_risk = None
        if ref_price and req.sl:
            try:
                est_risk = abs(float(ref_price) - float(req.sl)) * float(volume) * float(rules.get("contract_size") or 1.0)
                if est_risk > self.risk.max_risk_per_trade:
                    raise DemoSafetyError(
                        f"order rejected: estimated risk {est_risk:.2f} exceeds max {self.risk.max_risk_per_trade:.2f}")
            except DemoSafetyError:
                raise
            except Exception:
                est_risk = None
        if req.deviation is not None and (int(req.deviation) < 0 or int(req.deviation) > 1000):
            raise DemoSafetyError("order rejected: bad deviation")
        return {"symbol": req.symbol, "side": req.side, "volume": volume,
                "sl": req.sl, "tp": req.tp, "deviation": int(req.deviation),
                "est_risk": est_risk, "contract_size": rules.get("contract_size")}

    def build_request(self, norm: Dict[str, Any], price: float) -> Dict[str, Any]:
        """Build the mt5.order_send dict (market order). No send here."""
        side = norm["side"]
        try:
            ORDER_TYPE_BUY = getattr(self._mt5, "ORDER_TYPE_BUY", 0)
            ORDER_TYPE_SELL = getattr(self._mt5, "ORDER_TYPE_SELL", 1)
            TRADE_ACTION_DEAL = getattr(self._mt5, "TRADE_ACTION_DEAL", 1)
            FILLING = getattr(self._mt5, "ORDER_FILLING_IOC", 1)
        except Exception:
            ORDER_TYPE_BUY, ORDER_TYPE_SELL, TRADE_ACTION_DEAL, FILLING = 0, 1, 1, 1
        return {"action": TRADE_ACTION_DEAL,
                "symbol": norm["symbol"],
                "volume": float(norm["volume"]),
                "type": ORDER_TYPE_BUY if side == "BUY" else ORDER_TYPE_SELL,
                "price": float(price),
                "sl": float(norm["sl"]) if norm.get("sl") else 0.0,
                "tp": float(norm.get("tp")) if norm.get("tp") else 0.0,
                "deviation": int(norm.get("deviation", 20)),
                "magic": 20261016,
                "comment": "falcon-demo",
                "type_time": getattr(self._mt5, "ORDER_TIME_GTC", 0),
                "type_filling": FILLING}

    # -- mutating paths (each re-checks demo immediately before send) -----------
    def _check_precheck(self, request: Dict[str, Any]) -> None:
        check = getattr(self._mt5, "order_check", None)
        if check is None:
            return
        try:
            res = check(request)
        except Exception as e:
            raise DemoSafetyError(f"order pre-check failed: {e}")
        if res is None:
            raise DemoSafetyError("order pre-check returned no result — rejected")
        retcode = getattr(res, "retcode", 0) if not isinstance(res, dict) else res.get("retcode", 0)
        if int(retcode or 0) != 0:
            comment = getattr(res, "comment", "") if not isinstance(res, dict) else res.get("comment", "")
            raise DemoSafetyError(f"order_check rejected (retcode={retcode}): {comment}")

    @staticmethod
    def humanize_retcode(retcode: Any, comment: Any = "") -> str:
        table = {0: "OK", 10004: "requote", 10006: "rejected by server",
                 10007: "cancelled", 10009: "order placed", 10013: "invalid request",
                 10014: "invalid volume", 10015: "invalid price", 10016: "invalid stops",
                 10017: "trade disabled", 10018: "market closed", 10019: "no money",
                 10020: "price changed", 10021: "no quotes", 10022: "invalid expiration",
                 10024: "too frequent", 10027: "autotrading disabled",
                 10030: "unsupported filling"}
        base = table.get(int(retcode or -1), f"retcode={retcode}")
        return f"{base} ({comment})" if comment else base

    def open_position(self, req: OrderRequest, ref_price: Optional[float] = None,
                      tick_age_s: Optional[float] = None) -> Dict[str, Any]:
        """Validate -> pre-check -> DEMO re-check -> order_send. Human-gated only."""
        self._require_enabled_demo()  # guard B (pre-validation)
        norm = self.validate_order(req, ref_price, tick_age_s)
        # Reference price: live tick preferred.
        price = float(ref_price or 0) or 0.0
        if not price:
            try:
                tick = self._mt5.symbol_info_tick(req.symbol)
                bid = getattr(tick, "bid", None) if not isinstance(tick, dict) else tick.get("bid")
                ask = getattr(tick, "ask", None) if not isinstance(tick, dict) else tick.get("ask")
                price = float(ask if req.side == "BUY" else bid)
            except Exception as e:
                raise DemoSafetyError(f"no reference price for {req.symbol}: {e}")
        if not math.isfinite(price) or price <= 0:
            raise DemoSafetyError(f"invalid reference price for {req.symbol}: {price!r}")
        request = self.build_request(norm, price)
        self._check_precheck(request)
        self._require_enabled_demo()  # guard B again: mode may have changed after confirm
        try:
            result = self._mt5.order_send(request)
        except Exception as e:
            self._last_error = f"order_send failed: {e}"
            raise DemoSafetyError(self._last_error)
        retcode = getattr(result, "retcode", None) if not isinstance(result, dict) else result.get("retcode")
        if int(retcode or -1) != 10009:
            comment = getattr(result, "comment", "") if not isinstance(result, dict) else result.get("comment", "")
            self._last_error = f"order rejected: {self.humanize_retcode(retcode, comment)}"
            raise DemoSafetyError(self._last_error)
        ticket = getattr(result, "order", 0) if not isinstance(result, dict) else result.get("order", 0)
        out = {"ticket": int(ticket or 0), "symbol": req.symbol, "side": req.side,
               "volume": norm["volume"], "price": price,
               "retcode": int(retcode), "time": datetime.now(timezone.utc).isoformat()}
        self._open_results.append(out)
        logger.info("MT5 DEMO order placed: %s %s %.4f @ %s (ticket %s)",
                    req.symbol, req.side, norm["volume"], price, ticket)
        return out

    def close_position(self, ticket: int) -> Dict[str, Any]:
        """Close a DEMO position by ticket (opposite market order)."""
        self._require_enabled_demo()
        if self._mt5 is None:
            raise DemoSafetyError("MT5 module unavailable")
        # Locate the position in the terminal (authoritative).
        try:
            raw = self._mt5.positions_get(ticket=ticket)
            pos = raw[0] if raw else None
            if pos is None:
                all_pos = self._mt5.positions_get() or []
                pos = next((p for p in all_pos
                            if int(getattr(p, "ticket", -1) if not isinstance(p, dict) else p.get("ticket", -1)) == int(ticket)), None)
        except Exception as e:
            raise DemoSafetyError(f"position lookup failed: {e}")
        if pos is None:
            raise DemoSafetyError(f"position no longer exists (ticket {ticket})")
        parsed = parse_position(pos, self.server)
        if parsed is None:
            raise DemoSafetyError(f"unreadable position (ticket {ticket})")
        try:
            tick = self._mt5.symbol_info_tick(parsed.symbol)
            bid = getattr(tick, "bid", None) if not isinstance(tick, dict) else tick.get("bid")
            ask = getattr(tick, "ask", None) if not isinstance(tick, dict) else tick.get("ask")
        except Exception as e:
            raise DemoSafetyError(f"no quote to close {parsed.symbol}: {e}")
        # Close with the opposite side.
        close_side_buy = (parsed.side == "SELL")
        try:
            ORDER_TYPE_BUY = getattr(self._mt5, "ORDER_TYPE_BUY", 0)
            ORDER_TYPE_SELL = getattr(self._mt5, "ORDER_TYPE_SELL", 1)
            TRADE_ACTION_DEAL = getattr(self._mt5, "TRADE_ACTION_DEAL", 1)
        except Exception:
            ORDER_TYPE_BUY, ORDER_TYPE_SELL, TRADE_ACTION_DEAL = 0, 1, 1
        price = float(ask if close_side_buy else bid)
        request = {"action": TRADE_ACTION_DEAL, "symbol": parsed.symbol,
                   "volume": float(parsed.volume),
                   "type": ORDER_TYPE_BUY if close_side_buy else ORDER_TYPE_SELL,
                   "position_id": int(ticket), "price": price,
                   "deviation": int(self.risk.deviation_points),
                   "magic": 20261016, "comment": "falcon-demo-close",
                   "type_time": getattr(self._mt5, "ORDER_TIME_GTC", 0),
                   "type_filling": getattr(self._mt5, "ORDER_FILLING_IOC", 1)}
        self._check_precheck(request)
        self._require_enabled_demo()
        try:
            result = self._mt5.order_send(request)
        except Exception as e:
            raise DemoSafetyError(f"close failed: {e}")
        retcode = getattr(result, "retcode", None) if not isinstance(result, dict) else result.get("retcode")
        if int(retcode or -1) != 10009:
            comment = getattr(result, "comment", "") if not isinstance(result, dict) else result.get("comment", "")
            raise DemoSafetyError(f"close rejected: {self.humanize_retcode(retcode, comment)}")
        return {"ticket": int(ticket), "closed": True,
                "time": datetime.now(timezone.utc).isoformat()}

    def modify_sltp(self, ticket: int, sl: Optional[float] = None,
                    tp: Optional[float] = None) -> Dict[str, Any]:
        """Modify SL/TP of a DEMO position (TRADE_ACTION_SLTP)."""
        self._require_enabled_demo()
        try:
            TRADE_ACTION_SLTP = getattr(self._mt5, "TRADE_ACTION_SLTP", 6)
        except Exception:
            TRADE_ACTION_SLTP = 6
        request = {"action": TRADE_ACTION_SLTP, "position": int(ticket),
                   "symbol": "", "sl": float(sl or 0.0), "tp": float(tp or 0.0)}
        # Resolve symbol for the position (some servers require it).
        try:
            raw = self._mt5.positions_get(ticket=ticket) or []
            if raw:
                sym = getattr(raw[0], "symbol", "") if not isinstance(raw[0], dict) else raw[0].get("symbol", "")
                request["symbol"] = str(sym or "")
        except Exception:
            pass
        self._check_precheck(request)
        self._require_enabled_demo()
        try:
            result = self._mt5.order_send(request)
        except Exception as e:
            raise DemoSafetyError(f"SL/TP modify failed: {e}")
        retcode = getattr(result, "retcode", None) if not isinstance(result, dict) else result.get("retcode")
        if int(retcode or -1) != 10009:
            comment = getattr(result, "comment", "") if not isinstance(result, dict) else result.get("comment", "")
            raise DemoSafetyError(f"SL/TP modify rejected: {self.humanize_retcode(retcode, comment)}")
        return {"ticket": int(ticket), "sl": sl, "tp": tp,
                "time": datetime.now(timezone.utc).isoformat()}
