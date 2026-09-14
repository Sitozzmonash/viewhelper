"""Shared plumbing for wire-facing services.

Two problems appear in every service:

1. sending events without ever letting a transport failure break the caller;
2. crossing the thread boundary -- ASR pipelines and the global hotkey call back
   from *their own* threads, so anything they trigger has to be marshalled onto
   the asyncio loop with :meth:`asyncio.AbstractEventLoop.call_soon_threadsafe`.

:class:`ServiceBase` implements both once. It also keeps strong references to
the tasks it spawns (a bare ``create_task`` result can be garbage collected
mid-flight) and can cancel them on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from src.transport.websocket import RelayTransport
from src.util.log import get_logger

__all__ = ["ServiceBase", "TaskFactory"]

#: Zero-argument coroutine factory (so the task body is only built on the loop).
TaskFactory = Callable[[], Awaitable[None]]


class ServiceBase:
    """Transport + thread-marshalling helpers shared by all services."""

    def __init__(self, *, transport: RelayTransport | None = None, logger_name: str | None = None) -> None:
        self._transport = transport
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._logger = get_logger(logger_name or type(self).__name__.lower())

    # -- loop binding ---------------------------------------------------
    def bind_loop(self) -> asyncio.AbstractEventLoop:
        """Remember the running loop so worker threads can post back safely."""
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._loop_thread = threading.get_ident()
        return loop

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """Loop bound by :meth:`bind_loop` (``None`` before/after binding)."""
        return self._loop

    @property
    def transport(self) -> RelayTransport | None:
        """Relay transport used to emit events."""
        return self._transport

    def attach_transport(self, transport: RelayTransport) -> None:
        """Late-bind the transport (services can be built before it exists)."""
        self._transport = transport

    def _on_loop_thread(self) -> bool:
        return self._loop is not None and threading.get_ident() == self._loop_thread

    # -- emitting -------------------------------------------------------
    async def emit(self, event_type: str, payload: Mapping[str, Any] | None = None) -> bool:
        """Send one event; transport problems are logged, never raised."""
        if self._transport is None:
            self._logger.debug("no transport bound, dropping %s", event_type)
            return False
        try:
            return await self._transport.send_event(event_type, payload)
        except Exception as exc:  # noqa: BLE001 - offline must never crash a service
            self._logger.warning("cannot send %s: %s", event_type, exc)
            return False

    def emit_threadsafe(self, event_type: str, payload: Mapping[str, Any] | None = None) -> bool:
        """Schedule :meth:`emit` from any thread; ``False`` when there is no loop."""
        return self.spawn_threadsafe(lambda: self.emit(event_type, payload), name=f"emit-{event_type}")

    async def emit_error(self, message: str, *, code: str | None = None) -> bool:
        """Send the protocol ``error`` event."""
        payload: dict[str, Any] = {"message": message}
        if code:
            payload["code"] = code
        return await self.emit("error", payload)

    # -- task spawning --------------------------------------------------
    def spawn(self, factory: TaskFactory, *, name: str | None = None) -> asyncio.Task[None] | None:
        """Create a task; from a foreign thread it is scheduled onto the loop.

        Returns the task when called from the loop thread, otherwise ``None``
        (a task created on another thread cannot be handed back synchronously);
        the boolean :meth:`spawn_threadsafe` still reports whether it was queued.
        """
        if self._on_loop_thread():
            return self._create_task(factory, name)
        self.spawn_threadsafe(factory, name=name)
        return None

    def spawn_threadsafe(self, factory: TaskFactory, *, name: str | None = None) -> bool:
        """Create a task from *any* thread; ``False`` when no loop is running."""
        loop = self._loop
        if loop is None or loop.is_closed():
            self._logger.debug("no running loop, ignoring %s", name or "task")
            return False
        if self._on_loop_thread():
            return self._create_task(factory, name) is not None
        try:
            loop.call_soon_threadsafe(self._create_task, factory, name)
        except RuntimeError as exc:  # loop closed underneath us
            self._logger.debug("cannot schedule %s: %s", name or "task", exc)
            return False
        return True

    def _create_task(self, factory: TaskFactory, name: str | None) -> asyncio.Task[None] | None:
        try:
            coroutine = factory()
        except Exception as exc:  # noqa: BLE001 - a broken factory must not kill the loop
            self._logger.exception("cannot build task %s: %s", name or "?", exc)
            return None
        task = asyncio.ensure_future(coroutine)
        if name is not None:
            task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._logger.error("task %s failed: %s: %s", task.get_name(), type(exc).__name__, exc)

    @property
    def pending_tasks(self) -> int:
        """Number of in-flight tasks spawned by this service."""
        return len(self._tasks)

    async def wait_tasks(self, timeout: float = 5.0) -> int:
        """Wait for spawned tasks to finish (no cancellation); returns leftovers."""
        tasks = [task for task in list(self._tasks) if not task.done()]
        if not tasks:
            return 0
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
        return sum(1 for task in tasks if not task.done())

    async def cancel_tasks(self, timeout: float = 5.0) -> None:
        """Cancel and reap every spawned task (shutdown only)."""
        tasks = [task for task in list(self._tasks) if not task.done()]
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
