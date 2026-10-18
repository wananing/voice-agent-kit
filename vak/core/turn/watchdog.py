"""Per-turn watchdogs (spec 01 §2.5).

One cancellable task per timer, all driven by the injected clock. Expiry
is reported to a single callback so the turn machine has one place that
turns "timer fired" into "send error → send turn.closed" (spec 01 §7).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from vak.core.clock import Clock
from vak.core.types import Watchdog

log = logging.getLogger(__name__)

# Keys are `Watchdog` members (spec 06 §3.9 names) or a local name such as "T_hold".
ExpiryHandler = Callable[[str], Awaitable[None]]


class WatchdogSet:
    def __init__(self, clock: Clock, on_expire: ExpiryHandler) -> None:
        self._clock = clock
        self._on_expire = on_expire
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, which: Watchdog | str, delay_ms: int) -> None:
        """(Re)start one timer; an already running timer of the same name is replaced."""
        self.cancel(which)
        deadline = self._clock.now_ms() + delay_ms  # fixed now, not when the task first runs
        self._tasks[which] = asyncio.get_running_loop().create_task(self._run(which, deadline))

    def cancel(self, which: Watchdog | str) -> None:
        task = self._tasks.pop(which, None)
        if task is not None and not task.done():
            task.cancel()

    def cancel_all(self) -> None:
        for which in list(self._tasks):
            self.cancel(which)

    def active(self) -> set[str]:
        return {w for w, t in self._tasks.items() if not t.done()}

    async def _run(self, which: Watchdog | str, deadline_ms: int) -> None:
        try:
            await self._clock.sleep(max(0, deadline_ms - self._clock.now_ms()))
        except asyncio.CancelledError:
            return
        self._tasks.pop(which, None)
        try:
            await self._on_expire(which)
        except Exception:  # noqa: BLE001
            log.exception("watchdog %s handler failed", which)
