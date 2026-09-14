"""Streaming token statistics (PRD 12, AC-08).

No external tokenizer is used: Chinese text is estimated at ~1.6 characters per
token and other scripts at ~4 characters per token. When the provider reports
``usage.completion_tokens`` the final numbers are corrected with the real value.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "estimate_tokens",
    "StatsSnapshot",
    "FinalStats",
    "TokenStatsTracker",
    "DEFAULT_EMIT_INTERVAL_SEC",
]

#: ``llm_stats`` cadence required by the protocol ("about every 500ms").
DEFAULT_EMIT_INTERVAL_SEC = 0.5

_CJK_CHARS_PER_TOKEN = 1.6
_LATIN_CHARS_PER_TOKEN = 4.0


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF  # hiragana / katakana
        or 0x3400 <= code <= 0x4DBF  # CJK ext A
        or 0x4E00 <= code <= 0x9FFF  # CJK unified
        or 0xF900 <= code <= 0xFAFF  # CJK compatibility
        or 0xFF00 <= code <= 0xFFEF  # halfwidth / fullwidth forms
        or 0x20000 <= code <= 0x2FA1F  # CJK ext B+
    )


def estimate_tokens(text: str) -> int:
    """Heuristic token count for *text* (never negative)."""
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    other = len(text) - cjk
    estimate = cjk / _CJK_CHARS_PER_TOKEN + other / _LATIN_CHARS_PER_TOKEN
    return int(round(estimate))


@dataclass(frozen=True, slots=True)
class StatsSnapshot:
    """Intermediate statistics emitted as ``llm_stats``."""

    estimated_tokens: int
    elapsed_ms: int
    tokens_per_second: float


@dataclass(frozen=True, slots=True)
class FinalStats:
    """Terminal statistics emitted as ``llm_done``."""

    completion_tokens: int
    estimated_tokens: int
    elapsed_ms: int
    tokens_per_second: float
    from_provider_usage: bool


class TokenStatsTracker:
    """Accumulates streamed text and derives token/s figures.

    The clock is injectable so timing behaviour can be tested deterministically.
    """

    def __init__(
        self,
        *,
        interval_sec: float = DEFAULT_EMIT_INTERVAL_SEC,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._interval = max(0.05, interval_sec)
        self._clock = clock or time.monotonic
        self._started_at: float | None = None
        self._last_emit: float | None = None
        self._text_parts: list[str] = []
        self._estimated_tokens = 0
        self._completion_tokens: int | None = None

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        """Mark the beginning of generation (idempotent)."""
        if self._started_at is None:
            self._started_at = self._clock()
            self._last_emit = self._started_at

    @property
    def started(self) -> bool:
        """Whether :meth:`start` was called."""
        return self._started_at is not None

    @property
    def estimated_tokens(self) -> int:
        """Locally estimated completion tokens so far."""
        return self._estimated_tokens

    @property
    def text(self) -> str:
        """Streamed text accumulated so far."""
        return "".join(self._text_parts)

    @property
    def completion_tokens(self) -> int | None:
        """Real completion tokens when the provider reported usage."""
        return self._completion_tokens

    # -- accumulation ---------------------------------------------------
    def add(self, delta: str) -> int:
        """Record a streamed delta; returns the new estimated token count."""
        if not delta:
            return self._estimated_tokens
        self.start()
        self._text_parts.append(delta)
        self._estimated_tokens += estimate_tokens(delta)
        return self._estimated_tokens

    def set_usage(self, completion_tokens: int | None) -> None:
        """Store provider-reported usage (takes precedence over estimates)."""
        if completion_tokens is not None and completion_tokens >= 0:
            self._completion_tokens = int(completion_tokens)

    # -- timing ---------------------------------------------------------
    def elapsed_ms(self) -> int:
        """Milliseconds since :meth:`start` (0 when not started)."""
        if self._started_at is None:
            return 0
        return max(0, int((self._clock() - self._started_at) * 1000))

    def should_emit(self) -> bool:
        """True at most once per ``interval_sec`` (500ms by default)."""
        self.start()
        now = self._clock()
        assert self._last_emit is not None  # noqa: S101 - guaranteed by start()
        if now - self._last_emit + 1e-9 >= self._interval:
            self._last_emit = now
            return True
        return False

    def snapshot(self) -> StatsSnapshot:
        """Current estimates for an ``llm_stats`` payload."""
        elapsed = self.elapsed_ms()
        tokens = self._completion_tokens if self._completion_tokens is not None else self._estimated_tokens
        return StatsSnapshot(
            estimated_tokens=tokens,
            elapsed_ms=elapsed,
            tokens_per_second=_tokens_per_second(tokens, elapsed),
        )

    def finalize(self, completion_tokens: int | None = None) -> FinalStats:
        """Final numbers; provider usage wins over the local estimate."""
        self.set_usage(completion_tokens)
        elapsed = self.elapsed_ms()
        from_provider = self._completion_tokens is not None
        tokens = self._completion_tokens if from_provider else self._estimated_tokens
        return FinalStats(
            completion_tokens=int(tokens),
            estimated_tokens=self._estimated_tokens,
            elapsed_ms=elapsed,
            tokens_per_second=_tokens_per_second(tokens, elapsed),
            from_provider_usage=bool(from_provider),
        )


def _tokens_per_second(tokens: int, elapsed_ms: int) -> float:
    if elapsed_ms <= 0 or tokens <= 0:
        return 0.0
    return round(tokens / (elapsed_ms / 1000.0), 2)
