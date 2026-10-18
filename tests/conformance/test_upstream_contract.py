"""Spec 04 §7 / §8.1 acceptance items that need no real upstream, run over
all four pipelines on mock providers (architecture §7 items 9–10)."""

from __future__ import annotations

import asyncio
import random
import re
from pathlib import Path

import pytest

from tests.unit.upstream.helpers import (
    T1,
    TOOL_CALL,
    Built,
    build,
    drain,
    make_init,
    of,
    send_frames,
    settle,
    until,
)
from vak.core.clock import FakeClock
from vak.core.types import ErrorReason
from vak.upstream.base import (
    AckCode,
    AudioMeta,
    ConfigPatch,
    EventType,
    ResponseOverrides,
    ResponseStatus,
    Upstream,
)
from vak.upstream.events import DELTA_TYPES
from vak.upstream.providers.mock import MockReply, silence_pcm, tone_pcm

PIPELINES = ["cascade", "half_cascade", "s2s", "full_duplex"]
VAK = Path(__file__).resolve().parents[2] / "vak"


async def run_turn(b: Built, reply_ref: str = "rr-1") -> list:
    """Open, one user turn, one response, drained events."""
    init = make_init(tools=[] if b.name == "full_duplex" else None)
    assert (await b.up.open(init)).ok
    await send_frames(b.up, sr=24000 if b.name == "full_duplex" else 16000)
    ack = await b.up.commit_turn(T1, "commit")
    assert ack.ok, ack
    await b.up.request_response(T1, reply_ref)
    await settle(80)
    return drain(b.up)


@pytest.mark.parametrize("name", PIPELINES)
async def test_implements_upstream_protocol(name: str) -> None:
    b = build(name)
    assert isinstance(b.up, Upstream)


@pytest.mark.parametrize("name", PIPELINES)
async def test_event_flow_invariants_deltas_ids_and_started_done_pairing(name: str) -> None:
    b = build(name, script=[MockReply(text="好的，我在。这是第二句。")], bursts=[(400, 800, "x")])
    evs = await run_turn(b)
    deltas = [e for e in evs if e.type in DELTA_TYPES]
    assert deltas
    assert all(e.response_id and e.item_id for e in deltas)
    assert all(e.duration_ms and e.sample_rate for e in evs if e.type is EventType.RESPONSE_AUDIO_DELTA)
    started = [e.response_id for e in of(evs, EventType.RESPONSE_STARTED)]
    done = [e.response_id for e in of(evs, EventType.RESPONSE_DONE)]
    assert started and sorted(started) == sorted(done) and len(set(done)) == len(done)
    for e in evs:
        assert e.seq > 0
    assert [e.seq for e in evs] == sorted(e.seq for e in evs)
    assert (await b.up.close("t")).ok
    closed = drain(b.up)
    assert closed[-1].type is EventType.SESSION_CLOSED
    assert [e.response_id for e in of(closed, EventType.RESPONSE_DONE)] == []  # nothing left dangling


@pytest.mark.parametrize("name", ["cascade", "half_cascade", "s2s"])
async def test_zero_deltas_after_cancel_ack_with_late_delta_injection(name: str) -> None:
    b = build(name, script=[MockReply(text="第一句话说完了。第二句话还在说。第三句。", audio_deltas=6)], late=True, delta_interval_ms=50)
    init = make_init()
    assert (await b.up.open(init)).ok
    await send_frames(b.up)
    await b.up.commit_turn(T1, "commit")
    rid = (await b.up.request_response(T1, "rr-1")).data["response_id"]
    if name == "s2s":
        await b.clock.advance(120)
        assert of(drain(b.up), EventType.RESPONSE_AUDIO_DELTA)
    else:
        await until(b.up, EventType.RESPONSE_AUDIO_DELTA)
    ack = await b.up.cancel_response(rid)
    assert ack.ok
    await settle(20)
    done = [e for e in drain(b.up) if e.response_id == rid]
    assert [e.type for e in done] == [EventType.RESPONSE_DONE] and done[0].status is ResponseStatus.CANCELLED
    await b.clock.advance(2000)
    await settle(50)
    assert not [e for e in drain(b.up) if e.response_id == rid and e.type in DELTA_TYPES]


