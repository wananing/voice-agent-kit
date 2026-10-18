"""Cross-connection snapshot (spec 05 §8.2).

Written after every turn terminal (`turn.closed`), TTL = recovery window.
Contains no upstream mirror and no audio. In-memory with an optional JSON
file directory so a restarted gateway can still restore within the window.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vak.core.bus import Bus
from vak.core.clock import Clock
from vak.core.defaults import ContextParams
from vak.core.types import (
    ClosedBy,
    ContextEntry,
    EntrySource,
    InterruptAt,
    PlaybackState,
    PresentedVia,
    Role,
    Summary,
    TurnId,
    UpstreamSynced,
    WriteEffect,
)

if TYPE_CHECKING:
    from vak.core.context.store import ContextStore

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Snapshot:
    user_session_id: str
    written_at_epoch_ms: int
    business_state: dict[str, Any]
    state_values: dict[str, str | None]
    state_text: str
    schema_version: str
    state_rev: int
    recent_entries: list[ContextEntry]
    summary: Summary | None
    pending_tool_calls: list[str]
    executed_writes: list[dict[str, Any]]
    session_seq: int
    persona_version: str
    tools_version: str
    form: dict[str, Any] | None = None  # profile B only

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["recent_entries"] = [_entry_to_dict(e) for e in self.recent_entries]
        d["summary"] = dataclasses.asdict(self.summary) if self.summary else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Snapshot:
        d = dict(d)
        d["recent_entries"] = [_entry_from_dict(x) for x in d["recent_entries"]]
        s = d.get("summary")
        d["summary"] = Summary(s["is_summary"], tuple(s["covers"]), s["text"], s["created_at_ms"]) if s else None
        return cls(**d)


def take_snapshot(
    context: ContextStore,
    params: ContextParams,
    business_state: dict[str, Any] | None = None,
    form: dict[str, Any] | None = None,
) -> Snapshot:
    turns = context.turn_ids()
    keep = {str(t) for t in turns[-params.snapshot_recent_turns:]}
    return Snapshot(
        user_session_id=context.user_session_id,
        written_at_epoch_ms=context.clock.epoch_ms(),
        business_state=dict(business_state or {}),
        state_values=context.state.values(),
        state_text=context.state.render(),
        schema_version=context.state.schema.schema_version,
        state_rev=context.state.state_rev,
        recent_entries=[e for e in context.entries if str(e.turn_id) in keep],
        summary=context.summary,
        pending_tool_calls=context.pending_tool_calls(),
        executed_writes=context.executed_writes(),
        session_seq=context.session_seq,
        persona_version=context.persona.version,
        tools_version=context.tools_version,
        form=form,
    )


def apply_snapshot(context: ContextStore, snap: Snapshot) -> None:
    """Restore a context store from a snapshot (same user session)."""
    context.entries = list(snap.recent_entries)
    context.summary = snap.summary
    context.session_seq = snap.session_seq
    context.state.restore(snap.state_values, snap.state_rev)


class SnapshotStore:
    def __init__(self, params: ContextParams, clock: Clock, directory: str | Path | None = None) -> None:
        self.params = params
        self.clock = clock
        self.directory = Path(directory) if directory else None
        self._mem: dict[str, Snapshot] = {}
        self._bound: list[tuple[Bus, Any]] = []

    def write(self, snap: Snapshot) -> None:
        self._mem[snap.user_session_id] = snap
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"{snap.user_session_id}.json"
            path.write_text(json.dumps(snap.to_dict(), ensure_ascii=False), encoding="utf-8")

    def restore(self, user_session_id: str) -> Snapshot | None:
        """Latest snapshot within the recovery window, else None."""
        snap = self._mem.get(user_session_id)
        if snap is None and self.directory is not None:
            path = self.directory / f"{user_session_id}.json"
            if path.exists():
                snap = Snapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if snap is None:
            return None
        if self.clock.epoch_ms() - snap.written_at_epoch_ms > self.params.recovery_window_ms:
            return None
        return snap

    def bind(self, bus: Bus, context: ContextStore, business_state: Any = None) -> None:
        """Write a snapshot on every turn terminal (spec 05 §8.2)."""

        async def on_closed(_payload: dict[str, Any]) -> None:
            state = business_state() if callable(business_state) else business_state
            self.write(take_snapshot(context, self.params, state))

        bus.on("turn.closed", on_closed)
        self._bound.append((bus, on_closed))

    def unbind(self) -> None:
        for bus, h in self._bound:
            bus.off("turn.closed", h)
        self._bound.clear()


# ----------------------------------------------------------------- serialise

_ENUMS = {
    "role": Role,
    "source": EntrySource,
    "presented_via": PresentedVia,
    "playback_state": PlaybackState,
    "tool_closed_by": ClosedBy,
    "upstream_synced": UpstreamSynced,
}


def _entry_to_dict(e: ContextEntry) -> dict[str, Any]:
    d = dataclasses.asdict(e)
    d["turn_id"] = str(e.turn_id)
    for k in _ENUMS:
        if d[k] is not None:
            d[k] = d[k].value
    return d


def _entry_from_dict(d: dict[str, Any]) -> ContextEntry:
    d = dict(d)
    d["turn_id"] = TurnId.parse(d["turn_id"])
    for k, enum_cls in _ENUMS.items():
        if d.get(k) is not None:
            d[k] = enum_cls(d[k])
    if d.get("interrupt_at"):
        d["interrupt_at"] = InterruptAt(**d["interrupt_at"])
    if d.get("write_effect"):
        d["write_effect"] = WriteEffect(**d["write_effect"])
    return ContextEntry(**d)
