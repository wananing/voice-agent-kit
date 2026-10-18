"""Spec 05 §8.2: snapshot on turn terminal, TTL, restore."""

from __future__ import annotations

from vak.core.bus import Bus
from vak.core.clock import FakeClock
from vak.core.context.snapshot import SnapshotStore, apply_snapshot
from vak.core.defaults import ContextParams
from vak.core.types import Summary, WriteEffect

from .fakes import chat_turn, make_call, make_store, tid


async def test_written_on_turn_closed_and_restorable(tmp_path):
    clock = FakeClock()
    params = ContextParams(snapshot_recent_turns=2)
    store = make_store(clock=clock, params=params)
    bus = Bus()
    snaps = SnapshotStore(params, clock, tmp_path)
    snaps.bind(bus, store, business_state=lambda: {"stage": "confirm"})
    for n in range(1, 4):
        chat_turn(store, n, f"问题{n}", f"回答{n}")
    store.record_tool_call(make_call(3, "w-1", "submit", closed_by=None), WriteEffect("k", True, "r"))
    store.state.update("user_name", "小明")
    store.state.apply_pending()
    store.set_summary(Summary(True, (store.entries[0].entry_id, store.entries[1].entry_id), "前情", 5))
    store.begin_upstream_session(3)
    assert snaps.restore("us-1") is None
    await bus.emit("turn.closed", turn_id=tid(3))
    snap = snaps.restore("us-1")
    assert snap is not None
    assert {str(e.turn_id) for e in snap.recent_entries} == {"c2", "c3"}
    assert snap.business_state == {"stage": "confirm"} and snap.session_seq == 3
    assert snap.state_values["user_name"] == "小明" and snap.state_rev == 1 and snap.schema_version == "2"
    assert snap.summary is not None and snap.summary.text == "前情"
    assert snap.executed_writes == [{"idempotency_key": "k", "result": "r"}]
    assert snap.pending_tool_calls == ["w-1"]
    assert snap.persona_version == "3" and snap.tools_version == "7"

    # a fresh process: restore from the JSON file into a fresh store
    other = SnapshotStore(params, clock, tmp_path)
    restored = other.restore("us-1")
    assert restored is not None
    fresh = make_store(clock=clock, params=params)
    apply_snapshot(fresh, restored)
    assert fresh.session_seq == 3 and fresh.state.render().splitlines()[1] == "称呼：小明。"
    assert fresh.presented_text(tid(3)) == "回答3"
    assert fresh.summary is not None and fresh.summary.covers == store.summary.covers
    assert fresh.entries[-1].write_effect == WriteEffect("k", True, "r")


async def test_ttl_is_recovery_window():
    clock = FakeClock()
    params = ContextParams(recovery_window_ms=1000)
    store = make_store(clock=clock, params=params)
    bus = Bus()
    snaps = SnapshotStore(params, clock)
    snaps.bind(bus, store)
    chat_turn(store, 1, "a", "b")
    await bus.emit("turn.closed", turn_id=tid(1))
    await clock.advance(999)
    assert snaps.restore("us-1") is not None
    await clock.advance(2)
    assert snaps.restore("us-1") is None
