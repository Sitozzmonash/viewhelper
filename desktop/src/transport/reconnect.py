"""Reconnection policy for the relay WebSocket (PRD 21).

Backoff sequence: ``1, 2, 4, 8, 16, 30, 30, ...`` seconds with optional jitter.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

__all__ = [
    "BackoffPolicy",
    "DEFAULT_BACKOFF",
    "HEARTBEAT_INTERVAL_SEC",
    "PRESENCE_TIMEOUT_SEC",
    "AUTH_TIMEOUT_SEC",
]

#: PC -> relay heartbeat cadence (relay marks the PC offline after 15s).
HEARTBEAT_INTERVAL_SEC: float = 5.0
PRESENCE_TIMEOUT_SEC: float = 15.0
#: How long we wait for ``auth_ok`` after connecting before giving up.
AUTH_TIMEOUT_SEC: float = 15.0


@dataclass(slots=True)
class BackoffPolicy:
    """Exponential backoff with a hard ceiling.

    Satisfies :class:`src.util.supervisor.DelayPolicy`, so it is reused for
    subsystem restarts as well as WebSocket reconnects.
    """

    initial_sec: float = 1.0
    factor: float = 2.0
    max_sec: float = 30.0
    jitter_sec: float = 0.0
    _attempt: int = field(default=0, init=False, repr=False)

    @property
    def attempt(self) -> int:
        """How many delays were handed out since the last :meth:`reset`."""
        return self._attempt

    def next_delay(self) -> float:
        """Next delay in seconds: 1, 2, 4, 8, 16, 30, 30 ..."""
        self._attempt += 1
        delay = min(self.initial_sec * (self.factor ** (self._attempt - 1)), self.max_sec)
        if self.jitter_sec > 0:
            delay += random.uniform(0.0, self.jitter_sec)
        return round(delay, 3)

    def peek_delay(self) -> float:
        """Delay that :meth:`next_delay` would return, without consuming it."""
        return round(min(self.initial_sec * (self.factor ** self._attempt), self.max_sec), 3)

    def reset(self) -> None:
        """Reset after a successful connection / healthy run."""
        self._attempt = 0

    async def wait(self) -> float:
        """Sleep for the next backoff delay (cancellable) and return it."""
        delay = self.next_delay()
        await asyncio.sleep(delay)
        return delay


DEFAULT_BACKOFF = BackoffPolicy()
