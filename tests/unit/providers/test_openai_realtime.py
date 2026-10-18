"""OpenAI Realtime event mapping (spec 04 §2.4) and primitive encoding."""

from __future__ import annotations

import base64
import json

import pytest

from vak.core.clock import FakeClock
from vak.core.types import ErrorClass, ErrorReason, ErrorScope, HistoryItem, ReplyModality
from vak.upstream.base import EventType, ResponseOrigin, ResponseStatus, ToolDef
from vak.upstream.providers._util import ProviderConfigError
from vak.upstream.providers.openai_realtime import OpenAIRealtime, OpenAIRealtimeAudioLLM
from vak.upstream.providers.realtime_common import RealtimeTimeout

from .fakes import FakeWS

PCM_24K_100MS = bytes(2 * 2400)


def make(**cfg) -> tuple[OpenAIRealtime, FakeWS]:
    rt = OpenAIRealtime({"api_key": "k", **cfg}, clock=FakeClock())
    ws = FakeWS()
    rt._ws = ws
    return rt, ws


def ev(rt: OpenAIRealtime, **vendor) -> list:
    return rt._handle_vendor_event(vendor)


# ---------------------------------------------------------------------------
# event mapping, one test per vendor event type listed in the brief
# ---------------------------------------------------------------------------


def test_session_created_and_updated_map_to_session_opened():
    rt, _ = make()
    out = ev(rt, type="session.created", session={"id": "sess_1", "model": "gpt-realtime"})
    assert [e.type for e in out] == [EventType.SESSION_OPENED]
    assert out[0].session_id == "sess_1" and out[0].effective["model"] == "gpt-realtime"
    out = ev(rt, type="session.updated", session={"id": "sess_1", "voice": "marin"})
    assert out[0].type is EventType.SESSION_OPENED and out[0].effective["voice"] == "marin"


def test_speech_started_stopped_source_server_vad():
    rt, _ = make()
    (a,) = ev(rt, type="input_audio_buffer.speech_started", audio_start_ms=120, item_id="i1")
    (b,) = ev(rt, type="input_audio_buffer.speech_stopped", audio_end_ms=1900, item_id="i1")
    assert a.type is EventType.INPUT_SPEECH_STARTED and a.source == "server_vad" and a.audio_ms == 120
    assert b.type is EventType.INPUT_SPEECH_STOPPED and b.source == "server_vad" and b.item_id == "i1"


async def test_committed_by_client_vs_server():
    rt, _ = make()
    (srv,) = ev(rt, type="input_audio_buffer.committed", item_id="i1")
    assert srv.type is EventType.INPUT_COMMITTED and srv.committed_by == "server"
    await rt.commit_input()
    (cli,) = ev(rt, type="input_audio_buffer.committed", item_id="i2")
    assert cli.committed_by == "client"


def test_user_transcription_delta_and_completed():
    rt, _ = make()
    (p,) = ev(rt, type="conversation.item.input_audio_transcription.delta", item_id="i1", delta="你好")
    (f,) = ev(rt, type="conversation.item.input_audio_transcription.completed", item_id="i1", transcript="你好，世界")
    assert p.type is EventType.TRANSCRIPT_USER_PARTIAL and p.text == "你好" and p.is_final is False
    assert f.type is EventType.TRANSCRIPT_USER_FINAL and f.text == "你好，世界" and f.is_final is True


async def test_response_created_origin_client_when_we_asked_else_server_vad():
    rt, ws = make()
    (auto,) = ev(rt, type="response.created", response={"id": "r0"})
    assert auto.type is EventType.RESPONSE_STARTED and auto.origin is ResponseOrigin.SERVER_VAD and auto.response_ref is None
    ref = await rt.create_response({"response_ref": "rr-42"})
    assert ref == "rr-42"
    sent = ws.sent_json[-1]
    assert sent["type"] == "response.create" and sent["response"]["metadata"] == {"vak_response_ref": "rr-42"}
    (mine,) = ev(rt, type="response.created", response={"id": "r1", "metadata": {"vak_response_ref": "rr-42"}})
    assert mine.origin is ResponseOrigin.CLIENT and mine.response_ref == "rr-42" and mine.response_id == "r1"


async def test_response_created_fifo_correlation_without_metadata():
    rt, _ = make(dialect="beta")
    ref = await rt.create_response(None)
    (mine,) = ev(rt, type="response.created", response={"id": "r1"})
    assert mine.origin is ResponseOrigin.CLIENT and mine.response_ref == ref


