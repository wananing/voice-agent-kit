"""Volcengine bidirectional TTS v3: framing, session sequence, pooling, flush.
All against a scripted fake WebSocket; no network."""

from __future__ import annotations

import asyncio
import gzip
import json
import struct

import pytest

from vak.upstream.providers._util import ProviderConfigError
from vak.upstream.providers.tts import volcengine as v
from vak.upstream.providers.tts.volcengine import (
    VolcengineStreamingTTS,
    decode_frame,
    encode_error,
    encode_frame,
)

from .fakes import FakeWS

CREDS = {"app_key": "app", "access_key": "key"}


def server(event: int, payload: bytes | dict = b"{}", id: str | None = None) -> bytes:
    return encode_frame(event, payload, id, msg_type=v.MSG_FULL_SERVER)


def audio(sid: str, pcm: bytes) -> bytes:
    return encode_frame(v.EV_TTS_RESPONSE, pcm, sid, msg_type=v.MSG_AUDIO_ONLY, serialization=v.SER_NONE)


class ServerWS(FakeWS):
    """Answers like the TTS server: ConnectionStarted, SessionStarted, and on
    FinishSession the scripted audio + SessionFinished (unless `hold`)."""

    def __init__(self, pcm: list[bytes] | None = None, *, hold: bool = False, confirm_cancel: bool = True) -> None:
        super().__init__()
        self.pcm = [b"\x01\x00" * 4, b"\x02\x00" * 4] if pcm is None else pcm
        self.hold = hold
        self.confirm_cancel = confirm_cancel
        self.sid: str | None = None

    @property
    def frames(self) -> list[v.Frame]:
        return [decode_frame(b) for b in self.sent]

    @property
    def events(self) -> list[int]:
        return [f.event for f in self.frames]

    def finish(self) -> None:
        self.feed(server(v.EV_SESSION_FINISHED, {"usage": {"text_words": 2}}, self.sid))

    async def send(self, data) -> None:
        if self.closed:
            raise ConnectionError("closed")
        await super().send(data)
        f = decode_frame(data)
        if f.event == v.EV_START_CONNECTION:
            self.feed(server(v.EV_CONNECTION_STARTED, b"{}", "conn-1"))
        elif f.event == v.EV_START_SESSION:
            self.sid = f.id
            self.feed(server(v.EV_SESSION_STARTED, b"{}", f.id))
        elif f.event == v.EV_FINISH_SESSION:
            self.feed(server(v.EV_TTS_SENTENCE_START, {"text": "x"}, f.id))
            self.feed(*(audio(f.id, p) for p in self.pcm))
            if not self.hold:
                self.feed(server(v.EV_TTS_SENTENCE_END, {"text": "x"}, f.id))
                self.finish()
        elif f.event == v.EV_CANCEL_SESSION and self.confirm_cancel:
            self.feed(server(v.EV_SESSION_CANCELED, b"{}", f.id))


def make(monkeypatch, *wss: FakeWS, **config) -> tuple[VolcengineStreamingTTS, list[dict]]:
    tts = VolcengineStreamingTTS({**CREDS, **config})
    pending = list(wss)
    seen: list[dict] = []

    async def fake_connect(url, headers):
        seen.append({"url": url, "headers": headers})
        return pending.pop(0)

    monkeypatch.setattr(tts, "_ws_connect", fake_connect)
    return tts, seen


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


# --- framing -----------------------------------------------------------------


@pytest.mark.parametrize(
    "event, sid",
    [
        (v.EV_START_CONNECTION, None),
        (v.EV_FINISH_CONNECTION, None),
        (v.EV_START_SESSION, "s1"),
        (v.EV_TASK_REQUEST, "s1"),
        (v.EV_FINISH_SESSION, "s1"),
        (v.EV_CANCEL_SESSION, "s1"),
    ],
)
def test_client_frame_round_trip(event, sid):
    raw = encode_frame(event, {"event": event, "req_params": {"text": "你好"}}, sid)
    assert raw[:4] == bytes([0x11, 0x14, 0x10, 0x00])  # v1/4-byte header, full client + event, JSON, no gzip
    f = decode_frame(raw)
    assert (f.msg_type, f.event, f.id) == (v.MSG_FULL_CLIENT, event, sid)
    assert f.json()["req_params"]["text"] == "你好"


