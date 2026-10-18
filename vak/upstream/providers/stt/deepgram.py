"""Deepgram live streaming STT, classic `/v1/listen` (`STT`).

Wire format (livekit-plugins-deepgram stt.py, pipecat services/deepgram/stt.py;
https://developers.deepgram.com/reference/speech-to-text/listen-streaming):

    connect  wss://api.deepgram.com/v1/listen?model=nova-3&language=en&encoding=linear16
             &sample_rate=16000&channels=1&interim_results=true&punctuate=true
             &smart_format=false[&no_delay=true][&endpointing=..][&keyterm=..]*[&keywords=w:boost]*
             header  Authorization: Token <key>
    -> binary PCM16 mono frames
    -> JSON  {"type": "Finalize"}       on finish(): flush buffered audio now
    -> JSON  {"type": "CloseStream"}    after the finalize result (server then sends Metadata and closes)
    <- JSON  {"type": "Results", "is_final": bool, "speech_final": bool, "from_finalize"?: bool,
              "start": s, "duration": s, "channel": {"alternatives": [{"transcript": "...", ...}]}}
    <- JSON  {"type": "Metadata", ...}  after CloseStream, right before the server closes
    <- JSON  {"type": "SpeechStarted" | "UtteranceEnd", ...}   ignored
    <- JSON  {"type": "Error", "description": ..., "message": ..., "variant": ...}

Result semantics: an interim (`is_final: false`) result replaces the previous
interim; an `is_final: true` result closes a segment of audio for good and the
next result starts a new one. `speech_final` only marks Deepgram's endpointing
and is treated like `is_final` here. The utterance text is all finalized
segments joined in order plus the current interim.

Utterance complete after finish(): the first `is_final` result with
`from_finalize: true` (Deepgram answers Finalize with one, possibly empty when
everything was already finalized - pipecat relies on this), else a `Metadata`
message, else the socket ending (fallback final from the best text, warning).
Only then is the single `Transcript(final=True)` emitted; vendor finals before
that are folded into the cumulative partials.

Differences between references: livekit sends Finalize on flush but never looks
at `from_finalize` (it waits for CloseStream / socket close); pipecat checks
`from_finalize` to confirm the flush. We follow pipecat for completion and send
CloseStream (livekit's close message) right after, so the server tears down.
Neither reconnects here (project rule). KeepAlive is not needed: one short
push-to-talk utterance per connection, audio flows the whole time.

Config keys: api_key (else $DEEPGRAM_API_KEY), url (default
wss://api.deepgram.com/v1/listen), model ("nova-3"), language ("en"),
sample_rate (16000), punctuate (True), smart_format (False), endpointing
(ms or False; server default when unset), keyterms (list[str], Nova-3),
keywords (list[str] "word:boost" or {word: boost}, Nova-2 and older).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

from vak.upstream.providers._util import cfg, secret
from vak.upstream.providers.base import Transcript

log = logging.getLogger(__name__)

_NAME = "deepgram"
_CJK = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]")


def join_text(parts: list[str]) -> str:
    """Join segments with a space, except next to CJK characters."""
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out and not (_CJK.match(out[-1]) or _CJK.match(p[0])):
            out += " "
        out += p
    return out


class DeepgramStreamingSTT:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.api_key: str = secret(config, "api_key", "DEEPGRAM_API_KEY", _NAME) or ""
        self.sample_rate: int = int(cfg(config, "sample_rate", 16000))
        self.language: str | None = cfg(config, "language", "en")
        self._ws: Any = None
        self._queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._segments: list[str] = []  # finalized segments, in order
        self._interim = ""
        self._last = ""  # last text put on the queue (duplicate suppression)
        self._start_ms: int | None = None
        self._end_ms: int | None = None
        self._finished = False
        self._done = False  # the single final was emitted
        self._cancelled = False

    # ------------------------------------------------------------------

    def build_url(self) -> str:
        params: dict[str, Any] = {
            "model": str(cfg(self.config, "model", "nova-3")),
            "encoding": "linear16",
            "sample_rate": self.sample_rate,
            "channels": 1,
            "interim_results": True,
            "punctuate": bool(cfg(self.config, "punctuate", True)),
            "smart_format": bool(cfg(self.config, "smart_format", False)),
        }
        if self.language:
            params["language"] = self.language
        if params["smart_format"]:
            params["no_delay"] = True  # don't hold results back for number formatting
        endpointing = self.config.get("endpointing")
        if endpointing is not None:
            params["endpointing"] = endpointing
        keyterms = self.config.get("keyterms")
        if keyterms:
            params["keyterm"] = [keyterms] if isinstance(keyterms, str) else list(keyterms)
        keywords = self.config.get("keywords")
        if keywords:
            if isinstance(keywords, dict):
                params["keywords"] = [f"{w}:{b}" for w, b in keywords.items()]
            else:
                params["keywords"] = [keywords] if isinstance(keywords, str) else list(keywords)
        params = {k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items()}
        base = str(cfg(self.config, "url", "wss://api.deepgram.com/v1/listen"))
        return f"{base}?{urlencode(params, doseq=True)}"

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self.api_key}"}

    async def _ws_connect(self, url: str, headers: dict[str, str]) -> Any:
        from websockets.asyncio.client import connect

        return await connect(url, additional_headers=headers, max_size=None)

    async def start(self) -> None:
        self._ws = await self._ws_connect(self.build_url(), self.headers())
        self._reader = asyncio.create_task(self._read_loop())

    async def feed(self, pcm16: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("deepgram: not started")
        if pcm16 and not self._finished:
            await self._ws.send(pcm16)

    async def finish(self) -> None:
        if self._ws is None:
            raise RuntimeError("deepgram: not started")
        if self._finished:
            return
        self._finished = True
        try:
            await self._ws.send(json.dumps({"type": "Finalize"}))
        except Exception as e:  # the reader's finally emits the fallback final
            log.warning("deepgram: sending Finalize failed: %s", e)

    async def cancel(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        if self._reader is not None:
            self._reader.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
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
        ws = self._ws
        complete = False
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if self.handle_message(msg):
                    complete = True
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("deepgram: connection ended: %s", e)
        finally:
            if not self._cancelled:
                if self._finished and not self._done:
                    log.warning("deepgram: stream ended after finish without a finalize result; using best text")
                    self._emit_final()
                self._queue.put_nowait(None)
            if ws is not None:
                if complete:
                    try:
                        await ws.send(json.dumps({"type": "CloseStream"}))
                    except Exception:
                        pass
                try:
                    await ws.close()
                except Exception:
                    pass

    def _text(self) -> str:
        return join_text([*self._segments, self._interim])

    def _emit_partial(self) -> None:
        text = self._text()
        if text and text != self._last and not self._done:
            self._last = text
            self._queue.put_nowait(Transcript(text=text, final=False, start_ms=self._start_ms, end_ms=self._end_ms))

    def _emit_final(self) -> None:
        self._done = True
        self._queue.put_nowait(Transcript(text=self._text(), final=True, start_ms=self._start_ms, end_ms=self._end_ms))

    def handle_message(self, msg: dict[str, Any]) -> bool:
        """Map one server JSON message onto the queue. Returns True when the
        utterance is complete (stop reading)."""
        mtype = msg.get("type")
        if mtype == "Results":
            alts = (msg.get("channel") or {}).get("alternatives") or []
            text = (alts[0].get("transcript") or "") if alts else ""
            if msg.get("is_final"):
                if text.strip():
                    self._segments.append(text)
                    start = msg.get("start")
                    if isinstance(start, (int, float)):
                        if self._start_ms is None:
                            self._start_ms = int(start * 1000)
                        self._end_ms = int((start + float(msg.get("duration") or 0)) * 1000)
                self._interim = ""
            else:
                self._interim = text
            self._emit_partial()
            if self._finished and msg.get("is_final") and msg.get("from_finalize"):
                self._emit_final()
                return True
            return False
        if mtype == "Metadata":
            if self._finished:
                self._emit_final()
                return True
            return False
        if mtype == "Error":
            log.warning(
                "deepgram: error %s: %s", msg.get("variant") or msg.get("message"), msg.get("description")
            )
            return True  # the reader's finally emits the fallback final if finish() was called
        return False  # SpeechStarted, UtteranceEnd, unknown
