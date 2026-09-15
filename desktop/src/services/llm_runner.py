"""Shared LLM execution controller (single active request, streaming, cancel).

Both conversation answers (PRD 9-12) and screenshot answers (PRD 13) go through
this runner so that the V1 rule "only one active LLM request" and the cancel
semantics (PRD 11 / AC-07) are implemented exactly once:

* ``llm_started`` immediately, ``llm_chunk`` per delta, ``llm_stats`` every
  ~500ms, ``llm_done`` / ``llm_cancelled`` / ``llm_error`` once;
* cancel really cancels the :class:`asyncio.Task` **and** closes the upstream
  HTTP stream (``contextlib.aclosing`` around the client generator);
* partial text is kept and persisted with ``status='cancelled'``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from src.config.env import ProviderCredentials
from src.llm.client import ChatMessage, LLMError, LLMTimeoutError, OpenAICompatClient
from src.llm.token_stats import FinalStats, TokenStatsTracker
from src.storage.models import AnswerMode, AnswerStatus
from src.storage.sqlite import Database
from src.transport.protocol import EventType
from src.transport.websocket import RelayTransport
from src.util.clock import utc_now_ms
from src.util.log import get_logger, redact

__all__ = ["LlmJob", "SubmitResult", "LlmRunner", "BUSY_MESSAGE", "NOT_CONFIGURED_MESSAGE"]

#: Exact message required by the task/protocol for the single-active rule.
BUSY_MESSAGE = "another request is active"
NOT_CONFIGURED_MESSAGE = "LLM provider is not configured on the PC"


@dataclass(frozen=True, slots=True)
class LlmJob:
    """One LLM request to execute."""

    request_id: str
    mode: AnswerMode
    messages: list[ChatMessage]
    provider: ProviderCredentials
    fallback: ProviderCredentials | None = None
    target_id: str | None = None
    model: str | None = None
    created_at: int = field(default_factory=utc_now_ms)

    @property
    def resolved_model(self) -> str:
        """Model id used for the request and stored in SQLite."""
        return self.model or self.provider.model


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Outcome of :meth:`LlmRunner.submit`."""

    accepted: bool
    reason: str | None = None


@dataclass(slots=True)
class _AttemptState:
    """Live state of a single provider attempt, mutated while streaming."""

    provider: ProviderCredentials
    model: str
    tracker: TokenStatsTracker
    parts: list[str] = field(default_factory=list)
    provider_tokens: int | None = None
    #: True once any delta reached the client. A fallback is only safe while this
    #: is False, otherwise the retry would duplicate already-streamed text.
    emitted: bool = False

    @property
    def text(self) -> str:
        return "".join(self.parts)


ClientFactory = Callable[[ProviderCredentials], OpenAICompatClient]
Emitter = Callable[[str, dict[str, Any]], Awaitable[bool]]


