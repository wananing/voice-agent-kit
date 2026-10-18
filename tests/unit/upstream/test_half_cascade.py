"""Half-cascade pipeline (spec 04 §6; handbook half-cascade.md)."""

from __future__ import annotations

import asyncio

from vak.core.types import ReplyModality
from vak.upstream.base import AckCode, AudioMeta, EventType, ResponseStatus
from vak.upstream.pipelines.half_cascade import NO_TRANSCRIPT_PLACEHOLDER
from vak.upstream.providers.mock import MockReply

from .helpers import T1, TOOL_CALL, build, drain, frame, make_init, of, send_frames, settle


async def test_whole_utterance_accumulates_and_submits_at_commit_with_side_transcript() -> None:
    b = build("half_cascade", script=[MockReply(text="现在三点。")])
    assert b.up.capabilities.get("uplink.streaming_input") == "whole_utterance"
    assert b.up.capabilities.get("cascade.user_transcript") == "side_request"
    await b.up.open(make_init())
    await send_frames(b.up, n=4)
    assert b.llm.fed == b""  # nothing fed live
    ack = await b.up.commit_turn(T1, "commit")
    assert ack.ok and ack.data["text"] == "现在几点了"
    assert b.up._history[-1].content == "现在几点了"  # history stored as text
    await b.up.request_response(T1, "rr-1")
    await settle(60)
    turn = b.llm.audio_turns[0]
    assert turn is not None and len(turn.pcm16) == 4 * len(frame()) and turn.transcript_hint == "现在几点了"
    evs = drain(b.up)
    assert of(evs, EventType.TRANSCRIPT_USER_FINAL)
    assert of(evs, EventType.RESPONSE_DONE)[0].reply_modality is ReplyModality.AUDIO
    # the second request of the session carries no audio
    await b.up.request_response(T1, "rr-2")
    await settle(60)
    assert b.llm.audio_turns[1] is None


async def test_backpressure_event_when_buffer_exceeds_limit_without_dropping() -> None:
    b = build("half_cascade", backpressure_ms=250)
    await b.up.open(make_init())
    await send_frames(b.up, n=4)  # 400 ms
    evs = drain(b.up)
    bp = of(evs, EventType.UPLINK_BACKPRESSURE)
    assert len(bp) == 1 and bp[0].buffered_ms == 300
    await b.up.commit_turn(T1, "commit")
    assert len(b"".join(b.up._pending_audio.chunks if hasattr(b.up._pending_audio, "chunks") else [b.up._pending_audio.pcm16])) == 4 * len(frame())


async def test_streaming_input_feeds_live() -> None:
    b = build("half_cascade", streaming_input=True, no_side_stt=True)
    assert b.up.capabilities.get("uplink.streaming_input") == "streaming"
    await b.up.open(make_init())
    await send_frames(b.up, n=3)
    assert len(b.llm.fed) == 3 * len(frame())
    assert (await b.up.commit_turn(T1, "commit")).ok
    assert b.up._history[-1].content == NO_TRANSCRIPT_PLACEHOLDER
    assert b.up.capabilities.get("cascade.user_transcript") == "none"


async def test_side_stt_timeout_falls_back_to_partial_with_ack_timeout() -> None:
    b = build("half_cascade", final_on_finish=False)
    await b.up.open(make_init())
    await send_frames(b.up, n=4)
    task = asyncio.create_task(b.up.commit_turn(T1, "commit"))
    await settle()
    await b.clock.advance(b.up.timeouts.stt_final_ms + 1)
    ack = await task
    assert ack.code is AckCode.TIMEOUT and ack.data["text"] == "现在"
    evs = drain(b.up)
    assert of(evs, EventType.ERROR) and of(evs, EventType.TRANSCRIPT_USER_FINAL)[0].final_by_timeout


async def test_tool_call_precedes_audio_p0_15() -> None:
    b = build("half_cascade", script=[MockReply(text="我查一下时间。", tool_call=TOOL_CALL), MockReply(text="三点。")])
    await b.up.open(make_init())
    await send_frames(b.up)
    await b.up.commit_turn(T1, "commit")
    await b.up.request_response(T1, "rr-1")
    await settle(60)
    evs = drain(b.up)
    first = {}
    for i, e in enumerate(evs):
        first.setdefault(e.type, i)
    assert first[EventType.TOOL_CALL] < first[EventType.RESPONSE_AUDIO_DELTA]
    ack = await b.up.send_tool_result("call-1", {"hour": 3}, True)
    assert ack.ok
    await settle(60)
    assert of(drain(b.up), EventType.RESPONSE_DONE)[0].status is ResponseStatus.COMPLETED


async def test_discard_clears_buffer_and_send_audio_rules() -> None:
    b = build("half_cascade")
    await b.up.open(make_init())
    assert (await b.up.send_audio(b"", AudioMeta(None, 0, 0, 0, "vad"))).detail == "turn_id"
    await send_frames(b.up, n=2)
    assert (await b.up.commit_turn(T1, "discard")).ok and b.up._utt is None
    assert b.stts[0].cancelled
    assert (await b.up.send_audio(frame(), AudioMeta(T1, 9, 1, 0, "vad"))).detail == "already_committed"
