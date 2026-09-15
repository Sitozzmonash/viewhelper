"""Voice activity detection: FSMN-VAD (FunASR) with an energy-based fallback.

``FsmnVad`` wraps ``funasr.AutoModel("fsmn-vad")`` in streaming mode. When
funasr/torch are not installed, :class:`EnergyVad` provides a dependency-free
RMS silence detector so the pipeline can still segment utterances (PRD 27:
a missing module degrades, it never crashes the program).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

import numpy as np

from src.asr.base import FunasrComponent
from src.asr.shared_models import SharedAsrModels
from src.audio.resample import rms_int16
from src.util.log import get_logger

__all__ = ["VadEvent", "VadLike", "FsmnVad", "EnergyVad"]

_logger = get_logger(__name__)

VadKind = Literal["start", "end"]


@dataclass(frozen=True, slots=True)
class VadEvent:
    """Utterance boundary at an absolute stream position (milliseconds)."""

    kind: VadKind
    position_ms: int


class VadLike(Protocol):
    """Common interface for the two VAD implementations."""

    @property
    def available(self) -> bool: ...

    @property
    def name(self) -> str: ...

    def load(self) -> bool: ...

    def reset(self) -> None: ...

    def feed(self, pcm: np.ndarray, *, position_ms: int | None = None) -> list[VadEvent]: ...

    def flush(self) -> list[VadEvent]: ...


class FsmnVad(FunasrComponent):
    """Streaming FSMN-VAD via ``funasr.AutoModel`` (lazy, degrades to unavailable)."""

    component_name = "fsmn-vad"

    def __init__(
        self,
        model: str = "fsmn-vad",
        *,
        device: str = "cpu",
        disable_update: bool = True,
        sample_rate: int = 16000,
        max_end_silence_ms: int = 800,
        shared: SharedAsrModels | None = None,
    ) -> None:
        super().__init__(
            model,
            device=device,
            disable_update=disable_update,
            sample_rate=sample_rate,
            shared=shared,
        )
        self._max_end_silence_ms = max(100, int(max_end_silence_ms))
        self._cache: dict[str, Any] = {}
        self._in_speech = False
        self._elapsed_ms = 0.0

    @property
    def in_speech(self) -> bool:
        """Whether FunASR currently reports an open utterance."""
        return self._in_speech

    def reset(self) -> None:
        """Reset per-stream state (new utterance / new session)."""
        self._cache = {}
        self._in_speech = False
        self._elapsed_ms = 0.0

    def feed(self, pcm: np.ndarray, *, position_ms: int | None = None) -> list[VadEvent]:
        """Feed one 16 kHz mono int16 block and return boundary events."""
        samples = np.asarray(pcm, dtype=np.int16)
        block_ms = samples.size / self._sample_rate * 1000.0
        end_ms = int(position_ms) if position_ms is not None else int(self._elapsed_ms + block_ms)
        if not self.available:
            self._elapsed_ms = float(end_ms)
            return []

        results = self._generate(
            input=samples.astype(np.float32) / 32768.0,
            cache=self._cache,
            is_final=False,
            chunk_size=max(1, int(block_ms)),
            max_end_silence_time=self._max_end_silence_ms,
        )
        self._elapsed_ms = float(end_ms)

        events: list[VadEvent] = []
        for begin, end in _iter_segments(results):
            if begin >= 0 and not self._in_speech:
                self._in_speech = True
                events.append(VadEvent("start", max(0, begin)))
            if end >= 0 and self._in_speech:
                self._in_speech = False
                events.append(VadEvent("end", max(0, end)))
        return events

    def flush(self) -> list[VadEvent]:
        """Close an open utterance (shutdown / forced finalize)."""
        if not self._in_speech:
            return []
        self._in_speech = False
        return [VadEvent("end", int(self._elapsed_ms))]


def _iter_segments(results: Any) -> list[tuple[int, int]]:
    """Normalise FunASR VAD output into ``(begin_ms, end_ms)`` pairs.

    FunASR reports ``-1`` as the end of a segment that is still open.
    """
    segments: list[tuple[int, int]] = []
    if not results:
        return segments
    items = results if isinstance(results, (list, tuple)) else [results]
    for item in items:
        value: Any = None
        if isinstance(item, dict):
            value = item.get("value")
        elif isinstance(item, (list, tuple)) and item and isinstance(item[0], dict):
            value = item[0].get("value")
        if value is None:
            continue
        entries = value if isinstance(value, (list, tuple)) else [value]
        for entry in entries:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            try:
                segments.append((int(entry[0]), int(entry[1])))
            except (TypeError, ValueError):
                continue
    return segments


class EnergyVad:
    """RMS based VAD used when FunASR is unavailable (pure numpy, no ML deps)."""

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        threshold_rms: float = 300.0,
        min_speech_ms: int = 250,
        end_silence_ms: int = 700,
        max_utterance_ms: int = 20_000,
    ) -> None:
        self._sample_rate = sample_rate
        self._threshold = max(0.0, threshold_rms)
        self._min_speech_ms = max(0, min_speech_ms)
        self._end_silence_ms = max(50, end_silence_ms)
        self._max_utterance_ms = max(500, max_utterance_ms)
        self.reset()

    @property
    def name(self) -> str:
        """Identifier used in logs."""
        return "energy-vad"

    @property
    def available(self) -> bool:
        """Always available (pure numpy)."""
        return True

    @property
    def in_speech(self) -> bool:
        """Whether an utterance is currently open."""
        return self._in_speech

    def load(self) -> bool:
        """No-op loader (kept for :class:`VadLike` compatibility)."""
        return True

    def reset(self) -> None:
        """Clear all utterance state."""
        self._in_speech = False
        self._voiced_ms = 0.0
        self._silence_ms = 0.0
        self._speech_ms = 0.0
        self._elapsed_ms = 0.0

    def feed(self, pcm: np.ndarray, *, position_ms: int | None = None) -> list[VadEvent]:
        """Feed one 16 kHz mono int16 block and return boundary events."""
        samples = np.asarray(pcm, dtype=np.int16)
        block_ms = samples.size / self._sample_rate * 1000.0
        if block_ms <= 0:
            return []
        end_ms = float(position_ms) if position_ms is not None else self._elapsed_ms + block_ms
        voiced = rms_int16(samples) >= self._threshold
        events: list[VadEvent] = []

        if not self._in_speech:
            if voiced:
                self._voiced_ms += block_ms
                if self._voiced_ms >= self._min_speech_ms:
                    self._in_speech = True
                    self._speech_ms = self._voiced_ms
                    self._silence_ms = 0.0
                    events.append(VadEvent("start", max(0, int(end_ms - self._voiced_ms))))
            else:
                self._voiced_ms = 0.0
        else:
            self._speech_ms += block_ms
            if voiced:
                self._silence_ms = 0.0
            else:
                self._silence_ms += block_ms
            if self._silence_ms >= self._end_silence_ms:
                events.append(VadEvent("end", max(0, int(end_ms - self._silence_ms))))
                self._close_utterance()
            elif self._speech_ms >= self._max_utterance_ms:
                events.append(VadEvent("end", int(end_ms)))
                self._close_utterance()

        self._elapsed_ms = end_ms
        return events

    def flush(self) -> list[VadEvent]:
        """Force-close an open utterance (used on shutdown/timeout)."""
        if not self._in_speech:
            return []
        event = VadEvent("end", int(self._elapsed_ms))
        self._close_utterance()
        return [event]

    def _close_utterance(self) -> None:
        self._in_speech = False
        self._voiced_ms = 0.0
        self._silence_ms = 0.0
        self._speech_ms = 0.0