def test_audio_delta_decodes_pcm_with_item_id_and_duration():
    rt, _ = make()
    b64 = base64.b64encode(PCM_24K_100MS).decode()
    (d,) = ev(rt, type="response.audio.delta", response_id="r1", item_id="it1", delta=b64)
    assert d.type is EventType.RESPONSE_AUDIO_DELTA
    assert d.response_id == "r1" and d.item_id == "it1"
    assert d.audio == PCM_24K_100MS and d.sample_rate == 24000 and d.duration_ms == 100
    # GA name is accepted too
    (d2,) = ev(rt, type="response.output_audio.delta", response_id="r1", item_id="it1", delta=b64)
    assert d2.type is EventType.RESPONSE_AUDIO_DELTA


def test_assistant_transcript_delta_and_done():
    rt, _ = make()
    (d,) = ev(rt, type="response.audio_transcript.delta", response_id="r1", item_id="it1", delta="Hel")
    (f,) = ev(rt, type="response.audio_transcript.done", response_id="r1", item_id="it1", transcript="Hello")
    assert d.type is EventType.TRANSCRIPT_ASSISTANT and d.text == "Hel" and d.is_final is False
    assert f.type is EventType.TRANSCRIPT_ASSISTANT and f.text == "Hello" and f.is_final is True


def test_text_delta_both_dialects():
    rt, _ = make()
    (a,) = ev(rt, type="response.text.delta", response_id="r1", item_id="it1", delta="x")
    (b,) = ev(rt, type="response.output_text.delta", response_id="r1", item_id="it1", delta="y")
    assert a.type is b.type is EventType.RESPONSE_TEXT_DELTA and a.text + b.text == "xy"


def test_tool_call_args_complete_and_before_commit_flag():
    committed = {"v": False}
    rt = OpenAIRealtime({"api_key": "k"}, clock=FakeClock(), is_committed=lambda: committed["v"])
    (call,) = ev(
        rt,
        type="response.function_call_arguments.done",
        response_id="r1",
        item_id="fc1",
        call_id="call_1",
        name="get_time",
        arguments='{"tz": "Asia/Shanghai"}',
    )
    assert call.type is EventType.TOOL_CALL
    assert call.call_id == "call_1" and call.name == "get_time" and call.args == {"tz": "Asia/Shanghai"}
    assert call.before_commit is True and call.response_id == "r1" and call.item_id == "fc1"
    # output_item.done for the same call does not emit a duplicate
    assert ev(rt, type="response.output_item.done", response_id="r1", item={"type": "function_call", "call_id": "call_1", "name": "get_time", "arguments": "{}"}) == []
    committed["v"] = True
    (call2,) = ev(rt, type="response.function_call_arguments.done", response_id="r1", item_id="fc2", call_id="call_2", name="t", arguments="{}")
    assert call2.before_commit is False


def test_tool_call_bad_json_is_benign_tool_parse_error():
    rt, _ = make()
    (err,) = ev(rt, type="response.function_call_arguments.done", response_id="r1", call_id="c", name="t", arguments="{not json")
    assert err.type is EventType.ERROR
    assert err.error.error_class is ErrorClass.BENIGN and err.error.reason is ErrorReason.TOOL_PARSE and err.error.scope is ErrorScope.CALL


def test_response_done_status_modality_and_usage_by_modality():
    rt, _ = make()
    out = ev(
        rt,
        type="response.done",
        response={
            "id": "r1",
            "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "audio", "transcript": "hi"}]}],
            "usage": {
                "input_tokens": 120,
                "output_tokens": 40,
                "input_token_details": {"text_tokens": 100, "audio_tokens": 20, "cached_tokens": 64, "cached_tokens_details": {"text_tokens": 60, "audio_tokens": 4}},
                "output_token_details": {"text_tokens": 10, "audio_tokens": 30},
            },
        },
    )
    assert [e.type for e in out] == [EventType.RESPONSE_DONE, EventType.USAGE]
    done, usage = out
    assert done.status is ResponseStatus.COMPLETED and done.reply_modality is ReplyModality.AUDIO and done.confirmed is True
    u = usage.usage
    assert (u.input_text_tokens, u.input_audio_tokens, u.cached_input_tokens) == (100, 20, 64)
    assert (u.cached_text_tokens, u.cached_audio_tokens, u.output_text_tokens, u.output_audio_tokens) == (60, 4, 10, 30)


