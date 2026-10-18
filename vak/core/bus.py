"""In-process async event bus for one session.

Event names follow the specs (turn.start, user.barge_in, turn.endpointing,
turn.awaiting_tool, turn.reply_done, turn.audio_drained, turn.no_output,
...). Handlers run in subscription order; exceptions are logged and do
not stop other handlers.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class Bus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)
        self._history: list[tuple[str, dict[str, Any]]] = []
        self.record = False  # tests flip this to inspect emitted events

    def on(self, name: str, handler: Handler) -> None:
        self._subs[name].append(handler)

    def off(self, name: str, handler: Handler) -> None:
        if handler in self._subs.get(name, []):
            self._subs[name].remove(handler)

    async def emit(self, name: str, **payload: Any) -> None:
        if self.record:
            self._history.append((name, dict(payload)))
        for h in list(self._subs.get(name, [])):
            try:
                await h(payload)
            except Exception:  # noqa: BLE001
                log.exception("bus handler for %s failed", name)

    def emit_soon(self, name: str, **payload: Any) -> None:
        asyncio.get_running_loop().create_task(self.emit(name, **payload))

    @property
    def history(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self._history)
