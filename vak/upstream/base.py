"""Upstream adapter contract (spec 04 §2.3–2.5).

Nine primitives, one Ack type, one event envelope, four error classes.
Adapters translate; they never decide. No retries, no reconnects, no turn
counting, no branching on content, no vendor names outside providers/.
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from vak.core.types import (
    ErrorClass,
    ErrorReason,
    ErrorScope,
    HistoryFormat,
    HistoryItem,
    ReplyModality,
    TurnId,
)
from vak.upstream.capabilities import Capabilities

# ---------------------------------------------------------------------------
# Ack
# ---------------------------------------------------------------------------


class AckCode(enum.StrEnum):
    UNSUPPORTED = "unsupported"
    INVALID_STATE = "invalid_state"
    SLOT_BUSY = "slot_busy"
    TIMEOUT = "timeout"
    UPSTREAM_ERROR = "upstream_error"


@dataclass(slots=True)
class Ack:
    ok: bool
    code: AckCode | None = None
    error_class: ErrorClass | None = None
    detail: str | None = None
    # extra facts a primitive may return (e.g. response_id, committed_by_server, local_only, duplicate)
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, **data: Any) -> Ack:
        return cls(ok=True, data=data)

    @classmethod
    def unsupported(cls, capability_field: str) -> Ack:
        return cls(ok=False, code=AckCode.UNSUPPORTED, detail=capability_field)

    @classmethod
    def invalid_state(cls, detail: str) -> Ack:
        return cls(ok=False, code=AckCode.INVALID_STATE, detail=detail)

    @classmethod
    def slot_busy(cls) -> Ack:
        return cls(ok=False, code=AckCode.SLOT_BUSY)

    @classmethod
    def timeout(cls, detail: str | None = None) -> Ack:
        return cls(ok=False, code=AckCode.TIMEOUT, detail=detail)

    @classmethod
    def upstream_error(cls, error_class: ErrorClass, detail: str | None = None) -> Ack:
        return cls(ok=False, code=AckCode.UPSTREAM_ERROR, error_class=error_class, detail=detail)


# ---------------------------------------------------------------------------
# Primitive arguments
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolDef:
    """Tool table entry as seen by the upstream (name/description/params only)."""

    name: str
    description: str
    params: dict[str, Any]


@dataclass(slots=True)
class SessionInit:
    """Everything an upstream session is seeded with, once (spec 04 open)."""

    user_session_id: str
    session_seq: int
    instructions: str  # persona + rules + state block rendered
    state_block_version: str
    tools: list[ToolDef]
    history: list[HistoryItem]
    history_format: HistoryFormat
    resume_handle: str | None = None
    output_default: Literal["audio", "text"] = "audio"
    voice: str | None = None
    language: str | None = None
    # None = ask the upstream to turn server-side turn detection off (ptt / local endpointing);
    # "default" = leave the vendor default; a dict = vendor-specific settings passed through.
    turn_detection: dict[str, Any] | None | Literal["default"] = "default"


@dataclass(slots=True)
class AudioMeta:
    turn_id: TurnId | None  # None only in open_mic outside a turn
    seq: int
    sample_count: int
    captured_at_ms: int
    source: str
    sample_rate: int = 16000


@dataclass(slots=True)
class ResponseOverrides:
    instructions: str | None = None
    modalities: list[Literal["audio", "text"]] | None = None
    tools: list[ToolDef] | None = None
    tool_choice: str | None = None


@dataclass(slots=True)
class ConfigPatch:
    instructions: str | None = None
    tools: list[ToolDef] | None = None
    voice: str | None = None
    state_block_version: str | None = None


# ---------------------------------------------------------------------------
# Standardised events (spec 04 §2.4)
# ---------------------------------------------------------------------------


class EventType(enum.StrEnum):
    INPUT_SPEECH_STARTED = "input.speech_started"
    INPUT_SPEECH_STOPPED = "input.speech_stopped"
    INPUT_SEGMENTED = "input.segmented"
    INPUT_COMMITTED = "input.committed"
    TRANSCRIPT_USER_PARTIAL = "transcript.user.partial"
    TRANSCRIPT_USER_FINAL = "transcript.user.final"
    TRANSCRIPT_ASSISTANT = "transcript.assistant"
    ITEM_ECHO = "item.echo"
    RESPONSE_STARTED = "response.started"
    RESPONSE_TEXT_DELTA = "response.text.delta"
    RESPONSE_AUDIO_DELTA = "response.audio.delta"
    RESPONSE_SENTENCE = "response.sentence"
    RESPONSE_DONE = "response.done"
    TOOL_CALL = "tool.call"
    TOOL_CALL_CANCELLED = "tool.call_cancelled"
    SESSION_OPENED = "session.opened"
    SESSION_RESUMABLE = "session.resumable"
    SESSION_EXPIRING = "session.expiring"
    SESSION_CLOSED = "session.closed"
    UPLINK_BACKPRESSURE = "uplink.backpressure"
    USAGE = "usage"
    ERROR = "error"


class ResponseStatus(enum.StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


class ResponseOrigin(enum.StrEnum):
    CLIENT = "client"
    SERVER_VAD = "server_vad"
    AUTO_TOOL = "auto_tool"


@dataclass(slots=True)
class UpstreamError:
    error_class: ErrorClass
    reason: ErrorReason
    scope: ErrorScope
    ref: dict[str, str] = field(default_factory=dict)
    raw_code: str | None = None
    raw_message: str | None = None


@dataclass(slots=True)
class Usage:
    response_id: str | None
    input_text_tokens: int | None = None
    input_audio_tokens: int | None = None
    cached_input_tokens: int | None = None
    cached_text_tokens: int | None = None
    cached_audio_tokens: int | None = None
    output_text_tokens: int | None = None
    output_audio_tokens: int | None = None
    generated_audio_ms: int | None = None
    usage_semantics: Literal["net", "gross"] = "net"


@dataclass(slots=True)
class UpstreamEvent:
    """Common envelope. Every delta carries response_id and item_id."""

    type: EventType
    seq: int
    t_local_ms: int
    session_id: str | None = None
    turn_id: TurnId | None = None
    response_id: str | None = None
    response_ref: str | None = None
    item_id: str | None = None
    t_upstream_ms: int | None = None
    raw_ref: str | None = None
    # payload (fields depend on type; names follow spec 04 §2.4)
    text: str | None = None
    is_final: bool | None = None
    final_by_timeout: bool = False
    late: bool = False
    audio: bytes | None = None  # PCM16 mono at `sample_rate`
    sample_rate: int = 16000
    duration_ms: int | None = None
    audio_ms: int | None = None
    sentence_index: int | None = None
    submitted_at_ms: int | None = None  # response.sentence: when its text went to the TTS (spec 04 §2.4)
    segment_index: int | None = None
    committed_by: Literal["client", "server"] | None = None
    source: str | None = None
    origin: ResponseOrigin | None = None
    status: ResponseStatus | None = None
    reply_modality: ReplyModality | None = None
    confirmed: bool | None = None
    call_id: str | None = None
    name: str | None = None
    args: dict[str, Any] | None = None
    before_commit: bool = False
    reason: str | None = None
    local_id: str | None = None
    kind: str | None = None
    handle: str | None = None
    remaining_s: int | None = None
    effective: dict[str, Any] | None = None
    buffered_ms: int | None = None
    usage: Usage | None = None
    error: UpstreamError | None = None


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


@runtime_checkable
class Upstream(Protocol):
    """Nine primitives (spec 04 §2.3). open/close/update_config are serialised
    per session by the caller; others may be concurrent but upstream order
    follows call order."""

    capabilities: Capabilities

    async def open(self, init: SessionInit) -> Ack: ...

    async def send_audio(self, frame: bytes, meta: AudioMeta) -> Ack: ...

    async def commit_turn(self, turn_id: TurnId, mode: Literal["commit", "discard"]) -> Ack: ...

    async def send_text(self, turn_id: TurnId, text: str) -> Ack:
        """A text-input turn (`turn.start{source:"text"}`, spec 01 §2.3): the
        text is the whole user input and the turn counts as committed. The
        caller still calls `request_response`. Upstreams without a text item
        return `unsupported(uplink.text_input)`."""
        ...

    async def request_response(
        self, turn_id: TurnId, response_ref: str, overrides: ResponseOverrides | None = None
    ) -> Ack: ...

    async def cancel_response(self, response_id: str) -> Ack: ...

    async def truncate(self, item_id: str, audio_end_ms: int, played_text: str | None = None) -> Ack: ...

    async def send_tool_result(self, call_id: str, result: dict[str, Any], reply_required: bool) -> Ack: ...

    async def update_config(
        self, patch: ConfigPatch, mode: Literal["live", "append", "next_session"]
    ) -> Ack: ...

    async def close(self, reason: str) -> Ack: ...

    def events(self) -> AsyncIterator[UpstreamEvent]: ...


class GuardrailHook(Protocol):
    """Cascade / half-cascade pre-speech hook (spec 04 §5): called per
    complete sentence before TTS. Budget 50 ms."""

    async def __call__(
        self, response_id: str, turn_id: TurnId | None, sentence_index: int, text: str, is_last: bool
    ) -> GuardrailVerdict: ...


@dataclass(slots=True)
class GuardrailVerdict:
    action: Literal["allow", "replace", "block"]
    text: str | None = None
    escalate: bool = False