class LlmRunner:
    """Executes at most one streaming LLM request at a time."""

    def __init__(
        self,
        *,
        transport: RelayTransport,
        db: Database,
        client_factory: ClientFactory | None = None,
        stats_interval_sec: float = 0.5,
    ) -> None:
        self._transport = transport
        self._db = db
        self._client_factory: ClientFactory = client_factory or OpenAICompatClient
        self._stats_interval = stats_interval_sec
        self._clients: dict[str, OpenAICompatClient] = {}
        self._active: LlmJob | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._completed = 0
        self._logger = get_logger("llm.runner")

    # -- introspection --------------------------------------------------
    @property
    def is_busy(self) -> bool:
        """Whether a request is currently streaming."""
        return self._active is not None

    @property
    def active_request_id(self) -> str | None:
        """``request_id`` of the running request, if any."""
        return self._active.request_id if self._active else None

    @property
    def stats(self) -> dict[str, Any]:
        """Counters for diagnostics."""
        return {"completed": self._completed, "active": self.active_request_id, "clients": len(self._clients)}

    # -- submission -----------------------------------------------------
    async def submit(self, job: LlmJob) -> SubmitResult:
        """Start *job*; rejects when busy or when the provider is not configured."""
        async with self._lock:
            if self._active is not None:
                self._logger.warning(
                    "rejecting %s (%s): %s is still active",
                    job.request_id,
                    job.mode,
                    self._active.request_id,
                )
                return SubmitResult(False, BUSY_MESSAGE)
            if not job.provider.is_configured and not (job.fallback and job.fallback.is_configured):
                self._logger.error("cannot run %s: %s", job.request_id, NOT_CONFIGURED_MESSAGE)
                return SubmitResult(False, NOT_CONFIGURED_MESSAGE)
            self._active = job
            self._task = asyncio.create_task(self._run(job), name=f"llm-{job.request_id}")
        self._logger.info(
            "llm request %s started (mode=%s model=%s target=%s fallback=%s)",
            job.request_id,
            job.mode,
            job.resolved_model,
            job.target_id,
            job.fallback.model if (job.fallback and job.fallback.is_configured) else "none",
        )
        return SubmitResult(True)

    async def cancel(self, request_id: str) -> bool:
        """Cancel the active request, close the stream and emit ``llm_cancelled``."""
        job, task = self._active, self._task
        if job is None or task is None or job.request_id != request_id:
            self._logger.info("ignoring llm_cancel for unknown/finished request %s", request_id)
            return False
        self._logger.info("cancelling llm request %s", request_id)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=10.0)
        await self._emit(EventType.LLM_CANCELLED, {"request_id": request_id})
        return True

    async def shutdown(self) -> None:
        """Cancel any running request and close cached HTTP clients."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=5.0)
        clients, self._clients = self._clients, {}
        for client in clients.values():
            with contextlib.suppress(Exception):
                await client.aclose()

    # -- execution ------------------------------------------------------
    def _provider_chain(self, job: LlmJob) -> list[ProviderCredentials]:
        """Ordered providers to try: primary first, then a distinct fallback."""
        chain: list[ProviderCredentials] = []
        if job.provider.is_configured:
            chain.append(job.provider)
        fb = job.fallback
        if fb is not None and fb.is_configured:
            key = (fb.base_url, fb.model, fb.api_key)
            if not any((p.base_url, p.model, p.api_key) == key for p in chain):
                chain.append(fb)
        return chain

    async def _run(self, job: LlmJob) -> None:
        attempts = self._provider_chain(job)
        await self._emit(
            EventType.LLM_STARTED,
            {"request_id": job.request_id, "mode": job.mode, "target_id": job.target_id},
        )

        status: AnswerStatus = "error"
        error_message: str | None = "no provider available"
        state: _AttemptState | None = None
        final: FinalStats | None = None

        try:
            for index, provider in enumerate(attempts):
                is_last = index == len(attempts) - 1
                tracker = TokenStatsTracker(interval_sec=self._stats_interval)
                state = _AttemptState(
                    provider=provider,
                    model=job.model or provider.model,
                    tracker=tracker,
                )
                tracker.start()
                try:
                    await self._stream_attempt(job, provider, state)
                except asyncio.CancelledError:
                    status = "cancelled"
                    self._logger.info("llm request %s cancelled by user", job.request_id)
                    raise
                except LLMError as exc:
                    status = "error"
                    error_message = redact(exc.message)
                    self._logger.warning(
                        "llm request %s failed on %s: %s", job.request_id, provider.model, error_message
                    )
                except Exception as exc:  # noqa: BLE001 - never let a request kill the app
                    status = "error"
                    error_message = redact(f"{type(exc).__name__}: {exc}")
                    self._logger.exception("llm request %s crashed on %s", job.request_id, provider.model)
                else:
                    status = "done"
                    error_message = None
                    break

                # Retry on the backup only when the primary produced nothing yet;
                # once deltas reached the client a retry would duplicate text.
                if not is_last and not state.emitted:
                    self._logger.warning(
                        "llm request %s: %s unavailable (%s); falling back to %s",
                        job.request_id,
                        provider.model,
                        error_message,
                        attempts[index + 1].model,
                    )
                    continue
                break
        finally:
            # Synchronous on purpose: this must also run when the task is being
            # cancelled (no awaits allowed in that path).
            if state is not None:
                final = state.tracker.finalize(state.provider_tokens)
                self._persist(job, status, state.text, final, error_message, state.model)
            self._clear_active(job.request_id)

        if status == "done" and final is not None:
            await self._emit(
                EventType.LLM_DONE,
                {
                    "request_id": job.request_id,
                    "completion_tokens": final.completion_tokens,
                    "elapsed_ms": final.elapsed_ms,
                    "tokens_per_second": final.tokens_per_second,
                },
            )
            self._logger.info(
                "llm request %s done: %d tokens in %dms (%.1f token/s, provider_usage=%s, model=%s)",
                job.request_id,
                final.completion_tokens,
                final.elapsed_ms,
                final.tokens_per_second,
                final.from_provider_usage,
                state.model if state else job.resolved_model,
            )
        elif status == "error":
            await self._emit(
                EventType.LLM_ERROR,
                {"request_id": job.request_id, "message": error_message or "unknown error"},
            )

    async def _stream_attempt(
        self, job: LlmJob, provider: ProviderCredentials, state: _AttemptState
    ) -> None:
        """Stream one provider attempt into *state*, emitting chunk/stats events.

        A per-event budget of ``min(stall_timeout, remaining wall-clock)`` makes a
        provider that accepts the request but never sends a first token fail fast
        (so the fallback kicks in quickly) while still capping total runtime.
        """
        client = self._client_for(provider)
        deadline = provider.timeout_sec
        stall = provider.stall_timeout_sec
        started = time.monotonic()
        # aclosing guarantees the HTTP stream is closed on cancellation, which is
        # what makes "stop answering" real (PRD 11).
        async with aclosing(client.stream_chat(job.messages, model=job.model)) as stream:
            iterator = stream.__aiter__()
            while True:
                elapsed = time.monotonic() - started
                remaining = deadline - elapsed
                if remaining <= 0:
                    raise LLMTimeoutError(f"provider did not finish within {deadline:g}s")
                budget = stall if stall < remaining else remaining
                try:
                    async with asyncio.timeout(budget):
                        event = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    if time.monotonic() - started >= deadline:
                        raise LLMTimeoutError(
                            f"provider did not finish within {deadline:g}s"
                        ) from None
                    raise LLMTimeoutError(
                        f"provider stalled: no data within {stall:g}s"
                    ) from None

                if event.delta:
                    state.parts.append(event.delta)
                    state.emitted = True
                    state.tracker.add(event.delta)
                    await self._emit(
                        EventType.LLM_CHUNK,
                        {"request_id": job.request_id, "delta": event.delta},
                    )
                if event.usage is not None and event.usage.completion_tokens is not None:
                    state.provider_tokens = event.usage.completion_tokens
                    state.tracker.set_usage(state.provider_tokens)
                if state.tracker.should_emit():
                    snapshot = state.tracker.snapshot()
                    await self._emit(
                        EventType.LLM_STATS,
                        {
                            "request_id": job.request_id,
                            "tokens_per_second": snapshot.tokens_per_second,
                            "elapsed_ms": snapshot.elapsed_ms,
                            "estimated_tokens": snapshot.estimated_tokens,
                        },
                    )

    # -- helpers --------------------------------------------------------
    def _client_for(self, provider: ProviderCredentials) -> OpenAICompatClient:
        digest = hashlib.sha256(
            f"{provider.base_url}|{provider.model}|{provider.api_key}".encode("utf-8")
        ).hexdigest()[:12]
        client = self._clients.get(digest)
        if client is None:
            client = self._client_factory(provider)
            self._clients[digest] = client
            self._logger.info("created LLM client for %s", provider.describe())
        return client

    def _clear_active(self, request_id: str) -> None:
        if self._active is not None and self._active.request_id == request_id:
            self._active = None
            self._task = None
            self._completed += 1

    def _persist(
        self,
        job: LlmJob,
        status: AnswerStatus,
        answer: str,
        final: Any,
        error_message: str | None,
        model: str | None = None,
    ) -> None:
        try:
            self._db.insert_ai_answer(
                request_id=job.request_id,
                mode=job.mode,
                target_id=job.target_id,
                model=model or job.resolved_model,
                answer=answer,
                completion_tokens=int(final.completion_tokens),
                estimated_tokens=int(final.estimated_tokens),
                elapsed_ms=int(final.elapsed_ms),
                tokens_per_second=float(final.tokens_per_second),
                status=status,
                error_message=error_message,
                created_at=job.created_at,
            )
        except Exception as exc:  # noqa: BLE001 - persistence must not mask cancellation
            self._logger.error("cannot persist ai_answer for %s: %s", job.request_id, exc)

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> bool:
        try:
            return await self._transport.send_event(event_type, payload)
        except Exception as exc:  # noqa: BLE001 - transport problems must not break streaming
            self._logger.warning("cannot send %s: %s", event_type, exc)
            return False
