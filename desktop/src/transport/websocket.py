"""Relay WebSocket client: auth, heartbeat, reconnect, dispatch.

Responsibilities (PRD 20/21/23 and shared/protocol/README.md):

* connect to ``RELAY_URL`` and send ``auth`` as the very first frame;
* heartbeat every 5s so the relay reports ``presence {pc_online: true}``;
* reconnect with exponential backoff (1,2,4,8,16,30,30...) and re-auth;
* dispatch incoming mobile commands to service handlers, ignoring unknown types;
* never let a handler or a socket error kill the process (PRD 27).

Handlers must return quickly: they are executed inline (in order) on the reader
loop and are expected to spawn their own :class:`asyncio.Task` for heavy work.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from src.config.env import RelayCredentials
from src.transport.protocol import Envelope, EventType, build_envelope, parse_envelope
from src.transport.reconnect import AUTH_TIMEOUT_SEC, HEARTBEAT_INTERVAL_SEC, BackoffPolicy
from src.util.log import get_logger

__all__ = [
    "RelayTransport",
    "resolve_ws_url",
    "websockets_connector",
    "EventEnvelope",
    "CommandHandler",
    "Connector",
    "SocketLike",
    "TransportError",
    "AuthError",
]

_logger = get_logger(__name__)

EventEnvelope = Envelope
CommandHandler = Callable[[Envelope], Awaitable[None]]
#: Anything with async ``send``/``recv``/``close`` (real websockets or a test fake).
SocketLike = Any

#: Slow-handler threshold; dispatch stays inline to preserve message ordering.
_SLOW_HANDLER_SEC = 1.0
_MAX_FRAME_BYTES = 16 * 1024 * 1024


class TransportError(RuntimeError):
    """Base class for relay transport failures."""


class AuthError(TransportError):
    """Relay rejected the credentials or never answered ``auth``."""


class Connector(Protocol):
    """Factory returning an async context manager yielding a socket."""

    def __call__(self, url: str) -> Any: ...


def resolve_ws_url(relay_url: str) -> str:
    """Normalise ``RELAY_URL`` into the endpoint the relay serves.

    ``https://host`` -> ``wss://host/ws``; an explicit path is preserved.
    """
    raw = (relay_url or "").strip()
    if not raw:
        raise TransportError("RELAY_URL is empty")
    if "//" not in raw:
        raw = f"wss://{raw}"
    parts = urlsplit(raw)
    scheme_map = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}
    scheme = scheme_map.get(parts.scheme.lower(), "wss")
    path = parts.path or ""
    if path in ("", "/"):
        path = "/ws"
    return urlunsplit((scheme, parts.netloc, path, parts.query, ""))


@asynccontextmanager
async def websockets_connector(
    url: str,
    *,
    max_size: int = _MAX_FRAME_BYTES,
    open_timeout: float = 15.0,
    close_timeout: float = 5.0,
) -> Any:
    """Default connector built on the ``websockets`` package (lazy import).

    Works with both the modern ``websockets.asyncio.client`` API and the legacy
    ``websockets.client`` API.
    """
    connect = _load_connect_fn()
    kwargs: dict[str, Any] = {
        "max_size": max_size,
        "open_timeout": open_timeout,
        "close_timeout": close_timeout,
        # Application-level heartbeats are used instead of protocol pings.
        "ping_interval": None,
    }
    async with connect(url, **kwargs) as socket:
        yield socket


_CONNECT_FN: Callable[..., Any] | None = None


def _load_connect_fn() -> Callable[..., Any]:
    global _CONNECT_FN  # noqa: PLW0603 - module level cache
    if _CONNECT_FN is not None:
        return _CONNECT_FN
    try:
        from websockets.asyncio.client import connect as connect_fn  # websockets >= 13
    except ImportError:  # pragma: no cover - depends on installed version
        try:
            from websockets import connect as legacy_connect  # type: ignore[attr-defined]

            connect_fn = legacy_connect  # websockets 10..12
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise TransportError(
                "the 'websockets' package is not installed; run `uv sync` in desktop/"
            ) from exc
    _CONNECT_FN = connect_fn
    return connect_fn


class RelayTransport:
    """Long-lived relay connection with automatic reconnect."""

    def __init__(
        self,
        *,
        credentials: RelayCredentials,
        handlers: Mapping[str, CommandHandler] | None = None,
        on_state_change: Callable[[bool], None] | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SEC,
        auth_timeout: float = AUTH_TIMEOUT_SEC,
        backoff: BackoffPolicy | None = None,
        connector: Connector | None = None,
        send_queue_size: int = 256,
    ) -> None:
        self._credentials = credentials
        self._device_id = credentials.device_id
        self._handlers: dict[str, CommandHandler] = dict(handlers or {})
        self._on_state_change = on_state_change
        self._heartbeat_interval = max(0.5, heartbeat_interval)
        self._auth_timeout = auth_timeout
        self._backoff = backoff or BackoffPolicy()
        self._connector: Connector = connector or websockets_connector
        self._out_queue: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=max(1, send_queue_size))
        self._stop_event = asyncio.Event()
        self._connected = asyncio.Event()
        self._socket: SocketLike | None = None
        self._sent = 0
        self._dropped = 0
        self._connections = 0
        self._logger = get_logger("transport.relay")

    # -- introspection --------------------------------------------------
    @property
    def connected(self) -> bool:
        """True while authenticated and able to send."""
        return self._connected.is_set()

    @property
    def device_id(self) -> str:
        """Device id stamped on every outgoing envelope."""
        return self._device_id

    @property
    def stats(self) -> dict[str, int]:
        """Counters for diagnostics (never contains payload data)."""
        return {
            "sent": self._sent,
            "dropped": self._dropped,
            "connections": self._connections,
            "queued": self._out_queue.qsize(),
        }

    # -- handler registration ------------------------------------------
    def register(self, event_type: str, handler: CommandHandler) -> None:
        """Register (or replace) the handler for *event_type*."""
        self._handlers[event_type] = handler

    def register_many(self, handlers: Mapping[str, CommandHandler]) -> None:
        """Bulk registration helper."""
        self._handlers.update(handlers)

    # -- lifecycle ------------------------------------------------------
    async def run(self) -> None:
        """Connect/reconnect until :meth:`stop` is called."""
        if not self._credentials.url:
            raise TransportError("RELAY_URL is not configured")
        while not self._stop_event.is_set():
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                self._logger.warning("relay connection failed: %s: %s", type(exc).__name__, exc)
            finally:
                self._mark_disconnected()
            if self._stop_event.is_set():
                break
            delay = self._backoff.next_delay()
            self._logger.info("reconnecting in %.1fs (attempt %d)", delay, self._backoff.attempt)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        self._logger.info("transport stopped (%s)", self.stats)

    async def stop(self) -> None:
        """Graceful shutdown: stop reconnecting and close the socket."""
        self._stop_event.set()
        socket = self._socket
        if socket is not None:
            with contextlib.suppress(Exception):
                await socket.close()
        self._mark_disconnected()

    def request_stop(self) -> None:
        """Thread/task-safe variant of :meth:`stop` that only sets the flag."""
        self._stop_event.set()

    async def _connect_once(self) -> None:
        url = resolve_ws_url(self._credentials.url)
        self._logger.info("connecting to %s (room=%s)", url, self._credentials.room)
        async with self._connector(url) as socket:
            self._socket = socket
            self._connections += 1
            await self._authenticate(socket)
            self._connected.set()
            self._backoff.reset()
            self._logger.info("relay authenticated (room=%s)", self._credentials.room)
            self._notify_state(True)
            try:
                await self._session(socket)
            finally:
                self._mark_disconnected()

    async def _authenticate(self, socket: SocketLike) -> None:
        """Send ``auth`` first and wait for ``auth_ok`` (PRD 22)."""
        auth = build_envelope(
            EventType.AUTH,
            {
                "role": "pc",
                "room": self._credentials.room,
                "token": self._credentials.secret,
            },
            device_id=self._device_id,
        )
        await socket.send(auth.to_json())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._auth_timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AuthError("timed out waiting for auth_ok")
            raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
            envelope = parse_envelope(raw) if raw is not None else None
            if envelope is None:
                continue
            if envelope.type == EventType.AUTH_OK:
                return
            if envelope.type == EventType.ERROR:
                message = envelope.get("message", "relay rejected authentication")
                raise AuthError(str(message))
            # Anything else arriving before auth_ok is queued for later dispatch.
            await self._dispatch(envelope)

    async def _session(self, socket: SocketLike) -> None:
        """Run writer + heartbeat + reader until one of them finishes/fails."""
        writer = asyncio.create_task(self._writer_loop(socket), name="relay-writer")
        heartbeat = asyncio.create_task(self._heartbeat_loop(), name="relay-heartbeat")
        reader = asyncio.create_task(self._reader_loop(socket), name="relay-reader")
        tasks = {writer, heartbeat, reader}
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
        self._logger.info("relay socket closed by peer")

    async def _reader_loop(self, socket: SocketLike) -> None:
        while not self._stop_event.is_set():
            raw = await socket.recv()
            if raw is None:
                return
            envelope = parse_envelope(raw)
            if envelope is None:
                self._logger.debug("ignoring unparsable frame (%d bytes)", len(str(raw)))
                continue
            await self._dispatch(envelope)

    async def _dispatch(self, envelope: Envelope) -> None:
        handler = self._handlers.get(envelope.type)
        if handler is None:
            # Protocol rule: unknown types are ignored, never fatal.
            self._logger.debug("no handler for %s", envelope.summary())
            if envelope.type == EventType.ERROR:
                self._logger.warning("relay error: %s", envelope.get("message"))
            return
        started = time.monotonic()
        try:
            await handler(envelope)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad command must not kill us
            self._logger.exception("handler for %s failed: %s: %s", envelope.type, type(exc).__name__, exc)
        finally:
            elapsed = time.monotonic() - started
            if elapsed > _SLOW_HANDLER_SEC:
                self._logger.warning("handler %s took %.2fs", envelope.type, elapsed)

    async def _writer_loop(self, socket: SocketLike) -> None:
        while not self._stop_event.is_set():
            envelope = await self._out_queue.get()
            try:
                await socket.send(envelope.to_json())
                self._sent += 1
            finally:
                self._out_queue.task_done()

    async def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._heartbeat_interval)
                return
            except asyncio.TimeoutError:
                pass
            await self._enqueue(build_envelope(EventType.HEARTBEAT, {}, device_id=self._device_id))

    # -- outgoing -------------------------------------------------------
    async def send_event(self, event_type: str, payload: Mapping[str, Any] | None = None) -> bool:
        """Queue an event for delivery; returns False when currently offline."""
        envelope = build_envelope(event_type, payload, device_id=self._device_id)
        return await self.send_envelope(envelope)

    async def send_envelope(self, envelope: Envelope) -> bool:
        """Queue a pre-built envelope; returns False when currently offline."""
        if not self._connected.is_set() or self._stop_event.is_set():
            self._dropped += 1
            self._logger.debug("offline, dropping %s", envelope.type)
            return False
        return await self._enqueue(envelope)

    async def _enqueue(self, envelope: Envelope) -> bool:
        try:
            self._out_queue.put_nowait(envelope)
            return True
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                dropped = self._out_queue.get_nowait()
                self._out_queue.task_done()
                self._dropped += 1
                self._logger.warning("send queue full, dropped oldest %s", dropped.type)
            with contextlib.suppress(asyncio.QueueFull):
                self._out_queue.put_nowait(envelope)
            return False

    async def drain(self, timeout: float = 5.0) -> None:
        """Wait until queued events were written (best effort, shutdown only)."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._out_queue.join(), timeout=timeout)

    # -- internals ------------------------------------------------------
    def _mark_disconnected(self) -> None:
        was_connected = self._connected.is_set()
        self._connected.clear()
        self._socket = None
        if was_connected:
            self._logger.info("relay disconnected")
            self._notify_state(False)

    def _notify_state(self, connected: bool) -> None:
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(connected)
        except Exception as exc:  # noqa: BLE001 - observer bugs must not break transport
            self._logger.exception("state observer failed: %s", exc)
