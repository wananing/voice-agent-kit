"""FunASR 2pass framing: init JSON, binary chunks, is_speaking=false, result mapping."""

from __future__ import annotations

import asyncio
import json

from vak.upstream.providers.stt.funasr import FunASRStreamingSTT

from .fakes import FakeWS


def test_init_message_matches_funasr_wss_client():
    stt = FunASRStreamingSTT({"url": "ws://x:10095", "hotwords": {"阿里巴巴": 20}, "wav_name": "dev"})
    m = stt.init_message()
    assert m["mode"] == "2pass" and m["chunk_size"] == [5, 10, 5] and m["chunk_interval"] == 10
    assert m["encoder_chunk_look_back"] == 4 and m["decoder_chunk_look_back"] == 0
    assert m["audio_fs"] == 16000 and m["wav_format"] == "pcm" and m["is_speaking"] is True
    assert json.loads(m["hotwords"]) == {"阿里巴巴": 20} and m["itn"] is True and m["wav_name"] == "dev"


async def test_wire_sequence(monkeypatch):
    stt = FunASRStreamingSTT({})
    ws = FakeWS()

    async def fake_connect(url):
        assert url == "ws://localhost:10095"
        return ws

    monkeypatch.setattr(stt, "_ws_connect", fake_connect)
    await stt.start()
    await stt.feed(b"\x01\x02" * 800)
    await stt.finish()
    assert json.loads(ws.sent[0])["is_speaking"] is True
    assert ws.sent[1] == b"\x01\x02" * 800
    assert json.loads(ws.sent[2]) == {"is_speaking": False, "is_end": True}
    await stt.cancel()
    assert ws.closed


async def drain(stt: FunASRStreamingSTT) -> list:
    out = []
    async for t in stt.results():
        out.append(t)
    return out


def test_online_partials_accumulate_and_offline_closes_segment():
    stt = FunASRStreamingSTT({})
    assert stt.handle_message({"mode": "2pass-online", "text": "今天", "wav_name": "demo", "is_final": False}) is False
    assert stt.handle_message({"mode": "2pass-online", "text": "天气", "wav_name": "demo", "is_final": False}) is False
    assert stt.handle_message({"mode": "2pass-offline", "text": "今天天气不错。", "wav_name": "demo", "is_final": False, "timestamp": "[[0,300],[300,900]]"}) is False
    assert stt.handle_message({"mode": "2pass-online", "text": "我们", "wav_name": "demo", "is_final": False}) is False
    got = [stt._queue.get_nowait() for _ in range(4)]
    assert [(t.text, t.final, t.segment) for t in got] == [
        ("今天", False, 0),
        ("今天天气", False, 0),
        ("今天天气不错。", False, 0),  # a closed segment before finish() is not the turn's final
        ("今天天气不错。我们", False, 1),
    ]
    assert (got[2].start_ms, got[2].end_ms) == (0, 900)


async def test_finish_then_offline_final_ends_stream():
    stt = FunASRStreamingSTT({})
    stt._finished = True
    assert stt.handle_message({"mode": "2pass-online", "text": "你好"}) is False
    assert stt.handle_message({"mode": "2pass-offline", "text": "你好。", "is_final": True}) is True
    await stt._queue.put(None)
    out = await drain(stt)
    assert [t.text for t in out] == ["你好", "你好。"] and out[-1].final


async def test_end_ack_without_offline_text_emits_empty_final():
    stt = FunASRStreamingSTT({})
    stt._finished = True
    assert stt.handle_message({"is_end": True, "is_final": True, "mode": "2pass"}) is True
    await stt._queue.put(None)
    out = await drain(stt)
    assert len(out) == 1 and out[0].final and out[0].text == ""


async def test_connection_close_after_finish_still_yields_final(monkeypatch):
    stt = FunASRStreamingSTT({})
    ws = FakeWS()

    async def fake_connect(url):
        return ws

    monkeypatch.setattr(stt, "_ws_connect", fake_connect)
    await stt.start()
    ws.feed(json.dumps({"mode": "2pass-online", "text": "部分"}))
    await stt.finish()
    ws.end()  # server closes without an offline result
    out = await asyncio.wait_for(drain(stt), 2)
    assert out[-1].final and out[-1].text == "部分"


async def test_multi_segment_utterance_yields_one_final_with_all_segments():
    stt = FunASRStreamingSTT({})
    stt.handle_message({"mode": "2pass-offline", "text": "帮我订明天的票。"})
    stt.handle_message({"mode": "2pass-online", "text": "去上海"})
    stt._finished = True
    assert stt.handle_message({"mode": "2pass-offline", "text": "去上海。"}) is True
    await stt._queue.put(None)
    out = await drain(stt)
    finals = [t for t in out if t.final]
    assert len(finals) == 1 and finals[0].text == "帮我订明天的票。去上海。"
