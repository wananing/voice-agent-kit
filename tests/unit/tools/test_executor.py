"""ToolLayer: lifecycle, six terminal states, interrupt matrix, duplicates, cue, A3,
route fallback, before_commit, rebuild, late results, delegation (spec 03 §4–§7)."""

from __future__ import annotations

import asyncio
import random

import pytest

from tests.unit.tools.conftest import Harness, build
from tests.unit.tools.fakes import ScriptedDelegate, settle, tool_call_event
from vak.core.tools.delegate import DelegateMessage
from vak.core.tools.executor import PHRASES, TRANSITIONS, CallEvent
from vak.core.tools.gates import register_checker
from vak.core.types import CallState, ClosedBy, IllegalTransition, InterruptPhase, ToolRoute, TurnId

C1 = TurnId.parse("c1")


# --------------------------------------------------------------------- A1 / silent / A0 basics

async def test_a1_verbatim_speaks_returns_immediately_no_regeneration(h: Harness):
    call = await h.call("call-1", "get_status")
    assert call.settled and call.closed_by is ClosedBy.RESULT and call.state is CallState.CLOSED
    assert call.route is ToolRoute.A1 and call.reply_required is False and call.output_sent
    speech = h.arbiter.by_origin("tool_result")
    assert len(speech) == 1 and speech[0].kind.value == "speech" and speech[0].generation == 1
    assert speech[0].content.text == "已经完成 三 个，还剩 十二 个。"
    body, reply = h.sent("call-1")[0]
    assert reply is False
    assert body["status"] == "ok" and body["spoken_to_user"] == "已经完成 三 个，还剩 十二 个。"
    assert body["delivery"] == "in_progress" and body["note"] == "已经告诉用户了，不要重复"
    assert body["summary"] == "报了进度"
    # cue enqueued on function call, withdrawn because the result was ready first
    cue = h.arbiter.by_origin("cue")
    assert len(cue) == 1 and cue[0].kind.value == "cue" and cue[0].content.duration_ms == 200
    assert h.arbiter.flushes == [("cue", C1, None)]
    assert h.arbiter.submitted.index(cue[0]) < h.arbiter.submitted.index(speech[0])
    # trace: per-call record + turn fields + latency
    rec = h.trace.last_record("call-1")
    for k in ("call_id", "name", "tool_route", "closed_by", "reply_required", "generation", "gate_result", "settled_at_ms"):
        assert k in rec
    assert rec["tool_route"] == "A1" and rec["closed_by"] == "result" and rec["gate_result"] == "n_a"
    assert rec["late_result_dropped"] == 0 and rec["presentation"] == "verbatim" and rec["is_write"] is False
    assert h.trace.field(C1, "turn_kind") == "tool_verbatim" and h.trace.field(C1, "speak_mode") == "verbatim"
    assert h.trace.field(C1, "write_gate") == "n_a" and h.trace.field(C1, "action") == "get_status"
    assert h.trace.field(C1, "decision_layer") == "model"
    lat = h.trace.latency(C1)
    assert {"fc_received_at_ms", "pre_tool_ms", "tool_done_at_ms", "output_sent_at_ms"} <= set(lat)
    assert h.context.tool_calls[0].call_id == "call-1" and h.context.tool_outputs[-1].call_id == "call-1"
    assert not h.layer.has_open_calls(C1)


async def test_a1_business_failure_speaks_alternatives_max_two(h: Harness):
    h.knobs["ok"] = False
    call = await h.call("call-1", "get_status")
    assert call.closed_by is ClosedBy.RESULT and call.result.ok is False
    body, _ = h.sent("call-1")[0]
    assert body["status"] == "failed" and body["reason_code"] == "not_supported" and len(body["alternatives"]) == 2
    text = h.arbiter.by_origin("tool_result")[0].content.text
    assert text == "现在查不了。可以稍后再试，或者换个问法，你想要哪个？"


async def test_silent_close_no_speech(h: Harness):
    call = await h.call("call-1", "remember_preference", {"key": "k", "value": "v"})
    assert call.closed_by is ClosedBy.RESULT and call.route is ToolRoute.SILENT
    assert h.arbiter.submitted == []
    body, reply = h.sent("call-1")[0]
    assert reply is False and body["spoken_to_user"] == "" and body["note"] == "已记录，不需要告诉用户"
    assert h.trace.field(C1, "speak_mode") == "none"


async def test_silent_close_none_falls_back_to_a0_with_reason():
    h = build(silent_close="none")
    call = await h.call("call-1", "get_status")
    assert call.route is ToolRoute.A0 and call.route_fallback_reason == "tools.silent_close=none"
    body, reply = h.sent("call-1")[0]
    assert reply is True and body["spoken_to_user"] == "" and "delivery" not in body
    assert h.arbiter.by_origin("tool_result") == []  # the model speaks, not us
    assert h.arbiter.by_origin("cue")  # cue still enqueued
    rec = h.trace.last_record("call-1")
    assert rec["tool_route"] == "A0" and rec["route_fallback_reason"] == "tools.silent_close=none" and rec["reply_required"] is True
    assert h.trace.field(C1, "speak_mode") == "model"
    c2 = await h.call("call-2", "remember_preference", {"key": "k", "value": "v"})
    assert c2.route is ToolRoute.SILENT  # silent route is not a fallback candidate


