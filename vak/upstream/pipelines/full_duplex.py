"""Native full-duplex pipeline (moshi-like) per architecture §3 "原生全双工的降级映射".

The model has no turns, tools, truncation or history. The adapter:
- streams uplink continuously and synthesises `input.speech_started /
  stopped` from uplink energy (vak.audio.silence);
- opens a response (`response.started{origin: server_vad}`, fresh
  response_id/item_id) when the model's audio turns non-silent after
  silence, forwards `response.audio.delta` with duration and the declared
  sample rate (resampling is the arbiter's job), and closes it with
  `response.done{completed}` after `silence_ms` of model silence (measured
  in model audio time);
- forwards the model's text frames as `transcript.assistant`.
Everything else returns `unsupported(<field>)`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from vak.audio.silence import duration_ms, is_silent
from vak.core.clock import Clock
from vak.core.defaults import UpstreamTimeouts
from vak.core.ids import IdGen
from vak.core.types import ErrorScope, ReplyModality, TurnId
from vak.upstream.base import (
    Ack,
    AudioMeta,
    ConfigPatch,
    ResponseOrigin,
    ResponseOverrides,
    ResponseStatus,
    SessionInit,
)
from vak.upstream.capabilities import Capabilities
from vak.upstream.pipelines.base import TIMEOUT, PipelineBase
from vak.upstream.providers.base import FullDuplexModel

log = logging.getLogger(__name__)


class FullDuplexUpstream(PipelineBase):
    """`FullDuplexUpstream(model, caps, clock, timeouts, ids, silence_ms=700)`."""

    def __init__(
        self,
        model: FullDuplexModel,
        caps: Capabilities | None = None,
        clock: Clock | None = None,
        timeouts: UpstreamTimeouts | None = None,
        ids: IdGen | None = None,
        silence_ms: int = 700,
        threshold_dbfs: float = -45.0,
    ) -> None:
        from vak.upstream.pipelines import load_builtin_caps

        super().__init__(caps or load_builtin_caps("full_duplex_builtin"), clock, timeouts, ids)
        self._model = model
        self._silence_ms = silence_ms
        self._threshold = threshold_dbfs
        self._pump: asyncio.Task[None] | None = None
        # uplink VAD state
        self._user_speaking = False
        self._user_silence_ms = 0
        self._turn_id: TurnId | None = None
        # downlink response state
        self._rid: str | None = None
        self._item_id: str | None = None
        self._model_silence_ms = 0

    # -- open / close --------------------------------------------------------

    async def open(self, init: SessionInit) -> Ack:
        async with self._lock:
            if self._closed:
                return Ack.invalid_state("closed")
            if self._open:
                return Ack.invalid_state("already_open")
            if init.resume_handle is not None:
                return Ack.unsupported("session.resumption")
            if init.history:
                return Ack.unsupported("history.injection")
            if init.tools:
                return Ack.unsupported("tools.calling")
            res = await self._with_timeout(
                self._model.connect({"instructions": init.instructions, "language": init.language}),
                self.timeouts.open_connect_ms,
                "open_timeout",
                ErrorScope.SESSION,
            )
            if res is TIMEOUT:
                await self._model.disconnect()
                return Ack.timeout("open_connect")
            self._mirror.reset()
            self.session_id = self.ids.new("us")
            self._emitter.session_id = self.session_id
            self._open = True
            self._pump = asyncio.create_task(self._receive())
            effective = {"history_items_sent": 0, "tools_sent": 0}
            self._emit(self._emitter.session_opened(self.session_id, effective))
            return Ack.success(session_id=self.session_id, effective=effective)

    async def close(self, reason: str) -> Ack:
        async with self._lock:
            if self._closed:
                return Ack.success()
            self._closed = True
            if self._pump is not None:
                self._pump.cancel()
            self._cancel_all_active(reason)
            res = await self._with_timeout(
                self._model.disconnect(), self.timeouts.close_grace_ms, "close_timeout", ErrorScope.SESSION
            )
            self._emit(self._emitter.session_closed(reason, "client"))
            self._emitter.close()
            return Ack.success() if res is not TIMEOUT else Ack.timeout("close_grace")

    # -- uplink ----------------------------------------------------------------

    async def send_audio(self, frame: bytes, meta: AudioMeta) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        # open-mic outside a turn is legitimate here: turns are synthesised
        self._turn_id = meta.turn_id
        ms = duration_ms(frame, meta.sample_rate)
        if is_silent(frame, self._threshold):
            if self._user_speaking:
                self._user_silence_ms += ms
                if self._user_silence_ms >= self._silence_ms:
                    self._user_speaking = False
                    self._emit(self._emitter.speech_stopped(meta.turn_id, source="adapter_vad"))
        else:
            self._user_silence_ms = 0
            if not self._user_speaking:
                self._user_speaking = True
                self._emit(self._emitter.speech_started(meta.turn_id, source="adapter_vad"))
        await self._model.send(frame)
        return Ack.success()

    async def commit_turn(self, turn_id: TurnId, mode: Literal["commit", "discard"]) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        if mode == "commit":
            return Ack.success(committed_by_server=True)  # turn.commit_mode = none
        return Ack.unsupported("turn.commit_mode")

    # -- downlink --------------------------------------------------------------

    async def _receive(self) -> None:
        try:
            async for fr in self._model.receive():
                if fr.text:
                    if self._rid is None:
                        self._open_response()
                    assert self._rid is not None
                    self._emit(self._emitter.transcript_assistant(self._rid, self._item_id, fr.text, False, self._turn_id))
                if fr.pcm16 is None:
                    continue
                ms = duration_ms(fr.pcm16, fr.sample_rate)
                silent = is_silent(fr.pcm16, self._threshold)
                if self._rid is None:
                    if silent:
                        continue
                    self._open_response()
                assert self._rid is not None and self._item_id is not None
                self._emit(
                    self._emitter.audio_delta(self._rid, self._item_id, fr.pcm16, fr.sample_rate, ms, self._turn_id)
                )
                self._model_silence_ms = self._model_silence_ms + ms if silent else 0
                if self._model_silence_ms >= self._silence_ms:
                    self._close_response(ResponseStatus.COMPLETED)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err = self._errors.map(type(exc).__name__, str(exc), scope=ErrorScope.SESSION)
            self._emit(self._emitter.error(err))
            self._close_response(ResponseStatus.FAILED)

    def _open_response(self) -> None:
        self._rid = self.ids.new("resp")
        self._item_id = self.ids.new("item")
        self._model_silence_ms = 0
        self._start_response(self._rid, ResponseOrigin.SERVER_VAD, None, self._turn_id)

    def _close_response(self, status: ResponseStatus) -> None:
        if self._rid is None:
            return
        self._finish_response(self._rid, status, ReplyModality.AUDIO)
        self._rid = None
        self._item_id = None

    # -- unsupported primitives (explicit, architecture §3) ----------------------

    async def request_response(
        self, turn_id: TurnId, response_ref: str, overrides: ResponseOverrides | None = None
    ) -> Ack:
        return Ack.unsupported("response.client_initiated")

    async def cancel_response(self, response_id: str) -> Ack:
        if not self._tracker.is_active(response_id):
            return Ack.success()
        # no cancel API: local drop only (spec 04 §3.5)
        self._mark_cancelled(response_id)
        self._finish_response(response_id, ResponseStatus.CANCELLED, ReplyModality.AUDIO, confirmed=False)
        if self._rid == response_id:
            self._rid = None
            self._item_id = None
        return Ack.success(local_only=True)

    async def truncate(self, item_id: str, audio_end_ms: int, played_text: str | None = None) -> Ack:
        return Ack.unsupported("truncate.mode")

    async def send_tool_result(self, call_id: str, result: dict[str, Any], reply_required: bool) -> Ack:
        return Ack.unsupported("tools.calling")

    async def update_config(
        self, patch: ConfigPatch, mode: Literal["live", "append", "next_session"]
    ) -> Ack:
        async with self._lock:
            if mode == "next_session":
                return Ack.success(deferred=True)
            if mode == "append":
                return Ack.unsupported("config.append_channels")
            for target, value in (("instructions", patch.instructions), ("tools", patch.tools), ("voice", patch.voice)):
                if value is not None:
                    return Ack.unsupported(f"config.update.{target}")
            return Ack.success()
