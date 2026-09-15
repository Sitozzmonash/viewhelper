"""ASR building blocks without funasr/torch (PRD 7, PRD 27 degradation)."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Sequence
from typing import Any

import numpy as np
import pytest

from src.asr import base as base_module
from src.asr import pipeline as pipeline_module
from src.asr.base import FunasrComponent, extract_text, funasr_importable
from src.asr.hotwords import MAX_HOTWORDS, HotwordSet, merge_hotwords, parse_hotwords
from src.asr.pipeline import AsrPipeline, FinalTranscript
from src.asr.shared_models import SharedAsrModels
from src.asr.vad import EnergyVad, VadEvent
from src.config.settings import AsrConfig, EnergyVadConfig

SAMPLE_RATE = 16000
BLOCK = SAMPLE_RATE // 10  # 100 ms


# --------------------------------------------------------------------- helpers


def block(amplitude: int, count: int = BLOCK) -> np.ndarray:
    """One 100 ms int16 block (constant amplitude => predictable RMS)."""
    return np.full(count, amplitude, dtype=np.int16)


class FakeFrames:
    """Minimal :class:`~src.audio.base.FrameSource` feeding canned blocks."""

    def __init__(self, blocks: Sequence[np.ndarray]) -> None:
        self._blocks = list(blocks)
        self.started = False
        self.stopped = False
        self.on_exhausted: Any = None

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def read(self, timeout: float = 0.5) -> np.ndarray | None:
        if self._blocks:
            return self._blocks.pop(0)
        if self.on_exhausted is not None:
            self.on_exhausted()
        return None

    @property
    def available(self) -> bool:
        return self.started

    @property
    def running(self) -> bool:
        return self.started and not self.stopped

    def check_health(self) -> None:
        return None

    def stats(self) -> dict[str, Any]:
        return {"blocks": len(self._blocks), "dropped": 0}


class FakeFsmnVad:
    """Stands in for the FunASR VAD and reports "unavailable"."""

    def __init__(self, model: str, **_kwargs: Any) -> None:
        self.model = model

    @property
    def available(self) -> bool:
        return False

    def load(self) -> bool:
        return False


class FakeStreaming:
    """Accumulates a deterministic partial transcript."""

    def __init__(self, model: str, **_kwargs: Any) -> None:
        self.model = model
        self.available = True
        self.text = ""
        self.blocks = 0

    def load(self) -> bool:
        return self.available

    def reset(self) -> None:
        self.text = ""
        self.blocks = 0

    def feed(self, pcm: np.ndarray) -> str:
        """Only voiced blocks advance the transcript (like a real recognizer)."""
        self.blocks += 1
        if pcm.size and float(np.abs(pcm.astype(np.float32)).max()) > 100.0:
            self.text += "字"
        return self.text

    def finalize(self) -> str:
        return self.text


class FakeFinalizer:
    """Records the hotwords it was called with."""

    seen_hotwords: list[str] = []

    def __init__(self, model: str, **_kwargs: Any) -> None:
        self.model = model
        self.available = True

    def load(self) -> bool:
        return self.available

    def transcribe(self, pcm: np.ndarray, hotwords: str = "") -> str:
        FakeFinalizer.seen_hotwords.append(hotwords)
        return f"离线识别{pcm.size // BLOCK}块"


class FakePunctuation:
    def __init__(self, model: str, **_kwargs: Any) -> None:
        self.available = True

    def load(self) -> bool:
        return self.available

    def restore(self, text: str) -> str:
        return f"{text}。"


@pytest.fixture
def patched_components(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every FunASR component with an in-process fake."""
    FakeFinalizer.seen_hotwords.clear()
    monkeypatch.setattr(pipeline_module, "FsmnVad", FakeFsmnVad)
    monkeypatch.setattr(pipeline_module, "StreamingRecognizer", FakeStreaming)
    monkeypatch.setattr(pipeline_module, "OfflineFinalizer", FakeFinalizer)
    monkeypatch.setattr(pipeline_module, "PunctuationRestorer", FakePunctuation)


def asr_config(**overrides: Any) -> AsrConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "hotwords": "张伟 李娜",
        "show_partial": True,
        "partial_interval_ms": 0,
        "max_utterance_sec": 20.0,
        "preroll_ms": 200,
        "energy_vad": EnergyVadConfig(threshold_rms=300, min_speech_ms=250, end_silence_ms=700),
    }
    defaults.update(overrides)
    return AsrConfig(**defaults)


