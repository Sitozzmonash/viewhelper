"""Wire protocol envelope: build, parse, tolerate, redact."""

from __future__ import annotations

import json
import uuid

import pytest

from src.transport.protocol import (
    ALL_EVENT_TYPES,
    MOBILE_TO_PC_EVENTS,
    PC_TO_MOBILE_EVENTS,
    Envelope,
    build_envelope,
    envelope_from_dict,
    is_known_event_type,
    parse_envelope,
)
from src.util.clock import utc_now_ms


def test_build_envelope_has_uuid_event_id_and_utc_ms_timestamp() -> None:
    before = utc_now_ms()
    envelope = build_envelope("asr_final", {"speaker": "me"}, device_id="pc-1")
    after = utc_now_ms()

    assert envelope.type == "asr_final"
    assert envelope.device_id == "pc-1"
    assert uuid.UUID(envelope.event_id).version == 4
    assert before <= envelope.timestamp <= after
    assert isinstance(envelope.timestamp, int)
    assert envelope.payload == {"speaker": "me"}


def test_roundtrip_through_json() -> None:
    original = build_envelope("llm_chunk", {"request_id": "r1", "delta": "hi"}, device_id="pc-1")
    parsed = parse_envelope(original.to_json())

    assert parsed is not None
    assert parsed.to_dict() == original.to_dict()
    assert json.loads(original.to_json())["type"] == "llm_chunk"


def test_roundtrip_accepts_bytes_frames() -> None:
    original = build_envelope("heartbeat", {}, device_id="pc-1")
    parsed = parse_envelope(original.to_json().encode("utf-8"))
    assert parsed is not None
    assert parsed.event_id == original.event_id


@pytest.mark.parametrize(
    "raw",
    ["", "not json", "{", "[1,2,3]", '"a string"', "null"],
)
def test_unparsable_frames_return_none(raw: str) -> None:
    assert parse_envelope(raw) is None


def test_missing_event_id_is_regenerated() -> None:
    envelope = envelope_from_dict({"type": "presence", "payload": {"pc_online": True}})
    assert envelope is not None
    assert uuid.UUID(envelope.event_id).version == 4
    assert envelope.payload == {"pc_online": True}


@pytest.mark.parametrize(
    ("raw_timestamp", "expected"),
    [(1757858400000, 1757858400000), (1757858400000.75, 1757858400000), ("1757858400000", 1757858400000)],
)
def test_timestamp_is_coerced_to_int_ms(raw_timestamp: object, expected: int) -> None:
    envelope = envelope_from_dict(
        {"type": "asr_partial", "timestamp": raw_timestamp, "payload": {}}
    )
    assert envelope is not None
    assert envelope.timestamp == expected


def test_non_dict_payload_becomes_empty() -> None:
    envelope = envelope_from_dict({"type": "capture_screen", "payload": ["oops"]})
    assert envelope is not None
    assert envelope.payload == {}


def test_missing_type_is_rejected() -> None:
    assert envelope_from_dict({"payload": {}}) is None
    assert envelope_from_dict({"type": "", "payload": {}}) is None


def test_unknown_type_is_parsed_but_not_known() -> None:
    envelope = parse_envelope('{"type":"totally_new","event_id":"e","device_id":"d","timestamp":1}')
    assert envelope is not None
    assert envelope.type == "totally_new"
    assert is_known_event_type("totally_new") is False
    assert is_known_event_type("asr_final") is True


def test_routing_sets_match_the_shared_readme() -> None:
    assert MOBILE_TO_PC_EVENTS == {
        "llm_request",
        "llm_cancel",
        "capture_screen",
        "screenshot_delete",
        "conversation_clear",
        "screenshot_clear",
        "history_sync_request",
        "settings_get",
        "settings_update",
    }
    assert {
        "asr_partial",
        "asr_final",
        "screenshot_created",
        "llm_started",
        "llm_chunk",
        "llm_stats",
        "llm_done",
        "llm_cancelled",
        "llm_error",
        "history_sync_response",
        "conversation_cleared",
        "screenshot_cleared",
        "settings_updated",
        "error",
    } <= PC_TO_MOBILE_EVENTS
    assert ALL_EVENT_TYPES >= MOBILE_TO_PC_EVENTS | PC_TO_MOBILE_EVENTS
    assert {"auth", "auth_ok", "heartbeat", "presence"} <= ALL_EVENT_TYPES


def test_summary_never_dumps_base64_or_long_text() -> None:
    envelope = Envelope(
        type="screenshot_created",
        event_id="e" * 36,
        device_id="pc-1",
        timestamp=1,
        payload={
            "screenshot_id": "ss-1",
            "preview": "data:image/jpeg;base64," + "A" * 5000,
            "text": "x" * 500,
        },
    )
    summary = envelope.summary()

    assert "AAAA" not in summary
    assert "xxxx" not in summary
    assert "5023 chars" in summary  # len(preview) reported instead of the value
    assert "ss-1" in summary
    assert summary.startswith("screenshot_created")


def test_get_returns_payload_value_or_default() -> None:
    envelope = build_envelope("llm_request", {"mode": "conversation"}, device_id="m1")
    assert envelope.get("mode") == "conversation"
    assert envelope.get("missing", "fallback") == "fallback"