@pytest.mark.parametrize("name", ["cascade", "half_cascade"])
async def test_tool_call_precedes_any_audio_of_its_response(name: str) -> None:
    b = build(name, script=[MockReply(text="我看看时间。", tool_call=TOOL_CALL)])
    evs = await run_turn(b)
    first_audio = next(i for i, e in enumerate(evs) if e.type is EventType.RESPONSE_AUDIO_DELTA)
    first_call = next(i for i, e in enumerate(evs) if e.type is EventType.TOOL_CALL)
    assert first_call < first_audio
    assert of(evs, EventType.TOOL_CALL)[0].args == {}


@pytest.mark.parametrize("name", ["cascade", "half_cascade"])
async def test_guardrail_hook_called_per_sentence_and_block_means_zero_audio(name: str) -> None:
    calls: list[str] = []

    async def hook(rid, turn_id, idx, text, is_last):  # type: ignore[no-untyped-def]
        calls.append(text)
        from vak.upstream.base import GuardrailVerdict

        return GuardrailVerdict("block") if idx == 0 else GuardrailVerdict("allow")

    b = build(name, script=[MockReply(text="第一句要拦。第二句不该出来。")], guardrail=hook)
    evs = await run_turn(b)
    assert calls == ["第一句要拦。"]
    assert not of(evs, EventType.RESPONSE_AUDIO_DELTA)
    done = of(evs, EventType.RESPONSE_DONE)[0]
    assert done.status is ResponseStatus.CANCELLED and done.reason == "guardrail"


@pytest.mark.parametrize("name", PIPELINES)
async def test_every_primitive_timeout_produces_ack_timeout_and_error_event(name: str) -> None:
    clock = FakeClock()
    if name in ("cascade", "half_cascade"):
        b = build(name, clock=clock, final_on_finish=False)
        await b.up.open(make_init())
        await send_frames(b.up)
        task = asyncio.create_task(b.up.commit_turn(T1, "commit"))
        wait = b.up.timeouts.stt_final_ms
    elif name == "s2s":
        b = build(name, clock=clock, ack_commit=False)
        await b.up.open(make_init(history=[]))
        await send_frames(b.up)
        task = asyncio.create_task(b.up.commit_turn(T1, "commit"))
        wait = b.up.timeouts.commit_ack_ms
    else:
        b = build(name, clock=clock)

        async def hang(config):  # type: ignore[no-untyped-def]
            await asyncio.Event().wait()

        b.model.connect = hang  # type: ignore[method-assign]
        task = asyncio.create_task(b.up.open(make_init(tools=[])))
        wait = b.up.timeouts.open_connect_ms
    await settle()
    assert not task.done()
    await clock.advance(wait + 1)
    ack = await task
    assert ack.code is AckCode.TIMEOUT
    errs = of(drain(b.up), EventType.ERROR)
    assert errs and errs[0].error.reason is ErrorReason.TIMEOUT


# -- unsupported combinations return unsupported(<field>) (spec 04 §8.1-4) ---

UNSUPPORTED_CASES = [
    ({"response.client_initiated": "no"}, "request_response", "response.client_initiated"),
    ({"response.overrides": {}}, "request_override", "response.overrides.instructions"),
    ({"tools.auto_reply_after_result": "yes", "tools.silent_close": "none"}, "silent_close", "tools.silent_close"),
    ({"tools.auto_reply_after_result": "unknown", "tools.silent_close": "unknown"}, "silent_close", "tools.silent_close"),
    ({"truncate.mode": "none"}, "truncate", "truncate.mode"),
    ({"config.update.instructions": "next_session"}, "update_live", "config.update.instructions"),
    ({"config.update.voice": "append_only"}, "update_voice", "config.update.voice"),
    ({"config.append_channels": []}, "update_append", "config.append_channels"),
    ({"turn.commit_mode": "server_only"}, "discard", "turn.commit_mode"),
    ({"session.resumption": "none"}, "open_resume", "session.resumption"),
    ({"history.tool_calls": "drop"}, "open_tools_history", "history.tool_calls"),
]


