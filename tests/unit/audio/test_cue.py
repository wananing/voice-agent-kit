"""CueLibrary (CuePort): loads generated wavs, resamples, trims leading silence, synth defaults."""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np

from vak.audio.silence import first_nonsilent_ms
from vak.core.defaults import DEFAULTS
from vak.cue.library import WELL_KNOWN_CUES, CueLibrary, synth_tone_cue


def write_wav(path: Path, rate: int, channels: int, silence_ms: int, tone_ms: int) -> None:
    n_sil = rate * silence_ms // 1000
    t = np.arange(rate * tone_ms // 1000) / rate
    tone = (0.4 * 32767 * np.sin(2 * math.pi * 440 * t)).astype(np.int16)
    mono = np.concatenate([np.zeros(n_sil, dtype=np.int16), tone])
    data = np.repeat(mono, channels) if channels > 1 else mono
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


def test_loads_wav_resamples_and_trims_leading_silence(tmp_path: Path) -> None:
    write_wav(tmp_path / "thinking_long.wav", 24000, 1, silence_ms=300, tone_ms=400)
    write_wav(tmp_path / "error_generic.wav", 16000, 2, silence_ms=0, tone_ms=250)  # stereo downmix
    (tmp_path / "notes.txt").write_text("ignored")
    lib = CueLibrary(tmp_path, sample_rate=16000)
    assert lib.ids() == ["error_generic", "thinking_long"]
    pcm, rate, dur = lib.get("thinking_long")  # type: ignore[misc]
    assert rate == 16000
    assert abs(dur - 400) <= 10  # 300 ms silence trimmed (spec 06 §2.3), resampled 24k -> 16k
    assert first_nonsilent_ms(pcm, 16000) == 0
    pcm2, _, dur2 = lib.get("error_generic")  # type: ignore[misc]
    assert abs(dur2 - 250) <= 10 and len(pcm2) == dur2 * 16000 // 1000 * 2
    assert lib.get("missing") is None
    assert "thinking_short" in lib.missing_well_known()


def test_synth_defaults_cover_well_known_ids() -> None:
    lib = CueLibrary.with_synth_defaults(16000)
    assert lib.missing_well_known() == []
    assert set(lib.ids()) == set(WELL_KNOWN_CUES)
    pcm, rate, dur = lib.get("thinking_short")  # type: ignore[misc]
    assert rate == 16000 and 0 < dur <= DEFAULTS.tools.cue_short_max_ms  # spec 03 §2.1: short cue <= 300 ms
    assert first_nonsilent_ms(pcm, 16000) == 0
    assert lib.get("thinking_long")[2] == DEFAULTS.tools.cue_long_ms  # type: ignore[index]


def test_synth_tone_cue_shape() -> None:
    pcm = synth_tone_cue(100, 440, 24000)
    assert len(pcm) == 24000 * 100 // 1000 * 2
    assert first_nonsilent_ms(pcm, 24000) == 0
