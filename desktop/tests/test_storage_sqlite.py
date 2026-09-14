"""SQLite persistence: inserts, protocol query shapes, clearing (PRD 18/19)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.storage.sqlite import Database
from src.util.files import delete_files

# --------------------------------------------------------------------- helpers


def insert_messages(db: Database, texts: list[str], **kwargs: object) -> list[str]:
    """Insert one message per text with strictly increasing created_at."""
    ids: list[str] = []
    for index, text in enumerate(texts):
        record = db.insert_message(
            text=text,
            speaker=kwargs.get("speaker", "me" if index % 2 == 0 else "other"),
            source=kwargs.get("source", "mic"),
            created_at=1_000 + index,
            started_at=900 + index,
            ended_at=950 + index,
        )
        ids.append(record.id)
    return ids


def make_screenshot(db: Database, tmp_path: Path, name: str, created_at: int) -> tuple[str, Path, Path]:
    """Insert a screenshot row with real (tiny) files on disk."""
    original = tmp_path / f"{name}.png"
    preview = tmp_path / f"{name}.preview.jpg"
    original.write_bytes(b"png-bytes-" + name.encode())
    preview.write_bytes(b"jpg-bytes-" + name.encode())
    record = db.insert_screenshot(
        local_path=original,
        preview_path=preview,
        preview_mime="image/jpeg",
        preview_bytes=preview.stat().st_size,
        width=1920,
        height=1080,
        trigger="hotkey",
        created_at=created_at,
    )
    return record.id, original, preview


# ------------------------------------------------------------------ messages


def test_insert_message_roundtrip(database: Database) -> None:
    record = database.insert_message(
        text="你好世界",
        speaker="other",
        source="system",
        utterance_id="u-1",
        started_at=1000,
        ended_at=2500,
        created_at=2500,
    )
    assert record.id.startswith("m-")
    assert record.session_id == database.session_id

    fetched = database.get_message(record.id)
    assert fetched is not None
    assert fetched.text == "你好世界"
    assert fetched.speaker == "other"
    assert fetched.source == "system"
    assert fetched.is_final is True
    assert fetched.duration == pytest.approx(1.5)

    by_utterance = database.find_message_by_utterance("u-1")
    assert by_utterance is not None and by_utterance.id == record.id


def test_insert_message_is_idempotent_on_utterance_id(database: Database) -> None:
    first = database.insert_message(text="初稿", speaker="me", source="mic", utterance_id="u-dup")
    second = database.insert_message(text="终稿", speaker="me", source="mic", utterance_id="u-dup")
    assert second.id == first.id
    assert second.text == "终稿"
    assert database.count_messages() == 1


def test_recent_messages_are_chronological_and_limited(database: Database) -> None:
    insert_messages(database, ["a", "b", "c", "d", "e"])
    recent = database.recent_messages(limit=3)
    assert [record.text for record in recent] == ["c", "d", "e"]  # oldest first
    assert len(database.recent_messages(limit=100)) == 5


def test_context_window_returns_last_n_before_target(database: Database) -> None:
    ids = insert_messages(database, ["m1", "m2", "m3", "target", "m5"])
    context, target = database.context_window(ids[3], limit=2)
    assert target is not None and target.text == "target"
    assert [record.text for record in context] == ["m2", "m3"]  # never the target itself


def test_context_window_unknown_target(database: Database) -> None:
    context, target = database.context_window("m-nope", limit=5)
    assert context == [] and target is None


def test_clear_messages_deletes_everything(database: Database) -> None:
    insert_messages(database, ["a", "b"])
    assert database.clear_messages() == 2
    assert database.count_messages() == 0
    assert database.recent_messages() == []


# ---------------------------------------------------------------- ai answers


def test_insert_ai_answer_and_lookup(database: Database) -> None:
    record = database.insert_ai_answer(
        request_id="req-1",
        mode="conversation",
        target_id="m-1",
        model="test-model",
        answer="回答内容",
        completion_tokens=42,
        estimated_tokens=40,
        elapsed_ms=1200,
        tokens_per_second=35.0,
        status="done",
        created_at=5000,
    )
    assert record.id.startswith("a-")
    fetched = database.get_ai_answer("req-1")
    assert fetched is not None
    assert fetched.answer == "回答内容"
    assert fetched.status == "done"
    assert fetched.completion_tokens == 42

    database.insert_ai_answer(
        request_id="req-2", mode="screenshot", answer="", status="error",
        error_message="boom", created_at=6000,
    )
    assert len(database.recent_ai_answers()) == 2
    assert database.clear_ai_answers("conversation") == 1
    assert len(database.recent_ai_answers()) == 1
    assert database.recent_ai_answers()[0].request_id == "req-2"


# --------------------------------------------------------------- screenshots


def test_screenshot_insert_latest_and_model_update(database: Database, tmp_path: Path) -> None:
    old_id, _, _ = make_screenshot(database, tmp_path, "old", created_at=1000)
    new_id, _, _ = make_screenshot(database, tmp_path, "new", created_at=2000)

    latest = database.latest_screenshot()
    assert latest is not None and latest.id == new_id
    assert [record.id for record in database.recent_screenshots()] == [new_id, old_id]

    database.set_screenshot_model(new_id, "vision-model")
    assert database.get_screenshot(new_id).model == "vision-model"


def test_delete_screenshot_returns_record_for_file_removal(database: Database, tmp_path: Path) -> None:
    shot_id, original, preview = make_screenshot(database, tmp_path, "shot", created_at=1000)
    record = database.delete_screenshot(shot_id)
    assert record is not None and record.id == shot_id
    assert database.get_screenshot(shot_id) is None
    assert database.delete_screenshot("missing") is None

    removed = delete_files(database.screenshot_files([record]))
    assert removed == 2
    assert not original.exists() and not preview.exists()


def test_clear_screenshots_deletes_rows_and_files(database: Database, tmp_path: Path) -> None:
    _, original_a, preview_a = make_screenshot(database, tmp_path, "a", created_at=1000)
    _, original_b, preview_b = make_screenshot(database, tmp_path, "b", created_at=2000)

    records = database.clear_screenshots()
    assert len(records) == 2
    assert database.count_screenshots() == 0

    removed = delete_files(database.screenshot_files(records))
    assert removed == 4
    for path in (original_a, preview_a, original_b, preview_b):
        assert not path.exists()


# ------------------------------------------------------------ history payload


def test_history_payload_matches_protocol_shape(database: Database, tmp_path: Path) -> None:
    insert_messages(database, ["第一句", "第二句"])
    database.insert_ai_answer(
        request_id="req-1", mode="conversation", target_id="m-x", answer="答",
        status="done", completion_tokens=10, elapsed_ms=500, tokens_per_second=20.0,
        created_at=3000,
    )
    make_screenshot(database, tmp_path, "older", created_at=1000)
    make_screenshot(database, tmp_path, "newest", created_at=2000)

    payload = database.history_payload(limit=50)
    assert set(payload) == {"messages", "screenshots", "ai_answers"}

    # messages: oldest first, schema keys present with correct types.
    assert [entry["text"] for entry in payload["messages"]] == ["第一句", "第二句"]
    message = payload["messages"][0]
    for key in ("message_id", "speaker", "text", "created_at", "duration_sec"):
        assert key in message
    assert isinstance(message["message_id"], str)
    assert message["speaker"] in ("me", "other")
    assert isinstance(message["created_at"], int)
    assert message["duration_sec"] is None or isinstance(message["duration_sec"], float)

    # screenshots: newest first; preview data URL ONLY on the latest entry.
    assert len(payload["screenshots"]) == 2
    newest, older = payload["screenshots"]
    assert newest["created_at"] > older["created_at"]
    assert isinstance(newest["preview"], str) and newest["preview"].startswith("data:image/jpeg;base64,")
    assert older["preview"] is None
    for entry in payload["screenshots"]:
        assert {"screenshot_id", "preview", "created_at"} <= set(entry)

    # ai_answers: schema keys, oldest first.
    answer = payload["ai_answers"][0]
    for key in ("request_id", "mode", "target_id", "answer", "status", "tokens_per_second", "created_at"):
        assert key in answer
    assert answer["status"] in ("done", "cancelled", "error")
    assert answer["mode"] in ("conversation", "screenshot")

    # The payload must be JSON serialisable exactly as sent over the wire.
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def test_session_lifecycle(database: Database) -> None:
    assert database.session_id is not None
    database.end_session(ended_at=123456)
    rows = database._query("SELECT ended_at FROM sessions WHERE id = ?", (database.session_id,))
    assert rows[0]["ended_at"] == 123456
    # close() is idempotent and flushes
    database.close()
    database.close()
