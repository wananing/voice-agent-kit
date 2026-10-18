"""Delegation protocol (spec 03 §6): TextLLMDelegate channels, speculation gate."""

from __future__ import annotations

from tests.unit.tools import sample_tools
from tests.unit.tools.fakes import FakeLLM
from vak.core.tools import Registry, SpeculationGate, TextLLMDelegate
from vak.core.tools.delegate import DELEGATE_CONSTRAINTS, DelegateMessage, DelegationRequest
from vak.core.types import TurnId
from vak.upstream.providers.base import LLMDelta


def req(**kw) -> DelegationRequest:
    base = dict(
        delegation_id="dlg-1", call_id="call-1", turn_id=TurnId.parse("c1"), generation=3, intent_hint="explain_topic",
        last_utterance_gist="讲讲天气", transcript="讲讲今天的天气", state_block=None, recent_turns=[],
        allowed_tools=["get_status", "set_flag"], constraints=list(DELEGATE_CONSTRAINTS), deadline_ms=8000,
        tool_name="explain_topic", args={"target": "天气"},
    )
    base.update(kw)
    return DelegationRequest(**base)


async def collect(d: TextLLMDelegate, r: DelegationRequest) -> list[DelegateMessage]:
    return [m async for m in d.run(r)]


async def test_streams_sentences_then_done():
    llm = FakeLLM([[LLMDelta(text="今天天气不错，"), LLMDelta(text="适合出门。明天"), LLMDelta(text="会下雨。", done=True)]])
    msgs = await collect(TextLLMDelegate(llm, Registry(sample_tools.ALL, "A")), req())
    speak = [m.text for m in msgs if m.channel == "speak"]
    assert speak == ["今天天气不错，", "适合出门。", "明天会下雨。"]
    assert msgs[-1].channel == "terminal" and msgs[-1].terminal == "done"
    sys_msg = llm.calls[0][0]
    assert sys_msg.role == "system" and "数字逐位念" in sys_msg.content
    assert llm.calls[0][-1].role == "user" and "explain_topic" in llm.calls[0][-1].content
    # only read-only tools in allowed_tools are offered
    assert llm.tools_seen[0] == ["get_status"]


async def test_readonly_tool_step_goes_to_context_channel_and_write_is_refused():
    sample_tools.reset_knobs(None)
    llm = FakeLLM([
        [LLMDelta(tool_call={"id": "t1", "name": "get_status", "arguments": {}}),
         LLMDelta(tool_call={"id": "t2", "name": "set_flag", "arguments": {"key": "a", "value": "b"}}), LLMDelta(done=True)],
        [LLMDelta(text="还剩十二个。", done=True)],
    ])
    msgs = await collect(TextLLMDelegate(llm, Registry(sample_tools.ALL, "A")), req())
    ctx = [m for m in msgs if m.channel == "context"]
    assert [c.tool_call.name for c in ctx] == ["get_status", "set_flag"]
    assert ctx[0].tool_call.result.ok is True and ctx[1].tool_call.result.reason_code == "not_allowed"
    assert sample_tools.KNOBS["backend_calls"] == 0  # the write tool never ran
    assert [m.text for m in msgs if m.channel == "speak"] == ["还剩十二个。"]
    assert msgs[-1].terminal == "done"
    assert llm.calls[1][-1].role == "tool"


async def test_empty_and_failed_terminals():
    d = TextLLMDelegate(FakeLLM([[LLMDelta(done=True)]]), Registry(sample_tools.ALL, "A"))
    assert (await collect(d, req()))[-1].terminal == "empty"

    class Boom(FakeLLM):
        async def stream(self, *a, **k):
            raise RuntimeError("down")
            yield  # pragma: no cover

    d = TextLLMDelegate(Boom([]), Registry(sample_tools.ALL, "A"))
    assert (await collect(d, req()))[-1].terminal == "failed"


async def test_cancel_stops_stream():
    llm = FakeLLM([[LLMDelta(text="这是完整的第一句话。"), LLMDelta(text="这是完整的第二句话。"), LLMDelta(done=True)]])
    d = TextLLMDelegate(llm, Registry(sample_tools.ALL, "A"))
    out = []
    async for m in d.run(req()):
        out.append(m)
        await d.cancel("dlg-1")
    assert [m.text for m in out] == ["这是完整的第一句话。"] and llm.cancelled == 1


def test_speculation_gate_holds_until_matching_turn():
    g = SpeculationGate(TurnId.parse("c1"), 2)
    assert g.hold(DelegateMessage.speak("a")) and len(g.buffer) == 1
    assert not g.release(TurnId.parse("c2"), "call-9")
    assert g.release(TurnId.parse("c1"), "call-9") and g.call_id == "call-9"
    assert not g.hold(DelegateMessage.speak("b"))
    assert [m.text for m in g.drain()] == ["a"] and g.buffer == []
    g.discard()
    assert g.discarded and not g.release(TurnId.parse("c1"), "x")
