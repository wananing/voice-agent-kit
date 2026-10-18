"""Volcengine / ByteDance (豆包) bidirectional streaming TTS v3 (`TTS`).

Endpoint: wss://openspeech.bytedance.com/api/v3/tts/bidirection
(docs: https://www.volcengine.com/docs/6561/1329505).

Auth is in the HTTP upgrade headers: X-Api-App-Key, X-Api-Access-Key,
X-Api-Resource-Id, X-Api-Connect-Id (a fresh UUID per connection). The
response's X-Tt-Logid is logged for support tickets.

Binary frame (all integers big-endian):

    byte 0  version(4)=0b0001 | header_size(4)=1 (x4 bytes)
    byte 1  message_type(4)   | flags(4)        flags 0b0100 = "carries an event"
    byte 2  serialization(4)  | compression(4)  JSON=1 / raw=0; none=0, gzip=1
    byte 3  reserved
    int32  event
    [uint32 id_size, id]   session id for session events (100-153, 200, 350-352);
                           connect id on ConnectionStarted/Failed/Finished (50-52);
                           absent on StartConnection / FinishConnection (1, 2)
    uint32 payload_size, payload

    message types: 0b0001 full client request (JSON)
                   0b1001 full server response (JSON)
                   0b1011 audio-only response (raw PCM, event TTSResponse=352)
                   0b1111 error: uint32 code, uint32 size, UTF-8 message

Events used: StartConnection 1 / ConnectionStarted 50 / ConnectionFailed 51,
FinishConnection 2 / ConnectionFinished 52, StartSession 100 /
SessionStarted 150, CancelSession 101 / SessionCanceled 151, FinishSession
102 / SessionFinished 152, SessionFailed 153, TaskRequest 200,
TTSSentenceStart 350, TTSSentenceEnd 351, TTSResponse 352.

Sequence per connection / per session:

    -> StartConnection {}                     <- ConnectionStarted   (once per connection)
    -> StartSession {user, event, namespace="BidirectionalTTS", req_params:
           {speaker, audio_params: {format="pcm", sample_rate, [speech_rate],
            [loudness_rate], [emotion]}}}     <- SessionStarted
    -> TaskRequest (same req_params + text) x N
    -> FinishSession {}
    <- TTSSentenceStart{text} / audio-only TTSResponse frames / TTSSentenceEnd ...
    <- SessionFinished (or SessionFailed -> RuntimeError with the server message)

Two ways in (`supports_text_stream=True`):
- `open_text_stream(request_id)`: one session per reply. The cascade pushes
  every LLM token delta as its own TaskRequest (xiaozhi huoshan_double_stream
  does the same) and the server segments sentences; each TTSSentenceStart's
  text rides on the sentence's first audio chunk (`TTSChunk.sentence`).
- `synthesize(text, request_id)`: one sentence per session (a text stream with
  a single push), used for verbatim speech and cues.

Connections and sessions: a connection carries one session at a time (both
references open the next session only after the previous one ended), so a
small pool is kept. A session takes an idle open connection or opens a new
one; with `tts_lookahead=1` (sentence mode) two sessions run concurrently on
two connections. A connection goes back to the pool only once its session
reached SessionFinished / SessionFailed / SessionCanceled; more than two idle
connections are closed. A pooled connection is checked for being open before
reuse. Nothing is retried: a send or receive failure drops that connection
and the error is raised.

`flush(request_id)` stops yielding at once and sends CancelSession (once
SessionStarted was seen; otherwise the stream's release sends it). When the
consumer stops early (flush, cancellation, `close()` before the end), the
connection goes to a background task that sends CancelSession if not yet
sent and drains until the session's terminal event (5 s limit); the
connection is pooled only then, closed otherwise.

Unverified against the live service: how long a session may sit without
text (the cascade opens it at reply start, before the first token), and
whether many one-token TaskRequests change pacing or prosody.

Reference differences (xiaozhi-esp32-server huoshan_double_stream.py vs TEN
bytedance_tts_duplex):
- StartConnection: TEN sends it and waits for ConnectionStarted; xiaozhi
  defines it but never sends it. We follow TEN and the official doc.
- SessionStarted: TEN waits for it before sending text; xiaozhi sends
  StartSession and text back-to-back. We wait (costs one RTT per sentence;
  skipping the wait is unverified).
- Cancel: xiaozhi sends CancelSession (101) and treats SessionCanceled (151)
  as the end; TEN has no CancelSession and tears down the whole connection on
  flush. We send CancelSession and drain; if the drain fails or times out the
  connection is closed, which is TEN's behaviour.
- Integers: xiaozhi packs event/sizes signed, TEN unsigned; identical for the
  values in use. We pack the event signed and sizes unsigned.
- ConnectionFailed: xiaozhi reads one string field (the error JSON), TEN
  reads a connect id then the payload. The decoder accepts both layouts.
- `additions`: xiaozhi sends it as a JSON string, TEN as an object; we send
  none. TEN also sends `model` (default "") and supports a single-header
  `x-api-key` auth; neither is used here.
- Resource: both default to `volc.service_type.10029` (豆包语音合成大模型 1.0,
  voices like zh_female_shuangkuaisisi_moon_bigtts). xiaozhi also documents
  `seed-tts-2.0` (豆包语音合成 2.0, voices like zh_female_xiaohe_uranus_bigtts),
  which emits no TTSSentenceStart/End events; audio framing is unchanged.
- Default speaker: TEN uses zh_female_shuangkuaisisi_moon_bigtts, xiaozhi
  zh_female_wanwanxiaohe_moon_bigtts (a separately purchased voice). We use
  TEN's.

Config keys: app_key (env VOLC_TTS_APP_KEY, else VOLC_ASR_APP_KEY),
access_key (env VOLC_TTS_ACCESS_KEY, else VOLC_ASR_ACCESS_KEY), resource_id
("volc.service_type.10029"), voice ("zh_female_shuangkuaisisi_moon_bigtts"),
sample_rate (24000), speech_rate (-50..100, unset = server default 0),
loudness_rate (-50..100, unset = 0), emotion (only for multi-emotion voices,
unset), url (the endpoint above).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import struct
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from vak.upstream.providers._util import ProviderConfigError, cfg
from vak.upstream.providers.base import TTSChunk

log = logging.getLogger(__name__)

_NAME = "volcengine_tts"
DEFAULT_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
DEFAULT_RESOURCE_ID = "volc.service_type.10029"
DEFAULT_VOICE = "zh_female_shuangkuaisisi_moon_bigtts"

VERSION = 0b0001
MSG_FULL_CLIENT = 0b0001
MSG_FULL_SERVER = 0b1001
MSG_AUDIO_ONLY = 0b1011
MSG_ERROR = 0b1111
FLAG_SEQ_MASK = 0b0011
FLAG_EVENT = 0b0100
SER_NONE = 0b0000
SER_JSON = 0b0001
COMP_NONE = 0b0000
COMP_GZIP = 0b0001

EV_START_CONNECTION = 1
EV_FINISH_CONNECTION = 2
EV_CONNECTION_STARTED = 50
EV_CONNECTION_FAILED = 51
EV_CONNECTION_FINISHED = 52
EV_START_SESSION = 100
EV_CANCEL_SESSION = 101
EV_FINISH_SESSION = 102
EV_SESSION_STARTED = 150
EV_SESSION_CANCELED = 151
EV_SESSION_FINISHED = 152
EV_SESSION_FAILED = 153
EV_TASK_REQUEST = 200
EV_TTS_SENTENCE_START = 350
EV_TTS_SENTENCE_END = 351
EV_TTS_RESPONSE = 352

_CONNECTION_EVENTS = {EV_START_CONNECTION, EV_FINISH_CONNECTION, EV_CONNECTION_STARTED, EV_CONNECTION_FAILED, EV_CONNECTION_FINISHED}
_SESSION_END = {EV_SESSION_CANCELED, EV_SESSION_FINISHED, EV_SESSION_FAILED}

_HANDSHAKE_TIMEOUT_S = 10.0  # ConnectionStarted / SessionStarted (TEN waits 10 s)
_FRAME_TIMEOUT_S = 30.0  # longest silence between server frames during a session
_DRAIN_TIMEOUT_S = 5.0  # CancelSession -> terminal session event
_MAX_IDLE = 2


@dataclass(slots=True)
class Frame:
    msg_type: int
    flags: int = FLAG_EVENT
    event: int = 0
    id: str | None = None  # session id, or connect id on connection events
    payload: bytes = b""
    code: int = 0  # MSG_ERROR only
    serialization: int = SER_JSON

    def json(self) -> dict[str, Any]:
        if not self.payload:
            return {}
        try:
            v = json.loads(self.payload)
        except (ValueError, UnicodeDecodeError):
            return {"raw": self.payload.decode("utf-8", "replace")}
        return v if isinstance(v, dict) else {"value": v}

    def message(self) -> str:
        """Human-readable server message for errors / failures."""
        d = self.json()
        return str(d.get("message") or d.get("error") or d.get("raw") or d or f"event {self.event}")


def encode_frame(
    event: int,
    payload: bytes | dict[str, Any] = b"{}",
    id: str | None = None,
    *,
    msg_type: int = MSG_FULL_CLIENT,
    serialization: int = SER_JSON,
) -> bytes:
    """Build one uncompressed event frame (client direction; also used by tests
    to build server frames)."""
    if isinstance(payload, dict):
        payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    out = bytearray([(VERSION << 4) | 1, (msg_type << 4) | FLAG_EVENT, (serialization << 4) | COMP_NONE, 0])
    out += struct.pack(">i", event)
    if id is not None:
        b = id.encode("utf-8")
        out += struct.pack(">I", len(b)) + b
    out += struct.pack(">I", len(payload)) + payload
    return bytes(out)


def encode_error(code: int, message: str) -> bytes:
    body = message.encode("utf-8")
    return bytes([(VERSION << 4) | 1, (MSG_ERROR << 4), (SER_JSON << 4), 0]) + struct.pack(">II", code, len(body)) + body


def _fields(data: bytes, offset: int) -> list[bytes]:
    out: list[bytes] = []
    while offset + 4 <= len(data):
        (n,) = struct.unpack_from(">I", data, offset)
        offset += 4
        if offset + n > len(data):
            raise ValueError("truncated field")
        out.append(data[offset : offset + n])
        offset += n
    return out


def decode_frame(data: bytes) -> Frame:
    """Decode one frame (either direction)."""
    if len(data) < 4:
        raise ValueError(f"frame too short: {len(data)} bytes")
    header_len = (data[0] & 0x0F) * 4
    msg_type, flags = data[1] >> 4, data[1] & 0x0F
    serialization, compression = data[2] >> 4, data[2] & 0x0F
    off = header_len
    frame = Frame(msg_type=msg_type, flags=flags, serialization=serialization)
    if msg_type == MSG_ERROR:
        frame.code, n = struct.unpack_from(">II", data, off)
        frame.payload = data[off + 8 : off + 8 + n]
        return frame
    if flags & FLAG_SEQ_MASK in (0b0001, 0b0011):
        off += 4  # sequence number; not used by TTS v3
    if flags & FLAG_EVENT:
        (frame.event,) = struct.unpack_from(">i", data, off)
        off += 4
    fields = _fields(data, off)
    if not (flags & FLAG_EVENT):
        frame.payload = fields[0] if fields else b""
    elif frame.event in _CONNECTION_EVENTS:
        # connect id is optional (xiaozhi parses ConnectionFailed without one)
        if len(fields) >= 2:
            frame.id = fields[0].decode("utf-8", "replace")
        frame.payload = fields[-1] if fields else b""
    else:
        if fields:
            frame.id = fields[0].decode("utf-8", "replace")
        frame.payload = fields[1] if len(fields) > 1 else b""
    if compression == COMP_GZIP and frame.payload:
        frame.payload = gzip.decompress(frame.payload)
    return frame


@dataclass(eq=False, slots=True)
class _Conn:
    ws: Any


@dataclass(slots=True)
class _Active:
    conn: _Conn
    session_id: str
    started: bool = False  # SessionStarted seen: CancelSession may be sent from now on
    cancel_sent: bool = False


def _env(*names: str) -> str | None:
    for n in names:
        if v := os.environ.get(n):
            return v
    return None


class VolcengineStreamingTTS:
    supports_text_stream = True

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.app_key = config.get("app_key") or _env("VOLC_TTS_APP_KEY", "VOLC_ASR_APP_KEY")
        self.access_key = config.get("access_key") or _env("VOLC_TTS_ACCESS_KEY", "VOLC_ASR_ACCESS_KEY")
        if not self.app_key:
            raise ProviderConfigError(f"{_NAME}: set config 'app_key' or environment variable VOLC_TTS_APP_KEY")
        if not self.access_key:
            raise ProviderConfigError(f"{_NAME}: set config 'access_key' or environment variable VOLC_TTS_ACCESS_KEY")
        self.resource_id: str = str(cfg(config, "resource_id", DEFAULT_RESOURCE_ID))
        self.url: str = str(cfg(config, "url", DEFAULT_URL))
        self.voice: str = str(cfg(config, "voice", DEFAULT_VOICE))
        self.sample_rate: int = int(cfg(config, "sample_rate", 24000))
        self._idle: list[_Conn] = []
        self._conns: set[_Conn] = set()
        self._active: dict[str, _Active] = {}
        self._stopped: set[str] = set()
        self._bg: set[asyncio.Task[None]] = set()

    # -- request building ------------------------------------------------------

    def headers(self) -> dict[str, str]:
        return {
            "X-Api-App-Key": str(self.app_key),
            "X-Api-Access-Key": str(self.access_key),
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

    def request(self, event: int, text: str | None = None) -> dict[str, Any]:
        audio: dict[str, Any] = {"format": "pcm", "sample_rate": self.sample_rate}
        for key in ("speech_rate", "loudness_rate"):
            if self.config.get(key) is not None:
                audio[key] = int(self.config[key])
        if self.config.get("emotion"):
            audio["emotion"] = str(self.config["emotion"])
        params: dict[str, Any] = {"speaker": self.voice, "audio_params": audio}
        if text is not None:
            params["text"] = text
        return {"user": {"uid": "vak"}, "event": event, "namespace": "BidirectionalTTS", "req_params": params}

    # -- connection pool -------------------------------------------------------

    async def _ws_connect(self, url: str, headers: dict[str, str]) -> Any:
        from websockets.asyncio.client import connect

        ws = await connect(url, additional_headers=headers, max_size=None)
        response = getattr(ws, "response", None)
        if response is not None:
            log.info("volcengine tts: connected %s logid=%s", url, response.headers.get("X-Tt-Logid"))
        return ws

    @staticmethod
    def _is_open(conn: _Conn) -> bool:
        state = getattr(conn.ws, "state", None)
        if state is not None:
            return getattr(state, "name", "") == "OPEN"
        return not getattr(conn.ws, "closed", False)

    async def _recv(self, conn: _Conn, timeout: float) -> Frame:
        while True:
            raw = await asyncio.wait_for(conn.ws.recv(), timeout)
            if isinstance(raw, bytes | bytearray):
                return decode_frame(bytes(raw))
            log.debug("volcengine tts: ignoring text message %r", str(raw)[:200])

    async def _open(self) -> _Conn:
        ws = await self._ws_connect(self.url, self.headers())
        conn = _Conn(ws)
        self._conns.add(conn)
        try:
            await ws.send(encode_frame(EV_START_CONNECTION))
            async with asyncio.timeout(_HANDSHAKE_TIMEOUT_S):
                while True:
                    f = await self._recv(conn, _HANDSHAKE_TIMEOUT_S)
                    if f.msg_type == MSG_ERROR:
                        raise RuntimeError(f"{_NAME}: server error {f.code}: {f.message()}")
                    if f.event == EV_CONNECTION_STARTED:
                        return conn
                    if f.event == EV_CONNECTION_FAILED:
                        raise RuntimeError(f"{_NAME}: connection failed: {f.message()}")
                    log.debug("volcengine tts: unexpected event %s before ConnectionStarted", f.event)
        except BaseException:
            await self._discard(conn)
            raise

    async def _acquire(self) -> _Conn:
        while self._idle:
            conn = self._idle.pop()
            if self._is_open(conn):
                return conn
            await self._discard(conn)
        return await self._open()

    async def _release(self, conn: _Conn) -> None:
        if self._is_open(conn) and len(self._idle) < _MAX_IDLE and conn in self._conns:
            self._idle.append(conn)
        else:
            await self._discard(conn)

    async def _discard(self, conn: _Conn) -> None:
        self._conns.discard(conn)
        if conn in self._idle:
            self._idle.remove(conn)
        try:
            if self._is_open(conn):
                await conn.ws.send(encode_frame(EV_FINISH_CONNECTION))
        except Exception as e:
            log.debug("volcengine tts: FinishConnection failed: %s", e)
        try:
            await conn.ws.close()
        except Exception as e:
            log.debug("volcengine tts: close failed: %s", e)

    async def warmup(self) -> None:
        """Open one connection ahead of the first sentence (errors are logged)."""
        if any(self._is_open(c) for c in self._idle):
            return
        try:
            conn = await self._open()
        except Exception as e:
            log.warning("volcengine tts: warmup failed: %s", e)
            return
        await self._release(conn)

    # -- synthesis -------------------------------------------------------------

    async def open_text_stream(self, request_id: str) -> VolcengineTextStream:
        """Take a connection and start a session (StartSession, wait for
        SessionStarted); text is pushed afterwards. On failure or cancellation
        the connection is dropped."""
        conn = await self._acquire()
        sid = uuid.uuid4().hex
        active = _Active(conn, sid)
        self._active[request_id] = active
        self._stopped.discard(request_id)
        try:
            await conn.ws.send(encode_frame(EV_START_SESSION, self.request(EV_START_SESSION), sid))
            await self._await_session_started(conn, sid)
        except BaseException:
            self._active.pop(request_id, None)
            self._stopped.discard(request_id)
            self._finish_use(active, "broken")
            raise
        active.started = True
        return VolcengineTextStream(self, request_id, active)

    async def synthesize(self, text: str, request_id: str) -> AsyncIterator[TTSChunk]:
        """One sentence = one session: a text stream with a single push."""
        stream = await self.open_text_stream(request_id)
        try:
            if request_id in self._stopped:
                return
            await stream.push(text)
            await stream.end()
            async for chunk in stream.audio():
                yield chunk
        finally:
            stream.close()

    async def _await_session_started(self, conn: _Conn, sid: str) -> None:
        async with asyncio.timeout(_HANDSHAKE_TIMEOUT_S):
            while True:
                f = await self._recv(conn, _HANDSHAKE_TIMEOUT_S)
                if f.msg_type == MSG_ERROR:
                    raise RuntimeError(f"{_NAME}: server error {f.code}: {f.message()}")
                if f.event == EV_SESSION_STARTED and f.id == sid:
                    return
                if f.event == EV_SESSION_FAILED and f.id == sid:
                    raise RuntimeError(f"{_NAME}: session failed: {f.message()}")
                if f.event in _CONNECTION_EVENTS:
                    raise RuntimeError(f"{_NAME}: connection ended (event {f.event}): {f.message()}")
                log.debug("volcengine tts: unexpected event %s before SessionStarted", f.event)

    def _finish_use(self, active: _Active, state: str) -> None:
        """Hand the connection back (pool, close, or cancel-and-drain in the
        background). Synchronous so it is safe in a cancelled generator."""
        if state == "clean":
            coro = self._release(active.conn)
        elif state == "broken":
            coro = self._discard(active.conn)
        else:
            coro = self._abandon(active)
        t = asyncio.get_running_loop().create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _abandon(self, active: _Active) -> None:
        """Cancel an unfinished session and pool the connection only once the
        server confirms the session ended."""
        conn = active.conn
        try:
            if not active.cancel_sent:
                active.cancel_sent = True
                await conn.ws.send(encode_frame(EV_CANCEL_SESSION, b"{}", active.session_id))
            async with asyncio.timeout(_DRAIN_TIMEOUT_S):
                while True:
                    f = await self._recv(conn, _DRAIN_TIMEOUT_S)
                    if f.msg_type == MSG_ERROR or f.event in _CONNECTION_EVENTS:
                        raise RuntimeError(f"event {f.event} code {f.code}: {f.message()}")
                    if f.event in _SESSION_END and f.id == active.session_id:
                        break
        except (Exception, asyncio.CancelledError) as e:
            log.info("volcengine tts: dropping connection after cancel: %r", e)
            await self._discard(conn)
            return
        await self._release(conn)

    async def flush(self, request_id: str) -> None:
        active = self._active.get(request_id)
        if active is None:
            return
        self._stopped.add(request_id)
        if active.started and not active.cancel_sent:
            active.cancel_sent = True
            try:
                await active.conn.ws.send(encode_frame(EV_CANCEL_SESSION, b"{}", active.session_id))
            except Exception as e:
                log.debug("volcengine tts: CancelSession send failed: %s", e)

    async def close(self) -> None:
        for t in list(self._bg):
            t.cancel()
        if self._bg:
            await asyncio.gather(*self._bg, return_exceptions=True)
        for conn in list(self._conns):
            await self._discard(conn)
        self._idle.clear()


class VolcengineTextStream:
    """One session fed incrementally (`TTSTextStream`): every `push` is one
    TaskRequest, `end` is FinishSession. The server segments sentences itself
    and announces each with TTSSentenceStart (its `text`); that text is put on
    the sentence's first audio chunk as `TTSChunk.sentence`. `seed-tts-2.0`
    sends no TTSSentenceStart, so its chunks carry no marks."""

    def __init__(self, tts: VolcengineStreamingTTS, request_id: str, active: _Active) -> None:
        self._tts = tts
        self.request_id = request_id
        self._active = active
        self._state = "active"  # active: session may be open; clean: session ended; broken: drop connection
        self._ended = False
        self._closed = False

    @property
    def _stopped(self) -> bool:
        return self.request_id in self._tts._stopped

    async def _send(self, frame: bytes) -> None:
        try:
            await self._active.conn.ws.send(frame)
        except BaseException:
            self._state = "broken"
            raise

    async def push(self, text: str) -> None:
        if not text or self._ended or self._stopped or self._closed:
            return
        sid = self._active.session_id
        await self._send(encode_frame(EV_TASK_REQUEST, self._tts.request(EV_TASK_REQUEST, text), sid))

    async def end(self) -> None:
        if self._ended or self._stopped or self._closed:
            return
        self._ended = True
        await self._send(encode_frame(EV_FINISH_SESSION, b"{}", self._active.session_id))

    async def audio(self) -> AsyncIterator[TTSChunk]:
        tts, conn, sid = self._tts, self._active.conn, self._active.session_id
        carry = b""
        mark: str | None = None
        try:
            while not self._stopped and not self._closed:
                f = await tts._recv(conn, _FRAME_TIMEOUT_S)
                if self._stopped:
                    return
                if f.msg_type == MSG_ERROR:
                    raise RuntimeError(f"{_NAME}: server error {f.code}: {f.message()}")
                if f.event in _CONNECTION_EVENTS:
                    raise RuntimeError(f"{_NAME}: connection ended (event {f.event}): {f.message()}")
                if f.id is not None and f.id != sid:
                    log.debug("volcengine tts: dropping event %s of session %s", f.event, f.id)
                    continue
                if f.event == EV_TTS_RESPONSE and f.msg_type == MSG_AUDIO_ONLY:
                    data = carry + f.payload
                    cut = len(data) - len(data) % 2
                    carry = data[cut:]
                    if cut:
                        yield TTSChunk(pcm16=data[:cut], sample_rate=tts.sample_rate, sentence=mark)
                        mark = None
                elif f.event == EV_TTS_SENTENCE_START:
                    mark = str(f.json().get("text") or "")
                elif f.event == EV_SESSION_FINISHED:
                    self._state = "clean"
                    yield TTSChunk(pcm16=b"", sample_rate=tts.sample_rate, is_last=True)
                    return
                elif f.event == EV_SESSION_FAILED:
                    self._state = "clean"
                    raise RuntimeError(f"{_NAME}: session failed: {f.message()}")
                elif f.event == EV_SESSION_CANCELED:
                    self._state = "clean"
                    return
                else:
                    log.debug("volcengine tts: ignoring event %s", f.event)
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except BaseException:
            if self._state == "active":
                self._state = "broken"
            raise

    def close(self) -> None:
        """Release the connection: pool it after a clean end, drop it when
        broken, otherwise cancel the session and drain in the background."""
        if self._closed:
            return
        self._closed = True
        self._tts._active.pop(self.request_id, None)
        self._tts._stopped.discard(self.request_id)
        self._tts._finish_use(self._active, self._state)
