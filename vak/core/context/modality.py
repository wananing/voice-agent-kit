"""Reply-modality monitor (spec 05 §3.5).

Counts `text_only` replies per upstream session; at the threshold it
schedules a proactive rebuild (`rebuild_reason=modality`) and resets.
Speaking the text through local TTS is the arbiter's job, not ours.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from vak.core.bus import Bus
from vak.core.defaults import ContextParams
from vak.core.types import RebuildReason, ReplyModality

log = logging.getLogger(__name__)


class ModalityMonitor:
    def __init__(
        self, params: ContextParams, rebuild: Callable[[RebuildReason], Awaitable[Any]], bus: Bus | None = None
    ) -> None:
        self.params = params
        self._rebuild = rebuild
        self._bus = bus
        self.text_only_count = 0
        self._pending: asyncio.Task[Any] | None = None

    def start(self) -> None:
        if self._bus is not None:
            self._bus.on("session.rebuilt", self._on_rebuilt)

    def stop(self) -> None:
        if self._bus is not None:
            self._bus.off("session.rebuilt", self._on_rebuilt)

    def on_response_done(self, reply_modality: ReplyModality | str) -> bool:
        """Returns True when this reply tipped the counter and a rebuild was scheduled."""
        if ReplyModality(reply_modality) is not ReplyModality.TEXT_ONLY:
            return False
        self.text_only_count += 1
        log.warning("text_only reply in audio session (%d so far)", self.text_only_count)
        if self.text_only_count < self.params.modality_anomaly_rebuild_after:
            return False
        if self._pending is None or self._pending.done():
            self._pending = asyncio.get_running_loop().create_task(self._rebuild(RebuildReason.MODALITY))
        self.text_only_count = 0
        return True

    async def _on_rebuilt(self, _payload: dict[str, Any]) -> None:
        self.text_only_count = 0
