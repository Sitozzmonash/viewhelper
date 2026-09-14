"""Unit tests for the in-memory PubSub (presence TTL, room isolation)."""

from __future__ import annotations

import asyncio

import pytest

from app.pubsub import InMemoryPubSub


async def test_publish_subscribe_roundtrip():
    pubsub = InMemoryPubSub(presence_ttl_sec=15.0)
    received: list[dict] = []

    async def consume() -> None:
        async for envelope in pubsub.subscribe("room-a"):
            received.append(envelope)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # let the subscriber register

    await pubsub.publish("room-a", {"type": "asr_partial", "payload": {"text": "hi"}})
    await asyncio.sleep(0.05)
    assert received == [{"type": "asr_partial", "payload": {"text": "hi"}}]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_room_isolation():
    pubsub = InMemoryPubSub(presence_ttl_sec=15.0)
    received_a: list[dict] = []

    async def consume_a() -> None:
        async for envelope in pubsub.subscribe("room-a"):
            received_a.append(envelope)

    task = asyncio.create_task(consume_a())
    await asyncio.sleep(0.05)

    await pubsub.publish("room-b", {"type": "asr_partial"})
    await pubsub.publish("room-a", {"type": "asr_final"})
    await asyncio.sleep(0.05)

    assert [e["type"] for e in received_a] == ["asr_final"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_presence_ttl_expiry():
    pubsub = InMemoryPubSub(presence_ttl_sec=0.2)

    assert await pubsub.get_pc_online("room-a") is False

    await pubsub.set_pc_presence("room-a", True)
    assert await pubsub.get_pc_online("room-a") is True

    # Heartbeat refresh keeps it online.
    await asyncio.sleep(0.1)
    await pubsub.set_pc_presence("room-a", True)
    await asyncio.sleep(0.15)
    assert await pubsub.get_pc_online("room-a") is True

    # Without refresh it expires.
    await asyncio.sleep(0.15)
    assert await pubsub.get_pc_online("room-a") is False


async def test_presence_explicit_offline():
    pubsub = InMemoryPubSub(presence_ttl_sec=15.0)
    await pubsub.set_pc_presence("room-a", True)
    assert await pubsub.get_pc_online("room-a") is True
    await pubsub.set_pc_presence("room-a", False)
    assert await pubsub.get_pc_online("room-a") is False
