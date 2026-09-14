"""LlmRunner: wall-clock timeout, single-active rule, slot release.

Regression cover for the incident where a provider returned ``200 OK`` and then
stalled the SSE stream (keep-alive trickle). httpx's per-read timeout never
fired, so the request stayed ``active`` forever and every later answer was
rejected as "still active" -- one hung request bricked the whole assistant.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from src.config.env import ProviderCredentials
from src.llm.client import StreamEvent, Usage
from src.services.llm_runner import BUSY_MESSAGE, LlmJob, LlmRunner
from src.storage.sqlite import Database

MESSAGES = [{"role": "user", "content": "hi"}]


def _provider(timeout_sec: float) -> ProviderCredentials:
    return ProviderCredentials(
        api_key="sk-test-abcdef123456",
        base_url="https://llm.test/v1",
        model="test-model",
        timeout_sec=timeout_sec,
        connect_timeout_sec=0.1,
    )


class HangingClient:
    """Fake client: emits one delta then stalls (200 OK, body never finishes)."""

    def __init__(self, provider: ProviderCredentials) -> None:
        self._provider = provider
        self.closed = False

    @property
    def model(self) -> str:
        return self._provider.model

    async def stream_chat(
        self, messages: Any, *, model: str | None = None, extra: Any = None
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(delta="partial")
        await asyncio.sleep(30)  # far longer than any test deadline
        yield StreamEvent(delta="unreachable")  # pragma: no cover - never reached

    async def aclose(self) -> None:
        self.closed = True


class EchoClient:
    """Fake client that completes normally (happy path)."""

    def __init__(self, provider: ProviderCredentials) -> None:
        self._provider = provider

    @property
    def model(self) -> str:
        return self._provider.model

    async def stream_chat(
        self, messages: Any, *, model: str | None = None, extra: Any = None
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(delta="你")
        yield StreamEvent(delta="好")
        yield StreamEvent(finish_reason="stop", usage=Usage(completion_tokens=2))

    async def aclose(self) -> None:
        return None


class DeadClient:
    """Fake client that accepts the request but never streams a byte.

    Mirrors the DeepSeek outage symptom: ``200 OK`` then an empty, hung body.
    """

    def __init__(self, provider: ProviderCredentials) -> None:
        self._provider = provider

    @property
    def model(self) -> str:
        return self._provider.model

    async def stream_chat(
        self, messages: Any, *, model: str | None = None, extra: Any = None
    ) -> AsyncIterator[StreamEvent]:
        await asyncio.sleep(30)  # never yields; only the deadline ends it
        yield StreamEvent(delta="unreachable")  # pragma: no cover - never reached

    async def aclose(self) -> None:
        return None


def _model_provider(model: str, timeout_sec: float, base_url: str = "https://llm.test/v1") -> ProviderCredentials:
    return ProviderCredentials(
        api_key="sk-test-abcdef123456",
        base_url=base_url,
        model=model,
        timeout_sec=timeout_sec,
        connect_timeout_sec=0.1,
    )


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 3.0, interval: float = 0.01
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def test_stalled_stream_is_aborted_and_slot_released(
    database: Database, transport: Any
) -> None:
    runner = LlmRunner(transport=transport, db=database, client_factory=HangingClient)
    job = LlmJob(
        request_id="req-stall",
        mode="conversation",
        messages=MESSAGES,
        provider=_provider(timeout_sec=0.3),
    )

    assert (await runner.submit(job)).accepted
    assert runner.is_busy

    # The 0.3s wall-clock deadline must abort the 30s stall, release the slot
    # and surface exactly one llm_error (never llm_done).
    assert await _wait_until(lambda: not runner.is_busy, timeout=3.0)
    assert runner.active_request_id is None

    errors = transport.payloads("llm_error")
    assert len(errors) == 1
    assert errors[0]["request_id"] == "req-stall"
    assert "within" in errors[0]["message"]
    assert transport.count("llm_done") == 0

    # Slot is reusable: a second request is accepted, not rejected as busy.
    job2 = LlmJob(
        request_id="req-after",
        mode="conversation",
        messages=MESSAGES,
        provider=_provider(timeout_sec=0.3),
    )
    assert (await runner.submit(job2)).accepted

    await runner.shutdown()


async def test_busy_rejects_second_request(database: Database, transport: Any) -> None:
    runner = LlmRunner(transport=transport, db=database, client_factory=HangingClient)
    job1 = LlmJob(
        request_id="req-a",
        mode="conversation",
        messages=MESSAGES,
        provider=_provider(timeout_sec=5.0),
    )
    assert (await runner.submit(job1)).accepted

    job2 = LlmJob(
        request_id="req-b",
        mode="conversation",
        messages=MESSAGES,
        provider=_provider(timeout_sec=5.0),
    )
    result2 = await runner.submit(job2)
    assert not result2.accepted
    assert result2.reason == BUSY_MESSAGE

    await runner.shutdown()


async def test_normal_stream_completes_and_emits_done(
    database: Database, transport: Any
) -> None:
    runner = LlmRunner(transport=transport, db=database, client_factory=EchoClient)
    job = LlmJob(
        request_id="req-ok",
        mode="conversation",
        messages=MESSAGES,
        provider=_provider(timeout_sec=5.0),
    )
    assert (await runner.submit(job)).accepted

    assert await _wait_until(lambda: not runner.is_busy, timeout=3.0)
    assert transport.count("llm_done") == 1
    assert transport.count("llm_error") == 0
    chunks = transport.payloads("llm_chunk")
    assert "".join(chunk["delta"] for chunk in chunks) == "你好"

    await runner.shutdown()


def _routing_factory(mapping: dict[str, Callable[[ProviderCredentials], Any]]) -> Callable[[ProviderCredentials], Any]:
    def factory(provider: ProviderCredentials) -> Any:
        return mapping[provider.model](provider)

    return factory


async def test_dead_primary_falls_back_to_backup(database: Database, transport: Any) -> None:
    """A primary that hangs with zero output must auto-retry on the fallback."""
    runner = LlmRunner(
        transport=transport,
        db=database,
        client_factory=_routing_factory({"dead-primary": DeadClient, "backup-echo": EchoClient}),
    )
    job = LlmJob(
        request_id="req-fallback",
        mode="conversation",
        messages=MESSAGES,
        provider=_model_provider("dead-primary", timeout_sec=0.3),
        fallback=_model_provider("backup-echo", timeout_sec=5.0, base_url="https://backup.test/v1"),
    )
    assert (await runner.submit(job)).accepted

    assert await _wait_until(lambda: not runner.is_busy, timeout=3.0)
    # Exactly one answer, produced by the fallback; no duplicate text, no error.
    assert transport.count("llm_done") == 1
    assert transport.count("llm_error") == 0
    chunks = transport.payloads("llm_chunk")
    assert "".join(chunk["delta"] for chunk in chunks) == "你好"

    record = database.get_ai_answer("req-fallback")
    assert record is not None
    assert record.model == "backup-echo"
    assert record.status == "done"

    await runner.shutdown()


async def test_no_fallback_after_partial_stream(database: Database, transport: Any) -> None:
    """Once the primary streamed a delta, a retry would duplicate text: don't fall back."""
    runner = LlmRunner(
        transport=transport,
        db=database,
        client_factory=_routing_factory({"hang-after-one": HangingClient, "backup-echo": EchoClient}),
    )
    job = LlmJob(
        request_id="req-partial",
        mode="conversation",
        messages=MESSAGES,
        provider=_model_provider("hang-after-one", timeout_sec=0.3),
        fallback=_model_provider("backup-echo", timeout_sec=5.0, base_url="https://backup.test/v1"),
    )
    assert (await runner.submit(job)).accepted

    assert await _wait_until(lambda: not runner.is_busy, timeout=3.0)
    assert transport.count("llm_error") == 1
    assert transport.count("llm_done") == 0
    # Only the primary's partial reached the client; the backup was never used.
    chunks = transport.payloads("llm_chunk")
    assert "".join(chunk["delta"] for chunk in chunks) == "partial"

    await runner.shutdown()
