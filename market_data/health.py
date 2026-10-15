"""Connection state machine + feed health.

States: DISCONNECTED -> CONNECTING -> CONNECTED -> (STALE) -> RECONNECTING
-> CONNECTED ... -> STOPPING -> STOPPED. STOPPED is terminal for a run;
a new run starts again from DISCONNECTED.

Clocks are injectable (now_fn) so stale-transition tests never sleep.
"""

from __future__ import annotations

import enum
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional


class ConnectionState(str, enum.Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    STALE = "STALE"
    RECONNECTING = "RECONNECTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


_ALLOWED = {
    ConnectionState.DISCONNECTED: {ConnectionState.CONNECTING, ConnectionState.STOPPING, ConnectionState.STOPPED},
    ConnectionState.CONNECTING: {ConnectionState.CONNECTED, ConnectionState.RECONNECTING,
                                 ConnectionState.DISCONNECTED, ConnectionState.STOPPING},
    ConnectionState.CONNECTED: {ConnectionState.STALE, ConnectionState.RECONNECTING,
                                ConnectionState.STOPPING, ConnectionState.DISCONNECTED},
    ConnectionState.STALE: {ConnectionState.CONNECTED, ConnectionState.RECONNECTING,
                            ConnectionState.STOPPING, ConnectionState.DISCONNECTED},
    ConnectionState.RECONNECTING: {ConnectionState.CONNECTING, ConnectionState.CONNECTED,
                                    ConnectionState.STOPPING, ConnectionState.DISCONNECTED},
    ConnectionState.STOPPING: {ConnectionState.STOPPED, ConnectionState.DISCONNECTED},
    ConnectionState.STOPPED: set(),
}


@dataclass
class HealthSnapshot:
    provider: str
    connected: bool
    stale: bool
    state: str
    last_message_at: Optional[str]
    seconds_since_message: Optional[float]
    reconnects: int
    messages_received: int
    duplicates_dropped: int
    out_of_order_dropped: int
    rejected: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "connected": self.connected,
            "stale": self.stale,
            "state": self.state,
            "last_message_at": self.last_message_at,
            "seconds_since_message": self.seconds_since_message,
            "reconnects": self.reconnects,
            "messages_received": self.messages_received,
            "duplicates_dropped": self.duplicates_dropped,
            "out_of_order_dropped": self.out_of_order_dropped,
            "rejected": self.rejected,
        }


class ConnectionStateMachine:
    """Explicit, inspectable connection state with bounded backoff."""

    def __init__(self, provider: str, now_fn: Optional[Callable[[], float]] = None,
                 stale_after_s: float = 30.0):
        self.provider = provider
        self.state = ConnectionState.DISCONNECTED
        self._now = now_fn or time.monotonic
        self.stale_after_s = float(stale_after_s)
        self.reconnects = 0
        self.messages_received = 0
        self.duplicates_dropped = 0
        self.out_of_order_dropped = 0
        self.rejected = 0
        self.last_message_at: Optional[float] = None  # monotonic clock
        self.last_message_wall: Optional[str] = None
        self._transitions: list = []

    # -- state -----------------------------------------------------------
    def transition(self, nxt: ConnectionState) -> bool:
        if nxt == self.state:
            return True
        if self.state == ConnectionState.STOPPED:
            return False
        allowed = _ALLOWED.get(self.state, set())
        if nxt not in allowed:
            # Illegal jumps (e.g. CONNECTED -> CONNECTING without
            # RECONNECTING) are rejected explicitly rather than coerced.
            return False
        self._transitions.append((self.state.value, nxt.value))
        self.state = nxt
        return True

    def force(self, nxt: ConnectionState):
        """Test/escape-hatch transition bypassing the allowed graph."""
        self._transitions.append((self.state.value, nxt.value))
        self.state = nxt

    def mark_connected(self):
        if self.state in (ConnectionState.CONNECTING, ConnectionState.RECONNECTING,
                          ConnectionState.DISCONNECTED, ConnectionState.STALE):
            self.transition(ConnectionState.CONNECTED)
        elif self.state == ConnectionState.STALE:
            self.transition(ConnectionState.CONNECTED)

    def mark_message(self):
        now = self._now()
        self.last_message_at = now
        self.messages_received += 1
        self.last_message_wall = datetime.now(timezone.utc).isoformat()
        if self.state in (ConnectionState.STALE, ConnectionState.RECONNECTING):
            self.transition(ConnectionState.CONNECTED)

    @property
    def connected(self) -> bool:
        return self.state in (ConnectionState.CONNECTED, ConnectionState.STALE)

    @property
    def seconds_since_message(self) -> Optional[float]:
        if self.last_message_at is None:
            return None
        return max(0.0, self._now() - self.last_message_at)

    @property
    def is_stale(self) -> bool:
        ssm = self.seconds_since_message
        if ssm is None:
            return False
        return ssm > self.stale_after_s
    def refresh_stale(self) -> bool:
        """Move CONNECTED -> STALE when the timeout elapsed; True on change."""
        if self.state == ConnectionState.CONNECTED and self.is_stale:
            self.transition(ConnectionState.STALE)
            return True
        return False

    def backoff_delay(self, attempt: int, base: float = 0.5,
                      cap: float = 30.0, jitter: bool = True) -> float:
        delay = min(base * (2.0 ** max(0, attempt - 1)), cap)
        if jitter:
            delay = delay * (0.5 + random.random() * 0.5)
        return min(delay, cap)

    def health(self) -> HealthSnapshot:
        # Staleness is a property of data freshness, reported independently
        # of the connection flag so a disconnected-with-old-data provider
        # never looks fresh.
        stale = self.state == ConnectionState.STALE or self.is_stale
        return HealthSnapshot(
            provider=self.provider,
            connected=self.connected,
            stale=bool(stale),
            state=self.state.value,
            last_message_at=self.last_message_wall,
            seconds_since_message=self.seconds_since_message,
            reconnects=self.reconnects,
            messages_received=self.messages_received,
            duplicates_dropped=self.duplicates_dropped,
            out_of_order_dropped=self.out_of_order_dropped,
            rejected=self.rejected,
        )
