"""open_mic: gateway endpointer drives turn.start{vad}/turn.end, VAD barge-in with filtering."""

from __future__ import annotations

from tests.unit.turn.fakes import FakeEndpointer
from tests.unit.turn.harness import make
from tests.unit.turn.harness import ms_to_samples as S
from vak.core.defaults import TurnTimers
from vak.core.turn.machine import EndpointEvent
from vak.core.types import Mode, TurnId, TurnState
from vak.upstream.base import EventType


async def _open(h):
    await h.machine.handle_message({"type": "listen.open", "mode": "open_mic", "reason": "wake"})


async def test_vad_turn_start_endpoint_commit():
    ep = FakeEndpointer()
    h = make(mode=Mode.OPEN_MIC, endpointer=ep)
    await _open(h)
    await h.clock.advance(5000)
    await h.audio(0)  # outside a turn: forwarded with turn_id None (open mic streams continuously)
    assert h.upstream.audio[-1].turn_id is None
    ep.pending = [EndpointEvent("speech_start", S(20))]
    await h.audio(S(20))
    assert h.transport.of_type("turn.start") == [{"type": "turn.start", "turn_id": "s1", "source": "vad", "ts_samples": S(20)}]
    assert h.machine.state is TurnState.LISTENING
    await h.audio(S(40), turn_u32=0)
    assert h.upstream.audio[-1].turn_id == TurnId("s", 1)  # attributed by window, not header
    ep.pending = [EndpointEvent("speech_end", S(800))]
    await h.audio(S(800))
    assert h.arbiter.endpointing == [(TurnId("s", 1), True)]
    assert ("turn.endpointing", {"turn_id": TurnId("s", 1), "active": True}) in h.bus.history
    ep.pending = [EndpointEvent("endpoint", S(1000), prob=0.9)]
    await h.audio(S(1000))
    assert h.arbiter.endpointing[-1] == (TurnId("s", 1), False)
    assert h.transport.of_type("turn.end") == [{"type": "turn.end", "turn_id": "s1", "source": "vad", "ts_samples": S(1000), "reason": "endpoint"}]
    assert h.machine.state is TurnState.COMMITTED
    tr = h.trace.merged("s1")
    assert tr["anchors"]["primary"] == "endpoint" and tr["speech_ms"] == 780 and "audio_end_at_ms" in tr["anchors"]
    await h.full_reply("s1")
    assert h.closed("s1")[0]["state"] == "completed"


async def test_button_outranks_vad_same_turn():
    ep = FakeEndpointer()
    h = make(mode=Mode.OPEN_MIC, endpointer=ep)
    await _open(h)
    ep.pending = [EndpointEvent("speech_start", 0)]
    await h.audio(0)
    await h.start("c7", ts=S(100))  # R2.3-1: button while listening{vad}
    assert h.transport.of_type("turn.merge") == [{"type": "turn.merge", "from": "c7", "into": "s1"}]
    assert h.machine.current_turn.source.value == "button" and h.arbiter.generation == 1
    await h.end("c7", ts=S(1000))
    assert h.machine.state is TurnState.COMMITTED


async def _play_s1(h, ep):
    await h.clock.advance(TurnTimers().aec_warmup_ms)
    ep.pending = [EndpointEvent("speech_start", 0)]
    await h.audio(0)
    ep.pending = [EndpointEvent("speech_end", S(800)), EndpointEvent("endpoint", S(1000), prob=0.9)]
    await h.audio(S(1000))
    await h.transcript_final()
    await h.response_started("r1")
    await h.audio_delta("r1", ms=2000)
    assert h.machine.state is TurnState.RESPONDING


async def test_item14_speech_in_aec_warmup_does_not_flush():
    ep = FakeEndpointer()
    h = make(mode=Mode.OPEN_MIC, endpointer=ep)
    await _open(h)
    ep.pending = [EndpointEvent("speech_start", 0)]
    await h.audio(0)
    ep.pending = [EndpointEvent("speech_end", S(800)), EndpointEvent("endpoint", S(1000), prob=0.9)]
    await h.audio(S(1000))
    await h.transcript_final()
    await h.response_started("r1")
    await h.audio_delta("r1")
    assert h.clock.now_ms() < TurnTimers().aec_warmup_ms
    ep.pending = [EndpointEvent("speech_start", S(1200))]
    await h.audio(S(1200))
    await h.audio(S(2200), n_ms=1000)
    assert h.transport.of_type("flush.start") == []
    assert h.machine.state is TurnState.RESPONDING
    assert h.trace.merged("s1")["false_interrupt_filtered"] == 1


