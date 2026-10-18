"""Spec 02 §6 acceptance items that need no real device or upstream."""

from __future__ import annotations

import random

import pytest

from tests.unit.arbiter.fakes import FRAME_MS, ClientSim, World, make_world, settle, tone
from vak.core.arbiter import (
    Arbiter,
    ArbiterEvent,
    ArbiterState,
    RequestEvent,
    arbiter_next,
    build_request,
    request_next,
)
from vak.core.defaults import ArbiterParams
from vak.core.types import (
    ContentType,
    ContextPolicy,
    DropReason,
    IllegalTransition,
    InterruptSource,
    Kind,
    Origin,
    OutputContent,
    OutputRequest,
    OutputStatus,
    PlaybackReceipt,
    Priority,
    Profile,
    RequestStatus,
    TurnId,
    TurnSource,
)

# ----------------------------------------------------------------- helpers


def speech(w: World, turn: TurnId, text: str, origin: Origin = Origin.TOOL_RESULT, **kw) -> OutputRequest:
    return build_request(
        w.ids,
        origin,
        OutputContent(ContentType.TEXT_TTS, text=text),
        generation=w.arb.generation,
        turn_id=turn,
        **kw,
    )


def cue(w: World, turn: TurnId, cue_id: str = "short", generation: int | None = None) -> OutputRequest:
    return build_request(
        w.ids,
        Origin.CUE,
        OutputContent(ContentType.PRERECORDED, text=cue_id),
        generation=w.arb.generation if generation is None else generation,
        turn_id=turn,
    )


def narration(w: World, text: str, origin: Origin = Origin.TASK_RESULT, **kw) -> OutputRequest:
    return build_request(w.ids, origin, OutputContent(ContentType.TEXT_TTS, text=text), **kw)


def stream(w: World, turn: TurnId, response_id: str, item_id: str) -> OutputRequest:
    return build_request(
        w.ids,
        Origin.MODEL,
        OutputContent(ContentType.UPSTREAM_STREAM, response_id=response_id, item_id=item_id),
        generation=w.arb.generation,
        turn_id=turn,
    )


async def start_turn(w: World, n: int, *, speaking_done: bool = True) -> TurnId:
    t = TurnId("c", n)
    await w.arb.on_turn_start(t, TurnSource.BUTTON)
    if speaking_done:
        w.arb.set_user_speaking(False)
    await settle()
    return t


def events(w: World, name: str) -> list[dict]:
    return [p for n, p in w.bus.history if n == name]


# ------------------------------------------------------------ state tables


def test_state_tables_illegal_transitions():
    assert arbiter_next(ArbiterState.IDLE, ArbiterEvent.GRANT) is ArbiterState.GRANTED
    assert arbiter_next(ArbiterState.FLUSHING, ArbiterEvent.FLUSH) is ArbiterState.FLUSHING
    with pytest.raises(IllegalTransition):
        arbiter_next(ArbiterState.IDLE, ArbiterEvent.FLUSH_END)
    assert request_next(RequestStatus.INTERRUPTED, RequestEvent.DELIVERED) is RequestStatus.DONE
    with pytest.raises(IllegalTransition):
        request_next(RequestStatus.DONE, RequestEvent.GRANT)


def test_intake_validation_and_defaults():
    w = make_world()
    with pytest.raises(ValueError):
        w.arb._validate(
            OutputRequest("x", Origin.MODEL, Kind.SPEECH, OutputContent(ContentType.TEXT_TTS, text="a"))
        )
    n = narration(w, "hello")
    assert n.generation is None and n.kind is Kind.NARRATION and n.priority is Priority.NORMAL
    n.generation = 3
    with pytest.raises(ValueError):
        w.arb._validate(n)
    c = cue(w, TurnId("c", 1))
    assert c.kind is Kind.CUE and c.priority is Priority.HIGH and c.context_policy is ContextPolicy.NONE
    # interruptible=false outside the whitelist is treated as true (spec 02 §2.5)
    s = speech(w, TurnId("c", 1), "一二三四五六七八", interruptible=False)
    w.arb._validate(s)
    assert s.interruptible is True
    ok = speech(w, TurnId("c", 1), "请牵好大人的手", origin=Origin.SYSTEM_NOTICE, interruptible=False)
    w.arb._validate(ok)
    assert ok.interruptible is False
    r = narration(w, "x", on_interrupt="retry")
    w.arb._validate(r)
    assert r.max_retries == ArbiterParams().max_retries_result


