"""TraceCollector: merge, unknown-key rejection, T0 conversion, derived fields,
invariants, finalize wait / null-fill, usage intake (spec 06 §2–§3, §10.1)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from vak.core.bus import Bus
from vak.core.clock import FakeClock
from vak.core.defaults import TraceParams
from vak.core.trace import MemoryExporter, TraceCollector, UnknownTraceField, validate
from vak.core.types import Profile, TurnId
from vak.upstream.base import Usage

T0 = 10_000


def make(clock: FakeClock | None = None, **kw: Any) -> tuple[TraceCollector, MemoryExporter, Bus, FakeClock]:
    clock = clock or FakeClock(start_ms=T0)
    bus = Bus()
    exp = MemoryExporter()
    params = kw.pop("params", TraceParams(finalize_wait_ms=5_000))
    col = TraceCollector(
        session_id="s_1",
        user_session_id="u_1",
        profile=kw.pop("profile", Profile.A),
        pipeline="s2s:mock",
        provider="mock",
        model="mock-rt",
        prompt_version_fn=lambda: "s2s/p@1+tools@1+state@1",
        gateway_build="gw-test",
        clock=clock,
        params=params,
        exporter=exp,
        bus=bus,
        device_id=kw.pop("device_id", "dev_1"),
        **kw,
    )
    return col, exp, bus, clock


def full_chat_fragments(col: TraceCollector, tid: TurnId) -> None:
    """Every M field for a chat turn, with ABSOLUTE times (T0 = 10_000)."""
    col.write(tid, {"t0_ms": T0, "turn_source": "button", "generation": 3, "speech_ms": 900,
                    "turn_kind": "chat", "user_text": "hi", "action": "none", "decision_layer": "model",
                    "anchors": {"primary": "release", "release_at_ms": T0, "audio_end_at_ms": T0 - 150,
                                "clock_quality": "gateway_rx"},
                    "session_seq": 1, "rebuild_reason": "none", "state_block_version": "1",
                    "state_update": "none", "reply_modality": "audio", "turns_in_upstream_session": 1,
                    "interrupted": False, "double_voice_count": 0, "stale_frames_dropped": 0,
                    "lease_violations": 0, "queue_truncated": False,
                    "drops_by_reason": {"stale": 0, "expired": 0, "duplicate": 0, "overflow": 0,
                                        "retry_exhausted": 0},
                    "flush_count": 0, "flush_ms_max": 0, "flush_timeouts": 0,
                    "receipt_timeouts": {"started": 0, "ended": 0}, "consumer_dropped_frames": 0,
                    "suppressed_frames": 0, "receipt_mode": "receipts", "uplink_seq_gaps": 0,
                    "uplink_jitter_ms": 5, "rtt_ms": 40, "downlink_lead_ms": 300,
                    "server_vad_segments_in_turn": 1, "dropped_short_press": False,
                    "turn_terminal": "completed", "turn_closed_by": "playback_ended",
                    "restart_attempts_in_turn": 0, "watchdog_fired": "none",
                    "device_events": [], "playback_receipts": [], "receipt_missing": 0,
                    "stt_echo_timeout": False,
                    "latency": {"sound_first_at_ms": T0 + 800, "content_first_at_ms": T0 + 800,
                                "content_kind": "answer"},
                    "usage": {"generated_audio_ms": 2000, "played_audio_ms": 2000, "input_audio_ms": 900,
                              "local_tts_chars": 0}})
    col.append(tid, "outputs", {"request_id": "req-1", "origin": "model", "kind": "speech",
                                "content_role": "answer", "generation": 3, "sentence_id": 0,
                                "sent_at_ms": T0 + 800, "played_ms": 2000, "status": "played"})


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def close_and_finalize(bus: Bus, col: TraceCollector, tid: TurnId, clock: FakeClock,
                             advance_ms: int = 0) -> None:
    await bus.emit("turn.closed", turn_id=tid, state="completed")
    await asyncio.sleep(0)
    if advance_ms:
        await clock.advance(advance_ms)
    await settle()


# --- write / append / incr --------------------------------------------------------


def test_fragment_merge_by_session_turn() -> None:
    col, _, _, _ = make()
    tid = TurnId("c", 1)
    col.write(tid, {"turn_kind": "chat", "anchors": {"primary": "release"}})
    col.write(tid, {"anchors": {"clock_quality": "synced"}, "latency": {"seg": {"llm_ms": 5}}})
    col.write(tid, {"latency": {"seg": {"vad_ms": 7}}})
    rec = col.peek(tid)
    assert rec is not None
    assert rec["anchors"] == {"primary": "release", "clock_quality": "synced"}
    assert rec["latency"]["seg"] == {"llm_ms": 5, "vad_ms": 7}
    assert rec["session_id"] == "s_1" and rec["turn_id"] == "c1" and rec["device_id"] == "dev_1"
    assert col.open_turns() == ["c1"]


def test_write_rejects_unknown_keys_top_and_nested() -> None:
    col, _, _, _ = make()
    tid = TurnId("c", 1)
    with pytest.raises(UnknownTraceField):
        col.write(tid, {"first_audio_ms": 1})
    with pytest.raises(UnknownTraceField):
        col.write(tid, {"anchors": {"t_rel": 0}})
    with pytest.raises(UnknownTraceField):
        col.write(tid, {"latency": {"seg": {"asr_ms": 1}}})
    with pytest.raises(UnknownTraceField):
        col.write(tid, {"usage": {"cost_usd": 0.1}})  # spec 06 §3.10: quantities only, no money
    with pytest.raises(UnknownTraceField):
        col.append(tid, "outputs", {"request_id": "r", "foo": 1})
    with pytest.raises(UnknownTraceField):
        col.append(tid, "playback", {})
    with pytest.raises(UnknownTraceField):
        col.incr(tid, "drops_by_reason.late")
    col.write(tid, {"slots": {"anything": 1}, "tool_calls": [{"call_id": "c", "args": {"x": 1}}]})  # free-form ok


def test_append_and_incr_dotted_paths() -> None:
    col, _, _, _ = make()
    tid = TurnId("s", 2)
    col.append(tid, "usage.responses", {"response_id": "r1"})
    col.incr(tid, "drops_by_reason.stale")
    col.incr(tid, "drops_by_reason.stale", 2)
    col.incr(tid, "double_voice_count")
    rec = col.peek(tid)
    assert rec is not None
    assert rec["usage"]["responses"] == [{"response_id": "r1"}]
    assert rec["drops_by_reason"] == {"stale": 3}
    assert rec["double_voice_count"] == 1
    assert col.now_ms() == T0


# --- finalize --------------------------------------------------------------------------


async def test_finalize_converts_to_t0_relative_and_sets_epoch() -> None:
    col, exp, bus, clock = make()
    tid = TurnId("c", 1)
    full_chat_fragments(col, tid)
    col.append(tid, "playback_receipts", {"sentence_id": 0, "request_id": "req-1", "started_at_ms": T0 + 870,
                                          "ended_at_ms": T0 + 2870, "interrupted": False, "played_ms": 2000})
    col.mark_complete(tid, "usage")
    col.mark_complete(tid, "receipt")
    await clock.advance(500)
    await close_and_finalize(bus, col, tid, clock)
    assert len(exp.records) == 1
    rec = exp.records[0]
    assert "t0_ms" not in rec
    assert rec["anchors"]["release_at_ms"] == 0 and rec["anchors"]["audio_end_at_ms"] == -150
    assert rec["latency"]["sound_first_at_ms"] == 800
    assert rec["outputs"][0]["sent_at_ms"] == 800
    assert rec["playback_receipts"][0]["started_at_ms"] == 870
    assert rec["t0_epoch_ms"] == clock.epoch_ms() - (clock.now_ms() - T0)
    assert rec["latency"]["ptt_tail_silence_ms"] == 150
    assert rec["stuck"] is False
    assert "invariant_violations" not in rec
    assert validate(rec) == []
    assert col.open_turns() == []


async def test_finalize_waits_for_usage_and_receipt_then_times_out_with_null_fill() -> None:
    col, exp, bus, clock = make(params=TraceParams(finalize_wait_ms=5_000))
    tid = TurnId("c", 1)
    full_chat_fragments(col, tid)
    await bus.emit("turn.closed", turn_id=tid)
    await asyncio.sleep(0)
    await clock.advance(4_000)
    assert exp.records == []  # still waiting (spec 06 §3.1: up to 5 s)
    # a late fragment inside the window still merges
    col.write(tid, {"latency": {"stt_final_at_ms": T0 + 300}})
    await clock.advance(1_500)
    await settle()
    assert len(exp.records) == 1
    rec = exp.records[0]
    assert rec["latency"]["stt_final_at_ms"] == 300
    assert rec["usage"]["usage_semantics"] is None and rec["usage"]["responses"] is None
    assert "usage.usage_semantics" in rec["missing_fields"] and "usage.responses" in rec["missing_fields"]
    assert validate(rec) == []


async def test_finalize_immediately_when_both_signals_arrived() -> None:
    col, exp, bus, clock = make()
    tid = TurnId("c", 1)
    full_chat_fragments(col, tid)
    col.mark_complete(tid, "receipt")
    col.on_usage(tid, Usage(response_id="r1", input_text_tokens=10, input_audio_tokens=5,
                            output_text_tokens=3, output_audio_tokens=4, cached_input_tokens=8,
                            usage_semantics="gross"), interrupted=False, response_status="completed")
    await close_and_finalize(bus, col, tid, clock)
    assert len(exp.records) == 1
    rec = exp.records[0]
    assert rec["usage"]["usage_semantics"] == "gross"
    assert rec["usage"]["responses"][0] == {
        "response_id": "r1", "response_status": "completed", "interrupted": False, "input_text_tokens": 10,
        "input_audio_tokens": 5, "output_text_tokens": 3, "output_audio_tokens": 4, "cached_input_tokens": 8,
    }
    assert rec["cached_input_tokens"] == 8 and rec["uncached_input_tokens"] == 7  # gross: 15 - 8
    assert validate(rec) == []


async def test_on_usage_for_interrupted_response_is_recorded() -> None:
    col, _, _, _ = make()
    tid = TurnId("c", 1)
    col.on_usage(tid, Usage(response_id="r1", generated_audio_ms=1200), interrupted=True, response_status="cancelled")
    col.on_usage(tid, Usage(response_id="r2", generated_audio_ms=800), interrupted=False, response_status="completed")
    rec = col.peek(tid)
    assert rec is not None
    assert [r["interrupted"] for r in rec["usage"]["responses"]] == [True, False]
    assert rec["usage"]["generated_audio_ms"] == 2000
    with pytest.raises(ValueError):
        col.mark_complete(tid, "everything")


async def test_derived_tool_fields_and_cue_gap() -> None:
    col, exp, bus, clock = make()
    tid = TurnId("c", 5)
    full_chat_fragments(col, tid)
    col.write(tid, {"turn_kind": "tool_verbatim", "speak_mode": "verbatim",
                    "latency": {"sound_first_at_ms": T0 + 830, "content_first_at_ms": T0 + 1120,
                                "content_first_ready_at_ms": T0 + 820, "fc_received_at_ms": T0 + 800},
                    "outputs": []})
    col.append(tid, "outputs", {"request_id": "req-c", "origin": "cue", "kind": "cue", "content_role": "filler",
                                "generation": 3, "sentence_id": 0, "sent_at_ms": T0 + 830, "played_ms": 280,
                                "status": "played"})
    col.append(tid, "outputs", {"request_id": "req-r", "origin": "tool_result", "kind": "speech",
                                "content_role": "answer", "generation": 3, "sentence_id": 1,
                                "sent_at_ms": T0 + 1120, "played_ms": 900, "status": "played"})
    col.append(tid, "tool_calls", {"call_id": "call_1", "name": "get_route", "args": {}, "presentation": "verbatim",
                                   "is_write": False, "latency_class": "ms", "tool_route": "A1", "generation": 3,
                                   "late_result_dropped": 0, "started_at_ms": T0 + 805, "done_at_ms": T0 + 815,
                                   "tool_exec_ms": 10, "reply_required": False, "closed_by": "result",
                                   "settled_at_ms": T0 + 816})
    col.mark_complete(tid, "usage")
    col.mark_complete(tid, "receipt")
    await close_and_finalize(bus, col, tid, clock)
    lat = exp.records[0]["latency"]
    assert lat["pre_tool_ms"] == 805
    assert lat["tool_done_at_ms"] == 815
    assert lat["post_tool_ms"] == 1120 - 815
    assert lat["queue_wait_ms"] == 300
    assert lat["cue_to_content_gap_ms"] == 1120 - (830 + 280)
    assert lat["content_kind"] == "answer"
    assert validate(exp.records[0]) == []


async def test_invariant_violations_logged_not_fatal(caplog: pytest.LogCaptureFixture) -> None:
    col, exp, bus, clock = make()
    tid = TurnId("c", 7)
    full_chat_fragments(col, tid)
    col.write(tid, {"latency": {"sound_first_at_ms": T0 + 900, "content_first_at_ms": T0 + 800}})
    col.append(tid, "tool_calls", {"call_id": "hang", "name": "slow", "args": {}, "presentation": "model",
                                   "is_write": False, "latency_class": "seconds", "tool_route": "A0",
                                   "generation": 3, "late_result_dropped": 0, "started_at_ms": T0 + 500,
                                   "done_at_ms": None, "tool_exec_ms": None, "reply_required": True})
    col.mark_complete(tid, "usage")
    col.mark_complete(tid, "receipt")
    with caplog.at_level("WARNING"):
        await close_and_finalize(bus, col, tid, clock)
    rec = exp.records[0]
    v = rec["invariant_violations"]
    assert any("content_first_at_ms(800) < sound_first_at_ms(900)" in x for x in v)
    assert any(x.startswith("hanging_calls: hang") for x in v)
    assert rec["tool_calls"][0]["closed_by"] is None and "tool_calls.0.closed_by" in rec["missing_fields"]
    assert "invariant" in caplog.text or "hanging" in caplog.text


async def test_missing_t0_falls_back_to_first_write_and_flags() -> None:
    col, exp, bus, clock = make()
    tid = TurnId("c", 8)
    full_chat_fragments(col, tid)
    col.write(tid, {"t0_ms": None})
    col.peek(tid)
    col._turns["c8"].record.pop("t0_ms")
    col.mark_complete(tid, "usage")
    col.mark_complete(tid, "receipt")
    await close_and_finalize(bus, col, tid, clock)
    assert any("t0_ms missing" in v for v in exp.records[0]["invariant_violations"])
    assert exp.records[0]["anchors"]["release_at_ms"] == 0  # first write was at T0 anyway


async def test_stuck_when_no_sound_beyond_threshold_without_filler() -> None:
    col, exp, bus, clock = make(params=TraceParams(finalize_wait_ms=0, stuck_silence_ms=2_500))
    tid = TurnId("c", 9)
    full_chat_fragments(col, tid)
    col.write(tid, {"latency": {"sound_first_at_ms": None, "content_first_at_ms": None, "content_kind": "none"},
                    "outputs": [], "turn_terminal": "completed"})
    await clock.advance(3_000)  # 3 s of silence after T0, then the turn closes
    await close_and_finalize(bus, col, tid, clock, advance_ms=10)
    assert exp.records[0]["stuck"] is True


async def test_not_stuck_when_filler_played_or_discarded() -> None:
    col, exp, bus, clock = make(params=TraceParams(finalize_wait_ms=0))
    tid = TurnId("c", 10)
    full_chat_fragments(col, tid)
    col.write(tid, {"latency": {"sound_first_at_ms": T0 + 2_800, "content_first_at_ms": T0 + 4_000}})
    col.append(tid, "outputs", {"request_id": "req-f", "origin": "cue", "kind": "cue", "content_role": "filler",
                                "generation": 3, "sentence_id": 0, "sent_at_ms": T0 + 2_800, "played_ms": 300,
                                "status": "played"})
    await close_and_finalize(bus, col, tid, clock, advance_ms=10)
    assert exp.records[0]["stuck"] is False
    tid2 = TurnId("c", 11)
    full_chat_fragments(col, tid2)
    col.write(tid2, {"turn_terminal": "discarded", "turn_closed_by": "short_press",
                     "latency": {"sound_first_at_ms": None, "content_first_at_ms": None, "content_kind": "none"}})
    await clock.advance(5_000)
    await close_and_finalize(bus, col, tid2, clock, advance_ms=10)
    assert exp.records[1]["stuck"] is False


async def test_stuck_when_terminal_missing_on_session_close() -> None:
    col, exp, bus, clock = make()
    tid = TurnId("c", 12)
    full_chat_fragments(col, tid)
    col.write(tid, {"turn_terminal": None})
    await col.close()
    assert exp.records[0]["stuck"] is True
    assert exp.records[0]["turn_terminal"] is None


async def test_profile_b_null_fills_m_b_fields() -> None:
    col, exp, bus, clock = make(profile="B", params=TraceParams(finalize_wait_ms=0), user_id_hash="h1", device_id=None)
    tid = TurnId("s", 3)
    full_chat_fragments(col, tid)
    await close_and_finalize(bus, col, tid, clock, advance_ms=10)
    rec = exp.records[0]
    assert rec["profile"] == "B" and rec["user_id_hash"] == "h1" and "device_id" not in rec
    assert rec["route"] is None and "route" in rec["missing_fields"]
    assert rec["latency"]["ui_feedback_first_at_ms"] is None
    assert validate(rec) == []


async def test_turn_closed_payload_with_str_turn_id() -> None:
    col, exp, bus, clock = make(params=TraceParams(finalize_wait_ms=0))
    full_chat_fragments(col, TurnId("c", 1))
    await bus.emit("turn.closed", turn_id="c1")
    await settle()
    assert len(exp.records) == 1 and exp.records[0]["turn_id"] == "c1"
