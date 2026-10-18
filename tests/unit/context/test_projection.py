"""Spec 05 §3 projection purity (checklist: assistant text 100 % from text_presented)."""

from __future__ import annotations

import pytest

from vak.core.context.projection import (
    INTERRUPT_MARKER,
    PACKED_TRAILER,
    SUMMARY_PLACEHOLDER_USER,
    ProjectionError,
    project_history,
)
from vak.core.defaults import ContextParams
from vak.core.types import ClosedBy, HistoryFormat, HistoryScope, Summary

from .fakes import chat_turn, make_call, make_caps, make_store, tid

P = ContextParams()


def _project(store, caps=None, fmt=HistoryFormat.PAIRS, k=5, summary=None):
    return project_history(store.entries, summary, caps or make_caps(), fmt, k, P)


def test_pairs_alternate_user_assistant_from_text_presented():
    s = make_store()
    chat_turn(s, 1, "你好", "你好呀，我是小驼。")
    chat_turn(s, 2, "几点了", "现在三点。")
    items, scope, n = _project(s)
    assert [(i.role, i.text) for i in items] == [
        ("user", "你好"), ("assistant", "你好呀，我是小驼。"), ("user", "几点了"), ("assistant", "现在三点。"),
    ]
    assert scope is HistoryScope.FULL and n == 4


def test_skipped_and_unplayed_not_projected():
    s = make_store()
    s.record_user(tid(1), "你好", 0, 500)
    s.record_assistant_pending(tid(1), "req-1", 0, "没播出去的话", "model")
    s.record_assistant_playback("req-1", 0, "skipped", 0)
    s.record_assistant_pending(tid(1), "req-1", 1, "还在等回执", "model")  # pending, no receipt
    items, _, _ = _project(s)
    assert [i.role for i in items] == ["user"]
    assert all("没播出去" not in i.text and "还在等" not in i.text for i in items)


def test_cue_never_projected():
    s = make_store()
    s.record_user(tid(1), "帮我查一下", 0, 500)
    s.record_assistant_pending(tid(1), "cue-1", 0, "稍等。", "cue")
    s.record_assistant_playback("cue-1", 0, "ended", 400)
    chat_turn(s, 2, "好了吗", "查到了。")
    items, _, _ = _project(s)
    assert all("稍等" not in i.text for i in items)


def test_interrupted_marked_with_natural_language_and_truncated_text():
    s = make_store()
    s.record_user(tid(1), "讲讲这里", 0, 500)
    s.record_assistant_pending(tid(1), "req-1", 0, "这里是山顶，视野很好，适合看日落。", "model")
    s.record_assistant_playback("req-1", 0, "started", 0)
    s.record_assistant_playback("req-1", 0, "interrupted", 1000, duration_ms=4000)
    items, _, _ = _project(s)
    assert items[1].text.endswith(INTERRUPT_MARKER)
    assert items[1].text.startswith("这里是山")
    assert "适合看日落" not in items[1].text


def test_unsettled_tool_call_excluded_settled_kept_natively():
    s = make_store()
    caps = make_caps(history__tool_calls="keep")
    s.record_user(tid(1), "几点了", 0, 500)
    s.record_tool_call(make_call(1, "call-1"))
    s.record_tool_output(make_call(1, "call-1"))
    s.record_tool_call(make_call(1, "call-2", closed_by=None))  # still running
    items, _, _ = _project(s, caps)
    roles = [i.role for i in items]
    assert roles == ["user", "tool_call", "tool_output"]
    assert all(i.tool_call_id != "call-2" for i in items)


def test_tool_calls_drop_capability_omits_tool_entries():
    s = make_store()
    s.record_user(tid(1), "几点了", 0, 500)
    s.record_tool_call(make_call(1, "call-1"))
    s.record_tool_output(make_call(1, "call-1"))
    items, _, _ = _project(s, make_caps(history__tool_calls="drop"))
    assert [i.role for i in items] == ["user"]


def test_pairs_tools_as_text_format():
    s = make_store()
    s.record_user(tid(1), "几点了", 0, 500)
    s.record_tool_call(make_call(1, "call-1"))
    s.record_tool_output(make_call(1, "call-1"))
    s.record_assistant_pending(tid(1), "req-1", 0, "现在三点。", "local")
    s.record_assistant_playback("req-1", 0, "ended", 800)
    items, _, _ = _project(s, make_caps(history__tool_calls="as_text"), fmt=HistoryFormat.PAIRS_TOOLS_AS_TEXT)
    assert [i.role for i in items] == ["user", "assistant"]
    assert "get_time" in items[1].text and "报了当前时间" in items[1].text and "现在三点。" in items[1].text


def test_packed_format_single_user_item_with_trailer():
    s = make_store()
    chat_turn(s, 1, "你好", "你好呀。")
    chat_turn(s, 2, "几点了", "三点。")
    items, _, n = _project(s, fmt=HistoryFormat.PACKED)
    assert n == 1 and items[0].role == "user"
    assert items[0].text.endswith(PACKED_TRAILER)
    assert "用户：几点了" in items[0].text and "助手：三点。" in items[0].text


def test_recent_k_truncates_and_summary_pair_leads():
    s = make_store()
    for n in range(1, 8):
        chat_turn(s, n, f"问题{n}", f"回答{n}")
    items, scope, _ = _project(s, k=3)
    assert scope is HistoryScope.TRUNCATED
    assert items[0].text == "问题5" and len(items) == 6
    summary = Summary(True, (s.entries[0].entry_id, s.entries[7].entry_id), "之前聊了一到四。", 0)
    items, scope, _ = _project(s, k=3, summary=summary)
    assert scope is HistoryScope.SUMMARY_RECENT
    assert items[0].text == SUMMARY_PLACEHOLDER_USER and items[1].text == "之前聊了一到四。"
    assert items[2].text == "问题5"


def test_limits_trim_whole_turns_oldest_first():
    s = make_store()
    for n in range(1, 5):
        chat_turn(s, n, f"问题{n}", f"回答{n}")
    items, scope, n = _project(s, make_caps(history__limits={"max_items": 4}))
    assert n == 4 and items[0].text == "问题3" and scope is HistoryScope.TRUNCATED


def test_constraint_violation_raises_named_error():
    s = make_store()
    chat_turn(s, 1, "你好", "你好呀。")
    with pytest.raises(ProjectionError, match="end_with_user"):
        _project(s, make_caps(history__constraints=["end_with_user"]))
    # start_with_user is satisfied by a normal turn
    _project(s, make_caps(history__constraints=["start_with_user"]))


def test_segments_merge_into_one_user_entry():
    s = make_store()
    s.record_user(tid(1), "我想", 0, 300)
    s.record_user(tid(1), "去看日落", 400, 900)
    assert s.user_segments(tid(1)) == 2
    items, _, _ = _project(s)
    assert [i.text for i in items] == ["我想去看日落"]


def test_system_event_never_projected():
    s = make_store()
    chat_turn(s, 1, "你好", "你好呀。")
    s.record_system_event(tid(1), "断线重建")
    items, _, _ = _project(s)
    assert all("断线" not in i.text for i in items)


def test_closed_by_other_than_result_still_settled():
    s = make_store()
    s.record_user(tid(1), "几点了", 0, 500)
    s.record_tool_call(make_call(1, "call-1"))
    s.record_tool_output(make_call(1, "call-1", closed_by=ClosedBy.INTERRUPTED))
    items, _, _ = _project(s, make_caps(history__tool_calls="keep"))
    assert [i.role for i in items] == ["user", "tool_call", "tool_output"]
