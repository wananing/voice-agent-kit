"""Spec 01 §8 items 1, 2, 9 in process: random turn sequences with interrupts,
disconnects, timeouts and late upstream output. Every turn.start has exactly
one turn.closed and no downlink audio follows it."""

from __future__ import annotations

import random

import pytest

from tests.unit.turn.harness import Harness, make
from tests.unit.turn.harness import ms_to_samples as S
from vak.core.turn.machine import ResumeInfo
from vak.core.types import TurnId
from vak.upstream.base import EventType, ResponseOrigin, ResponseStatus

TURNS_PER_SEED = 12
SEEDS = 20  # 240 turns in total


async def _drive_turn(h: Harness, rng: random.Random, n: int, ts: int) -> int:
    tid = f"c{n}"
    rid = f"r{n}"
    await h.start(tid, ts=ts)
    for _ in range(rng.randint(0, 3)):
        ts += S(20)
        await h.audio(ts, turn_u32=n)
    if rng.random() < 0.1:
        await h.ev(EventType.INPUT_SEGMENTED)
    if rng.random() < 0.08:
        await h.response_started(f"srv{n}", origin=ResponseOrigin.SERVER_VAD)  # before key-up
    r = rng.random()
    if r < 0.1:
        await h.end(tid, ts=ts + S(50))  # short press
        return ts + S(100)
    ts += S(rng.randint(400, 3000))
    await h.end(tid, ts=ts)
    r = rng.random()
    if r < 0.08:
        await h.clock.advance(3500)  # T_asr
        return ts
    if r < 0.15:
        await h.transcript_final("")  # no_speech
        return ts
    await h.transcript_final("hi")
    if rng.random() < 0.1:
        await h.clock.advance(9000)  # T_respond
        return ts
    await h.response_started(rid)
    await h.audio_delta(rid, ms=rng.randint(100, 1500))
    r = rng.random()
    if r < 0.25:
        # button barge-in right away, possibly followed by the next turn in the loop
        if rng.random() < 0.7:
            await h.receipt("interrupted", tid, played_ms=rng.randint(0, 500), reason="button")
        await h.abort(tid, "button")
        await h.clock.advance(rng.choice([0, 100, 400]))
        await h.audio_delta(rid)  # late delta after abort
        return ts
    if r < 0.35:
        await h.machine.on_resume(ResumeInfo(ok=True, disconnected_at_ms=h.clock.now_ms()))  # link drop
        if rng.random() < 0.5:
            await h.receipt("interrupted", tid, played_ms=100, reason="link")
        else:
            await h.clock.advance(400)
        return ts
    if r < 0.45:
        return ts  # leave it responding: the next turn.start interrupts it
    if rng.random() < 0.1:
        await h.response_done(rid, status=ResponseStatus.FAILED)
        return ts
    await h.response_done(rid)
    await h.audio_delta(rid)  # late delta after done: ignored
    if rng.random() < 0.6:
        await h.receipt("ended", tid, played_ms=200)
        await h.drained(tid)
    else:
        await h.clock.advance(5000)  # T_drain
    return ts


@pytest.mark.parametrize("seed", range(SEEDS))
async def test_no_dangling_turns_and_no_audio_after_closed(seed: int):
    rng = random.Random(seed)
    h = make()
    ts = 0
    for n in range(1, TURNS_PER_SEED + 1):
        ts = await _drive_turn(h, rng, n, ts + S(500))
        await h.clock.advance(rng.randint(0, 500))
    await h.clock.advance(1000)
    await h.machine.close_session("hangup")

    started = [m["turn_id"] for m in h.transport.of_type("turn.start")]  # downlink turn.start (none in ptt)
    started += [f"c{n}" for n in range(1, TURNS_PER_SEED + 1)]
    closed = [m["turn_id"] for m in h.transport.of_type("turn.closed")]
    assert sorted(closed) == sorted(started), "every turn.start has exactly one turn.closed"

    closed_at: dict[str, int] = {}
    for i, (kind, item) in enumerate(h.transport.log):
        if kind == "json" and item["type"] == "turn.closed":
            closed_at[item["turn_id"]] = i
    for i, (kind, item) in enumerate(h.transport.log):
        if kind == "audio":
            tid, _ = item
            assert tid not in closed_at or i < closed_at[tid], f"audio for {tid} after its turn.closed"
    # also no JSON frames for a turn after its turn.closed (R2.4-3)
    for i, (kind, item) in enumerate(h.transport.log):
        if kind == "json" and item.get("turn_id") in closed_at and item["type"] != "turn.closed":
            assert i < closed_at[item["turn_id"]], f"{item['type']} for {item['turn_id']} after turn.closed"

    for t in h.machine._turns.values():
        assert t.terminal and t.close_sent
        tr = h.trace.merged(t.turn_id)
        assert tr["turn_terminal"] == t.state.value and "turn_closed_by" in tr and "generation" in tr
    assert h.machine.state.value == "idle"
    assert all(isinstance(t, TurnId) for t in h.tools.closed)
