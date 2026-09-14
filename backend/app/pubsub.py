"""Pub/Sub abstraction for the relay.

Two implementations:

- ``InMemoryPubSub``: default, single-instance. Channels are per-room sets of
  asyncio queues; presence is kept in a dict with TTL-like expiry.
- ``RedisPubSub``: used when ``REDIS_URL`` is set. One Redis channel per room
  (``vh:room:<room>``); presence is a Redis key (``vh:presence:<room>``) with a
  short TTL so that multiple relay instances agree on PC online state without
  sharing in-process memory.

Interface (identical for both):
- ``publish(room, envelope)``
- ``subscribe(room) -> async iterator of envelopes``
- ``set_pc_presence(room, online)``
- ``get_pc_online(room)``

The relay never stores chat history, screenshots or answers: envelopes are
only ever forwarded, never persisted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Any, AsyncIterator

logger = logging.getLogger("relay.pubsub")

ROOM_CHANNEL_PREFIX = "vh:room:"
PRESENCE_KEY_PREFIX = "vh:presence:"


class PubSub(ABC):
    """Minimal pub/sub + presence contract used by the relay."""

    def __init__(self, presence_ttl_sec: float) -> None:
        self._presence_ttl_sec = presence_ttl_sec

    @abstractmethod
    async def publish(self, room: str, envelope: dict[str, Any]) -> None:
        """Publish an envelope to every subscriber of ``room``."""

    @abstractmethod
    def subscribe(self, room: str) -> AsyncIterator[dict[str, Any]]:
        """Return an async iterator of envelopes published to ``room``.

        Cancelling the consumer (or breaking out of the iterator) must
        release all underlying resources.
        """

    @abstractmethod
    async def set_pc_presence(self, room: str, online: bool) -> None:
        """Mark the room's PC as online (with TTL refresh) or offline."""

    @abstractmethod
    async def get_pc_online(self, room: str) -> bool:
        """Return whether the room's PC is currently considered online."""

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        """Release any underlying connections."""


class InMemoryPubSub(PubSub):
    """Single-instance pub/sub backed by asyncio queues."""

    def __init__(self, presence_ttl_sec: float) -> None:
        super().__init__(presence_ttl_sec)
        self._channels: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        # room -> (online, expires_at_monotonic). Explicit offline never expires
        # back to online; online entries expire after presence_ttl_sec.
        self._presence: dict[str, tuple[bool, float]] = {}

    async def publish(self, room: str, envelope: dict[str, Any]) -> None:
        for queue in list(self._channels.get(room, ())):
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:  # pragma: no cover - queues are unbounded
                logger.warning("pubsub queue full for room, dropping envelope")

    def subscribe(self, room: str) -> AsyncIterator[dict[str, Any]]:
        return self._subscribe(room)

    async def _subscribe(self, room: str) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._channels[room].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._channels[room].discard(queue)
            if not self._channels[room]:
                self._channels.pop(room, None)

    async def set_pc_presence(self, room: str, online: bool) -> None:
        if online:
            self._presence[room] = (True, time.monotonic() + self._presence_ttl_sec)
        else:
            self._presence[room] = (False, math.inf)

    async def get_pc_online(self, room: str) -> bool:
        entry = self._presence.get(room)
        if entry is None:
            return False
        online, expires_at = entry
        return online and time.monotonic() < expires_at

    async def aclose(self) -> None:
        self._channels.clear()
        self._presence.clear()


class RedisPubSub(PubSub):
    """Cross-instance pub/sub backed by Redis (``redis.asyncio``).

    Presence lives in a Redis key with a short TTL: a PC heartbeat refreshes
    the key; if heartbeats stop, the key expires and every instance sees the
    PC as offline. No in-process state is relied upon across instances.
    """

    def __init__(self, redis_url: str, presence_ttl_sec: float) -> None:
        super().__init__(presence_ttl_sec)
        import redis.asyncio as aioredis  # imported lazily: redis is an optional extra

        self._redis = aioredis.from_url(redis_url, decode_responses=True)

    @staticmethod
    def _channel(room: str) -> str:
        return f"{ROOM_CHANNEL_PREFIX}{room}"

    @staticmethod
    def _presence_key(room: str) -> str:
        return f"{PRESENCE_KEY_PREFIX}{room}"

    async def publish(self, room: str, envelope: dict[str, Any]) -> None:
        await self._redis.publish(self._channel(room), json.dumps(envelope, separators=(",", ":")))

    def subscribe(self, room: str) -> AsyncIterator[dict[str, Any]]:
        return self._subscribe(room)

    async def _subscribe(self, room: str) -> AsyncIterator[dict[str, Any]]:
        pubsub = self._redis.pubsub()
        channel = self._channel(room)
        await pubsub.subscribe(channel)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if isinstance(data, bytes):  # pragma: no cover - decode_responses=True
                    data = data.decode("utf-8")
                try:
                    yield json.loads(data)
                except (TypeError, ValueError):
                    logger.warning("dropping malformed pubsub message")
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(channel)
            with contextlib.suppress(Exception):
                await pubsub.aclose()

    async def set_pc_presence(self, room: str, online: bool) -> None:
        ttl = max(1, math.ceil(self._presence_ttl_sec))
        await self._redis.set(self._presence_key(room), "1" if online else "0", ex=ttl)

    async def get_pc_online(self, room: str) -> bool:
        return await self._redis.get(self._presence_key(room)) == "1"

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self._redis.aclose()


def create_pubsub(settings: Any) -> PubSub:
    """Factory: RedisPubSub when ``settings.redis_url`` is set, else in-memory."""
    if settings.redis_url:
        logger.info("using Redis pub/sub (cross-instance mode)")
        return RedisPubSub(settings.redis_url, settings.presence_timeout_sec)
    logger.info("using in-memory pub/sub (single-instance mode)")
    return InMemoryPubSub(settings.presence_timeout_sec)
