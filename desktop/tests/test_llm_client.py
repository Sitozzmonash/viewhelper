"""OpenAI-compatible streaming client: SSE parsing, typed errors, cancellation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import aclosing

import httpx
import pytest

from src.config.env import ProviderCredentials
from src.llm.client import (
    SSE_DONE,
    LLMError,
    LLMHTTPError,
    LLMNotConfiguredError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    LLMTransportError,
    OpenAICompatClient,
    StreamEvent,
    http_error_from_status,
    parse_sse_line,
)

MESSAGES = [{"role": "user", "content": "你好"}]


def provider(**overrides: object) -> ProviderCredentials:
    defaults: dict = {
        "api_key": "sk-test-abcdef123456",
        "base_url": "https://llm.test/v1",
        "model": "test-model",
        "timeout_sec": 5.0,
        "connect_timeout_sec": 2.0,
    }
    defaults.update(overrides)
    return ProviderCredentials(**defaults)


def sse(delta: str) -> bytes:
    chunk = {"choices": [{"delta": {"content": delta}}]}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


USAGE_LINE = b'data: {"choices":[],"usage":{"completion_tokens":42,"prompt_tokens":7}}\n\n'
DONE_LINE = b"data: [DONE]\n\n"


class TrackingStream(httpx.AsyncByteStream):
    """Byte stream that records whether httpx closed it."""

    def __init__(self, chunks: list[bytes], *, hang_after: int | None = None) -> None:
        self._chunks = chunks
        self._hang_after = hang_after
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self._chunks):
            if self._hang_after is not None and index == self._hang_after:
                await asyncio.sleep(30)  # simulate a stalled provider
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class TrackingTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream: TrackingStream, status: int = 200) -> None:
        self._stream = stream
        self._status = status

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            self._status,
            request=request,
            stream=self._stream,
            headers={"content-type": "text/event-stream"},
        )


def client_with_body(body: list[bytes], status: int = 200, headers: dict | None = None):
    """Client backed by a MockTransport returning a fixed SSE body."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            request=request,
            content=b"".join(body),
            headers={"content-type": "text/event-stream", **(headers or {})},
        )

    return OpenAICompatClient(
        provider(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def client_raising(exc_factory) -> OpenAICompatClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_factory(request)

    return OpenAICompatClient(
        provider(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


# ------------------------------------------------------------- parse_sse_line


def test_parse_sse_line_variants() -> None:
    assert parse_sse_line("") is None
    assert parse_sse_line(": keep-alive") is None
    assert parse_sse_line("event: message") is None
    assert parse_sse_line("data:") is None
    assert parse_sse_line("data: not-json") is None
    assert parse_sse_line("data: [DONE]") is SSE_DONE

    event = parse_sse_line('data: {"choices":[{"delta":{"content":"你"}}]}')
    assert isinstance(event, StreamEvent) and event.delta == "你"

    # completions-style providers stream plain "text"
    event = parse_sse_line('data: {"choices":[{"text":"好"}]}')
    assert isinstance(event, StreamEvent) and event.delta == "好"

    # finish_reason without delta
    event = parse_sse_line('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
    assert isinstance(event, StreamEvent) and event.finish_reason == "stop"

    # usage-only chunk
    event = parse_sse_line('data: {"usage":{"completion_tokens":"17"}}')
    assert isinstance(event, StreamEvent)
    assert event.usage is not None and event.usage.completion_tokens == 17


# -------------------------------------------------------------------- streaming


async def test_stream_chat_yields_deltas_and_usage() -> None:
    client = client_with_body([sse("你"), sse("好"), USAGE_LINE, DONE_LINE, sse("unreachable")])
    events: list[StreamEvent] = []
    async with aclosing(client.stream_chat(MESSAGES)) as stream:
        async for event in stream:
            events.append(event)
    await client.aclose()

    assert "".join(event.delta for event in events) == "你好"
    usage_events = [event for event in events if event.usage is not None]
    assert usage_events and usage_events[-1].usage.completion_tokens == 42
    # [DONE] terminates the stream: the chunk after it is never yielded.
    assert all(event.delta != "unreachable" for event in events)


async def test_stream_chat_posts_streaming_payload_with_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, request=request, content=DONE_LINE)

    client = OpenAICompatClient(
        provider(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    async with aclosing(client.stream_chat(MESSAGES)) as stream:
        async for _ in stream:
            pass
    await client.aclose()

    request = seen[0]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"].startswith("Bearer ")
    body = json.loads(request.content)
    assert body["stream"] is True
    assert body["model"] == "test-model"
    assert body["stream_options"] == {"include_usage": True}


async def test_not_configured_provider_raises_typed_error() -> None:
    client = OpenAICompatClient(provider(api_key=""))
    with pytest.raises(LLMNotConfiguredError):
        async with aclosing(client.stream_chat(MESSAGES)) as stream:
            async for _ in stream:
                pass


# ---------------------------------------------------------------- typed errors


async def test_rate_limit_429_raises_with_retry_after() -> None:
    body = json.dumps({"error": {"message": "slow down"}}).encode()
    client = client_with_body([body], status=429, headers={"retry-after": "1.5"})
    with pytest.raises(LLMRateLimitError) as info:
        async with aclosing(client.stream_chat(MESSAGES)) as stream:
            async for _ in stream:
                pass
    await client.aclose()
    assert info.value.status_code == 429
    assert info.value.retry_after_sec == 1.5
    assert "slow down" in info.value.message


async def test_server_error_5xx_raises() -> None:
    client = client_with_body([b"internal boom"], status=503)
    with pytest.raises(LLMServerError) as info:
        async with aclosing(client.stream_chat(MESSAGES)) as stream:
            async for _ in stream:
                pass
    await client.aclose()
    assert info.value.status_code == 503


async def test_timeout_raises_typed_error() -> None:
    client = client_raising(lambda request: httpx.ConnectTimeout("too slow", request=request))
    with pytest.raises(LLMTimeoutError):
        async with aclosing(client.stream_chat(MESSAGES)) as stream:
            async for _ in stream:
                pass
    await client.aclose()


async def test_transport_failure_raises_typed_error() -> None:
    client = client_raising(lambda request: httpx.ConnectError("dns down", request=request))
    with pytest.raises(LLMTransportError):
        async with aclosing(client.stream_chat(MESSAGES)) as stream:
            async for _ in stream:
                pass
    await client.aclose()


def test_http_error_from_status_mapping() -> None:
    assert isinstance(http_error_from_status(429, b"{}"), LLMRateLimitError)
    assert isinstance(http_error_from_status(500, b"{}"), LLMServerError)
    assert isinstance(http_error_from_status(408, b"{}"), LLMTimeoutError)
    assert isinstance(http_error_from_status(401, b"{}"), LLMHTTPError)
    assert isinstance(http_error_from_status(418, b"{}"), LLMHTTPError)
    assert all(isinstance(http_error_from_status(code, b"{}"), LLMError) for code in (429, 500, 408, 401, 418))


# ----------------------------------------------------------------- cancellation


async def test_closing_the_generator_closes_the_http_stream() -> None:
    stream = TrackingStream([sse("你"), sse("好"), DONE_LINE])
    client = OpenAICompatClient(
        provider(), client=httpx.AsyncClient(transport=TrackingTransport(stream))
    )
    generator = client.stream_chat(MESSAGES)
    event = await generator.__anext__()
    assert event.delta == "你"
    assert stream.closed is False

    await generator.aclose()  # what contextlib.aclosing does on cancellation
    assert stream.closed is True
    await client.aclose()


async def test_task_cancellation_closes_the_http_stream() -> None:
    """PRD 11 / AC-07: cancelling the consumer really closes the upstream stream."""
    stream = TrackingStream([sse("你"), sse("好"), DONE_LINE], hang_after=1)
    client = OpenAICompatClient(
        provider(), client=httpx.AsyncClient(transport=TrackingTransport(stream))
    )
    received: list[str] = []

    async def consume() -> None:
        async with aclosing(client.stream_chat(MESSAGES)) as chat:
            async for event in chat:
                if event.delta:
                    received.append(event.delta)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # first delta arrives, then the provider stalls
    assert received == ["你"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed is True
    await client.aclose()


async def test_error_body_is_never_leaked_verbatim_into_logs_or_messages() -> None:
    secret_body = json.dumps({"error": {"message": "invalid key sk-test-abcdef123456"}}).encode()
    client = client_with_body([secret_body], status=500)
    with pytest.raises(LLMServerError) as info:
        async with aclosing(client.stream_chat(MESSAGES)) as stream:
            async for _ in stream:
                pass
    await client.aclose()
    assert "sk-test-abcdef123456" not in info.value.message  # redacted by util.log.redact
