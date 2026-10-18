"""Forwarding proxies so modules can hold one reference while the session
swaps the real object underneath: the upstream changes on every session
rebuild (spec 05 §4), the transport changes on every reconnect (spec 01 §6).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from vak.upstream.base import UpstreamEvent

log = logging.getLogger(__name__)


class Forwarder:
    """Attribute access goes to the current target."""

    def __init__(self, target: Any = None) -> None:
        object.__setattr__(self, "_target", target)

    @property
    def target(self) -> Any:
        return object.__getattribute__(self, "_target")

    def set_target(self, target: Any) -> None:
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name: str) -> Any:
        target = object.__getattribute__(self, "_target")
        if target is None:
            raise AttributeError(f"proxy has no target (asked for {name})")
        return getattr(target, name)


class UpstreamProxy(Forwarder):
    """One `events()` stream across successive real upstreams. The session
    calls `set_target(new)` after a rebuild; the pump of the old upstream is
    cancelled and a new one started, the consumer never notices."""

    def __init__(self, target: Any = None) -> None:
        super().__init__(None)
        object.__setattr__(self, "_queue", asyncio.Queue())
        object.__setattr__(self, "_pump", None)
        object.__setattr__(self, "_closed", False)
        if target is not None:
            self.set_target(target)

    def set_target(self, target: Any) -> None:
        pump: asyncio.Task[None] | None = object.__getattribute__(self, "_pump")
        if pump is not None and not pump.done():
            pump.cancel()
        super().set_target(target)
        if target is not None:
            object.__setattr__(self, "_pump", asyncio.get_running_loop().create_task(self._run_pump(target)))

    async def _run_pump(self, target: Any) -> None:
        queue: asyncio.Queue[UpstreamEvent | None] = object.__getattribute__(self, "_queue")
        try:
            async for ev in target.events():
                await queue.put(ev)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("upstream event pump failed")

    async def events(self) -> AsyncIterator[UpstreamEvent]:
        queue: asyncio.Queue[UpstreamEvent | None] = object.__getattribute__(self, "_queue")
        while True:
            ev = await queue.get()
            if ev is None:
                return
            yield ev

    async def shutdown(self) -> None:
        object.__setattr__(self, "_closed", True)
        pump: asyncio.Task[None] | None = object.__getattribute__(self, "_pump")
        if pump is not None and not pump.done():
            pump.cancel()
        queue: asyncio.Queue[UpstreamEvent | None] = object.__getattribute__(self, "_queue")
        await queue.put(None)


class TransportProxy(Forwarder):
    """Forwarder with a hook on every downlink audio frame (the rebuilder's
    1 s quiet rule, spec 05 §4.3)."""

    def __init__(self, target: Any = None, on_audio_sent: Callable[[], None] | None = None) -> None:
        super().__init__(target)
        object.__setattr__(self, "_on_audio_sent", on_audio_sent)

    async def send_audio(self, *args: Any, **kwargs: Any) -> bool:
        hook = object.__getattribute__(self, "_on_audio_sent")
        if hook is not None:
            hook()
        return await self.target.send_audio(*args, **kwargs)

    @property
    def lead_ms(self) -> int:
        return self.target.lead_ms

    @property
    def sample_rate(self) -> int:
        return self.target.sample_rate


OnAudio = Callable[[int, int, int, int, bytes], Awaitable[None]]