@pytest.mark.parametrize(("overrides", "action", "field"), UNSUPPORTED_CASES)
async def test_s2s_unsupported_table(overrides: dict, action: str, field: str) -> None:
    b = build("s2s", caps_overrides=overrides, script=[MockReply(text="", tool_call=TOOL_CALL, audio_deltas=0)])
    if action == "open_resume":
        ack = await b.up.open(make_init(resume_handle="h"))
    elif action == "open_tools_history":
        from vak.core.types import HistoryItem

        ack = await b.up.open(make_init(history=[HistoryItem("tool_call", "", tool_call_id="c")]))
    else:
        assert (await b.up.open(make_init(history=[]))).ok
        await settle()
        if action == "request_response":
            ack = await b.up.request_response(T1, "r")
        elif action == "request_override":
            ack = await b.up.request_response(T1, "r", ResponseOverrides(instructions="x"))
        elif action == "silent_close":
            await b.up.request_response(T1, "r")
            await settle(20)
            ack = await b.up.send_tool_result("call-1", {}, False)
            assert not b.provider.tool_outputs
        elif action == "truncate":
            ack = await b.up.truncate("i", 10)
        elif action == "update_live":
            ack = await b.up.update_config(ConfigPatch(instructions="x"), "live")
        elif action == "update_voice":
            ack = await b.up.update_config(ConfigPatch(voice="x"), "live")
        elif action == "update_append":
            ack = await b.up.update_config(ConfigPatch(instructions="x"), "append")
        else:
            ack = await b.up.commit_turn(T1, "discard")
    assert ack.code is AckCode.UNSUPPORTED and ack.detail == field
    assert not b.provider.updates and not b.provider.disconnected  # no silent substitute, no reconnect


FULL_DUPLEX_UNSUPPORTED = [
    ("request_response", "response.client_initiated"),
    ("truncate", "truncate.mode"),
    ("tool_result", "tools.calling"),
    ("update_live", "config.update.instructions"),
    ("discard", "turn.commit_mode"),
]


@pytest.mark.parametrize(("action", "field"), FULL_DUPLEX_UNSUPPORTED)
async def test_full_duplex_unsupported_table(action: str, field: str) -> None:
    b = build("full_duplex", bursts=[])
    assert (await b.up.open(make_init(tools=[]))).ok
    acks = {
        "request_response": lambda: b.up.request_response(T1, "r"),
        "truncate": lambda: b.up.truncate("i", 0),
        "tool_result": lambda: b.up.send_tool_result("c", {}, True),
        "update_live": lambda: b.up.update_config(ConfigPatch(instructions="x"), "live"),
        "discard": lambda: b.up.commit_turn(T1, "discard"),
    }
    ack = await acks[action]()
    assert ack.code is AckCode.UNSUPPORTED and ack.detail == field


@pytest.mark.parametrize("name", ["cascade", "half_cascade"])
async def test_text_pipelines_unsupported_table(name: str) -> None:
    b = build(name)
    assert (await b.up.open(make_init(resume_handle="h"))).detail == "session.resumption"
    assert (await b.up.open(make_init())).ok
    assert (await b.up.update_config(ConfigPatch(voice="v"), "live")).detail == "config.update.voice"
    assert (await b.up.update_config(ConfigPatch(instructions="v"), "append")).detail == "config.append_channels"


# -- mirror only moves on confirmation (spec 04 rule 5) ------------------------


async def test_mirror_updates_only_on_confirmation_s2s() -> None:
    from vak.core.types import HistoryItem

    b = build("s2s", caps_overrides={"history.item_ack": "no"}, ack_items=False)
    assert (await b.up.open(make_init(history=[HistoryItem("user", "x", local_entry_id="e1")]))).ok
    await settle()
    assert not b.up.mirror.items[0].confirmed
    b.provider.push(b.provider._ev(EventType.ITEM_ECHO, item_id="srv", kind="user", local_id="e1"))
    await settle()
    assert b.up.mirror.items[0].confirmed and b.up.mirror.items[0].item_id == "srv"


