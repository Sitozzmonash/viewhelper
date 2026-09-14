"""Provider-agnostic OpenAI-compatible streaming client.

* ``POST {base_url}/chat/completions`` with ``stream=true`` over
  :class:`httpx.AsyncClient`;
* Server-Sent-Events parsing that yields incremental deltas and surfaces
  ``usage.completion_tokens`` when the provider sends it (PRD 12);
* typed errors for timeout / 429 / 5xx so callers can react differently;
* cancellation safe: the HTTP stream lives inside an ``async with`` in an async
  generator, so ``aclose()`` (or a cancelled consumer task) always closes the
  upstream connection instead of silently draining tokens (PRD 11 / AC-07).

API keys are never logged; error bodies are redacted and truncated.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from src.config.env import ProviderCredentials
from src.util.log import get_logger, redact

__all__ = [
    "LLMError",
    "LLMNotConfiguredError",
    "LLMTimeoutError",
    "LLMRateLimitError",
    "LLMServerError",
    "LLMHTTPError",
    "LLMTransportError",
    "Usage",
    "StreamEvent",
    "SSE_DONE",
    "parse_sse_line",
    "parse_completion_chunk",
    "http_error_from_status",
    "OpenAICompatClient",
    "ChatMessage",
]

ChatMessage = dict[str, Any]

_logger = get_logger(__name__)

_MAX_ERROR_BODY = 400


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class LLMError(RuntimeError):
    """Base class for every LLM provider failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_sec: float | None = None,
        provider_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retry_after_sec = retry_after_sec
        self.provider_message = provider_message


class LLMNotConfiguredError(LLMError):
    """API key / base URL / model missing from the environment."""


class LLMTimeoutError(LLMError):
    """Provider did not respond in time."""


class LLMRateLimitError(LLMError):
    """HTTP 429 from the provider."""


class LLMServerError(LLMError):
    """HTTP 5xx from the provider."""


class LLMHTTPError(LLMError):
    """Any other non-2xx HTTP response."""


class LLMTransportError(LLMError):
    """Network level failure (DNS, TLS, connection reset, ...)."""


# ---------------------------------------------------------------------------
# Stream events
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Usage:
    """``usage`` object reported by the provider (all fields optional)."""

    completion_tokens: int | None = None
    prompt_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One parsed SSE chunk."""

    delta: str = ""
    finish_reason: str | None = None
    usage: Usage | None = None
    model: str | None = None


class _SseDone:
    """Sentinel for the terminating ``data: [DONE]`` line."""

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return "<SSE_DONE>"


SSE_DONE = _SseDone()


def parse_sse_line(line: str) -> StreamEvent | _SseDone | None:
    """Parse one raw SSE line.

    Returns ``None`` for keep-alives/comments/blank lines and non-JSON data,
    :data:`SSE_DONE` for ``[DONE]`` and a :class:`StreamEvent` otherwise.
    """
    if not line:
        return None
    stripped = line.strip()
    if not stripped or stripped.startswith(":"):
        return None
    if not stripped.startswith("data:"):
        # event:/id:/retry: fields are irrelevant for chat completions.
        return None
    data = stripped[len("data:") :].strip()
    if not data:
        return None
    if data == "[DONE]":
        return SSE_DONE
    return parse_completion_chunk(data)


def parse_completion_chunk(data: str) -> StreamEvent | None:
    """Parse the JSON body of an SSE ``data:`` field."""
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        _logger.debug("ignoring malformed SSE chunk (%d bytes)", len(data))
        return None
    if not isinstance(chunk, dict):
        return None

    usage = _parse_usage(chunk.get("usage"))
    model = chunk.get("model") if isinstance(chunk.get("model"), str) else None

    delta = ""
    finish_reason: str | None = None
    choices = chunk.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            delta_content = first.get("delta")
            if isinstance(delta_content, dict):
                content = delta_content.get("content")
                if isinstance(content, str):
                    delta = content
                elif isinstance(delta_content.get("text"), str):
                    delta = delta_content["text"]
            elif isinstance(delta_content, str):
                delta = delta_content
            if not delta and isinstance(first.get("text"), str):
                # Completions-style providers stream plain "text".
                delta = first["text"]
            reason = first.get("finish_reason")
            finish_reason = reason if isinstance(reason, str) else None

    if not delta and finish_reason is None and usage is None:
        return None
    return StreamEvent(delta=delta, finish_reason=finish_reason, usage=usage, model=model)


def _parse_usage(raw: Any) -> Usage | None:
    if not isinstance(raw, dict):
        return None
    return Usage(
        completion_tokens=_as_int(raw.get("completion_tokens")),
        prompt_tokens=_as_int(raw.get("prompt_tokens")),
        total_tokens=_as_int(raw.get("total_tokens")),
    )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def http_error_from_status(
    status_code: int, body: bytes | str, headers: Mapping[str, str] | None = None
) -> LLMError:
    """Map an HTTP status onto the typed error hierarchy."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else body
    provider_message = _extract_error_message(text)
    summary = redact(provider_message or text.strip())[:_MAX_ERROR_BODY] or f"HTTP {status_code}"
    retry_after = _parse_retry_after((headers or {}).get("retry-after"))

    if status_code in (408, 499):
        return LLMTimeoutError(f"provider timeout (HTTP {status_code}): {summary}", status_code=status_code)
    if status_code == 429:
        return LLMRateLimitError(
            f"provider rate limit (HTTP 429): {summary}",
            status_code=429,
            retry_after_sec=retry_after,
            provider_message=provider_message,
        )
    if 500 <= status_code <= 599:
        return LLMServerError(
            f"provider server error (HTTP {status_code}): {summary}",
            status_code=status_code,
            provider_message=provider_message,
        )
    if status_code in (401, 403):
        return LLMHTTPError(
            f"provider authentication failed (HTTP {status_code})",
            status_code=status_code,
            provider_message=provider_message,
        )
    return LLMHTTPError(
        f"provider returned HTTP {status_code}: {summary}",
        status_code=status_code,
        provider_message=provider_message,
    )


