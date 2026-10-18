"""Spec 01 §4.3 / rule 12 (acceptance 15): pace, lead bound, re-align, flush, generation filter."""

from __future__ import annotations

import asyncio

from vak.core.clock import FakeClock
from vak.core.types import TurnId
from vak.transport.pacer import DownlinkPacer, PacedFrame

LEAD = 300
FRAME = 20


def frame(idx: int, gen: int = 1, turn: int = 1) -> PacedFrame:
    return PacedFrame(b"x", FRAME, gen, TurnId("c", turn), 0, idx)


async def settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_never_ahead_of_lead_plus_one_frame() -> None:
    clock = FakeClock()
    sent: list[tuple[int, PacedFrame]] = []

    async def send(f: PacedFrame) -> None:
        sent.append((clock.now_ms(), f))

    pacer = DownlinkPacer(clock, send, LEAD)
    for i in range(100):
        assert pacer.push(frame(i))
    pacer.start()
    await settle()
    assert pacer.frames_sent == LEAD // FRAME  # pre-buffer of one lead (R4.3-2), not the whole queue (R4.3-5)
    for _ in range(200):
        await clock.advance(5)
        ahead = pacer.frames_sent * FRAME - clock.now_ms()
        assert ahead <= LEAD + FRAME
        assert pacer.lead_now_ms() <= LEAD
        assert ahead >= LEAD - FRAME  # keeps real-time pace, does not fall behind
    assert pacer.frames_sent == 15 + 1000 // FRAME
    assert [f.frame_idx for _, f in sent] == list(range(pacer.frames_sent))  # in order
    await pacer.stop()


async def test_realigns_after_stall_instead_of_bursting() -> None:
    clock = FakeClock()
    sent: list[tuple[int, PacedFrame]] = []
    stall = {"on": False}

    async def send(f: PacedFrame) -> None:
        if stall["on"]:
            stall["on"] = False
            await clock.sleep(1000)  # the socket blocks for 1 s
        sent.append((clock.now_ms(), f))

    pacer = DownlinkPacer(clock, send, LEAD)
    for i in range(200):
        pacer.push(frame(i))
    pacer.start()
    await settle()
    assert pacer.frames_sent == 15
    stall["on"] = True
    await clock.advance(FRAME)  # frame 15 pops and blocks
    await clock.advance(1000)  # unblocks at t=1020
    at_resume = [t for t, _ in sent if t == 1020]
    # R4.3-3: after the stall only one lead's worth goes out at once, the 50-frame backlog is not burst
    assert len(at_resume) <= LEAD // FRAME + 1
    before = pacer.frames_sent
    await clock.advance(500)
    assert pacer.frames_sent - before == 500 // FRAME
    await pacer.stop()


async def test_generation_filter_drops_stale_and_counts() -> None:
    clock = FakeClock()
    sent: list[PacedFrame] = []

    async def send(f: PacedFrame) -> None:
        sent.append(f)

    pacer = DownlinkPacer(clock, send, LEAD)
    pacer.set_generation(2)
    assert pacer.push(frame(0, gen=1)) is False  # filtered at push
    assert pacer.consumer_dropped_frames == 1
    for i in range(3):
        assert pacer.push(frame(i, gen=2))
    pacer.set_generation(3)  # barge-in after the frames were queued
    pacer.start()
    await settle()
    assert sent == [] and pacer.consumer_dropped_frames == 4  # filtered again at send time
    assert pacer.push(frame(0, gen=3))
    await settle()
    assert len(sent) == 1
    await pacer.stop()


async def test_flush_clears_queue_synchronously() -> None:
    clock = FakeClock()
    sent: list[PacedFrame] = []

    async def send(f: PacedFrame) -> None:
        sent.append(f)

    pacer = DownlinkPacer(clock, send, LEAD)
    for i in range(50):
        pacer.push(frame(i))
    pacer.start()
    await settle()
    assert len(sent) == 15
    assert pacer.flush() == 35  # no await: same tick as flush.start
    assert pacer.queued_frames == 0 and pacer.frames_flushed == 35
    await clock.advance(2000)
    assert len(sent) == 15  # nothing of the flushed turn ever reaches the wire
    for i in range(20):
        pacer.push(frame(i, turn=2))
    await settle()
    assert len(sent) == 15 + 15  # new timeline: pre-buffer allowed again
    await pacer.stop()
