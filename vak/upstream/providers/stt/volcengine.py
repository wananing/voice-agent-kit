"""Volcengine / ByteDance (豆包) big-model streaming ASR, SAUC v3 (`STT`).

Endpoints (selected by `mode`):

    async    wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async     (default; bidirectional,
             a response is sent only when the result changes)
    stream   wss://openspeech.bytedance.com/api/v3/sauc/bigmodel           (bidirectional, one response per packet)
    nostream wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream  (streaming input, result only
             after the last packet; the only mode that accepts `audio.language`)

Auth is in the HTTP upgrade headers: X-Api-App-Key, X-Api-Access-Key,
X-Api-Resource-Id, X-Api-Connect-Id (a fresh UUID per connection). The
response's X-Tt-Logid is logged for support tickets.

Binary frame (all integers big-endian):

    byte 0  version(4)=0b0001 | header_size(4)=1 (x4 bytes)
    byte 1  message_type(4)   | flags(4)
    byte 2  serialization(4)  | compression(4)
    byte 3  reserved
    [int32 sequence]   if flags & 0b0001
    [int32 event]      if flags & 0b0100   (server only)
    uint32 payload_size, payload

    message types: 0b0001 full client request (JSON, gzip)
                   0b0010 audio-only request  (raw bytes, gzip)
                   0b1001 full server response (JSON, gzip)
                   0b1111 server error: uint32 code, uint32 size, UTF-8 message (uncompressed)
    flags:         0b0001 positive sequence follows, 0b0010 last packet,
                   0b0011 last packet with a negative sequence

Sequence on the wire:

    -> full client request, seq 1, gzip JSON {"user", "audio", "request"}
    -> audio-only requests, seq 2, 3, ... (PCM16 buffered to ~100 ms, gzip)
    -> last audio packet: flags 0b0011, seq = -n, carries the remaining buffered audio
    <- full server responses {"audio_info": {"duration"}, "result": {"text", "utterances":
           [{"text", "start_time", "end_time", "definite"}]}}; the response to the last
           packet carries flags & 0b0010

Mapping: definite utterances are kept by `start_time`; partials carry the
cumulative text (definite utterances + the current tentative one). Exactly one
`Transcript(final=True)` is emitted, only after `finish()`, when the server's
last-packet response arrives (or, as a fallback, when the connection ends).

`result_type` defaults to "full": the server then resends the whole utterance
list each time, so cumulative text survives a missed response and needs no
client-side stitching. "single" (both references' default) is also handled:
definites are accumulated by start_time either way.

Reference differences (xiaozhi-esp32-server doubao_stream.py vs TEN
bytedance_llm_based_asr):
- Sequence numbers: xiaozhi sends every frame without a sequence (flags 0b0000)
  and ends with flags 0b0010 + empty payload; TEN numbers every frame (flags
  0b0001) but never sends a last packet (it pads 800 ms of silence instead).
  We number frames like TEN and end with 0b0011 + negative sequence, the
  pairing used by the official SAUC demo (0b0010 without sequence is the
  documented alternative).
- xiaozhi also sends a v2-style {"app": {"appid", "token"}} block and
  codec "pcm"; v3 authenticates via headers only, so we send no "app" block
  and the documented codec "raw".
- end_window_size: xiaozhi always sends 200, TEN 800 (= server default). We
  send it only when configured; push-to-talk ends with the last packet anyway.
- TEN defaults enable_nonstream=true (second-pass re-recognition of definite
  utterances, async mode only); we expose it but leave it off by default.

Config keys: app_key (env VOLC_ASR_APP_KEY), access_key (env
VOLC_ASR_ACCESS_KEY), resource_id ("volc.bigasr.sauc.duration"), mode
("async"), url (overrides mode), sample_rate (16000), language (sent only in
nostream mode), enable_itn (True), enable_punc (True), enable_ddc (False),
enable_nonstream (False), show_utterances (True), result_type ("full"),
end_window_size (unset), hotwords (list of str -> corpus.context),
boosting_table_name (unset), uid ("vak").
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import struct
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from vak.upstream.providers._util import ProviderConfigError, cfg, secret
from vak.upstream.providers.base import Transcript

log = logging.getLogger(__name__)

_NAME = "volcengine_stt"
_BASE = "wss://openspeech.bytedance.com/api/v3/sauc/"
MODE_URLS = {
    "async": _BASE + "bigmodel_async",
    "stream": _BASE + "bigmodel",
    "nostream": _BASE + "bigmodel_nostream",
}

VERSION = 0b0001
MSG_FULL_CLIENT = 0b0001
MSG_AUDIO_ONLY = 0b0010
MSG_FULL_SERVER = 0b1001
MSG_ERROR = 0b1111
FLAG_POS_SEQ = 0b0001
FLAG_LAST = 0b0010
FLAG_NEG_SEQ = 0b0011
FLAG_EVENT = 0b0100
SER_NONE = 0b0000
SER_JSON = 0b0001
GZIP = 0b0001

_CHUNK_MS = 100  # official recommendation: 100-200 ms per audio packet


def encode_frame(msg_type: int, flags: int, serialization: int, payload: bytes, seq: int | None = None) -> bytes:
    """Build one protocol frame; `payload` is gzip-compressed here."""
    body = gzip.compress(payload)
    out = bytearray([(VERSION << 4) | 1, (msg_type << 4) | flags, (serialization << 4) | GZIP, 0])
    if seq is not None:
        out += struct.pack(">i", seq)
    out += struct.pack(">I", len(body)) + body
    return bytes(out)


@dataclass(slots=True)
class Frame:
    msg_type: int
    flags: int
    seq: int | None = None
    payload: Any = None  # parsed JSON (dict), raw bytes, or error message str
    code: int = 0  # error code for MSG_ERROR

    @property
    def is_last(self) -> bool:
        return bool(self.flags & FLAG_LAST)


def parse_frame(data: bytes) -> Frame:
    """Decode one frame (server or client direction)."""
    if len(data) < 4:
        raise ValueError(f"frame too short: {len(data)} bytes")
    header_len = (data[0] & 0x0F) * 4
    msg_type, flags = data[1] >> 4, data[1] & 0x0F
    serialization, compression = data[2] >> 4, data[2] & 0x0F
    rest = data[header_len:]
    frame = Frame(msg_type=msg_type, flags=flags)
    if flags & FLAG_POS_SEQ:
        frame.seq = struct.unpack(">i", rest[:4])[0]
        rest = rest[4:]
    if flags & FLAG_EVENT:
        rest = rest[4:]
    if msg_type == MSG_ERROR:
        frame.code, size = struct.unpack(">iI", rest[:8])
        frame.payload = rest[8 : 8 + size].decode("utf-8", "replace")
        return frame
    if len(rest) < 4:
        return frame
    size = struct.unpack(">I", rest[:4])[0]
    body = rest[4 : 4 + size]
    if compression == GZIP and body:
        body = gzip.decompress(body)
    frame.payload = json.loads(body.decode("utf-8")) if serialization == SER_JSON and body else body
    return frame


class VolcengineStreamingSTT:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.app_key = secret(config, "app_key", "VOLC_ASR_APP_KEY", _NAME)
        self.access_key = secret(config, "access_key", "VOLC_ASR_ACCESS_KEY", _NAME)
        self.resource_id = str(cfg(config, "resource_id", "volc.bigasr.sauc.duration"))
        self.mode = str(cfg(config, "mode", "async"))
        if self.mode not in MODE_URLS:
            raise ProviderConfigError(f"{_NAME}: mode must be one of {sorted(MODE_URLS)}, got {self.mode!r}")
        self.url = str(cfg(config, "url", MODE_URLS[self.mode]))
        self.sample_rate: int = int(cfg(config, "sample_rate", 16000))
        self.language: str | None = config.get("language")
        self._ws: Any = None
        self._queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._seq = 0
        self._buf = bytearray()
        self._chunk_bytes = self.sample_rate * 2 * _CHUNK_MS // 1000
        self._definite: dict[int, tuple[str, int]] = {}  # start_time -> (text, end_time)
        self._tentative = ""
        self._last_partial = ""
        self._start_ms: int | None = None
        self._end_ms: int | None = None
        self._finished = False
        self._got_final = False
        self._cancelled = False
        self._ended = False

    # ------------------------------------------------------------------

    def headers(self) -> dict[str, str]:
        return {
            "X-Api-App-Key": str(self.app_key),
            "X-Api-Access-Key": str(self.access_key),
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

    def full_request(self) -> dict[str, Any]:
        c = self.config
        audio: dict[str, Any] = {"format": "pcm", "codec": "raw", "rate": self.sample_rate, "bits": 16, "channel": 1}
        if self.mode == "nostream" and self.language:
            audio["language"] = self.language
        request: dict[str, Any] = {
            "model_name": "bigmodel",
            "enable_itn": bool(cfg(c, "enable_itn", True)),
            "enable_punc": bool(cfg(c, "enable_punc", True)),
            "enable_ddc": bool(cfg(c, "enable_ddc", False)),
            "show_utterances": bool(cfg(c, "show_utterances", True)),
            "result_type": str(cfg(c, "result_type", "full")),
        }
        if cfg(c, "enable_nonstream", False):
            request["enable_nonstream"] = True
        if c.get("end_window_size") is not None:
            request["end_window_size"] = int(c["end_window_size"])
        corpus: dict[str, Any] = {}
        if c.get("hotwords"):
            corpus["context"] = json.dumps({"hotwords": [{"word": w} for w in c["hotwords"]]}, ensure_ascii=False)
        if c.get("boosting_table_name"):
            corpus["boosting_table_name"] = str(c["boosting_table_name"])
        if corpus:
            request["corpus"] = corpus
        return {"user": {"uid": str(cfg(c, "uid", "vak"))}, "audio": audio, "request": request}

    async def _ws_connect(self, url: str, headers: dict[str, str]) -> Any:
        from websockets.asyncio.client import connect

        ws = await connect(url, additional_headers=headers, max_size=None)
        response = getattr(ws, "response", None)
        if response is not None:
            log.info("volcengine: connected %s logid=%s", url, response.headers.get("X-Tt-Logid"))
        return ws

    async def start(self) -> None:
        self._ws = await self._ws_connect(self.url, self.headers())
        self._seq = 1
        payload = json.dumps(self.full_request(), ensure_ascii=False).encode("utf-8")
        await self._ws.send(encode_frame(MSG_FULL_CLIENT, FLAG_POS_SEQ, SER_JSON, payload, seq=self._seq))
        self._reader = asyncio.create_task(self._read_loop())

    async def feed(self, pcm16: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("volcengine: not started")
        if self._finished:
            return
        self._buf += pcm16
        while len(self._buf) >= self._chunk_bytes:
            chunk = bytes(self._buf[: self._chunk_bytes])
            del self._buf[: self._chunk_bytes]
            self._seq += 1
            await self._ws.send(encode_frame(MSG_AUDIO_ONLY, FLAG_POS_SEQ, SER_NONE, chunk, seq=self._seq))

    async def finish(self) -> None:
        if self._ws is None:
            raise RuntimeError("volcengine: not started")
        if self._finished:
            return
        self._finished = True
        self._seq += 1
        tail = bytes(self._buf)
        self._buf.clear()
        await self._ws.send(encode_frame(MSG_AUDIO_ONLY, FLAG_NEG_SEQ, SER_NONE, tail, seq=-self._seq))

    async def cancel(self) -> None:
        self._cancelled = True
        if self._reader is not None:
            self._reader.cancel()
        if self._ws is not None:
            ws, self._ws = self._ws, None
            try:
                await ws.close()
            except Exception as e:
                log.debug("volcengine: close failed: %s", e)
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

    def _end(self) -> None:
        if not self._ended:
            self._ended = True
            self._queue.put_nowait(None)

    @property
    def text(self) -> str:
        """Cumulative utterance text: definite segments in time order + tentative."""
        return "".join(t for _, (t, _e) in sorted(self._definite.items())) + self._tentative

    def _emit_final(self) -> None:
        if not self._got_final:
            self._got_final = True
            self._queue.put_nowait(
                Transcript(
                    text=self.text, final=True, segment=len(self._definite), start_ms=self._start_ms, end_ms=self._end_ms
                )
            )

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                if not isinstance(raw, bytes):
                    log.debug("volcengine: ignoring text message %r", raw[:200])
                    continue
                try:
                    frame = parse_frame(raw)
                except (ValueError, struct.error, OSError, json.JSONDecodeError) as e:
                    log.warning("volcengine: undecodable frame (%s)", e)
                    continue
                if self.handle_frame(frame):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("volcengine: connection ended: %s", e)
        finally:
            if self._finished and not self._cancelled:
                self._emit_final()  # finish() must produce a final; use what we have
            self._end()
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass

    def handle_frame(self, frame: Frame) -> bool:
        """Map one decoded server frame. Returns True when reading should stop."""
        if frame.msg_type == MSG_ERROR:
            log.warning("volcengine: server error %s: %s", frame.code, frame.payload)
            return True
        if frame.msg_type != MSG_FULL_SERVER:
            return False
        return self.handle_payload(frame.payload if isinstance(frame.payload, dict) else {}, frame.is_last)

    def handle_payload(self, payload: dict[str, Any], last: bool = False) -> bool:
        """Map one server JSON payload to Transcript(s). `last` is the frame's
        last-packet flag. Returns True when the utterance is complete."""
        result = payload.get("result")
        if isinstance(result, dict):
            utterances = result.get("utterances") or []
            if utterances:
                tentative = []
                for u in utterances:
                    text = u.get("text", "") or ""
                    start, end = int(u.get("start_time", -1)), int(u.get("end_time", -1))
                    if start >= 0 and (self._start_ms is None or start < self._start_ms):
                        self._start_ms = start
                    if end >= 0 and (self._end_ms is None or end > self._end_ms):
                        self._end_ms = end
                    if u.get("definite"):
                        self._definite[start] = (text, end)
                    else:
                        tentative.append(text)
                self._tentative = "".join(tentative)
            elif result.get("text") and not self._definite:
                self._tentative = result["text"]  # show_utterances=false
        if last and self._finished:
            self._emit_final()
            return True
        if last:
            log.warning("volcengine: server ended the stream before finish()")
        text = self.text
        if text and text != self._last_partial:
            self._last_partial = text
            self._queue.put_nowait(Transcript(text=text, final=False, segment=len(self._definite)))
        return last
