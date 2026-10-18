"""WatchdogSet: clock driven, cancellable, replaces same-name timers."""

from __future__ import annotations

from vak.core.clock import FakeClock
from vak.core.turn.watchdog import WatchdogSet
from vak.core.types import Watchdog


async def test_fires_at_deadline_and_can_be_cancelled():
    clock = FakeClock()
    fired: list[str] = []

    async def on_expire(which: str) -> None:
        fired.append(which)

    wd = WatchdogSet(clock, on_expire)
    wd.start(Watchdog.T_ASR, 3000)
    wd.start(Watchdog.T_RESPOND, 8000)
    await clock.advance(2999)
    assert fired == []
    await clock.advance(1)
    assert fired == [Watchdog.T_ASR]
    wd.cancel(Watchdog.T_RESPOND)
    await clock.advance(10_000)
    assert fired == [Watchdog.T_ASR]
    assert wd.active() == set()


async def test_restart_replaces_and_cancel_all():
    clock = FakeClock()
    fired: list[str] = []

    async def on_expire(which: str) -> None:
        fired.append(str(which))

    wd = WatchdogSet(clock, on_expire)
    wd.start(Watchdog.T_DRAIN, 1000)
    await clock.advance(500)
    wd.start(Watchdog.T_DRAIN, 1000)  # restart: old deadline no longer counts
    await clock.advance(600)
    assert fired == []
    wd.start("T_hold", 50)
    wd.cancel_all()
    await clock.advance(5000)
    assert fired == []