# ------------------------------------------------------- §6 double voice 0


async def test_double_voice_0_random_submits():
    w = make_world()
    sim = ClientSim(w.arb, w.transport, w.clock)
    rng = random.Random(7)
    for n in range(1, 6):
        t = await start_turn(w, n)
        for _ in range(rng.randint(1, 4)):
            if rng.random() < 0.4:
                await w.arb.submit(cue(w, t, rng.choice(["short", "long"])))
            else:
                await w.arb.submit(speech(w, t, "好的，" + "明早八点出发。" * rng.randint(1, 3)))
            await sim.run(rng.randint(0, 400))
        await w.arb.on_reply_done(t, "completed")
        await sim.run(6000)
        assert w.arb.state is ArbiterState.IDLE
    assert sim.overlaps == 0
    assert sim.overlapping_pairs() == 0
    assert w.trace.counter("double_voice_count") == 0
    assert w.transport.consumer_dropped == 0


# ------------------------------------------------- §6 late frames after cancel


async def test_late_frames_after_cancel_filtered():
    w = make_world()
    t = await start_turn(w, 1)
    req = stream(w, t, "resp-1", "item-1")
    gen = w.arb.generation
    await w.arb.submit(req)
    await settle()
    for _ in range(5):
        await w.arb.feed_upstream_audio(req.request_id, tone(100), 16000, "resp-1", "item-1", gen)
    n_before = len(w.transport.frames)
    assert n_before == 25
    assert w.arb.request_for_response("resp-1") == req.request_id
    await w.arb.on_barge_in(t, InterruptSource.BUTTON)
    await settle()
    assert w.upstream.cancelled == ["resp-1"]
    # the mock upstream keeps pushing the old response for 2 s
    for _ in range(20):
        await w.arb.feed_upstream_audio(req.request_id, tone(100), 16000, "resp-1", "item-1", gen)
        await w.clock.advance(100)
    assert len(w.transport.frames) == n_before
    assert w.trace.counter("stale_frames_dropped") == 20
    assert w.trace.counter("suppressed_frames") == 20
    assert w.transport.consumer_dropped == 0
    assert w.transport.frames_after_flush() == []
    assert w.arb.record(req.request_id).status is RequestStatus.DONE  # user interruption = delivered


# ----------------------------------------------- §6 zero frames after flush_end


async def test_zero_frames_after_flush_end():
    w = make_world()
    w.tts.latency_ms = 50
    t = await start_turn(w, 1)
    req = speech(w, t, "明早八点出发。请牵好大人的手。然后我们去公园。")
    await w.arb.submit(req)
    await settle()
    await w.clock.advance(60)  # first sentence synthesised and sent, second one pending
    assert w.transport.frames
    await w.arb.on_barge_in(t, InterruptSource.BUTTON)
    await settle()
    await w.clock.advance(2000)
    assert w.tts.flushed == [req.request_id]
    assert [m["type"] for m in w.transport.json if m["type"] in ("flush.start", "flush.end", "tts.stop")][
        -3:
    ] == [
        "flush.start",
        "flush.end",
        "tts.stop",
    ]
    assert w.transport.json[-1] == {"type": "tts.stop", "turn_id": "c1", "reason": "interrupted"}
    assert w.transport.frames_after_flush() == []
    assert w.transport.flushes[0][3] == "abort"
    assert w.arb.state is ArbiterState.IDLE


async def test_flush_merging_and_timeout():
    w = make_world()
    w.tts.flush_latency_ms = 3000
    t = await start_turn(w, 1)
    await w.arb.submit(speech(w, t, "明早八点出发。"))
    await settle()
    await w.arb.on_barge_in(t, InterruptSource.BUTTON)
    t2 = await start_turn(w, 2)
    assert w.arb.state is ArbiterState.FLUSHING
    await w.arb.submit(speech(w, t2, "好的。"))
    assert w.arb.holder is None  # no grant while flushing
    await w.clock.advance(1100)
    assert w.arb.state is not ArbiterState.FLUSHING  # released after the 1 s cap
    assert w.trace.counter("flush_timeouts") == 1
    assert w.arb.holder is not None and w.arb.holder.req.turn_id == t2


