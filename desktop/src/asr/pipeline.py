"""Per-source ASR pipeline: VAD -> streaming partials -> offline final + punctuation.

One :class:`AsrPipeline` instance runs per audio source:

* microphone  -> ``speaker="me"``,    ``source="mic"``
* WASAPI loopback -> ``speaker="other"``, ``source="system"``

The whole recognition loop runs on a worker thread (via :func:`asyncio.to_thread`)
so no model inference ever blocks the event loop. Callbacks are invoked **from
that thread**; the transcription service marshals them back onto the loop with
``loop.call_soon_threadsafe``.

Graceful degradation (PRD 27):

* funasr/torch missing -> the pipeline logs a warning and stays idle;
* FSMN-VAD missing -> RMS :class:`~src.asr.vad.EnergyVad` fallback;
* offline model missing -> the accumulated streaming partial becomes the final;
* ct-punc missing -> finals are emitted without punctuation.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from src.asr.finalizer import OfflineFinalizer
from src.asr.hotwords import HotwordSet
from src.asr.punctuation import PunctuationRestorer
from src.asr.shared_models import SharedAsrModels
from src.asr.streaming import StreamingRecognizer
from src.asr.vad import EnergyVad, FsmnVad, VadEvent, VadLike
from src.audio.base import AudioDeviceError, FrameSource
from src.config.settings import AsrConfig
from src.util.clock import duration_sec, utc_now_ms
from src.util.log import get_logger

__all__ = ["AsrPipeline", "FinalTranscript", "PartialCallback", "FinalCallback", "PipelineSettings"]

#: ``on_partial(text, utterance_id)`` -- called from the ASR worker thread.
PartialCallback = Callable[[str, str], None]
#: ``on_final(transcript)`` -- called from the ASR worker thread.
FinalCallback = Callable[["FinalTranscript"], None]


@dataclass(frozen=True, slots=True)
class FinalTranscript:
    """A completed utterance handed to :data:`FinalCallback`."""

    text: str
    utterance_id: str
    speaker: str
    source: str
    started_at: int | None = None
    ended_at: int | None = None
    partial_text: str = ""
    used_offline_model: bool = False

    @property
    def duration(self) -> float | None:
        """Utterance duration in seconds (``duration_sec`` on the wire)."""
        return duration_sec(self.started_at, self.ended_at)


@dataclass(slots=True)
class PipelineSettings:
    """Live-tunable pipeline settings (updated from ``settings_update``)."""

    show_partial: bool = True
    hotwords: str = ""
    partial_interval_ms: int = 300
    max_utterance_sec: float = 20.0
    preroll_ms: int = 320


@dataclass(slots=True)
class _Utterance:
    """Mutable state of the utterance currently being recognised."""

    utterance_id: str
    started_at: int
    start_position_ms: float
    last_position_ms: float
    last_partial_at: float
    buffer: list[np.ndarray] = field(default_factory=list)
    samples: int = 0


@dataclass(slots=True)
class _Components:
    """Loaded model components for one pipeline."""

    vad: VadLike
    streaming: StreamingRecognizer
    finalizer: OfflineFinalizer
    punctuation: PunctuationRestorer
    vad_backend: str

    @property
    def can_recognise(self) -> bool:
        """True when at least one recognizer is usable."""
        return self.streaming.available or self.finalizer.available


class AsrPipeline:
    """Streaming ASR for a single audio source."""

    def __init__(
        self,
        *,
        speaker: str,
        source: str,
        frames: FrameSource,
        config: AsrConfig,
        hotwords: HotwordSet | None = None,
        on_partial: PartialCallback | None = None,
        on_final: FinalCallback | None = None,
        sample_rate: int = 16000,
        logger_name: str | None = None,
        shared_models: SharedAsrModels | None = None,
    ) -> None:
        self.speaker = speaker
        self.source = source
        self._frames = frames
        self._asr_config = config
        self._sample_rate = sample_rate
        self._shared_models = shared_models
        self._hotwords = hotwords if hotwords is not None else HotwordSet(config.hotwords)
        self._on_partial = on_partial
        self._on_final = on_final
        self._logger = get_logger(logger_name or f"asr.{source}")
        self._settings = PipelineSettings(
            show_partial=config.show_partial,
            hotwords=self._hotwords.as_funasr_string(),
            partial_interval_ms=config.partial_interval_ms,
            max_utterance_sec=config.max_utterance_sec,
            preroll_ms=config.preroll_ms,
        )
        self._settings_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._preroll: deque[np.ndarray] = deque()
        self._preroll_ms = 0.0
        self._utterance: _Utterance | None = None
        self._stream_position_ms = 0.0
        self._utterance_count = 0
        self._final_count = 0
        self._components: _Components | None = None

    # -- introspection --------------------------------------------------
    @property
    def name(self) -> str:
        """Identifier used in logs."""
        return f"asr[{self.source}/{self.speaker}]"

    @property
    def models_available(self) -> bool:
        """True when at least one recognizer could be loaded."""
        return bool(self._components and self._components.can_recognise)

    @property
    def vad_backend(self) -> str:
        """``fsmn-vad`` when FunASR is installed, otherwise ``energy-vad``."""
        return self._components.vad_backend if self._components else "unloaded"

    def stats(self) -> dict[str, Any]:
        """Counters for logs/diagnostics."""
        return {
            "source": self.source,
            "speaker": self.speaker,
            "utterances": self._utterance_count,
            "finals": self._final_count,
            "vad": self.vad_backend,
            "models": self.models_available,
            "frames": self._frames.stats(),
        }

    # -- settings -------------------------------------------------------
    def update_settings(
        self,
        *,
        show_partial: bool | None = None,
        hotwords: str | None = None,
        partial_interval_ms: int | None = None,
        max_utterance_sec: float | None = None,
        preroll_ms: int | None = None,
    ) -> None:
        """Apply live settings changes (thread safe)."""
        with self._settings_lock:
            if show_partial is not None:
                self._settings.show_partial = bool(show_partial)
            if hotwords is not None:
                if self._hotwords.update(hotwords):
                    self._logger.info("hotwords updated: %s", self._hotwords.describe())
                self._settings.hotwords = self._hotwords.as_funasr_string()
            if partial_interval_ms is not None:
                self._settings.partial_interval_ms = max(0, int(partial_interval_ms))
            if max_utterance_sec is not None:
                self._settings.max_utterance_sec = max(1.0, float(max_utterance_sec))
            if preroll_ms is not None:
                self._settings.preroll_ms = max(0, int(preroll_ms))

    def _settings_snapshot(self) -> PipelineSettings:
        with self._settings_lock:
            return replace(self._settings)

    def _block_ms(self, block: np.ndarray) -> float:
        """Duration of one 16 kHz block in milliseconds."""
        return block.size / self._sample_rate * 1000.0

    # -- lifecycle ------------------------------------------------------
    def request_stop(self) -> None:
        """Ask the worker loop to finish (thread safe)."""
        self._stop_event.set()

    async def run(self) -> None:
        """Start the capture device and run recognition until stopped.

        Intended to be wrapped by a :class:`~src.util.supervisor.Supervisor`: any
        exception (device unplugged, model crash) restarts this coroutine only.
        """
        self._stop_event.clear()
        self._reset_stream_state()
        await asyncio.to_thread(self._frames.start)
        try:
            await asyncio.to_thread(self._worker_loop)
        finally:
            await asyncio.to_thread(self._frames.stop)

    def unload(self) -> None:
        """Release the loaded components and stream state (idempotent).

        Called from the event loop thread *after* the worker thread of
        :meth:`run` has exited -- i.e. on the supervisor restart path or at
        shutdown -- so it never races with ``_worker_loop``. Frames are owned by
        the factory / ``run()``'s ``finally`` block and are not stopped here.
        """
        components = self._components
        self._components = None
        if components is not None:
            # EnergyVad is pure numpy and has no unload(); only FunASR-backed
            # components hold model references worth dropping.
            for component in (
                components.vad,
                components.streaming,
                components.finalizer,
                components.punctuation,
            ):
                unload = getattr(component, "unload", None)
                if callable(unload):
                    unload()
        self._reset_stream_state()

    def _reset_stream_state(self) -> None:
        self._utterance = None
        self._preroll.clear()
        self._preroll_ms = 0.0
        self._stream_position_ms = 0.0

    def _worker_loop(self) -> None:
        components = self._load_components()
        if not components.can_recognise:
            self._logger.warning(
                "no ASR recognizer available (install the 'asr' extra: funasr + torch); %s stays idle",
                self.name,
            )
            while not self._stop_event.is_set():
                self._stop_event.wait(1.0)
            return

        self._logger.info(
            "%s ready (vad=%s streaming=%s offline=%s punc=%s hotwords=%s)",
            self.name,
            components.vad_backend,
            components.streaming.available,
            components.finalizer.available,
            components.punctuation.available,
            self._hotwords.describe(),
        )

        while not self._stop_event.is_set():
            self._frames.check_health()  # raises -> supervisor restarts this pipeline
            block = self._frames.read(timeout=0.25)
            if block is None or block.size == 0:
                continue
            try:
                self._process_block(block)
            except AudioDeviceError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad block must not kill ASR
                self._logger.exception("%s block processing failed: %s", self.name, exc)

        # Shutdown: finalise whatever is still open so no sentence is lost.
        self._flush_open_utterance()

    def _load_components(self) -> _Components:
        models = self._asr_config.models
        vad: VadLike = FsmnVad(
            models.vad,
            device=models.device,
            disable_update=models.disable_update,
            max_end_silence_ms=models.max_end_silence_ms,
            shared=self._shared_models,
        )
        backend = "fsmn-vad"
        if not vad.load():
            energy = self._asr_config.energy_vad
            vad = EnergyVad(
                sample_rate=self._sample_rate,
                threshold_rms=energy.threshold_rms,
                min_speech_ms=energy.min_speech_ms,
                end_silence_ms=energy.end_silence_ms,
                max_utterance_ms=int(self._asr_config.max_utterance_sec * 1000),
            )
            vad.load()
            backend = "energy-vad"
            self._logger.info("FSMN-VAD unavailable, using RMS energy VAD fallback")

        streaming = StreamingRecognizer(
            models.streaming,
            device=models.device,
            disable_update=models.disable_update,
            sample_rate=self._sample_rate,
            shared=self._shared_models,
        )
        streaming.load()
        finalizer = OfflineFinalizer(
            models.offline,
            device=models.device,
            disable_update=models.disable_update,
            sample_rate=self._sample_rate,
            shared=self._shared_models,
        )
        finalizer.load()
        punctuation = PunctuationRestorer(
            models.punctuation,
            device=models.device,
            disable_update=models.disable_update,
            shared=self._shared_models,
        )
        punctuation.load()

        components = _Components(
            vad=vad,
            streaming=streaming,
            finalizer=finalizer,
            punctuation=punctuation,
            vad_backend=backend,
        )
        self._components = components
        return components

    # -- recognition ----------------------------------------------------
    def _process_block(self, block: np.ndarray) -> None:
        components = self._components
        if components is None:  # pragma: no cover - guarded by the worker loop
            return
        block_ms = self._block_ms(block)
        self._stream_position_ms += block_ms
        events: list[VadEvent] = components.vad.feed(block)
        consumed = False

        for event in events:
            if event.kind == "start":
                if self._utterance is None:
                    self._begin_utterance(event.position_ms)
            elif self._utterance is not None:
                if not consumed:
                    self._accumulate(block)
                    consumed = True
                self._finish_utterance(event.position_ms)

        if self._utterance is not None:
            if not consumed:
                self._accumulate(block)
            self._maybe_force_finalize()
        elif not consumed:
            self._push_preroll(block)

    def _push_preroll(self, block: np.ndarray) -> None:
        self._preroll.append(block)
        self._preroll_ms += self._block_ms(block)
        limit_ms = max(0.0, self._settings_snapshot().preroll_ms)
        while self._preroll and self._preroll_ms > limit_ms:
            dropped = self._preroll.popleft()
            self._preroll_ms = max(0.0, self._preroll_ms - self._block_ms(dropped))

    def _begin_utterance(self, position_ms: float) -> None:
        settings = self._settings_snapshot()
        self._utterance_count += 1
        start_position_ms = max(0.0, position_ms - settings.preroll_ms)
        preroll_span_ms = min(position_ms, settings.preroll_ms)
        self._utterance = _Utterance(
            utterance_id=f"u-{uuid.uuid4().hex[:12]}",
            started_at=utc_now_ms() - int(preroll_span_ms),
            start_position_ms=start_position_ms,
            last_position_ms=position_ms,
            last_partial_at=0.0,
        )
        if self._components and self._components.streaming.available:
            self._components.streaming.reset()
        if self._preroll:
            self._utterance.buffer.extend(self._preroll)
            self._utterance.samples += sum(int(chunk.size) for chunk in self._preroll)
            self._preroll.clear()
            self._preroll_ms = 0.0
        self._logger.debug("%s utterance %s started", self.name, self._utterance.utterance_id)

    def _accumulate(self, block: np.ndarray) -> None:
        utterance = self._utterance
        components = self._components
        if utterance is None or components is None:
            return
        utterance.buffer.append(block)
        utterance.samples += int(block.size)
        utterance.last_position_ms = self._stream_position_ms

        if not components.streaming.available:
            return
        components.streaming.feed(block)
        settings = self._settings_snapshot()
        if not settings.show_partial or self._on_partial is None:
            return
        text = components.streaming.text
        if not text:
            return
        now = time.monotonic()
        interval = settings.partial_interval_ms / 1000.0
        if utterance.last_partial_at and now - utterance.last_partial_at < interval:
            return
        utterance.last_partial_at = now
        self._safe_partial(text, utterance.utterance_id)

    def _maybe_force_finalize(self) -> None:
        utterance = self._utterance
        if utterance is None:
            return
        settings = self._settings_snapshot()
        elapsed_ms = self._stream_position_ms - utterance.start_position_ms
        if elapsed_ms >= settings.max_utterance_sec * 1000.0:
            self._logger.debug(
                "%s forcing finalize after %.1fs", self.name, elapsed_ms / 1000.0
            )
            self._finish_utterance(self._stream_position_ms)

    def _finish_utterance(self, position_ms: float) -> None:
        utterance = self._utterance
        components = self._components
        if utterance is None or components is None:
            return
        self._utterance = None

        partial_text = ""
        if components.streaming.available:
            components.streaming.finalize()
            partial_text = components.streaming.text

        pcm = (
            np.concatenate(utterance.buffer)
            if utterance.buffer
            else np.empty(0, dtype=np.int16)
        )
        settings = self._settings_snapshot()
        final_text = ""
        used_offline = False
        if components.finalizer.available and pcm.size:
            final_text = components.finalizer.transcribe(pcm, hotwords=settings.hotwords)
            used_offline = bool(final_text)
        if not final_text:
            final_text = partial_text
        final_text = final_text.strip()
        if not final_text:
            self._logger.debug("%s utterance %s produced no text", self.name, utterance.utterance_id)
            return
        if components.punctuation.available:
            final_text = components.punctuation.restore(final_text) or final_text

        ended_at = utc_now_ms()
        self._final_count += 1
        transcript = FinalTranscript(
            text=final_text,
            utterance_id=utterance.utterance_id,
            speaker=self.speaker,
            source=self.source,
            started_at=utterance.started_at,
            ended_at=ended_at,
            partial_text=partial_text,
            used_offline_model=used_offline,
        )
        self._logger.info(
            "%s final [%s] %s (%.2fs, offline=%s)",
            self.name,
            transcript.speaker,
            final_text if len(final_text) <= 80 else final_text[:80] + "…",
            transcript.duration or 0.0,
            used_offline,
        )
        if self._on_final is not None:
            try:
                self._on_final(transcript)
            except Exception as exc:  # noqa: BLE001 - consumer bugs must not kill ASR
                self._logger.exception("%s on_final callback failed: %s", self.name, exc)

    def _flush_open_utterance(self) -> None:
        components = self._components
        if components is None:
            return
        try:
            events = components.vad.flush()
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("vad flush failed: %s", exc)
            events = []
        if self._utterance is not None and not any(event.kind == "end" for event in events):
            events = [VadEvent("end", int(self._stream_position_ms))]
        for event in events:
            if event.kind == "end":
                self._finish_utterance(event.position_ms)

    def _safe_partial(self, text: str, utterance_id: str) -> None:
        if self._on_partial is None:
            return
        try:
            self._on_partial(text, utterance_id)
        except Exception as exc:  # noqa: BLE001 - consumer bugs must not kill ASR
            self._logger.exception("%s on_partial callback failed: %s", self.name, exc)
