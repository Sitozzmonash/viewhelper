"""Conversation prompt/context assembly (PRD 9, AC-05, AC-14).

Structure sent to the provider::

    system: conversation prompt (never the screenshot prompt)
    user:   【对话上下文】 last N FINAL messages, rendered as "对方: ..." / "我: ..."
    user:   【目标消息】  the bubble the user tapped, explicitly marked
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from src.llm.client import ChatMessage
from src.storage.models import SPEAKER_LABELS, MessageRecord

__all__ = [
    "ConversationTurn",
    "TargetNotFound",
    "speaker_label",
    "select_context",
    "render_context",
    "compose_system_prompt",
    "build_conversation_messages",
    "build_from_records",
    "CONTEXT_HEADER",
    "TARGET_HEADER",
    "RESUME_HEADER",
]

CONTEXT_HEADER = "【对话上下文】"
TARGET_HEADER = "【目标消息】"
TARGET_INSTRUCTION = f"请针对{TARGET_HEADER}给出回答。"
RESUME_HEADER = "【resume_context】"


def compose_system_prompt(
    system_prompt: str, resume_context: str = "", resume_hint: str = ""
) -> str:
    """System message: resume_context block, then the resume hint, then the prompt."""
    prompt = system_prompt.strip()
    resume = (resume_context or "").strip()
    hint = (resume_hint or "").strip()
    blocks: list[str] = []
    if resume:
        blocks.append(f"{RESUME_HEADER}\n{resume}")
    if hint:
        blocks.append(hint)
    blocks.append(prompt)
    return "\n\n".join(block for block in blocks if block)


class TargetNotFound(LookupError):
    """The requested ``message_id`` is not in the conversation history."""

    def __init__(self, message_id: str) -> None:
        super().__init__(f"message not found: {message_id}")
        self.message_id = message_id


def speaker_label(speaker: str) -> str:
    """``me`` -> ``我``, ``other`` -> ``对方`` (unknown speakers pass through)."""
    return SPEAKER_LABELS.get(speaker, speaker or "?")


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One final transcript line used to build LLM context."""

    message_id: str
    speaker: str
    text: str
    created_at: int = 0

    @classmethod
    def from_record(cls, record: MessageRecord) -> ConversationTurn:
        """Build a turn from a storage record."""
        return cls(
            message_id=record.id,
            speaker=record.speaker,
            text=record.text,
            created_at=record.created_at,
        )

    @property
    def label(self) -> str:
        """Chinese speaker label used in the prompt."""
        return speaker_label(self.speaker)

    def render(self) -> str:
        """``对方: ...`` line."""
        return f"{self.label}: {self.text.strip()}"


def select_context(
    history: Sequence[ConversationTurn], target_id: str, limit: int
) -> tuple[list[ConversationTurn], ConversationTurn]:
    """Split *history* into ``(context, target)``.

    * *context*: up to *limit* turns preceding the target, chronological order;
    * *target*: the explicitly requested turn (never part of *context*).

    Raises :class:`TargetNotFound` when *target_id* is absent.
    """
    target: ConversationTurn | None = None
    index = -1
    for position, turn in enumerate(history):
        if turn.message_id == target_id:
            target = turn
            index = position
            break
    if target is None:
        raise TargetNotFound(target_id)
    window = max(0, int(limit))
    return list(history[max(0, index - window) : index]), target


def render_context(turns: Sequence[ConversationTurn]) -> str:
    """Render context turns into the ``【对话上下文】`` block."""
    if not turns:
        return ""
    lines = [f"{CONTEXT_HEADER}（最近 {len(turns)} 条，仅供理解语境）"]
    lines.extend(turn.render() for turn in turns)
    return "\n".join(lines)


def build_conversation_messages(
    *,
    system_prompt: str,
    context: Sequence[ConversationTurn],
    target: ConversationTurn,
    resume_context: str = "",
    resume_hint: str = "",
) -> list[ChatMessage]:
    """Assemble the chat messages for a conversation LLM request."""
    messages: list[ChatMessage] = [
        {
            "role": "system",
            "content": compose_system_prompt(system_prompt, resume_context, resume_hint),
        }
    ]
    context_block = render_context(context)
    if context_block:
        messages.append({"role": "user", "content": context_block})
    messages.append(
        {
            "role": "user",
            "content": (
                f"{TARGET_HEADER}（{target.label}）\n"
                f"{target.text.strip()}\n\n"
                f"{TARGET_INSTRUCTION}"
            ),
        }
    )
    return messages


def build_from_records(
    *,
    system_prompt: str,
    history: Sequence[MessageRecord],
    target_id: str,
    limit: int,
    resume_context: str = "",
    resume_hint: str = "",
) -> tuple[list[ChatMessage], ConversationTurn, list[ConversationTurn]]:
    """Convenience wrapper over storage records.

    Returns ``(messages, target, context)``; raises :class:`TargetNotFound`.
    """
    turns = [ConversationTurn.from_record(record) for record in history]
    context, target = select_context(turns, target_id, limit)
    messages = build_conversation_messages(
        system_prompt=system_prompt,
        context=context,
        target=target,
        resume_context=resume_context,
        resume_hint=resume_hint,
    )
    return messages, target, context


def describe_request(target: ConversationTurn, context: Sequence[ConversationTurn]) -> dict[str, Any]:
    """Log-safe summary of a request (no full prompt, no secrets)."""
    return {
        "target_id": target.message_id,
        "speaker": target.speaker,
        "context_messages": len(context),
        "target_chars": len(target.text),
    }