# ------------------------------------------- §6 receipts lost: ended / started


async def test_dropped_all_ended_narration_still_plays_and_turn_drains():
    w = make_world()
    sim = ClientSim(w.arb, w.transport, w.clock, drop={"ended"})
    t = await start_turn(w, 1)
    s = speech(w, t, "明早八点出发。")
    await w.arb.submit(s)
    n = narration(w, "提醒：该出门了。")
    await w.arb.submit(n)
    await w.arb.on_reply_done(t, "completed")
    await sim.run(1500)
    assert w.arb.record(n.request_id).status is RequestStatus.QUEUED  # window closed: unplayed audio
    await sim.run(3000)  # ended timeout: 700 ms + 2 s after send complete
    assert w.arb.record(s.request_id).status is RequestStatus.DONE
    assert w.trace.fields[t]["receipt_timeouts"]["ended"] == 1
    assert events(w, "turn.audio_drained") == [{"turn_id": t}]
    await sim.run(1000)
    rec = w.arb.record(n.request_id)
    assert rec.status in (RequestStatus.SENT, RequestStatus.PLAYING, RequestStatus.DONE)
    assert rec.turn_id == TurnId("s", 1)
    assert {"type": "turn.start", "turn_id": "s1", "source": "event", "kind": "narration"} in w.transport.json
    assert sim.overlaps == 0


async def test_dropped_all_started_retries_bounded_never_overlapping():
    w = make_world()
    sim = ClientSim(w.arb, w.transport, w.clock, drop={"started", "ended"})
    t = await start_turn(w, 1)
    await w.arb.on_reply_done(t, "completed")
    n = narration(w, "一" * 50, on_interrupt="retry", max_retries=2)  # 5 s of audio
    await w.arb.submit(n)
    await sim.run(40_000)
    rec = w.arb.record(n.request_id)
    assert rec.status is RequestStatus.DROPPED and rec.drop_reason is DropReason.RETRY_EXHAUSTED
    assert rec.retries == 2
    starts = [m for m in w.transport.json if m["type"] == "tts.start"]
    assert len(starts) == 3  # first attempt + 2 retries
    assert w.trace.counter("double_voice_count") == 0 and sim.overlaps == 0
    assert w.trace.outputs()[-1]["status"] == OutputStatus.DROPPED_RETRY_EXHAUSTED.value
    assert len(w.context.playback) == 1  # no duplicate context notifications on retry


# -------------------------------------------------- §6 narration interrupted


async def test_narration_interrupted_by_user_not_replayed():
    w = make_world()
    sim = ClientSim(w.arb, w.transport, w.clock)
    t = await start_turn(w, 1)
    await w.arb.on_reply_done(t, "completed")
    n = narration(w, "后台任务完成了。", on_interrupt="retry", dedupe_key="task-1:result")
    await w.arb.submit(n)
    await sim.run(600)
    assert w.arb.record(n.request_id).status is RequestStatus.PLAYING
    await w.arb.on_turn_start(TurnId("c", 2), TurnSource.BUTTON)
    await sim.run(200)
    rec = w.arb.record(n.request_id)
    assert rec.status is RequestStatus.DONE and rec not in w.arb.queue
    frames_before = len(w.transport.frames)
    w.arb.set_user_speaking(False)
    await w.arb.on_reply_done(TurnId("c", 2), "completed")
    await sim.run(5000)
    assert len(w.transport.frames) == frames_before
    assert w.trace.outputs()[-1]["status"] == OutputStatus.TRUNCATED.value
    # same key again: already delivered
    await w.arb.submit(narration(w, "后台任务完成了。", dedupe_key="task-1:result"))
    assert w.trace.outputs()[-1]["status"] == OutputStatus.DROPPED_DUPLICATE.value


