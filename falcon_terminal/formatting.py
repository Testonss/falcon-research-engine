"""Pure display formatting for Falcon Terminal views (no Qt/Tk dependency)."""
from __future__ import annotations

import math
from typing import Optional


def number(value: Optional[float]) -> str:
    """Human price/quote formatting. Never invents precision."""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    if v == 0 or abs(v) >= 1:
        return f"{v:,.2f}"
    return f"{v:.10f}".rstrip("0").rstrip(".")


def compact_money(value: Optional[float]) -> str:
    """$1.83M / $73.40B sizing for liquidity/cap/FDV. None -> em dash."""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v) or v < 0:
        return "—"
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if v >= threshold:
            return f"${v / threshold:,.2f}{suffix}"
    return f"${v:,.2f}"


def change_text(value: Optional[float]) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def spread_text(bps: Optional[float]) -> str:
    if bps is None:
        return "—"
    try:
        v = float(bps)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v) or v < 0:
        return "—"
    return f"{v:.1f} bps"


def age_text(seconds: Optional[float]) -> str:
    """Honest feed-data age (not latency)."""
    if seconds is None:
        return "—"
    try:
        v = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v) or v < 0:
        return "—"
    if v < 1.0:
        return f"{v * 1000:.0f} ms"
    return f"{v:.1f}s"


def freshness_text(age_s: Optional[float], stale: bool = False) -> str:
    """Subtle human freshness: 'Updated now' / 'Updated 4s ago' / STALE."""
    if stale:
        base = age_text(age_s)
        return f"STALE · {base}" if base != "—" else "STALE"
    if age_s is None:
        return "Waiting for data"
    try:
        v = float(age_s)
    except (TypeError, ValueError):
        return "Waiting for data"
    if not math.isfinite(v) or v < 0:
        return "Waiting for data"
    if v < 1.5:
        return "Updated now"
    return f"Updated {age_text(v)} ago"


def feed_semantics(provider: str, source: str = "") -> str:
    """Honest streaming vs polling label. Never upgrades polling to live."""
    p = (provider or "").strip().lower()
    s = (source or "").strip()
    if p == "binance":
        return "Streaming"
    if p == "solana":
        # Source already carries the poll interval
        # ("DexScreener polling (15s)"); surface it compactly.
        if "polling" in s.lower():
            return s
        return "Polling"
    return s or "—"


def connection_state(connected: bool, stale: bool, state: str = "",
                     error: Optional[str] = None,
                     was_connected: bool = False) -> str:
    """Visible connection state: CONNECTING/CONNECTED/STALE/RECONNECTING/ERROR."""
    s = (state or "").upper()
    if error or s == "ERROR":
        return "RECONNECTING" if was_connected else "ERROR"
    if not connected:
        if s in ("CONNECTING", "DISCONNECTED", ""):
            return "RECONNECTING" if was_connected else "CONNECTING"
        return s or "CONNECTING"
    if stale or s == "STALE":
        return "STALE"
    if s in ("CONNECTED", "STREAMING", "LIVE"):
        return "CONNECTED"
    return s or "CONNECTED"
