"""DashScope dialect: same mapping as OpenAI plus the documented differences."""

from __future__ import annotations

import base64

import pytest

from vak.core.clock import FakeClock
from vak.core.types import ErrorClass, ErrorReason, ReplyModality
from vak.upstream.base import EventType, ResponseOrigin, ResponseStatus
from vak.upstream.providers.dashscope_realtime import DashScopeRealtime

from .fakes import FakeWS


def make(**cfg) -> tuple[DashScopeRealtime, FakeWS]:
    rt = DashScopeRealtime({"api_key": "k", **cfg}, clock=FakeClock())
    rt._ws = FakeWS()
    return rt, rt._ws


def test_url_and_headers():
    rt, _ = make(model="qwen3.5-omni-flash-realtime")
    assert rt._url(rt.config) == "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3.5-omni-flash-realtime"
    h = rt._headers(rt.config)
    assert h == {"Authorization": "Bearer k"}  # no OpenAI-Beta header


async def test_session_update_beta_flat_pcm_and_semantic_vad():
    rt, ws = make()
    await rt.update_session(rt._session_patch(rt.config))
    s = ws.sent_json[-1]["session"]
    assert s["turn_detection"] == {"type": "semantic_vad"}
    assert s["input_audio_format"] == "pcm" and s["output_audio_format"] == "pcm"
    assert s["modalities"] == ["audio", "text"] and "audio" not in s


async def test_session_update_nested_audio_for_qwen38():
    rt, ws = make(nested_audio_config=True, voice="Tina")
    await rt.update_session(rt._session_patch(rt.config))
    s = ws.sent_json[-1]["session"]
    assert s["audio"] == {
        "input": {"format": {"type": "pcm", "sample_rate": 16000}},
        "output": {"format": {"type": "pcm", "sample_rate": 24000}, "voice": "Tina"},
    }
    assert "voice" not in s and "input_audio_format" not in s


async def test_turn_detection_null_is_sent_as_null():
    rt, ws = make()
    await rt.update_session({"turn_detection": None})
    assert ws.sent_json[-1]["session"]["turn_detection"] is None


async def test_append_audio_keeps_16k():
    rt, ws = make()
    await rt.append_audio(bytes(3200), 16000)
    assert len(base64.b64decode(ws.sent_json[-1]["audio"])) == 3200


async def test_response_create_without_metadata_and_cancel_without_id():
    rt, ws = make()
    ref = await rt.create_response({"instructions": "x", "modalities": ["audio"]})
    body = ws.sent_json[-1]["response"]
    assert body == {"instructions": "x", "modalities": ["audio"]}
    (started,) = rt._handle_vendor_event({"type": "response.created", "response": {"id": "r1"}})
    assert started.origin is ResponseOrigin.CLIENT and started.response_ref == ref
    await rt.cancel_response(None)
    assert ws.sent_json[-1] == {"type": "response.cancel"}
    (done,) = rt._handle_vendor_event({"type": "response.done", "response": {"id": "r1", "status": "cancelled", "output": []}})
    assert done.status is ResponseStatus.CANCELLED


