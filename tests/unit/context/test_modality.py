"""Spec 05 §3.5: text_only counter per upstream session."""

from __future__ import annotations

import asyncio

from vak.core.bus import Bus
from vak.core.context.modality import ModalityMonitor
from vak.core.defaults import ContextParams
from vak.core.types import RebuildReason, ReplyModality


async def test_schedules_rebuild_after_two_text_only_replies():
    calls: list[RebuildReason] = []

    async def rebuild(reason: RebuildReason) -> None:
        calls.append(reason)

    bus = Bus()
    m = ModalityMonitor(ContextParams(), rebuild, bus)
    m.start()
    assert m.on_response_done(ReplyModality.AUDIO) is False
    assert m.on_response_done("text_only") is False and m.text_only_count == 1
    assert m.on_response_done(ReplyModality.TEXT_ONLY) is True
    await asyncio.sleep(0)
    assert calls == [RebuildReason.MODALITY] and m.text_only_count == 0


async def test_counter_resets_on_session_rebuilt():
    async def rebuild(reason: RebuildReason) -> None: ...

    bus = Bus()
    m = ModalityMonitor(ContextParams(), rebuild, bus)
    m.start()
    m.on_response_done(ReplyModality.TEXT_ONLY)
    await bus.emit("session.rebuilt", session_seq=2, reason="disconnect")
    assert m.text_only_count == 0
    assert m.on_response_done(ReplyModality.TEXT_ONLY) is False
