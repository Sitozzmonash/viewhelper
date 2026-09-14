"""ASR -> wire bridge: partials, finals, persistence and live settings.

The ASR pipelines run on their own worker threads, so every callback lands
outside the event loop. This service marshals them back with
``loop.call_soon_threadsafe`` (see :class:`~src.services.base.ServiceBase`):

* ``on_partial`` -> ``asr_partial {speaker, utterance_id, text}``;
* ``on_final``   -> persist the utterance in SQLite (worker thread), then send
  ``asr_final`` with the persisted ``message_id`` (AC-01/AC-02).

Finals are serialised with an :class:`asyncio.Lock` so two sources can never
interleave their insert/emit pairs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from src.asr.hotwords import HotwordSet
from src.asr.pipeline import AsrPipeline, FinalTranscript
from src.audio.base import FrameSource
from src.config.settings import AppConfig, ConfigStore
from src.services.base import ServiceBase
from src.storage.models import AudioSource, Speaker
from src.storage.sqlite import Database
from src.transport.protocol import EventType
from src.util.clock import utc_now_ms
from src.util.log import get_logger

__all__ = ["TranscriptionService"]

_logger = get_logger("services.transcription")


class TranscriptionService(ServiceBase):
    """Owns the ASR pipelines and their protocol output."""

    def __init__(self, *, db: Database, config: ConfigStore, transport: Any = None) -> None:
        super().__init__(transport=transport, logger_name="services.transcription")
        self._db = db
        self._config = config
        self._pipelines: dict[str, AsrPipeline] = {}
        self._final_lock = asyncio.Lock()
        self._partials = 0
        self._finals = 0

    # -- pipelines ------------------------------------------------------
    def create_pipeline(
        self,
        frames: FrameSource,
        *,
        speaker: Speaker,
        source: AudioSource,
        config: AppConfig | None = None,
    ) -> AsrPipeline:
        """Build (and remember) the pipeline for one audio source."""
        cfg = config or self._config.config
        pipeline = AsrPipeline(
            speaker=speaker,
            source=source,
            frames=frames,
            config=cfg.asr,
            hotwords=HotwordSet(cfg.asr.hotwords),
            on_partial=lambda text, utterance_id: self.on_partial(
                speaker=speaker, text=text, utterance_id=utterance_id
            ),
            on_final=self.on_final,
            sample_rate=cfg.audio.sample_rate,
        )
        self._pipelines[source] = pipeline
        return pipeline

    @property
    def pipelines(self) -> dict[str, AsrPipeline]:
        """Pipelines keyed by audio source (``mic`` / ``system``)."""
        return dict(self._pipelines)

    def stop_pipelines(self) -> None:
        """Ask every pipeline worker to finish (thread safe)."""
        for pipeline in self._pipelines.values():
            pipeline.request_stop()

    def apply_config(self, cfg: AppConfig) -> None:
        """Push live settings (hotwords, ``show_partial``, ...) into the pipelines."""
        for pipeline in self._pipelines.values():
            pipeline.update_settings(
                show_partial=cfg.asr.show_partial,
                hotwords=cfg.asr.hotwords,
                partial_interval_ms=cfg.asr.partial_interval_ms,
                max_utterance_sec=cfg.asr.max_utterance_sec,
                preroll_ms=cfg.asr.preroll_ms,
            )
        _logger.debug("applied settings to %d pipeline(s)", len(self._pipelines))

    def subscribe_config(self, store: ConfigStore | None = None) -> Callable[[], None]:
        """React to ``settings_update`` by re-applying the config (returns unsubscribe)."""
        return (store or self._config).subscribe(self.apply_config)

    def stats(self) -> dict[str, Any]:
        """Counters for diagnostics (no transcript text)."""
        return {
            "partials": self._partials,
            "finals": self._finals,
            "pipelines": {source: pipeline.stats() for source, pipeline in self._pipelines.items()},
        }

    # -- ASR callbacks (worker threads) ---------------------------------
    def on_partial(self, *, speaker: str, text: str, utterance_id: str) -> None:
        """Emit ``asr_partial``; called from the ASR worker thread."""
        if not text:
            return
        self._partials += 1
        self.emit_threadsafe(
            EventType.ASR_PARTIAL,
            {"speaker": speaker, "utterance_id": utterance_id, "text": text},
        )

    def on_final(self, transcript: FinalTranscript) -> None:
        """Persist + emit a finished utterance; called from the ASR worker thread."""
        if not transcript.text.strip():
            return
        self.spawn_threadsafe(
            lambda: self._handle_final(transcript),
            name=f"asr-final-{transcript.utterance_id}",
        )

    async def _handle_final(self, transcript: FinalTranscript) -> None:
        async with self._final_lock:
            created_at = transcript.ended_at or utc_now_ms()
            try:
                record = await asyncio.to_thread(
                    self._db.insert_message,
                    text=transcript.text,
                    speaker=transcript.speaker,
                    source=transcript.source,
                    utterance_id=transcript.utterance_id,
                    started_at=transcript.started_at,
                    ended_at=transcript.ended_at,
                    created_at=created_at,
                )
            except Exception as exc:  # noqa: BLE001 - keep transcribing even if SQLite fails
                _logger.error("cannot persist utterance %s: %s", transcript.utterance_id, exc)
                return
            self._finals += 1
            await self.emit(
                EventType.ASR_FINAL,
                {
                    "speaker": record.speaker,
                    "utterance_id": record.utterance_id or transcript.utterance_id,
                    "message_id": record.id,
                    "text": record.text,
                    "started_at": record.started_at,
                    "ended_at": record.ended_at,
                    "duration_sec": record.duration,
                    "created_at": record.created_at,
                    "source": record.source,
                },
            )
            _logger.debug(
                "asr_final %s -> %s (%d chars)",
                transcript.utterance_id,
                record.id,
                len(record.text),
            )

    # -- shutdown -------------------------------------------------------
    async def shutdown(self) -> None:
        """Stop the pipelines and let in-flight finals finish."""
        self.stop_pipelines()
        pending = await self.wait_tasks(timeout=3.0)
        if pending:
            _logger.warning("%d transcription task(s) still pending at shutdown", pending)
