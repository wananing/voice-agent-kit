"""Provider interfaces that developers implement to add a new model/service.

Pipelines (vak/upstream/pipelines/) compose these into the nine-primitive
`Upstream` contract. Keep these small: streaming in, streaming out, cancel.
All audio is PCM16 mono; sample rates are declared, never assumed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from vak.upstream.base import ToolDef, UpstreamEvent

# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Transcript:
    text: str
    final: bool
    segment: int = 0
    start_ms: int | None = None
    end_ms: int | None = None


@runtime_checkable
class STT(Protocol):
    """Streaming speech recognition for one utterance stream."""

    sample_rate: int
    language: str | None

    async def start(self) -> None: ...

    async def feed(self, pcm16: bytes) -> None: ...

    async def finish(self) -> None:
        """Signal end of input; the stream must emit a final Transcript."""
        ...

    async def cancel(self) -> None: ...

    def results(self) -> AsyncIterator[Transcript]: ...


# ---------------------------------------------------------------------------
# LLM (text)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None  # assistant tool calls (OpenAI-style shape)
    name: str | None = None


@dataclass(slots=True)
class LLMDelta:
    text: str | None = None
    tool_call: dict[str, Any] | None = None  # complete call: {id, name, arguments(dict)}
    done: bool = False
    finish_reason: str | None = None
    usage: dict[str, int] | None = None


@runtime_checkable
class LLM(Protocol):
    """Streaming chat completion. Tool calls are reported only once their
    arguments are complete (spec 04 rule 10)."""

    model: str

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDef] | None = None,
        tool_choice: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMDelta]: ...

    async def cancel(self) -> None: ...


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TTSChunk:
    pcm16: bytes
    sample_rate: int
    is_last: bool = False
    # optional word/char timing for play-position truncation
    text_offset: int | None = None
    # text-stream mode only: set on the first audio chunk of a sentence the
    # engine segmented itself (its text as the engine reports it)
    sentence: str | None = None


@runtime_checkable
class TTSTextStream(Protocol):
    """One reply's synthesis with incremental text input: the engine segments
    sentences itself. `push` text as it is generated (LLM token deltas or whole
    sentences), `end` when there is no more; `audio()` yields until the engine
    finishes (a last chunk with `is_last=True`). `TTS.flush(request_id)` stops
    it; `close()` releases it and is safe to call at any time, any number of times."""

    async def push(self, text: str) -> None: ...

    async def end(self) -> None: ...

    def audio(self) -> AsyncIterator[TTSChunk]: ...

    def close(self) -> None: ...


@runtime_checkable
class TTS(Protocol):
    """Streaming synthesis. `supports_text_stream` is True only for engines
    that accept incremental text (handbook: CosyVoice 2/3 only among the
    six self-hosted ones; cloud: Volcengine bidirectional TTS). Those also
    implement `async def open_text_stream(request_id) -> TTSTextStream`, which
    the cascade uses for one stream per reply; `synthesize` (one call per
    sentence) stays available for everything else."""

    sample_rate: int
    supports_text_stream: bool
    voice: str

    def synthesize(self, text: str, request_id: str) -> AsyncIterator[TTSChunk]: ...

    # Optional: `async def warmup(self) -> None` opens a connection ahead of the
    # first request; the cascade calls it at turn start (LLM has the same hook).

    async def flush(self, request_id: str) -> None:
        """Stop producing chunks for `request_id` as soon as possible
        (tts_flush in spec 02 §2.7). Engines without a cancel API must
        set a stop flag and drop remaining chunks locally."""
        ...


# ---------------------------------------------------------------------------
# Audio-understanding LLM (half-cascade): audio in, text out
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AudioTurn:
    pcm16: bytes
    sample_rate: int
    transcript_hint: str | None = None


@runtime_checkable
class AudioLLM(Protocol):
    """Audio in, text (and tool calls) out. `streaming_input` tells the
    pipeline whether to feed frames live or submit the whole utterance."""

    model: str
    streaming_input: bool

    def stream(
        self,
        messages: list[ChatMessage],
        audio: AudioTurn | None,
        tools: list[ToolDef] | None = None,
    ) -> AsyncIterator[LLMDelta]: ...

    async def feed(self, pcm16: bytes) -> None: ...

    async def cancel(self) -> None: ...


# ---------------------------------------------------------------------------
# Realtime S2S (vendor protocol wrapped into standardised events)
# ---------------------------------------------------------------------------


@runtime_checkable
class RealtimeS2S(Protocol):
    """A vendor realtime session. The s2s pipeline wraps one of these; the
    provider does the protocol encode/decode and emits UpstreamEvent.

    Methods mirror the nine primitives but with vendor-level semantics; the
    pipeline applies the capability-based degradation table on top."""

    async def connect(self, config: dict[str, Any]) -> None: ...

    async def append_audio(self, pcm16: bytes, sample_rate: int) -> None: ...

    async def commit_input(self) -> None: ...

    async def clear_input(self) -> None: ...

    async def create_response(self, overrides: dict[str, Any] | None) -> str | None: ...

    async def cancel_response(self, response_id: str | None) -> None: ...

    async def truncate_item(self, item_id: str, audio_end_ms: int) -> None: ...

    async def add_tool_output(self, call_id: str, output: str, silent: bool = False) -> None:
        """`silent` is honoured only when the capability declaration says
        `tools.silent_close=flag`; otherwise ignored."""
        ...

    async def add_user_text(self, text: str) -> None:
        """Append a user text item (text-input turns and the §4.5 replay)."""
        ...

    async def update_session(self, patch: dict[str, Any]) -> None: ...

    async def disconnect(self) -> None: ...

    def events(self) -> AsyncIterator[UpstreamEvent]: ...


# ---------------------------------------------------------------------------
# Native full-duplex model (moshi-like)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DuplexFrame:
    pcm16: bytes | None = None
    sample_rate: int = 24000
    text: str | None = None  # the model's own inner-monologue text, if any


@runtime_checkable
class FullDuplexModel(Protocol):
    """Two audio streams, no turns. The full_duplex pipeline synthesises
    turn/response events from audio activity."""

    sample_rate_in: int
    sample_rate_out: int

    async def connect(self, config: dict[str, Any]) -> None: ...

    async def send(self, pcm16: bytes) -> None: ...

    def receive(self) -> AsyncIterator[DuplexFrame]: ...

    async def disconnect(self) -> None: ...


# ---------------------------------------------------------------------------
# Provider registry (string type -> factory), filled by providers/__init__
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProviderSpec:
    kind: Literal["stt", "llm", "tts", "audio_llm", "s2s", "full_duplex"]
    type_name: str
    factory: Any  # Callable[[dict], provider]
    capabilities_file: str | None = None
    extras: list[str] = field(default_factory=list)  # pip extras needed
