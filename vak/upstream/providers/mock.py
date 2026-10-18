"""Deterministic, scriptable providers (architecture §7 item 9).

Used by `tests/conformance/` and by `vak run` without any real service.
Every provider records what it was asked to do so tests can assert on it,
and every timing goes through the injected clock so `FakeClock.advance()`
drives it deterministically.
"""

from __future__ import annotations

import asyncio
import itertools
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from vak.audio.text import flush_remainder, split_sentences
from vak.core.clock import Clock
from vak.core.types import ErrorClass, ErrorReason, ErrorScope, ReplyModality
from vak.upstream.base import (
    EventType,
    ResponseStatus,
    ToolDef,
    UpstreamError,
    UpstreamEvent,
    Usage,
)
from vak.upstream.capabilities import Capabilities
from vak.upstream.providers.base import (
    AudioTurn,
    ChatMessage,
    DuplexFrame,
    LLMDelta,
    Transcript,
    TTSChunk,
)

# ---------------------------------------------------------------------------
# audio helpers
# ---------------------------------------------------------------------------


def tone_pcm(ms: int, sample_rate: int, freq: float = 440.0, amplitude: float = 0.3) -> bytes:
    """PCM16 mono sine tone of `ms` milliseconds."""
    n = sample_rate * ms // 1000
    t = np.arange(n, dtype=np.float32) / sample_rate
    return (np.sin(2 * math.pi * freq * t) * amplitude * 32767).astype(np.int16).tobytes()


def silence_pcm(ms: int, sample_rate: int) -> bytes:
    return bytes(2 * (sample_rate * ms // 1000))


def _chunks_of(text: str) -> list[str]:
    """Stream text by words (English) or by two characters (no whitespace)."""
    words = text.split(" ")
    if len(words) > 1:
        return [w + (" " if i < len(words) - 1 else "") for i, w in enumerate(words)]
    return [text[i : i + 2] for i in range(0, len(text), 2)]


@dataclass(slots=True)
class MockReply:
    """One scripted model turn: optional tool call, then text."""

    text: str = "好的，我在。"
    tool_call: dict[str, Any] | None = None  # {id, name, arguments}
    audio_deltas: int = 3  # MockRealtimeS2S only
    fail: Exception | None = None


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------


class MockSTT:
    """Emits partials every `partial_every` frames and the configured final on `finish`."""

    def __init__(
        self,
        transcript: str = "现在几点了",
        partial_every: int = 2,
        sample_rate: int = 16000,
        language: str | None = "zh",
        final_on_finish: bool = True,
    ) -> None:
        self.sample_rate = sample_rate
        self.language = language
        self.transcript = transcript
        self.partial_every = partial_every
        self.final_on_finish = final_on_finish
        self.frames = 0
        self.finished = False
        self.cancelled = False
        self._q: asyncio.Queue[Transcript | None] = asyncio.Queue()

    async def start(self) -> None:
        pass

    async def feed(self, pcm16: bytes) -> None:
        self.frames += 1
        if self.frames % self.partial_every == 0:
            k = self.frames // self.partial_every
            self._q.put_nowait(Transcript(text=self.transcript[:k], final=False))

    async def finish(self) -> None:
        self.finished = True
        if self.final_on_finish:
            self._q.put_nowait(Transcript(text=self.transcript, final=True))
            self._q.put_nowait(None)

    async def cancel(self) -> None:
        self.cancelled = True
        self._q.put_nowait(None)

    def emit_final(self, text: str | None = None) -> None:
        """Test hook: deliver the final later than `finish` (late final)."""
        self._q.put_nowait(Transcript(text=text or self.transcript, final=True))
        self._q.put_nowait(None)

    async def results(self) -> AsyncIterator[Transcript]:
        while True:
            t = await self._q.get()
            if t is None:
                return
            yield t


# ---------------------------------------------------------------------------
# text LLM / audio LLM
# ---------------------------------------------------------------------------


class MockLLM:
    """Streams scripted replies word by word; tool calls come first (rule 10)."""

    model = "mock-llm"

    def __init__(
        self,
        script: list[MockReply] | None = None,
        clock: Clock | None = None,
        chunk_delay_ms: int = 0,
        late_delta_after_cancel: bool = False,
    ) -> None:
        self.script = list(script or [])
        self.clock = clock
        self.chunk_delay_ms = chunk_delay_ms
        self.late_delta_after_cancel = late_delta_after_cancel
        self.calls: list[dict[str, Any]] = []
        self.cancels = 0
        self._cancelled = False

    def _next(self) -> MockReply:
        return self.script.pop(0) if self.script else MockReply()

    async def _pace(self) -> None:
        if self.clock is not None and self.chunk_delay_ms > 0:
            await self.clock.sleep(self.chunk_delay_ms)
        else:
            await asyncio.sleep(0)

    async def _stream_reply(self, reply: MockReply) -> AsyncIterator[LLMDelta]:
        self._cancelled = False
        if reply.fail is not None:
            raise reply.fail
        if reply.tool_call is not None:
            yield LLMDelta(tool_call=dict(reply.tool_call))
        for piece in _chunks_of(reply.text):
            await self._pace()
            if self._cancelled:
                if self.late_delta_after_cancel:
                    yield LLMDelta(text=piece)
                return
            yield LLMDelta(text=piece)
        yield LLMDelta(done=True, finish_reason="tool_calls" if reply.tool_call else "stop", usage={"total": 1})

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDef] | None = None,
        tool_choice: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMDelta]:
        self.calls.append({"messages": list(messages), "tools": tools, "tool_choice": tool_choice})
        return self._stream_reply(self._next())

    async def cancel(self) -> None:
        self.cancels += 1
        self._cancelled = True


