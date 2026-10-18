"""Pre-synthesised cue library (architecture §1 "可选：预合成 cue 音频"; spec 02 cue kind).

Implements `CuePort`: `get(cue_id) -> (pcm16, sample_rate, duration_ms)`.
Files are `<dir>/<cue_id>.wav`, 16-bit PCM, mono (stereo is down-mixed),
any rate (resampled to the library rate). Leading silence is trimmed so
the arbiter's first-audio clock starts at real sound (spec 06 §2.3).

Well-known ids the kit itself asks for are listed in `WELL_KNOWN_CUES`.
`CueLibrary.with_synth_defaults()` fills them with tones so the kit runs
without any audio files (tests use this too).
"""

from __future__ import annotations

import logging
import math
import wave
from pathlib import Path

import numpy as np

from vak.audio.resample import resample_pcm16
from vak.audio.silence import duration_ms, trim_leading_silence
from vak.core.defaults import DEFAULTS

log = logging.getLogger(__name__)

WELL_KNOWN_CUES: dict[str, str] = {
    "thinking_short": "A1 tool cue, <= 300 ms (spec 03 §2.1 cue=short; ToolParams.cue_short_max_ms)",
    "thinking_long": "A2/A3 tool cue and T_filler filler, ~760 ms (ToolParams.cue_long_ms)",
    "error_generic": "spoken fallback for failed turns; profile A must make a sound (spec 01 §2.4)",
    "error_no_speech": "failed{no_speech} / failed{asr_timeout}: 'did not catch that, say it again'",
    "reconnecting": "link lost or upstream rebuilding: 'signal is bad' (spec 01 §6.3)",
}


def synth_tone_cue(ms: int, freq: float, sample_rate: int = 16000, amplitude: float = 0.3) -> bytes:
    """Sine tone with a 10 ms fade in/out, PCM16 mono. Starts above -45 dBFS."""
    n = max(1, sample_rate * ms // 1000)
    t = np.arange(n, dtype=np.float32) / sample_rate
    y = amplitude * np.sin(2 * math.pi * freq * t)
    fade = min(n // 2, sample_rate * 10 // 1000)
    if fade > 0:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        y[:fade] *= ramp
        y[-fade:] *= ramp[::-1]
    return (y * 32767).astype(np.int16).tobytes()


def read_wav_pcm16(path: str | Path) -> tuple[bytes, int]:
    """Return (pcm16 mono, sample_rate) from a 16-bit WAV file."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: cue files must be 16-bit PCM (got {w.getsampwidth() * 8}-bit)")
        rate = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    if ch > 1:
        x = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch).astype(np.int32)
        raw = x.mean(axis=1).astype(np.int16).tobytes()
    return raw, rate


class CueLibrary:
    def __init__(self, dir: str | Path | None = None, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate
        self._cues: dict[str, tuple[bytes, int, int]] = {}
        if dir is not None:
            self.load_dir(dir)

    @classmethod
    def with_synth_defaults(cls, sample_rate: int = 16000) -> CueLibrary:
        lib = cls(None, sample_rate)
        tools = DEFAULTS.tools
        lib.add("thinking_short", synth_tone_cue(tools.cue_short_max_ms, 880, sample_rate), sample_rate)
        lib.add("thinking_long", synth_tone_cue(tools.cue_long_ms, 660, sample_rate), sample_rate)
        lib.add("error_generic", synth_tone_cue(400, 330, sample_rate), sample_rate)
        lib.add("error_no_speech", synth_tone_cue(400, 440, sample_rate), sample_rate)
        lib.add("reconnecting", synth_tone_cue(500, 523, sample_rate), sample_rate)
        return lib

    def load_dir(self, dir: str | Path) -> list[str]:
        """Load every `*.wav` in `dir`; the file stem is the cue id. Returns the ids loaded."""
        loaded: list[str] = []
        for path in sorted(Path(dir).glob("*.wav")):
            try:
                pcm, rate = read_wav_pcm16(path)
            except (ValueError, wave.Error) as e:
                log.warning("skipping cue %s: %s", path.name, e)
                continue
            self.add(path.stem, pcm, rate)
            loaded.append(path.stem)
        return loaded

    def add(self, cue_id: str, pcm16: bytes, sample_rate: int) -> tuple[bytes, int, int]:
        pcm = resample_pcm16(pcm16, sample_rate, self.sample_rate)
        pcm = trim_leading_silence(pcm, self.sample_rate)
        entry = (pcm, self.sample_rate, duration_ms(pcm, self.sample_rate))
        self._cues[cue_id] = entry
        return entry

    def get(self, cue_id: str) -> tuple[bytes, int, int] | None:
        return self._cues.get(cue_id)

    def ids(self) -> list[str]:
        return sorted(self._cues)

    def missing_well_known(self) -> list[str]:
        return [c for c in WELL_KNOWN_CUES if c not in self._cues]
