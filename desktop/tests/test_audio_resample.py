"""Audio conversion to 16 kHz mono int16 (PRD 6.2)."""

from __future__ import annotations

import numpy as np

from src.audio.resample import (
    TARGET_SAMPLE_RATE,
    LinearResampler,
    resample_int16,
    rms_int16,
    to_int16,
    to_mono,
)


def test_to_mono_downmixes_stereo() -> None:
    stereo = np.array([[100, 300], [200, 400]], dtype=np.int16)
    mono = to_mono(stereo)
    assert mono.shape == (2,)
    assert list(mono) == [200, 300]


def test_to_mono_keeps_single_channel_arrays() -> None:
    column = np.array([[1], [2], [3]], dtype=np.int16)
    assert list(to_mono(column)) == [1, 2, 3]
    flat = np.array([1, 2, 3], dtype=np.int16)
    assert to_mono(flat) is flat


def test_to_int16_converts_float_and_int32() -> None:
    floats = np.array([-1.0, -0.5, 0.0, 0.5, 1.0, 2.0], dtype=np.float32)
    converted = to_int16(floats)
    assert converted.dtype == np.int16
    assert converted[0] == -32767
    assert converted[-1] == 32767  # clipped to 1.0
    assert converted[2] == 0

    wide = np.array([65536, -131072], dtype=np.int32)
    assert list(to_int16(wide)) == [1, -2]


def test_to_int16_is_a_noop_for_int16() -> None:
    samples = np.array([1, 2, 3], dtype=np.int16)
    assert to_int16(samples).dtype == np.int16
    assert list(to_int16(samples)) == [1, 2, 3]


def test_rms_of_silence_is_zero_and_of_a_square_wave_is_its_amplitude() -> None:
    assert rms_int16(np.zeros(160, dtype=np.int16)) == 0.0
    loud = np.full(160, 1000, dtype=np.int16)
    assert rms_int16(loud) == 1000.0
    assert rms_int16(np.empty(0, dtype=np.int16)) == 0.0


def test_integer_decimation_48k_to_16k() -> None:
    resampler = LinearResampler(48000, TARGET_SAMPLE_RATE)
    block = np.full(480, 900, dtype=np.int16)  # 10ms at 48 kHz
    out = resampler.process(block)
    assert out.dtype == np.int16
    assert out.size == 160  # 10ms at 16 kHz
    assert np.all(out == 900)  # averaging a constant signal preserves it


def test_decimation_carries_leftover_samples_between_blocks() -> None:
    resampler = LinearResampler(48000, TARGET_SAMPLE_RATE)
    first = resampler.process(np.full(100, 300, dtype=np.int16))
    second = resampler.process(np.full(100, 300, dtype=np.int16))
    assert first.size + second.size == (200 // 3)
    assert resampler.input_samples == 200
    assert resampler.output_samples == first.size + second.size


def test_non_integer_ratio_uses_interpolation_without_drift() -> None:
    resampler = LinearResampler(44100, TARGET_SAMPLE_RATE)
    total_in = 0
    total_out = 0
    for _ in range(20):
        block = np.full(1470, 500, dtype=np.int16)  # 1470 = 44.1k * 33.3ms
        out = resampler.process(block)
        total_in += block.size
        total_out += out.size
    # 44100 -> 16000 is a 2.75625 ratio; allow one sample of rounding slack.
    assert abs(total_out - total_in * 16000 / 44100) <= 1.0
    assert out.dtype == np.int16


def test_same_rate_passes_through() -> None:
    resampler = LinearResampler(16000, 16000)
    block = np.arange(320, dtype=np.int16)
    assert np.array_equal(resampler.process(block), block)


def test_empty_blocks_are_handled() -> None:
    resampler = LinearResampler(48000)
    assert resampler.process(np.empty(0, dtype=np.int16)).size == 0
    assert resampler.reset() is None


def test_stereo_float_input_is_converted() -> None:
    stereo = np.array([[0.5, -0.5]] * 480, dtype=np.float32)
    out = resample_int16(stereo, 48000)
    assert out.dtype == np.int16
    assert out.size == 160