async def test_screen_in_profile_b_shows_card(monkeypatch):
    h = build(profile="B", phase="propose")
    call = await h.call("call-1", "show_options", {"query": "q"})
    assert call.route is ToolRoute.A1
    body, _ = h.sent("call-1")[0]
    assert body["shown_to_user"] == "c-17" and body["spoken_to_user"] == "给你列了三个方案，看屏幕。"
    assert h.trace.field(C1, "speak_mode") == "screen"


async def test_screen_profile_a_override_is_verbatim():
    h = build(profile="A", phase="propose")
    await h.call("call-1", "show_options")
    assert h.arbiter.by_origin("tool_result")[0].content.text == "有三个方案，前两个是甲和乙。"


# --------------------------------------------------------------------- lifecycle table

def test_transition_table_rejects_illegal():
    assert TRANSITIONS[(CallState.RECEIVED, CallEvent.START)] is CallState.RUNNING
    assert (CallState.CLOSED, CallEvent.RESULT) not in TRANSITIONS
    assert (CallState.RECEIVED, CallEvent.RESULT) not in TRANSITIONS


async def test_illegal_transition_raises(h: Harness):
    await h.call("call-1", "get_status")
    entry = h.layer._entries["call-1"]
    with pytest.raises(IllegalTransition):
        h.layer._transition(entry, CallEvent.START)


async def test_unknown_tool_closes_not_available(h: Harness):
    call = await h.call("call-1", "nope")
    assert call.closed_by is ClosedBy.RESULT and call.result.reason_code == "not_available_in_phase"
    assert h.sent("call-1")[0][1] is False


async def test_phase_invisible_closes_result_ok_false_local_phrase():
    h = build(phase="collect")
    call = await h.call("call-1", "submit_request", {"option_id": "o1"})
    assert call.closed_by is ClosedBy.RESULT and call.result.ok is False
    assert call.result.reason_code == "not_available_in_phase" and h.knobs["backend_calls"] == 0
    body, reply = h.sent("call-1")[0]
    assert body["status"] == "failed" and reply is False
    notice = h.arbiter.by_origin("system_notice")
    assert notice and notice[0].content.text == PHRASES["not_available_in_phase"]
    assert h.trace.last_record("call-1")["gate_result"] == "not_available_in_phase"
    assert h.arbiter.flushes == [("cue", C1, None)]


async def test_visible_after_unlocks_after_prerequisite_ok():
    h = build(profile="B", phase="confirm")
    h.layer.on_user_final(C1, "确认提交", action="confirm_submit")
    c = await h.call("call-1", "submit_request", {"option_id": "o1"})
    assert c.result.reason_code == "not_available_in_phase"
    h.phase[0] = "propose"
    await h.call("call-2", "show_options", turn="c2")
    h.phase[0] = "confirm"
    h.layer.on_user_final(TurnId.parse("c3"), "确认提交", action="confirm_submit")
    c3 = await h.call("call-3", "submit_request", {"option_id": "o1"}, turn="c3")
    assert c3.closed_by is ClosedBy.PROGRESS


# --------------------------------------------------------------------- gates and writes

async def test_gate_rejection_each_reason(h: Harness):
    h.layer.on_user_final(C1, "打开灯", action="chat")
    c = await h.call("call-1", "set_flag", {"key": "灯", "value": "on"})
    assert c.result.reason_code == "intent_mismatch" and c.gate_result == "blocked"
    assert h.trace.field(C1, "write_gate") == "blocked" and h.trace.field(C1, "decision_layer") == "gate"
    assert h.trace.field(C1, "turn_kind") == "tool_write" and h.trace.last_record("call-1")["gate_result"] == "intent_mismatch"
    h.layer.on_user_final(TurnId.parse("c2"), "怎么设", action="set_flag")
    c = await h.call("call-2", "set_flag", {"key": "灯", "value": "on"}, turn="c2")
    assert c.result.reason_code == "utterance_not_explicit"
    assert h.arbiter.by_origin("system_notice")[-1].content.text == PHRASES["utterance_not_explicit"]

    async def busy(args, ctx):
        return {"ok": False, "alternatives": [{"label": "等一下", "action": "wait"}], "message": "有进行中的操作"}

    register_checker("flag_ok", busy)
    h.layer.on_user_final(TurnId.parse("c3"), "打开灯", action="set_flag")
    c = await h.call("call-3", "set_flag", {"key": "灯", "value": "on"}, turn="c3")
    assert c.result.reason_code == "backend_state"
    body, _ = h.sent("call-3")[0]
    assert body["alternatives"] == [{"label": "等一下", "action": "wait"}]
    assert h.arbiter.by_origin("system_notice")[-1].content.text == "有进行中的操作。可以等一下，要吗？"
    assert h.knobs["backend_calls"] == 0


async def test_write_a1_row2_executes_after_gates(h: Harness):
    h.layer.on_user_final(C1, "打开灯", action="set_flag")
    c = await h.call("call-1", "set_flag", {"key": "灯", "value": "on"})
    assert c.closed_by is ClosedBy.RESULT and c.gate_result == "passed" and h.knobs["backend_calls"] == 1
    assert h.trace.field(C1, "write_gate") == "passed" and h.trace.last_record("call-1")["gate_result"] == "pass"
    assert h.arbiter.by_origin("tool_result")[0].content.text == "已经把灯设为on。"


