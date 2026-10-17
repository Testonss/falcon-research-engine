"""Autonomous MT5 DEMO execution path with anti-duplicate protection.

Wraps :class:`execution.mt5_demo.DemoExecutionAdapter` (double DEMO guard
kept: enable-time AND pre-send). This module never calls
``mt5.order_send`` directly and never weakens the guard. Layers:

  1. process-level dedupe: in-memory ``_sent`` set seeded from the store's
     executed ids on start (crash recovery);
  2. persistent dedupe: ``RuntimeStore.record_decision`` (INSERT OR IGNORE)
     + ``is_executed`` — a restarted process will not re-send;
  3. adapter-level dedupe: none needed beyond the above, but the adapter's
     own validation + order_check still run per order.

``dry_run=True`` validates + records without sending (used for offline
soaks and broker-less CI); the DEMO enable check still runs so a REAL
account can never be mistaken for safe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from execution.mt5_demo import (
    DemoExecutionAdapter,
    DemoRiskConfig,
    DemoSafetyError,
    OrderRequest,
)
from runtime.decision import Decision
from runtime.store import RuntimeStore

logger = logging.getLogger(__name__)

MAGIC_COMMENT = "falcon-demo-auto"


@dataclass
class DemoExecResult:
    sent: bool
    ticket: int = 0
    duplicate: bool = False
    skipped: bool = False
    reason: str = ""
    error: str = ""


class RuntimeDemoExecutor:
    """Idempotent DEMO-only sender. Fail-closed on any safety violation."""

    def __init__(
        self,
        adapter: DemoExecutionAdapter,
        store: Optional[RuntimeStore] = None,
        dry_run: bool = False,
    ):
        self.adapter = adapter
        self.store = store
        self.dry_run = bool(dry_run)
        self._sent: Set[str] = set()
        if store is not None:
            try:
                for did in store.executed_decision_ids():
                    self._sent.add(did)
            except Exception as e:
                logger.debug("executor recovery seed failed: %s", e)

    def enable(self, mt5_module: Any = None) -> Dict[str, Any]:
        """Enable DEMO execution. Raises DemoSafetyError on non-DEMO."""
        return self.adapter.enable(mt5_module)

    def execute(self, decision: Decision, tick_age_s: Optional[float] = None) -> DemoExecResult:
        if decision.action in ("HOLD", "SKIP") or decision.volume <= 0:
            return DemoExecResult(sent=False, skipped=True,
                                  reason=decision.reason or "no_trade")
        if decision.decision_id in self._sent:
            return DemoExecResult(sent=False, duplicate=True,
                                  reason="in_memory_duplicate")
        if self.store is not None:
            if self.store.is_executed(decision.decision_id):
                self._sent.add(decision.decision_id)
                return DemoExecResult(sent=False, duplicate=True,
                                      reason="store_duplicate")
            self.store.record_decision(
                decision.decision_id, decision.symbol, decision.action,
                decision.volume, decision.price, decision.reason,
                decision.blocked_by, "")
        req = OrderRequest(
            canonical_id=decision.canonical_id or decision.symbol,
            symbol=decision.symbol, side=decision.action,
            volume=float(decision.volume),
            sl=decision.stop_loss, tp=decision.take_profit,
            comment=MAGIC_COMMENT,
        )
        if self.dry_run:
            self._sent.add(decision.decision_id)
            if self.store is not None:
                self.store.mark_executed(decision.decision_id, ticket=0)
                self.store.record_fill(decision.decision_id, 0, decision.symbol,
                                       decision.action, decision.volume,
                                       decision.price)
            return DemoExecResult(sent=True, ticket=0, reason="dry_run")
        try:
            out = self.adapter.open_position(req, ref_price=decision.price,
                                             tick_age_s=tick_age_s)
        except DemoSafetyError as e:
            logger.warning("DEMO execution blocked (%s): %s", decision.decision_id, e)
            if self.store is not None:
                try:
                    self.store.record_attempt(
                        decision.decision_id, decision.symbol, decision.action,
                        decision.volume, decision.price, "rejected",
                        ticket=0, error=str(e))
                except Exception:
                    pass
            return DemoExecResult(sent=False, skipped=True,
                                  reason="safety_block", error=str(e))
        except Exception as e:  # fail closed, never half-mark
            logger.error("DEMO execution failed (%s): %s", decision.decision_id, e)
            if self.store is not None:
                try:
                    self.store.record_attempt(
                        decision.decision_id, decision.symbol, decision.action,
                        decision.volume, decision.price, "error",
                        ticket=0, error=str(e))
                except Exception:
                    pass
            return DemoExecResult(sent=False, skipped=True,
                                  reason="error", error=str(e))
        ticket = int(out.get("ticket", 0) or 0)
        self._sent.add(decision.decision_id)
        if self.store is not None:
            self.store.mark_executed(decision.decision_id, ticket=ticket)
            self.store.record_fill(decision.decision_id, ticket, decision.symbol,
                                   decision.action, decision.volume,
                                   float(out.get("price", decision.price) or decision.price))
            try:
                self.store.record_attempt(
                    decision.decision_id, decision.symbol, decision.action,
                    decision.volume, decision.price, "accepted",
                    ticket=ticket, error="")
            except Exception:
                pass
            try:
                self.store.upsert_position(
                    ticket, decision.symbol, decision.action,
                    decision.volume, float(out.get("price", decision.price)
                                           or decision.price),
                    stop_loss=float(decision.stop_loss or 0.0),
                    take_profit=float(decision.take_profit or 0.0),
                    origin_decision_id=decision.decision_id,
                    origin_signal_id=decision.decision_id, status="OPEN")
            except Exception:
                pass
        logger.info("DEMO auto order %s %s vol=%s ticket=%s",
                    decision.symbol, decision.action, decision.volume, ticket)
        return DemoExecResult(sent=True, ticket=ticket, reason="sent")

    # -- read helpers ---------------------------------------------------
    def open_positions(self) -> List[Any]:
        try:
            return list(self.adapter.get_positions())
        except Exception:
            return []

    def account(self) -> Dict[str, Any]:
        try:
            return self.adapter.account_snapshot()
        except Exception as e:
            return {"error": str(e)}

    def close(self, ticket: int, reason: str = "") -> Dict[str, Any]:
        """DEMO-only close by ticket. Fail-closed; never raises for control flow.

        When the ticket is already absent from the terminal (broker SL/TP or
        manual close), reports ``already_closed=True`` so the caller
        reconciles instead of sending another close.
        """
        try:
            still_open = False
            for p in self.open_positions():
                try:
                    t = getattr(p, "ticket", None)
                    if t is None and isinstance(p, dict):
                        t = p.get("ticket")
                    if int(t or -1) == int(ticket):
                        still_open = True
                        break
                except (TypeError, ValueError):
                    continue
            if not still_open:
                return {"closed": False, "already_closed": True,
                        "ticket": int(ticket), "reason": reason or "not_on_terminal"}
        except Exception:
            pass
        if self.dry_run:
            if self.store is not None:
                try:
                    self.store.mark_position_closed(int(ticket), reason or "exit:dry_run")
                except Exception:
                    pass
            return {"closed": True, "dry_run": True, "ticket": int(ticket),
                    "reason": reason or "exit:dry_run"}
        try:
            out = self.adapter.close_position(int(ticket))
        except Exception as e:
            msg = str(e)
            if "no longer exists" in msg:
                return {"closed": False, "already_closed": True,
                        "ticket": int(ticket), "reason": reason or "not_on_terminal",
                        "error": msg}
            return {"closed": False, "ticket": int(ticket),
                    "reason": reason or "exit", "error": msg}
        if self.store is not None:
            try:
                self.store.mark_position_closed(int(ticket), reason or "exit")
            except Exception:
                pass
        out = dict(out or {})
        out.setdefault("ticket", int(ticket))
        out.setdefault("reason", reason or "exit")
        return out


def build_adapter_for_config(mt5_module: Any, server: str = "",
                             risk_kwargs: Optional[Dict[str, Any]] = None) -> DemoExecutionAdapter:
    risk = DemoRiskConfig(**(risk_kwargs or {}))
    return DemoExecutionAdapter(mt5_module=mt5_module, server=server, risk=risk)
