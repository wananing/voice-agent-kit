"""Mirror only changes on upstream confirmation events (spec 04 rule 5)."""

from __future__ import annotations

from vak.upstream.base import EventType, ResponseOrigin, ResponseStatus, UpstreamEvent
from vak.upstream.mirror import Mirror


def ev(t: EventType, **f: object) -> UpstreamEvent:
    return UpstreamEvent(type=t, seq=0, t_local_ms=0, **f)  # type: ignore[arg-type]


def test_seeded_items_unconfirmed_until_echo() -> None:
    m = Mirror()
    m.seed(["e1", "e2"], ["user", "assistant"])
    assert all(not it.confirmed and it.item_id is None for it in m.items)
    m.on_event(ev(EventType.ITEM_ECHO, item_id="srv-1", local_id="e1", kind="user"))
    assert m.items[0].confirmed and m.items[0].item_id == "srv-1"
    assert not m.items[1].confirmed


def test_echo_without_local_id_aligns_by_order() -> None:
    m = Mirror()
    m.seed([None, None], ["user", "assistant"])
    m.on_event(ev(EventType.ITEM_ECHO, item_id="a", kind="user"))
    m.on_event(ev(EventType.ITEM_ECHO, item_id="b", kind="assistant"))
    assert [it.item_id for it in m.items] == ["a", "b"]


def test_response_lifecycle_from_events() -> None:
    m = Mirror()
    m.on_event(ev(EventType.RESPONSE_STARTED, response_id="r1", response_ref="rr1", origin=ResponseOrigin.CLIENT))
    assert m.response_by_ref("rr1") is not None and m.active_responses()
    m.on_event(ev(EventType.RESPONSE_DONE, response_id="r1", status=ResponseStatus.COMPLETED))
    assert not m.active_responses() and m.responses["r1"].status is ResponseStatus.COMPLETED


def test_tool_result_duplicate_detection() -> None:
    m = Mirror()
    assert not m.mark_result_sent("c1")  # unknown call: nothing to mark
    m.on_event(ev(EventType.TOOL_CALL, call_id="c1", name="t", response_id="r1"))
    assert m.mark_result_sent("c1")
    assert not m.mark_result_sent("c1")  # duplicate
    assert m.result_sent("c1")


def test_truncate_only_after_confirmation() -> None:
    m = Mirror()
    m.on_event(ev(EventType.ITEM_ECHO, item_id="i1", kind="assistant"))
    assert m.item("i1") is not None and m.item("i1").audio_end_ms is None
    m.on_event(ev(EventType.ITEM_ECHO, item_id="i1", kind="truncated", audio_ms=1200))
    assert m.item("i1").audio_end_ms == 1200


def test_non_confirmation_events_do_not_touch_mirror() -> None:
    m = Mirror()
    m.on_event(ev(EventType.RESPONSE_AUDIO_DELTA, response_id="r9", item_id="i9"))
    m.on_event(ev(EventType.TRANSCRIPT_USER_FINAL, text="x"))
    assert not m.items and not m.responses and not m.calls


def test_reset_rebuilds_on_open() -> None:
    m = Mirror()
    m.seed(["e1"], ["user"])
    m.on_event(ev(EventType.TOOL_CALL, call_id="c1", name="t"))
    m.reset()
    assert not m.items and not m.calls
