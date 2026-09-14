"""Per-room PC presence tracking.

Protocol (shared/protocol/README.md):
- The PC sends ``heartbeat`` every ``HEARTBEAT_INTERVAL_SEC`` (5s).
- If no heartbeat (and no connection) for ``PRESENCE_TIMEOUT_SEC`` (15s), the
  relay broadcasts ``presence {pc_online: false}`` to the room's mobiles.
- On PC connect / heartbeat the relay marks the PC online (and broadcasts
  ``presence {pc_online: true}`` on transitions).
- When a PC socket closes, ``presence {pc_online: false}`` is broadcast.

An asyncio sweeper task runs every ``PRESENCE_SWEEP_INTERVAL_SEC`` (~3s) and
publishes presence transitions caused by TTL expiry. Presence state itself
lives in the PubSub layer (Redis key with TTL when Redis is enabled), so
multiple relay instances agree without sharing in-process memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Any

from .config import Settings
from .pubsub import PubSub

logger = logging.getLogger("relay.presence")


def make_envelope(type_: str, payload: dict[str, Any], device_id: str = "relay") -> dict[str, Any]:
    """Build a protocol envelope (UTC epoch milliseconds timestamp)."""
    return {
        "type": type_,
        "event_id": str(uuid.uuid4()),
        "device_id": device_id,
        "timestamp": int(time.time() * 1000),
        "payload": payload,
    }


class PresenceTracker:
    def __init__(self, pubsub: PubSub, settings: Settings) -> None:
        self._pubsub = pubsub
        self._settings = settings
        # Rooms with at least one local connection: room -> local connection count.
        self._watched: dict[str, int] = {}
        # Last presence value broadcast by *this* instance per room (dedupe only;
        # authoritative state lives in the PubSub layer).
        self._last_broadcast: dict[str, bool] = {}
        self._sweeper: asyncio.Task[None] | None = None

    # -- room watching ----------------------------------------------------

    def watch(self, room: str) -> None:
        self._watched[room] = self._watched.get(room, 0) + 1

    def unwatch(self, room: str) -> None:
        count = self._watched.get(room, 0) - 1
        if count > 0:
            self._watched[room] = count
        else:
            self._watched.pop(room, None)
            self._last_broadcast.pop(room, None)

    # -- PC lifecycle events ------------------------------------------------

    async def pc_connected(self, room: str) -> None:
        self._watched.setdefault(room, 0)
        await self._pubsub.set_pc_presence(room, True)
        await self._broadcast(room, True)

    async def pc_heartbeat(self, room: str) -> None:
        was_online = await self._pubsub.get_pc_online(room)
        await self._pubsub.set_pc_presence(room, True)
        if not was_online:
            # Transition (connect or recovery after a missed-heartbeat window).
            await self._broadcast(room, True)
        else:
            self._last_broadcast[room] = True

    async def pc_disconnected(self, room: str) -> None:
        await self._pubsub.set_pc_presence(room, False)
        await self._broadcast(room, False)

    # -- broadcasting -------------------------------------------------------

    async def _broadcast(self, room: str, online: bool) -> None:
        envelope = make_envelope("presence", {"pc_online": online})
        await self._pubsub.publish(room, envelope)
        self._last_broadcast[room] = online
        logger.debug("presence broadcast room=%s pc_online=%s", room, online)

    # -- sweeper task ---------------------------------------------------------

    def start(self) -> None:
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep_loop(), name="presence-sweeper")

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None

    async def _sweep_loop(self) -> None:
        interval = self._settings.presence_sweep_interval_sec
        try:
            while True:
                await asyncio.sleep(interval)
                for room in list(self._watched):
                    try:
                        online = await self._pubsub.get_pc_online(room)
                        if self._last_broadcast.get(room, online) != online:
                            await self._broadcast(room, online)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("presence sweep failed for room=%s", room)
        except asyncio.CancelledError:
            logger.debug("presence sweeper cancelled")
            raise
        except Exception:  # pragma: no cover - defensive, never crash the server
            logger.exception("presence sweeper crashed")
