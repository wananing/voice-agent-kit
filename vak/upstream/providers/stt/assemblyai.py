"""AssemblyAI Universal-Streaming v3 STT (`STT`).

Wire format (livekit-plugins-assemblyai stt.py, pipecat services/assemblyai/
stt.py + models.py; https://www.assemblyai.com/docs/api-reference/streaming-api):

    connect  wss://streaming.assemblyai.com/v3/ws?sample_rate=16000&encoding=pcm_s16le
             &speech_model=universal-streaming-english&format_turns=true[&keyterms_prompt=["..",..]]
             header  Authorization: <key>          (raw key, no scheme)
    <- JSON  {"type": "Begin", "id": ..., "expires_at": ...}
    -> binary PCM16 mono frames
    <- JSON  {"type": "Turn", "turn_order": n, "end_of_turn": bool, "turn_is_formatted": bool,
              "transcript": "...", "end_of_turn_confidence": f,
              "words": [{"text", "start", "end", "confidence", "word_is_final"}]}
    <- JSON  {"type": "SpeechStarted", "timestamp": ms}           ignored
    -> JSON  {"type": "ForceEndpoint"}    on finish(): end the current turn now
    -> JSON  {"type": "Terminate"}        on finish(), right after ForceEndpoint
    <- JSON  {"type": "Termination", "audio_duration_seconds": .., "session_duration_seconds": ..}
    <- JSON  {"error": "..."}             (or a close code with a reason)

Turn semantics: `words` is cumulative for the open turn and includes words not
yet final, so a partial's text is the words joined; `transcript` holds the
finalized words and, on `end_of_turn`, the whole turn. With format_turns the
server sends an unformatted end_of_turn first and the formatted one (same
turn_order) after it; only `end_of_turn and (turn_is_formatted or not
format_turns)` closes a turn - both references use exactly this rule. The
utterance text is all closed turns joined in turn order plus the open turn.

finish() sends ForceEndpoint and Terminate together. ForceEndpoint is what
pipecat sends on VAD stop: it closes the open turn at once, which is the fastest
final. Terminate (what both references send on close) guarantees an end signal
even when no turn is open (e.g. the last turn already ended on its own, where
ForceEndpoint yields nothing): the server flushes and answers with Termination.

Utterance complete after finish(): a closing Turn whose turn_order is at least
that of the turn open at finish() (or, with none open, the next turn - i.e. the
forced one), else `Termination`, else the socket ending (fallback final from
the best text, warning). Closing turns of earlier turns arriving late are
folded, not treated as complete. Remaining race: if the open turn ends
naturally and the user speaks a new turn whose first partial has not reached
us when the button is released, the utterance closes one turn early - only a
real run can tell how often.

Differences between references: pipecat defaults format_turns=True, livekit
leaves it to the server; we default True (punctuated text for the LLM, at the
cost of the formatting delay). Neither reference reconnects here (project rule).

Config keys: api_key (else $ASSEMBLYAI_API_KEY), url (default
wss://streaming.assemblyai.com/v3/ws), sample_rate (16000), speech_model
("universal-streaming-english"; "universal-streaming-multilingual" or a
Universal-3 Pro model for other languages), format_turns (True), keyterms
(list[str] -> keyterms_prompt), language (informational only).
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

_NAME = "assemblyai"
_CJK = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]")


def join_text(parts: list[str]) -> str:
    """Join pieces with a space, except next to CJK characters."""
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out and not (_CJK.match(out[-1]) or _CJK.match(p[0])):
            out += " "
        out += p
    return out


class AssemblyAIStreamingSTT:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.api_key: str = secret(config, "api_key", "ASSEMBLYAI_API_KEY", _NAME) or ""
        self.sample_rate: int = int(cfg(config, "sample_rate", 16000))
        self.language: str | None = config.get("language")
        self.format_turns: bool = bool(cfg(config, "format_turns", True))
        self._ws: Any = None
        self._queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._turns: dict[int, str] = {}  # closed turns by turn_order
        self._current = ""  # text of the open turn
        self._open_order: int | None = None  # turn_order of the open turn
        self._final_order = 0  # the first turn_order that may close the utterance after finish()
        self._last = ""  # last text put on the queue (duplicate suppression)
        self._start_ms: int | None = None
        self._end_ms: int | None = None
        self._finished = False
        self._done = False  # the single final was emitted
        self._cancelled = False

    # ------------------------------------------------------------------

    def build_url(self) -> str:
        params: dict[str, Any] = {
            "sample_rate": self.sample_rate,
            "encoding": "pcm_s16le",
            "speech_model": str(cfg(self.config, "speech_model", "universal-streaming-english")),
            "format_turns": "true" if self.format_turns else "false",
        }
        keyterms = self.config.get("keyterms")
        if keyterms:
            params["keyterms_prompt"] = json.dumps([keyterms] if isinstance(keyterms, str) else list(keyterms))
        base = str(cfg(self.config, "url", "wss://streaming.assemblyai.com/v3/ws"))
        return f"{base}?{urlencode(params)}"

    def headers(self) -> dict[str, str]:
        return {"Authorization": self.api_key}

    async def _ws_connect(self, url: str, headers: dict[str, str]) -> Any:
        from websockets.asyncio.client import connect

        return await connect(url, additional_headers=headers, max_size=None)

    async def start(self) -> None:
        self._ws = await self._ws_connect(self.build_url(), self.headers())
        self._reader = asyncio.create_task(self._read_loop())

    async def feed(self, pcm16: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("assemblyai: not started")
        if pcm16 and not self._finished:
            await self._ws.send(pcm16)

    async def finish(self) -> None:
        if self._ws is None:
            raise RuntimeError("assemblyai: not started")
        if self._finished:
            return
        self._finished = True
        # ForceEndpoint closes the open turn; with none open, the next turn (if any)
        if self._open_order is not None:
            self._final_order = self._open_order
        else:
            self._final_order = max(self._turns, default=-1) + 1
        try:
            await self._ws.send(json.dumps({"type": "ForceEndpoint"}))
            await self._ws.send(json.dumps({"type": "Terminate"}))
        except Exception as e:  # the reader's finally emits the fallback final
            log.warning("assemblyai: sending ForceEndpoint/Terminate failed: %s", e)

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
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if self.handle_message(msg):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("assemblyai: connection ended: %s", e)
        finally:
            if not self._cancelled:
                if self._finished and not self._done:
                    log.warning("assemblyai: stream ended after finish without a closing turn; using best text")
                    self._emit_final()
                self._queue.put_nowait(None)
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass

    def _text(self) -> str:
        return join_text([*(self._turns[k] for k in sorted(self._turns)), self._current])

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
        utterance is complete or the session is over (stop reading)."""
        if "error" in msg:
            log.warning("assemblyai: error: %s", msg.get("error"))
            return True  # the reader's finally emits the fallback final if finish() was called
        mtype = msg.get("type")
        if mtype == "Turn":
            words = [w for w in msg.get("words") or [] if isinstance(w, dict)]
            transcript = msg.get("transcript") or ""
            if words:
                if self._start_ms is None and isinstance(words[0].get("start"), (int, float)):
                    self._start_ms = int(words[0]["start"])
                if isinstance(words[-1].get("end"), (int, float)):
                    self._end_ms = int(words[-1]["end"])
            closing = bool(msg.get("end_of_turn")) and (bool(msg.get("turn_is_formatted")) or not self.format_turns)
            order = msg.get("turn_order")
            if not isinstance(order, int):
                order = self._open_order if self._open_order is not None else max(self._turns, default=-1) + 1
            if msg.get("end_of_turn"):
                # unformatted end_of_turn: text is final, the formatted copy replaces it later
                self._turns[order] = transcript
                if self._open_order is None or self._open_order <= order:  # not a late copy of an older turn
                    self._current = ""
                    self._open_order = None if closing else order
            else:
                self._open_order = order
                self._current = join_text([str(w.get("text", "")) for w in words]) or transcript
            self._emit_partial()
            if closing and self._finished and order >= self._final_order:
                self._emit_final()
                return True
            return False
        if mtype == "Termination":
            if self._finished and not self._done:
                self._emit_final()
            return True
        if mtype == "Begin":
            log.debug("assemblyai: session %s begins", msg.get("id"))
        return False  # Begin, SpeechStarted, unknown
