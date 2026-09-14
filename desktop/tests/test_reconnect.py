"""Reconnect backoff: 1, 2, 4, 8, 16, 30, 30 ... (PRD 21)."""

from __future__ import annotations

from src.transport.reconnect import (
    AUTH_TIMEOUT_SEC,
    HEARTBEAT_INTERVAL_SEC,
    PRESENCE_TIMEOUT_SEC,
    BackoffPolicy,
)


def test_protocol_cadence_constants() -> None:
    assert HEARTBEAT_INTERVAL_SEC == 5.0
    assert PRESENCE_TIMEOUT_SEC == 15.0
    assert AUTH_TIMEOUT_SEC == 15.0


def test_default_backoff_sequence_matches_the_spec() -> None:
    policy = BackoffPolicy()
    assert [policy.next_delay() for _ in range(8)] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


def test_reset_restarts_the_sequence() -> None:
    policy = BackoffPolicy()
    policy.next_delay()
    policy.next_delay()
    policy.reset()
    assert policy.attempt == 0
    assert policy.next_delay() == 1.0


def test_peek_does_not_consume_the_attempt() -> None:
    policy = BackoffPolicy()
    assert policy.peek_delay() == 1.0
    assert policy.peek_delay() == 1.0
    assert policy.next_delay() == 1.0
    assert policy.peek_delay() == 2.0


def test_custom_policy_respects_its_ceiling() -> None:
    policy = BackoffPolicy(initial_sec=0.5, factor=3.0, max_sec=4.0)
    assert [policy.next_delay() for _ in range(4)] == [0.5, 1.5, 4.0, 4.0]


def test_jitter_stays_within_bounds() -> None:
    policy = BackoffPolicy(initial_sec=1.0, jitter_sec=0.5)
    for _ in range(20):
        delay = policy.next_delay()
        assert 1.0 <= delay <= 1.5
        policy.reset()


async def test_wait_sleeps_and_returns_the_delay() -> None:
    policy = BackoffPolicy(initial_sec=0.01)
    delay = await policy.wait()
    assert delay == 0.01
    assert policy.attempt == 1
