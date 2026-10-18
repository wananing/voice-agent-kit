"""Spec 01 acceptance items that need no device or real upstream, ptt mode."""

from __future__ import annotations

import pytest

from tests.unit.turn.fakes import FakeEndpointer
from tests.unit.turn.harness import make
from tests.unit.turn.harness import ms_to_samples as S
from vak.core.defaults import TurnTimers
from vak.core.turn.machine import EndpointEvent
from vak.core.types import IllegalTransition, Mode, TurnId, TurnState
from vak.upstream.base import EventType, ResponseOrigin, ResponseStatus


async def test_normal_ptt_turn_reaches_completed():
    h = make()
    await h.start("c1", ts=0)
    assert h.machine.state is TurnState.LISTENING
    await h.audio(0)
    await h.end("c1", ts=S(1500))
    assert h.machine.state is TurnState.COMMITTED
    assert h.upstream.of("commit_turn") == [("commit_turn", TurnId("c", 1), "commit")]
    assert len(h.upstream.of("request_response")) == 1  # response.client_initiated=yes
    await h.full_reply("c1")
    assert h.machine.state is TurnState.IDLE
    closed = h.closed("c1")
    assert len(closed) == 1 and closed[0]["state"] == "completed" and closed[0]["by"] == "playback_ended"
    assert closed[0]["heard_ms"] == 200 and closed[0]["segments"] == 1
    tr = h.trace.merged("c1")
    assert tr["turn_terminal"] == "completed" and tr["anchors"]["primary"] == "release"
    assert tr["t0_ms"] == tr["anchors"]["release_at_ms"] and tr["watchdog_fired"] == "none"
    assert tr["press_ms"] == 1500 and tr["generation"] == 1
    assert ("turn.start", {"turn_id": TurnId("c", 1), "source": h.machine.turn("c1").source, "generation": 1}) in h.bus.history
    assert any(n == "turn.reply_done" for n, _ in h.bus.history)
    assert h.context.user == [(TurnId("c", 1), "hello")]


async def test_item3_T_respond_fires_upstream_timeout():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.transcript_final("hello")
    await h.clock.advance(TurnTimers().T_respond - 1)
    assert h.closed("c1") == []
    await h.clock.advance(1)
    assert [e["code"] for e in h.errors("c1")] == ["upstream_timeout"]
    assert h.closed("c1")[0]["state"] == "failed" and h.closed("c1")[0]["by"] == "upstream_timeout"
    assert h.types().index("error") < h.types().index("turn.closed")
    tr = h.trace.merged("c1")
    assert tr["watchdog_fired"] == "T_respond" and tr["error_code"] == "upstream_timeout"


async def test_item3_T_filler_inserts_cue_without_closing():
    h = make(cues=True)
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.clock.advance(TurnTimers().T_filler_A)
    assert [r.kind.value for r in h.arbiter.requests] == ["cue"]
    assert h.machine.state is TurnState.COMMITTED and h.closed("c1") == []


async def test_T_asr_fires_when_no_transcript():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.clock.advance(TurnTimers().T_asr)
    assert [e["code"] for e in h.errors("c1")] == ["asr_timeout"]
    assert h.closed("c1")[0]["by"] == "asr_timeout"


async def test_item4_empty_asr_result_is_no_speech():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.transcript_final("")
    assert [e["code"] for e in h.errors("c1")] == ["no_speech"]
    assert h.closed("c1")[0]["state"] == "failed"
    assert h.types()[-2:] == ["error", "turn.closed"]


async def test_item5_ptt_ignores_upstream_speech_started():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.transcript_final()
    await h.response_started("r1")
    await h.audio_delta("r1")
    assert h.machine.state is TurnState.RESPONDING
    await h.ev(EventType.INPUT_SPEECH_STARTED)
    assert h.machine.state is TurnState.RESPONDING
    assert not any(c[0] == "on_barge_in" for c in h.arbiter.calls)
    assert h.transport.of_type("flush.start") == []