class MockAudioLLM(MockLLM):
    """Audio in, text out. Records the audio turns it was given."""

    model = "mock-audio-llm"

    def __init__(self, script: list[MockReply] | None = None, streaming_input: bool = False, **kw: Any) -> None:
        super().__init__(script, **kw)
        self.streaming_input = streaming_input
        self.audio_turns: list[AudioTurn | None] = []
        self.fed = bytearray()

    def stream(  # type: ignore[override]
        self,
        messages: list[ChatMessage],
        audio: AudioTurn | None,
        tools: list[ToolDef] | None = None,
    ) -> AsyncIterator[LLMDelta]:
        self.audio_turns.append(audio)
        self.calls.append({"messages": list(messages), "tools": tools, "audio": audio})
        return self._stream_reply(self._next())

    async def feed(self, pcm16: bytes) -> None:
        self.fed.extend(pcm16)


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------


class MockTTS:
    """Tone PCM proportional to text length, in `chunk_ms` chunks; honours flush."""

    supports_text_stream = False

    def __init__(
        self,
        sample_rate: int = 16000,
        ms_per_char: int = 50,
        chunk_ms: int = 100,
        voice: str = "mock",
        clock: Clock | None = None,
        chunk_delay_ms: int = 0,
        late_chunk_after_flush: bool = False,
    ) -> None:
        self.sample_rate = sample_rate
        self.voice = voice
        self.ms_per_char = ms_per_char
        self.chunk_ms = chunk_ms
        self.clock = clock
        self.chunk_delay_ms = chunk_delay_ms
        self.late_chunk_after_flush = late_chunk_after_flush
        self.requests: list[tuple[str, str]] = []
        self.flushed: list[str] = []

    async def synthesize(self, text: str, request_id: str) -> AsyncIterator[TTSChunk]:
        self.requests.append((text, request_id))
        total = max(self.chunk_ms, len(text) * self.ms_per_char)
        n = math.ceil(total / self.chunk_ms)
        for i in range(n):
            if self.clock is not None and self.chunk_delay_ms > 0:
                await self.clock.sleep(self.chunk_delay_ms)
            else:
                await asyncio.sleep(0)
            if request_id in self.flushed:
                if self.late_chunk_after_flush:
                    yield TTSChunk(tone_pcm(self.chunk_ms, self.sample_rate), self.sample_rate, is_last=True)
                return
            yield TTSChunk(tone_pcm(self.chunk_ms, self.sample_rate), self.sample_rate, is_last=i == n - 1)

    async def flush(self, request_id: str) -> None:
        self.flushed.append(request_id)


