"""Gateway monotonic clock (spec 06 §2.1).

All timestamps inside the gateway are integer milliseconds from this
clock. Tests inject a `FakeClock` to make timers deterministic.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...

    def epoch_ms(self) -> int: ...

    async def sleep(self, ms: int) -> None: ...


class MonotonicClock:
    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def now_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def epoch_ms(self) -> int:
        return int(time.time() * 1000)

    async def sleep(self, ms: int) -> None:
        await asyncio.sleep(ms / 1000)


class FakeClock:
    """Deterministic clock for tests. `advance()` wakes sleepers whose
    deadline has passed."""

    def __init__(self, start_ms: int = 0, epoch_ms: int = 1_790_000_000_000) -> None:
        self._now = start_ms
        self._epoch0 = epoch_ms
        self._waiters: list[tuple[int, asyncio.Future[None]]] = []

    def now_ms(self) -> int:
        return self._now

    def epoch_ms(self) -> int:
        return self._epoch0 + self._now

    async def sleep(self, ms: int) -> None:
        if ms <= 0:
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((self._now + ms, fut))
        await fut

    async def advance(self, ms: int) -> None:
        target = self._now + ms
        while True:
            due = sorted([(t, f) for t, f in self._waiters if t <= target], key=lambda x: x[0])
            if not due:
                break
            t, f = due[0]
            self._waiters.remove((t, f))
            self._now = max(self._now, t)
            if not f.done():
                f.set_result(None)
            # let the woken coroutine run (and possibly schedule new sleeps)
            for _ in range(3):
                await asyncio.sleep(0)
        self._now = target
        for _ in range(3):
            await asyncio.sleep(0)
