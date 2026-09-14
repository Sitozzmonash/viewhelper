"""Sample-rate / channel conversion to the 16 kHz mono int16 FunASR expects.

Two paths are provided:

* integer decimation (48 kHz -> 16 kHz, 44.1 kHz is *not* an integer ratio) with
  block averaging, which acts as a simple anti-alias filter;
* linear interpolation with phase carry-over for arbitrary ratios, so block
  boundaries do not introduce drift or clicks.

Only numpy is used -- no scipy/torchaudio dependency.
"""

from __future__ import annotations

from typing import Any

import numpy as np

__all__ = [
    "TARGET_SAMPLE_RATE",
    "to_mono",
    "to_int16",
    "resample_int16",
    "LinearResampler",
    "rms_int16",
]

TARGET_SAMPLE_RATE = 16000


def to_mono(samples: np.ndarray) -> np.ndarray:
    """Downmix ``(n, channels)`` or ``(n,)`` audio to a 1-D array."""
    array = np.asarray(samples)
    if array.ndim == 1:
        return array
    if array.ndim == 2:
        if array.shape[1] == 1:
            return array[:, 0]
        # Mean over channels; keep float precision until the final cast.
        return array.astype(np.float32).mean(axis=1).astype(array.dtype)
    return array.reshape(-1)


def to_int16(samples: np.ndarray) -> np.ndarray:
    """Convert float32/float64 (-1..1) or int32 audio to contiguous int16."""
    array = np.asarray(samples)
    if array.dtype == np.int16:
        return np.ascontiguousarray(array)
    if array.dtype in (np.float32, np.float64):
        clipped = np.clip(array, -1.0, 1.0)
        return (clipped * 32767.0).astype(np.int16)
    if array.dtype == np.int32:
        return (array >> 16).astype(np.int16)
    if array.dtype == np.uint8:  # pragma: no cover - rare device format
        return ((array.astype(np.int16) - 128) * 256).astype(np.int16)
    return array.astype(np.int16)


def rms_int16(samples: np.ndarray) -> float:
    """RMS amplitude of an int16 block (0..32767), used by the fallback VAD."""
    if samples.size == 0:
        return 0.0
    values = samples.astype(np.float32)
    return float(np.sqrt(np.mean(np.square(values))))


class LinearResampler:
    """Stateful converter from *src_rate* to *dst_rate* for int16 mono blocks."""

    def __init__(self, src_rate: int, dst_rate: int = TARGET_SAMPLE_RATE) -> None:
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self._ratio = self.src_rate / self.dst_rate
        self._integer_factor = self.src_rate // self.dst_rate if self.src_rate % self.dst_rate == 0 else 0
        self._carry: np.ndarray = np.empty(0, dtype=np.int16)
        self._phase = 0.0
        self._last_sample = 0.0
        self._input_samples = 0
        self._output_samples = 0

    @property
    def ratio(self) -> float:
        """Input samples per output sample."""
        return self._ratio

    @property
    def input_samples(self) -> int:
        """Total input samples consumed (diagnostics)."""
        return self._input_samples

    @property
    def output_samples(self) -> int:
        """Total output samples produced (diagnostics)."""
        return self._output_samples

    def reset(self) -> None:
        """Drop buffered state (e.g. after a device switch)."""
        self._carry = np.empty(0, dtype=np.int16)
        self._phase = 0.0
        self._last_sample = 0.0

    def process(self, block: np.ndarray) -> np.ndarray:
        """Convert one int16 mono block, returning int16 mono at ``dst_rate``."""
        samples = to_int16(to_mono(np.asarray(block)))
        if samples.size == 0:
            return np.empty(0, dtype=np.int16)
        self._input_samples += int(samples.size)

        if self.src_rate == self.dst_rate:
            out = samples
        elif self._integer_factor > 1:
            out = self._decimate(samples)
        else:
            out = self._interpolate(samples)

        self._output_samples += int(out.size)
        return np.ascontiguousarray(out, dtype=np.int16)

    # -- strategies -----------------------------------------------------
    def _decimate(self, samples: np.ndarray) -> np.ndarray:
        factor = self._integer_factor
        buffered = np.concatenate([self._carry, samples]) if self._carry.size else samples
        usable = (buffered.size // factor) * factor
        self._carry = buffered[usable:]
        if usable == 0:
            return np.empty(0, dtype=np.int16)
        grouped = buffered[:usable].reshape(-1, factor).astype(np.float32)
        return grouped.mean(axis=1).astype(np.int16)

    def _interpolate(self, samples: np.ndarray) -> np.ndarray:
        count_in = samples.size
        ratio = self._ratio
        phase = self._phase
        if phase > count_in - 1:
            self._phase = phase - count_in
            self._last_sample = float(samples[-1])
            return np.empty(0, dtype=np.int16)

        positions_count = int(np.floor((count_in - 1 - phase) / ratio)) + 1
        if positions_count <= 0:  # pragma: no cover - guarded by the check above
            self._phase = phase - count_in
            self._last_sample = float(samples[-1])
            return np.empty(0, dtype=np.int16)

        positions = phase + ratio * np.arange(positions_count, dtype=np.float64)
        # Shift by one so index -1 (the previous block's last sample) is usable.
        extended = np.concatenate((np.array([self._last_sample], dtype=np.float32), samples.astype(np.float32)))
        values = np.interp(positions + 1.0, np.arange(extended.size, dtype=np.float64), extended)

        self._phase = float(positions[-1]) + ratio - count_in
        self._last_sample = float(samples[-1])
        return np.rint(values).astype(np.int16)


def resample_int16(samples: Any, src_rate: int, dst_rate: int = TARGET_SAMPLE_RATE) -> np.ndarray:
    """One-shot resample helper (stateless flavour of :class:`LinearResampler`)."""
    return LinearResampler(src_rate, dst_rate).process(np.asarray(samples))