class MockStreamTTS(MockTTS):
    """MockTTS that also takes incremental text (`supports_text_stream`).
    The "engine" segments pushed text with the cascade splitter's later-sentence
    rule and marks each sentence on its first chunk; `marks=False` behaves like
    an engine that reports no boundaries (Volcengine seed-tts-2.0)."""

    supports_text_stream = True

    def __init__(self, *args: Any, marks: bool = True, open_error: BaseException | None = None, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.marks = marks
        self.open_error = open_error
        self.streams: list[MockTextStream] = []

    async def open_text_stream(self, request_id: str) -> MockTextStream:
        await asyncio.sleep(0)
        if self.open_error is not None:
            raise self.open_error
        st = MockTextStream(self, request_id)
        self.streams.append(st)
        return st


class MockTextStream:
    def __init__(self, tts: MockStreamTTS, request_id: str) -> None:
        self.tts = tts
        self.request_id = request_id
        self.pushes: list[str] = []
        self.ended = False
        self.closed = False
        self._buf = ""
        self._q: asyncio.Queue[str | None] = asyncio.Queue()

    async def push(self, text: str) -> None:
        self.pushes.append(text)
        sentences, self._buf = split_sentences(self._buf + text, first=False)
        for s in sentences:
            self._q.put_nowait(s)

    async def end(self) -> None:
        self.ended = True
        for s in flush_remainder(self._buf):
            self._q.put_nowait(s)
        self._buf = ""
        self._q.put_nowait(None)

    async def audio(self) -> AsyncIterator[TTSChunk]:
        t = self.tts
        while (text := await self._q.get()) is not None:
            n = math.ceil(max(t.chunk_ms, len(text) * t.ms_per_char) / t.chunk_ms)
            for i in range(n):
                if t.clock is not None and t.chunk_delay_ms > 0:
                    await t.clock.sleep(t.chunk_delay_ms)
                else:
                    await asyncio.sleep(0)
                if self.request_id in t.flushed:
                    return
                mark = text if i == 0 and t.marks else None
                yield TTSChunk(tone_pcm(t.chunk_ms, t.sample_rate), t.sample_rate, sentence=mark)
        yield TTSChunk(b"", t.sample_rate, is_last=True)

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Realtime S2S
# ---------------------------------------------------------------------------


class MockRealtimeS2S:
    """A scriptable vendor session.

    Behaviour knobs mirror `capabilities/mock_s2s.yaml`: `ack_commit`,
    `ack_items`, `ack_session_update`, `emit_opened`, `emit_started`,
    `auto_reply_after_result`, `late_deltas_after_cancel`, `text_only`.
    `script` is consumed one `MockReply` per response.
    """

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        script: list[MockReply] | None = None,
        sample_rate: int = 24000,
        delta_ms: int = 100,
        delta_interval_ms: int = 0,
        ack_commit: bool = True,
        ack_items: bool = True,
        ack_session_update: bool = True,
        emit_opened: bool = True,
        emit_started: bool = True,
        return_response_id: bool = True,
        auto_reply_after_result: bool = False,
        late_deltas_after_cancel: bool = False,
        text_only: bool = False,
        resume_handle: str | None = "mock-handle-1",
    ) -> None:
        self.clock = clock
        self.script = list(script or [])
        self.sample_rate = sample_rate
        self.delta_ms = delta_ms
        self.delta_interval_ms = delta_interval_ms
        self.ack_commit = ack_commit
        self.ack_items = ack_items
        self.ack_session_update = ack_session_update
        self.emit_opened = emit_opened
        self.emit_started = emit_started
        self.return_response_id = return_response_id
        self.auto_reply_after_result = auto_reply_after_result
        self.late_deltas_after_cancel = late_deltas_after_cancel
        self.text_only = text_only
        self.resume_handle = resume_handle
        # recorded calls
        self.connected = False
        self.disconnected = False
        self.config: dict[str, Any] | None = None
        self.appended: list[tuple[bytes, int]] = []
        self.commits = 0
        self.clears = 0
        self.created: list[dict[str, Any] | None] = []
        self.cancelled: list[str] = []
        self.truncated: list[tuple[str, int]] = []
        self.tool_outputs: list[tuple[str, str]] = []
        self.user_texts: list[str] = []
        self.updates: list[dict[str, Any]] = []
        self._q: asyncio.Queue[UpstreamEvent | None] = asyncio.Queue()
        self._ids = itertools.count(1)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_flags: set[str] = set()

    # -- event construction --------------------------------------------------

    def _ev(self, type_: EventType, **fields: Any) -> UpstreamEvent:
        ev = UpstreamEvent(type=type_, seq=0, t_local_ms=0, **fields)
        ev.t_upstream_ms = self.clock.now_ms() if self.clock else None
        ev.raw_ref = f"mock:{type_.value}"
        return ev

    def push(self, ev: UpstreamEvent) -> None:
        """Inject any event (tests)."""
        self._q.put_nowait(ev)

    def inject_error(
        self,
        raw_code: str,
        message: str | None = None,
        scope: ErrorScope = ErrorScope.SESSION,
        ref: dict[str, str] | None = None,
    ) -> None:
        err = UpstreamError(
            error_class=ErrorClass.TRANSIENT, reason=ErrorReason.UNKNOWN, scope=scope, ref=ref or {},
            raw_code=raw_code, raw_message=message,
        )
        self.push(self._ev(EventType.ERROR, error=err))

    def start_server_response(self, reply: MockReply | None = None) -> str:
        """A response the server started on its own (server VAD or auto reply)."""
        rid = f"mr-{next(self._ids)}"
        self._tasks[rid] = asyncio.create_task(self._run(rid, reply or self._next_reply()))
        return rid

    def _next_reply(self) -> MockReply:
        return self.script.pop(0) if self.script else MockReply()

    # -- RealtimeS2S -----------------------------------------------------------

    async def connect(self, config: dict[str, Any]) -> None:
        self.connected = True
        self.config = config
        if self.emit_opened:
            self.push(self._ev(EventType.SESSION_OPENED, session_id="mock-session", effective={"tools": len(config.get("tools") or [])}))
        if self.ack_items:
            for i, h in enumerate(config.get("history") or []):
                self.push(self._ev(EventType.ITEM_ECHO, item_id=f"i{i}", kind=h.get("role", "item"), local_id=h.get("local_entry_id")))
        if self.resume_handle:
            self.push(self._ev(EventType.SESSION_RESUMABLE, handle=self.resume_handle))

    async def append_audio(self, pcm16: bytes, sample_rate: int) -> None:
        self.appended.append((pcm16, sample_rate))

    async def commit_input(self) -> None:
        self.commits += 1
        if self.ack_commit:
            self.push(self._ev(EventType.INPUT_COMMITTED, committed_by="client"))

    async def clear_input(self) -> None:
        self.clears += 1

    async def create_response(self, overrides: dict[str, Any] | None) -> str | None:
        self.created.append(overrides)
        rid = f"mr-{next(self._ids)}"
        reply = self._next_reply()
        if overrides and overrides.get("modalities") == ["text"]:
            reply = MockReply(text=reply.text, tool_call=reply.tool_call, audio_deltas=0)
        self._tasks[rid] = asyncio.create_task(self._run(rid, reply))
        return rid if self.return_response_id else None

    async def _pace(self) -> None:
        if self.clock is not None and self.delta_interval_ms > 0:
            await self.clock.sleep(self.delta_interval_ms)
        else:
            await asyncio.sleep(0)

    async def _run(self, rid: str, reply: MockReply) -> None:
        item_id = f"mi-{rid}"
        if self.emit_started:
            self.push(self._ev(EventType.RESPONSE_STARTED, response_id=rid))  # origin left to the pipeline
        if reply.fail is not None:
            self.inject_error(type(reply.fail).__name__, str(reply.fail), ErrorScope.RESPONSE, {"response_id": rid})
            self.push(self._ev(EventType.RESPONSE_DONE, response_id=rid, status=ResponseStatus.FAILED, reply_modality=ReplyModality.NONE))
            return
        if reply.tool_call is not None:
            tc = reply.tool_call
            self.push(self._ev(EventType.TOOL_CALL, call_id=tc["id"], name=tc["name"], args=dict(tc.get("arguments") or {}), response_id=rid))
        if reply.text:
            self.push(self._ev(EventType.RESPONSE_TEXT_DELTA, response_id=rid, item_id=item_id, text=reply.text))
            self.push(self._ev(EventType.TRANSCRIPT_ASSISTANT, response_id=rid, item_id=item_id, text=reply.text, is_final=True))
        deltas = 0 if self.text_only else reply.audio_deltas
        for _ in range(deltas):
            await self._pace()
            if rid in self._cancel_flags:
                return
            self.push(
                self._ev(
                    EventType.RESPONSE_AUDIO_DELTA, response_id=rid, item_id=item_id,
                    audio=tone_pcm(self.delta_ms, self.sample_rate), sample_rate=self.sample_rate, duration_ms=self.delta_ms,
                )
            )
        if deltas:
            modality = ReplyModality.AUDIO
        elif reply.text:
            modality = ReplyModality.TEXT_ONLY
        else:
            modality = ReplyModality.NONE
        self.push(self._ev(EventType.RESPONSE_DONE, response_id=rid, status=ResponseStatus.COMPLETED, reply_modality=modality))
        self.push(self._ev(EventType.USAGE, response_id=rid, usage=Usage(response_id=rid, input_text_tokens=10, cached_input_tokens=4, output_audio_tokens=deltas)))

    async def cancel_response(self, response_id: str | None) -> None:
        if response_id is None:
            return
        self.cancelled.append(response_id)
        self._cancel_flags.add(response_id)
        task = self._tasks.get(response_id)
        if task is not None and not task.done():
            task.cancel()
        self.push(self._ev(EventType.RESPONSE_DONE, response_id=response_id, status=ResponseStatus.CANCELLED, reply_modality=ReplyModality.AUDIO))
        if self.late_deltas_after_cancel:
            for _ in range(2):
                self.push(
                    self._ev(
                        EventType.RESPONSE_AUDIO_DELTA, response_id=response_id, item_id=f"mi-{response_id}",
                        audio=tone_pcm(self.delta_ms, self.sample_rate), sample_rate=self.sample_rate, duration_ms=self.delta_ms,
                    )
                )

    async def truncate_item(self, item_id: str, audio_end_ms: int) -> None:
        self.truncated.append((item_id, audio_end_ms))
        if self.ack_items:
            self.push(self._ev(EventType.ITEM_ECHO, item_id=item_id, kind="truncated", audio_ms=audio_end_ms))

    async def add_user_text(self, text: str) -> None:
        self.user_texts.append(text)

    async def add_tool_output(self, call_id: str, output: str, silent: bool = False) -> None:
        self.tool_outputs.append((call_id, output))
        if self.ack_items:
            self.push(self._ev(EventType.ITEM_ECHO, item_id=f"i-{call_id}", kind="tool_output", local_id=call_id))
        if self.auto_reply_after_result:
            self.start_server_response()

    async def update_session(self, patch: dict[str, Any]) -> None:
        self.updates.append(patch)
        if self.ack_session_update:
            self.push(self._ev(EventType.ITEM_ECHO, item_id="session", kind="session.updated"))

    async def disconnect(self) -> None:
        self.disconnected = True
        for t in self._tasks.values():
            t.cancel()
        self._q.put_nowait(None)

    async def events(self) -> AsyncIterator[UpstreamEvent]:
        while True:
            ev = await self._q.get()
            if ev is None:
                return
            yield ev


