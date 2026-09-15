"""Conversation context building and prompt independence (PRD 9, AC-05, AC-14)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import settings as settings_module
from src.config.settings import (
    DEFAULT_CONVERSATION_PROMPT,
    DEFAULT_SCREENSHOT_PROMPT,
    AppConfig,
    ConfigStore,
)
from src.llm.conversation import (
    CONTEXT_HEADER,
    NOTES_HEADER,
    RESUME_HEADER,
    TARGET_HEADER,
    ConversationTurn,
    TargetNotFound,
    build_conversation_messages,
    build_from_records,
    compose_system_prompt,
    render_context,
    select_context,
    speaker_label,
)
from src.llm.vision import build_vision_messages, describe_vision_request
from src.services.conversation_service import ConversationService
from src.services.llm_runner import LlmJob, SubmitResult
from src.services.screenshot_service import ScreenshotService
from src.storage.models import MessageRecord
from src.storage.sqlite import Database

# --------------------------------------------------------------------- helpers


def turns(count: int) -> list[ConversationTurn]:
    return [
        ConversationTurn(
            message_id=f"m-{i}",
            speaker="me" if i % 2 == 0 else "other",
            text=f"消息{i}",
            created_at=1000 + i,
        )
        for i in range(count)
    ]


class FakeRunner:
    """Records submitted jobs instead of calling a provider."""

    def __init__(self) -> None:
        self.jobs: list[LlmJob] = []

    async def submit(self, job: LlmJob) -> SubmitResult:
        self.jobs.append(job)
        return SubmitResult(True)

    async def cancel(self, request_id: str) -> bool:  # pragma: no cover - unused here
        return False


def all_contents(messages: list[dict]) -> str:
    """Flatten every message content (str or vision parts) into one string."""
    chunks: list[str] = []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            chunks.append(content)
        else:
            for part in content:
                chunks.append(str(part.get("text", "")))
    return "\n".join(chunks)


# ------------------------------------------------------------------ selection


def test_speaker_labels() -> None:
    assert speaker_label("me") == "我"
    assert speaker_label("other") == "对方"
    assert speaker_label("alien") == "alien"


def test_select_context_uses_last_n_before_target() -> None:
    history = turns(6)
    context, target = select_context(history, "m-4", limit=3)
    assert target.message_id == "m-4"
    assert [turn.message_id for turn in context] == ["m-1", "m-2", "m-3"]  # chronological
    assert target not in context


def test_select_context_clamps_to_available_history() -> None:
    history = turns(3)
    context, target = select_context(history, "m-2", limit=50)
    assert [turn.message_id for turn in context] == ["m-0", "m-1"]
    assert target.message_id == "m-2"


def test_select_context_first_message_has_empty_context() -> None:
    context, target = select_context(turns(3), "m-0", limit=5)
    assert context == []
    assert target.message_id == "m-0"


def test_select_context_unknown_target_raises() -> None:
    with pytest.raises(TargetNotFound) as info:
        select_context(turns(2), "m-404", limit=5)
    assert info.value.message_id == "m-404"


# ------------------------------------------------------------------- messages


def test_build_conversation_messages_structure() -> None:
    history = turns(4)
    context, target = select_context(history, "m-3", limit=2)
    messages = build_conversation_messages(
        system_prompt=DEFAULT_CONVERSATION_PROMPT, context=context, target=target
    )

    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == DEFAULT_CONVERSATION_PROMPT.strip()

    # context block: header + one "对方: ..."/"我: ..." line per turn
    context_block = messages[1]["content"]
    assert context_block.startswith(CONTEXT_HEADER)
    assert "对方: 消息1" in context_block
    assert "我: 消息2" in context_block
    assert render_context([]) == ""

    # target block: explicitly marked with TARGET_HEADER, label and instruction
    target_block = messages[-1]["content"]
    assert target_block.startswith(TARGET_HEADER)
    assert "（对方）" in target_block
    assert "消息3" in target_block
    assert TARGET_HEADER in target_block and "请针对" in target_block


def test_build_from_records_maps_storage_rows() -> None:
    history = [
        MessageRecord(id=f"m-{i}", speaker="me", source="mic", text=f"文本{i}", created_at=i)
        for i in range(3)
    ]
    messages, target, context = build_from_records(
        system_prompt="SYS", history=history, target_id="m-2", limit=10
    )
    assert target.message_id == "m-2"
    assert [turn.message_id for turn in context] == ["m-0", "m-1"]
    assert messages[0] == {"role": "system", "content": "SYS"}


# ------------------------------------------------- prompt independence (AC-14)


def test_conversation_and_screenshot_prompts_are_never_mixed() -> None:
    conversation_messages = build_conversation_messages(
        system_prompt=DEFAULT_CONVERSATION_PROMPT, context=turns(2), target=turns(3)[2]
    )
    conversation_text = all_contents(conversation_messages)
    assert DEFAULT_CONVERSATION_PROMPT.strip() in conversation_text
    assert DEFAULT_SCREENSHOT_PROMPT.strip() not in conversation_text

    vision_messages = build_vision_messages(
        system_prompt=DEFAULT_SCREENSHOT_PROMPT,
        image_data_url="data:image/jpeg;base64,QUJD",
    )
    vision_text = all_contents(vision_messages)
    assert DEFAULT_SCREENSHOT_PROMPT.strip() in vision_text
    assert DEFAULT_CONVERSATION_PROMPT.strip() not in vision_text


async def test_conversation_service_uses_conversation_prompt_only(
    database: Database, config_store: ConfigStore, env_config, transport
) -> None:
    config_store.update(
        {"prompts": {"conversation": "CONV-PROMPT-XY", "screenshot": "SHOT-PROMPT-XY"}},
        persist=False,
    )
    record = database.insert_message(text="目标句子", speaker="other", source="system")
    runner = FakeRunner()
    service = ConversationService(
        db=database, config=config_store, env=env_config, runner=runner, transport=transport
    )

    accepted = await service.start_conversation_request("req-1", record.id)
    assert accepted is True
    assert len(runner.jobs) == 1
    job = runner.jobs[0]
    assert job.mode == "conversation"
    assert job.provider.api_key == env_config.llm.api_key  # conversation provider
    text = all_contents(job.messages)
    assert "CONV-PROMPT-XY" in text
    assert "SHOT-PROMPT-XY" not in text
    assert "目标句子" in text


async def test_screenshot_service_uses_screenshot_prompt_only(
    database: Database, config_store: ConfigStore, env_config, transport
) -> None:
    config_store.update(
        {"prompts": {"conversation": "CONV-PROMPT-XY", "screenshot": "SHOT-PROMPT-XY"}},
        persist=False,
    )
    runner = FakeRunner()
    service = ScreenshotService(
        db=database, config=config_store, env=env_config, runner=runner, transport=transport
    )

    accepted = await service.analyze_image(
        screenshot_id="ss-1",
        request_id="req-9",
        image_data_url="data:image/jpeg;base64,QUJDREVG",
    )
    assert accepted is True
    job = runner.jobs[0]
    assert job.mode == "screenshot"
    assert job.provider.api_key == env_config.vision.api_key  # vision provider
    text = all_contents(job.messages)
    assert "SHOT-PROMPT-XY" in text
    assert "CONV-PROMPT-XY" not in text


async def test_conversation_service_reports_missing_message(
    database: Database, config_store: ConfigStore, env_config, transport
) -> None:
    service = ConversationService(
        db=database, config=config_store, env=env_config, runner=FakeRunner(), transport=transport
    )
    assert await service.start_conversation_request("req-2", None) is False
    assert await service.start_conversation_request("req-3", "m-404") is False
    errors = transport.payloads("llm_error")
    assert len(errors) == 2
    assert all(payload["request_id"] in ("req-2", "req-3") for payload in errors)


# --------------------------------------------------- interview notes injection
# NOTE: tests use throwaway files only -- never the real personal notes/resume.


def test_compose_system_prompt_orders_resume_then_notes_then_prompt() -> None:
    composed = compose_system_prompt(
        "PROMPT",
        resume_context="RESUME-BODY",
        resume_hint="USE-RESUME",
        interview_notes="NOTES-BODY",
        notes_hint="USE-NOTES",
    )
    assert composed == (
        f"{RESUME_HEADER}\nRESUME-BODY\n\nUSE-RESUME\n\n"
        f"{NOTES_HEADER}\nNOTES-BODY\n\nUSE-NOTES\n\nPROMPT"
    )


def test_compose_system_prompt_drops_hints_without_their_block() -> None:
    hints_without_blocks = compose_system_prompt(
        "PROMPT", resume_hint="USE-RESUME", notes_hint="USE-NOTES"
    )
    assert hints_without_blocks == "PROMPT"
    notes_only = compose_system_prompt("PROMPT", interview_notes="NOTES-BODY", notes_hint="USE-NOTES")
    assert notes_only == f"{NOTES_HEADER}\nNOTES-BODY\n\nUSE-NOTES\n\nPROMPT"


def test_message_builders_forward_interview_notes() -> None:
    conversation = build_conversation_messages(
        system_prompt="SYS",
        context=turns(2),
        target=turns(3)[2],
        interview_notes="NOTES-BODY",
        notes_hint="USE-NOTES",
    )
    system = conversation[0]["content"]
    assert f"{NOTES_HEADER}\nNOTES-BODY" in system and "USE-NOTES" in system
    assert system.endswith("SYS")

    vision = build_vision_messages(
        system_prompt=DEFAULT_SCREENSHOT_PROMPT,
        image_data_url="data:image/jpeg;base64,QUJD",
        interview_notes="NOTES-BODY",
        notes_hint="USE-NOTES",
    )
    vision_system = vision[0]["content"]
    assert f"{NOTES_HEADER}\nNOTES-BODY" in vision_system and "USE-NOTES" in vision_system


def test_build_from_records_forwards_interview_notes() -> None:
    history = [
        MessageRecord(id=f"m-{i}", speaker="me", source="mic", text=f"文本{i}", created_at=i)
        for i in range(2)
    ]
    messages, _, _ = build_from_records(
        system_prompt="SYS",
        history=history,
        target_id="m-1",
        limit=5,
        interview_notes="NOTES-BODY",
    )
    assert f"{NOTES_HEADER}\nNOTES-BODY" in messages[0]["content"]


def test_describe_vision_request_reports_notes_length_only() -> None:
    description = describe_vision_request(
        "data:image/jpeg;base64,QUJD",
        "model-x",
        context_messages=2,
        resume_chars=11,
        notes_chars=7,
    )
    assert description["interview_notes_chars"] == 7
    assert description["resume_context_chars"] == 11
    assert "NOTES-BODY" not in str(description)


def _write_notes(tmp_path: Path, text: str) -> str:
    notes_file = tmp_path / "notes.md"
    notes_file.write_text(text, encoding="utf-8")
    return str(notes_file)


def test_interview_notes_text_reads_absolute_path(tmp_path: Path) -> None:
    cfg = AppConfig(interview_notes={"path": _write_notes(tmp_path, "  NOTE-ONE\nNOTE-TWO  ")})
    assert cfg.interview_notes_text == "NOTE-ONE\nNOTE-TWO"


def test_interview_notes_text_missing_blank_or_disabled_is_empty(tmp_path: Path) -> None:
    missing = AppConfig(interview_notes={"path": str(tmp_path / "missing.md")})
    assert missing.interview_notes_text == ""
    blank = AppConfig(interview_notes={"path": _write_notes(tmp_path, "   \n")})
    assert blank.interview_notes_text == ""
    disabled = AppConfig(
        interview_notes={"enabled": False, "path": _write_notes(tmp_path, "NOTE-ONE")}
    )
    assert disabled.interview_notes_text == ""


def test_interview_notes_text_respects_max_chars(tmp_path: Path) -> None:
    path = _write_notes(tmp_path, "A" * 50 + "B" * 50)
    truncated = AppConfig(interview_notes={"path": path, "max_chars": 50}).interview_notes_text
    assert truncated.startswith("A" * 50)
    assert "B" not in truncated
    assert "截断" in truncated  # omission marker appended
    assert AppConfig(interview_notes={"path": path, "max_chars": 0}).interview_notes_text == (
        "A" * 50 + "B" * 50
    )
    assert AppConfig(interview_notes={"path": path, "max_chars": 500}).interview_notes_text == (
        "A" * 50 + "B" * 50
    )


def test_interview_notes_text_relative_path_resolves_under_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings_module, "REPO_ROOT", tmp_path)
    target = tmp_path / "docs" / "notes.md"
    target.parent.mkdir(parents=True)
    target.write_text("REL-OK", encoding="utf-8")
    cfg = AppConfig(interview_notes={"path": "docs/notes.md"})
    assert cfg.interview_notes_text == "REL-OK"


def test_interview_notes_never_reach_wire_settings(tmp_path: Path) -> None:
    cfg = AppConfig(interview_notes={"path": _write_notes(tmp_path, "NOTE-ONE")})
    payload = cfg.to_wire_settings()
    assert "interview_notes" not in payload
    assert "NOTE-ONE" not in str(payload)


async def test_services_inject_interview_notes_into_both_paths(
    tmp_path: Path, database: Database, config_store: ConfigStore, env_config, transport
) -> None:
    notes_path = _write_notes(tmp_path, "NOTES-BODY")
    config_store.update({"interview_notes": {"path": notes_path}}, persist=False)
    record = database.insert_message(text="目标句子", speaker="other", source="system")
    runner = FakeRunner()

    conversation = ConversationService(
        db=database, config=config_store, env=env_config, runner=runner, transport=transport
    )
    assert await conversation.start_conversation_request("req-notes", record.id) is True
    assert f"{NOTES_HEADER}\nNOTES-BODY" in all_contents(runner.jobs[0].messages)

    screenshot = ScreenshotService(
        db=database, config=config_store, env=env_config, runner=runner, transport=transport
    )
    assert (
        await screenshot.analyze_image(
            screenshot_id="ss-1",
            request_id="req-notes-2",
            image_data_url="data:image/jpeg;base64,QUJD",
        )
        is True
    )
    assert f"{NOTES_HEADER}\nNOTES-BODY" in all_contents(runner.jobs[1].messages)
