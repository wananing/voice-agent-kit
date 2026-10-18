"""Alibaba Cloud Model Studio (百炼 / DashScope) CosyVoice streaming TTS client (`TTS`).

Wire format (DashScope WebSocket task protocol, the same envelope as our ASR client in
vak/upstream/providers/stt/aliyun.py). References: xiaozhi-esp32-server
core/providers/tts/alibl_stream.py (raw websockets) and core/utils/alibl_endpoint.py;
ten-framework cosy_tts_python (drives the `dashscope` SDK `tts_v2.SpeechSynthesizer`,
whose request builders were cross-checked in dashscope 1.25.24):

    connect wss://dashscope.aliyuncs.com/api-ws/v1/inference
        headers: Authorization: Bearer <key>, X-DashScope-DataInspection: enable
    -> JSON {"header": {"action": "run-task", "task_id": <32 hex>, "streaming": "duplex"},
             "payload": {"task_group": "audio", "task": "tts", "function": "SpeechSynthesizer",
                         "model": ..., "parameters": {"text_type": "PlainText", "voice": ...,
                         "format": "pcm", "sample_rate": 24000, [volume, rate, pitch]},
                         "input": {}}}
    <- JSON {"header": {"event": "task-started", "task_id": ...}}
    -> JSON {"header": {"action": "continue-task", "task_id": ..., "streaming": "duplex"},
             "payload": {"input": {"text": <sentence>}}}
    -> JSON {"header": {"action": "finish-task", "task_id": ..., "streaming": "duplex"},
             "payload": {"input": {}}}
    <- binary PCM16 mono frames, interleaved with
       JSON {"header": {"event": "result-generated"}, "payload": {...}}  (informational)
    <- JSON {"header": {"event": "task-finished"}}  |  {"event": "task-failed",
             "error_code": ..., "error_message": ...}

Where the references differ, and what this adapter does:
- task-started: the SDK and xiaozhi's one-shot `to_tts` wait for it before continue-task;
  xiaozhi's streaming path sends continue-task right away. We wait.
- continue-task payload: xiaozhi sends only `{"input": {"text": ...}}`; the SDK repeats
  model/task_group/task/function. We send the minimal form (xiaozhi, production use).
- X-DashScope-DataInspection: xiaozhi (and our ASR client) send it, the SDK does not.
  We send it.
- volume/rate/pitch: both always send them (defaults 50 / 1.0 / 1.0); we send them only
  when configured so the server defaults apply. The SDK also sends `seed` and `type`
  (synthesis type), which xiaozhi omits; we omit them.
- Defaults: xiaozhi uses model `cosyvoice-v2` + voice `longxiaochun_v2` (our default);
  TEN's property.json uses `cosyvoice-v3` + `loongluna_v2` (its README still says
  cosyvoice-v1 / longxiaochun). Voices are tied to a model generation (`*_v2` voices for
  v2); other model/voice pairs are passed through unchanged.
- PCM sample rates: the SDK's AudioFormat (and TEN's mapping) allow 8000, 16000, 22050,
  24000, 44100, 48000 Hz; anything else is rejected at construction.
- task-failed: xiaozhi stops reading and closes the socket; we raise RuntimeError with
  the server message and close the connection rather than reuse it.

Connections: one sentence = one task. A connection runs tasks sequentially (the SDK's
SpeechSynthesizerObjectPool hands a connected synthesizer back to its pool after
task-finished; xiaozhi reuses its socket for the next task within 60 s). Frames carry
task_id (xiaozhi filters on it), but no reference runs two tasks concurrently on one
socket, so we do not multiplex: with `tts_lookahead=1` two tasks may be active, each on
its own connection. Idle connections are pooled (at most 2) and dropped after
`_IDLE_MAX_S` (DashScope closes idle sockets after about a minute; xiaozhi reuses only
within 60 s). A pooled socket is checked to be open before reuse; a send failure drops
it and raises (no retry, project rule).

Barge-in: `flush(request_id)` sets a stop flag; the generator yields nothing more and
returns at the next server message. Whenever a task ends early (flush, consumer closed
or cancelled the generator), a background task sends finish-task if it was not sent yet,
discards the remaining audio and returns the connection to the pool only once
task-finished arrives (within `_DRAIN_TIMEOUT_S`); otherwise the connection is closed.
DashScope has no cancel action: the SDK's `streaming_cancel` sends finish-task and
closes the socket.

Text streaming: `supports_text_stream = False`. continue-task accepts incremental text
(several continue-task messages per task; the SDK's `streaming_call`, xiaozhi's
dual-stream mode), so one task could span a whole LLM response. That would need a
text-stream entry point in the TTS protocol (open task, push deltas, finish) and a way to
map audio back to sentences (result-generated `sentence-begin`/`sentence-end` events with
`original_text`, as xiaozhi uses); this adapter sends one complete sentence per task.

Alternative: ten-framework qwen3_tts_python is *not* this API; it runs the open-weights
Qwen3-TTS model locally (qwen_tts / torch). DashScope's own realtime Qwen TTS
(qwen-tts-realtime) is a different WebSocket protocol and is not implemented here.

Endpoint: mainland `wss://dashscope.aliyuncs.com/api-ws/v1/inference`; international
(Singapore) `wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference` (alibl_endpoint.py).
API keys are region-specific.

Config keys: api_key (else env DASHSCOPE_API_KEY), model ("cosyvoice-v2"),
voice ("longxiaochun_v2"), sample_rate (24000), volume (0–100), rate (0.5–2.0),
pitch (0.5–2.0), url (mainland endpoint above).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from vak.upstream.providers._util import ProviderConfigError, cfg, secret
from vak.upstream.providers.base import TTSChunk

log = logging.getLogger(__name__)

DEFAULT_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
DEFAULT_MODEL = "cosyvoice-v2"
DEFAULT_VOICE = "longxiaochun_v2"
SAMPLE_RATES = (8000, 16000, 22050, 24000, 44100, 48000)
_MAX_IDLE = 2
_IDLE_MAX_S = 50.0
_DRAIN_TIMEOUT_S = 5.0


class AliyunStreamingTTS:
    supports_text_stream = False

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.api_key: str = secret(config, "api_key", "DASHSCOPE_API_KEY", "aliyun_tts") or ""
        self.model: str = str(cfg(config, "model", DEFAULT_MODEL))
        self.voice: str = str(cfg(config, "voice", DEFAULT_VOICE))
        self.sample_rate: int = int(cfg(config, "sample_rate", 24000))
        if self.sample_rate not in SAMPLE_RATES:
            raise ProviderConfigError(f"aliyun_tts: sample_rate must be one of {SAMPLE_RATES}")
        self.url: str = str(cfg(config, "url", DEFAULT_URL))
        self._idle: list[tuple[Any, float]] = []  # (connection, monotonic time returned)
        self._busy: set[Any] = set()
        self._stopped: set[str] = set()
        self._cleanups: set[asyncio.Task[None]] = set()

    # -- messages ------------------------------------------------------------

    def run_task_message(self, task_id: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "text_type": "PlainText",
            "voice": self.voice,
            "format": "pcm",
            "sample_rate": self.sample_rate,
        }
        for key, conv in (("volume", int), ("rate", float), ("pitch", float)):
            v = self.config.get(key)
            if v is not None and v != "":
                params[key] = conv(v)
        return {
            "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": self.model,
                "parameters": params,
                "input": {},
            },
        }

    @staticmethod
    def continue_task_message(task_id: str, text: str) -> dict[str, Any]:
        return {
            "header": {"action": "continue-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {"text": text}},
        }

    @staticmethod
    def finish_task_message(task_id: str) -> dict[str, Any]:
        return {
            "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {}},
        }

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "X-DashScope-DataInspection": "enable"}

    # -- connection pool -----------------------------------------------------

    async def _ws_connect(self, url: str, headers: dict[str, str]) -> Any:
        from websockets.asyncio.client import connect

        return await connect(url, additional_headers=headers, max_size=None)

    @staticmethod
    def _is_open(ws: Any) -> bool:
        state = getattr(ws, "state", None)
        return getattr(state, "name", None) == "OPEN"

    def _usable(self, ws: Any, since: float) -> bool:
        return self._is_open(ws) and time.monotonic() - since < _IDLE_MAX_S

    async def _acquire(self) -> Any:
        while self._idle:
            ws, since = self._idle.pop()
            if self._usable(ws, since):
                self._busy.add(ws)
                return ws
            await self._close_ws(ws)
        ws = await self._ws_connect(self.url, self.headers())
        self._busy.add(ws)
        return ws

    def _release(self, ws: Any) -> None:
        """Back to the pool (synchronously, so the next sentence finds it), or close."""
        self._busy.discard(ws)
        if self._is_open(ws) and len(self._idle) < _MAX_IDLE:
            self._idle.append((ws, time.monotonic()))
        else:
            self._spawn(self._close_ws(ws))

    def _discard(self, ws: Any) -> None:
        # stays in _busy until closed, so close() still reaches it if this task is cancelled
        self._spawn(self._close_busy(ws))

    async def _close_busy(self, ws: Any) -> None:
        await self._close_ws(ws)
        self._busy.discard(ws)

    @staticmethod
    async def _close_ws(ws: Any) -> None:
        try:
            await ws.close()
        except Exception as e:
            log.debug("aliyun_tts: close failed: %s", e)

    async def warmup(self) -> None:
        """Open one connection ahead of the first request (no-op if one is idle)."""
        if any(self._usable(ws, since) for ws, since in self._idle):
            return
        ws = await self._ws_connect(self.url, self.headers())
        self._busy.add(ws)
        self._release(ws)

    async def close(self) -> None:
        for t in list(self._cleanups):
            t.cancel()
        conns = [ws for ws, _ in self._idle] + list(self._busy)
        self._idle.clear()
        self._busy.clear()
        for ws in conns:
            await self._close_ws(ws)

    # -- synthesis -----------------------------------------------------------

    @staticmethod
    def _event(raw: str | bytes) -> tuple[str, dict[str, Any]]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return "", {}
        header = (msg.get("header") or {}) if isinstance(msg, dict) else {}
        return str(header.get("event", "")), header

    @staticmethod
    def _failure(header: dict[str, Any]) -> RuntimeError:
        return RuntimeError(
            f"aliyun_tts: task failed: {header.get('error_code')} {header.get('error_message')}"
        )

    async def synthesize(self, text: str, request_id: str) -> AsyncIterator[TTSChunk]:
        self._stopped.discard(request_id)
        task_id = uuid.uuid4().hex
        ws = await self._acquire()
        finish_sent = False
        # "running": the task may still be live on the socket -> drain it;
        # "finished": task-finished seen -> pool; "dead": failed or socket error -> close.
        state = "running"
        try:
            try:
                await ws.send(json.dumps(self.run_task_message(task_id), ensure_ascii=False))
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, bytes | bytearray):
                        continue  # no audio before task-started
                    event, header = self._event(raw)
                    if event == "task-started":
                        break
                    if event == "task-failed":
                        raise self._failure(header)
                if request_id in self._stopped:
                    return
                await ws.send(json.dumps(self.continue_task_message(task_id, text), ensure_ascii=False))
                await ws.send(json.dumps(self.finish_task_message(task_id), ensure_ascii=False))
                finish_sent = True

                carry = b""
                while True:
                    raw = await ws.recv()
                    if request_id in self._stopped:
                        if not isinstance(raw, bytes | bytearray) and self._event(raw)[0] == "task-finished":
                            state = "finished"  # nothing left to drain
                        return
                    if isinstance(raw, bytes | bytearray):
                        data = carry + bytes(raw)
                        cut = len(data) - len(data) % 2  # keep PCM16 sample alignment
                        carry = data[cut:]
                        if cut:
                            yield TTSChunk(pcm16=data[:cut], sample_rate=self.sample_rate)
                        continue
                    event, header = self._event(raw)
                    if event == "task-finished":
                        state = "finished"
                        yield TTSChunk(pcm16=b"", sample_rate=self.sample_rate, is_last=True)
                        return
                    if event == "task-failed":
                        raise self._failure(header)
                    # result-generated (sentence-begin/end, usage): informational only
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception:
                state = "dead"  # task-failed or send/recv failure: no retry, drop the socket
                raise
        finally:
            self._stopped.discard(request_id)
            if state == "finished":
                self._release(ws)
            elif state == "dead":
                self._discard(ws)
            else:
                self._spawn(self._drain(ws, task_id, finish_sent))

    def _spawn(self, coro: Any) -> None:
        # No awaits in the generator's finally: it may run inside a cancelled task.
        t = asyncio.get_running_loop().create_task(coro)
        self._cleanups.add(t)
        t.add_done_callback(self._cleanups.discard)

    async def _drain(self, ws: Any, task_id: str, finish_sent: bool) -> None:
        """Finish an abandoned task and discard its audio; pool the socket only if
        task-finished arrives in time, else close it."""
        try:
            async with asyncio.timeout(_DRAIN_TIMEOUT_S):
                if not finish_sent:
                    await ws.send(json.dumps(self.finish_task_message(task_id), ensure_ascii=False))
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, bytes | bytearray):
                        continue
                    event, _ = self._event(raw)
                    if event == "task-finished":
                        self._release(ws)
                        return
                    if event == "task-failed":
                        break
        except Exception as e:
            log.debug("aliyun_tts: drain of task %s failed: %s", task_id, e)
        self._discard(ws)

    async def flush(self, request_id: str) -> None:
        self._stopped.add(request_id)
