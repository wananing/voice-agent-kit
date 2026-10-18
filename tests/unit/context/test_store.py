"""Spec 05 §2.1 record rules: text_presented from receipts, offsets, presented_text."""

from __future__ import annotations

from vak.core.context.projection import INTERRUPT_MARKER
from vak.core.types import PlaybackState, UpstreamSynced, WriteEffect

from .fakes import chat_turn, make_call, make_caps, make_store, tid


def test_text_presented_set_only_by_receipt():
    s = make_store()
    e = s.record_assistant_pending(tid(1), "req-1", 0, "你好呀。", "model")
    assert e.text_presented == "" and e.playback_state is PlaybackState.PENDING
    s.record_assistant_playback("req-1", 0, "ended", 900)
    assert e.text_presented == "你好呀。" and e.playback_state is PlaybackState.COMPLETED


def test_interrupt_offset_proportional_with_known_duration():
    s = make_store()
    e = s.record_assistant_pending(tid(1), "req-1", 0, "一二三四五六七八九十", "model")
    s.record_assistant_playback("req-1", 0, "interrupted", 500, duration_ms=1000)
    assert e.text_presented == "一二三四五"
    assert e.interrupted and e.interrupt_at is not None
    assert e.interrupt_at.char_offset == 5 and e.interrupt_at.estimated is False


def test_interrupt_offset_estimated_when_duration_unknown():
    s = make_store()
    e = s.record_assistant_pending(tid(1), "req-1", 0, "一二三四五六七八", "model")
    s.record_assistant_playback("req-1", 0, "interrupted", 1000)  # 250 ms/char → 4 chars
    assert e.text_presented == "一二三四"
    assert e.interrupt_at is not None and e.interrupt_at.estimated is True


def test_interrupt_before_any_audio_is_skipped():
    s = make_store()
    e = s.record_assistant_pending(tid(1), "req-1", 0, "一二三四", "model")
    s.record_assistant_playback("req-1", 0, "interrupted", 0, duration_ms=1000)
    assert e.playback_state is PlaybackState.SKIPPED and not e.interrupted and e.text_presented == ""


def test_unknown_receipt_returns_none():
    s = make_store()
    assert s.record_assistant_playback("nope", 0, "ended", 1) is None


def test_presented_text_concatenates_played_sentences_with_marker():
    s = make_store()
    s.record_user(tid(1), "讲讲", 0, 500)
    s.record_assistant_pending(tid(1), "req-1", 0, "第一句。", "model")
    s.record_assistant_pending(tid(1), "req-1", 1, "第二句很长很长。", "model")
    s.record_assistant_pending(tid(1), "req-1", 2, "第三句。", "model")
    s.record_assistant_playback("req-1", 0, "ended", 800)
    s.record_assistant_playback("req-1", 1, "interrupted", 500, duration_ms=1000)
    s.record_assistant_playback("req-1", 2, "skipped", 0)
    assert s.presented_text(tid(1)) == "第一句。第二句很" + INTERRUPT_MARKER  # 7 chars × 0.5 → 4


def test_render_instructions_order_persona_rules_state_block():
    s = make_store(rules="工具只能按注册表调用。")
    s.state.update("task", "看日落")
    s.state.apply_pending()
    text = s.render_instructions()
    i_persona = text.index("小驼")
    i_rules = text.index("工具只能按注册表调用")
    i_state = text.index("当前任务：看日落。")
    assert i_persona < i_rules < i_state
    assert text.rstrip().endswith("刚说到：无。")
    assert s.render_messages_tail().startswith("当前状态（版本 2.1）")


def test_project_history_derives_format_from_tool_calls_capability():
    s = make_store()
    chat_turn(s, 1, "你好", "你好呀。")
    _, fmt, _, _ = s.project_history(make_caps(history__tool_calls="as_text"))
    assert fmt.value == "pairs_tools_as_text"
    _, fmt, _, _ = s.project_history(make_caps(history__tool_calls="keep"))
    assert fmt.value == "pairs"


def test_shrink_history_halves_k_and_summary_restores():
    s = make_store()
    assert s.shrink_history() == 2
    assert s.shrink_history() == 1
    assert s.shrink_history() == 1
    from vak.core.types import Summary

    s.set_summary(Summary(True, ("ent-1", "ent-1"), "x", 0))
    assert s._k_effective == s.params.recent_turns_k


def test_write_state_in_turn():
    s = make_store()
    s.record_user(tid(1), "帮我提交", 0, 500)
    assert s.write_state_in_turn(tid(1)) == "none"
    s.record_tool_call(make_call(1, "w-1", "submit", closed_by=None), WriteEffect("k1", executed=False))
    assert s.write_state_in_turn(tid(1)) == "unknown"
    s.set_write_effect("w-1", WriteEffect("k1", executed=True, result_ref="r-1"))
    assert s.write_state_in_turn(tid(1)) == "executed"
    assert s.executed_writes() == [{"idempotency_key": "k1", "result": "r-1"}]


def test_mark_seeded_and_session_seq_on_entries():
    s = make_store()
    chat_turn(s, 1, "你好", "你好呀。")
    s.begin_upstream_session(2)
    chat_turn(s, 2, "再见", "再见。")
    assert s.entries[0].session_seq == 1 and s.entries[-1].session_seq == 2
    s.mark_seeded([s.entries[0].entry_id])
    assert s.entries[0].upstream_synced is UpstreamSynced.SEEDED


def test_prompt_version_format():
    s = make_store()
    assert s.prompt_version("s2s") == "s2s/persona@3+tools@7+state@2.0"