# ---------------------------------------------------------------------------
# Full-duplex model
# ---------------------------------------------------------------------------


class MockFullDuplex:
    """Emits bursts of tone separated by silence; `bursts` = [(tone_ms, silence_ms, text)]."""

    def __init__(
        self,
        bursts: list[tuple[int, int, str | None]] | None = None,
        sample_rate_out: int = 24000,
        sample_rate_in: int = 24000,
        frame_ms: int = 80,
        clock: Clock | None = None,
        frame_interval_ms: int = 0,
    ) -> None:
        self.bursts = list(bursts or [(400, 1000, "hello there")])
        self.sample_rate_out = sample_rate_out
        self.sample_rate_in = sample_rate_in
        self.frame_ms = frame_ms
        self.clock = clock
        self.frame_interval_ms = frame_interval_ms
        self.connected = False
        self.disconnected = False
        self.sent: list[bytes] = []
        self._stop = asyncio.Event()

    async def connect(self, config: dict[str, Any]) -> None:
        self.connected = True

    async def send(self, pcm16: bytes) -> None:
        self.sent.append(pcm16)

    async def _pace(self) -> None:
        if self.clock is not None and self.frame_interval_ms > 0:
            await self.clock.sleep(self.frame_interval_ms)
        else:
            await asyncio.sleep(0)

    async def receive(self) -> AsyncIterator[DuplexFrame]:
        sr = self.sample_rate_out
        for tone_ms, silence_ms, text in self.bursts:
            if text:
                yield DuplexFrame(text=text, sample_rate=sr)
            for _ in range(max(1, tone_ms // self.frame_ms)):
                await self._pace()
                if self._stop.is_set():
                    return
                yield DuplexFrame(pcm16=tone_pcm(self.frame_ms, sr), sample_rate=sr)
            for _ in range(max(1, silence_ms // self.frame_ms)):
                await self._pace()
                if self._stop.is_set():
                    return
                yield DuplexFrame(pcm16=silence_pcm(self.frame_ms, sr), sample_rate=sr)

    async def disconnect(self) -> None:
        self.disconnected = True
        self._stop.set()


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------

_MOCK_CAPS = Path(__file__).resolve().parent.parent / "capabilities" / "mock_s2s.yaml"


def mock_caps(**overrides: Any) -> Capabilities:
    """`capabilities/mock_s2s.yaml` with single fields overridden, e.g.
    `mock_caps(**{"tools.silent_close": "none"})`."""
    caps = Capabilities.load(_MOCK_CAPS)
    for name, value in overrides.items():
        caps.set_probed(name, value, ["test override"])
    return caps


@dataclass(slots=True)
class MockSet:
    """One of everything, sharing a clock (convenience for `vak run` and tests)."""

    clock: Clock | None = None
    stt_transcript: str = "现在几点了"
    llm_script: list[MockReply] = field(default_factory=list)

    def stt_factory(self) -> MockSTT:
        return MockSTT(self.stt_transcript)

    def providers(self, pipeline: str) -> dict[str, Any]:
        if pipeline == "cascade":
            return {"stt_factory": self.stt_factory, "llm": MockLLM(self.llm_script, self.clock), "tts": MockTTS(clock=self.clock)}
        if pipeline == "half_cascade":
            return {"audio_llm": MockAudioLLM(self.llm_script, clock=self.clock), "tts": MockTTS(clock=self.clock), "side_stt_factory": self.stt_factory}
        if pipeline == "s2s":
            return {"provider": MockRealtimeS2S(self.clock, script=list(self.llm_script))}
        return {"model": MockFullDuplex(clock=self.clock)}


__all__ = [
    "MockAudioLLM",
    "MockFullDuplex",
    "MockLLM",
    "MockRealtimeS2S",
    "MockReply",
    "MockSTT",
    "MockSet",
    "MockTTS",
    "mock_caps",
    "silence_pcm",
    "tone_pcm",
]
