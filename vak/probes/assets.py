"""Synthetic probe audio (spec 04 §7.1 "探针音频集").

Everything is generated with numpy so the harness runs without any
recordings: tone bursts (speech stand-in for VAD-level probes), bursts with
inserted silences of 200/300/400/500/700 ms (P0-3), 50/100/150 ms snippets
(P0-4), band-limited noise (P0-2 interruption), plus optional real speech
loaded from a directory for probes that need the model to understand
content (P0-1, P0-9, P0-14).
"""

from __future__ import annotations

import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SILENCE_STEPS_MS = (200, 300, 400, 500, 700)
SNIPPET_STEPS_MS = (50, 100, 150)


def tone(duration_ms: int, sample_rate: int = 16000, *, freq_hz: float = 220.0, amplitude: float = 0.3) -> bytes:
    """A harmonic-rich tone burst with a 10 ms fade, loud enough for any VAD."""
    n = sample_rate * duration_ms // 1000
    if n <= 0:
        return b""
    t = np.arange(n, dtype=np.float64) / sample_rate
    x = np.zeros(n)
    for k, gain in ((1, 1.0), (2, 0.5), (3, 0.25), (4, 0.125)):
        x += gain * np.sin(2 * np.pi * freq_hz * k * t)
    x *= amplitude / np.max(np.abs(x))
    fade = min(n, sample_rate // 100)
    if fade:
        ramp = np.linspace(0, 1, fade)
        x[:fade] *= ramp
        x[-fade:] *= ramp[::-1]
    return (x * 32767).astype(np.int16).tobytes()


def silence(duration_ms: int, sample_rate: int = 16000) -> bytes:
    return bytes(2 * (sample_rate * duration_ms // 1000))


def noise(duration_ms: int, sample_rate: int = 16000, *, amplitude: float = 0.2, seed: int = 7) -> bytes:
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(sample_rate * duration_ms // 1000) * amplitude
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


def tone_with_gap(gap_ms: int, sample_rate: int = 16000, *, burst_ms: int = 1500) -> bytes:
    """burst - gap - burst: the P0-3 silence-threshold sample."""
    return tone(burst_ms, sample_rate) + silence(gap_ms, sample_rate) + tone(burst_ms, sample_rate)


def load_wav(path: str | Path, sample_rate: int = 16000) -> bytes:
    """PCM16 mono at `sample_rate`; raises on a different format."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != sample_rate:
            raise ValueError(f"{path}: need mono PCM16 {sample_rate} Hz")
        return w.readframes(w.getnframes())


@dataclass(slots=True)
class ProbeAssets:
    """Frozen asset set. `speech_dir` may hold `<name>.wav` files used by
    `speech(name)`; without it a tone burst stands in (fine for VAD probes,
    useless for content probes, which then report `skipped`)."""

    sample_rate: int = 16000
    speech_dir: Path | None = None
    frame_ms: int = 20
    _cache: dict[str, bytes] = field(default_factory=dict)

    def utterance(self, duration_ms: int = 2000) -> bytes:
        return tone(duration_ms, self.sample_rate)

    def utterance_then_silence(self, speech_ms: int = 2000, silence_ms: int = 1000) -> bytes:
        return tone(speech_ms, self.sample_rate) + silence(silence_ms, self.sample_rate)

    def gap_samples(self) -> dict[int, bytes]:
        return {g: tone_with_gap(g, self.sample_rate) for g in SILENCE_STEPS_MS}

    def snippets(self) -> dict[int, bytes]:
        return {ms: tone(ms, self.sample_rate) for ms in SNIPPET_STEPS_MS}

    def noise(self, duration_ms: int = 800) -> bytes:
        return noise(duration_ms, self.sample_rate)

    def has_speech(self, name: str) -> bool:
        return self.speech_dir is not None and (self.speech_dir / f"{name}.wav").exists()

    def speech(self, name: str, fallback_ms: int = 2000) -> bytes:
        if name in self._cache:
            return self._cache[name]
        if self.has_speech(name):
            assert self.speech_dir is not None
            pcm = load_wav(self.speech_dir / f"{name}.wav", self.sample_rate)
        else:
            pcm = tone(fallback_ms, self.sample_rate)
        self._cache[name] = pcm
        return pcm

    def frames(self, pcm16: bytes) -> list[bytes]:
        step = 2 * (self.sample_rate * self.frame_ms // 1000)
        return [pcm16[i : i + step] for i in range(0, len(pcm16), step)]
