"""Shared implementation for the two pipelines whose model emits text and
whose audio comes from a built-in TTS: cascade and half-cascade (spec 04 §6).

Covers: the local message list built in `open` (system prefix, text
history, state block at the tail), the text -> sentence -> guardrail -> TTS
path with `tool.call` reported as it arrives (rule 10) (or, when the TTS
takes incremental text, one text stream per reply: see `_open_text_stream`), `cancel_response`
as LLM cancel + TTS flush, `truncate` as `local_only`, `send_tool_result`
as a tool message (+ one more model call when `reply_required`),
`update_config` effective on the next request, and `close`.

Subclasses provide the input side (`send_audio`, `commit_turn`) and
`_stream` / `_cancel_model`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from vak.audio.silence import duration_ms
from vak.audio.text import flush_remainder, split_sentences
from vak.core.clock import Clock
from vak.core.defaults import UpstreamTimeouts
from vak.core.ids import IdGen
from vak.core.types import ErrorClass, ErrorScope, HistoryFormat, HistoryItem, ReplyModality, TurnId
from vak.upstream.base import (
    Ack,
    ConfigPatch,
    GuardrailHook,
    ResponseOrigin,
    ResponseOverrides,
    ResponseStatus,
    SessionInit,
    ToolDef,
)
from vak.upstream.capabilities import Capabilities
from vak.upstream.pipelines.base import TIMEOUT, PipelineBase
from vak.upstream.providers.base import STT, TTS, ChatMessage, LLMDelta, TTSChunk, TTSTextStream

log = logging.getLogger(__name__)


class _Blocked(Exception):
    """Raised inside a response task when the guardrail returned `block`."""


@dataclass(slots=True)
class _Synth:
    """One sentence on its way out: synthesis may run ahead, emission stays in order."""

    idx: int
    text: str
    request_id: str | None  # None: nothing to synthesise (text-only or empty after guardrail)
    submitted_ms: int | None = None  # handed to the TTS (spec 04 §2.4 `submitted_at_ms`)
    chunks: asyncio.Queue[TTSChunk | BaseException | None] = field(default_factory=asyncio.Queue)
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _ActiveResponse:
    response_id: str
    response_ref: str | None
    turn_id: TurnId | None
    origin: ResponseOrigin
    audio: bool
    task: asyncio.Task[None] | None = None
    sentences: list[str] = field(default_factory=list)
    text: str = ""
    audio_ms: int = 0
    audio_emitted: bool = False
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    assistant_msg: ChatMessage | None = None
    synths: list[_Synth] = field(default_factory=list)
    ready: asyncio.Queue[_Synth | None] = field(default_factory=asyncio.Queue)
    speaker: asyncio.Task[None] | None = None
    slots: asyncio.Semaphore | None = None
    # text-stream mode (TTS.supports_text_stream): one stream for the whole reply
    stream_open: asyncio.Task[TTSTextStream] | None = None
    stream_pushed: bool = False
    stream_marked: bool = False  # the engine reported its own sentence boundaries
    stream_first_push_ms: int | None = None


class SttTurn:
    """One per-turn STT stream: feeds frames, forwards partials, resolves the final."""

    def __init__(
        self,
        stt: STT,
        turn_id: TurnId,
        on_partial: Callable[[str], None],
        on_final: Callable[[str, bool], None],
    ) -> None:
        self.stt = stt
        self.turn_id = turn_id
        self.final: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.last_partial = ""
        self.timed_out = False
        self.audio_ms = 0
        self._seqs: set[int] = set()
        self._on_partial = on_partial
        self._on_final = on_final
        self._task: asyncio.Task[None] | None = None
        self._cancelled = False

    async def start(self) -> None:
        await self.stt.start()
        self._task = asyncio.create_task(self._pump())

    def accept_seq(self, seq: int) -> bool:
        """(turn_id, seq) de-duplication (spec 04 §3.2)."""
        if seq in self._seqs:
            return False
        self._seqs.add(seq)
        return True

    async def feed(self, pcm16: bytes, sample_rate: int) -> None:
        self.audio_ms += duration_ms(pcm16, sample_rate)
        await self.stt.feed(pcm16)

    async def finish(self) -> None:
        await self.stt.finish()

    async def cancel(self) -> None:
        self._cancelled = True
        await self.stt.cancel()
        if self._task is not None:
            self._task.cancel()
        if not self.final.done():
            self.final.cancel()

    async def _pump(self) -> None:
        async for t in self.stt.results():
            if self._cancelled:
                return
            if t.final:
                if not self.final.done():
                    self.final.set_result(t.text)
                self._on_final(t.text, self.timed_out)  # late=True when the final arrives after the timeout
                return
            self.last_partial = t.text
            self._on_partial(t.text)


class TextOutPipeline(PipelineBase):
    """Base for cascade and half-cascade. See module docstring."""

    def __init__(
        self,
        tts: TTS,
        caps: Capabilities,
        clock: Clock | None = None,
        timeouts: UpstreamTimeouts | None = None,
        guardrail: GuardrailHook | None = None,
        ids: IdGen | None = None,
    ) -> None:
        super().__init__(caps, clock, timeouts, ids)
        self._tts = tts  # shared voice config (spec 04 §6.1): never re-configured here
        self._guardrail = guardrail
        self._system = ""
        self._tools: list[ToolDef] = []
        self._history: list[ChatMessage] = []
        self._state_block_tail: str | None = None
        self._turns: dict[str, tuple[str, Ack]] = {}  # str(turn_id) -> (mode, first ack)
        self._refs: dict[str, str] = {}  # response_ref -> response_id (idempotency)
        self._active: _ActiveResponse | None = None
        self._by_response: dict[str, _ActiveResponse] = {}
        self._truncated: dict[tuple[str, int], int] = {}
        self._call_turn: dict[str, TurnId | None] = {}
        self._config_version: str | None = None

    # -- subclass hooks ------------------------------------------------------

    def _stream(
        self, messages: list[ChatMessage], tools: list[ToolDef] | None, tool_choice: str | None
    ) -> AsyncIterator[LLMDelta]:
        raise NotImplementedError

    async def _cancel_model(self) -> None:
        raise NotImplementedError

    async def _on_close(self) -> None:
        """Release input-side resources."""

    # -- state block (spec 05 §2.3: at the message TAIL for text LLMs) -------

    def set_state_block_tail(self, text: str | None) -> None:
        """Set the state block the context layer wants appended to the end of
        every request. `SessionInit.instructions` stays the stable system
        prefix so the prefix cache keeps hitting (spec 04 §6.2)."""
        self._state_block_tail = text

    def _build_messages(self, extra_instructions: str | None) -> list[ChatMessage]:
        msgs = [ChatMessage(role="system", content=self._system), *self._history]
        if extra_instructions:
            msgs.append(ChatMessage(role="system", content=extra_instructions))
        if self._state_block_tail:
            msgs.append(ChatMessage(role="system", content=self._state_block_tail))
        return msgs

    # -- open / close --------------------------------------------------------

    async def open(self, init: SessionInit) -> Ack:
        async with self._lock:
            if self._closed:
                return Ack.invalid_state("closed")
            if self._open:
                return Ack.invalid_state("already_open")
            if init.resume_handle is not None and self.capabilities.get("session.resumption") != "handle":
                return Ack.unsupported("session.resumption")
            self._system = init.instructions
            self._tools = list(init.tools)
            self._history = [m for item in init.history for m in self._history_to_messages(item, init.history_format)]
            self._config_version = init.state_block_version
            self._mirror.reset()
            local_ids = [h.local_entry_id for h in init.history]
            self._mirror.seed(local_ids, [h.role for h in init.history])
            self.session_id = self.ids.new("us")
            self._emitter.session_id = self.session_id
            self._open = True
            # the local list is the upstream: each seeded item is confirmed at once
            for i, h in enumerate(init.history):
                self._emit(self._emitter.item_echo(item_id=f"h{i}", kind=h.role, local_id=h.local_entry_id))
            effective = {"history_items_sent": len(init.history), "tools_sent": len(self._tools)}
            self._emit(self._emitter.session_opened(self.session_id, effective))
            return Ack.success(session_id=self.session_id, effective=effective)

    @staticmethod
    def _history_to_messages(item: HistoryItem, fmt: HistoryFormat) -> list[ChatMessage]:
        if item.role == "user":
            return [ChatMessage(role="user", content=item.text)]
        if item.role == "assistant":
            return [ChatMessage(role="assistant", content=item.text)]
        if fmt is HistoryFormat.PAIRS_TOOLS_AS_TEXT:
            # the policy layer rendered the text; keep it as an assistant line
            return [ChatMessage(role="assistant", content=item.text)]
        if item.role == "tool_call":
            call = {
                "id": item.tool_call_id,
                "type": "function",
                "function": {"name": item.tool_name, "arguments": json.dumps(item.tool_args or {}, ensure_ascii=False)},
            }
            return [ChatMessage(role="assistant", content=item.text or "", tool_calls=[call])]
        return [ChatMessage(role="tool", content=item.text, tool_call_id=item.tool_call_id, name=item.tool_name)]

    async def close(self, reason: str) -> Ack:
        async with self._lock:
            if self._closed:
                return Ack.success()
            self._closed = True
            if self._active is not None:
                await self._abort_active()
            self._cancel_all_active(reason)
            await self._on_close()
            self._emit(self._emitter.session_closed(reason, "client"))
            self._emitter.close()
            return Ack.success()

    # -- turn registry (shared by both input sides) ---------------------------

    def _turn_state(self, turn_id: TurnId) -> tuple[str, Ack] | None:
        return self._turns.get(str(turn_id))

    def _record_turn(self, turn_id: TurnId, mode: str, ack: Ack) -> Ack:
        self._turns[str(turn_id)] = (mode, ack)
        return ack

    def _repeat_commit(self, turn_id: TurnId, mode: str) -> Ack | None:
        """Spec 04 §3.3 idempotency: repeated commit returns the first result;
        discard after commit (or anything after discard) is invalid_state."""
        prev = self._turn_state(turn_id)
        if prev is None:
            return None
        prev_mode, prev_ack = prev
        if prev_mode == "commit" and mode == "commit":
            return prev_ack
        return Ack.invalid_state("already_committed" if prev_mode == "commit" else "discarded")

    async def send_text(self, turn_id: TurnId, text: str) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        if (prev := self._repeat_commit(turn_id, "commit")) is not None:
            return prev
        self._append_user(turn_id, text)
        return self._record_turn(turn_id, "commit", Ack.success(text=text))

    def _append_user(self, turn_id: TurnId, text: str) -> None:
        self._history.append(ChatMessage(role="user", content=text))
        self._emit(self._emitter.committed(turn_id, "client"))

    # -- request_response ----------------------------------------------------

    async def request_response(
        self, turn_id: TurnId, response_ref: str, overrides: ResponseOverrides | None = None
    ) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        return await self._launch(turn_id, response_ref, overrides, ResponseOrigin.CLIENT, first=True)

    async def _launch(
        self,
        turn_id: TurnId | None,
        response_ref: str,
        overrides: ResponseOverrides | None,
        origin: ResponseOrigin,
        *,
        first: bool,
    ) -> Ack:
        if response_ref in self._refs:
            return Ack.success(response_id=self._refs[response_ref])
        if (bad := self._check_overrides(overrides)) is not None:
            return bad
        if self._active is not None:  # response.single_slot = yes: LLM.cancel() has no request id
            return Ack.slot_busy()
        tools = self._tools if overrides is None or overrides.tools is None else overrides.tools
        tool_choice = overrides.tool_choice if overrides else None
        audio = overrides is None or overrides.modalities is None or "audio" in overrides.modalities
        messages = self._build_messages(overrides.instructions if overrides else None)
        rid = self.ids.new("resp")
        self._refs[response_ref] = rid
        ar = _ActiveResponse(rid, response_ref, turn_id, origin, audio)
        self._active = ar
        self._by_response[rid] = ar
        self._start_response(rid, origin, response_ref, turn_id)
        ar.task = asyncio.create_task(self._run_response(ar, messages, tools or None, tool_choice, first))
        return Ack.success(response_id=rid)

    def _prepare_stream(
        self, messages: list[ChatMessage], tools: list[ToolDef] | None, tool_choice: str | None, first: bool
    ) -> AsyncIterator[LLMDelta]:
        """Hook for the half-cascade to attach this turn's audio to the first request."""
        return self._stream(messages, tools, tool_choice)

    async def _run_response(
        self,
        ar: _ActiveResponse,
        messages: list[ChatMessage],
        tools: list[ToolDef] | None,
        tool_choice: str | None,
        first: bool,
    ) -> None:
        rid = ar.response_id
        buffer = ""
        idx = 0
        if ar.audio and getattr(self._tts, "supports_text_stream", False):
            self._open_text_stream(ar)
        tokens = ar.stream_open is not None and self._guardrail is None
        try:
            it = self._prepare_stream(messages, tools, tool_choice, first).__aiter__()
            got: Awaitable[LLMDelta] = it.__anext__()
            res = await self._with_timeout(
                got, self.timeouts.first_delta_ms, "first_delta", ErrorScope.RESPONSE, turn_id=ar.turn_id, response_id=rid
            )
            if res is TIMEOUT:
                self._finish_response(rid, ResponseStatus.FAILED, ReplyModality.NONE, reason="first_delta_timeout")
                return
            delta: LLMDelta = res  # type: ignore[assignment]
            while True:
                if rid in self._cancelled:
                    return
                if delta.tool_call is not None:
                    # rule 10: reported as soon as the arguments are complete, before any TTS of later text
                    self._report_tool_call(ar, delta.tool_call)
                if delta.text:
                    ar.text += delta.text
                    self._emit(self._emitter.text_delta(rid, str(idx), delta.text, ar.turn_id, ar.response_ref))
                    if tokens:  # no guardrail to wait for: every delta goes straight to the TTS
                        await self._push(ar, delta.text)
                    else:
                        buffer += delta.text
                        sentences, buffer = split_sentences(buffer, first=idx == 0)
                        for s in sentences:
                            await self._speak(ar, idx, s, is_last=False)
                            idx += 1
                if delta.done:
                    break
                try:
                    delta = await it.__anext__()
                except StopAsyncIteration:
                    break
            for s in flush_remainder(buffer):
                await self._speak(ar, idx, s, is_last=True)
                idx += 1
            await self._end_speech(ar)
            if rid in self._cancelled:
                return
            self._record_assistant(ar)
            self._finish_response(rid, ResponseStatus.COMPLETED, self._modality(ar))
        except _Blocked:
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # provider failure: report, never retry (rule 6)
            log.exception("response %s failed", rid)
            await self._stop_speech(ar)
            err = self._errors.map(type(exc).__name__, str(exc), scope=ErrorScope.RESPONSE, ref={"response_id": rid})
            self._emit(self._emitter.error(err, turn_id=ar.turn_id, response_id=rid))
            self._finish_response(rid, ResponseStatus.FAILED, self._modality(ar), reason=type(exc).__name__)
        finally:
            if self._active is ar:
                self._active = None

    def _report_tool_call(self, ar: _ActiveResponse, call: dict[str, Any]) -> None:
        call_id = str(call.get("id") or self.ids.new("call"))
        name = str(call.get("name") or "")
        args = call.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                err = self._errors.map("tool_parse", f"bad arguments for {name}", scope=ErrorScope.CALL)
                self._emit(self._emitter.error(err, response_id=ar.response_id, call_id=call_id))
                args = {"_raw": call.get("arguments")}
        ar.tool_calls.append({"id": call_id, "name": name, "arguments": args or {}})
        self._call_turn[call_id] = ar.turn_id
        self._emit(
            self._emitter.tool_call(
                call_id, name, args or {}, ar.response_id, ar.turn_id, before_commit=False, response_ref=ar.response_ref
            )
        )

    @staticmethod
    def _modality(ar: _ActiveResponse) -> ReplyModality:
        if ar.audio_emitted:
            return ReplyModality.AUDIO
        if ar.text:
            return ReplyModality.TEXT_ONLY
        return ReplyModality.NONE

    def _record_assistant(self, ar: _ActiveResponse) -> None:
        tool_calls = [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)},
            }
            for c in ar.tool_calls
        ] or None
        if not ar.text and tool_calls is None:
            return
        ar.assistant_msg = ChatMessage(role="assistant", content=ar.text, tool_calls=tool_calls)
        self._history.append(ar.assistant_msg)

    async def _speak(self, ar: _ActiveResponse, idx: int, text: str, *, is_last: bool) -> None:
        """Guardrail hook (spec 04 §6.4) then queue one sentence for TTS.

        Synthesis of up to `tts_lookahead` later sentences overlaps the
        emission of the current one, so each next sentence's TTS first chunk
        is off the critical path (handbook first-audio §4). The sentence
        event and its audio still leave in sentence order."""
        rid = ar.response_id
        if ar.audio and self._guardrail is not None:
            t0 = self.clock.now_ms()
            verdict = await self._guardrail(rid, ar.turn_id, idx, text, is_last)
            spent = self.clock.now_ms() - t0
            if spent > self.timeouts.guardrail_budget_ms:
                log.warning("guardrail took %d ms (budget %d)", spent, self.timeouts.guardrail_budget_ms)
            if verdict.action == "block":
                await self._cancel_model()
                await self._end_speech(ar)  # sentences already approved still play out
                self._mark_cancelled(rid)
                self._record_assistant(ar)
                self._finish_response(rid, ResponseStatus.CANCELLED, self._modality(ar), reason="guardrail")
                raise _Blocked
            if verdict.action == "replace":
                text = verdict.text or ""
        if ar.stream_open is not None:  # approved sentence into the reply's text stream
            await self._push(ar, text)
            return
        ar.sentences.append(text)
        if ar.speaker is None:
            ar.slots = asyncio.Semaphore(1 + max(0, self.timeouts.tts_lookahead))
            ar.speaker = asyncio.create_task(self._speaker(ar))
        synth = _Synth(idx, text, f"{rid}:{idx}" if ar.audio and text else None)
        if synth.request_id is not None:
            assert ar.slots is not None
            await ar.slots.acquire()  # back-pressure on the LLM loop once the lookahead is full
            synth.submitted_ms = self.clock.now_ms()
            synth.task = asyncio.create_task(self._synthesize(ar, synth))
        ar.synths.append(synth)
        ar.ready.put_nowait(synth)

    async def _synthesize(self, ar: _ActiveResponse, synth: _Synth) -> None:
        assert synth.request_id is not None and ar.slots is not None
        try:
            async for chunk in self._tts.synthesize(synth.text, synth.request_id):
                if ar.response_id in self._cancelled:
                    break
                synth.chunks.put_nowait(chunk)
            synth.chunks.put_nowait(None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # handed to the speaker, which fails the response in order
            synth.chunks.put_nowait(exc)
        finally:
            ar.slots.release()

    async def _speaker(self, ar: _ActiveResponse) -> None:
        rid = ar.response_id
        while (synth := await ar.ready.get()) is not None:
            if rid in self._cancelled:
                return
            self._emit(
                self._emitter.sentence(
                    rid, synth.idx, synth.text, None, ar.turn_id, ar.response_ref, submitted_at_ms=synth.submitted_ms
                )
            )
            if synth.task is None:
                continue
            while (chunk := await synth.chunks.get()) is not None:
                if isinstance(chunk, BaseException):
                    raise chunk
                if rid in self._cancelled:
                    return
                dur = duration_ms(chunk.pcm16, chunk.sample_rate)
                ar.audio_ms += dur
                ar.audio_emitted = True
                self._emit(
                    self._emitter.audio_delta(
                        rid, str(synth.idx), chunk.pcm16, chunk.sample_rate, dur, ar.turn_id, ar.response_ref,
                        sentence_index=synth.idx,
                    )
                )

    async def _end_speech(self, ar: _ActiveResponse) -> None:
        """No more sentences: wait until everything queued has been emitted."""
        if ar.stream_open is not None:
            await self._end_text_stream(ar)
            return
        if ar.speaker is None:
            return
        ar.ready.put_nowait(None)
        try:
            await asyncio.shield(ar.speaker)
        except Exception:
            await self._stop_speech(ar)
            raise

    async def _stop_speech(self, ar: _ActiveResponse) -> None:
        """Flush every synthesis still running and stop emitting (tts_flush, spec 02 §2.7)."""
        if ar.stream_open is not None:
            await self._drop_text_stream(ar)
        for synth in ar.synths:
            if synth.task is not None and not synth.task.done():
                assert synth.request_id is not None
                await self._tts.flush(synth.request_id)
                synth.task.cancel()
        if ar.speaker is not None and not ar.speaker.done() and ar.speaker is not asyncio.current_task():
            ar.speaker.cancel()

    # -- text-stream mode ------------------------------------------------------

    def _open_text_stream(self, ar: _ActiveResponse) -> None:
        """One TTS text stream per reply, opened at reply start so its handshake
        overlaps the LLM's first token. Without a guardrail every LLM delta is
        pushed as it arrives and the engine segments sentences; with one, each
        approved sentence is pushed (§6.4 still holds: nothing unapproved reaches
        the TTS). Outward events stay per sentence: a `response.sentence` for
        each sentence the engine reports (`TTSChunk.sentence`), its audio tagged
        with that index. An engine that reports no boundaries gets one sentence
        for the whole reply."""
        ar.stream_open = asyncio.create_task(self._tts.open_text_stream(self._stream_rid(ar)))  # type: ignore[attr-defined]
        ar.speaker = asyncio.create_task(self._stream_speaker(ar))

    @staticmethod
    def _stream_rid(ar: _ActiveResponse) -> str:
        return f"{ar.response_id}:stream"

    async def _push(self, ar: _ActiveResponse, text: str) -> None:
        if not text or ar.stream_open is None:
            return
        stream = await ar.stream_open  # an open failure fails the response here
        if ar.stream_first_push_ms is None:
            ar.stream_first_push_ms = self.clock.now_ms()
        await stream.push(text)
        ar.stream_pushed = True

    async def _stream_speaker(self, ar: _ActiveResponse) -> None:
        rid = ar.response_id
        assert ar.stream_open is not None
        stream = await ar.stream_open
        idx = -1
        try:
            async for chunk in stream.audio():
                if rid in self._cancelled:
                    return
                if not chunk.pcm16:
                    continue
                if chunk.sentence is not None or idx < 0:
                    idx += 1
                    ar.stream_marked = ar.stream_marked or chunk.sentence is not None
                    text = chunk.sentence if chunk.sentence is not None else ar.text  # unmarked: best effort so far
                    ar.sentences.append(text)
                    submitted = ar.stream_first_push_ms if idx == 0 else None  # later boundaries are the engine's
                    self._emit(
                        self._emitter.sentence(rid, idx, text, None, ar.turn_id, ar.response_ref, submitted_at_ms=submitted)
                    )
                dur = duration_ms(chunk.pcm16, chunk.sample_rate)
                ar.audio_ms += dur
                ar.audio_emitted = True
                self._emit(
                    self._emitter.audio_delta(
                        rid, str(idx), chunk.pcm16, chunk.sample_rate, dur, ar.turn_id, ar.response_ref,
                        sentence_index=idx,
                    )
                )
        finally:
            stream.close()

    async def _end_text_stream(self, ar: _ActiveResponse) -> None:
        """No more text: end the stream and wait until its audio is emitted."""
        assert ar.stream_open is not None and ar.speaker is not None
        if not ar.stream_pushed:  # tool call only, or nothing speakable: no audio to wait for
            await self._drop_text_stream(ar)
            return
        try:
            stream = await ar.stream_open
            await stream.end()
            await asyncio.shield(ar.speaker)
        except Exception:
            await self._drop_text_stream(ar)
            raise
        if not ar.stream_marked and ar.sentences:
            ar.sentences[0] = ar.text  # the single unmarked sentence is the whole reply

    async def _drop_text_stream(self, ar: _ActiveResponse) -> None:
        """Flush and release the stream wherever it is (opening, streaming, done)."""
        opening = ar.stream_open
        assert opening is not None
        await self._tts.flush(self._stream_rid(ar))
        if ar.speaker is not None and ar.speaker is not asyncio.current_task():
            if not ar.speaker.done():
                ar.speaker.cancel()
            elif not ar.speaker.cancelled():
                ar.speaker.exception()  # retrieved: an open failure is reported by `_push`, not here
        if not opening.done():
            opening.cancel()
        elif not opening.cancelled() and opening.exception() is None:
            opening.result().close()

    # -- cancel --------------------------------------------------------------

    async def cancel_response(self, response_id: str) -> Ack:
        if not self._tracker.is_active(response_id):
            return Ack.success()  # already finished or unknown: idempotent (spec 04 §3.5)
        self._mark_cancelled(response_id)
        ar = self._by_response.get(response_id)
        if ar is not None and self._active is ar:
            await self._abort_active()
        self._finish_response(response_id, ResponseStatus.CANCELLED, self._modality(ar) if ar else ReplyModality.NONE)
        return Ack.success(confirmed=True)

    async def _abort_active(self) -> None:
        ar = self._active
        if ar is None:
            return
        self._mark_cancelled(ar.response_id)
        self._active = None
        await self._cancel_model()
        await self._stop_speech(ar)
        if ar.task is not None and not ar.task.done() and ar.task is not asyncio.current_task():
            ar.task.cancel()
        if ar.assistant_msg is None:
            self._record_assistant(ar)  # local record keeps what was generated; truncate trims it

    # -- truncate (local_only) -------------------------------------------------

    async def truncate(self, item_id: str, audio_end_ms: int, played_text: str | None = None) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        mode = self.capabilities.get("truncate.mode")
        if mode == "local_only":
            ar = self._latest_response()
            if ar is None or not item_id.isdigit() or int(item_id) >= len(ar.sentences):
                return Ack.invalid_state("unknown_item")
            idx = int(item_id)
            prev = self._truncated.get((ar.response_id, idx))
            if prev is not None and audio_end_ms >= prev:
                return Ack.success(local_only=True)  # only monotonically smaller values are applied
            self._truncated[(ar.response_id, idx)] = audio_end_ms
            if played_text is None:
                kept = ar.sentences[:idx] + ([ar.sentences[idx]] if audio_end_ms > 0 else [])
                played_text = " ".join(kept)
            if ar.assistant_msg is not None:
                ar.assistant_msg.content = played_text
            return Ack.success(local_only=True)
        return Ack.unsupported("truncate.mode")

    def _latest_response(self) -> _ActiveResponse | None:
        return next(reversed(self._by_response.values()), None) if self._by_response else None

    # -- tool results (spec 04 §3.7, 级联行) ----------------------------------

    async def send_tool_result(self, call_id: str, result: dict[str, Any], reply_required: bool) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        if not self._mirror.has_call(call_id):
            return Ack.invalid_state("unknown_call")
        if self._mirror.result_sent(call_id):
            return Ack.success(duplicate=True)
        name = self._mirror.calls[call_id].name
        self._history.append(
            ChatMessage(role="tool", content=json.dumps(result, ensure_ascii=False), tool_call_id=call_id, name=name)
        )
        self._mirror.mark_result_sent(call_id)
        if not reply_required:
            return Ack.success()  # by_omission: no model call
        return await self._launch(self._call_turn.get(call_id), f"tool:{call_id}", None, ResponseOrigin.AUTO_TOOL, first=False)

    # -- update_config (spec 04 §3.8: effective on the next request) ---------

    async def update_config(
        self, patch: ConfigPatch, mode: Literal["live", "append", "next_session"]
    ) -> Ack:
        async with self._lock:
            if (bad := self._require_open()) is not None:
                return bad
            if mode == "next_session":
                return Ack.success(deferred=True)  # the rebuilder carries it into the next open
            if mode == "append":
                if not self.capabilities.get("config.append_channels"):
                    return Ack.unsupported("config.append_channels")
                return Ack.unsupported("config.append_channels")
            if patch.state_block_version is not None and patch.state_block_version == self._config_version:
                return Ack.success(duplicate=True)
            targets = (("instructions", patch.instructions), ("tools", patch.tools), ("voice", patch.voice))
            for target, value in targets:
                if value is not None and self.capabilities.get(f"config.update.{target}") not in ("live_full", "live_sparse"):
                    return Ack.unsupported(f"config.update.{target}")
            if patch.instructions is not None:
                self._system = patch.instructions
            if patch.tools is not None:
                self._tools = list(patch.tools)
            if patch.voice is not None:
                self._tts.voice = patch.voice
            if patch.state_block_version is not None:
                self._config_version = patch.state_block_version
            return Ack.success()

    # -- error helper for input-side subclasses ------------------------------

    def _stt_timeout_class(self) -> ErrorClass:
        return ErrorClass.TRANSIENT  # spec 04 §3.3: commit_timeout is transient
