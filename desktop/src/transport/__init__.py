"""Transport layer: shared protocol envelope plus the relay WebSocket client."""

from __future__ import annotations

from src.transport.protocol import (
    ALL_EVENT_TYPES,
    MOBILE_TO_PC_EVENTS,
    PC_TO_MOBILE_EVENTS,
    Envelope,
    EventType,
    build_envelope,
    envelope_from_dict,
    is_known_event_type,
    new_event_id,
    parse_envelope,
)
from src.transport.reconnect import HEARTBEAT_INTERVAL_SEC, BackoffPolicy
from src.transport.websocket import RelayTransport, resolve_ws_url

__all__ = [
    "ALL_EVENT_TYPES",
    "MOBILE_TO_PC_EVENTS",
    "PC_TO_MOBILE_EVENTS",
    "Envelope",
    "EventType",
    "build_envelope",
    "envelope_from_dict",
    "is_known_event_type",
    "new_event_id",
    "parse_envelope",
    "BackoffPolicy",
    "HEARTBEAT_INTERVAL_SEC",
    "RelayTransport",
    "resolve_ws_url",
]