async def test_upstream_stream_truncate_uses_offset_plus_played():
    w = make_world()
    sim = ClientSim(w.arb, w.transport, w.clock)
    t = await start_turn(w, 1)
    await w.arb.submit(cue(w, t, "short"))  # sentence 0, 300 ms
    req = stream(w, t, "resp-1", "item-1")
    await w.arb.submit(req)
    gen = w.arb.generation
    await sim.run(400)  # cue played; stream granted
    for _ in range(10):
        await w.arb.feed_upstream_audio(req.request_id, tone(100), 16000, "resp-1", "item-1", gen)
    await sim.run(500)
    assert w.arb.record(req.request_id).sentences[0].sentence_id == 1  # unified numbering across kinds
    await w.arb.on_barge_in(t, InterruptSource.VAD)
    await sim.run(100)
    assert w.upstream.cancelled == ["resp-1"]
    assert len(w.upstream.truncated) == 1
    item, end_ms, _ = w.upstream.truncated[0]
    assert item == "item-1" and 400 <= end_ms <= 620  # position within the item, not the turn
    assert w.transport.flushes[0][3] is None  # VAD: no abort to ack


async def test_truncate_estimated_without_interrupted_receipt():
    w = make_world(receipts={"started", "ended"})
    t = await start_turn(w, 1)
    req = stream(w, t, "resp-1", "item-1")
    await w.arb.submit(req)
    await settle()
    await w.arb.feed_upstream_audio(req.request_id, tone(500), 16000, "resp-1", "item-1", w.arb.generation)
    await w.clock.advance(200)
    await w.arb.on_barge_in(t, InterruptSource.BUTTON)
    await settle()
    assert w.upstream.truncated == [("item-1", 200, None)]
    assert w.trace.fields[t]["truncate_estimated"] is True


# ------------------------------------------ §6 window: ended + 350, awaiting_tool


async def test_narration_during_long_reply_waits_for_ended_plus_350():
    w = make_world()
    sim = ClientSim(w.arb, w.transport, w.clock)
    t = await start_turn(w, 1)
    s = speech(w, t, "这是一段很长的回答。它有好几句话。最后一句在这里。")
    await w.arb.submit(s)
    await sim.run(500)
    n = narration(w, "提醒：到点了。", origin=Origin.REMINDER)
    await w.arb.submit(n)
    await w.arb.on_reply_done(t, "completed")
    await sim.run(8000)
    speech_ended = max(r.t_ms for r in sim.receipts if r.request_id == s.request_id and r.kind == "ended")
    n_started = min(r.t_ms for r in sim.receipts if r.request_id == n.request_id and r.kind == "started")
    assert n_started >= speech_ended + 350
    assert events(w, "turn.audio_drained")[0] == {"turn_id": t}
    assert w.trace.fields[t]["latency"]["sound_first_at_ms"] is not None


async def test_awaiting_tool_blocks_narration_until_turn_done():
    w = make_world()
    sim = ClientSim(w.arb, w.transport, w.clock)
    t = await start_turn(w, 1)
    await w.arb.submit(cue(w, t, "short"))
    w.arb.set_awaiting_tool(t, True)
    n = narration(w, "后台结果。")
    await w.arb.submit(n)
    await sim.run(3000)
    assert w.arb.record(n.request_id).status is RequestStatus.QUEUED
    await w.arb.submit(speech(w, t, "查到了。"))
    w.arb.set_awaiting_tool(t, False)
    await w.arb.on_reply_done(t, "completed")
    await sim.run(3000)
    assert w.arb.record(n.request_id).status is RequestStatus.DONE
    kinds = [m["kind"] for m in w.transport.json if m["type"] == "tts.start"]
    assert kinds == ["cue", "speech", "narration"]


async def test_task_progress_needs_nothing_in_flight():
    w = make_world()
    sim = ClientSim(w.arb, w.transport, w.clock)
    t = await start_turn(w, 1)
    await w.arb.on_reply_done(t, "completed")
    await sim.run(400)
    w.arb.set_user_speaking(True)  # window closed, a speech queues up behind it
    await settle()
    p = narration(w, "进度。", origin=Origin.TASK_PROGRESS)
    await w.arb.submit(p)
    t2 = await start_turn(w, 2, speaking_done=False)
    await w.arb.submit(speech(w, t2, "回答。"))
    w.arb.set_user_speaking(False)
    await sim.run(100)
    assert w.arb.holder is not None and w.arb.holder.req.kind is Kind.SPEECH
    assert w.arb.record(p.request_id).status is RequestStatus.QUEUED


