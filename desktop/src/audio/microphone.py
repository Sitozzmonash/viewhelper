"""Microphone capture (sounddevice / PortAudio), PRD 6.1 Stream A.

``sounddevice`` is imported lazily so the module can be imported (and unit
tested) on machines without PortAudio.
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np

from src.audio.base import AudioDeviceError, BaseCapture, DeviceSpec, describe_devices, resolve_device
from src.util.log import get_logger

__all__ = ["MicrophoneCapture", "list_input_devices"]

_logger = get_logger(__name__)


def _import_sounddevice() -> Any:
    try:
        import sounddevice as sd  # noqa: PLC0415 - lazy by design
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise AudioDeviceError(
            "sounddevice is not installed; microphone capture unavailable (run `uv sync` in desktop/)"
        ) from exc
    except OSError as exc:  # PortAudio DLL missing
        raise AudioDeviceError(f"PortAudio could not be loaded: {exc}") from exc
    return sd


def list_input_devices() -> list[dict[str, Any]]:
    """Loggable list of input devices (used by ``--list-audio-devices``)."""
    try:
        sd = _import_sounddevice()
        devices = [
            {
                "index": index,
                "name": info.get("name"),
                "max_input_channels": info.get("max_input_channels", 0),
                "max_output_channels": info.get("max_output_channels", 0),
                "default_samplerate": info.get("default_samplerate"),
                "hostapi": info.get("hostapi"),
            }
            for index, info in enumerate(sd.query_devices())
        ]
    except AudioDeviceError as exc:
        _logger.warning("cannot list audio devices: %s", exc)
        return []
    return describe_devices(devices)


class MicrophoneCapture(BaseCapture):
    """``speaker="me"`` capture from the configured (or default) input device."""

    speaker = "me"
    source = "mic"

    def __init__(
        self,
        *,
        device: DeviceSpec = None,
        sample_rate: int = 16000,
        block_ms: int = 100,
        queue_size: int = 200,
        stale_after_sec: float | None = 15.0,
    ) -> None:
        super().__init__(
            device=device,
            sample_rate=sample_rate,
            block_ms=block_ms,
            queue_size=queue_size,
            stale_after_sec=stale_after_sec,
            logger_name="audio.mic",
        )
        self._stream: Any | None = None
        self._stream_lock = threading.Lock()

    # -- device ---------------------------------------------------------
    def _open_stream(self) -> None:
        sd = _import_sounddevice()
        try:
            devices = [
                {
                    "index": index,
                    "name": info.get("name"),
                    "max_input_channels": info.get("max_input_channels", 0),
                    "max_output_channels": info.get("max_output_channels", 0),
                    "default_samplerate": info.get("default_samplerate"),
                }
                for index, info in enumerate(sd.query_devices())
            ]
        except Exception as exc:  # noqa: BLE001 - any PortAudio error is a device error
            raise AudioDeviceError(f"cannot enumerate input devices: {exc}") from exc

        default_index: int | None = None
        try:
            default_info = sd.query_devices(kind="input")
            default_index = int(default_info["index"])
        except Exception:  # noqa: BLE001 - no default device configured
            default_index = None

        try:
            index = resolve_device(self._device_spec, "input", devices, default_index=default_index)
            info = sd.query_devices(index, "input")
            rate = int(info.get("default_samplerate") or 48000)
            channels = max(1, min(2, int(info.get("max_input_channels") or 1)))
        except AudioDeviceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AudioDeviceError(f"cannot open input device {self._device_spec!r}: {exc}") from exc

        if int(info.get("max_input_channels") or 0) <= 0:
            raise AudioDeviceError(f"device {info.get('name')!r} has no input channels")

        self._configure_device(name=str(info.get("name") or index), rate=rate, channels=channels)
        try:
            stream = sd.RawInputStream(
                samplerate=rate,
                blocksize=self._frame_count(),
                device=index,
                channels=channels,
                dtype="int16",
                latency="low",
                callback=self._on_audio,
            )
            stream.start()
        except Exception as exc:  # noqa: BLE001
            raise AudioDeviceError(f"cannot start microphone stream: {exc}") from exc
        with self._stream_lock:
            self._stream = stream

    def _close_stream(self) -> None:
        with self._stream_lock:
            stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception as exc:  # noqa: BLE001 - best effort
            self._logger.debug("mic stream stop failed: %s", exc)
        finally:
            try:
                stream.close()
            except Exception as exc:  # noqa: BLE001 - best effort
                self._logger.debug("mic stream close failed: %s", exc)

    # -- callback (PortAudio thread) ------------------------------------
    def _on_audio(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        self._note_status(status)
        try:
            block = np.frombuffer(indata, dtype=np.int16)
            if self._channels > 1:
                block = block.reshape(-1, self._channels)
            self._deliver(block)
        except Exception as exc:  # noqa: BLE001 - never raise into PortAudio
            self._logger.warning("microphone callback failed: %s", exc)