@pytest.mark.parametrize(
    "event",
    [v.EV_SESSION_STARTED, v.EV_SESSION_FINISHED, v.EV_SESSION_FAILED, v.EV_SESSION_CANCELED, v.EV_TTS_SENTENCE_START, v.EV_TTS_SENTENCE_END],
)
def test_server_session_frame_round_trip(event):
    f = decode_frame(server(event, {"message": "m"}, "sess"))
    assert (f.msg_type, f.event, f.id, f.message()) == (v.MSG_FULL_SERVER, event, "sess", "m")


def test_audio_and_connection_frames():
    f = decode_frame(audio("sess", b"\x00\x01\x02"))
    assert (f.msg_type, f.event, f.id, f.payload) == (v.MSG_AUDIO_ONLY, v.EV_TTS_RESPONSE, "sess", b"\x00\x01\x02")
    f = decode_frame(server(v.EV_CONNECTION_STARTED, b"{}", "conn-9"))
    assert (f.event, f.id) == (v.EV_CONNECTION_STARTED, "conn-9")
    # xiaozhi's layout: ConnectionFailed with only the error JSON
    f = decode_frame(server(v.EV_CONNECTION_FAILED, {"error": "auth"}))
    assert (f.event, f.id, f.message()) == (v.EV_CONNECTION_FAILED, None, "auth")


def test_error_and_gzip_frames():
    f = decode_frame(encode_error(45000001, "bad request"))
    assert (f.msg_type, f.code, f.message()) == (v.MSG_ERROR, 45000001, "bad request")
    body = gzip.compress(b'{"message": "zipped"}')
    raw = bytes([0x11, 0x94, 0x11, 0]) + struct.pack(">i", v.EV_SESSION_FAILED)
    raw += struct.pack(">I", 1) + b"s" + struct.pack(">I", len(body)) + body
    assert decode_frame(raw).message() == "zipped"


# --- session -----------------------------------------------------------------


async def test_wire_sequence_and_payloads(monkeypatch):
    ws = ServerWS()
    tts, seen = make(monkeypatch, ws, speech_rate=10, loudness_rate=-5)
    [c async for c in tts.synthesize("你好世界", "r1")]
    assert seen[0]["url"] == v.DEFAULT_URL
    h = seen[0]["headers"]
    assert (h["X-Api-App-Key"], h["X-Api-Access-Key"], h["X-Api-Resource-Id"]) == ("app", "key", "volc.service_type.10029")
    assert h["X-Api-Connect-Id"]
    frames = ws.frames
    assert [f.event for f in frames] == [v.EV_START_CONNECTION, v.EV_START_SESSION, v.EV_TASK_REQUEST, v.EV_FINISH_SESSION]
    assert frames[0].id is None and frames[1].id == frames[2].id == frames[3].id
    start, task = frames[1].json(), frames[2].json()
    assert start["namespace"] == "BidirectionalTTS" and start["event"] == v.EV_START_SESSION
    assert start["req_params"]["speaker"] == "zh_female_shuangkuaisisi_moon_bigtts"
    assert start["req_params"]["audio_params"] == {"format": "pcm", "sample_rate": 24000, "speech_rate": 10, "loudness_rate": -5}
    assert "text" not in start["req_params"]
    assert task["req_params"]["text"] == "你好世界" and task["event"] == v.EV_TASK_REQUEST
    assert frames[3].json() == {}


async def test_audio_frames_to_chunks(monkeypatch):
    ws = ServerWS([b"\x01\x02\x03", b"\x04\x05\x06\x07\x08", b"\x09\x0a"])  # odd sizes
    tts, _ = make(monkeypatch, ws, sample_rate=16000)
    chunks = [c async for c in tts.synthesize("x", "r")]
    assert [c.pcm16 for c in chunks] == [b"\x01\x02", b"\x03\x04\x05\x06\x07\x08", b"\x09\x0a", b""]
    assert [c.is_last for c in chunks] == [False, False, False, True]
    assert all(c.sample_rate == 16000 for c in chunks)
    assert json.loads(ws.frames[1].payload)["req_params"]["audio_params"]["sample_rate"] == 16000


