"""AssemblyAI v3: URL + auth, ForceEndpoint/Terminate framing, single cumulative final."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from vak.upstream.providers._util import ProviderConfigError
from vak.upstream.providers.base import Transcript
from vak.upstream.providers.stt.assemblyai import AssemblyAIStreamingSTT

from .fakes import FakeWS


def turn(order: int, words: list[str], *, end: bool = False, formatted: bool = False,
         transcript: str | None = None, t0: int = 0) -> dict[str, Any]:
    return {
        "type": "Turn",
        "turn_order": order,
        "turn_is_formatted": formatted,
        "end_of_turn": end,
        "transcript": " ".join(words) if transcript is None else transcript,
        "end_of_turn_confidence": 0.5,
        "words": [
            {"text": w, "start": t0 + i * 100, "end": t0 + i * 100 + 80, "confidence": 0.9, "word_is_final": end}
            for i, w in enumerate(words)
        ],
    }


TERMINATION = {"type": "Termination", "audio_duration_seconds": 2.0, "session_duration_seconds": 2.5}


def drain_nowait(stt: AssemblyAIStreamingSTT) -> list[Transcript]:
    out = []
    while not stt._queue.empty():
        t = stt._queue.get_nowait()
        assert t is not None
        out.append(t)
    return out


async def drain(stt: AssemblyAIStreamingSTT) -> list[Transcript]:
    return [t async for t in stt.results()]


async def started(monkeypatch, config: dict[str, Any] | None = None) -> tuple[AssemblyAIStreamingSTT, FakeWS, list]:
    stt = AssemblyAIStreamingSTT({"api_key": "k", **(config or {})})
    ws = FakeWS([json.dumps({"type": "Begin", "id": "s1", "expires_at": 1})])
    calls: list = []

    async def fake_connect(url, headers):
        calls.append((url, headers))
        return ws

    monkeypatch.setattr(stt, "_ws_connect", fake_connect)
    await stt.start()
    return stt, ws, calls


def test_url_and_auth_header():
    stt = AssemblyAIStreamingSTT({"api_key": "secret-key"})
    parts = urlsplit(stt.build_url())
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "wss://streaming.assemblyai.com/v3/ws"
    q = parse_qs(parts.query)
    assert q == {
        "sample_rate": ["16000"],
        "encoding": ["pcm_s16le"],
        "speech_model": ["universal-streaming-english"],
        "format_turns": ["true"],
    }
    assert stt.headers() == {"Authorization": "secret-key"}


def test_url_options():
    stt = AssemblyAIStreamingSTT({
        "api_key": "k", "url": "wss://streaming.eu.assemblyai.com/v3/ws", "sample_rate": 8000,
        "speech_model": "universal-streaming-multilingual", "format_turns": False, "keyterms": ["vak", "FunASR"],
    })
    parts = urlsplit(stt.build_url())
    assert parts.netloc == "streaming.eu.assemblyai.com"
    q = parse_qs(parts.query)
    assert q["sample_rate"] == ["8000"] and q["speech_model"] == ["universal-streaming-multilingual"]
    assert q["format_turns"] == ["false"] and json.loads(q["keyterms_prompt"][0]) == ["vak", "FunASR"]


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)
    with pytest.raises(ProviderConfigError):
        AssemblyAIStreamingSTT({})
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "from-env")
    assert AssemblyAIStreamingSTT({}).headers() == {"Authorization": "from-env"}


async def test_wire_sequence_and_forced_turn_completion(monkeypatch):
    stt, ws, calls = await started(monkeypatch)
    assert calls[0][1] == {"Authorization": "k"}
    await stt.feed(b"\x01\x02" * 800)
    await stt.feed(b"\x03\x04" * 800)
    await stt.finish()
    await stt.finish()  # idempotent
    assert ws.sent[:2] == [b"\x01\x02" * 800, b"\x03\x04" * 800]
    assert ws.sent_json == [{"type": "ForceEndpoint"}, {"type": "Terminate"}]
    ws.feed(
        json.dumps(turn(0, ["hello", "world"])),
        json.dumps(turn(0, ["hello", "world"], end=True)),
        json.dumps(turn(0, ["Hello", "world."], end=True, formatted=True)),
    )
    out = await asyncio.wait_for(drain(stt), 2)
    assert [(t.text, t.final) for t in out] == [("hello world", False), ("Hello world.", False), ("Hello world.", True)]
    assert ws.closed


def test_cumulative_partials_and_no_final_before_finish():
    stt = AssemblyAIStreamingSTT({"api_key": "k"})
    assert stt.handle_message({"type": "Begin", "id": "s", "expires_at": 1}) is False
    assert stt.handle_message({"type": "SpeechStarted", "timestamp": 10}) is False
    assert stt.handle_message(turn(0, ["hi"], transcript="")) is False
    assert stt.handle_message(turn(0, ["hi", "there"], transcript="hi")) is False
    assert stt.handle_message(turn(0, ["hi", "there"], end=True)) is False  # unformatted end: not closed yet
    assert stt.handle_message(turn(0, ["Hi", "there."], end=True, formatted=True)) is False
    assert stt.handle_message(turn(1, ["how"], t0=1000)) is False
    assert stt.handle_message(turn(1, ["How", "are", "you?"], end=True, formatted=True, t0=1000)) is False
    got = drain_nowait(stt)
    assert not any(t.final for t in got)
    assert [t.text for t in got] == [
        "hi", "hi there", "Hi there.", "Hi there. how", "Hi there. How are you?",
    ]
    stt._finished = True
    assert stt.handle_message(turn(2, ["Bye."], end=True, formatted=True, t0=2000)) is True
    got = drain_nowait(stt)
    assert got[-1].final and got[-1].text == "Hi there. How are you? Bye."
    assert (got[-1].start_ms, got[-1].end_ms) == (0, 2080)
    assert sum(1 for t in got if t.final) == 1


def test_late_close_of_earlier_turn_after_finish_is_folded():
    stt = AssemblyAIStreamingSTT({"api_key": "k"})
    stt.handle_message(turn(0, ["one"], end=True))  # unformatted end; formatted one still in flight
    stt.handle_message(turn(1, ["two"]))
    stt._ws = FakeWS()
    asyncio.run(stt.finish())
    assert stt.handle_message(turn(0, ["One."], end=True, formatted=True)) is False
    assert stt.handle_message(turn(1, ["Two."], end=True, formatted=True)) is True
    got = drain_nowait(stt)
    assert [(t.text, t.final) for t in got] == [
        ("one", False),
        ("one two", False),
        ("One. two", False),
        ("One. Two.", False),
        ("One. Two.", True),
    ]


def test_format_turns_off_closes_on_plain_end_of_turn():
    stt = AssemblyAIStreamingSTT({"api_key": "k", "format_turns": False})
    stt.handle_message(turn(0, ["one"], end=True))
    stt._finished = True
    assert stt.handle_message(turn(1, ["two"], end=True)) is True
    assert drain_nowait(stt)[-1].text == "one two"


def test_termination_after_finish_completes_when_no_turn_was_open():
    stt = AssemblyAIStreamingSTT({"api_key": "k"})
    stt.handle_message(turn(0, ["Done."], end=True, formatted=True))
    stt._finished = True
    assert stt.handle_message(TERMINATION) is True
    got = drain_nowait(stt)
    assert [(t.text, t.final) for t in got] == [("Done.", False), ("Done.", True)]


def test_cjk_turns_join_without_space():
    stt = AssemblyAIStreamingSTT({"api_key": "k", "speech_model": "universal-streaming-multilingual"})
    stt.handle_message(turn(0, ["你好。"], end=True, formatted=True))
    stt._finished = True
    stt.handle_message(turn(1, ["再见"], end=True, formatted=True))
    assert drain_nowait(stt)[-1].text == "你好。再见"


async def test_socket_close_after_finish_yields_fallback_final(monkeypatch, caplog):
    stt, ws, _ = await started(monkeypatch)
    ws.feed(json.dumps(turn(0, ["First."], end=True, formatted=True)), json.dumps(turn(1, ["second", "part"])))
    while stt._open_order != 1:  # let the reader see both before the button is released
        await asyncio.sleep(0)
    await stt.finish()
    ws.end()  # closes without the forced turn or Termination
    with caplog.at_level("WARNING"):
        out = await asyncio.wait_for(drain(stt), 2)
    assert [t.final for t in out] == [False, False, True]
    assert out[-1].text == "First. second part"
    assert "without a closing turn" in caplog.text


async def test_error_message_after_finish(monkeypatch, caplog):
    stt, ws, _ = await started(monkeypatch)
    ws.feed(json.dumps(turn(0, ["some", "text"])))
    await stt.finish()
    ws.feed(json.dumps({"error": "Invalid audio"}))
    with caplog.at_level("WARNING"):
        out = await asyncio.wait_for(drain(stt), 2)
    assert out[-1].final and out[-1].text == "some text"
    assert sum(1 for t in out if t.final) == 1
    assert "Invalid audio" in caplog.text


async def test_error_or_termination_before_finish_ends_without_final(monkeypatch):
    stt, ws, _ = await started(monkeypatch)
    ws.feed(json.dumps(turn(0, ["abc"])), json.dumps(TERMINATION))
    out = await asyncio.wait_for(drain(stt), 2)
    assert [(t.text, t.final) for t in out] == [("abc", False)]


async def test_cancel_is_idempotent_and_ends_results(monkeypatch):
    stt, ws, _ = await started(monkeypatch)
    ws.feed(json.dumps(turn(0, ["hi"])))
    await stt.finish()
    await stt.cancel()
    await stt.cancel()
    out = await asyncio.wait_for(drain(stt), 2)
    assert all(not t.final for t in out)
    assert ws.closed
