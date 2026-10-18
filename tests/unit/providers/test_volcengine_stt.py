"""Volcengine SAUC v3 framing, request JSON, wire sequence and result mapping (no network)."""

from __future__ import annotations

import asyncio
import gzip
import json
import struct

import pytest

from vak.upstream.providers._util import ProviderConfigError
from vak.upstream.providers.stt import volcengine as v
from vak.upstream.providers.stt.volcengine import VolcengineStreamingSTT, encode_frame, parse_frame

from .fakes import FakeWS

CREDS = {"app_key": "app", "access_key": "tok"}


def server_frame(payload: dict, seq: int, last: bool = False) -> bytes:
    flags = v.FLAG_NEG_SEQ if last else v.FLAG_POS_SEQ
    return encode_frame(v.MSG_FULL_SERVER, flags, v.SER_JSON, json.dumps(payload).encode(), seq=-seq if last else seq)


def error_frame(code: int, msg: str) -> bytes:
    body = msg.encode()
    return bytes([0x11, v.MSG_ERROR << 4, 0x10, 0]) + struct.pack(">iI", code, len(body)) + body


def utts(*items: tuple[str, int, int, bool]) -> dict:
    return {
        "audio_info": {"duration": 1000},
        "result": {
            "text": "".join(i[0] for i in items),
            "utterances": [{"text": t, "start_time": s, "end_time": e, "definite": d} for t, s, e, d in items],
        },
    }


async def drain(stt: VolcengineStreamingSTT) -> list:
    return [t async for t in stt.results()]


def connected(monkeypatch, stt: VolcengineStreamingSTT, ws: FakeWS) -> dict:
    seen: dict = {}

    async def fake_connect(url, headers):
        seen["url"], seen["headers"] = url, headers
        return ws

    monkeypatch.setattr(stt, "_ws_connect", fake_connect)
    return seen


# --- framing -----------------------------------------------------------------


def test_server_frame_round_trip():
    payload = utts(("你好", 0, 500, True))
    raw = server_frame(payload, 3)
    assert raw[0] == 0x11 and raw[1] == 0x91 and raw[2] == 0x11 and raw[3] == 0
    f = parse_frame(raw)
    assert (f.msg_type, f.seq, f.is_last, f.payload) == (v.MSG_FULL_SERVER, 3, False, payload)
    last = parse_frame(server_frame(payload, 4, last=True))
    assert last.is_last and last.seq == -4


def test_error_frame_parse():
    f = parse_frame(error_frame(45000001, "invalid params"))
    assert (f.msg_type, f.code, f.payload) == (v.MSG_ERROR, 45000001, "invalid params")


def test_frame_with_event_field_is_skipped():
    body = gzip.compress(b'{"result": {"text": "x"}}')
    raw = bytes([0x11, (v.MSG_FULL_SERVER << 4) | 0b0101, 0x11, 0]) + struct.pack(">iiI", 2, 150, len(body)) + body
    f = parse_frame(raw)
    assert f.seq == 2 and f.payload == {"result": {"text": "x"}}


# --- config --------------------------------------------------------------------


def test_full_request_json():
    stt = VolcengineStreamingSTT({**CREDS, "hotwords": ["豆包", "火山"], "end_window_size": 300, "uid": "u1"})
    m = stt.full_request()
    assert m["user"] == {"uid": "u1"}
    assert m["audio"] == {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1}
    r = m["request"]
    assert r["model_name"] == "bigmodel" and r["result_type"] == "full" and r["show_utterances"] is True
    assert r["enable_itn"] is True and r["enable_punc"] is True and r["enable_ddc"] is False
    assert r["end_window_size"] == 300 and "enable_nonstream" not in r
    assert json.loads(r["corpus"]["context"]) == {"hotwords": [{"word": "豆包"}, {"word": "火山"}]}
    assert "app" not in m


def test_language_only_in_nostream_mode():
    assert "language" not in VolcengineStreamingSTT({**CREDS, "language": "en-US"}).full_request()["audio"]
    m = VolcengineStreamingSTT({**CREDS, "language": "en-US", "mode": "nostream"}).full_request()
    assert m["audio"]["language"] == "en-US"


def test_mode_to_url():
    base = "wss://openspeech.bytedance.com/api/v3/sauc/"
    assert VolcengineStreamingSTT(CREDS).url == base + "bigmodel_async"
    assert VolcengineStreamingSTT({**CREDS, "mode": "stream"}).url == base + "bigmodel"
    assert VolcengineStreamingSTT({**CREDS, "mode": "nostream"}).url == base + "bigmodel_nostream"
    assert VolcengineStreamingSTT({**CREDS, "url": "wss://proxy/x"}).url == "wss://proxy/x"
    with pytest.raises(ProviderConfigError):
        VolcengineStreamingSTT({**CREDS, "mode": "bogus"})


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("VOLC_ASR_APP_KEY", raising=False)
    monkeypatch.delenv("VOLC_ASR_ACCESS_KEY", raising=False)
    with pytest.raises(ProviderConfigError, match="VOLC_ASR_APP_KEY"):
        VolcengineStreamingSTT({"access_key": "tok"})
    with pytest.raises(ProviderConfigError, match="VOLC_ASR_ACCESS_KEY"):
        VolcengineStreamingSTT({"app_key": "app"})
    monkeypatch.setenv("VOLC_ASR_APP_KEY", "a")
    monkeypatch.setenv("VOLC_ASR_ACCESS_KEY", "b")
    stt = VolcengineStreamingSTT({})
    h = stt.headers()
    assert (h["X-Api-App-Key"], h["X-Api-Access-Key"], h["X-Api-Resource-Id"]) == ("a", "b", "volc.bigasr.sauc.duration")
    assert h["X-Api-Connect-Id"] != stt.headers()["X-Api-Connect-Id"]


