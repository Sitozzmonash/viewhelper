"""Wire protocol helpers mirroring ``shared/protocol``.

This module is a pure-Python mirror of
``shared/protocol/events.schema.json`` + ``shared/protocol/README.md``:
envelope build/parse, event type constants and routing sets. It has no I/O and
no heavy dependencies so it can be unit tested anywhere.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from src.util.clock import utc_now_ms

__all__ = [
    "EventType",
    "ALL_EVENT_TYPES",
    "MOBILE_TO_PC_EVENTS",
    "PC_TO_MOBILE_EVENTS",
    "Envelope",
    "build_envelope",
    "parse_envelope",
    "envelope_from_dict",
    "is_known_event_type",
    "new_event_id",
]


class EventType:
    """Event ``type`` values from the shared schema."""

    AUTH = "auth"
    AUTH_OK = "auth_ok"
    HEARTBEAT = "heartbeat"
    PRESENCE = "presence"

    ASR_PARTIAL = "asr_partial"
    ASR_FINAL = "asr_final"

    HISTORY_SYNC_REQUEST = "history_sync_request"
    HISTORY_SYNC_RESPONSE = "history_sync_response"
    CONVERSATION_CLEAR = "conversation_clear"
    CONVERSATION_CLEARED = "conversation_cleared"
    SCREENSHOT_CLEAR = "screenshot_clear"
    SCREENSHOT_CLEARED = "screenshot_cleared"

    SCREENSHOT_CREATED = "screenshot_created"
    SCREENSHOT_DELETE = "screenshot_delete"
    CAPTURE_SCREEN = "capture_screen"

    LLM_REQUEST = "llm_request"
    LLM_STARTED = "llm_started"
    LLM_CHUNK = "llm_chunk"
    LLM_STATS = "llm_stats"
    LLM_DONE = "llm_done"
    LLM_CANCEL = "llm_cancel"
    LLM_CANCELLED = "llm_cancelled"
    LLM_ERROR = "llm_error"

    SETTINGS_GET = "settings_get"
    SETTINGS_UPDATE = "settings_update"
    SETTINGS_UPDATED = "settings_updated"

    ERROR = "error"


#: Events sent by the mobile client and handled by the PC (schema routing table).
MOBILE_TO_PC_EVENTS: frozenset[str] = frozenset(
    {
        EventType.LLM_REQUEST,
        EventType.LLM_CANCEL,
        EventType.CAPTURE_SCREEN,
        EventType.SCREENSHOT_DELETE,
        EventType.CONVERSATION_CLEAR,
        EventType.SCREENSHOT_CLEAR,
        EventType.HISTORY_SYNC_REQUEST,
        EventType.SETTINGS_GET,
        EventType.SETTINGS_UPDATE,
    }
)

#: Events the PC sends towards the mobile client (via relay fan-out).
PC_TO_MOBILE_EVENTS: frozenset[str] = frozenset(
    {
        EventType.ASR_PARTIAL,
        EventType.ASR_FINAL,
        EventType.SCREENSHOT_CREATED,
        EventType.LLM_STARTED,
        EventType.LLM_CHUNK,
        EventType.LLM_STATS,
        EventType.LLM_DONE,
        EventType.LLM_CANCELLED,
        EventType.LLM_ERROR,
        EventType.HISTORY_SYNC_RESPONSE,
        EventType.CONVERSATION_CLEARED,
        EventType.SCREENSHOT_CLEARED,
        EventType.SETTINGS_UPDATED,
        EventType.ERROR,
    }
)

#: Every type this implementation knows about.
ALL_EVENT_TYPES: frozenset[str] = MOBILE_TO_PC_EVENTS | PC_TO_MOBILE_EVENTS | frozenset(
    {EventType.AUTH, EventType.AUTH_OK, EventType.HEARTBEAT, EventType.PRESENCE}
)

#: Payload keys whose values are large/base64 and must never be logged verbatim.
REDACTED_PAYLOAD_KEYS: frozenset[str] = frozenset({"preview", "image_url", "url", "delta", "text", "answer"})

_MAX_LOGGED_STRING = 64


def new_event_id() -> str:
    """Fresh uuid v4 for ``event_id``."""
    return str(uuid.uuid4())


def is_known_event_type(event_type: str) -> bool:
    """Whether *event_type* is part of the shared schema."""
    return event_type in ALL_EVENT_TYPES


@dataclass(frozen=True, slots=True)
class Envelope:
    """One protocol message.

    ``timestamp`` is always UTC epoch **milliseconds**.
    """

    type: str
    event_id: str
    device_id: str
    timestamp: int
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable mapping matching the schema envelope."""
        return {
            "type": self.type,
            "event_id": self.event_id,
            "device_id": self.device_id,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        """Compact JSON text ready to be sent over the socket."""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    def get(self, key: str, default: Any = None) -> Any:
        """Payload lookup helper."""
        return self.payload.get(key, default)

    def summary(self) -> str:
        """Short, secret-safe description for logs (never dumps base64)."""
        parts: list[str] = []
        for key, value in self.payload.items():
            if key in REDACTED_PAYLOAD_KEYS:
                if isinstance(value, str):
                    parts.append(f"{key}=<{len(value)} chars>")
                else:
                    parts.append(f"{key}=<{type(value).__name__}>")
                continue
            if isinstance(value, str) and len(value) > _MAX_LOGGED_STRING:
                parts.append(f"{key}={value[:_MAX_LOGGED_STRING]}…")
            elif isinstance(value, (list, tuple)):
                parts.append(f"{key}=[{len(value)}]")
            else:
                parts.append(f"{key}={value}")
        payload_text = " ".join(parts)
        return f"{self.type} event_id={self.event_id[:8]} {payload_text}".strip()


def build_envelope(
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    *,
    device_id: str,
    event_id: str | None = None,
    timestamp: int | None = None,
) -> Envelope:
    """Build an outgoing envelope with a fresh ``event_id`` and UTC-ms timestamp."""
    return Envelope(
        type=event_type,
        event_id=event_id or new_event_id(),
        device_id=device_id,
        timestamp=timestamp if timestamp is not None else utc_now_ms(),
        payload=dict(payload or {}),
    )


def envelope_from_dict(data: Mapping[str, Any]) -> Envelope | None:
    """Tolerantly convert a decoded JSON object into an :class:`Envelope`.

    Returns ``None`` for anything that is not an object with a ``type``; unknown
    types are preserved (callers ignore them per protocol rule "Unknown type
    must be ignored, not crash").
    """
    event_type = data.get("type")
    if not isinstance(event_type, str) or not event_type:
        return None

    event_id = data.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        event_id = new_event_id()

    device_id = data.get("device_id")
    if not isinstance(device_id, str):
        device_id = str(device_id) if device_id is not None else ""

    raw_timestamp = data.get("timestamp")
    timestamp = _coerce_ms(raw_timestamp)

    payload = data.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    return Envelope(
        type=event_type,
        event_id=event_id,
        device_id=device_id,
        timestamp=timestamp,
        payload=payload,
    )


def _coerce_ms(value: Any) -> int:
    """Accept int/float/digit-string timestamps, defaulting to now."""
    if isinstance(value, bool):
        return utc_now_ms()
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(float(value.strip()))
    return utc_now_ms()


def parse_envelope(raw: str | bytes | bytearray) -> Envelope | None:
    """Parse a raw socket frame; ``None`` when it is not a usable envelope."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return envelope_from_dict(data)