async def test_short_speech_during_playback_is_filtered():
    ep = FakeEndpointer()
    h = make(mode=Mode.OPEN_MIC, endpointer=ep)
    await _open(h)
    await _play_s1(h, ep)
    ep.pending = [EndpointEvent("speech_start", S(1200))]
    await h.audio(S(1200))
    ep.pending = [EndpointEvent("speech_end", S(1400))]  # 200 ms < barge_in_min_ms
    await h.audio(S(1400))
    assert h.transport.of_type("flush.start") == []
    assert h.trace.merged("s1")["false_interrupt_filtered"] == 1
    assert h.machine.state is TurnState.RESPONDING


async def test_vad_barge_in_with_receipt():
    ep = FakeEndpointer()
    h = make(mode=Mode.OPEN_MIC, endpointer=ep)
    await _open(h)
    await _play_s1(h, ep)
    ep.pending = [EndpointEvent("speech_start", S(1200))]
    await h.audio(S(1200))
    assert h.transport.of_type("flush.start") == []
    await h.audio(S(1700), n_ms=20)  # ≥ barge_in_min_ms of speech → confirmed
    types = h.types()
    assert h.transport.of_type("flush.start")[0]["turn_ids"] == ["s1"]
    assert types.index("flush.start") < types.index("turn.start", 1)  # flush then turn.start{s2}
    assert h.transport.of_type("turn.start")[-1]["turn_id"] == "s2"
    assert h.closed("s1") == []  # waiting ≤ T_intr_receipt for playback.interrupted
    await h.receipt("interrupted", "s1", played_ms=640, reason="vad")
    closed = h.closed("s1")[0]
    assert closed["state"] == "interrupted" and closed["by"] == "vad" and closed["heard_ms"] == 640
    assert h.types().index("tts.stop") < h.types().index("turn.closed")
    assert h.machine.state is TurnState.LISTENING and str(h.machine.current_turn.turn_id) == "s2"
    assert h.arbiter.generation == 2  # one +1 for barge-in + new turn together
    assert h.trace.merged("s1")["truncate_estimated"] is False


async def test_vad_barge_in_receipt_timeout_marks_estimated():
    ep = FakeEndpointer()
    h = make(mode=Mode.OPEN_MIC, endpointer=ep)
    await _open(h)
    await _play_s1(h, ep)
    ep.pending = [EndpointEvent("speech_start", S(1200))]
    await h.audio(S(1200))
    await h.audio(S(1700))
    await h.clock.advance(TurnTimers().T_intr_receipt)
    assert h.closed("s1")[0]["state"] == "interrupted"
    tr = h.trace.merged("s1")
    assert tr["truncate_estimated"] is True and tr["watchdog_fired"] == "T_intr_receipt"
    # new turn continues normally
    ep.pending = [EndpointEvent("speech_end", S(2000)), EndpointEvent("endpoint", S(2200), prob=0.8)]
    await h.audio(S(2200))
    assert h.machine.state is TurnState.COMMITTED
    await h.full_reply("s2", rid="r2")
    assert h.closed("s2")[0]["state"] == "completed"


async def test_full_duplex_without_endpointer_uses_upstream_vad_events():
    h = make(mode=Mode.OPEN_MIC, endpointer=None)
    await _open(h)
    await h.audio(S(500), n_ms=500)
    await h.ev(EventType.INPUT_SPEECH_STARTED)
    assert h.machine.state is TurnState.LISTENING and h.transport.of_type("turn.start")[0]["source"] == "vad"
    await h.audio(S(1000), n_ms=1000)
    await h.ev(EventType.INPUT_SPEECH_STOPPED)
    assert h.machine.state is TurnState.COMMITTED


async def test_open_mic_short_speech_discarded():
    ep = FakeEndpointer()
    h = make(mode=Mode.OPEN_MIC, endpointer=ep)
    await _open(h)
    ep.pending = [EndpointEvent("speech_start", 0)]
    await h.audio(0)
    ep.pending = [EndpointEvent("speech_end", S(100)), EndpointEvent("endpoint", S(300), prob=0.9)]
    await h.audio(S(300))
    assert h.closed("s1")[0]["by"] == "short_speech"
    assert h.machine.state is TurnState.IDLE
