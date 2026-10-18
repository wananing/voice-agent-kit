"""PCM16 resampling between 16 k / 24 k / 48 k."""

from __future__ import annotations

import math

import numpy as np
import pytest

from vak.audio.resample import resample_pcm16


def tone(rate: int, ms: int, freq: float = 440.0) -> bytes:
    t = np.arange(rate * ms // 1000) / rate
    return (0.5 * 32767 * np.sin(2 * math.pi * freq * t)).astype(np.int16).tobytes()


def zero_crossings(pcm: bytes) -> int:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.int32)
    return int(np.sum(np.abs(np.diff(np.sign(x))) > 0))


@pytest.mark.parametrize(("src", "dst"), [(16000, 24000), (24000, 16000), (16000, 48000), (48000, 16000), (24000, 48000)])
def test_length_and_pitch_preserved(src: int, dst: int) -> None:
    pcm = tone(src, 100)
    out = resample_pcm16(pcm, src, dst)
    assert len(out) == dst * 100 // 1000 * 2
    assert abs(zero_crossings(out) - zero_crossings(pcm)) <= 2  # same frequency, same duration


def test_identity_and_empty() -> None:
    pcm = tone(16000, 20)
    assert resample_pcm16(pcm, 16000, 16000) is pcm
    assert resample_pcm16(b"", 16000, 24000) == b""
    with pytest.raises(ValueError):
        resample_pcm16(pcm, 0, 16000)