# ---------------------------------------------------------------- extract_text


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ([{"text": "你好"}], "你好"),
        ([{"text": "你"}, {"text": "好"}], "你好"),
        ({"text": " 单条 "}, "单条"),
        (["纯字符串"], "纯字符串"),
        ([{"text": ""}], ""),
        ([{"key": "text"}], ""),
        ([], ""),
        (None, ""),
    ],
)
def test_extract_text_handles_funasr_result_shapes(results: Any, expected: str) -> None:
    assert extract_text(results) == expected


def test_funasr_importable_is_a_bool_and_does_not_raise() -> None:
    assert isinstance(funasr_importable(), bool)


def test_funasr_component_degrades_when_the_model_cannot_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD 27: an unloadable model disables that component only."""
    monkeypatch.setattr(base_module, "load_auto_model", lambda *_a, **_k: None)
    component = FunasrComponent("paraformer-zh")
    assert component.load() is False
    assert component.available is False
    assert component.model_name == "paraformer-zh"
    assert "paraformer-zh" in component.name
    # Inference on an unloaded model must return None instead of raising.
    assert component._generate(input=np.zeros(10, dtype=np.float32)) is None
    component.unload()
    assert component.available is False


def test_funasr_component_swallows_inference_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenModel:
        def generate(self, **_kwargs: Any) -> Any:
            raise RuntimeError("cuda out of memory")

    monkeypatch.setattr(base_module, "load_auto_model", lambda *_a, **_k: BrokenModel())
    component = FunasrComponent("paraformer-zh")
    assert component.load() is True
    assert component.load() is True  # idempotent
    assert component.available is True
    assert component._generate(input=np.zeros(4, dtype=np.float32)) is None


# -------------------------------------------------------------- shared models


def test_shared_registry_builds_once_and_keys_on_config() -> None:
    builds: list[str] = []

    def build_factory(tag: str) -> Any:
        def build() -> Any:
            builds.append(tag)
            return object()

        return build

    registry = SharedAsrModels()
    first = registry.acquire(
        name="m", device="cuda:0", disable_update=True, extra=None, build=build_factory("a")
    )
    again = registry.acquire(
        name="m", device="cuda:0", disable_update=True, extra=None, build=build_factory("b")
    )
    other_device = registry.acquire(
        name="m", device="cpu", disable_update=True, extra=None, build=build_factory("c")
    )

    assert first is again
    assert first.model is again.model
    assert other_device is not first
    assert builds == ["a", "c"]


def test_shared_registry_remembers_a_failed_build() -> None:
    calls = 0

    def build() -> None:
        nonlocal calls
        calls += 1
        return None

    registry = SharedAsrModels()
    for _ in range(3):
        entry = registry.acquire(
            name="missing", device="cpu", disable_update=True, extra=None, build=build
        )
        assert entry.model is None
    assert calls == 1


def test_shared_components_reuse_one_model_and_serialise_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two pipelines on one registry share the model; generate() never overlaps."""

    class TrackingModel:
        def __init__(self) -> None:
            self._state = threading.Lock()
            self.active = 0
            self.max_active = 0

        def generate(self, **_kwargs: Any) -> Any:
            with self._state:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.02)
            with self._state:
                self.active -= 1
            return [{"text": "ok"}]

    built: list[Any] = []

    def fake_load(*_args: Any, **_kwargs: Any) -> Any:
        model = TrackingModel()
        built.append(model)
        return model

    monkeypatch.setattr(base_module, "load_auto_model", fake_load)
    registry = SharedAsrModels()
    first = FunasrComponent("paraformer-zh", shared=registry)
    second = FunasrComponent("paraformer-zh", shared=registry)

    assert first.load() is True
    assert second.load() is True
    assert len(built) == 1
    assert first._model is second._model

    def worker(component: FunasrComponent) -> None:
        for _ in range(5):
            assert component._generate(input=np.zeros(4, dtype=np.float32)) == [{"text": "ok"}]

    threads = [threading.Thread(target=worker, args=(item,)) for item in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert built[0].max_active == 1


# -------------------------------------------------------------------- hotwords


def test_parse_hotwords_splits_on_every_separator_and_dedupes() -> None:
    assert parse_hotwords("张伟, 李娜；王芳、赵磊\n张伟  ok") == ["张伟", "李娜", "王芳", "赵磊", "ok"]


def test_parse_hotwords_ignores_empty_and_overlong_entries() -> None:
    assert parse_hotwords("") == []
    assert parse_hotwords(None) == []
    assert parse_hotwords("   ") == []
    assert parse_hotwords("x" * 65) == []
    assert parse_hotwords('"quoted" ‘single’') == ["quoted", "single"]


def test_parse_hotwords_is_capped() -> None:
    words = parse_hotwords(" ".join(f"w{i}" for i in range(MAX_HOTWORDS + 25)))
    assert len(words) == MAX_HOTWORDS


def test_hotword_set_update_reports_changes() -> None:
    hotwords = HotwordSet("张伟")
    assert hotwords.as_funasr_string() == "张伟"
    assert hotwords.update("张伟") is False
    assert hotwords.update("张伟 李娜") is True
    assert hotwords.words == ("张伟", "李娜")
    assert hotwords.update("") is True
    assert not hotwords
    assert "0 hotword" in hotwords.describe()


def test_merge_hotwords_keeps_order_and_uniqueness() -> None:
    merged = merge_hotwords(["a", "b"], ["b", "c"])
    assert merged.words == ("a", "b", "c")


# ------------------------------------------------------------------ EnergyVad


def test_energy_vad_opens_and_closes_an_utterance() -> None:
    vad = EnergyVad(
        sample_rate=SAMPLE_RATE, threshold_rms=300, min_speech_ms=250, end_silence_ms=700
    )
    events: list[VadEvent] = []
    for _ in range(2):
        events += vad.feed(block(0))
    assert events == []

    for _ in range(4):
        events += vad.feed(block(2000))
    assert [event.kind for event in events] == ["start"]
    assert vad.in_speech is True

    tail: list[VadEvent] = []
    for _ in range(8):
        tail += vad.feed(block(0))
    assert [event.kind for event in tail] == ["end"]
    assert vad.in_speech is False
    assert tail[0].position_ms > events[0].position_ms


def test_energy_vad_flush_forces_an_open_utterance_closed() -> None:
    vad = EnergyVad(sample_rate=SAMPLE_RATE, threshold_rms=300, min_speech_ms=100)
    vad.feed(block(2000))
    vad.feed(block(2000))
    assert vad.in_speech is True
    flushed = vad.flush()
    assert [event.kind for event in flushed] == ["end"]
    assert vad.in_speech is False
    assert vad.flush() == []


def test_energy_vad_max_utterance_closes_long_speech() -> None:
    vad = EnergyVad(
        sample_rate=SAMPLE_RATE,
        threshold_rms=300,
        min_speech_ms=100,
        end_silence_ms=5000,
        max_utterance_ms=500,
    )
    events: list[VadEvent] = []
    for _ in range(5):  # 5 x 100 ms of speech -> hits the 500 ms ceiling
        events += vad.feed(block(2000))
    assert [event.kind for event in events] == ["start", "end"]
    assert events[0].position_ms == 0
    assert events[1].position_ms == 500  # forced cut, not a silence boundary
    assert vad.in_speech is False


def test_energy_vad_reset_clears_state() -> None:
    vad = EnergyVad(sample_rate=SAMPLE_RATE, min_speech_ms=100)
    vad.feed(block(2000))
    vad.reset()
    assert vad.in_speech is False
    assert vad.feed(block(0)) == []


# ------------------------------------------------------------------- pipeline


async def test_pipeline_stays_idle_without_any_recognizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD 27: missing funasr must not crash, it just disables ASR."""

    class UnavailableStreaming(FakeStreaming):
        def __init__(self, model: str, **_kwargs: Any) -> None:
            super().__init__(model)
            self.available = False

    class UnavailableFinalizer(FakeFinalizer):
        def __init__(self, model: str, **_kwargs: Any) -> None:
            super().__init__(model)
            self.available = False

    monkeypatch.setattr(pipeline_module, "FsmnVad", FakeFsmnVad)
    monkeypatch.setattr(pipeline_module, "StreamingRecognizer", UnavailableStreaming)
    monkeypatch.setattr(pipeline_module, "OfflineFinalizer", UnavailableFinalizer)
    monkeypatch.setattr(pipeline_module, "PunctuationRestorer", FakePunctuation)

    frames = FakeFrames([block(2000), block(0)])
    finals: list[FinalTranscript] = []
    pipeline = AsrPipeline(
        speaker="me",
        source="mic",
        frames=frames,
        config=asr_config(),
        on_final=finals.append,
        sample_rate=SAMPLE_RATE,
    )

    task = asyncio.create_task(pipeline.run())
    await asyncio.sleep(0.05)  # let the worker reach its idle loop
    pipeline.request_stop()  # the idle loop never reads frames, so stop it directly
    await asyncio.wait_for(task, timeout=5.0)

    assert pipeline.models_available is False
    assert pipeline.vad_backend == "energy-vad"
    assert finals == []
    assert frames.started and frames.stopped


async def test_pipeline_emits_partials_then_one_final(patched_components: None) -> None:
    blocks = [block(0), block(0)] + [block(2000)] * 4 + [block(0)] * 8
    frames = FakeFrames(blocks)
    partials: list[tuple[str, str]] = []
    finals: list[FinalTranscript] = []

    pipeline = AsrPipeline(
        speaker="other",
        source="system",
        frames=frames,
        config=asr_config(),
        on_partial=lambda text, utterance_id: partials.append((text, utterance_id)),
        on_final=finals.append,
        sample_rate=SAMPLE_RATE,
    )
    frames.on_exhausted = pipeline.request_stop

    await asyncio.wait_for(pipeline.run(), timeout=5.0)

    assert pipeline.models_available is True
    assert pipeline.vad_backend == "energy-vad"
    assert partials, "expected streaming partials"
    assert len({utterance_id for _, utterance_id in partials}) == 1
    # The first two loud blocks are held in the preroll buffer (they only go to
    # the offline finalizer), so streaming sees the remaining voiced blocks.
    assert partials[-1][0] == "字" * 2

    assert len(finals) == 1
    final = finals[0]
    assert final.text.startswith("离线识别")
    assert final.text.endswith("。")  # punctuation restorer ran
    assert final.speaker == "other"
    assert final.source == "system"
    assert final.utterance_id.startswith("u-")
    assert final.used_offline_model is True
    assert final.started_at is not None and final.ended_at is not None
    assert final.started_at <= final.ended_at
    assert final.duration is not None and final.duration >= 0.0
    # Hotwords are applied at the final pass only.
    assert FakeFinalizer.seen_hotwords == ["张伟 李娜"]
    assert pipeline.stats()["finals"] == 1
    assert frames.stopped is True


async def test_pipeline_falls_back_to_the_partial_when_offline_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableFinalizer(FakeFinalizer):
        def __init__(self, model: str, **_kwargs: Any) -> None:
            super().__init__(model)
            self.available = False

    monkeypatch.setattr(pipeline_module, "FsmnVad", FakeFsmnVad)
    monkeypatch.setattr(pipeline_module, "StreamingRecognizer", FakeStreaming)
    monkeypatch.setattr(pipeline_module, "OfflineFinalizer", UnavailableFinalizer)
    monkeypatch.setattr(pipeline_module, "PunctuationRestorer", FakePunctuation)

    frames = FakeFrames([block(2000)] * 4 + [block(0)] * 8)
    finals: list[FinalTranscript] = []
    pipeline = AsrPipeline(
        speaker="me",
        source="mic",
        frames=frames,
        config=asr_config(),
        on_final=finals.append,
        sample_rate=SAMPLE_RATE,
    )
    frames.on_exhausted = pipeline.request_stop

    await asyncio.wait_for(pipeline.run(), timeout=5.0)

    assert len(finals) == 1
    assert finals[0].used_offline_model is False
    # The first two loud blocks land in the preroll buffer and never reach the
    # streaming recognizer, so only the remaining voiced blocks add characters.
    assert finals[0].partial_text == "字字"
    assert finals[0].text == "字字。"  # punctuation restorer still ran


async def test_pipeline_update_settings_is_thread_safe(patched_components: None) -> None:
    frames = FakeFrames([block(0)])
    pipeline = AsrPipeline(
        speaker="me", source="mic", frames=frames, config=asr_config(), sample_rate=SAMPLE_RATE
    )
    frames.on_exhausted = pipeline.request_stop

    pipeline.update_settings(show_partial=False, hotwords="新词 另一个", partial_interval_ms=50)

    task = asyncio.create_task(pipeline.run())
    await asyncio.wait_for(task, timeout=5.0)
    assert pipeline.stats()["utterances"] == 0
