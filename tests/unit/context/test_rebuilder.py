"""Spec 05 §4, §7.2: the single rebuilder (checklist: audio loss 0, no write replay,
no re-sent tool results, fatal rebuilds 0, budget)."""

from __future__ import annotations

import asyncio
import random

import pytest

from vak.core.bus import Bus
from vak.core.clock import FakeClock
from vak.core.context.rebuilder import SessionRebuilder, backoff_ms
from vak.core.defaults import ContextParams
from vak.core.types import ErrorClass, ErrorReason, ErrorScope, RebuildReason, UpstreamSynced, WriteEffect
from vak.upstream.base import Ack, UpstreamError

from .fakes import FakeTools, FakeTrace, FakeUpstream, chat_turn, make_call, make_caps, make_store, tid

FRAME = b"\x00\x01" * 320  # 20 ms at 16 kHz


def build(params=None, caps=None, open_acks=None, factory=True, gate=None):
    """`open_acks` is one queue shared by every upstream the factory makes;
    `gate` (an Event) blocks `open` on every factory-made upstream."""
    params = params or ContextParams()
    open_acks = list(open_acks or [])
    caps = caps or make_caps(response__client_initiated="yes", history__tool_calls="keep")
    clock = FakeClock()
    trace = FakeTrace()
    store = make_store(clock=clock, params=params, trace=trace)
    bus = Bus()
    bus.record = True
    tools = FakeTools()
    ups: list[FakeUpstream] = []

    def make_up() -> FakeUpstream:
        up = FakeUpstream(caps, open_acks=open_acks)
        up.open_gate = gate
        ups.append(up)
        return up

    first = make_up()
    first.open_gate = None
    rb = SessionRebuilder(
        context=store, upstream_factory=make_up if factory else None, upstream=first, caps=caps, clock=clock,
        bus=bus, params=params, trace=trace, tools=tools, registry_tools=tools.tool_table,
    )
    rb.start()
    return rb, store, clock, bus, tools, trace, ups


async def closed_turns(bus, store, n):
    for i in range(1, n + 1):
        await bus.emit("turn.start", turn_id=tid(i))
        chat_turn(store, i, f"问题{i}", f"回答{i}")
        await bus.emit("turn.closed", turn_id=tid(i))


async def test_disconnect_rebuilds_immediately_and_seeds_history():
    rb, store, clock, bus, tools, trace, ups = build()
    await closed_turns(bus, store, 2)
    res = await rb.rebuild(RebuildReason.DISCONNECT)
    assert res.ok and res.session_seq == 2 and store.session_seq == 2
    assert ups[0].calls[-1][0] == "close"
    init = ups[1].inits[0]
    assert init.session_seq == 2 and len(init.history) == 4 and init.history_format.value == "pairs"
    assert "小驼" in init.instructions and init.instructions.rstrip().endswith("刚说到：无。")
    assert init.tools[0].name == "get_time"
    assert all(e.upstream_synced is UpstreamSynced.SEEDED for e in store.entries)
    assert tools.rebuilt == 1
    assert ("session.rebuilt", {"session_seq": 2, "reason": "disconnect"}) in bus.history


async def test_trace_fields_written_for_current_turn():
    rb, store, clock, bus, tools, trace, ups = build()
    await closed_turns(bus, store, 1)
    await clock.advance(1234)
    await rb.rebuild(RebuildReason.DISCONNECT)
    rec = trace.records["c1"]
    assert rec["session_seq"] == 2 and rec["rebuild_reason"] == "disconnect"
    assert rec["history_format"] == "pairs" and rec["history_scope"] == "full" and rec["history_items"] == 2
    assert rec["handoff_start_at_ms"] == 1234 and rec["handoff_done_at_ms"] >= 1234
    assert rec["restart_attempts_in_turn"] == 1


async def test_proactive_waits_for_idle_gap():
    rb, store, clock, bus, tools, trace, ups = build()
    await closed_turns(bus, store, 1)  # turn closed at t=0; quiet rule needs 1 s
    task = asyncio.create_task(rb.rebuild(RebuildReason.SCHEDULED))
    await asyncio.sleep(0)
    await clock.advance(500)
    assert len(ups) == 1, "must not rebuild inside the 1 s quiet window"
    await clock.advance(600)
    res = await task
    assert res.ok and res.reason is RebuildReason.SCHEDULED
    assert trace.records["c1"]["restart_attempts_in_turn"] == 0  # proactive: no budget consumed


