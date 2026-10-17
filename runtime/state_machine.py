"""Runtime state machine (explicit, testable, persisted).

States: STARTING CONNECTING WARMING_UP RUNNING DEGRADED PAUSED STOPPING
STOPPED ERROR. Only RUNNING permits autonomous entries. DEGRADED and PAUSED
block entries (heartbeats continue). Transitions are validated; illegal
jumps raise ValueError (fail-closed: caller maps to ERROR).
"""

from __future__ import annotations

from enum import Enum


class RuntimeState(str, Enum):
    STARTING = "STARTING"
    CONNECTING = "CONNECTING"
    WARMING_UP = "WARMING_UP"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


_ALLOWED = {
    RuntimeState.STARTING: (RuntimeState.CONNECTING, RuntimeState.WARMING_UP,
                            RuntimeState.DEGRADED, RuntimeState.ERROR,
                            RuntimeState.STOPPING),
    RuntimeState.CONNECTING: (RuntimeState.WARMING_UP, RuntimeState.RUNNING,
                              RuntimeState.DEGRADED, RuntimeState.ERROR,
                              RuntimeState.STOPPING),
    RuntimeState.WARMING_UP: (RuntimeState.RUNNING, RuntimeState.DEGRADED,
                              RuntimeState.PAUSED, RuntimeState.ERROR,
                              RuntimeState.STOPPING),
    RuntimeState.RUNNING: (RuntimeState.DEGRADED, RuntimeState.PAUSED,
                           RuntimeState.ERROR, RuntimeState.STOPPING,
                           RuntimeState.WARMING_UP),
    RuntimeState.DEGRADED: (RuntimeState.RUNNING, RuntimeState.PAUSED,
                            RuntimeState.ERROR, RuntimeState.STOPPING,
                            RuntimeState.CONNECTING, RuntimeState.WARMING_UP),
    RuntimeState.PAUSED: (RuntimeState.RUNNING, RuntimeState.DEGRADED,
                          RuntimeState.ERROR, RuntimeState.STOPPING),
    RuntimeState.STOPPING: (RuntimeState.STOPPED, RuntimeState.ERROR),
    RuntimeState.STOPPED: (RuntimeState.STARTING,),
    RuntimeState.ERROR: (RuntimeState.CONNECTING, RuntimeState.STOPPING,
                         RuntimeState.STARTING, RuntimeState.DEGRADED),
}

_ENTRY_STATES = frozenset({RuntimeState.RUNNING})


def can_enter(state: "RuntimeState | str") -> bool:
    """True only in RUNNING. Everything else blocks autonomous entries."""
    try:
        s = state if isinstance(state, RuntimeState) else RuntimeState(str(state))
    except ValueError:
        return False
    return s in _ENTRY_STATES


def transition(current: "RuntimeState | str",
               nxt: "RuntimeState | str") -> RuntimeState:
    cur = current if isinstance(current, RuntimeState) else RuntimeState(str(current))
    want = nxt if isinstance(nxt, RuntimeState) else RuntimeState(str(nxt))
    if want == cur:
        return cur
    if want in _ALLOWED.get(cur, ()):
        return want
    raise ValueError(f"illegal runtime transition {cur.value} -> {want.value}")