async def test_write_recheck_generation_cancels_before_backend(h: Harness):
    """Rule 5: generation changed between gate pass and the backend call → cancelled, backend untouched."""
    gate_hit = asyncio.Event()
    release = asyncio.Event()

    async def slow_ok(args, ctx):
        gate_hit.set()
        await release.wait()
        return True

    register_checker("flag_ok", slow_ok)
    h.layer.on_user_final(C1, "打开灯", action="set_flag")
    await h.layer.on_tool_call(tool_call_event("call-1", "set_flag", {"key": "k", "value": "v"}, C1))
    await gate_hit.wait()
    h.arbiter.generation += 1  # generation moved without a barge-in callback (new turn)
    release.set()
    await settle()
    c = h.layer.calls["call-1"]
    assert c.closed_by is ClosedBy.CANCELLED and h.knobs["backend_calls"] == 0
    assert h.sent("call-1") == [({"cancelled": True, "reason": "interrupt"}, False)]


async def test_write_recheck_backend_state_change_cancels(h: Harness):
    calls = {"n": 0}

    async def flips(args, ctx):
        calls["n"] += 1
        return calls["n"] == 1  # passes the chain, fails the re-check

    register_checker("flag_ok", flips)
    h.layer.on_user_final(C1, "打开灯", action="set_flag")
    c = await h.call("call-1", "set_flag", {"key": "k", "value": "v"})
    assert c.closed_by is ClosedBy.CANCELLED and h.knobs["backend_calls"] == 0 and calls["n"] == 2


async def test_first_write_per_turn_only_rule6(h: Harness):
    h.layer.on_user_final(C1, "打开灯", action="set_flag")
    c1 = await h.call("call-1", "set_flag", {"key": "a", "value": "1"})
    c2 = await h.call("call-2", "set_flag", {"key": "b", "value": "2"})  # different key: still a second write
    assert c1.closed_by is ClosedBy.RESULT and c2.closed_by is ClosedBy.DUPLICATE
    assert h.sent("call-2") == [({"duplicate": True, "message": "同一请求已在处理或已完成，不要再次调用"}, False)]
    assert h.knobs["backend_calls"] == 1
    h.layer.on_user_final(TurnId.parse("c2"), "打开灯", action="set_flag")
    c3 = await h.call("call-3", "set_flag", {"key": "c", "value": "3"}, turn="c2")
    assert c3.closed_by is ClosedBy.RESULT


# --------------------------------------------------------------------- duplicates

async def test_duplicate_replace_supersedes_in_flight(h: Harness):
    h.knobs["delay_ms"] = 500
    c1 = await h.call("call-1", "get_status")
    assert not c1.settled
    c2 = await h.call("call-2", "get_status")
    assert c1.closed_by is ClosedBy.SUPERSEDED and h.sent("call-1") == [({"superseded": True}, False)]
    assert h.layer._entries["call-1"].exec_task.cancelled() or h.layer._entries["call-1"].exec_task.done()
    await h.clock.advance(600)
    assert c2.closed_by is ClosedBy.RESULT
    assert h.trace.last_record("call-1")["closed_by"] == "superseded"


async def test_duplicate_reject_in_flight_scope(h: Harness):
    h.knobs["delay_ms"] = 1000
    c1 = await h.call("call-1", "long_job", {"label": "x"})
    c2 = await h.call("call-2", "long_job", {"label": "x"}, turn="c2")  # scope in_flight crosses turns
    assert c1.closed_by is ClosedBy.PROGRESS and c2.closed_by is ClosedBy.DUPLICATE
    assert h.sent("call-2")[0][0]["duplicate"] is True and h.sent("call-2")[0][1] is False
    await h.clock.advance(1500)
    c3 = await h.call("call-3", "long_job", {"label": "x"}, turn="c3")  # nothing in flight anymore
    assert c3.closed_by is ClosedBy.PROGRESS


async def test_duplicate_confirm_asks_model(h: Harness):
    c1 = await h.call("call-1", "get_time")
    c2 = await h.call("call-2", "get_time")
    assert c1.closed_by is ClosedBy.RESULT and c2.closed_by is ClosedBy.DUPLICATE
    body, reply = h.sent("call-2")[0]
    assert reply is True and body["duplicate"] is True and body["needs_confirmation"] is True and body["message"]
    c3 = await h.call("call-3", "get_time", turn="c2")  # scope turn: a new turn is not a duplicate
    assert c3.closed_by is ClosedBy.RESULT


async def test_duplicate_allow_runs_twice(h: Harness):
    h.layer.on_user_final(C1, "x")
    c1 = await h.call("call-1", "remember_preference", {"key": "k", "value": "v"})
    c2 = await h.call("call-2", "remember_preference", {"key": "k", "value": "v"})
    assert c1.closed_by is ClosedBy.RESULT and c2.closed_by is ClosedBy.RESULT


# --------------------------------------------------------------------- watchdog / timeouts

async def test_timeout_closes_cancelled_with_local_fallback(h: Harness):
    h.knobs["delay_ms"] = 5000
    c = await h.call("call-1", "get_status")
    await h.clock.advance(999)
    assert not c.settled and h.layer.has_open_calls(C1)
    await h.clock.advance(1)
    await settle()
    assert c.closed_by is ClosedBy.CANCELLED and c.settled
    assert h.sent("call-1") == [({"cancelled": True, "reason": "timeout"}, False)]
    notice = h.arbiter.by_origin("system_notice")
    assert notice[-1].content.text == PHRASES["timeout"] and notice[-1].generation == 1
    assert h.arbiter.flushes == [("cue", C1, None)]
    assert h.trace.last_record("call-1")["closed_by"] == "cancelled" and h.trace.field(C1, "speak_mode") == "none"
    # late result after the watchdog fired is refused and counted
    await h.clock.advance(5000)
    assert c.late_result_dropped == 0  # exec task was cancelled, nothing arrived
    assert h.sent("call-1")[-1][0]["reason"] == "timeout"


