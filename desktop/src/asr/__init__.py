"""Local ASR layer (FunASR): VAD, streaming partials, offline finals, punctuation."""

from __future__ import annotations

from src.asr.base import FunasrComponent, funasr_importable, load_auto_model
from src.asr.finalizer import OfflineFinalizer
from src.asr.hotwords import HotwordSet, parse_hotwords
from src.asr.pipeline import AsrPipeline, FinalCallback, FinalTranscript, PartialCallback, PipelineSettings
from src.asr.punctuation import PunctuationRestorer
from src.asr.shared_models import SharedAsrModels
from src.asr.streaming import StreamingRecognizer
from src.asr.vad import EnergyVad, FsmnVad, VadEvent

__all__ = [
    "AsrPipeline",
    "FinalTranscript",
    "FinalCallback",
    "PartialCallback",
    "PipelineSettings",
    "FsmnVad",
    "EnergyVad",
    "VadEvent",
    "StreamingRecognizer",
    "OfflineFinalizer",
    "PunctuationRestorer",
    "HotwordSet",
    "parse_hotwords",
    "FunasrComponent",
    "funasr_importable",
    "load_auto_model",
    "SharedAsrModels",
]
