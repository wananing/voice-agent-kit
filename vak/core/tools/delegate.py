"""Delegation protocol (spec 03 §6): foreground → background request, the
three return channels, the speculation gate, and a minimal text-LLM delegate.

The executor owns generation stamping and the arbiter; a `Delegate` only
yields messages. Write tools are never delegated (spec 03 §3.2 row 7, §6.3).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from vak.audio.text import flush_remainder, split_sentences
from vak.core.ids import IdGen
from vak.core.types import HistoryItem, StateBlock, ToolCall, ToolResult, ToolRoute, TurnId
from vak.upstream.base import ToolDef
from vak.upstream.providers.base import LLM, ChatMessage

log = logging.getLogger(__name__)

Terminal = Literal["done", "failed", "timeout", "empty"]

# spec 03 §6.1 `constraints` (voice-shaped output)
DELEGATE_CONSTRAINTS: tuple[str, ...] = (
    "一两句话，先说结论。",
    "不要用 markdown、列表、标题。",
    "数字逐位念，不要出现 ID、URL、路径。",
)

MAX_TOOL_STEPS = 3  # handbook tool-calls: recursion cap 3–5


@dataclass(slots=True)
class DelegationRequest:
    """Foreground → background (spec 03 §6.1)."""

    delegation_id: str
    call_id: str | None  # None while speculative (spec 03 §6.3)
    turn_id: TurnId
    generation: int
    intent_hint: str | None
    last_utterance_gist: str
    transcript: str | None
    state_block: StateBlock | None
    recent_turns: list[HistoryItem]
    allowed_tools: list[str]
    constraints: list[str]
    deadline_ms: int
    # what the foreground tool was asked to do (route A2 tool + the data its fn returned)
    tool_name: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    tool_result: ToolResult | None = None


@dataclass(slots=True)
class DelegateMessage:
    """Background → foreground, one of three channels (spec 03 §6.2)."""

    channel: Literal["speak", "context", "terminal"]
    text: str | None = None  # speak: one sentence; context: a fact
    tool_call: ToolCall | None = None  # context: a background read-only tool call record
    terminal: Terminal | None = None

    @classmethod
    def speak(cls, text: str) -> DelegateMessage:
        return cls("speak", text=text)

    @classmethod
    def context(cls, text: str | None = None, tool_call: ToolCall | None = None) -> DelegateMessage:
        return cls("context", text=text, tool_call=tool_call)

    @classmethod
    def end(cls, terminal: Terminal) -> DelegateMessage:
        return cls("terminal", terminal=terminal)


class Delegate(Protocol):
    def run(self, request: DelegationRequest) -> AsyncIterator[DelegateMessage]: ...

    async def cancel(self, delegation_id: str) -> None: ...


@dataclass(slots=True)
class SpeculationGate:
    """Holds speculative output until the real delegation call arrives with
    the same `turn_id` (spec 03 §6.3 item 2)."""

    turn_id: TurnId
    generation: int
    released: bool = False
    discarded: bool = False
    call_id: str | None = None
    buffer: list[DelegateMessage] = field(default_factory=list)

    @classmethod
    def open(cls, turn_id: TurnId, generation: int, call_id: str) -> SpeculationGate:
        """A gate that is already released (non-speculative delegation)."""
        return cls(turn_id, generation, released=True, call_id=call_id)

    def hold(self, msg: DelegateMessage) -> bool:
        """Buffer `msg` if the gate is closed; return True if buffered."""
        if self.released or self.discarded:
            return False
        self.buffer.append(msg)
        return True

    def release(self, turn_id: TurnId, call_id: str) -> bool:
        if self.discarded or turn_id != self.turn_id:
            return False
        self.released = True
        self.call_id = call_id
        return True

    def drain(self) -> list[DelegateMessage]:
        out, self.buffer = self.buffer, []
        return out

    def discard(self) -> None:
        self.discarded = True
        self.buffer.clear()


class TextLLMDelegate:
    """Spec 03 §6: background text LLM with a read-only tool subset, streaming
    sentences on the `speak` channel. Generation checks and TTS are the
    executor's job."""

    def __init__(self, llm: LLM, tools: Any, *, ids: IdGen | None = None) -> None:
        """`tools` is a `Registry` (duck-typed: get/fn/readonly_names/tool_table)."""
        self.llm = llm
        self.tools = tools
        self.ids = ids or IdGen()
        self._cancelled: set[str] = set()

    async def cancel(self, delegation_id: str) -> None:
        self._cancelled.add(delegation_id)
        await self.llm.cancel()

    def _messages(self, req: DelegationRequest) -> list[ChatMessage]:
        system = ["你是语音助手的后台，负责组织要说给用户听的话。", *req.constraints]
        if req.state_block is not None:
            lines = [f"{f.label}: {f.value}" for f in req.state_block.fields if f.value is not None]
            if lines:
                system.append("当前状态（" + req.state_block.version + "）：" + "；".join(lines))
        msgs = [ChatMessage(role="system", content="\n".join(system))]
        for item in req.recent_turns:
            if item.role in ("user", "assistant"):
                msgs.append(ChatMessage(role=item.role, content=item.text))
        user = req.transcript or req.last_utterance_gist
        if req.tool_name:
            user += f"\n（前台调用了 {req.tool_name}，参数 {json.dumps(req.args, ensure_ascii=False)}）"
        if req.tool_result is not None:
            user += f"\n工具结果：{req.tool_result.summary} {json.dumps(req.tool_result.data, ensure_ascii=False)}"
        msgs.append(ChatMessage(role="user", content=user))
        return msgs

    def _tool_defs(self, req: DelegationRequest) -> list[ToolDef]:
        readonly = set(self.tools.readonly_names())
        return [
            ToolDef(name=s.name, description=s.description, params=dict(s.params))
            for s in self.tools
            if s.name in readonly and s.name in req.allowed_tools
        ]

    async def _run_tool(self, req: DelegationRequest, call: dict[str, Any]) -> tuple[ToolCall, str]:
        name = call.get("name", "")
        args = dict(call.get("arguments") or {})
        rec = ToolCall(
            call_id=call.get("id") or self.ids.new("dcall"),
            name=name,
            args=args,
            turn_id=req.turn_id,
            generation=req.generation,
            route=ToolRoute.A2,
            received_at_ms=0,
        )
        if name not in self.tools or self.tools.get(name).write:
            # spec 03 §6.3 item 4: never a side effect from a delegate
            result = ToolResult(ok=False, reason_code="not_allowed", summary=f"{name} 不允许在后台调用")
        else:
            try:
                raw = await self.tools.fn(name)(**args)
                result = raw if isinstance(raw, ToolResult) else ToolResult(**raw)
            except Exception as exc:  # noqa: BLE001
                log.warning("delegate tool %s failed: %s", name, exc)
                result = ToolResult(ok=False, reason_code="tool_error", summary=f"{name} 执行失败")
        rec.result = result
        return rec, json.dumps({"ok": result.ok, "summary": result.summary, "data": result.data}, ensure_ascii=False)

    async def run(self, req: DelegationRequest) -> AsyncIterator[DelegateMessage]:
        msgs = self._messages(req)
        tool_defs = self._tool_defs(req)
        spoke = False
        try:
            for _step in range(MAX_TOOL_STEPS + 1):
                buffer = ""
                pending_calls: list[dict[str, Any]] = []
                async for delta in self.llm.stream(msgs, tools=tool_defs or None):
                    if req.delegation_id in self._cancelled:
                        return
                    if delta.text:
                        buffer += delta.text
                        sentences, buffer = split_sentences(buffer, first=not spoke)
                        for s in sentences:
                            spoke = True
                            yield DelegateMessage.speak(s)
                    if delta.tool_call:
                        pending_calls.append(delta.tool_call)
                    if delta.done:
                        break
                for s in flush_remainder(buffer):
                    spoke = True
                    yield DelegateMessage.speak(s)
                if not pending_calls:
                    break
                msgs.append(ChatMessage(role="assistant", content="", tool_calls=pending_calls))
                for call in pending_calls:
                    rec, output = await self._run_tool(req, call)
                    yield DelegateMessage.context(tool_call=rec)
                    msgs.append(ChatMessage(role="tool", content=output, tool_call_id=rec.call_id, name=rec.name))
        except Exception as exc:  # noqa: BLE001
            log.warning("delegation %s failed: %s", req.delegation_id, exc)
            yield DelegateMessage.end("failed")
            return
        yield DelegateMessage.end("done" if spoke else "empty")


__all__ = [
    "DELEGATE_CONSTRAINTS",
    "MAX_TOOL_STEPS",
    "Delegate",
    "DelegateMessage",
    "DelegationRequest",
    "SpeculationGate",
    "Terminal",
    "TextLLMDelegate",
]
