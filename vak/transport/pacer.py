"""Downlink pacer (spec 01 §4.3; handbook turn-protocol rule 12).

Sends audio at real-time pace, never more than `lead_ms` ahead of the
audio timeline. A new timeline starts whenever the pacer was idle or fell
behind (slow socket, no input): it re-aligns to "now" and may pre-buffer up
to `lead_ms` at once (R4.3-2), but never bursts the backlog (R4.3-3).

Also the transport's own second-level generation filter (spec 02 rule 11):
frames whose generation is below the current one are dropped at push and
again at send, counted in `consumer_dropped_frames`.

All waiting goes through the injected Clock so tests are deterministic.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from vak.core.clock import Clock
from vak.core.types import TurnId

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PacedFrame:
    payload: bytes  # already encoded with the negotiated codec
    duration_ms: int
    generation: int
    turn_id: TurnId
    sentence_id: int
    frame_idx: int
    uninterruptible: bool = False


class DownlinkPacer:
    def __init__(self, clock: Clock, send: Callable[[PacedFrame], Awaitable[None]], lead_ms: int) -> None:
        self._clock = clock
        self._send = send
        self.lead_ms = lead_ms
        self._queue: deque[PacedFrame] = deque()
        self._wakeup = asyncio.Event()
        self._generation = 0
        self._anchor_ms: int | None = None  # wall time at which the current timeline started
        self._timeline_ms = 0  # audio ms sent since the anchor
        self._task: asyncio.Task[None] | None = None
        self.consumer_dropped_frames = 0  # spec 02 rule 11 counter
        self.frames_sent = 0
        self.frames_flushed = 0

    # -- generation --------------------------------------------------------

    @property
    def generation(self) -> int:
        return self._generation

    def set_generation(self, generation: int) -> None:
        self._generation = generation

    # -- queue -------------------------------------------------------------

    def push(self, frame: PacedFrame) -> bool:
        """Queue a frame; False (and counted) if its generation is stale."""
        if frame.generation < self._generation:
            self.consumer_dropped_frames += 1
            return False
        self._queue.append(frame)
        self._wakeup.set()
        return True

    def flush(self) -> int:
        """Drop everything not yet sent, synchronously. Returns the count."""
        n = len(self._queue)
        self._queue.clear()
        self.frames_flushed += n
        self._anchor_ms = None  # next frame starts a fresh timeline
        return n

    @property
    def queued_frames(self) -> int:
        return len(self._queue)

    @property
    def queued_ms(self) -> int:
        return sum(f.duration_ms for f in self._queue)

    async def wait_drained(self) -> None:
        """Return once the queue is empty (sent or flushed). Used before `tts.stop{done}`
        so the stop frame follows the last audio frame on the wire."""
        while self._queue:
            await self._clock.sleep(5)

    def lead_now_ms(self) -> int:
        """How far the sent audio is ahead of real time right now (>= 0)."""
        if self._anchor_ms is None:
            return 0
        return max(0, self._anchor_ms + self._timeline_ms - self._clock.now_ms())

    # -- loop ----------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run(self) -> None:
        while True:
            if not self._queue:
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            frame = self._queue[0]
            if frame.generation < self._generation:  # second-level filter at send time
                self._queue.popleft()
                self.consumer_dropped_frames += 1
                continue
            now = self._clock.now_ms()
            if self._anchor_ms is None or self._anchor_ms + self._timeline_ms < now:
                # idle or fell behind: re-align instead of bursting (spec 01 R4.3-3)
                self._anchor_ms = now
                self._timeline_ms = 0
            ahead_after = self._anchor_ms + self._timeline_ms + frame.duration_ms - now
            if ahead_after > self.lead_ms:
                await self._clock.sleep(ahead_after - self.lead_ms)
                continue  # re-check: the queue may have been flushed meanwhile
            self._queue.popleft()
            self._timeline_ms += frame.duration_ms
            await self._send(frame)
            self.frames_sent += 1
