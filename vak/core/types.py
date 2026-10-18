"""Shared types for the pipeline-independent layer.

Every enum value and field name here is taken verbatim from the glossary
(specs/00-glossary.md) and the six specs. Do not invent new names; if a
concept is missing, add it here with a reference to the spec section.

Vendor or model names must never appear in this file.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Session / profile (spec 01 §1, §7)
# ---------------------------------------------------------------------------


class Profile(enum.StrEnum):
    """A = screenless device, B = app with a screen (spec 01 §7)."""

    A = "A"
    B = "B"


class Mode(enum.StrEnum):
    """Turn-boundary configuration (spec 01 §2.3)."""

    PTT = "ptt"
    OPEN_MIC = "open_mic"


class Pipeline(enum.StrEnum):
    """The four pipeline shapes (handbook 02-architectures)."""

    CASCADE = "cascade"
    HALF_CASCADE = "half_cascade"
    S2S = "s2s"
    FULL_DUPLEX = "full_duplex"


class Aec(enum.StrEnum):
    DEVICE = "device"
    OS = "os"
    NONE = "none"


# ---------------------------------------------------------------------------
# Turn (spec 01 §2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TurnId:
    """`c<n>` (client) / `s<n>` (gateway). Wire form is a uint32 whose MSB
    marks the gateway side (spec 01 §2.2)."""

    side: Literal["c", "s"]
    n: int

    def __post_init__(self) -> None:
        if not (0 < self.n < (1 << 31)):
            raise ValueError(f"turn id n out of range: {self.n}")

    def __str__(self) -> str:
        return f"{self.side}{self.n}"

    @classmethod
    def parse(cls, text: str) -> TurnId:
        if len(text) < 2 or text[0] not in ("c", "s") or not text[1:].isdigit():
            raise ValueError(f"bad turn id: {text!r}")
        return cls(text[0], int(text[1:]))  # type: ignore[arg-type]

    def to_u32(self) -> int:
        return self.n | (0x8000_0000 if self.side == "s" else 0)

    @classmethod
    def from_u32(cls, v: int) -> TurnId | None:
        """0 means "no turn" (open mic, outside a turn)."""
        if v == 0:
            return None
        return cls("s" if v & 0x8000_0000 else "c", v & 0x7FFF_FFFF)


class TurnState(enum.StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    HOLDING = "holding"
    COMMITTED = "committed"
    RESPONDING = "responding"
    DRAINING = "draining"
    # terminal states (spec 01 §2.4)
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    DISCARDED = "discarded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_TURN_STATES


TERMINAL_TURN_STATES = frozenset(
    {
        TurnState.COMPLETED,
        TurnState.INTERRUPTED,
        TurnState.DISCARDED,
        TurnState.FAILED,
        TurnState.CANCELLED,
    }
)


class TurnSource(enum.StrEnum):
    BUTTON = "button"
    WAKE = "wake"
    VAD = "vad"
    TEXT = "text"
    EVENT = "event"


class TurnInput(enum.StrEnum):
    TYPED = "typed"
    TAP = "tap"
    TEST = "test"


class TurnEndReason(enum.StrEnum):
    RELEASE = "release"
    MAX_DURATION = "max_duration"
    CANCEL = "cancel"
    ENDPOINT = "endpoint"


class AbortReason(enum.StrEnum):
    BUTTON = "button"
    WAKE = "wake"
    TEXT = "text"
    USER_CANCEL = "user_cancel"


class InterruptSource(enum.StrEnum):
    """`turn.closed.by` for interrupted turns and trace `interrupt_source`."""

    BUTTON = "button"
    VAD = "vad"
    WAKE = "wake"
    TEXT = "text"
    LINK = "link"
    CRITICAL = "critical"
    GUARDRAIL = "guardrail"


class DiscardReason(enum.StrEnum):
    SHORT_PRESS = "short_press"
    SHORT_SPEECH = "short_speech"
    FALSE_WAKE = "false_wake"
    RULE_SILENCE = "rule_silence"


class ErrorCode(enum.StrEnum):
    """Turn-level error codes on the client protocol (spec 01 §2.4)."""

    NO_SPEECH = "no_speech"
    ASR_TIMEOUT = "asr_timeout"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_REJECTED = "upstream_rejected"
    TTS_FAILED = "tts_failed"
    AUDIO_GAP = "audio_gap"
    LINK_LOST = "link_lost"
    PROTOCOL_ERROR = "protocol_error"
    SESSION_EXPIRED = "session_expired"
    INTERNAL = "internal"


class Watchdog(enum.StrEnum):
    NONE = "none"
    T_ASR = "T_asr"
    T_FILLER = "T_filler"
    T_RESPOND = "T_respond"
    T_DRAIN = "T_drain"
    T_INTR_RECEIPT = "T_intr_receipt"
    T_LISTEN_MAX = "T_listen_max"
    T_HOLD = "T_hold"
    TOOL_TIMEOUT = "tool_timeout"


@dataclass(slots=True)
class PlaybackReceipt:
    """`playback.started / ended / interrupted` (spec 01 §2.5). `played_ms`
    is the within-sentence duration consumed by the DAC."""

    kind: Literal["started", "ended", "interrupted"]
    turn_id: TurnId
    sentence_id: int
    played_ms: int
    ts_ms: int
    reason: str | None = None
    request_id: str | None = None  # filled in by the gateway from (turn_id, sentence_id)


# ---------------------------------------------------------------------------
# Output arbiter (spec 02 §2)
# ---------------------------------------------------------------------------


class Origin(enum.StrEnum):
    MODEL = "model"
    TOOL_RESULT = "tool_result"
    DELEGATED_RESULT = "delegated_result"
    CUE = "cue"
    TASK_PROGRESS = "task_progress"
    TASK_RESULT = "task_result"
    REMINDER = "reminder"
    DEVICE_EVENT = "device_event"
    SYSTEM_NOTICE = "system_notice"
    SYSTEM_EXIT = "system_exit"


class Kind(enum.StrEnum):
    CUE = "cue"
    SPEECH = "speech"
    NARRATION = "narration"


class Priority(enum.StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


PRIORITY_ORDER = {Priority.CRITICAL: 0, Priority.HIGH: 1, Priority.NORMAL: 2, Priority.LOW: 3}


class ContentRole(enum.StrEnum):
    """Spec 06 §2.3: decides sound-first vs content-first."""

    FILLER = "filler"
    ANSWER = "answer"
    PROGRESS = "progress"
    ERROR = "error"


class ContextPolicy(enum.StrEnum):
    NONE = "none"
    BEFORE_SPEAK = "before_speak"
    AFTER_PLAYED = "after_played"


class ContentType(enum.StrEnum):
    PRERECORDED = "prerecorded"
    TEXT_TTS = "text_tts"
    UPSTREAM_STREAM = "upstream_stream"


@dataclass(slots=True)
class OutputContent:
    type: ContentType
    text: str | None = None
    voice: str = "default"
    audio: bytes | None = None  # prerecorded PCM16 (already trimmed of leading silence)
    sample_rate: int = 16000
    response_id: str | None = None  # upstream_stream
    item_id: str | None = None  # upstream_stream
    duration_ms: int | None = None  # known for prerecorded


@dataclass(slots=True)
class OutputRequest:
    """Spec 02 §2.1. `generation` is None for narration until granted."""

    request_id: str
    origin: Origin
    kind: Kind
    content: OutputContent
    priority: Priority = Priority.NORMAL
    generation: int | None = None
    turn_id: TurnId | None = None
    content_role: ContentRole = ContentRole.ANSWER
    interruptible: bool = True
    on_interrupt: Literal["drop", "retry"] = "drop"
    max_retries: int = 0
    expires_at_ms: int | None = None
    dedupe_key: str | None = None
    supersedes: str | None = None
    context_policy: ContextPolicy = ContextPolicy.AFTER_PLAYED
    trace: dict[str, Any] = field(default_factory=dict)


class RequestStatus(enum.StrEnum):
    QUEUED = "queued"
    GRANTED = "granted"
    SENT = "sent"
    PLAYING = "playing"
    DONE = "done"
    INTERRUPTED = "interrupted"
    RETRY_WAIT = "retry_wait"
    DROPPED = "dropped"


class DropReason(enum.StrEnum):
    STALE = "stale"
    EXPIRED = "expired"
    DUPLICATE = "duplicate"
    OVERFLOW = "overflow"
    RETRY_EXHAUSTED = "retry_exhausted"


class OutputStatus(enum.StrEnum):
    """trace `outputs[].status` (spec 06 §2.5)."""

    PLAYED = "played"
    TRUNCATED = "truncated"
    FLUSHED = "flushed"
    DROPPED_STALE = "dropped_stale"
    DROPPED_EXPIRED = "dropped_expired"
    DROPPED_DUPLICATE = "dropped_duplicate"
    DROPPED_OVERFLOW = "dropped_overflow"
    DROPPED_RETRY_EXHAUSTED = "dropped_retry_exhausted"


@dataclass(frozen=True, slots=True)
class Lease:
    lease_id: str
    generation: int


# ---------------------------------------------------------------------------
# Tools (spec 03 §2)
# ---------------------------------------------------------------------------


class Presentation(enum.StrEnum):
    VERBATIM = "verbatim"
    MODEL = "model"
    SCREEN = "screen"
    SILENT = "silent"


class LatencyClass(enum.StrEnum):
    MS = "ms"
    SECONDS = "seconds"
    BACKGROUND = "background"


class CueLength(enum.StrEnum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


class InterruptPolicy(enum.StrEnum):
    CANCEL = "cancel"
    CONTINUE = "continue"
    FINISH_THEN_NOTIFY = "finish_then_notify"


class OnDuplicate(enum.StrEnum):
    ALLOW = "allow"
    REJECT = "reject"
    REPLACE = "replace"
    CONFIRM = "confirm"


class DuplicateScope(enum.StrEnum):
    TURN = "turn"
    IN_FLIGHT = "in_flight"
    SESSION = "session"


class ToolRoute(enum.StrEnum):
    A0 = "A0"
    A1 = "A1"
    A2 = "A2"
    A3 = "A3"
    SILENT = "silent"


class ClosedBy(enum.StrEnum):
    """Six terminal states of a tool call (spec 03 §2.6). Do not add or remove."""

    RESULT = "result"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    PROGRESS = "progress"
    SUPERSEDED = "superseded"
    DUPLICATE = "duplicate"


class CallState(enum.StrEnum):
    RECEIVED = "received"
    RUNNING = "running"
    CLOSED = "closed"


class InterruptPhase(enum.StrEnum):
    RECEIVED = "received"
    RUNNING = "running"
    DONE_NOT_SENT = "done_not_sent"
    SENT_SPEAKING = "sent_speaking"
    PROGRESS_CLOSED = "progress_closed"
    NARRATION = "narration"
    DELEGATING = "delegating"
    N_A = "n_a"


class GateKind(enum.StrEnum):
    INTENT = "intent"
    UTTERANCE = "utterance"
    BACKEND_STATE = "backend_state"


@dataclass(slots=True)
class Gate:
    kind: GateKind
    allowed_actions: list[str] = field(default_factory=list)  # intent
    must_match: list[str] = field(default_factory=list)  # utterance
    must_not_match: list[str] = field(default_factory=list)  # utterance
    check: str | None = None  # backend_state: name of a registered checker


@dataclass(slots=True)
class ProgressSpec:
    ack_template: str
    throttle_first_after_s: int = 60
    throttle_min_interval_s: int = 60
    replace_older: bool = True


@dataclass(slots=True)
class ToolSpec:
    """Registry entry (spec 03 §2.1). Validated at registration time."""

    name: str
    description: str
    params: dict[str, Any]
    presentation: Presentation
    latency_class: LatencyClass
    write: bool
    phases: list[str] | Literal["global"]
    interrupt_policy: InterruptPolicy
    on_duplicate: OnDuplicate
    timeout_ms: int
    result_schema: dict[str, Any] = field(default_factory=dict)
    voice_template: str | None = None
    cue: CueLength | None = None
    idempotency_key: list[str] = field(default_factory=list)
    gates: list[Gate] = field(default_factory=list)
    visible_after: list[str] = field(default_factory=list)
    cancellable_by_user: bool | None = None
    duplicate_scope: DuplicateScope = DuplicateScope.TURN
    progress: ProgressSpec | None = None
    profile_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    # static route computed at registration (spec 03 §2.4)
    route: ToolRoute | None = None


@dataclass(slots=True)
class ToolResult:
    """Common result fields (spec 03 §2.3)."""

    ok: bool
    summary: str
    reason_code: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    display: dict[str, Any] | None = None
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    escalation: dict[str, Any] | None = None


@dataclass(slots=True)
class ToolCall:
    """Call record (spec 03 §2.5). All callbacks check `settled` first."""

    call_id: str
    name: str
    args: dict[str, Any]
    turn_id: TurnId
    generation: int
    route: ToolRoute
    received_at_ms: int
    state: CallState = CallState.RECEIVED
    settled: bool = False
    closed_by: ClosedBy | None = None
    closed_at_ms: int | None = None
    started_at_ms: int | None = None
    done_at_ms: int | None = None
    reply_required: bool | None = None
    output_sent: bool = False
    interrupt_phase: InterruptPhase = InterruptPhase.N_A
    route_fallback_reason: str | None = None
    gate_result: Literal["passed", "blocked", "n_a"] = "n_a"
    late_result_dropped: int = 0
    result: ToolResult | None = None
    response_id: str | None = None
    before_commit: bool = False


# ---------------------------------------------------------------------------
# Context (spec 05 §2)
# ---------------------------------------------------------------------------


class Role(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_OUTPUT = "tool_output"
    SYSTEM_EVENT = "system_event"


class EntrySource(enum.StrEnum):
    MODEL = "model"
    LOCAL = "local"
    CUE = "cue"
    USER_VOICE = "user_voice"
    USER_UI = "user_ui"


class PresentedVia(enum.StrEnum):
    VOICE = "voice"
    SCREEN = "screen"
    BOTH = "both"


class PlaybackState(enum.StrEnum):
    PENDING = "pending"
    PLAYING = "playing"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"


class UpstreamSynced(enum.StrEnum):
    NONE = "none"
    LIVE = "live"
    SEEDED = "seeded"


@dataclass(slots=True)
class InterruptAt:
    played_ms: int
    char_offset: int
    estimated: bool


@dataclass(slots=True)
class WriteEffect:
    idempotency_key: str
    executed: bool
    result_ref: str | None = None


@dataclass(slots=True)
class ContextEntry:
    """Local conversation record (spec 05 §2.2). `text_generated` is for
    trace only; projection reads `text_presented`."""

    entry_id: str
    user_session_id: str
    session_seq: int
    turn_id: TurnId
    role: Role
    source: EntrySource
    t_start_ms: int
    t_end_ms: int | None = None
    sentence_id: int | None = None
    presented_via: PresentedVia | None = None
    text_generated: str | None = None
    text_presented: str | None = None
    text_user: str | None = None
    playback_state: PlaybackState | None = None
    interrupted: bool = False
    interrupt_at: InterruptAt | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_closed_by: ClosedBy | None = None
    tool_summary: str | None = None
    related_entry_ids: list[str] = field(default_factory=list)
    upstream_synced: UpstreamSynced = UpstreamSynced.NONE
    write_effect: WriteEffect | None = None
    request_id: str | None = None


class HistoryFormat(enum.StrEnum):
    PAIRS = "pairs"
    PACKED = "packed"
    PAIRS_TOOLS_AS_TEXT = "pairs_tools_as_text"


class HistoryScope(enum.StrEnum):
    FULL = "full"
    TRUNCATED = "truncated"
    SUMMARY_RECENT = "summary_recent"


class RebuildReason(enum.StrEnum):
    NONE = "none"
    DISCONNECT = "disconnect"
    UPSTREAM_ERROR = "upstream_error"
    CONTEXT_OVERFLOW = "context_overflow"
    STATE_CHANGE = "state_change"
    TURN_LIMIT = "turn_limit"
    SCHEDULED = "scheduled"
    MODALITY = "modality"
    APP_RESUME = "app_resume"


class ReplyModality(enum.StrEnum):
    AUDIO = "audio"
    TEXT_ONLY = "text_only"
    MIXED = "mixed"
    NONE = "none"


class StateLayer(enum.StrEnum):
    IMMEDIATE = "immediate"
    LAZY = "lazy"


@dataclass(slots=True)
class StateField:
    name: str
    label: str
    layer: StateLayer
    value: str | None = None


@dataclass(slots=True)
class StateBlock:
    schema_version: str
    state_rev: int
    fields: list[StateField]

    @property
    def version(self) -> str:
        return f"{self.schema_version}.{self.state_rev}"


@dataclass(slots=True)
class Persona:
    """Spec 05 §2.4: sections in fixed order."""

    version: str
    role: str
    register: str
    hard_rules: list[str]
    banned_words: list[str]
    boundaries: str


@dataclass(slots=True)
class HistoryItem:
    """One projected history item fed to `Upstream.open`."""

    role: Literal["user", "assistant", "tool_call", "tool_output"]
    text: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    local_entry_id: str | None = None


@dataclass(slots=True)
class Summary:
    is_summary: bool
    covers: tuple[str, str]
    text: str
    created_at_ms: int


# ---------------------------------------------------------------------------
# Errors (spec 04 §2.5, shared with 05/06)
# ---------------------------------------------------------------------------


class ErrorClass(enum.StrEnum):
    FATAL = "fatal"
    TRANSIENT = "transient"
    CONTEXT_OVERFLOW = "context_overflow"
    BENIGN = "benign"


class ErrorReason(enum.StrEnum):
    AUTH = "auth"
    QUOTA = "quota"
    BILLING = "billing"
    INVALID_CONFIG = "invalid_config"
    SESSION_LOST = "session_lost"
    TIMEOUT = "timeout"
    THROTTLED = "throttled"
    CANCEL_RACE = "cancel_race"
    EMPTY_COMMIT = "empty_commit"
    DUPLICATE = "duplicate"
    MODERATION = "moderation"
    ITEM_REJECTED = "item_rejected"
    TOOL_PARSE = "tool_parse"
    UNKNOWN = "unknown"


class ErrorScope(enum.StrEnum):
    SESSION = "session"
    RESPONSE = "response"
    ITEM = "item"
    CALL = "call"


# ---------------------------------------------------------------------------
# Trace (spec 06 §2.5) - only enums that other modules write
# ---------------------------------------------------------------------------


class TurnKind(enum.StrEnum):
    CHAT = "chat"
    TOOL_VERBATIM = "tool_verbatim"
    TOOL_COMPOSE = "tool_compose"
    TOOL_WRITE = "tool_write"
    EVENT = "event"
    UI_INPUT = "ui_input"
    ERROR = "error"


class SpeakMode(enum.StrEnum):
    VERBATIM = "verbatim"
    MODEL = "model"
    DELEGATED = "delegated"
    SCREEN = "screen"
    NONE = "none"


class Anchor(enum.StrEnum):
    RELEASE = "release"
    ENDPOINT = "endpoint"
    AUDIO_END = "audio_end"
    TAP = "tap"
    EVENT = "event"


class ClockQuality(enum.StrEnum):
    SYNCED = "synced"
    RX_MINUS_HALF_RTT = "rx_minus_half_rtt"
    GATEWAY_RX = "gateway_rx"


class ReceiptMode(enum.StrEnum):
    RECEIPTS = "receipts"
    ESTIMATED = "estimated"


class IllegalTransition(RuntimeError):
    """Raised by every explicit state machine on a transition not in its table."""
