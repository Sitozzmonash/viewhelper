"""Shift+Ctrl+Enter answers the LATEST turn with recent-N context (Task D).

``ConversationService.answer_latest`` is the loop-side body the global hotkey
triggers: it targets the newest final message (regardless of speaker) and carries
``conversation.hotkey_context_messages`` preceding turns as context -- the same
job the mobile ask dot produces, just self-initiated on the PC.
"""

from __future__ import annotations

from typing import Any

from src.config.env import EnvConfig
from src.config.settings import ConfigStore
from src.services.conversation_service import ConversationService
from src.services.llm_runner import LlmJob, SubmitResult
from src.storage.sqlite import Database


class StubRunner:
    """Records submitted jobs and always accepts (no real LLM call)."""

    def __init__(self) -> None:
        self.jobs: list[LlmJob] = []

    async def submit(self, job: LlmJob) -> SubmitResult:
        self.jobs.append(job)
        return SubmitResult(True)

    async def cancel(self, request_id: str) -> bool:  # pragma: no cover - unused here
        return False

    async def shutdown(self) -> None:  # pragma: no cover - unused here
        return None


def _seed(db: Database, count: int) -> list[str]:
    ids: list[str] = []
    for index in range(count):
        speaker = "me" if index % 2 == 0 else "other"
        record = db.insert_message(
            text=f"turn {index}",
            speaker=speaker,  # type: ignore[arg-type]
            source="mic" if speaker == "me" else "system",  # type: ignore[arg-type]
            created_at=1_700_000_000_000 + index * 1000,
        )
        ids.append(record.id)
    return ids


async def test_answer_latest_targets_newest_message(
    database: Database, config_store: ConfigStore, env_config: EnvConfig, transport: Any
) -> None:
    ids = _seed(database, 5)
    runner = StubRunner()
    service = ConversationService(
        db=database, config=config_store, env=env_config, runner=runner, transport=transport
    )
    service.bind_loop()

    assert await service.answer_latest() is True
    assert len(runner.jobs) == 1
    job = runner.jobs[0]
    assert job.mode == "conversation"
    assert job.target_id == ids[-1]
    assert job.request_id.startswith("req-hotkey-")
    # system + context + target messages were assembled.
    roles = [message["role"] for message in job.messages]
    assert roles[0] == "system"
    assert "user" in roles


async def test_hotkey_context_window_is_configurable(
    database: Database, config_store: ConfigStore, env_config: EnvConfig, transport: Any
) -> None:
    _seed(database, 8)
    config_store.update({"conversation": {"hotkey_context_messages": 3}}, persist=False)
    runner = StubRunner()
    service = ConversationService(
        db=database, config=config_store, env=env_config, runner=runner, transport=transport
    )
    service.bind_loop()

    await service.answer_latest()
    job = runner.jobs[0]
    # The context block is one user message; it must mention only the 3 turns
    # preceding the target (turn 4, 5, 6), not the older ones.
    context_block = next(
        m["content"] for m in job.messages if m["role"] == "user" and "对话上下文" in m["content"]
    )
    assert "最近 3 条" in context_block
    assert "turn 6" in context_block
    assert "turn 3" not in context_block


async def test_answer_latest_noop_without_messages(
    database: Database, config_store: ConfigStore, env_config: EnvConfig, transport: Any
) -> None:
    runner = StubRunner()
    service = ConversationService(
        db=database, config=config_store, env=env_config, runner=runner, transport=transport
    )
    service.bind_loop()

    assert await service.answer_latest() is False
    assert runner.jobs == []