# ----------------------------------------------- §6 dedupe / expired / lease


async def test_same_turn_duplicate_dedupe_key():
    w = make_world()
    t = await start_turn(w, 1)
    a = narration(w, "结果。", dedupe_key="task-88:result")
    b = narration(w, "结果。", dedupe_key="task-88:result")
    await w.arb.submit(a)
    await w.arb.submit(b)
    assert w.arb.record(a.request_id).status is RequestStatus.QUEUED
    assert w.arb.record(b.request_id).drop_reason is DropReason.DUPLICATE
    assert w.trace.fields[t]["drops_by_reason"]["duplicate"] == 1


async def test_expired_narration_while_window_closed():
    w = make_world()
    await start_turn(w, 1, speaking_done=False)
    n = narration(w, "到点了。", origin=Origin.REMINDER, expires_at_ms=2000)
    await w.arb.submit(n)
    await w.clock.advance(1500)
    assert w.arb.record(n.request_id).status is RequestStatus.QUEUED
    await w.clock.advance(1500)  # the 1 s self-wakeup re-checks even without events
    rec = w.arb.record(n.request_id)
    assert rec.status is RequestStatus.DROPPED and rec.drop_reason is DropReason.EXPIRED
    assert w.transport.frames == []


async def test_background_task_speaks_across_10_turns():
    w = make_world()
    sim = ClientSim(w.arb, w.transport, w.clock)
    for n in range(1, 12):
        t = await start_turn(w, n)
        await w.arb.submit(speech(w, t, "好的。"))
        await w.arb.on_reply_done(t, "completed")
        await sim.run(1500)
        r = narration(w, f"后台第{n}条。", dedupe_key=f"bg:{n}")
        await w.arb.submit(r)
        await sim.run(3000)
        rec = w.arb.record(r.request_id)
        assert rec.status is RequestStatus.DONE and rec.frames_sent > 0
        assert rec.lease is not None and rec.lease.generation == w.arb.generation
    assert sim.overlaps == 0


# -------------------------------------------------- §6 cue / critical / queue


async def test_cue_in_play_finishes_on_barge_in_and_old_cue_dropped():
    w = make_world()
    sim = ClientSim(w.arb, w.transport, w.clock)
    t = await start_turn(w, 1)
    old_gen = w.arb.generation
    c = cue(w, t, "long")
    await w.arb.submit(c)
    await sim.run(100)
    assert w.arb.record(c.request_id).status is RequestStatus.PLAYING
    await w.arb.on_barge_in(t, InterruptSource.BUTTON)
    await settle()
    assert w.transport.flushes == []  # cue is not flushed
    assert w.arb.state is ArbiterState.GRANTED
    late = cue(w, t, "short", generation=old_gen)
    await w.arb.submit(late)
    assert w.arb.record(late.request_id).drop_reason is DropReason.STALE
    await sim.run(1000)
    rec = w.arb.record(c.request_id)
    assert rec.status is RequestStatus.DONE and rec.played_ms == 760
    assert w.arb.state is ArbiterState.IDLE
    assert w.trace.outputs(t)[-1]["status"] == OutputStatus.PLAYED.value


