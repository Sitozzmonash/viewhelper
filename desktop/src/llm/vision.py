"""Vision (screenshot) prompt assembly (PRD 13, AC-06, AC-14).

The screenshot prompt is strictly separate from the conversation prompt: this
module only ever receives ``config.prompts.screenshot``.

The image travels as an RFC 2397 data URL inside an ``image_url`` content part,
which every OpenAI-compatible vision endpoint accepts. Data URLs are never
logged.
"""

from __future__ import annotations

from typing import Any

from src.llm.client import ChatMessage

__all__ = [
    "DEFAULT_VISION_INSTRUCTION",
    "VisionPayloadError",
    "build_vision_messages",
    "describe_vision_request",
    "is_image_data_url",
]

DEFAULT_VISION_INSTRUCTION = "这是当前屏幕的截图，请分析并直接回答。"


class VisionPayloadError(ValueError):
    """The screenshot data URL is missing or malformed."""


def is_image_data_url(value: Any) -> bool:
    """Whether *value* looks like a base64 image data URL."""
    return isinstance(value, str) and value.startswith("data:image/") and ";base64," in value


def build_vision_messages(
    *,
    system_prompt: str,
    image_data_url: str,
    instruction: str | None = None,
    detail: str | None = None,
) -> list[ChatMessage]:
    """Assemble the chat messages for a screenshot LLM request."""
    if not is_image_data_url(image_data_url):
        raise VisionPayloadError("screenshot preview is not a valid image data url")

    image_url: dict[str, Any] = {"url": image_data_url}
    if detail:
        image_url["detail"] = detail

    return [
        {"role": "system", "content": system_prompt.strip()},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": instruction or DEFAULT_VISION_INSTRUCTION},
                {"type": "image_url", "image_url": image_url},
            ],
        },
    ]


def describe_vision_request(image_data_url: str, model: str | None = None) -> dict[str, Any]:
    """Log-safe summary (payload size only, never the base64 body)."""
    return {
        "model": model or "?",
        "image_data_url_chars": len(image_data_url or ""),
    }