def test_every_mapped_event_type():
    rt, _ = make()
    rt._configured = True
    b64 = base64.b64encode(bytes(480)).decode()
    seq = [
        ({"type": "session.created", "session": {"id": "s"}}, EventType.SESSION_OPENED),
        ({"type": "session.updated", "session": {"id": "s"}}, EventType.SESSION_OPENED),
        ({"type": "input_audio_buffer.speech_started", "audio_start_ms": 0}, EventType.INPUT_SPEECH_STARTED),
        ({"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 900}, EventType.INPUT_SPEECH_STOPPED),
        ({"type": "input_audio_buffer.committed", "item_id": "i"}, EventType.INPUT_COMMITTED),
        ({"type": "conversation.item.input_audio_transcription.text", "item_id": "i", "text": "你"}, EventType.TRANSCRIPT_USER_PARTIAL),
        ({"type": "conversation.item.input_audio_transcription.delta", "item_id": "i", "delta": "好"}, EventType.TRANSCRIPT_USER_PARTIAL),
        ({"type": "conversation.item.input_audio_transcription.completed", "item_id": "i", "transcript": "你好"}, EventType.TRANSCRIPT_USER_FINAL),
        ({"type": "conversation.item.created", "item": {"id": "srv_1", "type": "message"}}, EventType.ITEM_ECHO),
        ({"type": "response.created", "response": {"id": "r"}}, EventType.RESPONSE_STARTED),
        ({"type": "response.audio.delta", "response_id": "r", "item_id": "i", "delta": b64}, EventType.RESPONSE_AUDIO_DELTA),
        ({"type": "response.audio_transcript.delta", "response_id": "r", "item_id": "i", "delta": "a"}, EventType.TRANSCRIPT_ASSISTANT),
        ({"type": "response.audio_transcript.done", "response_id": "r", "item_id": "i", "transcript": "a"}, EventType.TRANSCRIPT_ASSISTANT),
        ({"type": "response.text.delta", "response_id": "r", "item_id": "i", "delta": "t"}, EventType.RESPONSE_TEXT_DELTA),
        ({"type": "response.function_call_arguments.done", "response_id": "r", "call_id": "c", "name": "f", "arguments": "{}"}, EventType.TOOL_CALL),
        ({"type": "error", "error": {"message": "another response is in progress"}}, EventType.ERROR),
    ]
    for vendor, expected in seq:
        out = rt._handle_vendor_event(vendor)
        assert out and out[0].type is expected, vendor["type"]
    (first_tx,) = rt._handle_vendor_event({"type": "conversation.item.input_audio_transcription.text", "item_id": "i", "text": "x"})
    assert first_tx.text == "x"
    done = rt._handle_vendor_event({"type": "response.done", "response": {"id": "r", "status": "completed", "output": [{"type": "message", "content": [{"type": "audio"}]}]}})
    assert done[0].type is EventType.RESPONSE_DONE and done[0].reply_modality is ReplyModality.AUDIO
    assert len(done) == 1  # no usage reported -> no usage event (usage.cache_reported none)


def test_item_echo_no_local_id_for_server_assigned_id():
    rt, _ = make()
    (echo,) = rt._handle_vendor_event({"type": "conversation.item.created", "item": {"id": "srv_1", "type": "message"}})
    assert echo.local_id is None


@pytest.mark.parametrize(
    "message,cls,reason",
    [
        ("Invalid API-key provided", ErrorClass.FATAL, ErrorReason.AUTH),
        ("Access denied, account arrearage", ErrorClass.FATAL, ErrorReason.BILLING),
        ("AllocationQuota.FreeTierOnly: free allocated quota exceeded", ErrorClass.FATAL, ErrorReason.QUOTA),
        ("Model.AccessDenied", ErrorClass.FATAL, ErrorReason.INVALID_CONFIG),
        ("Cannot create response while another response is in progress", ErrorClass.BENIGN, ErrorReason.CANCEL_RACE),
        ("Cannot cancel: user is speaking", ErrorClass.BENIGN, ErrorReason.CANCEL_RACE),
        ("DataInspectionFailed: input data may contain inappropriate content", ErrorClass.BENIGN, ErrorReason.MODERATION),
        ("Throttling.RateQuota", ErrorClass.TRANSIENT, ErrorReason.THROTTLED),
        ("something entirely new", ErrorClass.TRANSIENT, ErrorReason.UNKNOWN),
    ],
)
def test_error_message_match(message, cls, reason):
    rt, _ = make()
    rt._configured = True
    (e,) = rt._handle_vendor_event({"type": "error", "error": {"code": "", "message": message}})
    assert e.error.error_class is cls and e.error.reason is reason and e.error.raw_message == message
