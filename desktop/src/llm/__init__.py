"""LLM layer: provider client, prompt builders and token statistics."""

from __future__ import annotations

from src.llm.client import (
    LLMError,
    LLMHTTPError,
    LLMNotConfiguredError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    LLMTransportError,
    OpenAICompatClient,
    StreamEvent,
    Usage,
    parse_sse_line,
)
from src.llm.conversation import (
    ConversationTurn,
    TargetNotFound,
    build_conversation_messages,
    select_context,
)
from src.llm.token_stats import FinalStats, StatsSnapshot, TokenStatsTracker, estimate_tokens
from src.llm.vision import VisionPayloadError, build_vision_messages

__all__ = [
    "OpenAICompatClient",
    "StreamEvent",
    "Usage",
    "parse_sse_line",
    "LLMError",
    "LLMHTTPError",
    "LLMNotConfiguredError",
    "LLMRateLimitError",
    "LLMServerError",
    "LLMTimeoutError",
    "LLMTransportError",
    "ConversationTurn",
    "TargetNotFound",
    "build_conversation_messages",
    "select_context",
    "TokenStatsTracker",
    "StatsSnapshot",
    "FinalStats",
    "estimate_tokens",
    "build_vision_messages",
    "VisionPayloadError",
]
