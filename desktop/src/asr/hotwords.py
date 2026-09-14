"""Hotword handling (PRD 7.3 / 8.3).

Hotwords are configured as a single string in ``config.yaml`` and handed to
FunASR as a space separated ``hotword=`` argument. Parsing is tolerant: newline,
comma, semicolon and whitespace all separate entries.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = ["HotwordSet", "parse_hotwords", "merge_hotwords", "MAX_HOTWORDS"]

#: FunASR slows down with very long hotword lists; cap it defensively.
MAX_HOTWORDS = 60

_SPLIT_RE = re.compile(r"[\s,;，；、\n\r\t]+")


def parse_hotwords(raw: str | None) -> list[str]:
    """Split a raw hotword string into a de-duplicated, order-preserving list."""
    if not raw:
        return []
    seen: dict[str, None] = {}
    for token in _SPLIT_RE.split(raw.strip()):
        word = token.strip().strip("\"'“”‘’")
        if not word or len(word) > 64:
            continue
        seen.setdefault(word, None)
    return list(seen.keys())[:MAX_HOTWORDS]


class HotwordSet:
    """Mutable hotword collection shared by the ASR pipelines."""

    def __init__(self, raw: str | None = "") -> None:
        self._raw = raw or ""
        self._words: tuple[str, ...] = tuple(parse_hotwords(self._raw))

    @property
    def raw(self) -> str:
        """Raw configured string."""
        return self._raw

    @property
    def words(self) -> tuple[str, ...]:
        """Parsed hotwords."""
        return self._words

    def __len__(self) -> int:
        return len(self._words)

    def __bool__(self) -> bool:
        return bool(self._words)

    def as_funasr_string(self) -> str:
        """Space separated string for FunASR ``hotword=`` (empty when unused)."""
        return " ".join(self._words)

    def update(self, raw: str | None) -> bool:
        """Replace the hotwords; returns True when something actually changed."""
        normalized = raw or ""
        words = tuple(parse_hotwords(normalized))
        if words == self._words and normalized == self._raw:
            return False
        self._raw = normalized
        self._words = words
        return True

    def describe(self) -> str:
        """Log-safe summary."""
        preview = ", ".join(self._words[:5])
        suffix = ", …" if len(self._words) > 5 else ""
        return f"{len(self._words)} hotword(s)" + (f": {preview}{suffix}" if self._words else "")


def merge_hotwords(*sources: Iterable[str]) -> HotwordSet:
    """Merge several hotword lists into one set (config + future per-session)."""
    combined: list[str] = []
    for source in sources:
        for word in source:
            if word and word not in combined:
                combined.append(word)
    return HotwordSet(" ".join(combined))
