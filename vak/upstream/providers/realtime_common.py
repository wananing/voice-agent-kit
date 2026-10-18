"""Shared implementation for OpenAI-Realtime-dialect S2S providers (spec 04 §2.4
event mapping, §3 primitive translation).

`RealtimeSessionBase` owns one WebSocket, encodes the vendor-level
primitives of `RealtimeS2S` and decodes vendor events into `UpstreamEvent`.
It translates only: no retry, no reconnect, no turn counting, no policy.
Vendor differences are expressed by class attributes and a handful of
overridable hooks (`openai_realtime.py`, `dashscope_realtime.py`).

Two wire dialects are supported:

- `beta`: `modalities`, `response.audio.delta`, `OpenAI-Beta: realtime=v1`
  (DashScope speaks this one; qwen-audio-agent `openai-compatible-protocol.mjs`).
- `ga`: `output_modalities`, nested `audio.input/output`, `response.output_audio.delta`
  (qwen-audio-agent `ga-protocol.mjs`, `gpt-live.mjs`).

Incoming events of either dialect are always accepted.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal

from vak.core.clock import Clock, MonotonicClock
from vak.core.types import ErrorClass, ErrorReason, ErrorScope, HistoryItem, ReplyModality
from vak.upstream.base import (
    EventType,
    ResponseOrigin,
    ResponseStatus,
    ToolDef,
    UpstreamError,
    UpstreamEvent,
    Usage,
)
from vak.upstream.providers._util import ProviderConfigError, cfg, pcm16_duration_ms, resample_pcm16

log = logging.getLogger(__name__)

Dialect = Literal["beta", "ga"]

# (class, reason, scope)
CodeEntry = tuple[ErrorClass, ErrorReason, ErrorScope]


class RealtimeTimeout(TimeoutError):
    """A connect/ready wait exceeded its budget (spec 04 §3.1)."""


@dataclass(slots=True)
class _Call:
    call_id: str
    name: str
    response_id: str | None
    item_id: str | None


# Metadata key used to correlate a client `response.create` with its
# `response.created` on dialects that echo metadata (ga-protocol.mjs).
RESPONSE_REF_KEY = "vak_response_ref"

_ID_PREFIX = {"message": "msg", "function_call": "fc", "function_call_output": "fco"}


class RealtimeSessionBase:
    """One vendor realtime session. Subclasses set the class attributes."""

    provider_name: str = "realtime"
    default_base_url: str = ""
    default_model: str = ""
    api_key_env: str = ""
    dialect: Dialect = "beta"
    input_sample_rate: int = 24000  # what the wire expects
    output_sample_rate: int = 24000  # what the wire delivers
    default_turn_detection: dict[str, Any] | None = {"type": "server_vad"}
    default_transcription: dict[str, Any] | None = None
    # stable code -> (class, reason, scope); spec 04 §4.6 + errors.code_map
    code_map: dict[str, CodeEntry] = {}
    # (regex, class, reason, scope) used when the code is unknown (message_match)
    message_rules: list[tuple[re.Pattern[str], ErrorClass, ErrorReason, ErrorScope]] = []

    def __init__(
        self,
        config: dict[str, Any],
        *,
        clock: Clock | None = None,
        is_committed: Callable[[], bool] | None = None,
    ) -> None:
        self.config: dict[str, Any] = dict(config)
        self.clock: Clock = clock or MonotonicClock()
        # spec 04 §4.4: before_commit = call arrived before the turn's commit_turn
        self.is_committed: Callable[[], bool] = is_committed or (lambda: True)
        self.session_id: str | None = None
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[UpstreamEvent | None] = asyncio.Queue()
        self._seq = 0
        self._closed = False
        self._configured = False
        self._created: asyncio.Future[dict[str, Any]] | None = None
        self._updated: asyncio.Future[dict[str, Any]] | None = None
        self._pending_refs: deque[str] = deque()
        self._ref_counter = 0
        self._pending_client_commits = 0
        self._active_response: str | None = None
        self._cancelled: set[str] = set()
        self._emitted_calls: set[str] = set()
        self._client_item_ids: dict[str, str] = {}  # upstream item id -> local id
        self._item_counter = 0
        self._batch: list[UpstreamEvent] | None = None
        self.dropped_after_cancel = 0

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------

    def _url(self, merged: dict[str, Any]) -> str:
        base = cfg(merged, "base_url", self.default_base_url).rstrip("/")
        model = cfg(merged, "model", self.default_model)
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}model={model}"

    def _headers(self, merged: dict[str, Any]) -> dict[str, str]:
        api_key = merged.get("api_key")
        if not api_key and self.api_key_env:
            import os

            api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise ProviderConfigError(
                f"{self.provider_name}: set config 'api_key' or environment variable {self.api_key_env}"
            )
        headers = {"Authorization": f"Bearer {api_key}"}
        if self.dialect == "beta":
            headers["OpenAI-Beta"] = "realtime=v1"
        headers.update(cfg(merged, "headers", {}))
        return headers

    async def _ws_connect(self, url: str, headers: dict[str, str]) -> Any:
        from websockets.asyncio.client import connect  # lazy: optional at import time

        return await connect(url, additional_headers=headers, max_size=None)

    async def connect(self, config: dict[str, Any]) -> None:
        """Open the socket, wait for `session.created`, push the session
        config, wait for `session.updated`, then seed history items. History
        goes in here and only here (spec 04 §3.1, history.injection)."""
        merged = {**self.config, **(config or {})}
        loop = asyncio.get_running_loop()
        self._created = loop.create_future()
        self._updated = loop.create_future()
        self._ws = await self._ws_connect(self._url(merged), self._headers(merged))
        self._reader = asyncio.create_task(self._read_loop())
        timeout = int(cfg(merged, "connect_timeout_ms", 10_000))
        await self._wait(self._created, timeout, "session.created")
        await self.update_session(self._session_patch(merged))
        await self._wait(self._updated, int(cfg(merged, "ready_timeout_ms", 5_000)), "session.updated")
        self._configured = True
        for item in cfg(merged, "history", []):
            await self._send({"type": "conversation.item.create", "item": self._history_item(item)})

    async def _wait(self, fut: asyncio.Future[Any], timeout_ms: int, what: str) -> Any:
        sleeper = asyncio.ensure_future(self.clock.sleep(timeout_ms))
        done, _ = await asyncio.wait({fut, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        if fut in done:
            sleeper.cancel()
            return fut.result()
        raise RealtimeTimeout(f"{self.provider_name}: timed out waiting for {what}")

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("%s: non-JSON frame dropped", self.provider_name)
                    continue
                self._handle_vendor_event(ev)
            self._emit(EventType.SESSION_CLOSED, reason="server_closed", committed_by="server")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # connection dropped: report, never reconnect (spec 04 §3.0)
            self._emit(
                EventType.ERROR,
                error=UpstreamError(
                    ErrorClass.TRANSIENT, ErrorReason.SESSION_LOST, ErrorScope.SESSION, raw_message=str(e)
                ),
            )
            self._emit(EventType.SESSION_CLOSED, reason="connection_lost", committed_by="server")
        finally:
            await self._queue.put(None)

    async def disconnect(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # best effort
                pass
        if self._active_response and self._active_response not in self._cancelled:
            self._emit(
                EventType.RESPONSE_DONE,
                response_id=self._active_response,
                status=ResponseStatus.CANCELLED,
                confirmed=False,
                reply_modality=ReplyModality.NONE,
            )
        self._emit(EventType.SESSION_CLOSED, reason="client_close", committed_by="client")
        await self._queue.put(None)

    def events(self) -> AsyncIterator[UpstreamEvent]:
        return self._iter_events()

    async def _iter_events(self) -> AsyncIterator[UpstreamEvent]:
        while True:
            ev = await self._queue.get()
            if ev is None:
                return
            yield ev

    # ------------------------------------------------------------------
    # primitives (vendor level)
    # ------------------------------------------------------------------

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError(f"{self.provider_name}: not connected")
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def append_audio(self, pcm16: bytes, sample_rate: int) -> None:
        data = resample_pcm16(pcm16, sample_rate, self.input_sample_rate)
        await self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(data).decode("ascii")})

    async def commit_input(self) -> None:
        self._pending_client_commits += 1
        await self._send({"type": "input_audio_buffer.commit"})

    async def clear_input(self) -> None:
        await self._send({"type": "input_audio_buffer.clear"})

    async def create_response(self, overrides: dict[str, Any] | None) -> str | None:
        """Send `response.create`. Returns the local response_ref that the
        matching `response.started` event will carry."""
        ov = dict(overrides or {})
        ref = ov.pop("response_ref", None)
        if not ref:
            self._ref_counter += 1
            ref = f"rr-{self._ref_counter}"
        body = self._encode_response(ov, ref)
        self._pending_refs.append(ref)
        payload: dict[str, Any] = {"type": "response.create"}
        if body:
            payload["response"] = body
        await self._send(payload)
        return ref

    async def cancel_response(self, response_id: str | None) -> None:
        rid = response_id or self._active_response
        if rid:
            self._cancelled.add(rid)
        payload: dict[str, Any] = {"type": "response.cancel"}
        if response_id:
            payload["response_id"] = response_id
        await self._send(payload)

    async def truncate_item(self, item_id: str, audio_end_ms: int) -> None:
        # spec 04 §3.6: audio_end_ms == 0 deletes the item instead
        if audio_end_ms <= 0:
            await self._send({"type": "conversation.item.delete", "item_id": item_id})
            return
        await self._send(
            {
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": 0,
                "audio_end_ms": int(audio_end_ms),
            }
        )

    async def add_user_text(self, text: str) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )

    async def add_tool_output(self, call_id: str, output: str, silent: bool = False) -> None:
        """function_call_output only. No `response.create` here: silent close
        is by omission (tools.silent_close=by_omission); the pipeline calls
        create_response when a reply is required."""
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {"type": "function_call_output", "call_id": call_id, "output": output},
            }
        )

    async def update_session(self, patch: dict[str, Any]) -> None:
        await self._send({"type": "session.update", "session": self._encode_session(patch)})

    # ------------------------------------------------------------------
    # encoding hooks
    # ------------------------------------------------------------------

    def _session_patch(self, merged: dict[str, Any]) -> dict[str, Any]:
        patch: dict[str, Any] = {
            "turn_detection": merged.get("turn_detection", self.default_turn_detection),
            "modalities": list(cfg(merged, "modalities", ["audio", "text"])),
        }
        for k in ("instructions", "tools", "voice", "temperature"):
            if merged.get(k) is not None:
                patch[k] = merged[k]
        tr = merged.get("input_audio_transcription", self.default_transcription)
        if tr is not None:
            patch["input_audio_transcription"] = tr
        return patch

    def _encode_tools(self, tools: list[Any]) -> list[dict[str, Any]]:
        out = []
        for t in tools:
            if isinstance(t, ToolDef):
                out.append({"type": "function", "name": t.name, "description": t.description, "parameters": t.params})
            else:
                out.append(t)
        return out

    def _encode_session(self, patch: dict[str, Any]) -> dict[str, Any]:
        p = dict(patch)
        tools = p.pop("tools", None)
        modalities = p.pop("modalities", None)
        td = p.pop("turn_detection", "__absent__")
        voice = p.pop("voice", None)
        transcription = p.pop("input_audio_transcription", None)
        s: dict[str, Any] = {}
        for k in ("instructions", "temperature", "max_response_output_tokens"):
            if k in p:
                s[k] = p[k]
        if tools is not None:
            s["tools"] = self._encode_tools(tools)
        if self.dialect == "ga":
            s["type"] = "realtime"
            if modalities is not None:
                s["output_modalities"] = modalities
            audio: dict[str, Any] = {}
            inp: dict[str, Any] = {}
            outp: dict[str, Any] = {}
            if td != "__absent__":
                inp["turn_detection"] = td
            if transcription is not None:
                inp["transcription"] = transcription
            if voice is not None:
                outp["voice"] = voice
            if not self._configured:  # formats only on first setup
                inp["format"] = {"type": "audio/pcm", "rate": self.input_sample_rate}
                outp["format"] = {"type": "audio/pcm", "rate": self.output_sample_rate}
            if inp:
                audio["input"] = inp
            if outp:
                audio["output"] = outp
            if audio:
                s["audio"] = audio
        else:
            if modalities is not None:
                s["modalities"] = modalities
            if td != "__absent__":
                s["turn_detection"] = td
            if voice is not None:
                s["voice"] = voice
            if transcription is not None:
                s["input_audio_transcription"] = transcription
            if not self._configured:
                s["input_audio_format"] = "pcm16"
                s["output_audio_format"] = "pcm16"
        return s

    def _encode_response(self, ov: dict[str, Any], ref: str) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if ov.get("instructions") is not None:
            body["instructions"] = ov["instructions"]
        if ov.get("modalities") is not None:
            body["output_modalities" if self.dialect == "ga" else "modalities"] = list(ov["modalities"])
        if ov.get("tools") is not None:
            body["tools"] = self._encode_tools(ov["tools"])
        if ov.get("tool_choice") is not None:
            body["tool_choice"] = ov["tool_choice"]
        if ov.get("conversation") is not None:
            body["conversation"] = ov["conversation"]
        if self.dialect == "ga":
            body["metadata"] = {RESPONSE_REF_KEY: ref}
        return body

    def _new_item_id(self, kind: str, local_id: str | None) -> str:
        self._item_counter += 1
        item_id = f"{_ID_PREFIX.get(kind, 'item')}_vak{self._item_counter:04d}"
        if local_id:
            self._client_item_ids[item_id] = local_id
        return item_id

    def _history_item(self, item: HistoryItem | dict[str, Any]) -> dict[str, Any]:
        if isinstance(item, dict):
            return item
        text_part = "output_text" if self.dialect == "ga" else "text"
        if item.role == "user":
            kind = "message"
            body: dict[str, Any] = {"role": "user", "content": [{"type": "input_text", "text": item.text}]}
        elif item.role == "assistant":
            kind = "message"
            body = {"role": "assistant", "content": [{"type": text_part, "text": item.text}]}
        elif item.role == "tool_call":
            kind = "function_call"
            body = {
                "call_id": item.tool_call_id,
                "name": item.tool_name,
                "arguments": json.dumps(item.tool_args or {}, ensure_ascii=False),
            }
        else:
            kind = "function_call_output"
            body = {"call_id": item.tool_call_id, "output": item.text}
        return {"type": kind, "id": self._new_item_id(kind, item.local_entry_id), **body}

    # ------------------------------------------------------------------
    # decoding: vendor event -> UpstreamEvent (spec 04 §2.4)
    # ------------------------------------------------------------------

    def _emit(self, type_: EventType, **fields: Any) -> UpstreamEvent:
        self._seq += 1
        ev = UpstreamEvent(type=type_, seq=self._seq, t_local_ms=self.clock.now_ms(), session_id=self.session_id, **fields)
        self._queue.put_nowait(ev)
        if self._batch is not None:
            self._batch.append(ev)
        return ev

    def _handle_vendor_event(self, ev: dict[str, Any]) -> list[UpstreamEvent]:
        """Map one vendor event. Returns the standardised events it produced
        (they are also queued for `events()`)."""
        self._batch = []
        try:
            handler = _HANDLERS.get(ev.get("type", ""))
            if handler is not None:
                handler(self, ev)
            return self._batch
        finally:
            self._batch = None

    def _raw(self, ev: dict[str, Any]) -> str:
        return f"{ev.get('type')}:{ev.get('event_id', '')}"

    # session -----------------------------------------------------------

    def _on_session(self, ev: dict[str, Any]) -> None:
        session = ev.get("session") or {}
        self.session_id = session.get("id") or self.session_id
        if ev["type"] == "session.created":
            if self._created is not None and not self._created.done():
                self._created.set_result(session)
        else:
            if self._updated is not None and not self._updated.done():
                self._updated.set_result(session)
        self._emit(EventType.SESSION_OPENED, effective=session, raw_ref=self._raw(ev))

    # input -------------------------------------------------------------

    def _on_speech_started(self, ev: dict[str, Any]) -> None:
        self._emit(
            EventType.INPUT_SPEECH_STARTED,
            source="server_vad",
            item_id=ev.get("item_id"),
            audio_ms=ev.get("audio_start_ms"),
            raw_ref=self._raw(ev),
        )

    def _on_speech_stopped(self, ev: dict[str, Any]) -> None:
        self._emit(
            EventType.INPUT_SPEECH_STOPPED,
            source="server_vad",
            item_id=ev.get("item_id"),
            audio_ms=ev.get("audio_end_ms"),
            raw_ref=self._raw(ev),
        )

    def _on_committed(self, ev: dict[str, Any]) -> None:
        if self._pending_client_commits > 0:
            self._pending_client_commits -= 1
            by: Literal["client", "server"] = "client"
        else:
            by = "server"
        self._emit(EventType.INPUT_COMMITTED, committed_by=by, item_id=ev.get("item_id"), raw_ref=self._raw(ev))

    def _on_user_transcript_delta(self, ev: dict[str, Any]) -> None:
        text = ev.get("delta")
        if text is None:
            text = ev.get("text") or ev.get("transcript") or ""
        self._emit(
            EventType.TRANSCRIPT_USER_PARTIAL, text=text, is_final=False, item_id=ev.get("item_id"), raw_ref=self._raw(ev)
        )

    def _on_user_transcript_done(self, ev: dict[str, Any]) -> None:
        self._emit(
            EventType.TRANSCRIPT_USER_FINAL,
            text=ev.get("transcript") or "",
            is_final=True,
            item_id=ev.get("item_id"),
            raw_ref=self._raw(ev),
        )

    def _on_user_transcript_failed(self, ev: dict[str, Any]) -> None:
        err = ev.get("error") or {}
        self._emit(
            EventType.ERROR,
            item_id=ev.get("item_id"),
            error=UpstreamError(
                ErrorClass.BENIGN,
                ErrorReason.ITEM_REJECTED,
                ErrorScope.ITEM,
                ref={"item_id": ev.get("item_id") or ""},
                raw_code=err.get("code"),
                raw_message=err.get("message"),
            ),
            raw_ref=self._raw(ev),
        )

    def _on_item_created(self, ev: dict[str, Any]) -> None:
        item = ev.get("item") or {}
        item_id = item.get("id")
        self._emit(
            EventType.ITEM_ECHO,
            item_id=item_id,
            local_id=self._client_item_ids.get(item_id or ""),
            kind=item.get("type"),
            raw_ref=self._raw(ev),
        )

    # response ----------------------------------------------------------

    def _on_response_created(self, ev: dict[str, Any]) -> None:
        resp = ev.get("response") or {}
        rid = resp.get("id")
        self._active_response = rid
        ref: str | None = None
        meta_ref = (resp.get("metadata") or {}).get(RESPONSE_REF_KEY)
        if meta_ref and meta_ref in self._pending_refs:
            self._pending_refs.remove(meta_ref)
            ref = meta_ref
        elif meta_ref is None and self._pending_refs:
            ref = self._pending_refs.popleft()
        origin = ResponseOrigin.CLIENT if ref else ResponseOrigin.SERVER_VAD
        self._emit(EventType.RESPONSE_STARTED, response_id=rid, response_ref=ref, origin=origin, raw_ref=self._raw(ev))

    def _on_audio_delta(self, ev: dict[str, Any]) -> None:
        rid = ev.get("response_id")
        if rid in self._cancelled:  # spec 04 §3.5: zero deltas after cancel
            self.dropped_after_cancel += 1
            return
        audio = base64.b64decode(ev.get("delta") or "")
        self._emit(
            EventType.RESPONSE_AUDIO_DELTA,
            response_id=rid,
            item_id=ev.get("item_id"),
            audio=audio,
            sample_rate=self.output_sample_rate,
            duration_ms=pcm16_duration_ms(audio, self.output_sample_rate),
            raw_ref=self._raw(ev),
        )

    def _on_assistant_transcript_delta(self, ev: dict[str, Any]) -> None:
        if ev.get("response_id") in self._cancelled:
            return
        self._emit(
            EventType.TRANSCRIPT_ASSISTANT,
            response_id=ev.get("response_id"),
            item_id=ev.get("item_id"),
            text=ev.get("delta") or "",
            is_final=False,
            raw_ref=self._raw(ev),
        )

    def _on_assistant_transcript_done(self, ev: dict[str, Any]) -> None:
        self._emit(
            EventType.TRANSCRIPT_ASSISTANT,
            response_id=ev.get("response_id"),
            item_id=ev.get("item_id"),
            text=ev.get("transcript") or "",
            is_final=True,
            raw_ref=self._raw(ev),
        )

    def _on_text_delta(self, ev: dict[str, Any]) -> None:
        if ev.get("response_id") in self._cancelled:
            self.dropped_after_cancel += 1
            return
        self._emit(
            EventType.RESPONSE_TEXT_DELTA,
            response_id=ev.get("response_id"),
            item_id=ev.get("item_id"),
            text=ev.get("delta") or "",
            is_final=False,
            raw_ref=self._raw(ev),
        )

    def _on_text_done(self, ev: dict[str, Any]) -> None:
        if ev.get("response_id") in self._cancelled:
            return
        self._emit(
            EventType.RESPONSE_TEXT_DELTA,
            response_id=ev.get("response_id"),
            item_id=ev.get("item_id"),
            text="",
            is_final=True,
            raw_ref=self._raw(ev),
        )

    def _on_function_args_done(self, ev: dict[str, Any]) -> None:
        self._emit_tool_call(
            call_id=ev.get("call_id") or "",
            name=ev.get("name") or "",
            arguments=ev.get("arguments") or "{}",
            response_id=ev.get("response_id"),
            item_id=ev.get("item_id"),
            raw=self._raw(ev),
        )

    def _on_output_item_done(self, ev: dict[str, Any]) -> None:
        item = ev.get("item") or {}
        if item.get("type") != "function_call":
            return
        self._emit_tool_call(
            call_id=item.get("call_id") or "",
            name=item.get("name") or "",
            arguments=item.get("arguments") or "{}",
            response_id=ev.get("response_id"),
            item_id=item.get("id"),
            raw=self._raw(ev),
        )

    def _emit_tool_call(
        self, *, call_id: str, name: str, arguments: str, response_id: str | None, item_id: str | None, raw: str
    ) -> None:
        if call_id in self._emitted_calls:
            return  # arguments.done and output_item.done both describe it
        try:
            args = json.loads(arguments) if arguments else {}
            if not isinstance(args, dict):
                raise ValueError("arguments is not an object")
        except ValueError as e:
            self._emit(
                EventType.ERROR,
                response_id=response_id,
                call_id=call_id,
                error=UpstreamError(
                    ErrorClass.BENIGN,
                    ErrorReason.TOOL_PARSE,
                    ErrorScope.CALL,
                    ref={"call_id": call_id},
                    raw_message=str(e),
                ),
                raw_ref=raw,
            )
            return
        self._emitted_calls.add(call_id)
        self._emit(
            EventType.TOOL_CALL,
            response_id=response_id,
            item_id=item_id,
            call_id=call_id,
            name=name,
            args=args,
            before_commit=not self.is_committed(),
            raw_ref=raw,
        )

    def _on_response_done(self, ev: dict[str, Any]) -> None:
        resp = ev.get("response") or {}
        rid = resp.get("id")
        status_raw = resp.get("status") or "completed"
        if rid in self._cancelled:
            status = ResponseStatus.CANCELLED
        else:
            try:
                status = ResponseStatus(status_raw)
            except ValueError:
                status = ResponseStatus.FAILED
        usage = self._usage(rid, resp.get("usage"))
        self._emit(
            EventType.RESPONSE_DONE,
            response_id=rid,
            status=status,
            confirmed=True,
            reply_modality=_reply_modality(resp.get("output") or []),
            usage=usage,
            raw_ref=self._raw(ev),
        )
        if usage is not None:
            self._emit(EventType.USAGE, response_id=rid, usage=usage, raw_ref=self._raw(ev))
        details = resp.get("status_details") or {}
        if status is ResponseStatus.FAILED and details.get("error"):
            err = details["error"]
            cls, reason, _scope = self._classify(err.get("code"), err.get("message"))
            if cls in (ErrorClass.TRANSIENT, ErrorClass.BENIGN):
                cls = ErrorClass.BENIGN  # spec 04 §4.6 rule 6
            self._emit(
                EventType.ERROR,
                response_id=rid,
                error=UpstreamError(
                    cls,
                    reason,
                    ErrorScope.RESPONSE,
                    ref={"response_id": rid or ""},
                    raw_code=err.get("code"),
                    raw_message=err.get("message"),
                ),
                raw_ref=self._raw(ev),
            )
        if self._active_response == rid:
            self._active_response = None
        self._cancelled.discard(rid)

    def _usage(self, rid: str | None, u: dict[str, Any] | None) -> Usage | None:
        if not u:
            return None
        ind = u.get("input_token_details") or {}
        outd = u.get("output_token_details") or {}
        cached = ind.get("cached_tokens_details") or {}
        return Usage(
            response_id=rid,
            input_text_tokens=ind.get("text_tokens"),
            input_audio_tokens=ind.get("audio_tokens"),
            cached_input_tokens=ind.get("cached_tokens"),
            cached_text_tokens=cached.get("text_tokens"),
            cached_audio_tokens=cached.get("audio_tokens"),
            output_text_tokens=outd.get("text_tokens"),
            output_audio_tokens=outd.get("audio_tokens"),
            usage_semantics="net",
        )

    # error -------------------------------------------------------------

    def _classify(self, code: str | None, message: str | None) -> CodeEntry:
        if code and code in self.code_map:
            return self.code_map[code]
        msg = message or ""
        for pattern, cls, reason, scope in self.message_rules:
            if pattern.search(msg) or (code and pattern.search(code)):
                return cls, reason, scope
        if not self._configured:  # spec 04 §4.6 rule 2
            return ErrorClass.FATAL, ErrorReason.INVALID_CONFIG, ErrorScope.SESSION
        log.warning("%s: unmapped error code=%r message=%r", self.provider_name, code, message)
        return ErrorClass.TRANSIENT, ErrorReason.UNKNOWN, ErrorScope.SESSION

    def _on_error(self, ev: dict[str, Any]) -> None:
        err = ev.get("error") or {}
        code = err.get("code")
        cls, reason, scope = self._classify(code, err.get("message"))
        ref: dict[str, str] = {}
        if err.get("event_id"):
            ref["event_id"] = err["event_id"]
            if cls is ErrorClass.TRANSIENT and reason is ErrorReason.UNKNOWN:
                cls, scope = ErrorClass.BENIGN, ErrorScope.ITEM  # rule 5: correlated item-level error
        if scope is ErrorScope.RESPONSE and self._active_response:
            ref["response_id"] = self._active_response
        self._emit(
            EventType.ERROR,
            response_id=ref.get("response_id"),
            error=UpstreamError(cls, reason, scope, ref=ref, raw_code=code, raw_message=err.get("message")),
            raw_ref=self._raw(ev),
        )


_HANDLERS: dict[str, Callable[[RealtimeSessionBase, dict[str, Any]], None]] = {
    "session.created": RealtimeSessionBase._on_session,
    "session.updated": RealtimeSessionBase._on_session,
    "input_audio_buffer.speech_started": RealtimeSessionBase._on_speech_started,
    "input_audio_buffer.speech_stopped": RealtimeSessionBase._on_speech_stopped,
    "input_audio_buffer.committed": RealtimeSessionBase._on_committed,
    "conversation.item.input_audio_transcription.delta": RealtimeSessionBase._on_user_transcript_delta,
    "conversation.item.input_audio_transcription.text": RealtimeSessionBase._on_user_transcript_delta,
    "conversation.item.input_audio_transcription.completed": RealtimeSessionBase._on_user_transcript_done,
    "conversation.item.input_audio_transcription.failed": RealtimeSessionBase._on_user_transcript_failed,
    "conversation.item.created": RealtimeSessionBase._on_item_created,
    "conversation.item.added": RealtimeSessionBase._on_item_created,
    "response.created": RealtimeSessionBase._on_response_created,
    "response.audio.delta": RealtimeSessionBase._on_audio_delta,
    "response.output_audio.delta": RealtimeSessionBase._on_audio_delta,
    "response.audio_transcript.delta": RealtimeSessionBase._on_assistant_transcript_delta,
    "response.output_audio_transcript.delta": RealtimeSessionBase._on_assistant_transcript_delta,
    "response.audio_transcript.done": RealtimeSessionBase._on_assistant_transcript_done,
    "response.output_audio_transcript.done": RealtimeSessionBase._on_assistant_transcript_done,
    "response.text.delta": RealtimeSessionBase._on_text_delta,
    "response.output_text.delta": RealtimeSessionBase._on_text_delta,
    "response.text.done": RealtimeSessionBase._on_text_done,
    "response.output_text.done": RealtimeSessionBase._on_text_done,
    "response.function_call_arguments.done": RealtimeSessionBase._on_function_args_done,
    "response.output_item.done": RealtimeSessionBase._on_output_item_done,
    "response.done": RealtimeSessionBase._on_response_done,
    "error": RealtimeSessionBase._on_error,
}


def _reply_modality(output: list[dict[str, Any]]) -> ReplyModality:
    has_audio = has_text = False
    for item in output:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            pt = part.get("type", "")
            if "audio" in pt:
                has_audio = True
            elif "text" in pt:
                has_text = True
    if has_audio and has_text:
        return ReplyModality.MIXED
    if has_audio:
        return ReplyModality.AUDIO
    if has_text:
        return ReplyModality.TEXT_ONLY
    return ReplyModality.NONE


def rules(*entries: tuple[str, ErrorClass, ErrorReason, ErrorScope]) -> list[tuple[re.Pattern[str], ErrorClass, ErrorReason, ErrorScope]]:
    return [(re.compile(p, re.I), c, r, s) for p, c, r, s in entries]