async def test_item6_segmenting_creates_no_turn_and_server_reply_is_cancelled():
    h = make()
    await h.start("c1")
    await h.audio(0)
    await h.ev(EventType.INPUT_SEGMENTED, segment_index=1)
    await h.response_started("srv-1", origin=ResponseOrigin.SERVER_VAD)  # before key-up
    await h.audio_delta("srv-1")
    assert h.upstream.of("cancel_response") == [("cancel_response", "srv-1")]
    assert h.arbiter.requests == []  # nothing submitted, nothing on the wire
    assert [k for k, _ in h.transport.log if k == "audio"] == []
    assert h.machine.current_turn is not None and str(h.machine.current_turn.turn_id) == "c1"
    assert len(h.machine._turns) == 1
    await h.end("c1", ts=S(1000))
    assert h.upstream.of("request_response")  # reply requested exactly once, at commit
    await h.full_reply("c1", rid="r1")
    closed = h.closed("c1")[0]
    assert closed["segments"] == 2 and closed["state"] == "completed"
    assert h.trace.merged("c1")["server_vad_segments_in_turn"] == 1


async def test_item7_supplement_window_merges_second_press():
    ep = FakeEndpointer()
    h = make(endpointer=ep)
    await h.start("c1", ts=0)
    ep.pending = [EndpointEvent("speech_start", 0), EndpointEvent("speech_end", S(900)), EndpointEvent("endpoint", S(1000), prob=0.2)]
    await h.audio(0, n_ms=1000)
    await h.end("c1", ts=S(1000))
    assert h.machine.state is TurnState.HOLDING
    assert h.upstream.of("commit_turn") == []
    await h.clock.advance(300)
    await h.start("c2", ts=S(1300))
    assert h.transport.of_type("turn.merge") == [{"type": "turn.merge", "from": "c2", "into": "c1"}]
    assert h.machine.state is TurnState.LISTENING and str(h.machine.current_turn.turn_id) == "c1"
    assert h.arbiter.generation == 1  # merged turn.start does not +1
    await h.audio(S(1300), turn_u32=2, n_ms=500)  # frames tagged c2 still belong to c1
    assert h.upstream.audio[-1].turn_id == TurnId("c", 1)
    ep.pending = [EndpointEvent("speech_start", S(1300)), EndpointEvent("speech_end", S(1800)), EndpointEvent("endpoint", S(1800), prob=0.9)]
    await h.audio(S(1800))
    await h.end("c2", ts=S(1800))  # alias resolves
    assert h.machine.state is TurnState.COMMITTED
    await h.full_reply("c1")
    assert len(h.closed("c1")) == 1 and h.closed("c2") == []
    assert h.trace.merged("c1")["supplement_window_hit"] is True


async def test_hold_expires_into_committed():
    ep = FakeEndpointer()
    h = make(endpointer=ep)
    await h.start("c1", ts=0)
    ep.pending = [EndpointEvent("speech_start", 0), EndpointEvent("speech_end", S(900)), EndpointEvent("endpoint", S(1000), prob=0.1)]
    await h.audio(0, n_ms=1000)
    await h.end("c1", ts=S(1000))
    await h.clock.advance(TurnTimers().T_hold)
    assert h.machine.state is TurnState.COMMITTED
    assert h.upstream.of("commit_turn")


async def test_item8_short_press_discarded_zero_output():
    h = make()
    await h.start("c1", ts=0)
    await h.end("c1", ts=S(100))
    closed = h.closed("c1")
    assert closed[0]["state"] == "discarded" and closed[0]["by"] == "short_press"
    assert h.upstream.of("commit_turn") == [("commit_turn", TurnId("c", 1), "discard")]
    assert h.arbiter.requests == [] and h.transport.of_type("tts.start") == []
    assert h.trace.merged("c1")["dropped_short_press"] is True
    # late upstream output for the discarded turn never reaches the arbiter
    await h.response_started("late")
    assert h.upstream.of("cancel_response") == [("cancel_response", "late")]
    assert h.arbiter.requests == []


