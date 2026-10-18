"""DashScope real-time ASR framing: run-task, buffered audio, finish-task, result folding."""

from __future__ import annotations

import asyncio
import json

import pytest

from vak.upstream.providers._util import ProviderConfigError
from vak.upstream.providers.stt.aliyun import AliyunStreamingSTT

from .fakes import FakeWS


def ev(event: str, **header) -> str:
    return json.dumps({"header": {"event": event, "task_id": "t", **header}, "payload": {}})


def result(text: str, *, end: bool, begin: int = 0, end_ms: int | None = None) -> str:
    sentence = {"begin_time": begin, "end_time": end_ms if end else None, "text": text, "sentence_end": end}
    return json.dumps(
        {
            "header": {"event": "result-generated", "task_id": "t"},
            "payload": {"output": {"sentence": sentence}},
        }
    )


async def connected(config: dict | None = None, monkeypatch=None) -> tuple[AliyunStreamingSTT, FakeWS, dict]:
    stt = AliyunStreamingSTT({"api_key": "sk-test", **(config or {})})
    ws = FakeWS()
    seen: dict = {}

    async def fake_connect(url, headers):
        seen.update(url=url, headers=headers)
        return ws

    monkeypatch.setattr(stt, "_ws_connect", fake_connect)
    await stt.start()
    return stt, ws, seen


async def drain(stt: AliyunStreamingSTT) -> list:
    return [t async for t in stt.results()]


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def test_run_task_paraformer_v2():
    stt = AliyunStreamingSTT(
        {
            "api_key": "k",
            "model": "paraformer-realtime-v2",
            "language_hints": ["zh", "en"],
            "vocabulary_id": "vocab-1",
            "disfluency_removal_enabled": True,
            "max_sentence_silence": "800",
        }
    )
    m = stt.run_task_message()
    assert m["header"] == {"action": "run-task", "task_id": stt.task_id, "streaming": "duplex"}
    assert len(stt.task_id) == 32
    p = m["payload"]
    assert (p["task_group"], p["task"], p["function"], p["model"], p["input"]) == (
        "audio",
        "asr",
        "recognition",
        "paraformer-realtime-v2",
        {},
    )
    assert p["parameters"] == {
        "format": "pcm",
        "sample_rate": 16000,
        "language_hints": ["zh", "en"],
        "vocabulary_id": "vocab-1",
        "disfluency_removal_enabled": True,
        "max_sentence_silence": 800,
    }


def test_run_task_fun_asr_and_defaults():
    stt = AliyunStreamingSTT(
        {"api_key": "k", "model": "fun-asr-realtime", "sample_rate": 8000, "vocabulary_id": "v"}
    )
    p = stt.run_task_message()["payload"]
    assert p["model"] == "fun-asr-realtime"
    assert p["parameters"] == {"format": "pcm", "sample_rate": 8000, "vocabulary_id": "v"}
    default = AliyunStreamingSTT({"api_key": "k"})
    assert default.run_task_message()["payload"]["model"] == "paraformer-realtime-v2"
    assert default.run_task_message()["payload"]["parameters"] == {"format": "pcm", "sample_rate": 16000}


def test_vocabulary_dropped_for_paraformer_v1():
    stt = AliyunStreamingSTT({"api_key": "k", "model": "paraformer-realtime-v1", "vocabulary_id": "v"})
    assert "vocabulary_id" not in stt.run_task_message()["payload"]["parameters"]


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(ProviderConfigError):
        AliyunStreamingSTT({})
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-env")
    assert AliyunStreamingSTT({}).headers()["Authorization"] == "Bearer sk-env"


async def test_buffer_until_task_started_then_flush_and_finish(monkeypatch):
    stt, ws, seen = await connected({}, monkeypatch)
    assert seen["url"] == "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    assert seen["headers"] == {"Authorization": "Bearer sk-test", "X-DashScope-DataInspection": "enable"}
    assert ws.sent_json[0]["header"]["action"] == "run-task"
    await stt.feed(b"\x01\x01")
    await stt.feed(b"\x02\x02")
    assert len(ws.sent) == 1  # nothing before task-started
    ws.feed(ev("task-started"))
    await settle()
    await stt.feed(b"\x03\x03")
    assert ws.sent[1:] == [b"\x01\x01", b"\x02\x02", b"\x03\x03"]
    await stt.finish()
    assert ws.sent_json[-1] == {
        "header": {"action": "finish-task", "task_id": stt.task_id, "streaming": "duplex"},
        "payload": {"input": {}},
    }
    await stt.feed(b"\x04\x04")  # dropped after finish
    assert b"\x04\x04" not in ws.sent
    await stt.cancel()
    await stt.cancel()  # idempotent
    assert ws.closed
    assert await asyncio.wait_for(drain(stt), 1) == []


