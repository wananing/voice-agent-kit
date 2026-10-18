"""Minimal fakes for the ports the tool layer calls (docs/contributing.md §4)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from vak.core.tools.delegate import DelegateMessage, DelegationRequest
from vak.core.types import (
    ContextEntry,
    EntrySource,
    HistoryItem,
    InterruptSource,
    OutputRequest,
    PlaybackReceipt,
    Role,
    StateBlock,
    ToolCall,
    TurnId,
    TurnSource,
)
from vak.upstream.base import Ack, EventType, UpstreamEvent
from vak.upstream.capabilities import Capabilities, CapField, Evidence
from vak.upstream.providers.base import ChatMessage, LLMDelta


class FakeArbiter:
    def __init__(self, generation: int = 1) -> None:
        self.generation = generation
        self.submitted: list[OutputRequest] = []
        self.flushes: list[tuple[str, TurnId | None, str | None]] = []

    async def submit(self, req: OutputRequest) -> None:
        self.submitted.append(req)

    async def on_turn_start(self, turn_id: TurnId, source: TurnSource, merged: bool = False) -> int:
        self.generation += 1
        return self.generation

    async def on_barge_in(self, turn_id: TurnId, source: InterruptSource) -> int:
        self.generation += 1
        return self.generation

    def set_user_speaking(self, speaking: bool) -> None: ...

    def set_endpointing(self, turn_id: TurnId, active: bool) -> None: ...

    def set_awaiting_tool(self, turn_id: TurnId, awaiting: bool) -> None: ...

    async def on_reply_done(self, turn_id: TurnId, outcome: str) -> None: ...

    async def on_receipt(self, receipt: PlaybackReceipt) -> None: ...

    async def flush_origin(self, origin: str, turn_id: TurnId | None = None, dedupe_key: str | None = None) -> None:
        self.flushes.append((origin, turn_id, dedupe_key))

    async def feed_upstream_audio(self, *a: Any, **k: Any) -> None: ...

    async def end_upstream_stream(self, request_id: str) -> None: ...

    def request_for_response(self, response_id: str) -> str | None:
        return None

    # helpers
    def by_origin(self, origin: str) -> list[OutputRequest]:
        return [r for r in self.submitted if r.origin.value == origin]

    def by_kind(self, kind: str) -> list[OutputRequest]:
        return [r for r in self.submitted if r.kind.value == kind]


class FakeUpstream:
    def __init__(self, caps: Capabilities, *, ack_ok: bool = True) -> None:
        self.capabilities = caps
        self.sent: list[tuple[str, dict[str, Any], bool]] = []
        self.ack_ok = ack_ok

    async def send_tool_result(self, call_id: str, result: dict[str, Any], reply_required: bool) -> Ack:
        self.sent.append((call_id, dict(result), reply_required))
        return Ack.success() if self.ack_ok else Ack.unsupported("tools.calling")

    def for_call(self, call_id: str) -> list[tuple[dict[str, Any], bool]]:
        return [(r, rr) for c, r, rr in self.sent if c == call_id]

    # unused primitives
    async def open(self, init: Any) -> Ack:
        return Ack.success()

    async def send_audio(self, frame: bytes, meta: Any) -> Ack:
        return Ack.success()

    async def commit_turn(self, turn_id: TurnId, mode: str) -> Ack:
        return Ack.success()

    async def request_response(self, turn_id: TurnId, response_ref: str, overrides: Any = None) -> Ack:
        return Ack.success()

    async def cancel_response(self, response_id: str) -> Ack:
        return Ack.success()

    async def truncate(self, item_id: str, audio_end_ms: int, played_text: str | None = None) -> Ack:
        return Ack.success()

    async def update_config(self, patch: Any, mode: str) -> Ack:
        return Ack.success()

    async def close(self, reason: str) -> Ack:
        return Ack.success()

    def events(self) -> AsyncIterator[UpstreamEvent]:  # pragma: no cover
        raise NotImplementedError


class FakeContext:
    def __init__(self) -> None:
        self.tool_calls: list[ToolCall] = []
        self.tool_outputs: list[ToolCall] = []
        self.system_events: list[tuple[TurnId, str]] = []
        self.presented: dict[str, str] = {}
        self.history: list[HistoryItem] = []
        self._n = 0

    def _entry(self, turn_id: TurnId, role: Role) -> ContextEntry:
        self._n += 1
        return ContextEntry(
            entry_id=f"ent-{self._n}", user_session_id="u", session_seq=1, turn_id=turn_id, role=role,
            source=EntrySource.LOCAL, t_start_ms=0,
        )

    def record_user(self, turn_id: TurnId, text: str, t_start_ms: int, t_end_ms: int, source: str = "user_voice") -> ContextEntry:
        return self._entry(turn_id, Role.USER)

    def record_assistant_pending(self, turn_id: TurnId, request_id: str, sentence_id: int, text_generated: str, source: str) -> ContextEntry:
        return self._entry(turn_id, Role.ASSISTANT)

    def record_assistant_playback(self, request_id: str, sentence_id: int, state: str, played_ms: int, estimated: bool = False) -> ContextEntry | None:
        return None

    def record_tool_call(self, call: ToolCall) -> ContextEntry:
        self.tool_calls.append(call)
        return self._entry(call.turn_id, Role.TOOL_CALL)

    def record_tool_output(self, call: ToolCall) -> ContextEntry:
        self.tool_outputs.append(call)
        return self._entry(call.turn_id, Role.TOOL_OUTPUT)

    def record_system_event(self, turn_id: TurnId, text: str) -> ContextEntry:
        self.system_events.append((turn_id, text))
        return self._entry(turn_id, Role.SYSTEM_EVENT)

    def presented_text(self, turn_id: TurnId) -> str:
        return self.presented.get(str(turn_id), "")

    def project_history(self, caps: Capabilities) -> tuple[list[HistoryItem], str, str, int]:
        return list(self.history), "pairs", "full", len(self.history)

    def state_block(self) -> StateBlock:
        return StateBlock(schema_version="1", state_rev=0, fields=[])

    def render_instructions(self) -> str:
        return ""

    def update_state(self, name: str, value: str) -> str:
        return "immediate"


class FakeTrace:
    def __init__(self) -> None:
        self.writes: list[tuple[TurnId, dict[str, Any]]] = []
        self.appends: list[tuple[TurnId, str, dict[str, Any]]] = []
        self.incrs: list[tuple[TurnId, str, int]] = []

    def write(self, turn_id: TurnId, fragment: dict[str, Any]) -> None:
        self.writes.append((turn_id, dict(fragment)))

    def append(self, turn_id: TurnId, list_field: str, item: dict[str, Any]) -> None:
        self.appends.append((turn_id, list_field, dict(item)))

    def incr(self, turn_id: TurnId, counter: str, by: int = 1) -> None:
        self.incrs.append((turn_id, counter, by))

    def now_ms(self) -> int:
        return 0

    # helpers
    def records(self, call_id: str) -> list[dict[str, Any]]:
        return [i for _t, f, i in self.appends if f == "tool_calls" and i.get("call_id") == call_id]

    def last_record(self, call_id: str) -> dict[str, Any]:
        return self.records(call_id)[-1]

    def latency(self, turn_id: TurnId) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for t, frag in self.writes:
            if t == turn_id and "latency" in frag:
                out.update(frag["latency"])
        return out

    def field(self, turn_id: TurnId, key: str) -> Any:
        val = None
        for t, frag in self.writes:
            if t == turn_id and key in frag:
                val = frag[key]
        return val


class FakeCues:
    def __init__(self) -> None:
        self.ids: dict[str, tuple[bytes, int, int]] = {
            "thinking_short": (b"\x00" * 320, 16000, 200),
            "thinking_long": (b"\x00" * 1600, 16000, 760),
        }

    def get(self, cue_id: str) -> tuple[bytes, int, int] | None:
        return self.ids.get(cue_id)


def make_caps(silent_close: str = "by_omission") -> Capabilities:
    caps = Capabilities.all_unknown("fake")
    caps.fields["tools.silent_close"] = CapField(silent_close, Evidence.PROBED)
    caps.fields["tools.calling"] = CapField("native", Evidence.PROBED)
    caps.fields["tools.auto_reply_after_result"] = CapField("no", Evidence.PROBED)
    return caps


def tool_call_event(call_id: str, name: str, args: dict[str, Any] | None, turn_id: TurnId, *, before_commit: bool = False, seq: int = 1) -> UpstreamEvent:
    return UpstreamEvent(
        type=EventType.TOOL_CALL, seq=seq, t_local_ms=0, turn_id=turn_id, call_id=call_id, name=name,
        args=dict(args or {}), before_commit=before_commit,
    )


@dataclass
class ScriptedDelegate:
    """Yields a scripted message list; `step` is an Event released by the test."""

    script: list[DelegateMessage]
    gate_each: bool = False
    cancelled: list[str] = field(default_factory=list)
    requests: list[DelegationRequest] = field(default_factory=list)
    step: asyncio.Event = field(default_factory=asyncio.Event)
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self, request: DelegationRequest) -> AsyncIterator[DelegateMessage]:
        self.requests.append(request)
        self.started.set()
        for msg in self.script:
            if self.gate_each:
                await self.step.wait()
                self.step.clear()
            yield msg

    async def cancel(self, delegation_id: str) -> None:
        self.cancelled.append(delegation_id)


class FakeLLM:
    """Scripted text LLM: each call to stream() pops the next delta list."""

    model = "fake"

    def __init__(self, scripts: list[list[LLMDelta]]) -> None:
        self.scripts = list(scripts)
        self.calls: list[list[ChatMessage]] = []
        self.tools_seen: list[list[str]] = []
        self.cancelled = 0

    async def stream(self, messages: list[ChatMessage], tools: Any = None, tool_choice: str | None = None, max_tokens: int | None = None) -> AsyncIterator[LLMDelta]:
        self.calls.append(list(messages))
        self.tools_seen.append([t.name for t in (tools or [])])
        script = self.scripts.pop(0) if self.scripts else [LLMDelta(done=True)]
        for d in script:
            yield d

    async def cancel(self) -> None:
        self.cancelled += 1


async def settle(n: int = 12) -> None:
    for _ in range(n):
        await asyncio.sleep(0)