async def test_short_speech_discarded_when_endpointer_present():
    ep = FakeEndpointer()
    h = make(endpointer=ep)
    await h.start("c1", ts=0)
    ep.pending = [EndpointEvent("speech_start", 0), EndpointEvent("speech_end", S(100))]
    await h.audio(0, n_ms=1000)
    await h.end("c1", ts=S(1000))
    assert h.closed("c1")[0]["by"] == "short_speech"


async def test_button_barge_in_sequence_order():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.transcript_final()
    await h.response_started("r1")
    await h.audio_delta("r1")
    n = len(h.transport.log)
    # spec 01 §5.1 ②③④ in one tick
    await h.receipt("interrupted", "c1", sentence_id=0, played_ms=120, reason="button")
    await h.abort("c1", "button")
    await h.start("c2", ts=S(5000))
    types = [m["type"] for k, m in h.transport.log[n:] if k == "json"]
    assert types == ["flush.start", "flush.end", "tts.stop", "turn.closed"]
    assert h.transport.of_type("flush.start")[0]["ack_of"] == "abort"
    assert h.transport.of_type("tts.stop")[0]["reason"] == "interrupted"
    closed = h.closed("c1")[0]
    assert closed["state"] == "interrupted" and closed["by"] == "button" and closed["heard_ms"] == 120
    assert h.machine.state is TurnState.LISTENING and str(h.machine.current_turn.turn_id) == "c2"
    # late delta for c1 after turn.closed: zero frames on the wire
    await h.audio_delta("r1")
    assert [k for k, _ in h.transport.log[n:] if k == "audio"] == []
    tr = h.trace.merged("c1")
    assert tr["interrupted"] is True and tr["interrupt_source"] == "button" and tr["truncate_estimated"] is False
    assert any(c[:2] == ("on_barge_in", TurnId("c", 1)) for c in h.arbiter.calls)


async def test_new_turn_start_interrupts_playing_turn_with_single_bump():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.transcript_final()
    await h.response_started("r1")
    await h.audio_delta("r1")
    gen_before = h.arbiter.generation
    await h.receipt("interrupted", "c1", played_ms=50, reason="button")
    await h.start("c2", ts=S(5000))
    assert h.arbiter.generation == gen_before + 1
    assert h.closed("c1")[0]["state"] == "interrupted"
    types = h.types()
    assert types.index("flush.end") < types.index("tts.stop") < types.index("turn.closed")


async def test_abort_without_receipt_waits_T_intr_receipt_then_estimates():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.transcript_final()
    await h.response_started("r1")
    await h.audio_delta("r1")
    await h.abort("c1", "button")
    assert h.closed("c1") == []  # waiting for playback.interrupted
    assert h.transport.of_type("flush.start")  # but the flush went out in the same tick
    await h.clock.advance(TurnTimers().T_intr_receipt)
    assert h.closed("c1")[0]["state"] == "interrupted"
    tr = h.trace.merged("c1")
    assert tr["truncate_estimated"] is True and tr["watchdog_fired"] == "T_intr_receipt"


async def test_item22_client_without_receipts_closes_with_estimate():
    h = make(client_caps={"aec": "device", "modes": ["ptt"], "receipts": []})
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.transcript_final()
    await h.response_started("r1")
    await h.audio_delta("r1")
    await h.abort("c1", "button")
    assert h.closed("c1")[0]["state"] == "interrupted"  # no wait: client can't send the receipt
    assert h.trace.merged("c1")["truncate_estimated"] is True
    # completed also works without `ended` receipts via T_drain
    await h.start("c2", ts=S(9000))
    await h.end("c2", ts=S(10000))
    await h.transcript_final()
    await h.response_started("r2")
    await h.audio_delta("r2", ms=400)
    await h.response_done("r2")
    assert h.machine.state is TurnState.DRAINING
    await h.clock.advance(400 + TurnTimers().T_drain_extra)
    closed = h.closed("c2")[0]
    assert closed["state"] == "completed" and closed["by"] == "T_drain" and closed["receipt_missing"] is True


