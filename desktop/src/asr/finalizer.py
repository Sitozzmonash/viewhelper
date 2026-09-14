"""Offline 2-pass finalisation (PRD 7.2 / 7.3).

When an utterance ends, the buffered audio is re-recognised with the offline
Paraformer model -- which is more accurate than the streaming one -- and hotwords
are applied at this stage only (never per streaming chunk, to keep latency low
and avoid flickering text).
"""

from __future__ import annotations

import numpy as np

from src.asr.base import FunasrComponent, extract_text
from src.util.log import get_logger

__all__ = ["OfflineFinalizer"]

_logger = get_logger(__name__)


class OfflineFinalizer(FunasrComponent):
    """Offline Paraformer used for the final (2-pass) transcript."""

    component_name = "paraformer-offline"

    def __init__(
        self,
        model: str = "paraformer-zh",
        *,
        device: str = "cpu",
        disable_update: bool = True,
        sample_rate: int = 16000,
        batch_size_s: int = 300,
    ) -> None:
        super().__init__(
            model,
            device=device,
            disable_update=disable_update,
            sample_rate=sample_rate,
        )
        self._batch_size_s = batch_size_s

    def transcribe(self, pcm: np.ndarray, *, hotwords: str = "") -> str:
        """Re-recognise a complete utterance; returns ``""`` when unavailable."""
        if not self.available:
            return ""
        samples = np.asarray(pcm, dtype=np.int16)
        if samples.size == 0:
            return ""
        kwargs: dict[str, object] = {
            "input": samples.astype(np.float32) / 32768.0,
            "batch_size_s": self._batch_size_s,
        }
        if hotwords:
            kwargs["hotword"] = hotwords
        text = extract_text(self._generate(**kwargs))
        if text:
            return text
        _logger.debug("%s returned no text for %.2fs of audio", self.name, samples.size / self._sample_rate)
        return ""
