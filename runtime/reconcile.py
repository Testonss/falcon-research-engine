"""Position reconciliation: local intent vs MT5 terminal truth.

The terminal is authoritative. This module diffs:

  expected: tickets the runtime opened and has not closed (from fills/store
            or caller-supplied set) — mapped ticket -> {symbol, volume, side}
  actual:   live ``MT5Position`` rows from ``DemoExecutionAdapter.get_positions()``

Reports:
  manually_closed  expected tickets absent from the terminal (human or SL/TP)
  unexpected       terminal tickets the runtime never opened (manual trades)
  volume_mismatch  same ticket, different volume/side (partial closes)

Pure function over plain dicts — no MT5 import needed for tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class ReconcileReport:
    manually_closed: List[int] = field(default_factory=list)
    unexpected: List[int] = field(default_factory=list)
    volume_mismatch: List[int] = field(default_factory=list)
    matched: List[int] = field(default_factory=list)
    ok: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "manually_closed": list(self.manually_closed),
            "unexpected": list(self.unexpected),
            "volume_mismatch": list(self.volume_mismatch),
            "matched": list(self.matched),
            "ok": self.ok,
        }


def _ticket_of(pos: Any) -> int:
    try:
        t = getattr(pos, "ticket", None)
        if t is None and isinstance(pos, dict):
            t = pos.get("ticket")
        return int(t)
    except (TypeError, ValueError):
        return -1


def reconcile_positions(
    expected: Dict[int, Dict[str, Any]],
    actual_positions: List[Any],
) -> ReconcileReport:
    actual_by_ticket: Dict[int, Any] = {}
    for p in actual_positions or []:
        t = _ticket_of(p)
        if t >= 0:
            actual_by_ticket[t] = p
    expected_tickets = set(int(t) for t in (expected or {}))
    actual_tickets = set(actual_by_ticket)
    manually_closed = sorted(expected_tickets - actual_tickets)
    unexpected = sorted(actual_tickets - expected_tickets)
    matched: List[int] = []
    volume_mismatch: List[int] = []
    for t in sorted(expected_tickets & actual_tickets):
        exp = expected.get(t, {}) or {}
        pos = actual_by_ticket[t]
        try:
            act_vol = float(getattr(pos, "volume", None)
                            if not isinstance(pos, dict) else pos.get("volume", 0))
        except (TypeError, ValueError):
            act_vol = 0.0
        try:
            exp_vol = float(exp.get("volume", act_vol))
        except (TypeError, ValueError):
            exp_vol = act_vol
        if abs(act_vol - exp_vol) > 1e-9:
            volume_mismatch.append(t)
        else:
            exp_side = str(exp.get("side", "") or "").upper()
            act_side = str(getattr(pos, "side", "")
                           if not isinstance(pos, dict) else pos.get("side", "")).upper()
            if exp_side and act_side and exp_side != act_side:
                volume_mismatch.append(t)
            else:
                matched.append(t)
    ok = not manually_closed and not unexpected and not volume_mismatch
    return ReconcileReport(
        manually_closed=manually_closed, unexpected=unexpected,
        volume_mismatch=volume_mismatch, matched=matched, ok=ok)
