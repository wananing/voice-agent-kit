"""Leading-silence detection (spec 06 §2.3): first-audio timing starts at
the first 10 ms window whose RMS exceeds −45 dBFS, so cue files and
streaming TTS are measured from real sound, not from the first frame.
"""

from __future__ import annotations

import math

import numpy as np


def rms_dbfs(pcm16: bytes) -> float:
    if not pcm16:
        return -math.inf
    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    if x.size == 0:
        return -math.inf
    r = float(np.sqrt(np.mean(x * x)))
    return 20 * math.log10(r) if r > 0 else -math.inf


def first_nonsilent_ms(
    pcm16: bytes, sample_rate: int, *, window_ms: int = 10, threshold_dbfs: float = -45.0
) -> int | None:
    """Offset in ms of the first non-silent window, or None if all silent."""
    win = int(sample_rate * window_ms / 1000) * 2
    if win <= 0:
        return None
    for off in range(0, len(pcm16) - win + 1, win):
        if rms_dbfs(pcm16[off : off + win]) > threshold_dbfs:
            return (off // 2) * 1000 // sample_rate
    return None


def trim_leading_silence(pcm16: bytes, sample_rate: int, **kw: float) -> bytes:
    ms = first_nonsilent_ms(pcm16, sample_rate, **kw)  # type: ignore[arg-type]
    if ms is None:
        return b""
    return pcm16[(ms * sample_rate // 1000) * 2 :]


def is_silent(pcm16: bytes, threshold_dbfs: float = -45.0) -> bool:
    return rms_dbfs(pcm16) <= threshold_dbfs


def duration_ms(pcm16: bytes, sample_rate: int) -> int:
    return (len(pcm16) // 2) * 1000 // sample_rate
