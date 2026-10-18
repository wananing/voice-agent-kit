"""Half-cascade pipeline: audio LLM (text out) -> TTS (spec 04 §6;
handbook 02-architectures/half-cascade.md).

Input side: frames are fed live when the model declares `streaming_input`,
otherwise accumulated and submitted whole at `commit_turn`
(`uplink.streaming_input = whole_utterance`), with `uplink.backpressure`
once the buffer passes `backpressure_ms`. The user transcript comes from an
optional side STT (`cascade.user_transcript = side_request`); without it
the history stores a placeholder (history must be text, spec 04 §6.2).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Literal

from vak.audio.silence import duration_ms
from vak.core.clock import Clock
from vak.core.defaults import UpstreamTimeouts
from vak.core.ids import IdGen
from vak.core.types import ErrorScope, TurnId
from vak.upstream.base import Ack, AudioMeta, GuardrailHook, ToolDef
from vak.upstream.capabilities import Capabilities, CapField, Evidence
from vak.upstream.pipelines.base import TIMEOUT
from vak.upstream.pipelines.textout import SttTurn, TextOutPipeline
from vak.upstream.providers.base import STT, TTS, AudioLLM, AudioTurn, ChatMessage, LLMDelta

NO_TRANSCRIPT_PLACEHOLDER = "[voice input]"


@dataclass(slots=True)
class _Utterance:
    turn_id: TurnId
    sample_rate: int
    chunks: list[bytes] = field(default_factory=list)
    ms: int = 0
    seqs: set[int] = field(default_factory=set)
    backpressure_sent: bool = False
    side: SttTurn | None = None


class HalfCascadeUpstream(TextOutPipeline):
    """`HalfCascadeUpstream(audio_llm, tts, side_stt_factory=None, caps, ...)`."""

    def __init__(
        self,
        audio_llm: AudioLLM,
        tts: TTS,
        side_stt_factory: Callable[[], STT] | None = None,
        caps: Capabilities | None = None,
        clock: Clock | None = None,
        timeouts: UpstreamTimeouts | None = None,
        guardrail: GuardrailHook | None = None,
        ids: IdGen | None = None,
        backpressure_ms: int = 30_000,
    ) -> None:
        from vak.upstream.pipelines import load_builtin_caps

        caps = caps or load_builtin_caps("half_cascade_builtin")
        # the declaration follows what was actually composed
        caps.fields["cascade.user_transcript"] = CapField(
            "side_request" if side_stt_factory else "none", Evidence.CODE_READ, ["HalfCascadeUpstream.__init__"]
        )
        caps.fields["uplink.streaming_input"] = CapField(
            "streaming" if audio_llm.streaming_input else "whole_utterance", Evidence.CODE_READ, ["AudioLLM.streaming_input"]
        )
        super().__init__(tts, caps, clock, timeouts, guardrail, ids)
        self._audio_llm = audio_llm
        self._side_stt_factory = side_stt_factory
        self._backpressure_ms = backpressure_ms
        self._utt: _Utterance | None = None
        self._pending_audio: AudioTurn | None = None

    # -- model hooks ---------------------------------------------------------

    def _stream(
        self, messages: list[ChatMessage], tools: list[ToolDef] | None, tool_choice: str | None
    ) -> AsyncIterator[LLMDelta]:
        return self._audio_llm.stream(messages, None, tools)

    def _prepare_stream(
        self, messages: list[ChatMessage], tools: list[ToolDef] | None, tool_choice: str | None, first: bool
    ) -> AsyncIterator[LLMDelta]:
        audio = self._pending_audio if first else None
        self._pending_audio = None
        return self._audio_llm.stream(messages, audio, tools)

    async def _cancel_model(self) -> None:
        await self._audio_llm.cancel()

    async def _on_close(self) -> None:
        if self._utt is not None and self._utt.side is not None:
            await self._utt.side.cancel()
        self._utt = None

    # -- input side -----------------------------------------------------------

    async def send_audio(self, frame: bytes, meta: AudioMeta) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        if meta.turn_id is None:
            return Ack.invalid_state("turn_id")
        if self._turn_state(meta.turn_id) is not None:
            return Ack.invalid_state("already_committed")
        if self._utt is None or self._utt.turn_id != meta.turn_id:
            if self._utt is not None and self._utt.side is not None:
                await self._utt.side.cancel()
            self._utt = _Utterance(meta.turn_id, meta.sample_rate)
            if self._side_stt_factory is not None:
                self._utt.side = await self._new_side_stt(meta.turn_id)
        utt = self._utt
        if meta.seq in utt.seqs:
            return Ack.success(duplicate=True)
        utt.seqs.add(meta.seq)
        utt.ms += duration_ms(frame, meta.sample_rate)
        if self._audio_llm.streaming_input:
            await self._audio_llm.feed(frame)
        else:
            utt.chunks.append(frame)  # whole_utterance: accumulate, never drop (spec 04 §3.2)
            if utt.ms > self._backpressure_ms and not utt.backpressure_sent:
                utt.backpressure_sent = True
                self._emit(self._emitter.backpressure(utt.ms, meta.turn_id))
        if utt.side is not None:
            await utt.side.feed(frame, meta.sample_rate)
        return Ack.success()

    async def _new_side_stt(self, turn_id: TurnId) -> SttTurn:
        assert self._side_stt_factory is not None

        def on_partial(text: str) -> None:
            self._emit(self._emitter.transcript_user_partial(turn_id, text))

        def on_final(text: str, late: bool) -> None:
            self._emit(self._emitter.transcript_user_final(turn_id, text, late=late))

        st = SttTurn(self._side_stt_factory(), turn_id, on_partial, on_final)
        await st.start()
        return st

    async def commit_turn(self, turn_id: TurnId, mode: Literal["commit", "discard"]) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        if (prev := self._repeat_commit(turn_id, mode)) is not None:
            return prev
        utt = self._utt if self._utt is not None and self._utt.turn_id == turn_id else None
        if mode == "discard":
            if utt is not None:
                if utt.side is not None:
                    await utt.side.cancel()
                self._utt = None
            return self._record_turn(turn_id, "discard", Ack.success(discarded=True))
        if utt is None:
            return Ack.invalid_state("no_audio")
        self._utt = None
        text = NO_TRANSCRIPT_PLACEHOLDER
        ack = Ack.success()
        if utt.side is not None:
            await utt.side.finish()
            res = await self._with_timeout(
                asyncio.shield(utt.side.final),
                self.timeouts.stt_final_ms,
                "commit_timeout",
                ErrorScope.SESSION,
                error_class=self._stt_timeout_class(),
                turn_id=turn_id,
            )
            if res is TIMEOUT:
                utt.side.timed_out = True
                text = utt.side.last_partial or NO_TRANSCRIPT_PLACEHOLDER
                self._emit(self._emitter.transcript_user_final(turn_id, text, final_by_timeout=True))
                ack = Ack.timeout("stt_final")
                ack.data = {"text": text, "final_by_timeout": True}
            else:
                text = str(res)
                ack = Ack.success(text=text)
        pcm = b"".join(utt.chunks)
        hint = None if text == NO_TRANSCRIPT_PLACEHOLDER else text
        self._pending_audio = AudioTurn(pcm16=pcm, sample_rate=utt.sample_rate, transcript_hint=hint)
        self._append_user(turn_id, text)  # history stays text; only this turn is audio (spec 04 §6.2)
        return self._record_turn(turn_id, "commit", ack)