async def test_every_received_call_closes_within_timeout_plus_margin(h: Harness):
    """Spec 03 §4.1 invariant 1 over random scripts: hanging calls = 0."""
    rng = random.Random(7)
    names = ["get_status", "remember_preference", "get_time", "explain_topic", "long_job", "set_flag", "submit_request"]
    h.layer.on_user_final(C1, "确认提交打开", action="confirm_submit")
    h.phase[0] = "confirm"
    h.layer._completed.add("show_options")
    for i in range(40):
        name = rng.choice(names)
        h.knobs["delay_ms"] = rng.choice([0, 10, 500, 3000, 20000])
        turn = f"c{rng.randint(1, 3)}"
        if rng.random() < 0.2:
            h.arbiter.generation += 1
            await h.layer.on_barge_in(h.arbiter.generation)
        await h.layer.on_tool_call(tool_call_event(
            f"call-{i}", name, {"key": "k", "value": "v", "option_id": "o", "target": "t"}, TurnId.parse(turn)))
        await h.clock.advance(rng.choice([0, 5, 200]))
    longest = max(s.timeout_ms for s in h.registry) + h.params.watchdog_margin_ms
    await h.clock.advance(longest)
    await settle()
    hanging = [c.call_id for c in h.layer.calls.values() if not c.settled]
    assert hanging == []
    for c in h.layer.calls.values():
        assert c.closed_by is not None and c.closed_at_ms - c.received_at_ms <= longest
        assert h.trace.last_record(c.call_id)["closed_by"] == c.closed_by.value


# --------------------------------------------------------------------- A3

async def test_a3_progress_close_ack_then_result_narration(h: Harness):
    h.knobs["delay_ms"] = 3000
    h.phase[0] = "confirm"
    h.layer._completed.add("show_options")
    h.layer.on_user_final(C1, "确认提交", action="confirm_submit")
    c = await h.call("call-1", "submit_request", {"option_id": "o2"})
    assert c.closed_by is ClosedBy.PROGRESS and c.settled and not h.layer.has_open_calls(C1)
    body, reply = h.sent("call-1")[0]
    assert body == {"status": "in_progress", "summary": "已提交，正在处理", "spoken_to_user": "好，已经提交o二，正在处理。",
                    "note": "任务仍在进行，不要编造结果、不要再次调用"} and reply is False
    ack = h.arbiter.by_origin("tool_result")[0]
    assert ack.kind.value == "speech" and ack.content.text == "好，已经提交o二，正在处理。" and ack.generation == 1
    assert h.arbiter.flushes == [("cue", C1, None)]
    assert h.trace.last_record("call-1")["closed_by"] == "progress"
    await h.clock.advance(3000)
    await settle()
    res = h.arbiter.by_origin("task_result")
    assert len(res) == 1 and res[0].kind.value == "narration" and res[0].generation is None
    assert res[0].content.text == "提交完成，提交了o二。" and res[0].supersedes == "call-1:progress"
    assert res[0].on_interrupt == "drop" and res[0].max_retries == 8
    assert len(h.sent("call-1")) == 1  # final result never hangs on the original call_id
    assert c.closed_by is ClosedBy.PROGRESS and h.context.tool_outputs[-1].result.summary == "提交了o2"
    assert h.trace.field(C1, "turn_kind") == "tool_write"


async def test_a3_progress_throttle_first_60s_interval_60s(h: Harness):
    h.knobs["progress"] = ["十分之一", "一半", "快好了", "差一点"]
    h.knobs["progress_gap_ms"] = 25_000  # at 25, 50, 75, 100 s
    c = await h.call("call-1", "long_job", {"label": "x"})
    assert c.closed_by is ClosedBy.PROGRESS
    await h.clock.advance(100_000)
    await settle()
    prog = h.arbiter.by_origin("task_progress")
    assert [p.content.text for p in prog] == ["快好了"]  # 25 s, 50 s < 60 s; 75 s ok; 100 s < 75 + 60
    assert prog[0].supersedes == "call-1:progress" and prog[0].dedupe_key == "call-1:progress" and prog[0].max_retries == 0
    assert h.arbiter.by_origin("task_result")[0].content.text == "任务做完了，x完成。"


async def test_a3_background_timeout_narrates_failure(h: Harness):
    h.knobs["delay_ms"] = 10_000_000
    c = await h.call("call-1", "long_job", {"label": "x"})
    await h.clock.advance(600_000)
    await settle()
    assert c.closed_by is ClosedBy.PROGRESS
    assert h.arbiter.by_origin("task_result")[-1].content.text == PHRASES["task_timeout"]


# --------------------------------------------------------------------- interrupt matrix (spec 03 §5.1)

async def test_row1_received_gate_checking_cancelled_backend_untouched(h: Harness):
    gate_hit, release = asyncio.Event(), asyncio.Event()

    async def slow(args, ctx):
        gate_hit.set()
        await release.wait()
        return True

    register_checker("flag_ok", slow)
    h.layer.on_user_final(C1, "打开灯", action="set_flag")
    await h.layer.on_tool_call(tool_call_event("call-1", "set_flag", {"key": "k", "value": "v"}, C1))
    await gate_hit.wait()
    await h.barge_in()
    c = h.layer.calls["call-1"]
    assert c.closed_by is ClosedBy.CANCELLED and c.interrupt_phase is InterruptPhase.RECEIVED
    assert h.sent("call-1") == [({"cancelled": True, "reason": "interrupt"}, False)]
    release.set()
    await settle()
    assert h.knobs["backend_calls"] == 0 and len(h.sent("call-1")) == 1
    assert h.trace.last_record("call-1")["interrupt_phase"] == "received"