async def test_abort_user_cancel_is_cancelled():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.abort("c1", "user_cancel")
    closed = h.closed("c1")[0]
    assert closed["state"] == "cancelled" and closed["by"] == "user_cancel"
    assert h.transport.of_type("tts.stop") == []  # nothing was playing
    assert h.transport.of_type("flush.start")  # but the queue is flushed


async def test_listen_close_cancels_listening_turn():
    h = make()
    await h.start("c1")
    await h.machine.handle_message({"type": "listen.close", "reason": "idle_timeout"})
    assert h.closed("c1")[0]["state"] == "cancelled"
    assert h.upstream.of("commit_turn")[0][2] == "discard"


async def test_text_turn_commits_immediately():
    h = make()
    await h.start("c1", source="text", text="what time is it", input="typed")
    assert h.machine.state is TurnState.COMMITTED
    assert h.context.user == [(TurnId("c", 1), "what time is it")]
    assert h.upstream.of("send_text") == [("send_text", TurnId("c", 1), "what time is it")]
    tr = h.trace.merged("c1")
    assert tr["turn_source"] == "text" and tr["turn_input"] == "typed" and tr["anchors"]["primary"] == "tap"


async def test_event_turn_starts_committed_and_completes_on_drain():
    h = make()
    tid = await h.machine.start_event_turn("narration")
    assert str(tid) == "s1" and h.machine.state is TurnState.COMMITTED
    assert h.transport.of_type("turn.start") == [{"type": "turn.start", "turn_id": "s1", "source": "event", "kind": "narration"}]
    assert not any(c[0] == "on_turn_start" for c in h.arbiter.calls)  # narration never +1
    await h.bus.emit("turn.sound_first", turn_id=tid, kind="narration")
    assert h.machine.state is TurnState.RESPONDING
    await h.drained("s1")
    assert h.closed("s1")[0]["state"] == "completed"
    assert h.trace.merged("s1")["anchors"]["primary"] == "event"
    with pytest.raises(IllegalTransition):
        await h.start("c1")
        await h.machine.start_event_turn("narration")


async def test_event_turn_T_respond_fails_silently_waiting_narration():
    h = make()
    await h.machine.start_event_turn("narration")
    await h.clock.advance(TurnTimers().T_respond)
    assert h.closed("s1")[0]["state"] == "failed"


async def test_awaiting_tool_defers_reply_done():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.transcript_final()
    await h.response_started("r1")
    h.tools.open.add("c1")
    await h.response_done("r1")
    assert h.arbiter.awaiting == [(TurnId("c", 1), True)]
    assert ("turn.awaiting_tool", {"turn_id": TurnId("c", 1), "awaiting": True}) in h.bus.history
    assert not any(c[0] == "on_reply_done" for c in h.arbiter.calls)
    await h.clock.advance(TurnTimers().T_respond * 2)  # T_respond is suspended while the tool runs
    assert h.closed("c1") == []
    h.tools.open.clear()
    await h.response_started("r2")
    assert h.arbiter.awaiting[-1] == (TurnId("c", 1), False)
    await h.audio_delta("r2")
    await h.response_done("r2")
    assert h.arbiter.calls[-1] == ("on_reply_done", TurnId("c", 1), "completed")
    await h.drained("c1")
    assert h.closed("c1")[0]["state"] == "completed"


async def test_no_output_closes_as_failed():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.transcript_final()
    await h.response_started("r1")
    await h.response_done("r1")  # text-only reply, no audio
    assert h.machine.state is TurnState.COMMITTED
    await h.bus.emit("turn.no_output", turn_id=TurnId("c", 1))
    assert h.errors("c1")[0]["code"] == "upstream_rejected"
    assert h.closed("c1")[0]["state"] == "failed"