@pytest.mark.parametrize(
    "content,expected",
    [
        ([{"type": "text", "text": "x"}], ReplyModality.TEXT_ONLY),
        ([{"type": "output_text", "text": "x"}], ReplyModality.TEXT_ONLY),
        ([{"type": "audio"}, {"type": "text"}], ReplyModality.MIXED),
        ([], ReplyModality.NONE),
    ],
)
def test_response_done_reply_modality_from_output_items(content, expected):
    rt, _ = make()
    done = ev(rt, type="response.done", response={"id": "r", "status": "completed", "output": [{"type": "message", "content": content}]})[0]
    assert done.reply_modality is expected


def test_response_done_function_call_only_is_modality_none():
    rt, _ = make()
    done = ev(rt, type="response.done", response={"id": "r", "status": "completed", "output": [{"type": "function_call", "name": "t"}]})[0]
    assert done.reply_modality is ReplyModality.NONE


def test_response_done_failed_emits_benign_response_error():
    rt, _ = make()
    rt._configured = True
    out = ev(rt, type="response.done", response={"id": "r", "status": "failed", "status_details": {"error": {"code": "server_error", "message": "boom"}}, "output": []})
    assert out[0].status is ResponseStatus.FAILED
    assert out[1].type is EventType.ERROR and out[1].error.error_class is ErrorClass.BENIGN and out[1].error.scope is ErrorScope.RESPONSE


@pytest.mark.parametrize(
    "code,cls,reason",
    [
        ("invalid_api_key", ErrorClass.FATAL, ErrorReason.AUTH),
        ("insufficient_quota", ErrorClass.FATAL, ErrorReason.QUOTA),
        ("session_expired", ErrorClass.TRANSIENT, ErrorReason.SESSION_LOST),
        ("rate_limit_exceeded", ErrorClass.TRANSIENT, ErrorReason.THROTTLED),
        ("response_cancel_not_active", ErrorClass.BENIGN, ErrorReason.CANCEL_RACE),
        ("conversation_already_has_active_response", ErrorClass.BENIGN, ErrorReason.CANCEL_RACE),
        ("input_audio_buffer_commit_empty", ErrorClass.BENIGN, ErrorReason.EMPTY_COMMIT),
    ],
)
def test_error_code_map(code, cls, reason):
    rt, _ = make()
    rt._configured = True
    (e,) = ev(rt, type="error", error={"type": "invalid_request_error", "code": code, "message": "m"})
    assert e.type is EventType.ERROR and e.error.error_class is cls and e.error.reason is reason and e.error.raw_code == code


def test_error_before_session_configured_is_fatal_invalid_config():
    rt, _ = make()
    (e,) = ev(rt, type="error", error={"code": "unknown_parameter", "message": "Unknown parameter: 'session.foo'"})
    assert e.error.error_class is ErrorClass.FATAL and e.error.reason is ErrorReason.INVALID_CONFIG


def test_unmapped_error_with_event_id_is_benign_item_scope():
    rt, _ = make()
    rt._configured = True
    (e,) = ev(rt, type="error", error={"code": "weird_new_code", "message": "?", "event_id": "evt_7"})
    assert e.error.error_class is ErrorClass.BENIGN and e.error.scope is ErrorScope.ITEM and e.error.ref["event_id"] == "evt_7"


def test_unmapped_error_without_correlation_is_transient_unknown():
    rt, _ = make()
    rt._configured = True
    (e,) = ev(rt, type="error", error={"code": "weird", "message": "nothing matches"})
    assert e.error.error_class is ErrorClass.TRANSIENT and e.error.reason is ErrorReason.UNKNOWN


def test_item_created_echoes_local_id_for_seeded_history():
    rt, _ = make()
    item = rt._history_item(HistoryItem(role="user", text="hi", local_entry_id="ent-9"))
    (echo,) = ev(rt, type="conversation.item.created", item={"id": item["id"], "type": "message"})
    assert echo.type is EventType.ITEM_ECHO and echo.local_id == "ent-9" and echo.kind == "message"


# ---------------------------------------------------------------------------
# cancel: zero deltas after cancel (spec 04 §3.5, acceptance 6)
# ---------------------------------------------------------------------------


async def test_cancel_drops_late_deltas_and_reports_cancelled():
    rt, ws = make()
    ev(rt, type="response.created", response={"id": "r1"})
    await rt.cancel_response("r1")
    assert ws.sent_json[-1] == {"type": "response.cancel", "response_id": "r1"}
    late = ev(rt, type="response.audio.delta", response_id="r1", item_id="i", delta=base64.b64encode(b"\0\0").decode())
    assert late == [] and rt.dropped_after_cancel == 1
    assert ev(rt, type="response.text.delta", response_id="r1", item_id="i", delta="late") == []
    (done,) = ev(rt, type="response.done", response={"id": "r1", "status": "cancelled", "output": []})
    assert done.status is ResponseStatus.CANCELLED
    # a new response is forwarded again
    assert ev(rt, type="response.audio.delta", response_id="r2", item_id="i", delta=base64.b64encode(b"\0\0").decode())