def _extract_error_message(text: str) -> str | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(error, str):
            return error
        if isinstance(data.get("message"), str):
            return data["message"]
    return None


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class OpenAICompatClient:
    """Minimal streaming client for any OpenAI-compatible ``/chat/completions``."""

    def __init__(
        self,
        provider: ProviderCredentials,
        *,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._provider = provider
        self._model_override = model
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(provider.timeout_sec, connect=provider.connect_timeout_sec),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=2),
        )

    # -- introspection --------------------------------------------------
    @property
    def model(self) -> str:
        """Model id used for requests."""
        return self._model_override or self._provider.model

    @property
    def provider(self) -> ProviderCredentials:
        """Underlying credentials (never log ``api_key``)."""
        return self._provider

    @property
    def is_configured(self) -> bool:
        """Whether base_url/model/api_key are all present."""
        return self._provider.is_configured

    # -- lifecycle ------------------------------------------------------
    async def aclose(self) -> None:
        """Close the underlying HTTP client when this instance owns it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> OpenAICompatClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # -- requests -------------------------------------------------------
    def build_payload(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        include_usage: bool | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Request body for ``/chat/completions``."""
        want_usage = self._provider.include_usage if include_usage is None else include_usage
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": list(messages),
            "stream": True,
        }
        if want_usage:
            payload["stream_options"] = {"include_usage": True}
        if self._provider.max_tokens is not None:
            payload["max_tokens"] = self._provider.max_tokens
        if self._provider.temperature is not None:
            payload["temperature"] = self._provider.temperature
        if extra:
            payload.update(extra)
        return payload

    def build_headers(self) -> dict[str, str]:
        """Request headers including the bearer token."""
        return {
            "Authorization": f"Bearer {self._provider.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream chat completion events.

        Use as ``async with contextlib.aclosing(client.stream_chat(...))`` (the
        LLM runner does) so the upstream stream is closed on cancellation.
        """
        provider = self._provider
        if not provider.is_configured:
            raise LLMNotConfiguredError(
                "LLM provider is not configured (need api key, base url and model)"
            )
        url = provider.endpoint
        headers = self.build_headers()
        timeout = httpx.Timeout(provider.timeout_sec, connect=provider.connect_timeout_sec)
        include_usage = provider.include_usage
        retried_without_usage = False

        while True:
            payload = self.build_payload(
                messages, model=model, include_usage=include_usage, extra=extra
            )
            try:
                async with self._client.stream(
                    "POST", url, json=payload, headers=headers, timeout=timeout
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        text = body.decode("utf-8", errors="replace")
                        if (
                            response.status_code in (400, 422)
                            and include_usage
                            and not retried_without_usage
                            and "stream_options" in text
                        ):
                            retried_without_usage = True
                            include_usage = False
                            _logger.warning(
                                "provider rejected stream_options; retrying without usage reporting"
                            )
                            continue
                        raise http_error_from_status(response.status_code, text, response.headers)

                    async for line in response.aiter_lines():
                        parsed = parse_sse_line(line)
                        if parsed is SSE_DONE:
                            return
                        if isinstance(parsed, StreamEvent):
                            yield parsed
                return
            except httpx.TimeoutException as exc:
                raise LLMTimeoutError(
                    f"provider timeout after {provider.timeout_sec:.0f}s ({type(exc).__name__})"
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMTransportError(
                    f"provider transport failure: {redact(str(exc))[:_MAX_ERROR_BODY]}"
                ) from exc