async def test_row1_received_no_send_when_silent_close_none():
    h = build(silent_close="none")
    h.knobs["delay_ms"] = 100
    h.layer.on_user_final(C1, "打开灯", action="set_flag")
    c = await h.call("call-1", "get_status")
    await h.barge_in()
    assert c.closed_by is ClosedBy.CANCELLED and h.sent("call-1") == [] and c.output_sent is False


async def test_row2_running_read_cancel_does_not_wait(h: Harness):
    h.knobs["delay_ms"] = 500
    c = await h.call("call-1", "get_status")
    await h.clock.advance(100)
    await h.barge_in()
    assert c.closed_by is ClosedBy.CANCELLED and c.interrupt_phase is InterruptPhase.RUNNING
    assert h.sent("call-1") == [({"cancelled": True, "reason": "interrupt"}, False)]
    await h.clock.advance(1000)
    await settle()
    assert len(h.sent("call-1")) == 1 and h.arbiter.by_origin("tool_result") == []


async def test_row2_running_write_waits_then_interrupted_and_notifies(h: Harness):
    h.knobs["delay_ms"] = 500
    h.layer.on_user_final(C1, "打开灯", action="set_flag")
    c = await h.call("call-1", "set_flag", {"key": "灯", "value": "on"})
    await h.clock.advance(100)
    await h.barge_in()
    assert not c.settled and c.interrupt_phase is InterruptPhase.RUNNING  # backend request not withdrawn
    await h.clock.advance(400)
    await settle()
    assert c.closed_by is ClosedBy.INTERRUPTED and h.knobs["backend_calls"] == 1
    body, reply = h.sent("call-1")[0]
    assert body["not_spoken"] is True and body["status"] == "ok" and body["spoken_to_user"] == "" and reply is False
    # finish_then_notify: one narration at the next idle window, not replayed if interrupted
    n = h.arbiter.by_origin("task_result")
    assert len(n) == 1 and n[0].kind.value == "narration" and n[0].content.text == "已经把灯设为on。" and n[0].on_interrupt == "drop"
    assert h.arbiter.by_origin("tool_result") == []
    assert h.trace.last_record("call-1")["interrupt_phase"] == "running"


async def test_row3_done_not_sent_silent_close_returns_not_spoken(h: Harness):
    h.knobs["delay_ms"] = 300
    c = await h.call("call-1", "get_status")
    h.arbiter.generation += 1  # generation moved; the tool completes afterwards
    await h.clock.advance(300)
    await settle()
    assert c.closed_by is ClosedBy.INTERRUPTED and c.interrupt_phase is InterruptPhase.DONE_NOT_SENT
    body, reply = h.sent("call-1")[0]
    assert body["not_spoken"] is True and body["summary"] == "报了进度" and reply is False
    assert h.arbiter.by_origin("tool_result") == []


async def test_row3_done_not_sent_without_silent_close_is_local_only():
    h = build(silent_close="none")
    h.knobs["delay_ms"] = 300
    c = await h.call("call-1", "get_status")
    h.arbiter.generation += 1
    await h.clock.advance(300)
    await settle()
    assert c.closed_by is ClosedBy.INTERRUPTED and c.output_sent is False and h.sent("call-1") == []
    assert h.trace.last_record("call-1")["closed_by"] == "interrupted"


async def test_row4_sent_speaking_supplements_interrupted(h: Harness):
    c = await h.call("call-1", "get_status")
    assert c.closed_by is ClosedBy.RESULT
    h.context.presented["c1"] = "已经完成 三 个"
    await h.barge_in()
    assert c.closed_by is ClosedBy.INTERRUPTED and c.interrupt_phase is InterruptPhase.SENT_SPEAKING
    assert h.sent("call-1")[-1] == ({"interrupted": True}, False)
    recs = h.trace.records("call-1")
    assert recs[-1]["closed_by"] == "interrupted" and recs[-1]["interrupt_phase"] == "sent_speaking"
    # a second barge-in does nothing more
    await h.barge_in()
    assert len(h.sent("call-1")) == 2


async def test_row4_after_turn_closed_nothing_happens(h: Harness):
    c = await h.call("call-1", "get_status")
    await h.layer.on_turn_closed(C1)
    await h.barge_in()
    assert c.closed_by is ClosedBy.RESULT and len(h.sent("call-1")) == 1


async def test_row4_a0_model_generating_supplemented():
    h = build(silent_close="flag")
    h.layer.registry.get("get_status").route = ToolRoute.A0
    c = await h.call("call-1", "get_status")
    assert c.route is ToolRoute.A0 and h.sent("call-1")[0][1] is True
    await h.barge_in()
    assert c.closed_by is ClosedBy.INTERRUPTED and h.sent("call-1")[-1] == ({"interrupted": True}, False)


async def test_row5_progress_closed_continues_and_narrates_later(h: Harness):
    h.knobs["delay_ms"] = 2000
    h.phase[0] = "confirm"
    h.layer._completed.add("show_options")
    h.layer.on_user_final(C1, "确认提交", action="confirm_submit")
    c = await h.call("call-1", "submit_request", {"option_id": "o1"})
    await h.clock.advance(500)
    await h.barge_in()
    assert c.closed_by is ClosedBy.PROGRESS and c.interrupt_phase is InterruptPhase.PROGRESS_CLOSED
    await h.clock.advance(1500)
    await settle()
    assert c.closed_by is ClosedBy.PROGRESS
    assert h.arbiter.by_origin("task_result")[0].content.text == "提交完成，提交了o一。"
    assert len(h.sent("call-1")) == 1
    assert h.trace.last_record("call-1")["interrupt_phase"] == "progress_closed"


