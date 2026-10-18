"""OpenAI Realtime API in transcription mode as a streaming `STT`.

One WebSocket per utterance (push-to-talk turn); server VAD is disabled and
the client decides end-of-utterance by committing the input buffer.

Wire format (GA Realtime API; livekit-plugins-openai `stt.py` `_session_update`
and pipecat `services/openai/stt.py` `OpenAIRealtimeSTTService`):

    connect  wss://api.openai.com/v1/realtime?intent=transcription
             header  Authorization: Bearer <key>   (no OpenAI-Beta header on GA)
    -> {"type": "session.update", "session": {"type": "transcription",
         "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000},
                             "transcription": {"model": ..., "language"?: ..., "prompt"?: ...},
                             "turn_detection": null,
                             "noise_reduction"?: {"type": "near_field"|"far_field"}}}}}
    -> {"type": "input_audio_buffer.append", "audio": <base64 PCM16 mono 24 kHz>}   per frame
    -> {"type": "input_audio_buffer.commit"}                                       on finish()
    <- session.created / session.updated / input_audio_buffer.committed            (informational)
    <- conversation.item.input_audio_transcription.delta     {"item_id", "delta"}  incremental fragment
    <- conversation.item.input_audio_transcription.completed {"item_id", "transcript"}
    <- conversation.item.input_audio_transcription.failed    {"item_id", "error"}
    <- error  {"error": {"type", "code", "message"}}   e.g. code input_audio_buffer_commit_empty

Session message shape: both references send the GA `session.update` with
`session.type="transcription"` (nested `audio.input`), so that is used here.
The older beta shape — `transcription_session.update` with flat
`input_audio_format: "pcm16"`, `input_audio_transcription`, `turn_detection`,
`input_audio_noise_reduction`, plus header `OpenAI-Beta: realtime=v1` — is not
implemented. For hosts other than api.openai.com, `?model=` is appended to the
URL (livekit: gateways route on it; api.openai.com rejects it with
invalid_model in transcription mode).

Ordering: like livekit, `session.update` is sent right after the socket opens
without waiting for `session.created`/`session.updated`; client events are
processed in order, so appends sent before the ack are not lost. The wire
rate is fixed at 24 kHz PCM16; input at `sample_rate` is resampled.

Partials: with `turn_detection: null` the server transcribes only committed
audio, so deltas start after `finish()` (commit) and arrive between the commit
and `.completed`; expect no partials while the user is still speaking.
gpt-4o(-mini)-transcribe stream incremental deltas, which are accumulated into
cumulative `final=False` Transcripts. Exactly one `final=True` follows: the
`.completed` transcript, or (after `.failed`, an `error`, or the socket ending)
the accumulated deltas; an empty-commit error gives "". Where the references
differ (pipecat handles `.failed`, livekit throttles partials to 0.5 s and
reconnects), the more complete event handling is kept and the retry dropped.

Config keys: api_key (else $OPENAI_API_KEY, required), base_url (default
wss://api.openai.com/v1/realtime), model (gpt-4o-transcribe; also
gpt-4o-mini-transcribe), language (ISO-639-1, optional), prompt (optional),
sample_rate (16000, input rate of `feed()`), noise_reduction
(near_field | far_field | None = off).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode, urlparse

from vak.upstream.providers._util import cfg, resample_pcm16, secret
from vak.upstream.providers.base import Transcript

log = logging.getLogger(__name__)

WIRE_SAMPLE_RATE = 24000
DEFAULT_BASE_URL = "wss://api.openai.com/v1/realtime"
DEFAULT_MODEL = "gpt-4o-transcribe"
_P = "conversation.item.input_audio_transcription."


class OpenAIRealtimeSTT:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.api_key: str = secret(config, "api_key", "OPENAI_API_KEY", "openai_realtime_stt") or ""
        self.model: str = str(cfg(config, "model", DEFAULT_MODEL))
        self.sample_rate: int = int(cfg(config, "sample_rate", 16000))
        self.language: str | None = config.get("language") or None
        self._ws: Any = None
        self._queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._text = ""  # accumulated delta fragments (best text so far)
        self._finished = False
        self._done = False  # final emitted (or stream ended)
        self._reader_ended = False

    # ------------------------------------------------------------------

    def url(self) -> str:
        base = str(cfg(self.config, "base_url", DEFAULT_BASE_URL)).rstrip("/")
        if base.startswith("http"):
            base = "ws" + base[4:]
        params = {"intent": "transcription"}
        if urlparse(base).hostname != "api.openai.com":
            params["model"] = self.model
        return f"{base}?{urlencode(params)}"

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def session_message(self) -> dict[str, Any]:
        transcription: dict[str, Any] = {"model": self.model}
        if self.language:
            transcription["language"] = self.language
        prompt = cfg(self.config, "prompt")
        if prompt:
            transcription["prompt"] = str(prompt)
        audio_input: dict[str, Any] = {
            "format": {"type": "audio/pcm", "rate": WIRE_SAMPLE_RATE},
            "transcription": transcription,
            "turn_detection": None,  # push-to-talk: the client commits
        }
        nr = cfg(self.config, "noise_reduction")
        if nr:
            audio_input["noise_reduction"] = {"type": str(nr)}
        return {"type": "session.update", "session": {"type": "transcription", "audio": {"input": audio_input}}}

    async def _ws_connect(self, url: str, headers: dict[str, str]) -> Any:
        from websockets.asyncio.client import connect

        return await connect(url, additional_headers=headers, max_size=None)

    async def start(self) -> None:
        self._ws = await self._ws_connect(self.url(), self.headers())
        await self._ws.send(json.dumps(self.session_message(), ensure_ascii=False))
        self._reader = asyncio.create_task(self._read_loop())

    async def feed(self, pcm16: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("openai_realtime_stt: not started")
        audio = resample_pcm16(pcm16, self.sample_rate, WIRE_SAMPLE_RATE)
        if audio:
            await self._ws.send(
                json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(audio).decode("ascii")})
            )

    async def finish(self) -> None:
        if self._ws is None:
            raise RuntimeError("openai_realtime_stt: not started")
        self._finished = True
        if self._reader_ended:
            # the socket already died before finish(): answer with what we have
            log.warning("openai_realtime_stt: connection ended before finish; using partial text")
            self._emit_final(self._text)
            self._queue.put_nowait(None)
            return
        await self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    async def cancel(self) -> None:
        self._done = True  # no fallback final from the reader after cancel
        if self._reader is not None:
            self._reader.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None
        await self._queue.put(None)

    def results(self) -> AsyncIterator[Transcript]:
        return self._results()

    async def _results(self) -> AsyncIterator[Transcript]:
        while True:
            t = await self._queue.get()
            if t is None:
                return
            yield t

    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if self.handle_message(msg):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("openai_realtime_stt: connection ended: %s", e)
        finally:
            self._reader_ended = True
            if self._finished:
                if not self._done:
                    log.warning("openai_realtime_stt: no completed transcript; using partial text")
                    self._emit_final(self._text)
                self._queue.put_nowait(None)
            # not finished yet: finish() emits the fallback final and ends the stream
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass

    def _emit_final(self, text: str) -> None:
        if not self._done:
            self._done = True
            self._queue.put_nowait(Transcript(text=text, final=True))

    def handle_message(self, msg: dict[str, Any]) -> bool:
        """Map one server event to Transcript(s). Returns True when the
        utterance is complete (stop reading)."""
        t = msg.get("type", "")
        if t == _P + "delta":
            delta = msg.get("delta") or ""
            if delta and not self._done:
                self._text += delta
                self._queue.put_nowait(Transcript(text=self._text, final=False))
            return False
        if t == _P + "completed":
            transcript = msg.get("transcript") or ""
            if not self._finished:
                # cannot happen with turn_detection null; keep it as best text
                log.debug("openai_realtime_stt: completed before finish ignored")
                self._text = transcript
                return False
            self._emit_final(transcript)
            return True
        if t == _P + "failed":
            err = msg.get("error") or {}
            log.warning("openai_realtime_stt: transcription failed: %s", err.get("message") or err)
            if self._finished:
                self._emit_final(self._text)
                return True
            return False
        if t == "error":
            err = msg.get("error") or {}
            code = err.get("code")
            if code == "input_audio_buffer_commit_empty":
                log.warning("openai_realtime_stt: empty or too-short utterance")
                if self._finished:
                    self._emit_final("")
                    return True
                return False
            log.warning("openai_realtime_stt: error %s: %s", code, err.get("message"))
            if self._finished:
                self._emit_final(self._text)
                return True
            return False
        return False
