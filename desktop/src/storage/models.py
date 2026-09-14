"""Storage records (PRD 19.1) and their wire representations.

Plain dataclasses keep the storage layer free of framework coupling; every
``to_wire`` output matches ``shared/protocol/events.schema.json``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from src.util.clock import duration_sec

__all__ = [
    "Speaker",
    "AudioSource",
    "AnswerMode",
    "AnswerStatus",
    "SessionRecord",
    "MessageRecord",
    "ScreenshotRecord",
    "AiAnswerRecord",
    "SPEAKER_LABELS",
]

Speaker = Literal["me", "other"]
AudioSource = Literal["mic", "system"]
AnswerMode = Literal["conversation", "screenshot"]
AnswerStatus = Literal["done", "cancelled", "error"]

#: Chinese labels used when rendering context for the LLM (PRD 9).
SPEAKER_LABELS: dict[str, str] = {"me": "我", "other": "对方"}


@dataclass(slots=True)
class SessionRecord:
    """One desktop run (started at boot, ended at shutdown)."""

    id: str
    started_at: int
    ended_at: int | None = None
    title: str | None = None


@dataclass(slots=True)
class MessageRecord:
    """A finalised ASR utterance (the unit shown as a chat bubble)."""

    id: str
    speaker: Speaker
    source: AudioSource
    text: str
    created_at: int
    session_id: str | None = None
    utterance_id: str | None = None
    is_final: bool = True
    started_at: int | None = None
    ended_at: int | None = None

    @property
    def duration(self) -> float | None:
        """Utterance duration in seconds, when both stamps are known."""
        return duration_sec(self.started_at, self.ended_at)

    def to_wire(self) -> dict[str, Any]:
        """``history_sync_response.messages[]`` shape."""
        return {
            "message_id": self.id,
            "speaker": self.speaker,
            "text": self.text,
            "created_at": self.created_at,
            "duration_sec": self.duration,
            "utterance_id": self.utterance_id,
            "source": self.source,
        }


@dataclass(slots=True)
class ScreenshotRecord:
    """Screenshot metadata; the original PNG never leaves the PC."""

    id: str
    local_path: str
    created_at: int
    session_id: str | None = None
    preview_path: str | None = None
    width: int | None = None
    height: int | None = None
    preview_bytes: int | None = None
    preview_mime: str | None = None
    model: str | None = None
    trigger: str = "hotkey"

    def to_wire(self, preview: str | None = None) -> dict[str, Any]:
        """``screenshot_created`` / ``history_sync_response.screenshots[]`` shape.

        *preview* is a data URL and is only supplied for the latest screenshot
        during history sync (schema: "only for the latest screenshot").
        """
        return {
            "screenshot_id": self.id,
            "preview": preview,
            "created_at": self.created_at,
            "width": self.width,
            "height": self.height,
            "trigger": self.trigger,
            "model": self.model,
        }


@dataclass(slots=True)
class AiAnswerRecord:
    """One LLM answer (streamed, cancelled or failed)."""

    id: str
    request_id: str
    mode: AnswerMode
    answer: str
    status: AnswerStatus
    created_at: int
    session_id: str | None = None
    target_id: str | None = None
    model: str | None = None
    completion_tokens: int | None = None
    estimated_tokens: int | None = None
    elapsed_ms: int | None = None
    tokens_per_second: float | None = None
    error_message: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        """``history_sync_response.ai_answers[]`` shape."""
        return {
            "request_id": self.request_id,
            "mode": self.mode,
            "target_id": self.target_id,
            "answer": self.answer,
            "status": self.status,
            "tokens_per_second": self.tokens_per_second,
            "completion_tokens": self.completion_tokens,
            "elapsed_ms": self.elapsed_ms,
            "model": self.model,
            "error_message": self.error_message,
            "created_at": self.created_at,
        }
