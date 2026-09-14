"""Time helpers.

Every value that crosses the WebSocket boundary is a UTC epoch **milliseconds**
integer (shared/protocol/README.md). Local time is only used for log formatting.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

__all__ = [
    "utc_now_ms",
    "monotonic_ms",
    "ms_to_datetime",
    "ms_to_iso",
    "duration_sec",
]


def utc_now_ms() -> int:
    """Current UTC time as epoch milliseconds."""
    return int(time.time() * 1000)


def monotonic_ms() -> int:
    """Monotonic clock in milliseconds (for elapsed/timeouts, never for the wire)."""
    return int(time.monotonic() * 1000)


def ms_to_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to an aware UTC datetime."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def ms_to_iso(ms: int) -> str:
    """ISO-8601 rendering of epoch milliseconds (logs only)."""
    return ms_to_datetime(ms).isoformat(timespec="milliseconds")


def duration_sec(started_at_ms: int | None, ended_at_ms: int | None) -> float | None:
    """Duration in seconds between two epoch-ms stamps, or ``None`` when unknown."""
    if started_at_ms is None or ended_at_ms is None:
        return None
    return round(max(0, ended_at_ms - started_at_ms) / 1000.0, 3)