async def test_critical_preemption():
    w = make_world(profile=Profile.B)
    sim = ClientSim(w.arb, w.transport, w.clock)
    t = await start_turn(w, 1)
    s = speech(w, t, "我来给您介绍一下。这个很长。", origin=Origin.MODEL)
    await w.arb.submit(s)
    await sim.run(300)
    g = w.arb.generation
    x = build_request(
        w.ids,
        Origin.SYSTEM_EXIT,
        OutputContent(ContentType.TEXT_TTS, text="正在为您转人工。"),
        generation=g,
        turn_id=t,
    )
    assert x.priority is Priority.CRITICAL
    await w.arb.submit(x)
    assert w.arb.generation == g + 1 and x.generation == g + 1
    assert w.arb.holder is not None and w.arb.holder.req is x
    await sim.run(100)
    assert w.transport.flushes[0][2] == "critical"
    assert w.arb.record(s.request_id).status is RequestStatus.DROPPED
    await sim.run(3000)
    assert w.arb.record(x.request_id).status is RequestStatus.DONE
    assert sim.overlaps == 0 and w.transport.frames_after_flush(w.arb) == []
    out = {o["request_id"]: o["status"] for o in w.trace.outputs(t)}
    assert (
        out[s.request_id] == OutputStatus.TRUNCATED.value and out[x.request_id] == OutputStatus.PLAYED.value
    )


async def test_queue_overflow_bounded_and_recorded():
    w = make_world(profile=Profile.B)
    t = await start_turn(w, 1, speaking_done=False)
    ids = []
    for i in range(20):
        r = narration(w, f"第{i}条", priority=Priority.NORMAL if i % 2 else Priority.LOW)
        ids.append(r.request_id)
        await w.arb.submit(r)
    assert len(w.arb.queue) == 16
    dropped = [w.arb.record(i) for i in ids if w.arb.record(i).status is RequestStatus.DROPPED]
    assert len(dropped) == 4 and all(d.drop_reason is DropReason.OVERFLOW for d in dropped)
    assert all(d.req.priority is Priority.LOW for d in dropped)  # lowest priority, earliest
    assert [d.req.request_id for d in dropped] == [ids[0], ids[2], ids[4], ids[6]]
    assert w.trace.fields[t]["queue_truncated"] is True
    assert w.trace.fields[t]["drops_by_reason"]["overflow"] == 4


async def test_progress_throttling_via_supersedes_and_directed_flush():
    w = make_world()
    await start_turn(w, 1, speaking_done=False)
    ids = []
    for i in range(5):
        p = narration(
            w,
            f"进度 {i}",
            origin=Origin.TASK_PROGRESS,
            dedupe_key="task-9:progress",
            supersedes="task-9:progress",
        )
        ids.append(p.request_id)
        await w.arb.submit(p)
    queued = [q.req.request_id for q in w.arb.queue]
    assert queued == [ids[-1]]  # newest replaces older progress
    assert w.arb.record(ids[0]).drop_reason is DropReason.DUPLICATE
    # task finished: its queued progress is removed, result stays
    res = narration(w, "完成。", origin=Origin.TASK_RESULT, dedupe_key="task-9:result")
    await w.arb.submit(res)
    await w.arb.flush_origin("task_progress", dedupe_key="task-9:progress")
    assert [q.req.request_id for q in w.arb.queue] == [res.request_id]
    assert w.arb.record(ids[-1]).status is RequestStatus.DROPPED
    assert w.trace.outputs()[-1]["status"] == OutputStatus.FLUSHED.value


# ------------------------------------------------- turn events / context / trace


async def test_turn_no_output_and_audio_drained():
    w = make_world()
    sim = ClientSim(w.arb, w.transport, w.clock)
    t = await start_turn(w, 1)
    await w.arb.on_reply_done(t, "failed")
    assert events(w, "turn.no_output") == [{"turn_id": t}]
    t2 = await start_turn(w, 2)
    await w.arb.submit(speech(w, t2, "好的。"))
    await sim.run(200)
    await w.arb.on_reply_done(t2, "completed")
    assert events(w, "turn.audio_drained") == []
    await sim.run(1500)
    assert events(w, "turn.audio_drained") == [{"turn_id": t2}]
    assert w.arb.window_open()


async def test_context_policies():
    w = make_world()
    sim = ClientSim(w.arb, w.transport, w.clock)
    t = await start_turn(w, 1)
    await w.arb.submit(cue(w, t))
    s = speech(w, t, "明早八点出发。", context_policy=ContextPolicy.AFTER_PLAYED)
    await w.arb.submit(s)
    n = narration(w, "结果先入上下文。", context_policy=ContextPolicy.BEFORE_SPEAK)
    await w.arb.submit(n)
    assert [p[1] for p in w.context.pending] == [n.request_id]  # before_speak at enqueue
    await w.arb.on_reply_done(t, "completed")
    await sim.run(6000)
    assert [p[1] for p in w.context.pending] == [n.request_id, s.request_id]
    by_id = {p[0]: p for p in w.context.playback}
    assert by_id[s.request_id][2] == "completed" and by_id[s.request_id][3] == 700
    assert by_id[n.request_id][2] == "completed"
    assert all(p[0] != w.arb.queue for p in w.context.playback)
    assert len(w.context.playback) == 2  # cue: never