async def test_row6_narration_playing_counts_as_delivered(h: Harness):
    h.knobs["delay_ms"] = 100
    c = await h.call("call-1", "long_job", {"label": "x"})
    await h.clock.advance(100)
    await settle()
    assert len(h.arbiter.by_origin("task_result")) == 1
    await h.barge_in()
    assert c.closed_by is ClosedBy.PROGRESS and c.interrupt_phase is InterruptPhase.NARRATION
    assert len(h.arbiter.by_origin("task_result")) == 1 and len(h.sent("call-1")) == 1


async def test_row7_delegating_nothing_spoken_cancelled():
    d = ScriptedDelegate([DelegateMessage.speak("第一句。"), DelegateMessage.end("done")], gate_each=True)
    h = build(delegate=d)
    c = await h.call("call-1", "explain_topic", {"target": "t"})
    await d.started.wait()
    assert not c.settled and h.layer.has_open_calls(C1)
    await h.barge_in()
    assert c.closed_by is ClosedBy.CANCELLED and c.interrupt_phase is InterruptPhase.DELEGATING
    assert h.sent("call-1") == [({"cancelled": True, "reason": "interrupt"}, False)]
    assert d.cancelled == [d.requests[0].delegation_id]
    d.step.set()
    await settle()
    assert h.arbiter.by_origin("delegated_result") == []  # unspoken text dropped


async def test_row7_delegating_partially_spoken_interrupted():
    d = ScriptedDelegate([DelegateMessage.speak("第一句。"), DelegateMessage.speak("第二句。"), DelegateMessage.end("done")],
                         gate_each=True)
    h = build(delegate=d)
    c = await h.call("call-1", "explain_topic", {"target": "t"})
    await d.started.wait()
    d.step.set()
    await settle()
    spoken = h.arbiter.by_origin("delegated_result")
    assert len(spoken) == 1 and spoken[0].generation == 1 and spoken[0].content.text == "第一句。"
    h.context.presented["c1"] = "第一句。"
    await h.barge_in()
    assert c.closed_by is ClosedBy.INTERRUPTED
    body, reply = h.sent("call-1")[0]
    assert body["interrupted"] is True and body["spoken_to_user"] == "第一句。" and reply is False
    d.step.set()
    await settle()
    assert len(h.arbiter.by_origin("delegated_result")) == 1


# --- racy rows repeated with random timing (docs/contributing.md §4) ---------

@pytest.mark.parametrize("seed", range(20))
async def test_rows_2_3_4_random_timing_always_settles(seed):
    rng = random.Random(seed)
    h = build()
    delay = rng.choice([50, 100, 300, 600])
    h.knobs["delay_ms"] = delay
    c = await h.call("call-1", "get_status")
    when = rng.randint(0, 700)
    await h.clock.advance(when)
    await h.barge_in()
    await h.clock.advance(1000)
    await settle()
    assert c.settled and c.closed_by in (ClosedBy.CANCELLED, ClosedBy.INTERRUPTED)
    if when < delay:
        assert c.closed_by is ClosedBy.CANCELLED and c.interrupt_phase is InterruptPhase.RUNNING
        assert h.arbiter.by_origin("tool_result") == []
    else:
        assert c.closed_by is ClosedBy.INTERRUPTED and c.interrupt_phase is InterruptPhase.SENT_SPEAKING
        assert h.sent("call-1")[-1] == ({"interrupted": True}, False)
    assert all(r.generation is None or r.generation == 1 for r in h.arbiter.submitted)
    assert h.trace.last_record("call-1")["closed_by"] == c.closed_by.value


@pytest.mark.parametrize("seed", range(20))
async def test_write_rows_random_timing_no_duplicate_side_effect(seed):
    rng = random.Random(seed)
    h = build()
    h.knobs["delay_ms"] = rng.choice([50, 200, 500])
    h.layer.on_user_final(C1, "打开灯", action="set_flag")
    c = await h.call("call-1", "set_flag", {"key": "k", "value": "v"})
    await h.clock.advance(rng.randint(0, 600))
    await h.barge_in()
    await h.clock.advance(2000)
    await settle()
    assert c.settled and h.knobs["backend_calls"] <= 1
    assert c.closed_by in (ClosedBy.INTERRUPTED, ClosedBy.RESULT)
    if c.interrupt_phase in (InterruptPhase.RUNNING, InterruptPhase.DONE_NOT_SENT):
        assert c.closed_by is ClosedBy.INTERRUPTED and len(h.arbiter.by_origin("task_result")) == 1  # told once at idle
    else:  # row 4: already spoken and returned, no extra sentence
        assert h.arbiter.by_origin("task_result") == [] and h.sent("call-1")[-1] == ({"interrupted": True}, False)
    assert len([s for s in h.sent("call-1") if "not_spoken" in s[0]]) <= 1


# --------------------------------------------------------------------- cue

async def test_cue_long_for_a2_and_short_for_a1_and_none_without_cueport():
    d = ScriptedDelegate([DelegateMessage.speak("一句。"), DelegateMessage.end("done")])
    h = build(delegate=d)
    await h.call("call-1", "explain_topic", {"target": "t"})
    cue = h.arbiter.by_origin("cue")[0]
    assert cue.content.duration_ms == 760 and cue.generation == 1 and cue.content_role.value == "filler"
    h2 = build(cues=False)
    await h2.call("call-1", "get_status")
    assert h2.arbiter.by_origin("cue") == [] and h2.arbiter.flushes == []


