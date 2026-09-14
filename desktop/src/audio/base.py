"""Shared audio capture plumbing.

Both capture back-ends (microphone via sounddevice, system loopback via
PyAudioWPatch) run their device callback on a native audio thread and hand
16 kHz mono int16 blocks to a bounded queue. Consumers (the ASR pipelines) call
:meth:`BaseCapture.read`, which blocks with a timeout -- so the asyncio event
loop never touches audio APIs directly.

PRD 6.1: the two streams are fully independent; a failure in one must not stop
the other. That isolation is provided by running each capture inside its own
supervised task (see :mod:`src.services.transcription_service` and ``main.py``).
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol

import numpy as np

from src.audio.resample import LinearResampler, to_int16, to_mono
from src.util.log import get_logger

__all__ = [
    "AudioDeviceError",
    "AudioStreamStalled",
    "FrameSource",
    "BaseCapture",
    "resolve_device",
    "DeviceSpec",
]

DeviceSpec = str | int | None


class AudioDeviceError(RuntimeError):
    """Device missing, busy, or the audio backend is not installed."""


class AudioStreamStalled(AudioDeviceError):
    """No audio arrived for too long (unplugged device / output switch)."""


class FrameSource(Protocol):
    """Blocking source of 16 kHz mono int16 blocks."""

    speaker: str
    source: str

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def read(self, timeout: float = 0.5) -> np.ndarray | None: ...

    @property
    def available(self) -> bool: ...

    @property
    def running(self) -> bool: ...

    def check_health(self) -> None: ...

    def stats(self) -> dict[str, Any]: ...


def resolve_device(
    spec: DeviceSpec,
    kind: str,
    devices: Iterable[Mapping[str, Any]],
    *,
    default_index: int | None = None,
) -> int:
    """Resolve a configured device (index, name substring or ``None``).

    *devices* is an iterable of mappings with at least ``index``/``name`` keys so
    the function stays unit-testable without audio hardware.
    """
    candidates = [dict(device) for device in devices]
    if spec is None:
        if default_index is not None:
            return int(default_index)
        for device in candidates:
            if int(device.get(f"max_{kind}_channels", 0) or 0) > 0 and device.get("default_samplerate"):
                return int(device["index"])
        raise AudioDeviceError(f"no {kind} device available")

    if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().lstrip("-").isdigit()):
        wanted = int(spec)
        for device in candidates:
            if int(device.get("index", -1)) == wanted:
                return wanted
        raise AudioDeviceError(f"{kind} device index {wanted} not found")

    needle = str(spec).strip().lower()
    if not needle:
        raise AudioDeviceError(f"empty {kind} device name")
    matches = [
        device
        for device in candidates
        if needle in str(device.get("name", "")).lower() and int(device.get(f"max_{kind}_channels", 0) or 0) > 0
    ]
    if not matches:
        # Fall back to a name match ignoring the channel count so users can still
        # select loopback endpoints that report 0 input channels.
        matches = [device for device in candidates if needle in str(device.get("name", "")).lower()]
    if not matches:
        raise AudioDeviceError(f"{kind} device matching {spec!r} not found")
    return int(matches[0]["index"])


class BaseCapture:
    """Queue-backed capture base class (subclass opens the actual device)."""

    #: Protocol speaker tag: ``me`` (microphone) or ``other`` (system audio).
    speaker: str = "me"
    #: Protocol source tag: ``mic`` or ``system``.
    source: str = "mic"

    def __init__(
        self,
        *,
        device: DeviceSpec = None,
        sample_rate: int = 16000,
        block_ms: int = 100,
        queue_size: int = 200,
        stale_after_sec: float | None = None,
        logger_name: str | None = None,
    ) -> None:
        self._device_spec = device
        self._sample_rate = int(sample_rate)
        self._block_ms = int(block_ms)
        self._stale_after_sec = stale_after_sec
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=max(4, int(queue_size)))
        self._resampler: LinearResampler | None = None
        self._device_rate: int | None = None
        self._channels = 1
        self._device_name: str = ""
        self._running = False
        self._stopped = threading.Event()
        self._lock = threading.RLock()
        self._last_block_at = time.monotonic()
        self._blocks = 0
        self._dropped = 0
        self._status_warnings = 0
        self._logger = get_logger(logger_name or f"audio.{self.source}")

    # -- FrameSource ----------------------------------------------------
    @property
    def available(self) -> bool:
        """True once the device stream has been opened successfully."""
        return self._running

    @property
    def running(self) -> bool:
        """Alias of :attr:`available` (part of :class:`FrameSource`)."""
        return self._running

    @property
    def device_name(self) -> str:
        """Human readable device name actually opened."""
        return self._device_name

    @property
    def device_rate(self) -> int | None:
        """Native sample rate of the opened device."""
        return self._device_rate

    def start(self) -> None:
        """Open the device stream. Raises :class:`AudioDeviceError` on failure."""
        with self._lock:
            if self._running:
                return
            self._stopped.clear()
            self._resampler = None
            self._open_stream()
            self._running = True
            self._last_block_at = time.monotonic()
            self._logger.info(
                "capturing %s: device=%r rate=%s channels=%d block=%dms",
                self.source,
                self._device_name,
                self._device_rate,
                self._channels,
                self._block_ms,
            )

    def stop(self) -> None:
        """Stop and close the device stream (idempotent, never raises)."""
        with self._lock:
            self._running = False
            self._stopped.set()
        try:
            self._close_stream()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            self._logger.warning("error while closing %s stream: %s", self.source, exc)
        # Wake up blocked consumers and drop buffered audio.
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._logger.info("stopped %s capture (%s)", self.source, self.stats())

    def read(self, timeout: float = 0.5) -> np.ndarray | None:
        """Blocking read of one 16 kHz mono int16 block (``None`` on timeout)."""
        if self._stopped.is_set():
            return None
        try:
            return self._queue.get(timeout=max(0.01, timeout))
        except queue.Empty:
            return None

    def check_health(self) -> None:
        """Raise :class:`AudioStreamStalled` when no audio arrived for too long."""
        if not self._running or self._stale_after_sec is None:
            return
        idle = time.monotonic() - self._last_block_at
        if idle > self._stale_after_sec:
            raise AudioStreamStalled(
                f"no audio from {self.source} device {self._device_name!r} for {idle:.1f}s"
            )

    def stats(self) -> dict[str, Any]:
        """Counters for logs/diagnostics (no audio data)."""
        return {
            "source": self.source,
            "device": self._device_name,
            "device_rate": self._device_rate,
            "running": self._running,
            "blocks": self._blocks,
            "dropped": self._dropped,
            "queued": self._queue.qsize(),
        }

    # -- subclass hooks -------------------------------------------------
    def _open_stream(self) -> None:
        raise NotImplementedError

    def _close_stream(self) -> None:
        raise NotImplementedError

    # -- shared plumbing ------------------------------------------------
    def _configure_device(self, *, name: str, rate: int, channels: int) -> None:
        """Record device properties and (re)create the resampler."""
        self._device_name = name
        self._device_rate = int(rate)
        self._channels = max(1, int(channels))
        self._resampler = LinearResampler(self._device_rate, self._sample_rate)

    def _frame_count(self) -> int:
        """Device frames per callback for the configured block length."""
        rate = self._device_rate or self._sample_rate
        return max(1, int(rate * self._block_ms / 1000))

    def _deliver(self, block: np.ndarray) -> None:
        """Convert a raw device block and enqueue it (called from audio thread)."""
        resampler = self._resampler
        if resampler is None or block.size == 0:
            return
        try:
            mono = to_int16(to_mono(block))
            resampled = resampler.process(mono)
        except Exception as exc:  # noqa: BLE001 - audio thread must never die
            self._logger.warning("resample failed on %s: %s", self.source, exc)
            return
        self._last_block_at = time.monotonic()
        self._blocks += 1
        if resampled.size == 0:
            return
        try:
            self._queue.put_nowait(resampled)
        except queue.Full:
            # Consumer is too slow: drop the oldest block, keep the newest audio.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(resampled)
            except queue.Empty:  # pragma: no cover - race with the consumer
                pass
            self._dropped += 1
            if self._dropped % 50 == 1:
                self._logger.warning(
                    "%s audio queue full, dropped %d block(s) so far", self.source, self._dropped
                )

    def _note_status(self, status: Any) -> None:
        """Throttled logging of PortAudio overflow/underflow flags."""
        if not status:
            return
        self._status_warnings += 1
        if self._status_warnings % 100 == 1:
            self._logger.warning("%s stream status: %s", self.source, status)


def describe_devices(devices: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Compact, loggable device list (``--list-audio-devices``)."""
    return [
        {
            "index": device.get("index"),
            "name": device.get("name"),
            "inputs": device.get("max_input_channels", 0),
            "outputs": device.get("max_output_channels", 0),
            "rate": device.get("default_samplerate"),
            "host": device.get("hostapi"),
        }
        for device in devices
    ]


def block_duration_ms(block: np.ndarray, sample_rate: int = 16000) -> float:
    """Duration of an int16 block in milliseconds."""
    if block.size == 0:
        return 0.0
    return block.size / float(sample_rate) * 1000.0


#: Type alias for the callback signature used by device back-ends.
AudioCallback = Callable[[np.ndarray], None]
