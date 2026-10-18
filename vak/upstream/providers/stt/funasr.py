"""FunASR `funasr_wss_server` 2pass client (`STT`).

Wire format (FunASR/runtime/python/websocket/funasr_wss_client.py and
funasr_wss_server.py; runtime/docs/websocket_protocol_zh.md):

    -> JSON  {"mode": "2pass", "chunk_size": [5,10,5], "chunk_interval": 10,
              "encoder_chunk_look_back": 4, "decoder_chunk_look_back": 0,
              "audio_fs": 16000, "wav_name": ..., "wav_format": "pcm",
              "is_speaking": true, "hotwords": "...", "itn": true}
    -> binary PCM16 chunks
    -> JSON  {"is_speaking": false}             (end of utterance)
    <- JSON  {"mode": "2pass-online",  "text": "...", "wav_name": ..., "is_final": false}   partial
    <- JSON  {"mode": "2pass-offline", "text": "...", "wav_name": ..., "is_final": ..., "timestamp"?: ...}  final per VAD segment
    <- JSON  {"is_end": true, "is_final": true|false, "mode": ..., "error"?: ...}  end acknowledgement (patched server)

Online partials are fragments to be concatenated within the current segment;
each offline message closes a segment. One user turn may produce several
offline segments (handbook 04-projects/asr/funasr.md); `Transcript.segment`
counts them. Segments closed before `finish()` are folded into the
cumulative partial text; the single final (after `finish()`) carries the
whole utterance, because the pipeline stops reading at the first final.
Audio: int16 PCM, 8/16 kHz (declared via `audio_fs`); no Opus.

Config keys: url (default ws://localhost:10095), sample_rate (16000),
chunk_size, chunk_interval, encoder_chunk_look_back, decoder_chunk_look_back,
hotwords (str or {word: weight}), itn (True), wav_name, ssl_verify (True),
language (informational only).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from vak.upstream.providers._util import cfg
from vak.upstream.providers.base import Transcript

log = logging.getLogger(__name__)


class FunASRStreamingSTT:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.sample_rate: int = int(cfg(config, "sample_rate", 16000))
        self.language: str | None = config.get("language")
        self._ws: Any = None
        self._queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._segment = 0
        self._online = ""  # concatenated online fragments of the current segment
        self._closed_text = ""  # offline text of the segments closed so far
        self._finished = False
        self._got_final = False

    # ------------------------------------------------------------------

    def init_message(self) -> dict[str, Any]:
        hot = cfg(self.config, "hotwords", "")
        if isinstance(hot, dict):
            hot = json.dumps(hot, ensure_ascii=False)
        return {
            "mode": "2pass",
            "chunk_size": list(cfg(self.config, "chunk_size", [5, 10, 5])),
            "chunk_interval": int(cfg(self.config, "chunk_interval", 10)),
            "encoder_chunk_look_back": int(cfg(self.config, "encoder_chunk_look_back", 4)),
            "decoder_chunk_look_back": int(cfg(self.config, "decoder_chunk_look_back", 0)),
            "audio_fs": self.sample_rate,
            "wav_name": str(cfg(self.config, "wav_name", "vak")),
            "wav_format": "pcm",
            "is_speaking": True,
            "hotwords": hot,
            "itn": bool(cfg(self.config, "itn", True)),
        }

    async def _ws_connect(self, url: str) -> Any:
        import ssl

        from websockets.asyncio.client import connect

        ssl_ctx = None
        if url.startswith("wss://") and not cfg(self.config, "ssl_verify", True):
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        return await connect(url, subprotocols=["binary"], ping_interval=None, ssl=ssl_ctx, max_size=None)

    async def start(self) -> None:
        self._ws = await self._ws_connect(str(cfg(self.config, "url", "ws://localhost:10095")))
        await self._ws.send(json.dumps(self.init_message(), ensure_ascii=False))
        self._reader = asyncio.create_task(self._read_loop())

    async def feed(self, pcm16: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("funasr: not started")
        if pcm16:
            await self._ws.send(pcm16)

    async def finish(self) -> None:
        if self._ws is None:
            raise RuntimeError("funasr: not started")
        self._finished = True
        await self._ws.send(json.dumps({"is_speaking": False, "is_end": True}))

    async def cancel(self) -> None:
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
            log.warning("funasr: connection ended: %s", e)
        finally:
            if self._finished and not self._got_final:
                # spec 04 §3.3: finish() must produce a final; use what we have
                self._queue.put_nowait(Transcript(text=self._closed_text + self._online, final=True, segment=self._segment))
            self._queue.put_nowait(None)
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass

    def handle_message(self, msg: dict[str, Any]) -> bool:
        """Map one server JSON message to Transcript(s). Returns True when the
        utterance is complete (stop reading)."""
        if msg.get("is_end"):
            if self._finished and not self._got_final:
                self._queue.put_nowait(Transcript(text=self._closed_text + self._online, final=True, segment=self._segment))
                self._got_final = True
            return True
        mode = msg.get("mode", "")
        text = msg.get("text", "") or ""
        if mode in ("2pass-online", "online"):
            if text:
                self._online += text
                self._queue.put_nowait(
                    Transcript(text=self._closed_text + self._online, final=False, segment=self._segment)
                )
            return False
        if mode in ("2pass-offline", "offline"):
            start_ms = end_ms = None
            ts = msg.get("timestamp")
            if isinstance(ts, str) and ts:
                try:
                    stamps = json.loads(ts)
                    start_ms, end_ms = int(stamps[0][0]), int(stamps[-1][1])
                except (ValueError, IndexError, TypeError):
                    pass
            self._closed_text += text
            if self._finished:
                self._queue.put_nowait(
                    Transcript(text=self._closed_text, final=True, segment=self._segment, start_ms=start_ms, end_ms=end_ms)
                )
                self._got_final = True
            elif text:
                # a VAD segment closed mid-utterance: not the end of the user's turn
                self._queue.put_nowait(
                    Transcript(text=self._closed_text, final=False, segment=self._segment, start_ms=start_ms, end_ms=end_ms)
                )
            self._segment += 1
            self._online = ""
            # the offline pass after is_speaking=false closes the utterance
            return self._finished
        return False