async def test_proactive_blocked_while_turn_open_user_speaking_or_tools_open():
    rb, store, clock, bus, tools, trace, ups = build()
    await closed_turns(bus, store, 1)
    await bus.emit("turn.start", turn_id=tid(2))
    task = asyncio.create_task(rb.rebuild(RebuildReason.TURN_LIMIT))
    await clock.advance(3000)
    assert len(ups) == 1
    store.record_user(tid(2), "继续", 2000, 2500)
    chat_turn(store, 2, "", "好的。")
    tools.open_calls = True
    await bus.emit("turn.closed", turn_id=tid(2))
    await clock.advance(3000)
    assert len(ups) == 1, "open tool call blocks the gap"
    tools.open_calls = False
    rb.set_user_speaking(True)
    await clock.advance(3000)
    assert len(ups) == 1, "user speaking blocks the gap"
    rb.set_user_speaking(False)
    await clock.advance(1100)
    res = await task
    assert res.ok


async def test_idle_wait_cap_forces_at_next_turn_end():
    rb, store, clock, bus, tools, trace, ups = build(params=ContextParams(idle_wait_max_ms=2000))
    await closed_turns(bus, store, 1)
    await bus.emit("turn.start", turn_id=tid(2))  # assistant busy with turn 2 the whole time
    task = asyncio.create_task(rb.rebuild(RebuildReason.SCHEDULED))
    await asyncio.sleep(0)  # let the wait start at t=0
    await clock.advance(2500)
    assert len(ups) == 1, "past the cap but a turn is open: never cut mid-speech"
    chat_turn(store, 2, "继续", "好的。")
    await bus.emit("turn.closed", turn_id=tid(2))  # forced at the next turn end, no quiet wait
    res = await task
    assert res.ok and len(ups) == 2


async def test_idle_wait_cap_forces_now_when_no_turn_is_open():
    rb, store, clock, bus, tools, trace, ups = build(params=ContextParams(idle_wait_max_ms=2000))
    await closed_turns(bus, store, 1)
    rb.set_user_speaking(True)  # open mic chatter keeps the gap from ever appearing
    task = asyncio.create_task(rb.rebuild(RebuildReason.SCHEDULED))
    await asyncio.sleep(0)
    await clock.advance(1900)
    assert len(ups) == 1
    await clock.advance(200)
    res = await task
    assert res.ok and len(ups) == 2


async def test_budget_exhausted_after_three_attempts_with_backoff_then_fatal():
    transient = Ack.upstream_error(ErrorClass.TRANSIENT)
    rb, store, clock, bus, tools, trace, ups = build(open_acks=[transient, transient, transient])
    await closed_turns(bus, store, 1)
    task = asyncio.create_task(rb.rebuild(RebuildReason.DISCONNECT))
    await asyncio.sleep(0)
    assert rb.attempts_in_turn == 1
    await clock.advance(499)
    assert rb.attempts_in_turn == 1
    await clock.advance(1)  # backoff 500
    assert rb.attempts_in_turn == 2
    await clock.advance(1000)  # backoff 1000
    assert rb.attempts_in_turn == 3
    res = await task
    assert not res.ok and res.error_class is ErrorClass.FATAL and rb.fatal
    assert trace.records["c1"]["error_class"] == "fatal" and trace.records["c1"]["restart_attempts_in_turn"] == 3
    assert any(name == "session.rebuild_failed" for name, _ in bus.history)
    # exhausted: a further rebuild in the same turn is refused without touching the upstream
    before = len(ups)
    res2 = await rb.rebuild(RebuildReason.DISCONNECT)
    assert not res2.ok and len(ups) == before
    # a new user-initiated turn starts the count over
    await bus.emit("turn.start", turn_id=tid(2))
    assert rb.attempts_in_turn == 0 and not rb.fatal


def test_backoff_values():
    p = ContextParams()
    assert [backoff_ms(i, p) for i in (1, 2, 3, 4, 5, 6)] == [500, 1000, 2000, 4000, 8000, 10000]