async def test_trace_sound_first_and_content_first():
    w = make_world()
    sim = ClientSim(w.arb, w.transport, w.clock)
    t = await start_turn(w, 1)
    await w.clock.advance(500)
    await w.arb.submit(cue(w, t, "long"))
    await settle()
    await w.clock.advance(100)
    s = speech(w, t, "查到了。")
    await w.arb.submit(s)
    await w.arb.on_reply_done(t, "completed")
    await sim.run(3000)
    lat = w.trace.fields[t]["latency"]
    assert lat["sound_first_at_ms"] == 500
    assert lat["content_first_ready_at_ms"] == 600
    assert lat["content_first_at_ms"] >= 500 + 760
    assert lat["queue_wait_ms"] == lat["content_first_at_ms"] - 600
    assert w.trace.fields[t]["receipt_mode"] == "receipts"
    outs = w.trace.outputs(t)
    assert [o["kind"] for o in outs] == ["cue", "speech"] and all(o["status"] == "played" for o in outs)
    assert outs[1]["sentence_id"] == 1 and outs[1]["played_ms"] == 400


async def test_estimated_receipt_mode_completes_without_receipts():
    w = make_world(receipts=set())
    t = await start_turn(w, 1)
    s = speech(w, t, "明早八点出发。")
    await w.arb.submit(s)
    await w.arb.on_reply_done(t, "completed")
    await settle()
    await w.clock.advance(3000)
    rec = w.arb.record(s.request_id)
    assert rec.status is RequestStatus.DONE and rec.estimated
    assert w.trace.fields[t]["receipt_mode"] == "estimated"
    assert "receipt_timeouts" not in w.trace.fields[t]
    assert events(w, "turn.audio_drained") == [{"turn_id": t}]


async def test_interrupted_receipt_from_client_counts_as_delivered():
    w = make_world()
    t = await start_turn(w, 1)
    s = speech(w, t, "明早八点出发。")
    await w.arb.submit(s)
    await settle()
    await w.arb.on_receipt(PlaybackReceipt("started", t, 0, 0, 0))
    await w.arb.on_receipt(PlaybackReceipt("interrupted", t, 0, 300, 300, reason="button"))
    rec = w.arb.record(s.request_id)
    assert rec.status is RequestStatus.DONE and rec.played_ms == 300
    assert w.arb.state is ArbiterState.IDLE


async def test_uninterruptible_frames_carry_new_generation():
    w = make_world()
    t = await start_turn(w, 1)
    w.tts.latency_ms = 100
    s = speech(w, t, "请牵好大人的手。", origin=Origin.SYSTEM_NOTICE, interruptible=False)
    await w.arb.submit(s)
    await settle()
    await w.arb.on_barge_in(t, InterruptSource.BUTTON)  # G+1 while synthesis is still pending
    await w.clock.advance(200)
    assert w.transport.flushes == []
    assert w.transport.frames and all(
        f.generation == w.arb.generation and f.uninterruptible for f in w.transport.frames
    )
    assert w.transport.json[1]["interruptible"] is False  # tts.sentence_start


# ----------------------------------------------------- randomized race test


