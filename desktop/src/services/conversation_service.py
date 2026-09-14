"""Conversation answers: ``llm_request {mode:"conversation"}`` (PRD 9-12).

Responsibilities
----------------
* resolve the tapped bubble (``message_id``) and its context from SQLite;
* build the chat messages with the **conversation** prompt only (AC-14: the
  screenshot prompt must never leak into a conversation request);
* hand the job to :class:`~src.services.llm_runner.LlmRunner`, which enforces the
  V1 single-active rule and streams the ``llm_*`` events;
* turn every rejection into exactly one ``llm_error`` frame (AC-04).
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.config.env import EnvConfig
from src.config.settings import ConfigStore
from src.llm.conversation import ConversationTurn, build_conversation_messages, describe_request
from src.services.base import ServiceBase
from src.services.llm_runner import LlmJob, LlmRunner
from src.storage.sqlite import Database
from src.transport.protocol import Envelope, EventType
from src.util.log import get_logger, redact

__all__ = ["ConversationService", "MISSING_MESSAGE_ID", "MESSAGE_NOT_FOUND"]

_logger = get_logger("services.conversation")

MISSING_MESSAGE_ID = "message_id is required for conversation mode"
MESSAGE_NOT_FOUND = "message not found on the PC"


class ConversationService(ServiceBase):
    """Builds and submits conversation LLM requests."""

    def __init__(
        self,
        *,
        db: Database,
        config: ConfigStore,
        env: EnvConfig,
        runner: LlmRunner,
        transport: Any = None,
    ) -> None:
        super().__init__(transport=transport, logger_name="services.conversation")
        self._db = db
        self._config = config
        self._env = env
        self._runner = runner

    # -- protocol handlers ---------------------------------------------
    async def handle_llm_request(self, envelope: Envelope) -> None:
        """Handle one ``llm_request`` with ``mode="conversation"``."""
        request_id = _clean(envelope.get("request_id"))
        if not request_id:
            _logger.warning("llm_request without request_id: %s", envelope.summary())
            await self.emit_error("request_id is required", code="bad_request")
            return
        await self.start_conversation_request(request_id, _clean(envelope.get("message_id")))

    async def handle_llm_cancel(self, envelope: Envelope) -> None:
        """Handle ``llm_cancel``: stop the task and close the upstream stream (AC-07)."""
        request_id = _clean(envelope.get("request_id"))
        if not request_id:
            await self.emit_error("request_id is required", code="bad_request")
            return
        await self._runner.cancel(request_id)

    # -- request building ----------------------------------------------
    async def start_conversation_request(self, request_id: str, message_id: str | None) -> bool:
        """Answer *message_id*; returns whether the job was accepted."""
        if not message_id:
            await self._fail(request_id, MISSING_MESSAGE_ID)
            return False

        cfg = self._config.config
        try:
            context_records, target_record = await asyncio.to_thread(
                self._db.context_window, message_id, cfg.conversation.context_messages
            )
        except Exception as exc:  # noqa: BLE001 - a broken DB must not kill the handler
            _logger.exception("cannot load context for %s", request_id)
            await self._fail(request_id, redact(f"cannot load conversation: {exc}"))
            return False

        if target_record is None:
            await self._fail(request_id, MESSAGE_NOT_FOUND)
            return False

        context = [ConversationTurn.from_record(record) for record in context_records]
        target = ConversationTurn.from_record(target_record)
        messages = build_conversation_messages(
            system_prompt=cfg.prompts.conversation,
            context=context,
            target=target,
            resume_context=cfg.resume_context,
        )
        _logger.info("conversation request %s: %s", request_id, describe_request(target, context))

        job = LlmJob(
            request_id=request_id,
            mode="conversation",
            messages=messages,
            provider=self._env.provider_for("conversation"),
            target_id=target_record.id,
        )
        result = await self._runner.submit(job)
        if not result.accepted:
            await self._fail(request_id, result.reason or "request rejected")
        return result.accepted

    # -- helpers --------------------------------------------------------
    async def _fail(self, request_id: str, message: str) -> None:
        """Report a rejected request with the single ``llm_error`` frame."""
        _logger.warning("llm request %s rejected: %s", request_id, message)
        await self.emit(EventType.LLM_ERROR, {"request_id": request_id, "message": message})


def _clean(value: Any) -> str:
    """Coerce a wire value into a stripped string (``""`` when unusable)."""
    return "" if value is None else str(value).strip()