# ---------------------------------------------------------------------------
# primitives -> wire
# ---------------------------------------------------------------------------


async def test_append_audio_resamples_16k_to_24k_and_base64s():
    rt, ws = make()
    await rt.append_audio(bytes(2 * 1600), 16000)  # 100 ms at 16 kHz
    msg = ws.sent_json[-1]
    assert msg["type"] == "input_audio_buffer.append"
    assert len(base64.b64decode(msg["audio"])) == 2 * 2400  # 100 ms at 24 kHz


async def test_commit_clear_truncate_delete_tool_output():
    rt, ws = make()
    await rt.commit_input()
    await rt.clear_input()
    await rt.truncate_item("it1", 1234)
    await rt.truncate_item("it2", 0)
    await rt.add_tool_output("call_1", '{"ok": true}')
    types = [m["type"] for m in ws.sent_json]
    assert types == [
        "input_audio_buffer.commit",
        "input_audio_buffer.clear",
        "conversation.item.truncate",
        "conversation.item.delete",
        "conversation.item.create",
    ]
    assert ws.sent_json[2] == {"type": "conversation.item.truncate", "item_id": "it1", "content_index": 0, "audio_end_ms": 1234}
    out = ws.sent_json[4]["item"]
    assert out == {"type": "function_call_output", "call_id": "call_1", "output": '{"ok": true}'}
    # silent close by omission: no response.create was sent
    assert "response.create" not in types


async def test_create_response_overrides_ga_dialect():
    rt, ws = make()
    tool = ToolDef(name="t", description="d", params={"type": "object"})
    await rt.create_response({"instructions": "say hi", "modalities": ["text"], "tools": [tool], "tool_choice": "t"})
    body = ws.sent_json[-1]["response"]
    assert body["instructions"] == "say hi" and body["output_modalities"] == ["text"] and body["tool_choice"] == "t"
    assert body["tools"] == [{"type": "function", "name": "t", "description": "d", "parameters": {"type": "object"}}]