@pytest.mark.parametrize("seed", range(24))
async def test_randomized_race(seed: int):
    w = make_world()
    rng = random.Random(seed)
    drop = {"ended"} if rng.random() < 0.3 else set()
    sim = ClientSim(w.arb, w.transport, w.clock, drop=drop)
    turn_n = 0
    t: TurnId | None = None
    live_streams: list[tuple[OutputRequest, int]] = []
    for _ in range(40):
        a = rng.random()
        if t is None or a < 0.12:
            turn_n += 1
            t = await start_turn(w, turn_n, speaking_done=rng.random() < 0.8)
        elif a < 0.22:
            await w.arb.on_barge_in(t, rng.choice(list(InterruptSource)))
            w.arb.set_user_speaking(rng.random() < 0.3)
        elif a < 0.35:
            await w.arb.submit(cue(w, t, rng.choice(["short", "long"])))
        elif a < 0.55:
            await w.arb.submit(speech(w, t, "好的，" + "明早八点出发。" * rng.randint(1, 2)))
        elif a < 0.65:
            r = stream(w, t, f"resp-{turn_n}-{rng.randint(0, 9)}", f"item-{rng.randint(0, 99)}")
            await w.arb.submit(r)
            live_streams.append((r, w.arb.generation))
        elif a < 0.78:
            await w.arb.submit(
                narration(
                    w,
                    rng.choice(["提醒。", "后台任务完成了。", "进度更新。"]),
                    origin=rng.choice([Origin.REMINDER, Origin.TASK_RESULT, Origin.TASK_PROGRESS]),
                    on_interrupt=rng.choice(["drop", "retry"]),
                    expires_at_ms=w.clock.now_ms() + rng.randint(500, 20_000),
                )
            )
        elif a < 0.88:
            await w.arb.on_reply_done(t, "completed")
            w.arb.set_user_speaking(False)
        elif a < 0.95 and live_streams:
            r, g = rng.choice(live_streams)
            await w.arb.feed_upstream_audio(
                r.request_id,
                tone(rng.randint(40, 400)),
                16000,
                r.content.response_id or "",
                r.content.item_id or "",
                g,
            )
            if rng.random() < 0.3:
                await w.arb.end_upstream_stream(r.request_id)
        else:
            await w.arb.submit(
                build_request(
                    w.ids,
                    Origin.SYSTEM_EXIT,
                    OutputContent(ContentType.TEXT_TTS, text="紧急出口。"),
                    generation=w.arb.generation,
                    turn_id=t,
                )
            )
        await sim.run(rng.choice([0, FRAME_MS, 200, 800, 2500]))
    for r, _ in live_streams:
        await w.arb.end_upstream_stream(r.request_id)
    await w.arb.on_reply_done(t, "completed")
    w.arb.set_user_speaking(False)
    await sim.run(30_000)

    assert sim.overlaps == 0, "frames of two requests interleaved"
    assert sim.overlapping_pairs() == 0
    assert w.trace.counter("double_voice_count") == 0
    assert w.transport.consumer_dropped == 0, "transport second filter should never fire"
    assert w.transport.frames_after_flush(w.arb) == []
    for f in w.transport.frames:  # no frame below the generation current when it was sent
        assert f.generation >= 0
    assert w.arb.state is ArbiterState.IDLE
    assert all(r.terminal or r.status is RequestStatus.QUEUED for r in w.arb._by_id.values())
    await w.arb.close()


def test_arbiter_is_an_arbiter_port():
    w = make_world()
    assert isinstance(w.arb, Arbiter)
    for name in (
        "submit",
        "on_turn_start",
        "on_barge_in",
        "set_user_speaking",
        "set_endpointing",
        "set_awaiting_tool",
        "on_reply_done",
        "on_receipt",
        "flush_origin",
        "feed_upstream_audio",
        "end_upstream_stream",
        "request_for_response",
        "generation",
    ):
        assert hasattr(w.arb, name)


async def test_stale_generation_frames_never_reach_transport():
    """Generation check 3/3 (spec 02 §2.5): if the generation moves while a holder is
    still synthesising (the flush has not reached it yet), its frames are dropped and
    counted; this is the line the revoke path does not cover."""
    w = make_world()
    w.tts.latency_ms = 100
    t = await start_turn(w, 1)
    s = speech(w, t, "明早八点出发。")
    await w.arb.submit(s)
    await settle()
    assert w.arb.holder is not None and w.arb.holder.req is s
    w.arb._bump_generation()  # simulate the race: generation +1 observed before the flush
    await w.clock.advance(500)
    assert w.transport.frames == []
    assert w.transport.consumer_dropped == 0
    assert w.trace.counter("stale_frames_dropped") >= 1