async def test_session_failed_raises(monkeypatch):
    ws = ServerWS(hold=True)
    tts, _ = make(monkeypatch, ws)
    gen = tts.synthesize("x", "r")
    await gen.__anext__()
    ws.feed(server(v.EV_SESSION_FAILED, {"message": "quota exceeded", "status_code": 45000292}, ws.sid))
    with pytest.raises(RuntimeError, match="quota exceeded"):
        [c async for c in gen]
    await settle()
    assert tts._idle and not ws.closed  # session ended: connection is clean


async def test_server_error_frame_drops_connection(monkeypatch):
    ws = ServerWS(hold=True)
    tts, _ = make(monkeypatch, ws)
    gen = tts.synthesize("x", "r")
    await gen.__anext__()
    ws.feed(encode_error(55000000, "internal"))
    with pytest.raises(RuntimeError, match="internal"):
        [c async for c in gen]
    await settle()
    assert ws.closed and not tts._idle


async def test_voice_change_used_in_next_session(monkeypatch):
    ws = ServerWS()
    tts, _ = make(monkeypatch, ws, voice="zh_female_a")
    [c async for c in tts.synthesize("一", "r1")]
    tts.voice = "zh_male_b"
    await settle()
    [c async for c in tts.synthesize("二", "r2")]
    starts = [f.json() for f in ws.frames if f.event == v.EV_START_SESSION]
    assert [s["req_params"]["speaker"] for s in starts] == ["zh_female_a", "zh_male_b"]


# --- pooling -----------------------------------------------------------------


async def test_connection_reused_across_sentences(monkeypatch):
    ws = ServerWS()
    tts, seen = make(monkeypatch, ws)
    [c async for c in tts.synthesize("一", "r1")]
    await settle()
    [c async for c in tts.synthesize("二", "r2")]
    assert len(seen) == 1
    assert ws.events.count(v.EV_START_CONNECTION) == 1 and ws.events.count(v.EV_START_SESSION) == 2
    sids = {f.id for f in ws.frames if f.event == v.EV_START_SESSION}
    assert len(sids) == 2


async def test_concurrent_sentences_use_two_connections(monkeypatch):
    a, b = ServerWS(hold=True), ServerWS(hold=True)
    tts, seen = make(monkeypatch, a, b)
    g1, g2 = tts.synthesize("一", "r1"), tts.synthesize("二", "r2")
    c1, c2 = await g1.__anext__(), await g2.__anext__()
    assert c1.pcm16 and c2.pcm16 and len(seen) == 2
    a.finish()
    b.finish()
    assert [c async for c in g1][-1].is_last and [c async for c in g2][-1].is_last
    await settle()
    assert len(tts._idle) == 2 and not a.closed and not b.closed


async def test_extra_idle_connections_are_closed(monkeypatch):
    wss = [ServerWS(hold=True) for _ in range(3)]
    tts, _ = make(monkeypatch, *wss)
    gens = [tts.synthesize("x", f"r{i}") for i in range(3)]
    for g in gens:
        await g.__anext__()
    for ws in wss:
        ws.finish()
    for g in gens:
        [c async for c in g]
    await settle()
    assert len(tts._idle) == 2 and sum(ws.closed for ws in wss) == 1


async def test_warmup_opens_connection_used_by_first_sentence(monkeypatch):
    ws = ServerWS()
    tts, seen = make(monkeypatch, ws)
    await tts.warmup()
    assert len(seen) == 1 and ws.events == [v.EV_START_CONNECTION] and len(tts._idle) == 1
    await tts.warmup()  # already warm: no second connection
    [c async for c in tts.synthesize("x", "r")]
    assert len(seen) == 1


async def test_dead_pooled_connection_is_replaced(monkeypatch):
    a, b = ServerWS(), ServerWS()
    tts, seen = make(monkeypatch, a, b)
    await tts.warmup()
    a.closed = True  # server dropped the idle connection
    [c async for c in tts.synthesize("x", "r")]
    assert len(seen) == 2 and v.EV_START_SESSION in b.events and v.EV_START_SESSION not in a.events


async def test_send_failure_raises_without_retry(monkeypatch):
    a = ServerWS()
    tts, seen = make(monkeypatch, a)
    await tts.warmup()

    async def broken_send(data):
        raise ConnectionError("reset by peer")

    monkeypatch.setattr(a, "send", broken_send)
    with pytest.raises(ConnectionError):
        [c async for c in tts.synthesize("x", "r")]
    await settle()
    assert len(seen) == 1 and not tts._idle and a.closed


