"""OpenAI Realtime transcription mode: session.update, append/commit framing, event mapping."""

from __future__ import annotations

import asyncio
import base64
import json

import numpy as np
import pytest

from vak.upstream.providers._util import ProviderConfigError
from vak.upstream.providers.stt.openai_realtime_stt import OpenAIRealtimeSTT

from .fakes import FakeWS

P = "conversation.item.input_audio_transcription."


def make(**cfg) -> OpenAIRealtimeSTT:
    return OpenAIRealtimeSTT({"api_key": "sk-test", **cfg})


async def started(monkeypatch, stt: OpenAIRealtimeSTT) -> FakeWS:
    ws = FakeWS()
    seen = {}

    async def fake_connect(url, headers):
        seen.update(url=url, headers=headers)
        return ws

    monkeypatch.setattr(stt, "_ws_connect", fake_connect)
    await stt.start()
    ws.seen = seen  # type: ignore[attr-defined]
    return ws


async def drain(stt: OpenAIRealtimeSTT) -> list:
    out = []
    async for t in stt.results():
        out.append(t)
    return out


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderConfigError):
        OpenAIRealtimeSTT({})


def test_key_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert OpenAIRealtimeSTT({}).headers() == {"Authorization": "Bearer sk-env"}


def test_session_message_disables_vad():
    m = make(language="zh", prompt="vak", noise_reduction="near_field").session_message()
    assert m["type"] == "session.update" and m["session"]["type"] == "transcription"
    inp = m["session"]["audio"]["input"]
    assert inp["format"] == {"type": "audio/pcm", "rate": 24000}
    assert "turn_detection" in inp and inp["turn_detection"] is None
    assert inp["transcription"] == {"model": "gpt-4o-transcribe", "language": "zh", "prompt": "vak"}
    assert inp["noise_reduction"] == {"type": "near_field"}
    # the null JSON value really goes on the wire
    assert '"turn_detection": null' in json.dumps(m)


def test_session_message_minimal():
    inp = make(model="gpt-4o-mini-transcribe").session_message()["session"]["audio"]["input"]
    assert inp["transcription"] == {"model": "gpt-4o-mini-transcribe"}
    assert "noise_reduction" not in inp


def test_url():
    assert make().url() == "wss://api.openai.com/v1/realtime?intent=transcription"
    assert make(base_url="https://gw.example/v1/realtime/").url() == "wss://gw.example/v1/realtime?intent=transcription&model=gpt-4o-transcribe"


async def test_wire_sequence_resamples_to_24k(monkeypatch):
    stt = make()
    ws = await started(monkeypatch, stt)
    assert ws.seen == {  # type: ignore[attr-defined]
        "url": "wss://api.openai.com/v1/realtime?intent=transcription",
        "headers": {"Authorization": "Bearer sk-test"},
    }
    pcm = np.arange(320, dtype=np.int16).tobytes()  # 20 ms @ 16 kHz
    await stt.feed(pcm)  # sent before any session ack
    await stt.finish()
    msgs = ws.sent_json
    assert [m["type"] for m in msgs] == ["session.update", "input_audio_buffer.append", "input_audio_buffer.commit"]
    audio = base64.b64decode(msgs[1]["audio"])
    assert len(audio) == 480 * 2  # 20 ms @ 24 kHz
    await stt.cancel()
    assert ws.closed


async def test_no_resample_at_24k(monkeypatch):
    stt = make(sample_rate=24000)
    ws = await started(monkeypatch, stt)
    await stt.feed(b"\x01\x02" * 100)
    assert base64.b64decode(ws.sent_json[1]["audio"]) == b"\x01\x02" * 100
    await stt.cancel()


async def test_deltas_accumulate_then_single_final():
    stt = make()
    assert stt.handle_message({"type": "session.updated"}) is False
    stt._finished = True
    assert stt.handle_message({"type": "input_audio_buffer.committed", "item_id": "i1"}) is False
    assert stt.handle_message({"type": P + "delta", "item_id": "i1", "delta": "Hello"}) is False
    assert stt.handle_message({"type": P + "delta", "item_id": "i1", "delta": ""}) is False  # duplicate skipped
    assert stt.handle_message({"type": P + "delta", "item_id": "i1", "delta": " world"}) is False
    assert stt.handle_message({"type": P + "completed", "item_id": "i1", "transcript": "Hello world."}) is True
    await stt._queue.put(None)
    out = await drain(stt)
    assert [(t.text, t.final) for t in out] == [("Hello", False), ("Hello world", False), ("Hello world.", True)]


async def test_end_to_end_final_ends_stream(monkeypatch):
    stt = make()
    ws = await started(monkeypatch, stt)
    await stt.feed(b"\x00\x00" * 320)
    await stt.finish()
    ws.feed(
        json.dumps({"type": P + "delta", "item_id": "i1", "delta": "你好"}),
        json.dumps({"type": P + "completed", "item_id": "i1", "transcript": "你好。"}),
        json.dumps({"type": P + "completed", "item_id": "i2", "transcript": "late"}),
    )
    out = await asyncio.wait_for(drain(stt), 2)
    assert [(t.text, t.final) for t in out] == [("你好", False), ("你好。", True)]
    assert ws.closed


@pytest.mark.parametrize(
    "event",
    [
        {"type": P + "failed", "item_id": "i1", "error": {"message": "boom"}},
        {"type": "error", "error": {"type": "server_error", "code": "server_error", "message": "x"}},
    ],
)
async def test_failure_after_finish_falls_back_to_partial(monkeypatch, event):
    stt = make()
    ws = await started(monkeypatch, stt)
    await stt.finish()
    ws.feed(json.dumps({"type": P + "delta", "item_id": "i1", "delta": "partial"}), json.dumps(event))
    out = await asyncio.wait_for(drain(stt), 2)
    assert [(t.text, t.final) for t in out] == [("partial", False), ("partial", True)]


async def test_socket_close_after_finish_yields_final(monkeypatch):
    stt = make()
    ws = await started(monkeypatch, stt)
    await stt.finish()
    ws.feed(json.dumps({"type": P + "delta", "item_id": "i1", "delta": "部分"}))
    ws.end()
    out = await asyncio.wait_for(drain(stt), 2)
    assert out[-1].final and out[-1].text == "部分" and sum(t.final for t in out) == 1


async def test_socket_close_before_finish_final_on_finish(monkeypatch):
    stt = make()
    ws = await started(monkeypatch, stt)
    ws.end()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await stt.finish()
    out = await asyncio.wait_for(drain(stt), 2)
    assert [(t.text, t.final) for t in out] == [("", True)]


async def test_commit_empty_gives_empty_final(monkeypatch):
    stt = make()
    ws = await started(monkeypatch, stt)
    await stt.finish()
    ws.feed(json.dumps({"type": "error", "error": {"type": "invalid_request_error", "code": "input_audio_buffer_commit_empty", "message": "buffer too small"}}))
    out = await asyncio.wait_for(drain(stt), 2)
    assert [(t.text, t.final) for t in out] == [("", True)]


async def test_error_before_finish_is_not_final():
    stt = make()
    assert stt.handle_message({"type": "error", "error": {"code": "invalid_value", "message": "x"}}) is False
    assert stt._queue.empty()


async def test_cancel_is_idempotent_and_ends_results(monkeypatch):
    stt = make()
    ws = await started(monkeypatch, stt)
    await stt.finish()
    await stt.cancel()
    await stt.cancel()
    out = await asyncio.wait_for(drain(stt), 2)
    assert out == [] and ws.closed
