"""OpenAI-compatible LLM: SSE parsing, tool-call assembly (spec 04 rule 10), usage, cancel."""

from __future__ import annotations

import json

import httpx
import pytest

from vak.upstream.base import ToolDef
from vak.upstream.providers.base import ChatMessage
from vak.upstream.providers.llm.openai_compat import OpenAICompatLLM, ToolCallAssembler, encode_messages

from .fakes import sse, streaming_transport


def chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> dict:
    c: dict = {"id": "x", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        c["usage"] = usage
    return c


def tc(index: int, *, id: str | None = None, name: str | None = None, args: str | None = None) -> dict:
    d: dict = {"index": index}
    if id:
        d["id"] = id
    fn: dict = {}
    if name:
        fn["name"] = name
    if args is not None:
        fn["arguments"] = args
    if fn:
        d["function"] = fn
    return d


def make_llm(body: bytes, **cfg) -> tuple[OpenAICompatLLM, httpx.MockTransport]:
    transport = streaming_transport(body)
    client = httpx.AsyncClient(transport=transport, base_url="http://llm")
    return OpenAICompatLLM({"model": "m", "base_url": "http://llm/v1", "api_key": "k", **cfg}, client=client), transport


async def collect(llm: OpenAICompatLLM, **kw):
    return [d async for d in llm.stream([ChatMessage(role="user", content="hi")], **kw)]


async def test_text_stream_and_usage_from_final_chunk():
    body = sse(
        [
            chunk({"role": "assistant", "content": "Hel"}),
            chunk({"content": "lo"}),
            chunk({}, finish="stop"),
            {"id": "x", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "prompt_tokens_details": {"cached_tokens": 8}}},
        ]
    )
    llm, transport = make_llm(body)
    out = await collect(llm)
    assert "".join(d.text for d in out if d.text) == "Hello"
    assert out[-1].done and out[-1].finish_reason == "stop"
    assert out[-1].usage == {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cached_tokens": 8}
    req = transport.handler.requests[0]
    sent = json.loads(req.content)
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True} and sent["model"] == "m"
    assert req.headers["authorization"] == "Bearer k"


async def test_streamed_tool_call_fragments_become_one_complete_call():
    body = sse(
        [
            chunk({"role": "assistant", "content": None, "tool_calls": [tc(0, id="call_a", name="get_weather", args="")]}),
            chunk({"tool_calls": [tc(0, args='{"ci')]}),
            chunk({"tool_calls": [tc(0, args='ty": "北京"')]}),
            chunk({"tool_calls": [tc(0, args="}")]}),
            chunk({}, finish="tool_calls"),
        ]
    )
    llm, _ = make_llm(body)
    out = await collect(llm, tools=[ToolDef(name="get_weather", description="d", params={})])
    calls = [d.tool_call for d in out if d.tool_call]
    assert calls == [{"id": "call_a", "name": "get_weather", "arguments": {"city": "北京"}}]
    assert out[-1].done and out[-1].finish_reason == "tool_calls"


async def test_parallel_tool_calls_each_reported_once_when_complete():
    body = sse(
        [
            chunk({"tool_calls": [tc(0, id="c0", name="a", args='{"x":')]}),
            chunk({"tool_calls": [tc(0, args="1}")]}),
            chunk({"tool_calls": [tc(1, id="c1", name="b", args="{")]}),  # index 1 starts -> index 0 is complete
            chunk({"tool_calls": [tc(1, args='"y":2}')]}),
            chunk({}, finish="tool_calls"),
        ]
    )
    llm, _ = make_llm(body)
    out = await collect(llm)
    calls = [d.tool_call for d in out if d.tool_call]
    assert calls == [{"id": "c0", "name": "a", "arguments": {"x": 1}}, {"id": "c1", "name": "b", "arguments": {"y": 2}}]
    # the first call was yielded before the stream ended
    first_idx = next(i for i, d in enumerate(out) if d.tool_call)
    assert first_idx < len(out) - 2


async def test_unparseable_arguments_are_dropped_not_emitted():
    body = sse([chunk({"tool_calls": [tc(0, id="c", name="a", args='{"broken":')]}), chunk({}, finish="tool_calls")])
    llm, _ = make_llm(body)
    out = await collect(llm)
    assert [d for d in out if d.tool_call] == [] and out[-1].done


def test_assembler_handles_missing_index_and_partial_json():
    asm = ToolCallAssembler()
    asm.add([{"id": "c", "function": {"name": "f", "arguments": "{\"a\": "}}])
    assert asm.pop_complete() == []
    asm.add([{"function": {"arguments": "1}"}}])
    assert asm.pop_complete() == []  # last index may still grow mid-stream
    assert asm.pop_complete(final=True) == [{"id": "c", "name": "f", "arguments": {"a": 1}}]
    assert asm.pop_complete(final=True) == []  # not reported twice


async def test_cancel_stops_the_stream():
    body = sse([chunk({"content": "a"})] * 50 + [chunk({}, finish="stop")])
    llm, _ = make_llm(body)
    got = []
    async for d in llm.stream([ChatMessage(role="user", content="hi")]):
        got.append(d)
        if len(got) == 3:
            await llm.cancel()
    assert len(got) == 3 and not any(d.done for d in got)


async def test_http_error_raises():
    transport = streaming_transport(b'{"error": "nope"}', status=401)
    client = httpx.AsyncClient(transport=transport)
    llm = OpenAICompatLLM({"model": "m", "base_url": "http://llm/v1"}, client=client)
    with pytest.raises(RuntimeError, match="HTTP 401"):
        await collect(llm)


def test_encode_messages_tool_roles_and_tool_choice_body():
    msgs = [
        ChatMessage(role="system", content="S"),
        ChatMessage(role="assistant", content="", tool_calls=[{"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}]),
        ChatMessage(role="tool", content='{"ok":1}', tool_call_id="c"),
    ]
    enc = encode_messages(msgs)
    assert enc[1]["content"] is None and enc[1]["tool_calls"][0]["id"] == "c"
    assert enc[2] == {"role": "tool", "content": '{"ok":1}', "tool_call_id": "c"}
    llm = OpenAICompatLLM({"model": "m", "base_url": "http://x", "include_usage": False, "extra_body": {"top_p": 0.9}}, client=httpx.AsyncClient())
    body = llm._body(msgs, [ToolDef(name="f", description="d", params={"type": "object"})], "f", 64)
    assert body["tool_choice"] == {"type": "function", "function": {"name": "f"}}
    assert body["tools"][0]["function"]["name"] == "f" and body["max_tokens"] == 64 and body["top_p"] == 0.9
    assert "stream_options" not in body


def test_model_is_required():
    with pytest.raises(ValueError, match="model"):
        OpenAICompatLLM({"base_url": "http://x"}, client=httpx.AsyncClient())