async def test_connection_failed_on_open(monkeypatch):
    ws = FakeWS([server(v.EV_CONNECTION_FAILED, {"error": "invalid access key"})])
    tts, _ = make(monkeypatch, ws)
    with pytest.raises(RuntimeError, match="invalid access key"):
        [c async for c in tts.synthesize("x", "r")]
    assert ws.closed and not tts._conns


async def test_close_closes_pool(monkeypatch):
    a = ServerWS()
    tts, _ = make(monkeypatch, a)
    await tts.warmup()
    await tts.close()
    assert a.closed and not tts._idle and a.events[-1] == v.EV_FINISH_CONNECTION


# --- flush / early close -----------------------------------------------------


async def test_flush_mid_stream_cancels_and_returns_clean(monkeypatch):
    ws = ServerWS([b"\x01\x00" * 4] * 3, hold=True)
    tts, seen = make(monkeypatch, ws)
    got = []
    async for c in tts.synthesize("长句子", "r1"):
        got.append(c)
        if len(got) == 1:
            ws.confirm_cancel = False  # hold the confirmation to inspect the in-between state
            await tts.flush("r1")
            assert ws.events[-1] == v.EV_CANCEL_SESSION
    assert len(got) == 1 and not got[0].is_last
    await settle()
    assert not tts._idle and not ws.closed  # session not confirmed over: not pooled yet
    ws.feed(server(v.EV_SESSION_CANCELED, b"{}", ws.sid))
    await settle()
    assert len(tts._idle) == 1 and ws.events.count(v.EV_CANCEL_SESSION) == 1
    # the next sentence reuses the cleaned connection
    ws.hold = False
    chunks = [c async for c in tts.synthesize("下一句", "r2")]
    assert chunks[-1].is_last and len(seen) == 1


async def test_flush_then_task_cancel_like_pipeline(monkeypatch):
    ws = ServerWS(hold=True)
    tts, _ = make(monkeypatch, ws)
    got: list = []
    first = asyncio.Event()

    async def consume():
        async for c in tts.synthesize("x", "r1"):
            got.append(c)
            first.set()

    task = asyncio.create_task(consume())
    await first.wait()
    await settle()
    await tts.flush("r1")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    n = len(got)
    ws.feed(audio(ws.sid, b"\x00\x00"))
    await settle()
    assert len(got) == n
    assert ws.events.count(v.EV_CANCEL_SESSION) == 1 and len(tts._idle) == 1


async def test_early_aclose_cancels_session(monkeypatch):
    ws = ServerWS(hold=True)
    tts, _ = make(monkeypatch, ws)
    gen = tts.synthesize("x", "r1")
    await gen.__anext__()
    await gen.aclose()
    await settle()
    assert ws.events[-1] == v.EV_CANCEL_SESSION and len(tts._idle) == 1


async def test_unconfirmed_cancel_closes_connection(monkeypatch):
    monkeypatch.setattr(v, "_DRAIN_TIMEOUT_S", 0.01)
    ws = ServerWS(hold=True, confirm_cancel=False)
    tts, _ = make(monkeypatch, ws)
    gen = tts.synthesize("x", "r1")
    await gen.__anext__()
    await tts.flush("r1")
    await gen.aclose()
    await asyncio.sleep(0.05)
    assert ws.closed and not tts._idle and not tts._conns


async def test_flush_unknown_request_is_noop(monkeypatch):
    tts, _ = make(monkeypatch)
    await tts.flush("nope")
    assert not tts._stopped


# --- config ------------------------------------------------------------------


