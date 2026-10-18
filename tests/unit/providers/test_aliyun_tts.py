"""DashScope CosyVoice streaming TTS: task framing, PCM chunks, connection pool, flush."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from vak.upstream.providers._util import ProviderConfigError
from vak.upstream.providers.tts import aliyun as aliyun_mod
from vak.upstream.providers.tts.aliyun import AliyunStreamingTTS

from .fakes import FakeWS


def ev(event: str, task_id: str = "t", **header: Any) -> str:
    return json.dumps({"header": {"event": event, "task_id": task_id, **header}, "payload": {}})


class TtsWS(FakeWS):
    """FakeWS with a websockets-like `state`; in `auto` mode it plays a well-behaved
    server: task-started on run-task, `frames` + task-finished on finish-task."""

    def __init__(self, frames: list[bytes] | None = None, *, auto: bool = True) -> None:
        super().__init__()
        self.frames = [b"\x01\x00\x02\x00", b"\x03\x00"] if frames is None else frames
        self.auto = auto
        self.fail_send = False

    @property
    def state(self) -> Any:
        return SimpleNamespace(name="CLOSED" if self.closed else "OPEN")

    async def send(self, data: Any) -> None:
        if self.fail_send:
            raise ConnectionError("broken pipe")
        await super().send(data)
        if not self.auto:
            return
        msg = json.loads(data)
        action, tid = msg["header"]["action"], msg["header"]["task_id"]
        if action == "run-task":
            self.feed(ev("task-started", tid))
        elif action == "finish-task":
            self.feed(json.dumps({"header": {"event": "result-generated", "task_id": tid}, "payload": {}}))
            self.feed(*self.frames, ev("task-finished", tid))

    def actions(self) -> list[str]:
        return [m["header"]["action"] for m in self.sent_json]


def make(monkeypatch: pytest.MonkeyPatch, sockets: list[TtsWS], **config: Any) -> tuple[AliyunStreamingTTS, dict]:
    tts = AliyunStreamingTTS({"api_key": "sk-test", **config})
    seen: dict = {"connects": 0}
    pending = list(sockets)

    async def fake_connect(url: str, headers: dict[str, str]) -> TtsWS:
        seen.update(url=url, headers=headers)
        seen["connects"] += 1
        return pending.pop(0)

    monkeypatch.setattr(tts, "_ws_connect", fake_connect)
    return tts, seen


async def collect(tts: AliyunStreamingTTS, text: str, rid: str) -> list:
    return [c async for c in tts.synthesize(text, rid)]


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_wire_messages(monkeypatch):
    ws = TtsWS()
    tts, seen = make(monkeypatch, [ws])
    await collect(tts, "你好。", "r1")
    assert seen["url"] == "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    assert seen["headers"] == {"Authorization": "Bearer sk-test", "X-DashScope-DataInspection": "enable"}
    run, cont, fin = ws.sent_json
    tid = run["header"]["task_id"]
    assert len(tid) == 32
    assert run["header"] == {"action": "run-task", "task_id": tid, "streaming": "duplex"}
    assert run["payload"] == {
        "task_group": "audio",
        "task": "tts",
        "function": "SpeechSynthesizer",
        "model": "cosyvoice-v2",
        "parameters": {"text_type": "PlainText", "voice": "longxiaochun_v2", "format": "pcm", "sample_rate": 24000},
        "input": {},
    }
    assert cont == {
        "header": {"action": "continue-task", "task_id": tid, "streaming": "duplex"},
        "payload": {"input": {"text": "你好。"}},
    }
    assert fin == {
        "header": {"action": "finish-task", "task_id": tid, "streaming": "duplex"},
        "payload": {"input": {}},
    }
    assert "你好" in ws.sent[1]  # ensure_ascii=False


async def test_optional_params_and_rate(monkeypatch):
    ws = TtsWS()
    tts, _ = make(
        monkeypatch, [ws], model="cosyvoice-v3", voice="longanyang", sample_rate=16000,
        volume="80", rate=1.2, pitch=0.9, url="wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference",
    )
    chunks = await collect(tts, "hi", "r")
    params = ws.sent_json[0]["payload"]["parameters"]
    assert ws.sent_json[0]["payload"]["model"] == "cosyvoice-v3"
    assert params == {
        "text_type": "PlainText", "voice": "longanyang", "format": "pcm", "sample_rate": 16000,
        "volume": 80, "rate": 1.2, "pitch": 0.9,
    }
    assert all(c.sample_rate == 16000 for c in chunks) and tts.sample_rate == 16000


async def test_waits_for_task_started_before_text(monkeypatch):
    ws = TtsWS(auto=False)
    tts, _ = make(monkeypatch, [ws])
    task = asyncio.create_task(collect(tts, "x", "r"))
    await settle()
    assert ws.actions() == ["run-task"]
    tid = ws.sent_json[0]["header"]["task_id"]
    ws.feed(ev("task-started", tid))
    await settle()
    assert ws.actions() == ["run-task", "continue-task", "finish-task"]
    assert {m["header"]["task_id"] for m in ws.sent_json} == {tid}
    ws.feed(b"\x00\x00", ev("task-finished", tid))
    assert [c.is_last for c in await task] == [False, True]


async def test_frames_become_ordered_aligned_chunks(monkeypatch):
    ws = TtsWS(frames=[b"\x01\x02\x03", b"\x04", b"\x05\x06"])
    tts, _ = make(monkeypatch, [ws])
    chunks = await collect(tts, "x", "r")
    assert [c.pcm16 for c in chunks] == [b"\x01\x02", b"\x03\x04", b"\x05\x06", b""]
    assert [c.is_last for c in chunks] == [False, False, False, True]
    assert tts.supports_text_stream is False


async def test_connection_reused_across_sentences(monkeypatch):
    ws = TtsWS()
    tts, seen = make(monkeypatch, [ws])
    await collect(tts, "one", "r:0")
    await collect(tts, "two", "r:1")
    assert seen["connects"] == 1 and not ws.closed
    runs = [m for m in ws.sent_json if m["header"]["action"] == "run-task"]
    assert len(runs) == 2 and runs[0]["header"]["task_id"] != runs[1]["header"]["task_id"]


def finish_manually(ws: TtsWS) -> None:
    tid = ws.sent_json[0]["header"]["task_id"]
    ws.feed(ev("task-started", tid), b"\x01\x00", ev("task-finished", tid))


async def test_concurrent_tasks_use_separate_connections(monkeypatch):
    a, b = TtsWS(auto=False), TtsWS(auto=False)
    tts, seen = make(monkeypatch, [a, b])
    t1 = asyncio.create_task(collect(tts, "one", "r:0"))
    t2 = asyncio.create_task(collect(tts, "two", "r:1"))
    await settle()
    assert seen["connects"] == 2 and a.actions() == b.actions() == ["run-task"]
    finish_manually(b)
    finish_manually(a)
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1[-1].is_last and r2[-1].is_last
    assert a.actions() == b.actions() == ["run-task", "continue-task", "finish-task"]
    # both idle now; the next sentence reuses one of them
    a.auto = b.auto = True
    await collect(tts, "three", "r:2")
    assert seen["connects"] == 2 and len(tts._idle) == 2


async def test_pool_keeps_at_most_two_idle(monkeypatch):
    socks = [TtsWS(auto=False) for _ in range(3)]
    tts, seen = make(monkeypatch, socks)
    tasks = [asyncio.create_task(collect(tts, str(i), f"r:{i}")) for i in range(3)]
    await settle()
    assert seen["connects"] == 3
    for s in socks:
        finish_manually(s)
    await asyncio.gather(*tasks)
    await settle()
    assert sum(s.closed for s in socks) == 1 and len(tts._idle) == 2


async def test_flush_mid_stream_stops_and_drains_before_reuse(monkeypatch):
    ws = TtsWS(auto=False)
    tts, seen = make(monkeypatch, [ws])
    gen = tts.synthesize("a long sentence", "r:0")
    pull = asyncio.ensure_future(gen.__anext__())
    await settle()
    tid = ws.sent_json[0]["header"]["task_id"]
    ws.feed(ev("task-started", tid), b"\x01\x00")
    first = await pull
    assert first.pcm16 == b"\x01\x00" and not first.is_last
    await tts.flush("r:0")
    ws.feed(b"\x02\x00")
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()  # nothing more is yielded
    await settle()
    assert ws.actions().count("finish-task") == 1  # already sent; not repeated
    assert tts._idle == [] and not ws.closed  # task still running: not pooled
    ws.feed(b"\x03\x00", ev("task-finished", tid))
    await settle()
    assert [w for w, _ in tts._idle] == [ws] and not ws.closed
    # the clean connection serves the next request
    ws.auto = True
    assert (await collect(tts, "next", "r:1"))[-1].is_last
    assert seen["connects"] == 1


async def test_cancel_before_task_started_sends_finish_and_drains(monkeypatch):
    ws = TtsWS(auto=False)
    tts, _ = make(monkeypatch, [ws])
    task = asyncio.create_task(collect(tts, "x", "r"))
    await settle()
    await tts.flush("r")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await settle()
    assert ws.actions() == ["run-task", "finish-task"]  # no text sent
    assert tts._idle == []
    ws.feed(ev("task-started"), ev("task-finished"))
    await settle()
    assert [w for w, _ in tts._idle] == [ws]


async def test_drain_timeout_closes_connection(monkeypatch):
    monkeypatch.setattr(aliyun_mod, "_DRAIN_TIMEOUT_S", 0.01)
    ws = TtsWS(auto=False)
    tts, _ = make(monkeypatch, [ws])
    gen = tts.synthesize("x", "r")
    pull = asyncio.ensure_future(gen.__anext__())
    await settle()
    ws.feed(ev("task-started"), b"\x01\x00")
    await pull
    await gen.aclose()  # consumer stops early; server never finishes
    await asyncio.sleep(0.05)
    assert ws.closed and tts._idle == []


async def test_task_failed_raises_and_drops_connection(monkeypatch):
    ws = TtsWS(auto=False)
    tts, _ = make(monkeypatch, [ws])
    ws.feed(ev("task-started"), b"\x01\x00", ev("task-failed", error_code="InvalidParameter", error_message="bad voice"))
    with pytest.raises(RuntimeError, match="bad voice"):
        await collect(tts, "x", "r")
    await settle()
    assert ws.closed and tts._idle == []


async def test_task_failed_before_start_raises(monkeypatch):
    ws = TtsWS(auto=False)
    tts, _ = make(monkeypatch, [ws])
    ws.feed(ev("task-failed", error_code="ModelNotFound", error_message="no such model"))
    with pytest.raises(RuntimeError, match="no such model"):
        await collect(tts, "x", "r")
    assert ws.actions() == ["run-task"]


async def test_send_failure_raises_without_retry(monkeypatch):
    ws, spare = TtsWS(), TtsWS()
    tts, seen = make(monkeypatch, [ws, spare])
    await tts.warmup()
    ws.fail_send = True
    with pytest.raises(ConnectionError):
        await collect(tts, "x", "r")
    await settle()
    assert seen["connects"] == 1 and ws.closed and tts._idle == []


async def test_dead_pooled_connection_is_replaced(monkeypatch):
    old, new = TtsWS(), TtsWS()
    tts, seen = make(monkeypatch, [old, new])
    await collect(tts, "one", "r:0")
    old.closed = True  # server closed it while idle
    await collect(tts, "two", "r:1")
    assert seen["connects"] == 2 and new.actions() == ["run-task", "continue-task", "finish-task"]


async def test_warmup_opens_connection_used_by_first_request(monkeypatch):
    ws = TtsWS()
    tts, seen = make(monkeypatch, [ws])
    await tts.warmup()
    assert seen["connects"] == 1 and ws.sent == []
    await tts.warmup()  # already warm
    await collect(tts, "x", "r")
    assert seen["connects"] == 1
    await tts.close()
    assert ws.closed and tts._idle == []


async def test_voice_change_applies_to_next_task(monkeypatch):
    ws = TtsWS()
    tts, _ = make(monkeypatch, [ws])
    await collect(tts, "one", "r:0")
    tts.voice = "longwan_v2"
    await collect(tts, "two", "r:1")
    voices = [m["payload"]["parameters"]["voice"] for m in ws.sent_json if m["header"]["action"] == "run-task"]
    assert voices == ["longxiaochun_v2", "longwan_v2"]


def test_missing_key_and_bad_rate(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(ProviderConfigError):
        AliyunStreamingTTS({})
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-env")
    assert AliyunStreamingTTS({}).headers()["Authorization"] == "Bearer sk-env"
    with pytest.raises(ProviderConfigError):
        AliyunStreamingTTS({"sample_rate": 11025})
