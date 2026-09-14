"""Service layer: wire-facing use cases built on storage, LLM and capture.

Every service is small and single-purpose (PRD 24 forbids god-files):

* :class:`LlmRunner`          - executes at most one streaming LLM request;
* :class:`LlmDispatcher`      - routes ``llm_request`` by ``mode``;
* :class:`ConversationService`- conversation answers (PRD 9-12);
* :class:`ScreenshotService`  - capture + vision answers (PRD 13);
* :class:`TranscriptionService` - ASR -> ``asr_partial`` / ``asr_final``;
* :class:`HistoryService`     - history sync + conversation clear (PRD 18);
* :class:`SettingsService`    - settings get/update (PRD 8.3).
"""

from __future__ import annotations

from src.services.base import ServiceBase
from src.services.conversation_service import ConversationService
from src.services.history_service import HistoryService
from src.services.llm_dispatcher import LlmDispatcher
from src.services.llm_runner import (
    BUSY_MESSAGE,
    NOT_CONFIGURED_MESSAGE,
    LlmJob,
    LlmRunner,
    SubmitResult,
)
from src.services.screenshot_service import ScreenshotService
from src.services.settings_service import SettingsService
from src.services.transcription_service import TranscriptionService

__all__ = [
    "ServiceBase",
    "LlmRunner",
    "LlmJob",
    "SubmitResult",
    "BUSY_MESSAGE",
    "NOT_CONFIGURED_MESSAGE",
    "LlmDispatcher",
    "ConversationService",
    "ScreenshotService",
    "TranscriptionService",
    "HistoryService",
    "SettingsService",
]
