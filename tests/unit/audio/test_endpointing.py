"""Spec 01 §2.3 open-mic endpointing (handbook turn-detection): VAD silence ->
semantic P(done) -> min / max wait; silence measured in samples, not wall clock."""

from __future__ import annotations

import math

import numpy as np

from vak.audio.endpointing import Endpointer, EndpointEvent, EpState, NullSemantic
from vak.audio.vad import EnergyVad
from vak.core.defaults import EndpointingParams

RATE = 16000
FRAME = RATE * 20 // 1000  # samples per 20 ms frame
PARAMS = EndpointingParams(vad_silence_ms=200, min_endpoint_ms=500, max_endpoint_ms=3000, semantic_threshold=0.6)


def tone() -> bytes:
    t = np.arange(FRAME) / RATE
    return (0.3 * 32767 * np.sin(2 * math.pi * 440 * t)).astype(np.int16).tobytes()


SILENCE = b"\x00\x00" * FRAME


class FixedSemantic:
    def __init__(self, prob: float) -> None:
        self.prob = prob
        self.calls: list[tuple[int, int]] = []

    def predict(self, pcm16_last_n_seconds: bytes, sample_rate: int) -> float:
        self.calls.append((len(pcm16_last_n_seconds), sample_rate))
        return self.prob


def make(semantic: object | None = None) -> Endpointer:
    return Endpointer(PARAMS, EnergyVad(hangover_frames=0), semantic, RATE)  # type: ignore[arg-type]


def run(ep: Endpointer, frames: list[bytes], start_ts: int = 0, now_ms: int = 0) -> list[tuple[int, EndpointEvent]]:
    out: list[tuple[int, EndpointEvent]] = []
    for i, f in enumerate(frames):
        for ev in ep.feed(f, start_ts + i * FRAME, now_ms):
            out.append((i, ev))
    return out


def test_speech_start_then_endpoint_after_min_wait_with_null_semantic() -> None:
    ep = make(NullSemantic())
    frames = [SILENCE] * 3 + [tone()] * 20 + [SILENCE] * 40
    events = run(ep, frames, start_ts=1000)
    kinds = [(i, e.kind) for i, e in events]
    # tone begins at frame 3; silence begins at frame 23 (ts = 1000 + 23*320)
    sil_start = 1000 + 23 * FRAME
    assert kinds[0] == (3, "speech_start") and events[0][1].ts_samples == 1000 + 3 * FRAME
    # vad_silence 200 ms = 10 frames -> speech_end on the 10th silent frame (index 32)
    assert kinds[1] == (32, "speech_end") and events[1][1].ts_samples == sil_start and events[1][1].prob == 1.0
    # min_endpoint 500 ms = 25 frames -> endpoint on silent frame index 47
    assert kinds[2] == (47, "endpoint") and events[2][1].ts_samples == sil_start
    assert len(events) == 3 and ep.state is EpState.IDLE


def test_low_semantic_probability_waits_until_max_endpoint() -> None:
    sem = FixedSemantic(0.2)
    ep = make(sem)
    frames = [tone()] * 10 + [SILENCE] * 200
    events = run(ep, frames)
    assert [(i, e.kind) for i, e in events] == [(0, "speech_start"), (19, "speech_end"), (159, "endpoint")]
    assert events[2][1].prob == 0.2  # max_endpoint 3000 ms = 150 frames after silence start at frame 10
    assert len(sem.calls) == 1 and sem.calls[0][1] == RATE and sem.calls[0][0] == 20 * FRAME * 2


def test_semantic_above_threshold_takes_min_path() -> None:
    ep = make(FixedSemantic(0.9))
    events = run(ep, [tone()] * 10 + [SILENCE] * 40)
    assert [(i, e.kind) for i, e in events] == [(0, "speech_start"), (19, "speech_end"), (34, "endpoint")]
    assert ep.endpointing is False


def test_resumed_speech_cancels_pending_endpoint() -> None:
    ep = make(FixedSemantic(0.2))
    frames = [tone()] * 10 + [SILENCE] * 30 + [tone()] * 5 + [SILENCE] * 200
    events = run(ep, frames)
    kinds = [(i, e.kind) for i, e in events]
    assert kinds[:3] == [(0, "speech_start"), (19, "speech_end"), (40, "speech_start")]
    assert kinds[3] == (54, "speech_end")  # silence starts at frame 45, +10 frames
    assert kinds[4] == (194, "endpoint")  # 45 + 150 - 1
    assert len(kinds) == 5


def test_short_pause_inside_utterance_emits_nothing() -> None:
    ep = make(NullSemantic())
    events = run(ep, [tone()] * 5 + [SILENCE] * 5 + [tone()] * 5)  # 100 ms pause < vad_silence
    assert [e.kind for _, e in events] == ["speech_start"]
    assert ep.speaking


def test_silence_measured_in_samples_not_wall_clock() -> None:
    ep = make(NullSemantic())
    run(ep, [tone()] * 5)
    # wall clock jumps 10 s, but only one silent frame (20 ms) arrived: no decision yet
    assert ep.feed(SILENCE, 5 * FRAME, now_ms=10_000) == []
    assert ep.state is EpState.SILENCE


def test_reset_and_endpointing_flag() -> None:
    ep = make(FixedSemantic(0.2))
    run(ep, [tone()] * 5 + [SILENCE] * 12)
    assert ep.endpointing is True  # speech_end emitted, waiting for max_endpoint
    ep.reset()
    assert ep.state is EpState.IDLE and ep.endpointing is False
