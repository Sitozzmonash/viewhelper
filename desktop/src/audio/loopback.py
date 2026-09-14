"""Windows WASAPI loopback capture (PyAudioWPatch), PRD 6.1 Stream B.

Captures whatever the default (or configured) *output* device is playing --
the remote party in Teams/Zoom/WeChat calls -- and tags it as
``speaker="other"``.

Runs completely independently from :mod:`src.audio.microphone`: if the loopback
endpoint disappears (headset unplugged, output device switched) only this stream
fails and its supervisor restarts it.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

import numpy as np

from src.audio.base import AudioDeviceError, BaseCapture, DeviceSpec, resolve_device
from src.util.log import get_logger

__all__ = ["LoopbackCapture", "list_loopback_devices"]

_logger = get_logger(__name__)


def _import_pyaudiowpatch() -> Any:
    try:
        import pyaudiowpatch as pyaudio  # noqa: PLC0415 - lazy by design
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise AudioDeviceError(
            "PyAudioWPatch is not installed; system loopback capture unavailable "
            "(run `uv sync` in desktop/)"
        ) from exc
    except OSError as exc:  # pragma: no cover - PortAudio DLL missing
        raise AudioDeviceError(f"PortAudio could not be loaded: {exc}") from exc
    return pyaudio


def _normalise(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Map PyAudio device keys onto the generic keys :func:`resolve_device` uses."""
    return {
        "index": candidate.get("index"),
        "name": candidate.get("name"),
        "max_input_channels": candidate.get("maxInputChannels", 0),
        "default_samplerate": candidate.get("defaultSampleRate"),
    }


def list_loopback_devices() -> list[dict[str, Any]]:
    """Loggable list of WASAPI loopback endpoints (``--list-audio-devices``)."""
    try:
        pyaudio = _import_pyaudiowpatch()
        manager = pyaudio.PyAudio()
    except AudioDeviceError as exc:
        _logger.warning("cannot list loopback devices: %s", exc)
        return []
    try:
        candidates = [_normalise(info) for info in manager.get_loopback_device_info_generator()]
    except Exception as exc:  # noqa: BLE001 - backend without loopback support
        _logger.warning("loopback enumeration failed: %s", exc)
        return []
    finally:
        _terminate(manager)
    return candidates


def _terminate(manager: Any) -> None:
    try:
        manager.terminate()
    except Exception as exc:  # noqa: BLE001 - best effort
        _logger.debug("PyAudio terminate failed: %s", exc)


class LoopbackCapture(BaseCapture):
    """``speaker="other"`` capture of the system output device."""

    speaker = "other"
    source = "system"

    def __init__(
        self,
        *,
        device: DeviceSpec = None,
        sample_rate: int = 16000,
        block_ms: int = 100,
        queue_size: int = 200,
        stale_after_sec: float | None = None,
    ) -> None:
        # Loopback legitimately delivers nothing while the PC is silent, so the
        # staleness watchdog is disabled by default.
        super().__init__(
            device=device,
            sample_rate=sample_rate,
            block_ms=block_ms,
            queue_size=queue_size,
            stale_after_sec=stale_after_sec,
            logger_name="audio.loopback",
        )
        self._stream: Any | None = None
        self._manager: Any | None = None
        self._pyaudio: Any | None = None
        self._stream_lock = threading.Lock()

    # -- device ---------------------------------------------------------
    def _open_stream(self) -> None:
        pyaudio = _import_pyaudiowpatch()
        manager = pyaudio.PyAudio()
        try:
            info = self._select_loopback(pyaudio, manager)
            rate = int(info.get("defaultSampleRate") or 48000)
            channels = max(1, min(2, int(info.get("maxInputChannels") or 2)))
            name = str(info.get("name") or info.get("index"))
            self._pyaudio = pyaudio
            self._configure_device(name=name, rate=rate, channels=channels)
            stream = manager.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                frames_per_buffer=self._frame_count(),
                input=True,
                input_device_index=int(info["index"]),
                stream_callback=self._on_audio,
            )
        except AudioDeviceError:
            _terminate(manager)
            raise
        except Exception as exc:  # noqa: BLE001 - map every backend error
            _terminate(manager)
            raise AudioDeviceError(f"cannot open WASAPI loopback stream: {exc}") from exc
        with self._stream_lock:
            self._stream = stream
            self._manager = manager

    def _select_loopback(self, pyaudio: Any, manager: Any) -> Mapping[str, Any]:
        """Pick the loopback endpoint matching the configured device."""
        try:
            candidates = list(manager.get_loopback_device_info_generator())
        except Exception as exc:  # noqa: BLE001
            raise AudioDeviceError(f"WASAPI loopback enumeration failed: {exc}") from exc
        if not candidates:
            raise AudioDeviceError(
                "no WASAPI loopback device found (Windows only; check the default output device)"
            )

        if self._device_spec is not None:
            try:
                index = resolve_device(
                    self._device_spec, "input", [_normalise(candidate) for candidate in candidates]
                )
            except AudioDeviceError as exc:
                raise AudioDeviceError(f"loopback device {self._device_spec!r} unavailable: {exc}") from exc
            for candidate in candidates:
                if int(candidate.get("index", -1)) == index:
                    return candidate
            raise AudioDeviceError(f"loopback device index {index} disappeared")

        # Default: the loopback of the current default WASAPI *output* device.
        default_name = self._default_output_name(pyaudio, manager)
        if default_name:
            lowered = default_name.lower()
            for candidate in candidates:
                name = str(candidate.get("name") or "").lower()
                if name and (name in lowered or lowered in name):
                    return candidate
            _logger.warning(
                "no loopback endpoint matches default output %r, using %r",
                default_name,
                candidates[0].get("name"),
            )
        return candidates[0]

    def _default_output_name(self, pyaudio: Any, manager: Any) -> str:
        try:
            wasapi_info = manager.get_host_api_info_by_type(pyaudio.paWASAPI)
            output_info = manager.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
            return str(output_info.get("name") or "")
        except Exception as exc:  # noqa: BLE001 - fall back to the first endpoint
            _logger.debug("cannot resolve default WASAPI output device: %s", exc)
            return ""

    def _close_stream(self) -> None:
        with self._stream_lock:
            stream, self._stream = self._stream, None
            manager, self._manager = self._manager, None
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception as exc:  # noqa: BLE001 - best effort
                self._logger.debug("loopback stop_stream failed: %s", exc)
            try:
                stream.close()
            except Exception as exc:  # noqa: BLE001 - best effort
                self._logger.debug("loopback close failed: %s", exc)
        if manager is not None:
            _terminate(manager)

    # -- callback (PortAudio thread) ------------------------------------
    def _on_audio(self, in_data: bytes, frame_count: int, time_info: Any, status: Any) -> tuple[None, int]:
        self._note_status(status)
        try:
            block = np.frombuffer(in_data, dtype=np.int16)
            if self._channels > 1:
                block = block.reshape(-1, self._channels)
            self._deliver(block)
        except Exception as exc:  # noqa: BLE001 - never raise into PortAudio
            self._logger.warning("loopback callback failed: %s", exc)
        pa_continue = getattr(self._pyaudio, "paContinue", 0)
        return (None, int(pa_continue))
