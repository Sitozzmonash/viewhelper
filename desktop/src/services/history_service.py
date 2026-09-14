"""History sync and conversation clearing (PRD 18.1, 21 reconnect flow).

SQLite is the source of truth: after a mobile reconnect the client sends
``history_sync_request`` and this service answers with messages, screenshots
(preview data URL for the latest one only) and AI answers straight from the DB.
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.config.settings import ConfigStore
from src.services.base import ServiceBase
from src.storage.sqlite import Database
from src.transport.protocol import Envelope, EventType
from src.util.log import get_logger

__all__ = ["HistoryService"]

_logger = get_logger("services.history")

_MIN_LIMIT = 1
_MAX_LIMIT = 1000
_DEFAULT_LIMIT = 100


class HistoryService(ServiceBase):
    """Serves ``history_sync_request`` and ``conversation_clear``."""

    def __init__(self, *, db: Database, config: ConfigStore, transport: Any = None) -> None:
        super().__init__(transport=transport, logger_name="services.history")
        self._db = db
        self._config = config

    # -- protocol handlers ---------------------------------------------
    async def handle_history_sync_request(self, envelope: Envelope) -> None:
        """Answer with the stored history (AC-13)."""
        limit = self._resolve_limit(envelope.get("limit"))
        try:
            payload = await asyncio.to_thread(self._db.history_payload, limit)
        except Exception as exc:  # noqa: BLE001 - a broken DB must not kill the socket
            _logger.exception("history sync failed")
            await self.emit_error(f"cannot load history: {exc}", code="storage_error")
            return
        counts = {key: len(value) for key, value in payload.items() if isinstance(value, list)}
        _logger.info("history_sync_response limit=%d %s", limit, counts)
        await self.emit(EventType.HISTORY_SYNC_RESPONSE, payload)

    async def handle_conversation_clear(self, envelope: Envelope) -> None:
        """Delete all messages + conversation answers, then ack (AC-12)."""
        del envelope  # payload is empty per protocol
        try:
            messages = await asyncio.to_thread(self._db.clear_messages)
            answers = await asyncio.to_thread(self._db.clear_ai_answers, "conversation")
        except Exception as exc:  # noqa: BLE001
            _logger.exception("conversation clear failed")
            await self.emit_error(f"cannot clear conversation: {exc}", code="storage_error")
            return
        _logger.info("conversation cleared (%d message(s), %d answer(s))", messages, answers)
        await self.emit(
            EventType.CONVERSATION_CLEARED, {"messages": messages, "ai_answers": answers}
        )

    # -- helpers --------------------------------------------------------
    def _resolve_limit(self, raw: Any) -> int:
        """Clamp the requested limit, defaulting to ``storage.history_limit``."""
        default = self._config.config.storage.history_limit or _DEFAULT_LIMIT
        try:
            limit = int(raw) if raw is not None else default
        except (TypeError, ValueError):
            return max(_MIN_LIMIT, min(_MAX_LIMIT, default))
        return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))
