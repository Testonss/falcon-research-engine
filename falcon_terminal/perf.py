"""Lightweight perceived-performance instrumentation for Falcon Terminal.

Monotonic clocks only (time.monotonic / time.perf_counter). No network, no Qt.
Human-readable timings for the product-quality pass; never fakes speed.

Budgets (product targets, not guarantees):
  UI click feedback ......... < 50 ms
  asset selection local ..... < 100 ms
  search/filter response .... < 100 ms
  cached timeframe switch ... < 150 ms
  uncached timeframe ........ immediate loading feedback, then real data
  registry -> UI visible .... 100-200 ms preferred
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# -- response budgets (ms) ----------------------------------------------------
BUDGET_CLICK_MS = 50.0
BUDGET_SELECT_MS = 100.0
BUDGET_SEARCH_MS = 100.0
BUDGET_CACHED_TF_MS = 150.0
BUDGET_REGISTRY_UI_MS = 200.0

# Canonical event names measured across the terminal.
EVENTS = (
    "startup_first_frame",
    "startup_first_state",
    "asset_click_identity",
    "asset_click_chart",
    "timeframe_active",
    "timeframe_chart",
    "search_filter",
    "provider_to_registry",
    "registry_to_ui",
)


def now() -> float:
    """Monotonic timestamp (seconds). perf_counter has sub-ms resolution
    on all platforms (Windows monotonic ticks are too coarse for the
    50-200 ms product budgets)."""
    return time.perf_counter()


def format_ms(seconds: Optional[float]) -> str:
    """Human-readable duration. None -> em dash (never invents data)."""
    if seconds is None:
        return "—"
    try:
        ms = float(seconds) * 1000.0
    except (TypeError, ValueError):
        return "—"
    if ms < 1.0:
        return f"{ms * 1000.0:.0f} µs"
    if ms < 1000.0:
        return f"{ms:.1f} ms"
    return f"{ms / 1000.0:.2f} s"


def within_budget(event: str, seconds: float) -> Optional[bool]:
    """True/False when the event has a budget, else None."""
    budgets = {
        "asset_click_identity": BUDGET_SELECT_MS / 1000.0,
        "timeframe_active": BUDGET_CLICK_MS / 1000.0,
        "timeframe_chart": BUDGET_CACHED_TF_MS / 1000.0,
        "search_filter": BUDGET_SEARCH_MS / 1000.0,
        "registry_to_ui": BUDGET_REGISTRY_UI_MS / 1000.0,
    }
    limit = budgets.get(event)
    if limit is None:
        return None
    try:
        return float(seconds) <= limit
    except (TypeError, ValueError):
        return None


@dataclass
class PerfSample:
    event: str
    seconds: float
    detail: str = ""


@dataclass
class PerfTracker:
    """Bounded in-memory timing log (never grows without bound)."""

    max_samples: int = 200
    _marks: Dict[str, float] = field(default_factory=dict)
    _samples: List[PerfSample] = field(default_factory=list)

    def mark(self, name: str) -> float:
        t = now()
        self._marks[name] = t
        return t

    def measure(self, event: str, start: float, detail: str = "") -> float:
        dt = max(0.0, now() - start)
        self._samples.append(PerfSample(event, dt, detail))
        del self._samples[:-self.max_samples]
        return dt

    def measure_since(self, event: str, mark_name: str,
                      detail: str = "") -> Optional[float]:
        start = self._marks.get(mark_name)
        if start is None:
            return None
        return self.measure(event, start, detail)

    def last(self, event: str) -> Optional[float]:
        for s in reversed(self._samples):
            if s.event == event:
                return s.seconds
        return None

    def report(self) -> str:
        """Human-readable one-line-per-event summary (latest sample each)."""
        seen = {}
        for s in self._samples:
            seen[s.event] = s
        if not seen:
            return "no timings recorded"
        lines = []
        for event in EVENTS:
            s = seen.get(event)
            if s is None:
                continue
            ok = within_budget(event, s.seconds)
            flag = ""
            if ok is True:
                flag = " ✓"
            elif ok is False:
                flag = " (over budget)"
            extra = f" · {s.detail}" if s.detail else ""
            lines.append(f"{event}: {format_ms(s.seconds)}{flag}{extra}")
        return "\n".join(lines) if lines else "no timings recorded"


# Process-wide tracker shared by bridge (worker) and controller (GUI).
# Access is plain attribute assignment; CPython GIL makes single
# float/dict ops atomic enough for best-effort timings.
global_tracker = PerfTracker()