async def test_finish_before_task_started_is_deferred(monkeypatch):
    stt, ws, _ = await connected({}, monkeypatch)
    await stt.feed(b"\x01\x01")
    await stt.finish()
    assert len(ws.sent) == 1
    ws.feed(ev("task-started"))
    await settle()
    assert ws.sent[1] == b"\x01\x01"
    assert json.loads(ws.sent[2])["header"]["action"] == "finish-task"
    await stt.cancel()


async def test_two_sentences_fold_into_one_final(monkeypatch):
    stt, ws, _ = await connected({}, monkeypatch)
    ws.feed(
        ev("task-started"),
        result("今天", end=False),
        result("今天", end=False),  # duplicate: skipped
        result("今天天气不错。", end=True, end_ms=900),
        result("我们", end=False, begin=1200),
        result("我们出去玩吧。", end=True, begin=1200, end_ms=2100),
    )
    await settle()
    got = []
    while not stt._queue.empty():
        got.append(stt._queue.get_nowait())
    assert [(t.text, t.final) for t in got] == [
        ("今天", False),
        ("今天天气不错。", False),
        ("今天天气不错。我们", False),
        ("今天天气不错。我们出去玩吧。", False),
    ]  # no final before finish()
    await stt.finish()
    ws.feed(ev("task-finished"))
    out = await asyncio.wait_for(drain(stt), 1)
    assert [(t.text, t.final) for t in out] == [("今天天气不错。我们出去玩吧。", True)]
    assert (out[0].start_ms, out[0].end_ms) == (0, 2100)


def test_tentative_sentence_included_in_final_and_english_spacing():
    stt = AliyunStreamingSTT({"api_key": "k"})
    stt._finished = True
    stt.handle_message(json.loads(result("Hello there.", end=True, end_ms=500)))
    stt.handle_message(json.loads(result("How are", end=False)))
    assert stt.handle_message(json.loads(ev("task-finished"))) is True
    texts = [stt._queue.get_nowait() for _ in range(3)]
    assert [(t.text, t.final) for t in texts] == [
        ("Hello there.", False),
        ("Hello there. How are", False),
        ("Hello there. How are", True),
    ]


async def test_task_failed_after_finish_gives_fallback_final(monkeypatch):
    stt, ws, _ = await connected({}, monkeypatch)
    ws.feed(ev("task-started"), result("部分", end=False))
    await settle()
    await stt.finish()
    ws.feed(ev("task-failed", error_code="InternalError", error_message="boom"))
    out = await asyncio.wait_for(drain(stt), 1)
    assert [(t.text, t.final) for t in out] == [("部分", False), ("部分", True)]
    assert ws.closed


async def test_socket_close_after_finish_gives_fallback_final(monkeypatch):
    stt, ws, _ = await connected({}, monkeypatch)
    ws.feed(ev("task-started"), result("第一句。", end=True, end_ms=500), result("第二", end=False))
    await settle()
    await stt.finish()
    ws.end()  # server closes without task-finished
    out = await asyncio.wait_for(drain(stt), 1)
    assert out[-1].final and out[-1].text == "第一句。第二"
    assert sum(t.final for t in out) == 1


async def test_failure_before_finish_keeps_text_for_finish(monkeypatch):
    stt, ws, _ = await connected({}, monkeypatch)
    ws.feed(ev("task-started"), result("你好", end=False), ev("task-failed", error_code="X"))
    await settle()
    await stt.feed(b"\x01\x01")  # ignored, no error
    await stt.finish()
    out = await asyncio.wait_for(drain(stt), 1)
    assert [(t.text, t.final) for t in out] == [("你好", False), ("你好", True)]