async def test_cue_withdrawn_only_once_and_before_speech(h: Harness):
    h.knobs["delay_ms"] = 50
    await h.call("call-1", "get_status")
    assert h.arbiter.flushes == []
    await h.clock.advance(50)
    await settle()
    assert h.arbiter.flushes == [("cue", C1, None)]
    assert h.layer._entries["call-1"].cue_request_id is None


# --------------------------------------------------------------------- before_commit / rebuild / late

async def test_before_commit_held_until_turn_committed(h: Harness):
    c = await h.call("call-1", "get_status", before_commit=True)
    assert not c.settled and c.state is CallState.RECEIVED and h.layer.has_open_calls(C1)
    await h.bus.emit("turn.committed", turn_id=C1)
    await settle()
    assert c.closed_by is ClosedBy.RESULT


async def test_before_commit_barge_in_while_held_is_row1(h: Harness):
    c = await h.call("call-1", "get_status", before_commit=True)
    await h.barge_in()
    assert c.closed_by is ClosedBy.CANCELLED and c.interrupt_phase is InterruptPhase.RECEIVED
    await h.bus.emit("turn.committed", turn_id=C1)
    await settle()
    assert len(h.sent("call-1")) == 1


async def test_before_commit_already_committed_runs_now(h: Harness):
    await h.bus.emit("turn.committed", turn_id=C1)
    c = await h.call("call-1", "get_status", before_commit=True)
    assert c.closed_by is ClosedBy.RESULT


async def test_session_rebuilt_closes_in_flight_superseded_without_send():
    d = ScriptedDelegate([DelegateMessage.speak("x。"), DelegateMessage.end("done")], gate_each=True)
    h = build(delegate=d)
    h.knobs["delay_ms"] = 1000
    c1 = await h.call("call-1", "get_status")
    c2 = await h.call("call-2", "explain_topic", {"target": "t"}, turn="c2")
    h.knobs["delay_ms"] = 0
    c3 = await h.call("call-3", "get_time", turn="c3")
    h.layer.on_user_final(TurnId.parse("c4"), "讲讲")
    await h.layer.on_session_rebuilt()
    await settle()
    assert c1.closed_by is ClosedBy.SUPERSEDED and c2.closed_by is ClosedBy.SUPERSEDED and c3.closed_by is ClosedBy.RESULT
    assert h.sent("call-1") == [] and h.sent("call-2") == [] and c1.output_sent is False
    assert d.cancelled and h.layer._speculations == {}
    await h.clock.advance(5000)
    await settle()
    assert c1.late_result_dropped == 0 and len(h.sent("call-1")) == 0
    assert h.trace.last_record("call-1")["closed_by"] == "superseded"


async def test_late_result_dropped_and_counted(h: Harness):
    """A `continue` read tool keeps running after its call was cancelled; its result is refused."""
    h.knobs["delay_ms"] = 300
    c = await h.call("call-1", "long_job", {"label": "x"})  # progress-closed, interrupt_policy=continue
    entry = h.layer._entries["call-1"]
    # force a non-progress terminal state while the task still runs (watchdog-style)
    c.closed_by = ClosedBy.CANCELLED
    entry.watchdog.cancel()
    await h.clock.advance(300)
    await settle()
    assert c.late_result_dropped == 1 and h.layer.late_result_dropped == 1
    assert h.arbiter.by_origin("task_result") == [] and len(h.sent("call-1")) == 1
    assert h.trace.last_record("call-1")["late_result_dropped"] == 1


async def test_late_write_result_still_tells_user_once(h: Harness):
    h.knobs["delay_ms"] = 300
    h.layer.on_user_final(C1, "打开灯", action="set_flag")
    c = await h.call("call-1", "set_flag", {"key": "k", "value": "v"})
    entry = h.layer._entries["call-1"]
    await h.layer._close(entry, ClosedBy.CANCELLED)  # simulate an external close (e.g. user "算了")
    await h.clock.advance(300)
    await settle()
    assert c.late_result_dropped == 1 and h.knobs["backend_calls"] == 1
    assert len(h.arbiter.by_origin("task_result")) == 1 and len(h.sent("call-1")) == 0


async def test_upstream_refusing_result_still_closes(h: Harness):
    h.upstream.ack_ok = False
    c = await h.call("call-1", "get_status")
    assert c.settled and c.closed_by is ClosedBy.RESULT and c.output_sent is False
    assert "output_sent_at_ms" not in h.trace.latency(C1)


# --------------------------------------------------------------------- delegation (A2)

async def test_a2_delegate_streams_with_generation_and_returns_spoken_text():
    d = ScriptedDelegate([DelegateMessage.speak("第一句。"), DelegateMessage.context(text="事实一"),
                          DelegateMessage.speak("第二句。"), DelegateMessage.end("done")])
    h = build(delegate=d)
    h.context.history = []
    c = await h.call("call-1", "explain_topic", {"target": "天气"})
    assert c.closed_by is ClosedBy.RESULT and c.route is ToolRoute.A2
    req = d.requests[0]
    assert req.call_id == "call-1" and req.turn_id == C1 and req.generation == 1 and req.tool_name == "explain_topic"
    assert req.allowed_tools == h.registry.readonly_names() and req.tool_result.summary == "关于天气的要点"
    assert req.constraints and req.deadline_ms == 8000 and req.state_block is not None
    speech = h.arbiter.by_origin("delegated_result")
    assert [s.content.text for s in speech] == ["第一句。", "第二句。"] and all(s.generation == 1 for s in speech)
    assert h.context.system_events == [(C1, "事实一")]
    body, reply = h.sent("call-1")[0]
    assert reply is False and body["spoken_to_user"] == "第一句。第二句。" and body["note"] == "已经告诉用户了，不要重复"
    assert body["summary"] == "关于天气的要点"
    rec = h.trace.last_record("call-1")
    assert rec["speculation_hit"] is False and rec["tool_route"] == "A2"
    assert h.trace.field(C1, "speak_mode") == "delegated" and h.trace.field(C1, "turn_kind") == "tool_compose"


