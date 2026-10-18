"""Minimal fakes for the ports the context module talks to."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Literal

from vak.core.clock import FakeClock
from vak.core.context.state_block import StateBlockStore, StateSchema
from vak.core.context.store import ContextStore
from vak.core.defaults import ContextParams
from vak.core.ids import IdGen
from vak.core.types import (
    ClosedBy,
    Persona,
    Profile,
    ToolCall,
    ToolResult,
    ToolRoute,
    TurnId,
)
from vak.upstream.base import (
    Ack,
    AudioMeta,
    ConfigPatch,
    ResponseOverrides,
    SessionInit,
    ToolDef,
    UpstreamEvent,
)
from vak.upstream.capabilities import Capabilities, CapField, Evidence
from vak.upstream.providers.base import ChatMessage, LLMDelta


def make_caps(**fields: Any) -> Capabilities:
    caps = Capabilities.all_unknown("fake")
    for name, value in fields.items():
        caps.fields[name.replace("__", ".")] = CapField(value=value, evidence=Evidence.PROBED)
    return caps


class FakeUpstream:
    """Records every primitive call; `open` acks come from `open_acks` (default ok)."""

    def __init__(self, caps: Capabilities, open_acks: list[Ack] | None = None) -> None:
        self.capabilities = caps
        self.calls: list[tuple[str, Any]] = []
        self.inits: list[SessionInit] = []
        self.open_acks = open_acks if open_acks is not None else []  # shared queue when passed in
        self.open_gate: asyncio.Event | None = None

    async def open(self, init: SessionInit) -> Ack:
        if self.open_gate is not None:
            await self.open_gate.wait()
        self.inits.append(init)
        self.calls.append(("open", init))
        return self.open_acks.pop(0) if self.open_acks else Ack.success()

    async def send_audio(self, frame: bytes, meta: AudioMeta) -> Ack:
        self.calls.append(("send_audio", frame))
        return Ack.success()

    async def commit_turn(self, turn_id: TurnId, mode: Literal["commit", "discard"]) -> Ack:
        self.calls.append(("commit_turn", mode))
        return Ack.success()

    async def request_response(
        self, turn_id: TurnId, response_ref: str, overrides: ResponseOverrides | None = None
    ) -> Ack:
        self.calls.append(("request_response", turn_id))
        return Ack.success()

    async def cancel_response(self, response_id: str) -> Ack:
        return Ack.success()

    async def truncate(self, item_id: str, audio_end_ms: int, played_text: str | None = None) -> Ack:
        return Ack.success()

    async def send_tool_result(self, call_id: str, result: dict[str, Any], reply_required: bool) -> Ack:
        self.calls.append(("send_tool_result", call_id))
        return Ack.success()

    async def update_config(self, patch: ConfigPatch, mode: Literal["live", "append", "next_session"]) -> Ack:
        return Ack.success()

    async def close(self, reason: str) -> Ack:
        self.calls.append(("close", reason))
        return Ack.success()

    def events(self) -> AsyncIterator[UpstreamEvent]:
        raise NotImplementedError


class FakeTools:
    def __init__(self) -> None:
        self.open_calls = False
        self.rebuilt = 0

    async def on_tool_call(self, event: UpstreamEvent) -> None: ...

    async def on_barge_in(self, generation: int) -> None: ...

    async def on_turn_closed(self, turn_id: TurnId) -> None: ...

    async def on_session_rebuilt(self) -> None:
        self.rebuilt += 1

    def has_open_calls(self, turn_id: TurnId) -> bool:
        return self.open_calls

    def tool_table(self) -> list[Any]:
        return [ToolDef("get_time", "report the time", {})]


class FakeTrace:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def write(self, turn_id: TurnId, fragment: dict[str, Any]) -> None:
        self.records.setdefault(str(turn_id), {}).update(fragment)

    def append(self, turn_id: TurnId, list_field: str, item: dict[str, Any]) -> None:
        self.records.setdefault(str(turn_id), {}).setdefault(list_field, []).append(item)

    def incr(self, turn_id: TurnId, counter: str, by: int = 1) -> None:
        rec = self.records.setdefault(str(turn_id), {})
        rec[counter] = rec.get(counter, 0) + by

    def now_ms(self) -> int:
        return 0


class FakeLLM:
    model = "fake"

    def __init__(self, text: str = "摘要：用户叫小明，想看日落。") -> None:
        self.text = text
        self.requests: list[list[ChatMessage]] = []
        self.gate: asyncio.Event | None = None

    async def _gen(self, messages: list[ChatMessage]) -> AsyncIterator[LLMDelta]:
        self.requests.append(messages)
        if self.gate is not None:
            await self.gate.wait()
        yield LLMDelta(text=self.text)
        yield LLMDelta(done=True)

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDef] | None = None,
        tool_choice: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMDelta]:
        return self._gen(messages)

    async def cancel(self) -> None: ...


# ----------------------------------------------------------------- builders

PERSONA = Persona(
    version="3",
    role="角色：我是小驼，一个景区的语音向导。\n背景：我从小在山里长大，喜欢讲故事。\n性格：热情、耐心、直接",
    register="对孩子用短句，叫小朋友；对大人直接说。",
    hard_rules=["每轮一两句", "数字口语化", "先结论后理由", "一次只问一个问题", "不用 markdown、列表和表情"],
    banned_words=["亲", "您好请问", "为您服务"],
    boundaries="只说被告知的事实；超出范围就说我不清楚。",
)

SCHEMA = StateSchema.from_dict(
    {
        "schema_version": "2",
        "fields": [
            {"name": "task", "label": "当前任务", "layer": "immediate"},
            {"name": "user_name", "label": "称呼", "layer": "immediate"},
            {"name": "progress", "label": "进度", "layer": "lazy"},
            {"name": "last_point", "label": "刚说到", "layer": "lazy"},
        ],
    }
)


def make_store(
    clock: FakeClock | None = None, params: ContextParams | None = None, trace: FakeTrace | None = None, **kw: Any
) -> ContextStore:
    params = params or ContextParams()
    return ContextStore(
        user_session_id="us-1",
        profile=Profile.A,
        params=params,
        clock=clock or FakeClock(),
        ids=IdGen(),
        persona=PERSONA,
        state=StateBlockStore(SCHEMA, params),
        trace=trace or FakeTrace(),
        tools_version="7",
        **kw,
    )


def tid(n: int) -> TurnId:
    return TurnId("c", n)


def chat_turn(store: ContextStore, n: int, user: str, assistant: str, source: str = "model") -> None:
    """One completed turn: user text + one fully played assistant sentence."""
    store.record_user(tid(n), user, 1000 * n, 1000 * n + 500)
    store.record_assistant_pending(tid(n), f"req-{n}", 0, assistant, source)
    store.record_assistant_playback(f"req-{n}", 0, "started", 0)
    store.record_assistant_playback(f"req-{n}", 0, "ended", 1000)


def make_call(n: int, call_id: str, name: str = "get_time", closed_by: ClosedBy | None = ClosedBy.RESULT) -> ToolCall:
    call = ToolCall(
        call_id=call_id, name=name, args={"q": 1}, turn_id=tid(n), generation=1, route=ToolRoute.A1,
        received_at_ms=1000 * n + 600,
    )
    if closed_by is not None:
        call.settled = True
        call.closed_by = closed_by
        call.result = ToolResult(ok=True, summary="报了当前时间")
        call.output_sent = True
    return call
