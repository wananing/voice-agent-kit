"""Minimal fakes for the ports the turn machine calls (contributing §4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vak.core.turn.machine import EndpointEvent
from vak.core.types import (
    ContextEntry,
    EntrySource,
    InterruptSource,
    OutputRequest,
    PlaybackReceipt,
    Role,
    ToolCall,
    TurnId,
    TurnSource,
)
from vak.upstream.base import Ack, AudioMeta, ResponseOverrides, SessionInit
from vak.upstream.capabilities import Capabilities


class FakeTransport:
    """Records every JSON frame and audio frame in one ordered log."""

    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []  # ("json", msg) | ("audio", (turn_id, gen))
        self.generation = 0
        self.lead_ms = 300

    @property
    def sent(self) -> list[dict[str, Any]]:
        return [m for k, m in self.log if k == "json"]

    def of_type(self, t: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["type"] == t]

    async def send_json(self, msg: dict[str, Any]) -> None:
        self.log.append(("json", msg))

    async def send_audio(
        self, pcm16: bytes, turn_id: TurnId, sentence_id: int, frame_idx: int, generation: int,
        uninterruptible: bool = False,
    ) -> bool:
        if generation < self.generation:
            return False
        self.log.append(("audio", (str(turn_id), generation)))
        return True

    async def flush(self, turn_ids: list[TurnId], reason: str, ack_of: str | None) -> str:
        fid = f"f-{len(self.log)}"
        self.log.append(("json", {"type": "flush.start", "flush_id": fid, "turn_ids": [str(t) for t in turn_ids],
                                  "reason": reason, "ack_of": ack_of}))
        self.log.append(("json", {"type": "flush.end", "flush_id": fid}))
        return fid

    def set_generation(self, generation: int) -> None:
        self.generation = generation


class FakeArbiter:
    """Bumps generation, flushes through the transport, forwards audio deltas as frames."""

    def __init__(self, transport: FakeTransport) -> None:
        self.transport = transport
        self.generation = 0
        self.calls: list[tuple[str, Any]] = []
        self.requests: list[OutputRequest] = []
        self._by_response: dict[str, str] = {}
        self.playing: TurnId | None = None
        self.endpointing: list[tuple[TurnId, bool]] = []
        self.user_speaking: list[bool] = []
        self.awaiting: list[tuple[TurnId, bool]] = []

    async def submit(self, req: OutputRequest) -> None:
        self.requests.append(req)
        self.calls.append(("submit", req.request_id))
        if req.content.response_id:
            self._by_response[req.content.response_id] = req.request_id

    async def on_turn_start(self, turn_id: TurnId, source: TurnSource, merged: bool = False) -> int:
        self.calls.append(("on_turn_start", turn_id, source, merged))
        if merged:
            return self.generation
        self.generation += 1
        self.transport.set_generation(self.generation)
        if self.playing is not None and self.playing != turn_id:
            await self.transport.flush([self.playing], reason=source.value, ack_of=None)
            self.playing = None
        return self.generation

    async def on_barge_in(self, turn_id: TurnId, source: InterruptSource) -> int:
        self.calls.append(("on_barge_in", turn_id, source))
        self.generation += 1
        self.transport.set_generation(self.generation)
        await self.transport.flush([turn_id], reason=source.value, ack_of="abort")
        if self.playing == turn_id:
            self.playing = None
        return self.generation

    def set_user_speaking(self, speaking: bool) -> None:
        self.user_speaking.append(speaking)

    def set_endpointing(self, turn_id: TurnId, active: bool) -> None:
        self.endpointing.append((turn_id, active))

    def set_awaiting_tool(self, turn_id: TurnId, awaiting: bool) -> None:
        self.awaiting.append((turn_id, awaiting))

    async def on_reply_done(self, turn_id: TurnId, outcome: str) -> None:
        self.calls.append(("on_reply_done", turn_id, outcome))

    async def on_receipt(self, receipt: PlaybackReceipt) -> None:
        self.calls.append(("on_receipt", receipt.kind, receipt.turn_id, receipt.played_ms))

    async def flush_origin(self, origin: str, turn_id: TurnId | None = None, dedupe_key: str | None = None) -> None:
        self.calls.append(("flush_origin", origin))

    async def feed_upstream_audio(
        self, request_id: str, pcm16: bytes, sample_rate: int, response_id: str, item_id: str, generation: int
    ) -> None:
        self.calls.append(("feed", request_id, generation))
        if generation < self.generation:
            return
        req = next(r for r in self.requests if r.request_id == request_id)
        assert req.turn_id is not None
        self.playing = req.turn_id
        await self.transport.send_audio(pcm16, req.turn_id, 0, 0, generation)

    async def end_upstream_stream(self, request_id: str) -> None:
        self.calls.append(("end_stream", request_id))

    def request_for_response(self, response_id: str) -> str | None:
        return self._by_response.get(response_id)


class FakeUpstream:
    def __init__(self, caps: Capabilities | None = None) -> None:
        self.capabilities = caps or Capabilities.all_unknown("fake")
        self.calls: list[tuple[str, Any]] = []
        self.audio: list[AudioMeta] = []
        self.commit_ack = Ack.success()

    async def open(self, init: SessionInit) -> Ack:
        return Ack.success()

    async def send_audio(self, frame: bytes, meta: AudioMeta) -> Ack:
        self.audio.append(meta)
        return Ack.success()

    async def commit_turn(self, turn_id: TurnId, mode: str) -> Ack:
        self.calls.append(("commit_turn", turn_id, mode))
        return self.commit_ack

    async def send_text(self, turn_id: TurnId, text: str) -> Ack:
        self.calls.append(("send_text", turn_id, text))
        return self.commit_ack

    async def request_response(self, turn_id: TurnId, response_ref: str, overrides: ResponseOverrides | None = None) -> Ack:
        self.calls.append(("request_response", turn_id, response_ref))
        return Ack.success()

    async def cancel_response(self, response_id: str) -> Ack:
        self.calls.append(("cancel_response", response_id))
        return Ack.success()

    async def truncate(self, item_id: str, audio_end_ms: int, played_text: str | None = None) -> Ack:
        self.calls.append(("truncate", item_id, audio_end_ms))
        return Ack.success()

    async def send_tool_result(self, call_id: str, result: dict[str, Any], reply_required: bool) -> Ack:
        return Ack.success()

    async def update_config(self, patch: Any, mode: str) -> Ack:
        return Ack.success()

    async def close(self, reason: str) -> Ack:
        return Ack.success()

    def events(self) -> Any:
        raise NotImplementedError

    def of(self, name: str) -> list[tuple[str, Any]]:
        return [c for c in self.calls if c[0] == name]


class FakeContext:
    def __init__(self) -> None:
        self.user: list[tuple[TurnId, str]] = []
        self.assistant: list[tuple[TurnId, str, int, str]] = []

    def _entry(self, turn_id: TurnId, role: Role, source: EntrySource) -> ContextEntry:
        return ContextEntry(entry_id="e", user_session_id="u", session_seq=1, turn_id=turn_id, role=role,
                            source=source, t_start_ms=0)

    def record_user(self, turn_id: TurnId, text: str, t_start_ms: int, t_end_ms: int, source: str = "user_voice") -> ContextEntry:
        self.user.append((turn_id, text))
        return self._entry(turn_id, Role.USER, EntrySource(source))

    def record_assistant_pending(self, turn_id: TurnId, request_id: str, sentence_id: int, text_generated: str, source: str) -> ContextEntry:
        self.assistant.append((turn_id, request_id, sentence_id, text_generated))
        return self._entry(turn_id, Role.ASSISTANT, EntrySource.MODEL)

    def record_assistant_playback(self, request_id: str, sentence_id: int, state: str, played_ms: int, estimated: bool = False) -> ContextEntry | None:
        return None

    def record_tool_call(self, call: ToolCall) -> ContextEntry:
        raise NotImplementedError

    def record_tool_output(self, call: ToolCall) -> ContextEntry:
        raise NotImplementedError

    def record_system_event(self, turn_id: TurnId, text: str) -> ContextEntry:
        return self._entry(turn_id, Role.SYSTEM_EVENT, EntrySource.LOCAL)

    def presented_text(self, turn_id: TurnId) -> str:
        return ""

    def project_history(self, caps: Capabilities) -> Any:
        return ([], "pairs", "full", 0)

    def state_block(self) -> Any:
        return None

    def render_instructions(self) -> str:
        return ""

    def update_state(self, name: str, value: str) -> str:
        return "immediate"


class FakeTools:
    def __init__(self) -> None:
        self.open: set[str] = set()
        self.closed: list[TurnId] = []

    async def on_tool_call(self, event: Any) -> None: ...

    async def on_barge_in(self, generation: int) -> None: ...

    async def on_turn_closed(self, turn_id: TurnId) -> None:
        self.closed.append(turn_id)

    async def on_session_rebuilt(self) -> None: ...

    def has_open_calls(self, turn_id: TurnId) -> bool:
        return str(turn_id) in self.open

    def tool_table(self) -> list[Any]:
        return []


class FakeTrace:
    def __init__(self) -> None:
        self.fragments: dict[str, list[dict[str, Any]]] = {}

    def write(self, turn_id: TurnId, fragment: dict[str, Any]) -> None:
        self.fragments.setdefault(str(turn_id), []).append(fragment)

    def append(self, turn_id: TurnId, list_field: str, item: dict[str, Any]) -> None: ...

    def incr(self, turn_id: TurnId, counter: str, by: int = 1) -> None: ...

    def now_ms(self) -> int:
        return 0

    def merged(self, turn_id: TurnId | str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for frag in self.fragments.get(str(turn_id), []):
            for k, v in frag.items():
                if isinstance(v, dict) and isinstance(out.get(k), dict):
                    out[k] = {**out[k], **v}
                else:
                    out[k] = v
        return out


@dataclass
class FakeEndpointer:
    """Returns the events queued by the test on the next feed()."""

    pending: list[EndpointEvent] = field(default_factory=list)
    fed: int = 0

    def feed(self, pcm16: bytes, ts_samples: int, now_ms: int) -> list[EndpointEvent]:
        self.fed += 1
        out, self.pending = self.pending, []
        return out


class FakeCues:
    def get(self, cue_id: str) -> tuple[bytes, int, int] | None:
        return (b"\x00\x00" * 160, 16000, 300)