def test_missing_credentials(monkeypatch):
    for k in ("VOLC_TTS_APP_KEY", "VOLC_TTS_ACCESS_KEY", "VOLC_ASR_APP_KEY", "VOLC_ASR_ACCESS_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ProviderConfigError, match="app_key"):
        VolcengineStreamingTTS({})
    with pytest.raises(ProviderConfigError, match="access_key"):
        VolcengineStreamingTTS({"app_key": "a"})


def test_env_credentials_fall_back_to_asr(monkeypatch):
    monkeypatch.delenv("VOLC_TTS_APP_KEY", raising=False)
    monkeypatch.setenv("VOLC_ASR_APP_KEY", "asr-app")
    monkeypatch.setenv("VOLC_TTS_ACCESS_KEY", "tts-key")
    monkeypatch.setenv("VOLC_ASR_ACCESS_KEY", "asr-key")
    tts = VolcengineStreamingTTS({"resource_id": "seed-tts-2.0", "emotion": "happy"})
    assert (tts.app_key, tts.access_key, tts.resource_id) == ("asr-app", "tts-key", "seed-tts-2.0")
    assert tts.supports_text_stream is True and tts.sample_rate == 24000
    assert tts.request(v.EV_START_SESSION)["req_params"]["audio_params"]["emotion"] == "happy"


# --- text stream (one session per reply, incremental text) -------------------


async def test_text_stream_one_task_request_per_push_and_sentence_marks(monkeypatch):
    ws = ServerWS([b"\x01\x00" * 4, b"\x02\x00" * 4])
    tts, _ = make(monkeypatch, ws)
    st = await tts.open_text_stream("r1")
    assert ws.events == [v.EV_START_CONNECTION, v.EV_START_SESSION]  # session open before any text
    for piece in ["你好", "，世", "", "界。"]:
        await st.push(piece)
    await st.end()
    await st.end()  # idempotent
    chunks = [c async for c in st.audio()]
    st.close()
    frames = ws.frames
    assert [f.event for f in frames] == [v.EV_START_CONNECTION, v.EV_START_SESSION] + [v.EV_TASK_REQUEST] * 3 + [v.EV_FINISH_SESSION]
    assert [f.json()["req_params"]["text"] for f in frames[2:5]] == ["你好", "，世", "界。"]
    assert len({f.id for f in frames[1:]}) == 1
    assert [c.sentence for c in chunks] == ["x", None, None]  # TTSSentenceStart text on the sentence's first chunk
    assert chunks[-1].is_last
    await settle()
    assert len(tts._idle) == 1  # clean end: pooled


async def test_text_stream_without_sentence_events_has_no_marks(monkeypatch):
    class Seed2(ServerWS):  # seed-tts-2.0: no TTSSentenceStart / End
        def feed(self, *frames):
            super().feed(*(f for f in frames if decode_frame(f).event not in (v.EV_TTS_SENTENCE_START, v.EV_TTS_SENTENCE_END)))

    tts, _ = make(monkeypatch, Seed2())
    st = await tts.open_text_stream("r1")
    await st.push("你好。")
    await st.end()
    chunks = [c async for c in st.audio()]
    assert chunks and all(c.sentence is None for c in chunks)


async def test_text_stream_flush_while_pushing_cancels_and_drains(monkeypatch):
    ws = ServerWS()
    tts, _ = make(monkeypatch, ws)
    st = await tts.open_text_stream("r1")
    await st.push("还没说完")
    await tts.flush("r1")  # session started: CancelSession goes out before FinishSession
    assert ws.events[-1] == v.EV_CANCEL_SESSION
    await st.push("后面的字")  # dropped after flush
    await st.end()
    assert [c async for c in st.audio()] == []
    st.close()
    await settle()
    assert ws.events.count(v.EV_TASK_REQUEST) == 1 and v.EV_FINISH_SESSION not in ws.events
    assert len(tts._idle) == 1 and ws.events.count(v.EV_CANCEL_SESSION) == 1


async def test_text_stream_close_before_end_cancels_session(monkeypatch):
    ws = ServerWS()
    tts, _ = make(monkeypatch, ws)
    st = await tts.open_text_stream("r1")
    await st.push("半句")
    st.close()
    st.close()  # idempotent
    await settle()
    assert ws.events[-1] == v.EV_CANCEL_SESSION and len(tts._idle) == 1


async def test_text_stream_open_cancelled_drops_connection(monkeypatch):
    class Silent(ServerWS):  # never answers StartSession
        async def send(self, data):
            if decode_frame(data).event == v.EV_START_SESSION:
                self.sent.append(data)
                return
            await super().send(data)

    ws = Silent()
    tts, _ = make(monkeypatch, ws)
    task = asyncio.create_task(tts.open_text_stream("r1"))
    await settle()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await settle()
    assert ws.closed and not tts._idle and "r1" not in tts._active
