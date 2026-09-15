"""Streaming partial recognition (Paraformer-online, PRD 7.1).

One instance is used per utterance stream: :meth:`reset` clears the FunASR cache
at the start of every utterance, :meth:`feed` returns the newly recognised text
for the block that was just pushed, and :attr:`text` accumulates the partial
transcript shown live on the mobile client.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from src.asr.base import FunasrComponent, extract_text
from src.asr.shared_models import SharedAsrModels
from src.util.log import get_logger

__all__ = ["StreamingRecognizer"]

_logger = get_logger(__name__)

#: FunASR paraformer-online works in 60ms units: [left, center, right].
_UNIT_MS = 60


class StreamingRecognizer(FunasrComponent):
    """Paraformer-online streaming recognizer (600ms chunks by default)."""

    component_name = "paraformer-online"

    def __init__(
        self,
        model: str = "paraformer-zh-streaming",
        *,
        device: str = "cpu",
        disable_update: bool = True,
        sample_rate: int = 16000,
        chunk_ms: int = 600,
        shared: SharedAsrModels | None = None,
    ) -> None:
        super().__init__(
            model,
            device=device,
            disable_update=disable_update,
            sample_rate=sample_rate,
            shared=shared,
        )
        center_units = max(1, int(round(chunk_ms / _UNIT_MS)))
        # FunASR paraformer-online expects [left, center, right] in 60ms units.
        self._chunk_size: list[int] = [5, center_units, 5]
        self._chunk_samples = int(sample_rate * center_units * _UNIT_MS / 1000)
        self._cache: dict[str, Any] = {}
        self._pending = np.empty(0, dtype=np.int16)
        self._parts: list[str] = []

    # -- state ----------------------------------------------------------
    @property
    def text(self) -> str:
        """Accumulated partial transcript for the current utterance."""
        return "".join(self._parts)

    @property
    def chunk_samples(self) -> int:
        """Samples per model call (diagnostics/tests)."""
        return self._chunk_samples

    def reset(self) -> None:
        """Start a new utterance: clear cache, buffer and partial text."""
        self._cache = {}
        self._pending = np.empty(0, dtype=np.int16)
        self._parts = []

    # -- inference ------------------------------------------------------
    def feed(self, pcm: np.ndarray) -> str:
        """Push audio and return the text recognised by this call (may be empty)."""
        if not self.available:
            return ""
        samples = np.asarray(pcm, dtype=np.int16)
        if samples.size == 0:
            return ""
        self._pending = (
            np.concatenate([self._pending, samples]) if self._pending.size else samples.astype(np.int16)
        )
        produced: list[str] = []
        while self._pending.size >= self._chunk_samples:
            chunk = self._pending[: self._chunk_samples]
            self._pending = self._pending[self._chunk_samples :]
            text = self._infer(chunk, is_final=False)
            if text:
                self._parts.append(text)
                produced.append(text)
        return "".join(produced)

    def finalize(self) -> str:
        """Flush the remaining audio of the utterance (``is_final=True``)."""
        if not self.available or self._pending.size == 0:
            self._pending = np.empty(0, dtype=np.int16)
            return ""
        chunk, self._pending = self._pending, np.empty(0, dtype=np.int16)
        text = self._infer(chunk, is_final=True)
        if text:
            self._parts.append(text)
        return text

    def _infer(self, chunk: np.ndarray, *, is_final: bool) -> str:
        results = self._generate(
            input=chunk.astype(np.float32) / 32768.0,
            cache=self._cache,
            is_final=is_final,
            chunk_size=self._chunk_size,
        )
        return extract_text(results)
