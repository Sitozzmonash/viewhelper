"""Stop-file sentinel for external management scripts.

Contract (used by the PowerShell start/stop scripts)::

    <base_dir>/runtime/app.stop

* creating the file asks the app to shut down gracefully -- the exact same
  path as SIGINT/SIGTERM (:meth:`DesktopApplication.request_stop`);
* the app deletes a **stale** sentinel at startup so a leftover file can never
  prevent a fresh run;
* the watcher polls about once per second from an asyncio task;
* right before the process exits after a sentinel-triggered graceful shutdown
  the file is deleted again and the exit code is 0.

The class is deliberately free of application imports so it can be unit tested
in isolation (``tests/test_sentinel_shutdown.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path

from src.util.log import get_logger

__all__ = ["StopFileSentinel", "DEFAULT_POLL_INTERVAL_SEC"]

#: Poll cadence required for the sentinel ("check every ~1s").
DEFAULT_POLL_INTERVAL_SEC = 1.0

_logger = get_logger("util.sentinel")


class StopFileSentinel:
    """Watches one sentinel file and flips a stop event when it appears."""

    def __init__(
        self,
        path: Path | str,
        *,
        interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        self._path = Path(path)
        self._interval = max(0.05, float(interval_sec))
        self._on_stop = on_stop
        self._triggered = False

    # -- introspection --------------------------------------------------
    @property
    def path(self) -> Path:
        """Sentinel file location."""
        return self._path

    @property
    def interval_sec(self) -> float:
        """Polling interval in seconds."""
        return self._interval

    @property
    def triggered(self) -> bool:
        """True when the stop file caused (or causes) this shutdown."""
        return self._triggered

    def exists(self) -> bool:
        """Whether the stop file currently exists."""
        return self._path.exists()

    # -- lifecycle ------------------------------------------------------
    def prepare(self) -> bool:
        """Create the runtime dir and delete a stale stop file.

        Returns ``True`` when a stale file was removed.
        """
        with contextlib.suppress(OSError):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        removed = self.clear()
        if removed:
            _logger.info("removed stale stop file %s", self._path)
        return removed

    def clear(self) -> bool:
        """Delete the stop file if present; returns whether one was removed."""
        try:
            self._path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            _logger.warning("cannot remove stop file %s: %s", self._path, exc)
            return False
        return True

    async def watch(self, stop_event: asyncio.Event) -> bool:
        """Poll the sentinel until it appears or *stop_event* is already set.

        When the file is found the stop event is set (single graceful shutdown
        path) and ``True`` is returned. Returns ``False`` when the shutdown was
        requested by something else.
        """
        while not stop_event.is_set():
            if self._path.exists():
                self._triggered = True
                _logger.info("stop file detected: %s -> graceful shutdown", self._path)
                if self._on_stop is not None:
                    try:
                        self._on_stop()
                    except Exception as exc:  # noqa: BLE001 - observer bugs must not break us
                        _logger.exception("sentinel on_stop callback failed: %s", exc)
                stop_event.set()
                return True
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval)
        return self._triggered

    def cleanup(self) -> bool:
        """Remove the stop file right before exit (only when it triggered us)."""
        if not self._triggered:
            return False
        removed = self.clear()
        self._triggered = False
        if removed:
            _logger.info("stop file consumed and removed: %s", self._path)
        return removed
