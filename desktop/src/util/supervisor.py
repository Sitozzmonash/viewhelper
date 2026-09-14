"""Subsystem supervision: restart with backoff, never kill the process.

PRD 27 requires that a failure in any single module (mic unplugged, ASR model
crash, relay socket closed, screenshot failure) must not exit the main program.
Each long running subsystem is therefore wrapped in a :class:`Supervisor`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from src.util.log import get_logger

__all__ = ["DelayPolicy", "Supervisor", "SubsystemFactory"]

SubsystemFactory = Callable[[], Awaitable[None]]


class DelayPolicy(Protocol):
    """Anything that can hand out restart delays (see transport.reconnect)."""

    def next_delay(self) -> float: ...

    def reset(self) -> None: ...


class Supervisor:
    """Runs *factory()* repeatedly until the stop event is set.

    The wrapped coroutine may raise or return: both are treated as "subsystem
    stopped" and are logged, then restarted after a backoff delay. Long healthy
    runs reset the backoff so transient failures do not permanently slow things
    down.
    """

    def __init__(
        self,
        name: str,
        *,
        stop_event: asyncio.Event,
        backoff: DelayPolicy,
        healthy_after_sec: float = 30.0,
        enabled: bool = True,
    ) -> None:
        self.name = name
        self._stop_event = stop_event
        self._backoff = backoff
        self._healthy_after_sec = healthy_after_sec
        self._enabled = enabled
        self._restarts = 0
        self._logger = get_logger(f"supervisor.{name}")

    @property
    def restarts(self) -> int:
        """Number of restarts performed so far (diagnostics/tests)."""
        return self._restarts

    @property
    def stopped(self) -> bool:
        """True once the stop event is set."""
        return self._stop_event.is_set()

    async def run(self, factory: SubsystemFactory) -> None:
        """Supervise *factory* until :meth:`request_stop` is called."""
        if not self._enabled:
            self._logger.info("subsystem disabled by configuration")
            await self._stop_event.wait()
            return

        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                await factory()
                if self._stop_event.is_set():
                    break
                self._logger.warning("subsystem returned unexpectedly, restarting")
            except asyncio.CancelledError:
                self._logger.debug("subsystem cancelled")
                raise
            except Exception as exc:  # noqa: BLE001 - a subsystem must never kill us
                self._logger.exception("subsystem crashed: %s: %s", type(exc).__name__, exc)

            if time.monotonic() - started >= self._healthy_after_sec:
                self._backoff.reset()
            if self._stop_event.is_set():
                break

            delay = self._backoff.next_delay()
            self._restarts += 1
            self._logger.info("restart #%d in %.1fs", self._restarts, delay)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)

    def request_stop(self) -> None:
        """Ask the supervisor to stop after the current attempt finishes."""
        self._stop_event.set()