async def test_update_session_ga_encoding_turn_detection_null_and_transcription():
    rt, ws = make()
    await rt.update_session({"turn_detection": None, "instructions": "I", "voice": "marin", "modalities": ["audio"], "input_audio_transcription": {"model": "gpt-4o-mini-transcribe"}, "tools": []})
    s = ws.sent_json[-1]["session"]
    assert s["type"] == "realtime" and s["instructions"] == "I" and s["output_modalities"] == ["audio"] and s["tools"] == []
    assert s["audio"]["input"]["turn_detection"] is None
    assert s["audio"]["input"]["transcription"] == {"model": "gpt-4o-mini-transcribe"}
    assert s["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert s["audio"]["output"]["voice"] == "marin"


async def test_update_session_beta_encoding_semantic_vad():
    rt, ws = make(dialect="beta")
    await rt.update_session({"turn_detection": {"type": "semantic_vad", "create_response": False}, "voice": "alloy"})
    s = ws.sent_json[-1]["session"]
    assert s["turn_detection"] == {"type": "semantic_vad", "create_response": False}
    assert s["voice"] == "alloy" and s["input_audio_format"] == "pcm16" and "audio" not in s


def test_history_items_encoding():
    rt, _ = make()
    items = [
        rt._history_item(HistoryItem(role="user", text="u")),
        rt._history_item(HistoryItem(role="assistant", text="a")),
        rt._history_item(HistoryItem(role="tool_call", text="", tool_call_id="c1", tool_name="get_time", tool_args={"tz": "x"})),
        rt._history_item(HistoryItem(role="tool_output", text='{"ok":true}', tool_call_id="c1")),
    ]
    assert items[0]["type"] == "message" and items[0]["content"] == [{"type": "input_text", "text": "u"}] and items[0]["id"].startswith("msg_")
    assert items[1]["content"] == [{"type": "output_text", "text": "a"}]
    assert items[2]["type"] == "function_call" and items[2]["id"].startswith("fc_") and json.loads(items[2]["arguments"]) == {"tz": "x"}
    assert items[3] == {"type": "function_call_output", "id": items[3]["id"], "call_id": "c1", "output": '{"ok":true}'}


# ---------------------------------------------------------------------------
# connect: created -> session.update -> updated -> history before any audio
# ---------------------------------------------------------------------------


async def test_connect_sequence_and_history_seeding(monkeypatch):
    clock = FakeClock()
    rt = OpenAIRealtime({"api_key": "k", "turn_detection": None}, clock=clock)
    ws = FakeWS()

    async def fake_connect(url, headers):
        assert url == "wss://api.openai.com/v1/realtime?model=gpt-realtime"
        assert headers["Authorization"] == "Bearer k"
        ws.feed(json.dumps({"type": "session.created", "session": {"id": "s1"}}))
        return ws

    monkeypatch.setattr(rt, "_ws_connect", fake_connect)

    async def answer_update():
        while not any(m["type"] == "session.update" for m in ws.sent_json):
            await asyncio.sleep(0)
        ws.feed(json.dumps({"type": "session.updated", "session": {"id": "s1"}}))

    import asyncio

    task = asyncio.create_task(answer_update())
    await rt.connect({"instructions": "I", "history": [HistoryItem(role="user", text="earlier")]})
    await task
    types = [m["type"] for m in ws.sent_json]
    assert types == ["session.update", "conversation.item.create"]
    assert ws.sent_json[0]["session"]["audio"]["input"]["turn_detection"] is None
    assert rt.session_id == "s1"
    await rt.disconnect()
    assert ws.closed


async def test_connect_timeout_uses_injected_clock(monkeypatch):
    clock = FakeClock()
    rt = OpenAIRealtime({"api_key": "k", "connect_timeout_ms": 500}, clock=clock)
    ws = FakeWS()

    async def fake_connect(url, headers):
        return ws

    monkeypatch.setattr(rt, "_ws_connect", fake_connect)
    import asyncio

    task = asyncio.create_task(rt.connect({}))
    for _ in range(5):  # let the clock.sleep waiter register before advancing
        await asyncio.sleep(0)
    await clock.advance(600)
    with pytest.raises(RealtimeTimeout):
        await task
    await rt.disconnect()


def test_missing_api_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rt = OpenAIRealtime({}, clock=FakeClock())
    with pytest.raises(ProviderConfigError, match="OPENAI_API_KEY"):
        rt._headers(rt.config)


# ---------------------------------------------------------------------------
# half-cascade text-modality wrapper
# ---------------------------------------------------------------------------


async def test_audio_llm_wrapper_yields_text_tool_call_and_done():
    import asyncio

    from vak.upstream.providers.base import AudioTurn, ChatMessage

    llm = OpenAIRealtimeAudioLLM({"api_key": "k"}, clock=FakeClock())
    rt = llm.session
    ws = FakeWS()
    rt._ws = ws
    llm._connected = True  # skip the socket handshake; exercise the event loop

    async def drive():
        while not any(m["type"] == "response.create" for m in ws.sent_json):
            await asyncio.sleep(0)
        ref = ws.sent_json[-1]["response"]["metadata"]["vak_response_ref"]
        rt._handle_vendor_event({"type": "response.created", "response": {"id": "r1", "metadata": {"vak_response_ref": ref}}})
        rt._handle_vendor_event({"type": "response.output_text.delta", "response_id": "r1", "item_id": "i", "delta": "Hel"})
        rt._handle_vendor_event({"type": "response.output_text.delta", "response_id": "r1", "item_id": "i", "delta": "lo"})
        rt._handle_vendor_event({"type": "response.function_call_arguments.done", "response_id": "r1", "call_id": "c1", "name": "t", "arguments": '{"a":1}'})
        rt._handle_vendor_event({"type": "response.done", "response": {"id": "r1", "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text"}]}], "usage": {"input_token_details": {"text_tokens": 3}}}})

    task = asyncio.create_task(drive())
    deltas = [d async for d in llm.stream([ChatMessage(role="system", content="S")], AudioTurn(pcm16=bytes(3200), sample_rate=16000))]
    await task
    assert [d.text for d in deltas if d.text] == ["Hel", "lo"]
    assert [d.tool_call for d in deltas if d.tool_call] == [{"id": "c1", "name": "t", "arguments": {"a": 1}}]
    assert deltas[-1].done and deltas[-1].finish_reason == "completed" and deltas[-1].usage == {"input_text_tokens": 3}
    types = [m["type"] for m in ws.sent_json]
    assert types == ["input_audio_buffer.append", "input_audio_buffer.commit", "response.create"]
    assert ws.sent_json[-1]["response"]["output_modalities"] == ["text"]
