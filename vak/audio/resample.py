"""PCM16 mono resampling between 16 k / 24 k / 48 k (spec 01 §4.1).

Linear interpolation, stateless per call: good enough for cue files and
for matching a TTS rate to the negotiated downlink rate. Not meant for
the ASR main path, which the spec says should be decoded straight to 16 kHz.
"""

from __future__ import annotations

import numpy as np


def resample_pcm16(pcm16: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate or not pcm16:
        return pcm16
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError("sample rates must be positive")
    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
    n_out = int(round(x.size * dst_rate / src_rate))
    if n_out <= 0:
        return b""
    # sample positions of the output grid expressed in input samples
    pos = np.arange(n_out, dtype=np.float64) * (src_rate / dst_rate)
    y = np.interp(pos, np.arange(x.size, dtype=np.float64), x)
    return np.clip(np.rint(y), -32768, 32767).astype(np.int16).tobytes()
