"""Alibaba Cloud Model Studio (百炼 / DashScope) real-time ASR client (`STT`).

Wire format (DashScope WebSocket task protocol; references:
xiaozhi-esp32-server core/providers/asr/aliyunbl_stream.py, core/providers/tts/alibl_stream.py,
core/utils/alibl_endpoint.py; ten-framework aliyun_asr_bigmodel_python, which drives the same
protocol through the `dashscope` SDK `Recognition` class):

    connect wss://dashscope.aliyuncs.com/api-ws/v1/inference
        headers: Authorization: Bearer <key>, X-DashScope-DataInspection: enable
    -> JSON {"header": {"action": "run-task", "task_id": <32 hex>, "streaming": "duplex"},
             "payload": {"task_group": "audio", "task": "asr", "function": "recognition",
                         "model": ..., "parameters": {"format": "pcm", "sample_rate": 16000, ...},
                         "input": {}}}
    <- JSON {"header": {"event": "task-started", "task_id": ...}}
    -> binary PCM16 mono frames            (only after task-started)
    <- JSON {"header": {"event": "result-generated"},
             "payload": {"output": {"sentence": {"begin_time": ms, "end_time": ms|null,
                                                  "text": "...", "sentence_end": bool, "words": [...]}}}}
    -> JSON {"header": {"action": "finish-task", "task_id": ..., "streaming": "duplex"},
             "payload": {"input": {}}}
    <- JSON {"header": {"event": "task-finished"}}                         (after the last results)
    <- JSON {"header": {"event": "task-failed", "error_code": ..., "error_message": ...}}

Each `result-generated` carries the *whole* current sentence (not a fragment). A sentence
is complete when `sentence_end` is true; when the key is absent we fall back to
`end_time is not None` (xiaozhi requires both, the SDK used by TEN checks either) so
both shapes work. Server-side VAD may split one push-to-talk turn into several sentences,
so this client folds them: partials carry completed sentences + the tentative one, and
exactly one final is emitted after `finish()` (at task-finished, or with the best text
we have if the task fails / the socket drops). A final mid-utterance would truncate the
turn in `SttTurn`, which resolves on the first final.

Start latency: `start()` connects and sends run-task but does not wait for task-started;
`feed()` buffers frames until task-started arrives, then the reader flushes the buffer in
order (xiaozhi does the same with its cached frames). `finish()` before task-started is
deferred until after the flush. If the task dies before `finish()`, `results()` stays
open and `finish()` emits the fallback final at once, so the partial text is not lost.

Models: `paraformer-realtime-v2` is the one both references use (default). The
`fun-asr-realtime` family is not present in the workspace references; it is assumed to
use the same run-task protocol and is passed through unchanged. Per the references,
`vocabulary_id` only applies to `*-v2` paraformer models (v1 used `phrase_id`), so it is
dropped with a warning for paraformer v1 models. All other optional parameters are sent
only when configured, so the server applies its own per-model defaults.

Endpoint: mainland `wss://dashscope.aliyuncs.com/api-ws/v1/inference`; the international
(Singapore) endpoint `wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference` takes the same
path (alibl_endpoint.py); API keys are region-specific.

Config keys: api_key (else env DASHSCOPE_API_KEY), model ("paraformer-realtime-v2"),
url (mainland endpoint above), sample_rate (16000), language_hints (list, e.g. ["zh", "en"]),
vocabulary_id, disfluency_removal_enabled, semantic_punctuation_enabled,
max_sentence_silence (ms, 200–6000, VAD sentence split only), language (informational).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from vak.upstream.providers._util import cfg, secret
from vak.upstream.providers.base import Transcript

log = logging.getLogger(__name__)

DEFAULT_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
DEFAULT_MODEL = "paraformer-realtime-v2"
_OPTIONAL_PARAMS = (
    "language_hints",
    "vocabulary_id",
    "disfluency_removal_enabled",
    "semantic_punctuation_enabled",
    "max_sentence_silence",
)


class AliyunStreamingSTT:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.api_key: str = secret(config, "api_key", "DASHSCOPE_API_KEY", "aliyun_stt") or ""
        self.model: str = str(cfg(config, "model", DEFAULT_MODEL))
        self.sample_rate: int = int(cfg(config, "sample_rate", 16000))
        self.language: str | None = config.get("language")
        self.task_id = uuid.uuid4().hex
        self._ws: Any = None
        self._queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._pending: list[bytes] = []  # audio fed before task-started
        self._task_started = False  # task-started event seen
        self._ready = False  # task-started seen and buffer flushed
        self._finished = False  # finish() called
        self._finish_sent = False
        self._dead = False  # reader ended before finish()
        self._got_final = False
        self._ended = False  # None put on the queue
        self._sentences: list[str] = []  # completed sentences
        self._current = ""  # tentative sentence
        self._last_partial = ""
        self._start_ms: int | None = None
        self._end_ms: int | None = None

    # ------------------------------------------------------------------

    def run_task_message(self) -> dict[str, Any]:
        params: dict[str, Any] = {"format": "pcm", "sample_rate": self.sample_rate}
        for key in _OPTIONAL_PARAMS:
            v = self.config.get(key)
            if v is None or v == "":
                continue
            if (
                key == "vocabulary_id"
                and self.model.startswith("paraformer")
                and not self.model.endswith("v2")
            ):
                log.warning("aliyun_stt: vocabulary_id is not supported by %s; ignored", self.model)
                continue
            if key == "language_hints" and isinstance(v, str):
                v = [v]
            params[key] = int(v) if key == "max_sentence_silence" else v
        return {
            "header": {"action": "run-task", "task_id": self.task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.model,
                "parameters": params,
                "input": {},
            },
        }

    def finish_task_message(self) -> dict[str, Any]:
        return {
            "header": {"action": "finish-task", "task_id": self.task_id, "streaming": "duplex"},
            "payload": {"input": {}},
        }

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "X-DashScope-DataInspection": "enable"}

    async def _ws_connect(self, url: str, headers: dict[str, str]) -> Any:
        from websockets.asyncio.client import connect

        return await connect(url, additional_headers=headers, max_size=None)

    async def start(self) -> None:
        self._ws = await self._ws_connect(str(cfg(self.config, "url", DEFAULT_URL)), self.headers())
        self._reader = asyncio.create_task(self._read_loop())
        await self._ws.send(json.dumps(self.run_task_message(), ensure_ascii=False))

    async def feed(self, pcm16: bytes) -> None:
        if self._ws is None and not self._dead:
            raise RuntimeError("aliyun_stt: not started")
        if not pcm16 or self._finished or self._dead:
            return
        if self._ready:
            await self._ws.send(pcm16)
        else:
            self._pending.append(pcm16)

    async def finish(self) -> None:
        if self._ws is None and not self._dead:
            raise RuntimeError("aliyun_stt: not started")
        if self._finished:
            return
        self._finished = True
        if self._dead:
            self._emit_final()
            self._end()
        elif self._ready:
            await self._send_finish()
        # else: sent by the reader right after the task-started flush

    async def cancel(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None
        self._end()

    def results(self) -> AsyncIterator[Transcript]:
        return self._results()

    async def _results(self) -> AsyncIterator[Transcript]:
        while True:
            t = await self._queue.get()
            if t is None:
                return
            yield t

    # ------------------------------------------------------------------

    async def _send_finish(self) -> None:
        if not self._finish_sent:
            self._finish_sent = True
            await self._ws.send(json.dumps(self.finish_task_message(), ensure_ascii=False))

    async def _flush(self) -> None:
        # feed() keeps appending while we await; loop until the buffer is empty,
        # then flip _ready with no await in between so order is preserved.
        while self._pending:
            await self._ws.send(self._pending.pop(0))
        self._ready = True
        if self._finished:
            await self._send_finish()

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                done = self.handle_message(msg)
                if self._task_started and not self._ready:
                    await self._flush()
                if done:
                    break
            else:
                if not self._got_final:
                    log.warning("aliyun_stt: connection closed before task-finished")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("aliyun_stt: connection ended: %s", e)
        finally:
            if self._finished:
                self._emit_final()  # no-op when task-finished already produced it
                self._end()
            else:
                self._dead = True  # finish() will emit the fallback final
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass

    def handle_message(self, msg: dict[str, Any]) -> bool:
        """Map one server JSON message to Transcript(s). Returns True when the
        task is over (task-finished / task-failed): stop reading."""
        header = msg.get("header") or {}
        event = header.get("event", "")
        if event == "task-started":
            self._task_started = True
            return False
        if event == "result-generated":
            sentence = ((msg.get("payload") or {}).get("output") or {}).get("sentence") or {}
            self._on_sentence(sentence)
            return False
        if event == "task-finished":
            if self._finished:
                self._emit_final()
            else:
                log.warning("aliyun_stt: task-finished before finish()")
            return True
        if event == "task-failed":
            log.warning(
                "aliyun_stt: task failed: %s %s", header.get("error_code"), header.get("error_message")
            )
            return True
        return False

    def _on_sentence(self, sentence: dict[str, Any]) -> None:
        if sentence.get("heartbeat"):
            return
        text = sentence.get("text") or ""
        begin, end = sentence.get("begin_time"), sentence.get("end_time")
        if self._start_ms is None and isinstance(begin, int) and text:
            self._start_ms = begin
        if isinstance(end, int):
            self._end_ms = end
        if "sentence_end" in sentence:
            sentence_end = bool(sentence["sentence_end"])
        else:
            sentence_end = end is not None
        if sentence_end:
            if text:
                self._sentences.append(text)
            self._current = ""
        else:
            self._current = text
        cumulative = self._text()
        if cumulative and cumulative != self._last_partial and not self._got_final:
            self._last_partial = cumulative
            self._queue.put_nowait(
                Transcript(
                    text=cumulative,
                    final=False,
                    segment=len(self._sentences),
                    start_ms=self._start_ms,
                    end_ms=self._end_ms,
                )
            )

    def _text(self) -> str:
        out = ""
        for part in [*self._sentences, self._current]:
            if out and part and out[-1].isascii() and part[0].isascii() and not part[0].isspace():
                out += " "  # English sentences need a separator; CJK ones do not
            out += part
        return out

    def _emit_final(self) -> None:
        if self._got_final:
            return
        self._got_final = True
        self._queue.put_nowait(
            Transcript(text=self._text(), final=True, segment=0, start_ms=self._start_ms, end_ms=self._end_ms)
        )

    def _end(self) -> None:
        if not self._ended:
            self._ended = True
            self._queue.put_nowait(None)
