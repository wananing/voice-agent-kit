"""Frame-level voice activity detection (handbook turn-detection, audio-preprocessing).

`VadPort.process` takes one PCM16 mono frame and answers "speech?". The
endpointer turns that frame signal into a turn-level decision. No absolute
volume gate lives outside the VAD itself (handbook: AGC first, no
`min_volume`).
"""

from __future__ import annotations

from typing import Any, Protocol

import numpy as np

from vak.audio.silence import rms_dbfs


class VadPort(Protocol):
    def process(self, pcm16_frame: bytes) -> bool: ...


class EnergyVad:
    """RMS threshold with hangover. Fine for tests and for PTT pause
    compression; use SileroVad for open-mic endpointing."""

    def __init__(self, threshold_dbfs: float = -40.0, hangover_frames: int = 2) -> None:
        self.threshold_dbfs = threshold_dbfs
        self.hangover_frames = hangover_frames
        self._hang = 0

    def process(self, pcm16_frame: bytes) -> bool:
        if rms_dbfs(pcm16_frame) > self.threshold_dbfs:
            self._hang = self.hangover_frames
            return True
        if self._hang > 0:
            self._hang -= 1
            return True
        return False

    def reset(self) -> None:
        self._hang = 0


class SileroVad:
    """Silero VAD through onnxruntime (optional dependency `vad`).

    Accepts frames of any length: samples are buffered and scored per
    model window (512 samples at 16 kHz, 256 at 8 kHz, ~32 ms). A frame is
    speech if any window completed inside it scored >= `threshold`, or, if
    no window completed, if the last window did. Supports the v5 layout
    (`input`, `state`, `sr`) and the v4 layout (`input`, `h`, `c`, `sr`).
    """

    def __init__(self, model_path: str, sample_rate: int = 16000, threshold: float = 0.5) -> None:
        if sample_rate not in (8000, 16000):
            raise ValueError("Silero VAD runs at 8 kHz or 16 kHz; resample first")
        try:
            import onnxruntime as ort  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                "SileroVad needs the optional dependency `onnxruntime` (pip install 'voice-agent-kit[vad]')"
            ) from e
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._sess: Any = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        names = {i.name for i in self._sess.get_inputs()}
        self._v5 = "state" in names
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.window = 512 if sample_rate == 16000 else 256
        self._buf = np.zeros(0, dtype=np.float32)
        self._last_prob = 0.0
        self.reset()

    def reset(self) -> None:
        if self._v5:
            self._state = np.zeros((2, 1, 128), dtype=np.float32)
        else:
            self._h = np.zeros((2, 1, 64), dtype=np.float32)
            self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._buf = np.zeros(0, dtype=np.float32)
        self._last_prob = 0.0

    def _score(self, chunk: np.ndarray) -> float:
        x = chunk[np.newaxis, :]
        sr = np.array(self.sample_rate, dtype=np.int64)
        if self._v5:
            out, self._state = self._sess.run(None, {"input": x, "state": self._state, "sr": sr})
        else:
            out, self._h, self._c = self._sess.run(None, {"input": x, "h": self._h, "c": self._c, "sr": sr})
        return float(out.reshape(-1)[0])

    def process(self, pcm16_frame: bytes) -> bool:
        x = np.frombuffer(pcm16_frame, dtype=np.int16).astype(np.float32) / 32768.0
        self._buf = np.concatenate([self._buf, x])
        hit = False
        scored = False
        while self._buf.size >= self.window:
            chunk, self._buf = self._buf[: self.window], self._buf[self.window :]
            self._last_prob = self._score(chunk)
            scored = True
            hit = hit or self._last_prob >= self.threshold
        if scored:
            return hit
        return self._last_prob >= self.threshold

    @property
    def last_prob(self) -> float:
        return self._last_prob