async def test_fatal_upstream_error_never_rebuilds():
    rb, store, clock, bus, tools, trace, ups = build()
    await closed_turns(bus, store, 1)
    res = await rb.on_upstream_error(UpstreamError(ErrorClass.FATAL, ErrorReason.AUTH, ErrorScope.SESSION))
    assert res is not None and not res.ok and len(ups) == 1
    assert await rb.on_upstream_error(UpstreamError(ErrorClass.BENIGN, ErrorReason.CANCEL_RACE, ErrorScope.RESPONSE)) is None
    assert len(ups) == 1


async def test_stable_connection_resets_budget():
    transient = Ack.upstream_error(ErrorClass.TRANSIENT)
    rb, store, clock, bus, tools, trace, ups = build(open_acks=[transient])
    await closed_turns(bus, store, 1)
    task = asyncio.create_task(rb.rebuild(RebuildReason.DISCONNECT))
    await asyncio.sleep(0)  # first attempt fails, backoff 500 registered
    await clock.advance(500)
    assert (await task).ok and rb.attempts_in_turn == 2
    await clock.advance(10_000)
    await rb.rebuild(RebuildReason.DISCONNECT)
    assert rb.attempts_in_turn == 1


async def test_context_overflow_shrinks_history_before_rebuild():
    rb, store, clock, bus, tools, trace, ups = build()
    await closed_turns(bus, store, 6)
    await rb.rebuild(RebuildReason.DISCONNECT)
    assert len(ups[1].inits[0].history) == 10  # K=5 → 5 turns
    await rb.rebuild(RebuildReason.CONTEXT_OVERFLOW)
    init = ups[2].inits[0]
    assert len(init.history) == 4  # K halved to 2
    assert trace.records["c6"]["history_scope"] == "truncated"
    assert trace.records["c6"]["rebuild_reason"] == "context_overflow"


async def test_handoff_replays_init_then_user_then_buffered_audio_in_order():
    gate = asyncio.Event()
    rb, store, clock, bus, tools, trace, ups = build(gate=gate)
    await closed_turns(bus, store, 1)
    await bus.emit("turn.start", turn_id=tid(2))
    store.record_user(tid(2), "几点了", 2000, 2500)  # no reply yet when the link drops
    task = asyncio.create_task(rb.rebuild(RebuildReason.DISCONNECT))
    await asyncio.sleep(0)
    assert rb.handoff_active
    frames = [bytes([i]) * 640 for i in range(3)]
    for f in frames:
        assert rb.buffer_uplink(f) is True
    gate.set()
    res = await task
    assert res.ok and res.replayed_user_text == "几点了" and res.replayed_audio_frames == 3
    names = [c[0] for c in ups[1].calls]
    assert names == ["open", "request_response", "send_audio", "send_audio", "send_audio"]
    assert [c[1] for c in ups[1].calls if c[0] == "send_audio"] == frames
    init = ups[1].inits[0]
    assert init.history[-1].role == "user" and init.history[-1].text == "几点了"
    assert [i.text for i in init.history[:-1]] == ["问题1", "回答1"]
    assert not rb.handoff_active and rb.buffer_uplink(FRAME) is False


async def test_handoff_buffer_cap_emits_cue_event_and_drops():
    p = ContextParams(handoff_buffer_max_ms=40)
    gate = asyncio.Event()
    rb, store, clock, bus, tools, trace, ups = build(params=p, gate=gate)
    task = asyncio.create_task(rb.rebuild(RebuildReason.DISCONNECT))
    await asyncio.sleep(0)
    assert rb.buffer_uplink(FRAME) and rb.buffer_uplink(FRAME)  # 40 ms
    assert rb.buffer_uplink(FRAME)  # over cap: taken but dropped
    await asyncio.sleep(0)
    assert any(n == "session.handoff_buffer_full" for n, _ in bus.history)
    gate.set()
    res = await task
    assert res.replayed_audio_frames == 2


async def test_write_executed_in_open_turn_suppresses_replay():
    rb, store, clock, bus, tools, trace, ups = build()
    await closed_turns(bus, store, 1)
    await bus.emit("turn.start", turn_id=tid(2))
    store.record_user(tid(2), "帮我提交", 2000, 2500)
    store.record_tool_call(make_call(2, "w-1", "submit", closed_by=None), WriteEffect("k", executed=True, result_ref="r"))
    res = await rb.rebuild(RebuildReason.DISCONNECT)
    assert res.ok and res.replayed_user_text is None and res.replay_skipped == "write_executed"
    assert all(c[0] != "request_response" for c in ups[1].calls)
    assert any(i.text == "帮我提交" for i in ups[1].inits[0].history)  # kept as history, not re-asked


