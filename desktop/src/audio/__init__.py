"""Audio capture layer (microphone + WASAPI loopback + resampling)."""

from __future__ import annotations

from src.audio.base import (
    AudioDeviceError,
    AudioStreamStalled,
    BaseCapture,
    DeviceSpec,
    FrameSource,
    resolve_device,
)
from src.audio.loopback import LoopbackCapture, list_loopback_devices
from src.audio.microphone import MicrophoneCapture, list_input_devices
from src.audio.resample import TARGET_SAMPLE_RATE, LinearResampler, resample_int16, rms_int16, to_int16, to_mono

__all__ = [
    "AudioDeviceError",
    "AudioStreamStalled",
    "BaseCapture",
    "DeviceSpec",
    "FrameSource",
    "resolve_device",
    "MicrophoneCapture",
    "list_input_devices",
    "LoopbackCapture",
    "list_loopback_devices",
    "LinearResampler",
    "TARGET_SAMPLE_RATE",
    "resample_int16",
    "rms_int16",
    "to_int16",
    "to_mono",
]
