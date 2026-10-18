"""Spec 05 §5: summary input, triggers, no re-summarising, change detection."""

from __future__ import annotations

import asyncio

from vak.core.clock import FakeClock
from vak.core.context.summary import Summarizer
from vak.core.defaults import ContextParams

from .fakes import FakeLLM, chat_turn, make_call, make_store, tid


async def test_summary_uses_only_presented_text_and_settled_tool_outputs():
    s = make_store()
    llm = FakeLLM("用户叫小明。")
    for n in range(1, 8):
        chat_turn(s, n, f"问题{n}", f"回答{n}")
    # an interrupted reply: generated text differs from presented
    s.record_assistant_pending(tid(1), "req-x", 1, "这句没播出去", "model")
    s.record_assistant_playback("req-x", 1, "skipped", 0)
    s.record_tool_call(make_call(2, "call-1"))
    s.record_tool_output(make_call(2, "call-1"))
    summ = Summarizer(llm, ContextParams(), FakeClock())
    result = await summ.summarize(lambda: s.entries_before_recent())  # K=5 → turns 1–2
    assert result is not None and result.is_summary and result.text == "用户叫小明。"
    prompt = llm.requests[0][1].content
    assert "问题1" in prompt and "回答2" in prompt and "报了当前时间" in prompt
    assert "没播出去" not in prompt and "问题3" not in prompt
    assert result.covers[0] == s.entries[0].entry_id


async def test_summary_cuts_before_unsettled_tool_call():
    s = make_store()
    llm = FakeLLM()
    chat_turn(s, 1, "a", "b")
    s.record_tool_call(make_call(1, "open-1", closed_by=None))
    chat_turn(s, 2, "c", "d")
    summ = Summarizer(llm, ContextParams(), FakeClock())
    result = await summ.summarize(lambda: s.entries)
    assert result is not None
    assert "c" not in llm.requests[0][1].content  # stopped before the open call
    assert result.covers[1] == s.entries[1].entry_id


async def test_summary_redone_when_input_changed_during_llm_call():
    s = make_store()
    llm = FakeLLM()
    llm.gate = asyncio.Event()
    s.record_user(tid(1), "讲讲", 0, 500)
    e = s.record_assistant_pending(tid(1), "req-1", 0, "一二三四五六", "model")
    s.record_assistant_playback("req-1", 0, "ended", 1500)
    summ = Summarizer(llm, ContextParams(), FakeClock())
    task = asyncio.create_task(summ.summarize(lambda: s.entries))
    await asyncio.sleep(0)
    e.text_presented = "一二三"  # late receipt rewrote the covered range
    llm.gate.set()
    result = await task
    assert result is not None and len(llm.requests) == 2  # first result discarded, redone


async def test_schedule_installs_summary_off_hot_path_and_never_resummarises():
    s = make_store()
    llm = FakeLLM("第一版")
    for n in range(1, 9):
        chat_turn(s, n, f"问题{n}", f"回答{n}")
    summ = Summarizer(llm, ContextParams(), FakeClock())
    task = summ.schedule(s)
    assert task is not None and s.summary is None
    await task
    assert s.summary is not None and s.summary.text == "第一版"
    for n in range(9, 12):
        chat_turn(s, n, f"问题{n}", f"回答{n}")
    task = summ.schedule(s)
    await task
    prompt = llm.requests[1][1].content
    assert "第一版" not in prompt and "问题1" in prompt  # raw entries again, not the old summary


def test_trigger_rule():
    p = ContextParams(turn_limit_threshold=12, summary_trigger_turns_before_limit=6)
    summ = Summarizer(FakeLLM(), p, FakeClock())
    assert not summ.should_trigger(5, 0, None)
    assert summ.should_trigger(6, 0, None)
    assert summ.should_trigger(0, 700, 1000)
    assert not Summarizer(None, p, FakeClock()).should_trigger(99, 99999, 10)
