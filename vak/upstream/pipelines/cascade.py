"""Cascade pipeline: STT -> text LLM -> TTS composed into the nine primitives
(spec 04 §6; handbook 02-architectures/cascade.md).

Input side: one STT stream per turn, `transcript.user.partial/final`,
`commit_turn` = STT finish + wait for the final (`final_by_timeout` after
`stt_final_ms`). Everything after the user text is in `TextOutPipeline`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

from vak.core.clock import Clock
from vak.core.defaults import UpstreamTimeouts
from vak.core.ids import IdGen
from vak.core.types import ErrorScope, TurnId
from vak.upstream.base import Ack, AudioMeta, GuardrailHook, ToolDef
from vak.upstream.capabilities import Capabilities
from vak.upstream.pipelines.base import TIMEOUT
from vak.upstream.pipelines.textout import SttTurn, TextOutPipeline
from vak.upstream.providers.base import LLM, STT, TTS, ChatMessage, LLMDelta


class CascadeUpstream(TextOutPipeline):
    """`CascadeUpstream(stt_factory, llm, tts, caps, ...)`.

    `stt_factory` is called once per turn (streaming STT is per utterance).
    `tts` is the shared voice configuration (spec 04 §6.1); `voice_config`
    is recorded for the capability declaration only.
    """

    def __init__(
        self,
        stt_factory: Callable[[], STT],
        llm: LLM,
        tts: TTS,
        caps: Capabilities | None = None,
        clock: Clock | None = None,
        timeouts: UpstreamTimeouts | None = None,
        guardrail: GuardrailHook | None = None,
        ids: IdGen | None = None,
        voice_config: dict[str, Any] | None = None,
    ) -> None:
        from vak.upstream.pipelines import load_builtin_caps

        super().__init__(tts, caps or load_builtin_caps("cascade_builtin"), clock, timeouts, guardrail, ids)
        self._stt_factory = stt_factory
        self._llm = llm
        self.voice_config = dict(voice_config or {})
        self._stt: SttTurn | None = None
        self._warmed_at: int | None = None
        self._warmups: set[asyncio.Task[None]] = set()

    # -- model hooks ---------------------------------------------------------

    def _stream(
        self, messages: list[ChatMessage], tools: list[ToolDef] | None, tool_choice: str | None
    ) -> AsyncIterator[LLMDelta]:
        return self._llm.stream(messages, tools, tool_choice)

    async def _cancel_model(self) -> None:
        await self._llm.cancel()

    async def _on_close(self) -> None:
        if self._stt is not None:
            await self._stt.cancel()
            self._stt = None
        for t in self._warmups:
            t.cancel()

    def _warm(self) -> None:
        """Connect to the LLM and TTS while the user is still talking, so the
        first request after commit does not pay TCP + TLS (handbook first-audio §4)."""
        now = self.clock.now_ms()
        if self._warmed_at is not None and now - self._warmed_at < self.timeouts.warmup_interval_ms:
            return
        self._warmed_at = now
        for provider in (self._llm, self._tts):
            warmup = getattr(provider, "warmup", None)  # optional on LLM / TTS
            if warmup is not None:
                t = asyncio.create_task(warmup())
                self._warmups.add(t)
                t.add_done_callback(self._warmups.discard)

    # -- input side -----------------------------------------------------------

    async def send_audio(self, frame: bytes, meta: AudioMeta) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        if meta.turn_id is None:
            return Ack.invalid_state("turn_id")  # spec 04 §3.2: only normalised, non-zero turn ids
        if self._turn_state(meta.turn_id) is not None:
            return Ack.invalid_state("already_committed")
        if self._stt is None or self._stt.turn_id != meta.turn_id:
            if self._stt is not None:
                await self._stt.cancel()  # previous turn neither committed nor discarded
            self._stt = await self._new_stt(meta.turn_id)
            self._warm()
        if not self._stt.accept_seq(meta.seq):
            return Ack.success(duplicate=True)
        await self._stt.feed(frame, meta.sample_rate)
        return Ack.success()

    async def _new_stt(self, turn_id: TurnId) -> SttTurn:
        def on_partial(text: str) -> None:
            self._emit(self._emitter.transcript_user_partial(turn_id, text))

        def on_final(text: str, late: bool) -> None:
            if late:
                self._emit(self._emitter.transcript_user_final(turn_id, text, late=True))
            else:
                self._emit(self._emitter.transcript_user_final(turn_id, text))

        st = SttTurn(self._stt_factory(), turn_id, on_partial, on_final)
        await st.start()
        return st

    async def commit_turn(self, turn_id: TurnId, mode: Literal["commit", "discard"]) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        if (prev := self._repeat_commit(turn_id, mode)) is not None:
            return prev
        stt = self._stt if self._stt is not None and self._stt.turn_id == turn_id else None
        if mode == "discard":
            if stt is not None:
                await stt.cancel()
                self._stt = None
            return self._record_turn(turn_id, "discard", Ack.success(discarded=True))
        if stt is None:
            return Ack.invalid_state("no_audio")
        min_ms = self.capabilities.get("turn.commit_min_ms")
        if isinstance(min_ms, int) and stt.audio_ms < min_ms:
            return Ack.invalid_state("too_short")
        await stt.finish()
        res = await self._with_timeout(
            asyncio.shield(stt.final),
            self.timeouts.stt_final_ms,
            "commit_timeout",
            ErrorScope.SESSION,
            error_class=self._stt_timeout_class(),
            turn_id=turn_id,
        )
        self._stt = None  # the pump stays alive to report a late final with late=True
        if res is TIMEOUT:
            stt.timed_out = True
            text = stt.last_partial
            self._emit(self._emitter.transcript_user_final(turn_id, text, final_by_timeout=True))
            ack = Ack.timeout("stt_final")
            ack.data = {"text": text, "final_by_timeout": True}
        else:
            text = str(res)
            ack = Ack.success(text=text)
        self._append_user(turn_id, text)
        return self._record_turn(turn_id, "commit", ack)
