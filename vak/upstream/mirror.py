"""Mirror of the upstream session (spec 04 §1.5, rule 5, §8 "产出骨架时的注意事项").

The mirror is a registry of what the upstream session currently holds:
items (local_id <-> item_id), responses (response_ref <-> response_id and
status), tool calls and which of them already have a result sent. It is
updated only from upstream confirmation events (`on_event`), plus the two
local facts that have no upstream event: a tool result was accepted by the
send primitive, and a seeded history item was handed to `open`.

It is never read as a source of history (rule 5): re-seeding uses the
local context, not this object.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vak.core.types import TurnId
from vak.upstream.base import EventType, ResponseOrigin, ResponseStatus, UpstreamEvent


@dataclass(slots=True)
class MirrorItem:
    item_id: str | None  # None until the upstream echoes it (history.item_id_echo != none)
    local_id: str | None
    kind: str
    seeded: bool = False
    confirmed: bool = False
    audio_end_ms: int | None = None  # set only after a truncate confirmation


@dataclass(slots=True)
class MirrorResponse:
    response_id: str
    response_ref: str | None
    origin: ResponseOrigin
    turn_id: TurnId | None
    status: ResponseStatus | None = None  # None while active


@dataclass(slots=True)
class MirrorCall:
    call_id: str
    name: str
    response_id: str | None
    result_sent: bool = False


@dataclass(slots=True)
class Mirror:
    items: list[MirrorItem] = field(default_factory=list)
    responses: dict[str, MirrorResponse] = field(default_factory=dict)
    calls: dict[str, MirrorCall] = field(default_factory=dict)

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        """Rebuilt on `open`: everything from the previous upstream session is gone."""
        self.items.clear()
        self.responses.clear()
        self.calls.clear()

    def seed(self, local_ids: list[str | None], kinds: list[str]) -> None:
        """Register items handed to `open` as seeded; confirmation comes via item.echo."""
        for local_id, kind in zip(local_ids, kinds, strict=True):
            self.items.append(MirrorItem(item_id=None, local_id=local_id, kind=kind, seeded=True))

    # -- confirmation events -----------------------------------------------

    def on_event(self, ev: UpstreamEvent) -> None:
        """Apply one upstream event. Unknown/irrelevant types are ignored."""
        if ev.type is EventType.ITEM_ECHO and ev.item_id is not None:
            self._confirm_item(ev.item_id, ev.local_id, ev.kind or "item", ev.audio_ms)
        elif ev.type is EventType.RESPONSE_STARTED and ev.response_id is not None:
            self.responses[ev.response_id] = MirrorResponse(
                response_id=ev.response_id,
                response_ref=ev.response_ref,
                origin=ev.origin or ResponseOrigin.CLIENT,
                turn_id=ev.turn_id,
            )
        elif ev.type is EventType.RESPONSE_DONE and ev.response_id is not None:
            r = self.responses.get(ev.response_id)
            if r is not None:
                r.status = ev.status
        elif ev.type is EventType.TOOL_CALL and ev.call_id is not None:
            self.calls.setdefault(
                ev.call_id, MirrorCall(call_id=ev.call_id, name=ev.name or "", response_id=ev.response_id)
            )

    def _confirm_item(self, item_id: str, local_id: str | None, kind: str, audio_end_ms: int | None) -> None:
        if kind == "truncated":
            it = self.item(item_id)
            if it is None:  # the upstream confirmed the item exists by truncating it
                it = MirrorItem(item_id=item_id, local_id=None, kind="assistant", confirmed=True)
                self.items.append(it)
            it.audio_end_ms = audio_end_ms
            return
        if local_id is not None:
            for it in self.items:
                if it.local_id == local_id and it.item_id is None:
                    it.item_id, it.confirmed = item_id, True
                    return
        else:
            # no id echo: align seeded items by order (history.item_id_echo = none)
            for it in self.items:
                if it.seeded and it.item_id is None:
                    it.item_id, it.confirmed = item_id, True
                    return
        self.items.append(MirrorItem(item_id=item_id, local_id=local_id, kind=kind, confirmed=True))

    # -- local facts without an upstream event ------------------------------

    def bind_ref(self, response_id: str, response_ref: str) -> None:
        r = self.responses.get(response_id)
        if r is not None and r.response_ref is None:
            r.response_ref = response_ref

    def mark_result_sent(self, call_id: str) -> bool:
        """Record that a result was sent; False if it was already sent (duplicate)."""
        c = self.calls.get(call_id)
        if c is None:
            return False
        if c.result_sent:
            return False
        c.result_sent = True
        return True

    # -- queries ------------------------------------------------------------

    def item(self, item_id: str) -> MirrorItem | None:
        return next((it for it in self.items if it.item_id == item_id), None)

    def response_by_ref(self, response_ref: str) -> MirrorResponse | None:
        return next((r for r in self.responses.values() if r.response_ref == response_ref), None)

    def active_responses(self) -> list[MirrorResponse]:
        return [r for r in self.responses.values() if r.status is None]

    def has_call(self, call_id: str) -> bool:
        return call_id in self.calls

    def result_sent(self, call_id: str) -> bool:
        c = self.calls.get(call_id)
        return bool(c and c.result_sent)
