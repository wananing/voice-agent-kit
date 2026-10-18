"""EnergyVad behaviour; SileroVad only when onnxruntime and a model file are available."""

from __future__ import annotations

import math
import os

import numpy as np
import pytest

from vak.audio.vad import EnergyVad

RATE = 16000


def tone(ms: int = 20, amp: float = 0.3) -> bytes:
    t = np.arange(RATE * ms // 1000) / RATE
    return (amp * 32767 * np.sin(2 * math.pi * 440 * t)).astype(np.int16).tobytes()


def silence(ms: int = 20) -> bytes:
    return b"\x00\x00" * (RATE * ms // 1000)


def test_energy_vad_speech_silence_and_hangover() -> None:
    vad = EnergyVad(threshold_dbfs=-40.0, hangover_frames=2)
    assert vad.process(silence()) is False
    assert vad.process(tone()) is True
    assert vad.process(silence()) is True  # hangover 1
    assert vad.process(silence()) is True  # hangover 2
    assert vad.process(silence()) is False
    vad.reset()
    assert vad.process(silence()) is False


def test_energy_vad_quiet_voice_passes_without_absolute_gate() -> None:
    # handbook: no absolute volume gate; a quiet (-30 dBFS) child voice is still speech
    vad = EnergyVad(threshold_dbfs=-40.0, hangover_frames=0)
    assert vad.process(tone(amp=0.03)) is True


@pytest.mark.skipif(
    not os.environ.get("VAK_SILERO_MODEL") or not os.path.exists(os.environ.get("VAK_SILERO_MODEL", "")),
    reason="set VAK_SILERO_MODEL=<silero_vad.onnx> to run",
)
def test_silero_vad_tone_vs_silence() -> None:
    pytest.importorskip("onnxruntime")
    from vak.audio.vad import SileroVad

    vad = SileroVad(os.environ["VAK_SILERO_MODEL"], RATE)
    assert vad.process(silence(100)) is False
    assert vad.last_prob < 0.5
