"""Deepgram /v1/listen: URL + auth, Finalize/CloseStream framing, single cumulative final."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from vak.upstream.providers._util import ProviderConfigError
from vak.upstream.providers.base import Transcript
from vak.upstream.providers.stt.deepgram import DeepgramStreamingSTT, join_text

from .fakes import FakeWS


def res(text: str, *, is_final: bool = False, speech_final: bool = False, from_finalize: bool | None = None,
        start: float = 0.0, duration: float = 1.0) -> dict[str, Any]:
    m: dict[str, Any] = {
        "type": "Results",
        "channel_index": [0, 1],
        "start": start,
        "duration": duration,
        "is_final": is_final,
        "speech_final": speech_final,
        "channel": {"alternatives": [{"transcript": text, "confidence": 0.9, "words": []}]},
        "metadata": {"request_id": "r1"},
    }
    if from_finalize is not None:
        m["from_finalize"] = from_finalize
    return m


def drain_nowait(stt: DeepgramStreamingSTT) -> list[Transcript | None]:
    out = []
    while not stt._queue.empty():
        out.append(stt._queue.get_nowait())
    return out


async def drain(stt: DeepgramStreamingSTT) -> list[Transcript]:
    return [t async for t in stt.results()]


async def started(monkeypatch, config: dict[str, Any] | None = None) -> tuple[DeepgramStreamingSTT, FakeWS, list]:
    stt = DeepgramStreamingSTT({"api_key": "k", **(config or {})})
    ws = FakeWS()
    calls: list = []

    async def fake_connect(url, headers):
        calls.append((url, headers))
        return ws

    monkeypatch.setattr(stt, "_ws_connect", fake_connect)
    await stt.start()
    return stt, ws, calls


def test_url_and_auth_header():
    stt = DeepgramStreamingSTT({"api_key": "secret-key"})
    url = stt.build_url()
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "wss://api.deepgram.com/v1/listen"
    q = parse_qs(parts.query)
    assert q["model"] == ["nova-3"] and q["language"] == ["en"]
    assert q["encoding"] == ["linear16"] and q["sample_rate"] == ["16000"] and q["channels"] == ["1"]
    assert q["interim_results"] == ["true"] and q["punctuate"] == ["true"] and q["smart_format"] == ["false"]
    assert "endpointing" not in q and "keyterm" not in q and "no_delay" not in q
    assert stt.headers() == {"Authorization": "Token secret-key"}


def test_url_options():
    stt = DeepgramStreamingSTT({
        "api_key": "k", "url": "wss://dg.example/v1/listen", "model": "nova-2", "language": "zh",
        "sample_rate": 8000, "smart_format": True, "punctuate": False, "endpointing": 300,
        "keyterms": ["vak", "Deepgram"], "keywords": {"FunASR": 2},
    })
    parts = urlsplit(stt.build_url())
    assert parts.netloc == "dg.example"
    q = parse_qs(parts.query)
    assert q["model"] == ["nova-2"] and q["language"] == ["zh"] and q["sample_rate"] == ["8000"]
    assert q["smart_format"] == ["true"] and q["no_delay"] == ["true"] and q["punctuate"] == ["false"]
    assert q["endpointing"] == ["300"] and q["keyterm"] == ["vak", "Deepgram"] and q["keywords"] == ["FunASR:2"]
    assert stt.sample_rate == 8000
    assert parse_qs(urlsplit(DeepgramStreamingSTT({"api_key": "k", "endpointing": False}).build_url()).query)[
        "endpointing"
    ] == ["false"]


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(ProviderConfigError):
        DeepgramStreamingSTT({})
    monkeypatch.setenv("DEEPGRAM_API_KEY", "from-env")
    assert DeepgramStreamingSTT({}).headers() == {"Authorization": "Token from-env"}


async def test_wire_sequence_and_finalize_completion(monkeypatch):
    stt, ws, calls = await started(monkeypatch)
    assert calls[0][1] == {"Authorization": "Token k"} and calls[0][0].startswith("wss://api.deepgram.com/v1/listen?")
    await stt.feed(b"\x01\x02" * 800)
    await stt.feed(b"")
    await stt.feed(b"\x03\x04" * 800)
    await stt.finish()
    assert ws.sent == [b"\x01\x02" * 800, b"\x03\x04" * 800, json.dumps({"type": "Finalize"})]
    ws.feed(json.dumps(res("hello world", is_final=True, from_finalize=True)))
    out = await asyncio.wait_for(drain(stt), 2)
    assert [(t.text, t.final) for t in out] == [("hello world", False), ("hello world", True)]
    await asyncio.sleep(0)
    assert ws.sent_json[-1] == {"type": "CloseStream"} and ws.closed


def test_cumulative_partials_and_no_final_before_finish():
    stt = DeepgramStreamingSTT({"api_key": "k"})
    assert stt.handle_message(res("hello")) is False
    assert stt.handle_message(res("hello there")) is False
    assert stt.handle_message(res("hello there", is_final=True, speech_final=True, start=0.0, duration=1.2)) is False
    assert stt.handle_message({"type": "UtteranceEnd", "last_word_end": 1.1}) is False
    assert stt.handle_message(res("how")) is False
    assert stt.handle_message(res("how are you", is_final=True, start=1.2, duration=0.8)) is False
    assert stt.handle_message(res("", is_final=True, speech_final=True, start=2.0, duration=0.5)) is False
    assert stt.handle_message({"type": "Metadata", "request_id": "r1"}) is False  # not finished yet
    got = drain_nowait(stt)
    assert all(t is not None and not t.final for t in got)
    assert [t.text for t in got if t] == ["hello", "hello there", "hello there how", "hello there how are you"]
    stt._finished = True
    assert stt.handle_message(res("", is_final=True, from_finalize=True, start=2.5, duration=0.1)) is True
    (final,) = drain_nowait(stt)
    assert final is not None and final.final and final.text == "hello there how are you"
    assert (final.start_ms, final.end_ms) == (0, 2000)


def test_regular_final_after_finish_is_folded_until_from_finalize():
    stt = DeepgramStreamingSTT({"api_key": "k"})
    stt.handle_message(res("one", is_final=True))
    stt._finished = True
    assert stt.handle_message(res("two", is_final=True, speech_final=True)) is False
    assert stt.handle_message(res("three", is_final=True, from_finalize=True)) is True
    got = drain_nowait(stt)
    assert [(t.text, t.final) for t in got if t] == [("one", False), ("one two", False), ("one two three", False),
                                                     ("one two three", True)]


def test_cjk_join_without_spaces():
    assert join_text(["今天天气", "不错。", "OK", "fine"]) == "今天天气不错。OK fine"
    stt = DeepgramStreamingSTT({"api_key": "k", "language": "zh"})
    stt.handle_message(res("今天天气", is_final=True))
    stt._finished = True
    stt.handle_message(res("不错", is_final=True, from_finalize=True))
    assert drain_nowait(stt)[-1].text == "今天天气不错"


def test_metadata_after_finish_completes():
    stt = DeepgramStreamingSTT({"api_key": "k"})
    stt.handle_message(res("almost", is_final=True))
    stt.handle_message(res("done"))
    stt._finished = True
    assert stt.handle_message({"type": "Metadata", "request_id": "r1", "duration": 2.0}) is True
    got = drain_nowait(stt)
    assert got[-1].final and got[-1].text == "almost done"
    assert sum(1 for t in got if t.final) == 1


async def test_socket_close_after_finish_yields_fallback_final(monkeypatch, caplog):
    stt, ws, _ = await started(monkeypatch)
    ws.feed(json.dumps(res("partial words", is_final=True)), json.dumps(res("tail")))
    await stt.finish()
    ws.end()  # server closes without a from_finalize result
    with caplog.at_level("WARNING"):
        out = await asyncio.wait_for(drain(stt), 2)
    assert [t.final for t in out] == [False, False, True]
    assert out[-1].text == "partial words tail"
    assert "without a finalize result" in caplog.text


async def test_error_message_after_finish(monkeypatch, caplog):
    stt, ws, _ = await started(monkeypatch)
    ws.feed(json.dumps(res("some text", is_final=True)))
    await stt.finish()
    ws.feed(json.dumps({"type": "Error", "description": "bad audio", "message": "DATA-0000", "variant": "x"}))
    with caplog.at_level("WARNING"):
        out = await asyncio.wait_for(drain(stt), 2)
    assert out[-1].final and out[-1].text == "some text"
    assert sum(1 for t in out if t.final) == 1
    assert "bad audio" in caplog.text


async def test_error_before_finish_ends_without_final(monkeypatch):
    stt, ws, _ = await started(monkeypatch)
    ws.feed(json.dumps(res("abc")), json.dumps({"type": "Error", "description": "nope"}))
    out = await asyncio.wait_for(drain(stt), 2)
    assert [(t.text, t.final) for t in out] == [("abc", False)]


async def test_cancel_is_idempotent_and_ends_results(monkeypatch):
    stt, ws, _ = await started(monkeypatch)
    ws.feed(json.dumps(res("hi")))
    await stt.finish()
    await stt.cancel()
    await stt.cancel()
    out = await asyncio.wait_for(drain(stt), 2)
    assert all(not t.final for t in out)
    assert ws.closed