async def test_assistant_text_recorded_pending():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.transcript_final()
    await h.response_started("r1")
    await h.ev(EventType.RESPONSE_SENTENCE, response_id="r1", text="Hi there.", sentence_index=0)
    await h.ev(EventType.TRANSCRIPT_ASSISTANT, response_id="r1", text="Hi there. Bye.")
    req = h.arbiter.requests[0].request_id
    assert h.context.assistant == [(TurnId("c", 1), req, 0, "Hi there."), (TurnId("c", 1), req, 0, "Hi there. Bye.")]


async def test_upstream_error_fails_turn():
    from vak.core.types import ErrorClass, ErrorReason, ErrorScope
    from vak.upstream.base import UpstreamError

    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.ev(EventType.ERROR, error=UpstreamError(ErrorClass.TRANSIENT, ErrorReason.SESSION_LOST, ErrorScope.SESSION))
    assert h.errors("c1")[0]["code"] == "upstream_unavailable"
    assert h.closed("c1")[0]["state"] == "failed"


async def test_response_failed_status_fails_turn():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.transcript_final()
    await h.response_started("r1")
    await h.response_done("r1", status=ResponseStatus.FAILED)
    assert h.errors("c1")[0]["code"] == "upstream_rejected"


async def test_T_listen_max_forces_turn_end():
    h = make()
    await h.start("c1", ts=0)
    await h.audio(S(29_900))
    await h.clock.advance(TurnTimers().T_listen_max_A)
    ends = h.transport.of_type("turn.end")
    assert ends == [{"type": "turn.end", "turn_id": "c1", "source": "button", "ts_samples": S(29_920), "reason": "max_duration"}]
    assert h.machine.state is TurnState.COMMITTED
    assert h.trace.merged("c1")["watchdog_fired"] == "T_listen_max"


async def test_rule9_mode_switch_only_in_idle():
    h = make()
    await h.start("c1")
    with pytest.raises(IllegalTransition):
        await h.machine.set_mode(Mode.OPEN_MIC)
    await h.machine.handle_message({"type": "listen.open", "mode": "open_mic"})
    assert h.errors()[-1]["code"] == "protocol_error" and h.machine.mode is Mode.PTT
    await h.end("c1", ts=S(100))  # short press → idle
    await h.machine.set_mode(Mode.OPEN_MIC)
    assert h.machine.mode is Mode.OPEN_MIC


async def test_open_mic_requires_aec():
    with pytest.raises(ValueError):
        make(mode=Mode.OPEN_MIC, client_caps={"aec": "none"})


async def test_protocol_errors_do_not_disconnect():
    h = make()
    await h.machine.handle_message({"type": "turn.start", "source": "button"})
    await h.machine.handle_message({"type": "turn.start", "turn_id": "c1"})
    await h.machine.handle_message({"type": "totally.unknown"})
    assert [e["code"] for e in h.errors()] == ["protocol_error", "protocol_error"]
    assert h.machine.state is TurnState.IDLE


async def test_event_ack_is_idempotent():
    h = make()
    await h.machine.handle_message({"type": "event", "id": "ev-1", "name": "nfc"})
    await h.machine.handle_message({"type": "event", "id": "ev-1", "name": "nfc"})
    assert [a["status"] for a in h.transport.of_type("event.ack")] == ["accepted", "duplicate"]


async def test_close_session_hangup_cancels_open_turn():
    h = make()
    await h.start("c1")
    await h.machine.close_session("hangup")
    assert h.closed("c1")[0]["state"] == "cancelled"
    assert h.trace.merged("c1")["turn_terminal"] == "cancelled"


async def test_ptt_without_user_transcript_starts_T_respond_at_commit():
    from vak.upstream.capabilities import Capabilities, CapField

    caps = Capabilities.all_unknown("fake")
    caps.fields["cascade.user_transcript"] = CapField("none")
    caps.fields["response.client_initiated"] = CapField("yes")
    h = make(upstream_caps=caps)
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.clock.advance(TurnTimers().T_asr)
    assert h.closed("c1") == []
    await h.clock.advance(TurnTimers().T_respond - TurnTimers().T_asr)
    assert h.errors("c1")[0]["code"] == "upstream_timeout"