# --- wire sequence ---------------------------------------------------------------


async def test_wire_sequence(monkeypatch):
    stt = VolcengineStreamingSTT(CREDS)
    ws = FakeWS()
    seen = connected(monkeypatch, stt, ws)
    await stt.start()
    assert seen["url"].endswith("/bigmodel_async") and seen["headers"]["X-Api-App-Key"] == "app"
    frame = b"\x01\x02" * 640  # 40 ms at 16 kHz
    for _ in range(3):
        await stt.feed(frame)  # 120 ms -> one 100 ms packet + 20 ms buffered
    await stt.finish()

    full, audio, tail = (parse_frame(f) for f in ws.sent)
    assert (full.msg_type, full.flags, full.seq) == (v.MSG_FULL_CLIENT, v.FLAG_POS_SEQ, 1)
    assert full.payload == stt.full_request()
    assert ws.sent[0][2] == 0x11  # JSON + gzip
    assert (audio.msg_type, audio.flags, audio.seq) == (v.MSG_AUDIO_ONLY, v.FLAG_POS_SEQ, 2)
    assert ws.sent[1][2] == 0x01  # raw + gzip
    assert audio.payload == (frame * 3)[:3200]
    assert (tail.msg_type, tail.flags, tail.seq, tail.is_last) == (v.MSG_AUDIO_ONLY, v.FLAG_NEG_SEQ, -3, True)
    assert tail.payload == (frame * 3)[3200:]
    await stt.cancel()
    await stt.cancel()  # idempotent
    assert ws.closed
    assert await asyncio.wait_for(drain(stt), 1) == []


async def test_partials_cumulative_and_single_final_after_finish(monkeypatch):
    stt = VolcengineStreamingSTT(CREDS)
    ws = FakeWS()
    connected(monkeypatch, stt, ws)
    await stt.start()
    ws.feed(
        server_frame({"result": {"text": ""}}, 1),  # ack of the full request
        server_frame(utts(("今天", 0, 400, False)), 2),
        server_frame(utts(("今天天气不错。", 0, 1200, True)), 3),  # definite before finish: no final
        server_frame(utts(("今天天气不错。", 0, 1200, True)), 4),  # duplicate: skipped
        server_frame(utts(("今天天气不错。", 0, 1200, True), ("我们", 1500, 1800, False)), 5),
        server_frame(utts(("今天天气不错。", 0, 1200, True), ("我们去公园。", 1500, 2600, True)), 6),
    )
    await asyncio.sleep(0.05)
    assert not stt._got_final
    await stt.finish()
    ws.feed(server_frame(utts(("今天天气不错。", 0, 1200, True), ("我们去公园。", 1500, 2600, True)), 7, last=True))
    out = await asyncio.wait_for(drain(stt), 2)
    assert [(t.text, t.final) for t in out] == [
        ("今天", False),
        ("今天天气不错。", False),
        ("今天天气不错。我们", False),
        ("今天天气不错。我们去公园。", False),
        ("今天天气不错。我们去公园。", True),
    ]
    final = out[-1]
    assert (final.segment, final.start_ms, final.end_ms) == (2, 0, 2600)


def test_single_result_type_accumulates_definites():
    stt = VolcengineStreamingSTT({**CREDS, "result_type": "single"})
    stt._finished = True
    assert stt.handle_payload(utts(("第一句。", 0, 900, True))) is False
    assert stt.handle_payload(utts(("第二", 1000, 1400, False))) is False
    assert stt.handle_payload(utts(("第二句。", 1000, 1800, True)), last=True) is True
    got = [stt._queue.get_nowait() for _ in range(stt._queue.qsize())]
    assert [(t.text, t.final) for t in got] == [
        ("第一句。", False),
        ("第一句。第二", False),
        ("第一句。第二句。", True),
    ]


def test_last_packet_before_finish_does_not_emit_final():
    stt = VolcengineStreamingSTT(CREDS)
    assert stt.handle_payload(utts(("你好", 0, 300, True)), last=True) is True
    got = [stt._queue.get_nowait() for _ in range(stt._queue.qsize())]
    assert [(t.text, t.final) for t in got] == [("你好", False)]


async def test_connection_close_after_finish_still_yields_final(monkeypatch):
    stt = VolcengineStreamingSTT(CREDS)
    ws = FakeWS()
    connected(monkeypatch, stt, ws)
    await stt.start()
    ws.feed(server_frame(utts(("一句。", 0, 500, True), ("部分", 600, 900, False)), 2))
    await stt.finish()
    ws.end()  # server closes without a last-packet response
    out = await asyncio.wait_for(drain(stt), 2)
    assert [(t.text, t.final) for t in out] == [("一句。部分", False), ("一句。部分", True)]


async def test_error_frame_ends_stream_with_fallback_final(monkeypatch, caplog):
    stt = VolcengineStreamingSTT(CREDS)
    ws = FakeWS()
    connected(monkeypatch, stt, ws)
    await stt.start()
    ws.feed(server_frame(utts(("你好", 0, 300, False)), 2))
    await stt.finish()
    ws.feed(error_frame(55000031, "server busy"))
    with caplog.at_level("WARNING"):
        out = await asyncio.wait_for(drain(stt), 2)
    assert [(t.text, t.final) for t in out] == [("你好", False), ("你好", True)]
    assert "55000031" in caplog.text
    assert ws.closed


async def test_error_frame_before_finish_ends_without_final(monkeypatch):
    stt = VolcengineStreamingSTT(CREDS)
    ws = FakeWS([error_frame(45000001, "bad request")])
    connected(monkeypatch, stt, ws)
    await stt.start()
    assert await asyncio.wait_for(drain(stt), 2) == []
