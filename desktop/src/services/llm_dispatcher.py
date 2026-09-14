"""Routing for ``llm_request`` / ``llm_cancel`` (PRD 9-13).

``llm_request`` carries a ``mode`` discriminator; this tiny dispatcher forwards
it to the conversation or the screenshot service so both keep their own prompt
and provider (AC-14) while sharing one runner and one single-active rule.
"""

from __future__ import annotations

from typing import Any

from src.services.base import ServiceBase
from src.services.conversation_service import ConversationService
from src.services.screenshot_service import ScreenshotService
from src.transport.protocol import Envelope, EventType
from src.util.log import get_logger

__all__ = ["LlmDispatcher"]

_logger = get_logger("services.llm")

CONVERSATION = "conversation"
SCREENSHOT = "screenshot"


class LlmDispatcher(ServiceBase):
    """Splits LLM traffic by mode and forwards cancels to the runner."""

    def __init__(
        self,
        *,
        conversation: ConversationService,
        screenshot: ScreenshotService,
        transport: Any = None,
    ) -> None:
        super().__init__(transport=transport, logger_name="services.llm")
        self._conversation = conversation
        self._screenshot = screenshot

    async def handle_llm_request(self, envelope: Envelope) -> None:
        """Dispatch one ``llm_request``."""
        request_id = _clean(envelope.get("request_id"))
        if not request_id:
            _logger.warning("llm_request without request_id: %s", envelope.summary())
            await self.emit_error("request_id is required", code="bad_request")
            return

        mode = _clean(envelope.get("mode")).lower()
        if mode == CONVERSATION:
            await self._conversation.start_conversation_request(
                request_id, _clean(envelope.get("message_id")) or None
            )
            return
        if mode == SCREENSHOT:
            await self._screenshot.start_screenshot_request(
                request_id, _clean(envelope.get("screenshot_id")) or None
            )
            return

        message = f"unsupported mode: {mode or '<missing>'}"
        _logger.warning("llm_request %s: %s", request_id, message)
        await self.emit(EventType.LLM_ERROR, {"request_id": request_id, "message": message})

    async def handle_llm_cancel(self, envelope: Envelope) -> None:
        """Cancel whichever request is active (AC-07)."""
        await self._conversation.handle_llm_cancel(envelope)


def _clean(value: Any) -> str:
    """Coerce a wire value into a stripped string."""
    return "" if value is None else str(value).strip()
