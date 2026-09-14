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
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from src.config.env import ProviderCredentials
from src.llm.client import ChatMessage, LLMError, OpenAICompatClient
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
            if not job.provider.is_configured:
                self._logger.error("cannot run %s: %s", job.request_id, NOT_CONFIGURED_MESSAGE)
                return SubmitResult(False, NOT_CONFIGURED_MESSAGE)
            self._active = job
            self._task = asyncio.create_task(self._run(job), name=f"llm-{job.request_id}")
        self._logger.info(
            "llm request %s started (mode=%s model=%s target=%s)",
            job.request_id,
            job.mode,
            job.resolved_model,
            job.target_id,
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
    async def _run(self, job: LlmJob) -> None:
        tracker = TokenStatsTracker(interval_sec=self._stats_interval)
        parts: list[str] = []
        status: AnswerStatus = "done"
        error_message: str | None = None
        provider_tokens: int | None = None
        final: FinalStats | None = None
        client = self._client_for(job.provider)

        # Wall-clock budget for the whole request. httpx's per-read timeout never
        # fires when a stalled provider dribbles keep-alives after a 200 OK, which
        # would otherwise pin the single active slot forever: one hung request
        # would make every later answer get rejected as "still active".
        deadline = job.provider.timeout_sec
        await self._emit(EventType.LLM_STARTED, {"request_id": job.request_id, "mode": job.mode})
        try:
            tracker.start()
            async with asyncio.timeout(deadline):
                # aclosing guarantees the HTTP stream is closed on cancellation,
                # which is what makes "stop answering" real (PRD 11).
                async with aclosing(client.stream_chat(job.messages, model=job.model)) as stream:
                    async for event in stream:
                        if event.delta:
                            parts.append(event.delta)
                            tracker.add(event.delta)
                            await self._emit(
                                EventType.LLM_CHUNK,
                                {"request_id": job.request_id, "delta": event.delta},
                            )
                        if event.usage is not None and event.usage.completion_tokens is not None:
                            provider_tokens = event.usage.completion_tokens
                            tracker.set_usage(provider_tokens)
                        if tracker.should_emit():
                            snapshot = tracker.snapshot()
                            await self._emit(
                                EventType.LLM_STATS,
                                {
                                    "request_id": job.request_id,
                                    "tokens_per_second": snapshot.tokens_per_second,
                                    "elapsed_ms": snapshot.elapsed_ms,
                                    "estimated_tokens": snapshot.estimated_tokens,
                                },
                            )
        except asyncio.CancelledError:
            status = "cancelled"
            self._logger.info("llm request %s cancelled by user", job.request_id)
            raise
        except TimeoutError:
            # asyncio.timeout deadline: the provider stalled mid-stream. Release
            # the slot (finally) and surface one llm_error so the client stops
            # waiting and the user can retry instead of being blocked forever.
            status = "error"
            error_message = f"provider did not finish within {deadline:g}s"
            self._logger.warning(
                "llm request %s aborted: no completion within %gs (wall-clock timeout)",
                job.request_id,
                deadline,
            )
        except LLMError as exc:
            status = "error"
            error_message = redact(exc.message)
            self._logger.warning("llm request %s failed: %s", job.request_id, error_message)
        except Exception as exc:  # noqa: BLE001 - never let a request kill the app
            status = "error"
            error_message = redact(f"{type(exc).__name__}: {exc}")
            self._logger.exception("llm request %s crashed", job.request_id)
        finally:
            # Synchronous on purpose: this must also run when the task is being
            # cancelled (no awaits allowed in that path).
            final = tracker.finalize(provider_tokens)
            self._persist(job, status, "".join(parts), final, error_message)
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
                "llm request %s done: %d tokens in %dms (%.1f token/s, provider_usage=%s)",
                job.request_id,
                final.completion_tokens,
                final.elapsed_ms,
                final.tokens_per_second,
                final.from_provider_usage,
            )
        elif status == "error":
            await self._emit(
                EventType.LLM_ERROR,
                {"request_id": job.request_id, "message": error_message or "unknown error"},
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
    ) -> None:
        try:
            self._db.insert_ai_answer(
                request_id=job.request_id,
                mode=job.mode,
                target_id=job.target_id,
                model=job.resolved_model,
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