# -- static checks (spec 04 §8.1-2/3; architecture §7 items 3–4) ---------------

VENDOR_WORDS = re.compile(r"\b(openai|dashscope|qwen|moshi|funasr|cosyvoice|gemini|doubao|stepfun|minicpm)\b", re.I)


def _py_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_vendor_names_in_pipeline_independent_layers() -> None:
    offenders = []
    for root in (VAK / "core", VAK / "transport"):
        for p in _py_files(root):
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if VENDOR_WORDS.search(line):
                    offenders.append(f"{p.relative_to(VAK)}:{n}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


# identifiers or calls named retry/reconnect, and `while True` loops that (re)connect
RETRY_CODE = re.compile(r"\w*(retr(y|ies)|reconnect)\w*\s*[(=:]", re.I)
WHILE_TRUE = re.compile(r"^\s*while\s+True\s*:")


def test_no_retry_or_reconnect_in_adapter_layer() -> None:
    offenders = []
    targets = [VAK / "upstream" / "pipelines", VAK / "upstream" / "mirror.py", VAK / "upstream" / "events.py",
               VAK / "upstream" / "providers" / "mock.py", VAK / "upstream" / "base.py"]
    for t in targets:
        for p in _py_files(t) if t.is_dir() else [t]:
            lines = p.read_text(encoding="utf-8").splitlines()
            for n, line in enumerate(lines, 1):
                code = line.split("#", 1)[0]
                if RETRY_CODE.search(code):
                    offenders.append(f"{p.relative_to(VAK)}:{n}: {line.strip()}")
                if WHILE_TRUE.search(code) and any("connect" in x for x in lines[n : n + 6]):
                    offenders.append(f"{p.relative_to(VAK)}:{n}: reconnect loop: {line.strip()}")
    assert not offenders, "\n".join(offenders)


# -- randomised timing: cancel never lets a late delta through (≥ 20 runs) ----


@pytest.mark.parametrize("seed", range(20))
async def test_cancel_race_randomised_s2s(seed: int) -> None:
    rnd = random.Random(seed)
    b = build("s2s", late=True, delta_interval_ms=rnd.choice([0, 10, 50]), script=[MockReply(text="x", audio_deltas=rnd.randint(1, 8))])
    assert (await b.up.open(make_init(history=[]))).ok
    rid = (await b.up.request_response(T1, "rr-1")).data["response_id"]
    await b.clock.advance(rnd.randint(0, 200))
    await settle(rnd.randint(0, 5))
    ack = await b.up.cancel_response(rid)
    assert ack.ok
    await settle(10)
    await b.clock.advance(1000)
    await settle(20)
    tail = [e for e in drain(b.up) if e.response_id == rid and e.type is not EventType.USAGE]
    dones = [i for i, e in enumerate(tail) if e.type is EventType.RESPONSE_DONE]
    assert len(dones) == 1  # exactly one done, whether it completed first or was cancelled
    assert all(e.type not in DELTA_TYPES for e in tail[dones[0] + 1 :])  # zero deltas after it


async def test_full_duplex_silence_vs_tone_helpers_consistent() -> None:
    from vak.audio.silence import is_silent

    assert is_silent(silence_pcm(80, 24000)) and not is_silent(tone_pcm(80, 24000))


async def test_send_audio_open_mic_without_turn_is_rejected_except_full_duplex() -> None:
    for name in ("cascade", "half_cascade", "s2s"):
        b = build(name)
        assert (await b.up.open(make_init(history=[]))).ok
        ack = await b.up.send_audio(tone_pcm(20, 16000), AudioMeta(None, 0, 320, 0, "vad"))
        assert ack.code is AckCode.INVALID_STATE and ack.detail == "turn_id"