async def test_a2_speak_dropped_when_stale_generation():
    d = ScriptedDelegate([DelegateMessage.speak("第一句。"), DelegateMessage.speak("过时的话。"), DelegateMessage.end("done")],
                         gate_each=True)
    h = build(delegate=d)
    c = await h.call("call-1", "explain_topic", {"target": "t"})
    await d.started.wait()
    d.step.set()
    await settle()
    h.arbiter.generation += 1  # new turn started without a barge-in callback
    d.step.set()
    await settle()
    assert [s.content.text for s in h.arbiter.by_origin("delegated_result")] == ["第一句。"]
    assert c.closed_by is ClosedBy.INTERRUPTED and d.cancelled
    assert h.layer._entries["call-1"].delegation.stale_dropped == 1


@pytest.mark.parametrize("terminal", ["failed", "timeout", "empty"])
async def test_a2_failure_terminals_speak_local_fallback(terminal):
    d = ScriptedDelegate([DelegateMessage.end(terminal)])
    h = build(delegate=d)
    c = await h.call("call-1", "explain_topic", {"target": "t"})
    assert c.closed_by is ClosedBy.CANCELLED
    assert h.sent("call-1") == [({"cancelled": True, "reason": terminal}, False)]
    assert h.arbiter.by_origin("system_notice")[-1].content.text == PHRASES["delegation"]


async def test_a2_timeout_by_watchdog():
    d = ScriptedDelegate([DelegateMessage.speak("x。")], gate_each=True)
    h = build(delegate=d)
    c = await h.call("call-1", "explain_topic", {"target": "t"})
    await h.clock.advance(8000)
    await settle()
    assert c.closed_by is ClosedBy.CANCELLED and h.sent("call-1")[0][0]["reason"] == "timeout" and d.cancelled


async def test_a2_second_delegation_in_turn_is_duplicate():
    d = ScriptedDelegate([DelegateMessage.speak("x。"), DelegateMessage.end("done")], gate_each=True)
    h = build(delegate=d)
    await h.call("call-1", "explain_topic", {"target": "t"})
    c2 = await h.call("call-2", "explain_topic", {"target": "u"})
    assert c2.closed_by is ClosedBy.DUPLICATE


async def test_a2_without_delegate_falls_back_to_a0():
    h = build(delegate=None)
    c = await h.call("call-1", "explain_topic", {"target": "t"})
    assert c.route is ToolRoute.A0 and c.route_fallback_reason == "no_delegate" and h.sent("call-1")[0][1] is True


async def test_speculation_hit_releases_held_output():
    d = ScriptedDelegate([DelegateMessage.speak("投机的第一句。"), DelegateMessage.speak("第二句。"), DelegateMessage.end("done")],
                         gate_each=True)
    h = build(delegate=d)
    h.layer.on_user_final(C1, "讲讲天气")
    await d.started.wait()
    assert d.requests[0].call_id is None and d.requests[0].transcript == "讲讲天气"
    d.step.set()
    await settle()
    assert h.arbiter.by_origin("delegated_result") == []  # held by the gate
    c = await h.call("call-1", "explain_topic", {"target": "天气"})
    assert [s.content.text for s in h.arbiter.by_origin("delegated_result")] == ["投机的第一句。"]
    assert len(d.requests) == 1  # no second background run
    d.step.set()
    await settle()
    d.step.set()
    await settle()
    assert c.closed_by is ClosedBy.RESULT and h.trace.last_record("call-1")["speculation_hit"] is True
    assert h.sent("call-1")[0][0]["spoken_to_user"] == "投机的第一句。第二句。"
    assert h.arbiter.flushes == [("cue", C1, None)]


async def test_speculation_miss_discarded_on_turn_closed_and_next_call_is_miss():
    d = ScriptedDelegate([DelegateMessage.speak("投机。"), DelegateMessage.end("done")], gate_each=True)
    h = build(delegate=d)
    h.layer.on_user_final(C1, "聊聊")
    await d.started.wait()
    d.step.set()
    await settle()
    await h.layer.on_turn_closed(C1)
    await settle()
    assert d.cancelled and h.arbiter.by_origin("delegated_result") == [] and h.context.system_events == []
    d2 = ScriptedDelegate([DelegateMessage.speak("真的。"), DelegateMessage.end("done")])
    h.layer.delegate = d2
    c = await h.call("call-2", "explain_topic", {"target": "t"}, turn="c2")
    assert c.closed_by is ClosedBy.RESULT and h.trace.last_record("call-2")["speculation_hit"] is False


async def test_speculation_discarded_on_barge_in():
    d = ScriptedDelegate([DelegateMessage.speak("投机。")], gate_each=True)
    h = build(delegate=d)
    h.layer.on_user_final(C1, "聊聊")
    await d.started.wait()
    await h.barge_in()
    assert d.cancelled and h.layer._speculations == {}