async def test_write_unknown_suppresses_replay():
    rb, store, clock, bus, tools, trace, ups = build()
    await bus.emit("turn.start", turn_id=tid(1))
    store.record_user(tid(1), "帮我提交", 0, 500)
    store.record_tool_call(make_call(1, "w-1", "submit", closed_by=None), WriteEffect("k", executed=False))
    res = await rb.rebuild(RebuildReason.DISCONNECT)
    assert res.replayed_user_text is None and res.replay_skipped == "write_unknown"


async def test_partial_reply_kept_in_history_with_marker_not_replayed():
    rb, store, clock, bus, tools, trace, ups = build()
    await bus.emit("turn.start", turn_id=tid(1))
    store.record_user(tid(1), "讲讲这里", 0, 500)
    store.record_assistant_pending(tid(1), "req-1", 0, "这里是山顶，适合看日落。", "model")
    store.record_assistant_playback("req-1", 0, "interrupted", 500, duration_ms=2000)
    res = await rb.rebuild(RebuildReason.DISCONNECT)
    assert res.replay_skipped == "partial_reply"
    hist = ups[1].inits[0].history
    assert hist[-1].role == "assistant" and "被用户打断" in hist[-1].text


async def test_delivered_registry_prevents_resending_seeded_tool_results():
    rb, store, clock, bus, tools, trace, ups = build()
    await bus.emit("turn.start", turn_id=tid(1))
    store.record_user(tid(1), "几点了", 0, 500)
    store.record_tool_call(make_call(1, "call-1"))
    store.record_tool_output(make_call(1, "call-1"))
    chat_turn(store, 1, "", "现在三点。")
    await bus.emit("turn.closed", turn_id=tid(1))
    res = await rb.rebuild(RebuildReason.DISCONNECT)
    assert res.delivered_registry.is_delivered("call-1")
    assert not res.delivered_registry.is_delivered("call-9")
    assert rb.delivered_registry is res.delivered_registry
    await clock.advance(1100)  # quiet gap for the proactive swap
    res2 = await rb.rebuild(RebuildReason.SCHEDULED)
    assert res2.delivered_registry is not res.delivered_registry and res2.delivered_registry.is_delivered("call-1")


async def test_urgent_reason_takes_over_a_waiting_proactive_rebuild():
    rb, store, clock, bus, tools, trace, ups = build()
    await closed_turns(bus, store, 1)
    rb.set_user_speaking(True)
    waiting = asyncio.create_task(rb.rebuild(RebuildReason.SCHEDULED))
    await asyncio.sleep(0)
    await clock.advance(300)
    urgent = asyncio.create_task(rb.rebuild(RebuildReason.DISCONNECT))
    r1, r2 = await asyncio.gather(waiting, urgent)
    assert len(ups) == 2, "one rebuild, not two"
    assert r1.reason is RebuildReason.DISCONNECT and r2 is r1


async def test_reuses_same_upstream_object_without_factory():
    rb, store, clock, bus, tools, trace, ups = build(factory=False)
    await closed_turns(bus, store, 1)
    await clock.advance(1100)
    res = await rb.rebuild(RebuildReason.SCHEDULED)
    assert res.ok and len(ups) == 1 and [c[0] for c in ups[0].calls] == ["close", "open"]


@pytest.mark.parametrize("seed", range(20))
async def test_random_handoff_timing_loses_no_audio(seed):
    """Checklist: handoff audio loss 0 under random interleavings."""
    rng = random.Random(seed)
    gate = asyncio.Event()
    rb, store, clock, bus, tools, trace, ups = build(gate=gate)
    await bus.emit("turn.start", turn_id=tid(1))
    store.record_user(tid(1), "你好", 0, 500)
    task = asyncio.create_task(rb.rebuild(RebuildReason.DISCONNECT))
    await asyncio.sleep(0)
    sent: list[bytes] = []
    for i in range(rng.randint(1, 12)):
        frame = bytes([i % 256]) * rng.choice([320, 640])
        if rng.random() < 0.3:
            await asyncio.sleep(0)
        assert rb.buffer_uplink(frame)
        sent.append(frame)
    gate.set()
    res = await task
    assert res.ok
    assert [c[1] for c in ups[1].calls if c[0] == "send_audio"] == sent
