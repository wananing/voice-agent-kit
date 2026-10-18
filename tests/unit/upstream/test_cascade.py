"""Cascade pipeline (spec 04 §6, §3)."""

from __future__ import annotations

import asyncio

from vak.core.types import ErrorClass, ErrorReason, HistoryFormat, HistoryItem, ReplyModality, TurnId
from vak.upstream.base import (
    AckCode,
    AudioMeta,
    ConfigPatch,
    EventType,
    GuardrailVerdict,
    ResponseOrigin,
    ResponseOverrides,
    ResponseStatus,
    ToolDef,
)
from vak.upstream.providers.mock import MockReply, MockStreamTTS

from .helpers import T1, T2, TOOL_CALL, build, drain, make_init, of, send_frames, settle, types, until


async def test_open_builds_messages_with_tool_calls_kept_and_state_block_tail() -> None:
    b = build("cascade")
    hist = [
        HistoryItem("user", "几点了", local_entry_id="e1"),
        HistoryItem("tool_call", "", tool_call_id="c0", tool_name="get_time", tool_args={}, local_entry_id="e2"),
        HistoryItem("tool_output", '{"hour": 3}', tool_call_id="c0", tool_name="get_time", local_entry_id="e3"),
        HistoryItem("assistant", "三点。", local_entry_id="e4"),
    ]
    ack = await b.up.open(make_init(history=hist))
    assert ack.ok and ack.data["effective"]["history_items_sent"] == 4
    b.up.set_state_block_tail("当前阶段：导览。")
    msgs = b.up._build_messages(None)
    assert msgs[0].role == "system" and msgs[0].content == "你是向导。"
    assert msgs[2].role == "assistant" and msgs[2].tool_calls[0]["function"]["name"] == "get_time"
    assert msgs[3].role == "tool" and msgs[3].tool_call_id == "c0"
    assert msgs[-1].role == "system" and msgs[-1].content == "当前阶段：导览。"  # spec 05 §2.3: tail
    evs = drain(b.up)
    assert types(evs)[-1] == "session.opened"
    assert len(of(evs, EventType.ITEM_ECHO)) == 4
    assert all(it.confirmed for it in b.up.mirror.items)


async def test_open_rejects_resume_handle_and_double_open() -> None:
    b = build("cascade")
    ack = await b.up.open(make_init(resume_handle="h"))
    assert ack.code is AckCode.UNSUPPORTED and ack.detail == "session.resumption"
    assert (await b.up.open(make_init())).ok
    assert (await b.up.open(make_init())).code is AckCode.INVALID_STATE


async def test_transcripts_partial_then_final_and_commit_appends_user() -> None:
    b = build("cascade")
    await b.up.open(make_init())
    await send_frames(b.up, n=4)
    ack = await b.up.commit_turn(T1, "commit")
    await settle()
    assert ack.ok and ack.data["text"] == "现在几点了"
    evs = drain(b.up)
    assert [e.type for e in evs if e.type.value.startswith("transcript")] == [
        EventType.TRANSCRIPT_USER_PARTIAL,
        EventType.TRANSCRIPT_USER_PARTIAL,
        EventType.TRANSCRIPT_USER_FINAL,
    ]
    assert of(evs, EventType.INPUT_COMMITTED)[0].committed_by == "client"
    assert b.up._history[-1].role == "user" and b.up._history[-1].content == "现在几点了"


async def test_commit_timeout_reports_ack_timeout_error_event_and_final_by_timeout() -> None:
    b = build("cascade", final_on_finish=False)
    await b.up.open(make_init())
    await send_frames(b.up, n=4)
    task = asyncio.create_task(b.up.commit_turn(T1, "commit"))
    await settle()
    assert not task.done()
    await b.clock.advance(b.up.timeouts.stt_final_ms + 1)
    ack = await task
    assert ack.code is AckCode.TIMEOUT and ack.data["final_by_timeout"] is True
    evs = drain(b.up)
    err = of(evs, EventType.ERROR)[0]
    assert err.error.reason is ErrorReason.TIMEOUT and err.error.error_class is ErrorClass.TRANSIENT
    fin = of(evs, EventType.TRANSCRIPT_USER_FINAL)[0]
    assert fin.final_by_timeout and fin.text == "现在"  # last partial
    # a late final still carries the original turn_id, flagged late
    b.stts[0].emit_final()
    await settle()
    late = of(drain(b.up), EventType.TRANSCRIPT_USER_FINAL)[0]
    assert late.late and late.turn_id == T1


async def test_commit_idempotent_and_discard_rules() -> None:
    b = build("cascade")
    await b.up.open(make_init())
    assert (await b.up.commit_turn(T1, "commit")).code is AckCode.INVALID_STATE  # no_audio
    await send_frames(b.up)
    first = await b.up.commit_turn(T1, "commit")
    assert (await b.up.commit_turn(T1, "commit")) is first
    assert (await b.up.commit_turn(T1, "discard")).code is AckCode.INVALID_STATE
    await send_frames(b.up, T2)
    assert (await b.up.commit_turn(T2, "discard")).ok
    assert (await b.up.commit_turn(T2, "commit")).detail == "discarded"
    assert b.stts[1].cancelled


async def test_send_audio_rejects_missing_turn_and_dedupes_seq() -> None:
    b = build("cascade")
    await b.up.open(make_init())
    assert (await b.up.send_audio(b"\0\0", AudioMeta(None, 0, 1, 0, "vad"))).detail == "turn_id"
    assert (await b.up.send_audio(b"\0\0", AudioMeta(T1, 5, 1, 0, "vad"))).ok
    assert (await b.up.send_audio(b"\0\0", AudioMeta(T1, 5, 1, 0, "vad"))).data == {"duplicate": True}
    assert b.stts[0].frames == 1


async def _turn_and_request(b, script_ref="rr-1", overrides=None):  # type: ignore[no-untyped-def]
    await b.up.open(make_init())
    await send_frames(b.up)
    await b.up.commit_turn(T1, "commit")
    drain(b.up)
    ack = await b.up.request_response(T1, script_ref, overrides)
    assert ack.ok, ack
    await settle(60)
    return ack.data["response_id"], drain(b.up)


async def test_response_flow_sentences_audio_and_done() -> None:
    b = build("cascade", script=[MockReply(text="好的，我在。请问需要什么帮助？")])
    rid, evs = await _turn_and_request(b)
    assert types(evs)[0] == "response.started"
    sents = of(evs, EventType.RESPONSE_SENTENCE)
    assert [s.text for s in sents] == ["好的，我在。", "请问需要什么帮助？"]  # first segment ≥ 6 chars
    assert [s.item_id for s in sents] == ["0", "1"]
    audio = of(evs, EventType.RESPONSE_AUDIO_DELTA)
    assert audio and all(a.response_id == rid and a.item_id is not None and a.duration_ms for a in audio)
    done = of(evs, EventType.RESPONSE_DONE)[0]
    assert done.status is ResponseStatus.COMPLETED and done.reply_modality is ReplyModality.AUDIO
    assert done.response_ref == "rr-1"
    assert b.up._history[-1].role == "assistant"
    # text precedes its audio for every sentence
    first_audio = next(i for i, e in enumerate(evs) if e.type is EventType.RESPONSE_AUDIO_DELTA)
    first_sentence = next(i for i, e in enumerate(evs) if e.type is EventType.RESPONSE_SENTENCE)
    assert first_sentence < first_audio
    assert (await b.up.request_response(T1, "rr-1")).data["response_id"] == rid  # idempotent by ref


async def test_tool_call_before_any_audio_then_tool_result_reply_is_auto_tool() -> None:
    b = build("cascade", script=[MockReply(text="我查一下。", tool_call=TOOL_CALL), MockReply(text="三点。")])
    _rid, evs = await _turn_and_request(b)
    idx = {e.type: i for i, e in reversed(list(enumerate(evs)))}
    assert idx[EventType.TOOL_CALL] < idx[EventType.RESPONSE_AUDIO_DELTA]
    call = of(evs, EventType.TOOL_CALL)[0]
    assert call.call_id == "call-1" and call.args == {} and call.turn_id == T1 and not call.before_commit
    assert b.up._history[-1].tool_calls[0]["id"] == "call-1"
    assert (await b.up.send_tool_result("nope", {}, False)).detail == "unknown_call"
    ack = await b.up.send_tool_result("call-1", {"hour": 3}, True)
    assert ack.ok and "response_id" in ack.data
    await settle(60)
    evs = drain(b.up)
    assert of(evs, EventType.RESPONSE_STARTED)[0].origin is ResponseOrigin.AUTO_TOOL
    assert of(evs, EventType.RESPONSE_DONE)[0].status is ResponseStatus.COMPLETED
    assert b.llm.calls[1]["messages"][-1].role == "tool"
    assert (await b.up.send_tool_result("call-1", {}, False)).data == {"duplicate": True}


async def test_silent_close_appends_without_llm_call() -> None:
    b = build("cascade", script=[MockReply(text="", tool_call=TOOL_CALL)])
    _rid, evs = await _turn_and_request(b)
    assert of(evs, EventType.RESPONSE_DONE)[0].reply_modality is ReplyModality.NONE
    assert (await b.up.send_tool_result("call-1", {"ok": True}, False)).ok
    assert len(b.llm.calls) == 1 and b.up._history[-1].role == "tool"


async def test_guardrail_replace_and_block() -> None:
    seen: list[tuple[int, str, bool]] = []

    async def hook(rid: str, turn_id, idx: int, text: str, is_last: bool) -> GuardrailVerdict:  # type: ignore[no-untyped-def]
        seen.append((idx, text, is_last))
        if idx == 0:
            return GuardrailVerdict("replace", text="改写后的首句，")
        if "禁词" in text:
            return GuardrailVerdict("block", escalate=True)
        return GuardrailVerdict("allow")

    b = build("cascade", script=[MockReply(text="好的，我在。这里有禁词。后面不该出声。")], guardrail=hook)
    rid, evs = await _turn_and_request(b)
    sents = of(evs, EventType.RESPONSE_SENTENCE)
    assert sents[0].text == "改写后的首句，"
    assert b.tts.requests[0][0] == "改写后的首句，"
    done = of(evs, EventType.RESPONSE_DONE)[0]
    assert done.status is ResponseStatus.CANCELLED and done.reason == "guardrail"
    audio = of(evs, EventType.RESPONSE_AUDIO_DELTA)
    assert audio and all(a.item_id in ("0", "1") for a in audio)  # zero audio after block
    assert all(e.type is not EventType.RESPONSE_AUDIO_DELTA for e in evs[evs.index(done) :])
    assert b.llm.cancels == 1
    assert len(seen) == 2  # one hook call per sentence before TTS; block stops the stream


async def test_cancel_stops_llm_flushes_tts_and_suppresses_late_deltas() -> None:
    b = build("cascade", script=[MockReply(text="第一句话说完了。第二句话还在说。第三句。")], late=True)
    await b.up.open(make_init())
    await send_frames(b.up)
    await b.up.commit_turn(T1, "commit")
    ack = await b.up.request_response(T1, "rr-1")
    rid = ack.data["response_id"]
    await until(b.up, EventType.RESPONSE_AUDIO_DELTA)  # first sentence's TTS is in progress
    ack = await b.up.cancel_response(rid)
    assert ack.ok and ack.data["confirmed"] is True
    done_seq = of(drain(b.up), EventType.RESPONSE_DONE)[0]
    assert done_seq.status is ResponseStatus.CANCELLED
    await settle(60)
    after = drain(b.up)
    assert not [e for e in after if e.response_id == rid]
    assert b.llm.cancels == 1 and b.tts.flushed
    assert (await b.up.cancel_response(rid)).ok  # idempotent
    assert (await b.up.cancel_response("resp-404")).ok


async def test_truncate_local_only_and_monotonic() -> None:
    b = build("cascade", script=[MockReply(text="第一句话说完了。第二句话还在说。")])
    _rid, _evs = await _turn_and_request(b)
    ack = await b.up.truncate("1", 300, played_text="第一句话说完了。第二")
    assert ack.ok and ack.data == {"local_only": True}
    assert b.up._history[-1].content == "第一句话说完了。第二"
    assert (await b.up.truncate("1", 500)).ok and b.up._history[-1].content == "第一句话说完了。第二"  # not smaller: no-op
    assert (await b.up.truncate("1", 0)).ok and b.up._history[-1].content == "第一句话说完了。"
    assert (await b.up.truncate("9", 0)).code is AckCode.INVALID_STATE


async def test_update_config_live_append_next_session() -> None:
    b = build("cascade")
    await b.up.open(make_init())
    new_tool = ToolDef("submit", "s", {})
    assert (await b.up.update_config(ConfigPatch(instructions="新人设", tools=[new_tool]), "live")).ok
    assert b.up._build_messages(None)[0].content == "新人设" and b.up._tools == [new_tool]
    assert (await b.up.update_config(ConfigPatch(voice="v2"), "live")).detail == "config.update.voice"
    assert (await b.up.update_config(ConfigPatch(instructions="x"), "append")).detail == "config.append_channels"
    ack = await b.up.update_config(ConfigPatch(instructions="later"), "next_session")
    assert ack.ok and ack.data == {"deferred": True} and b.up._build_messages(None)[0].content == "新人设"
    assert (await b.up.update_config(ConfigPatch(instructions="z", state_block_version="1.0"), "live")).data == {"duplicate": True}


async def test_overrides_text_only_and_slot_busy() -> None:
    b = build("cascade", script=[MockReply(text="只出文本。"), MockReply(text="x")])
    ov = ResponseOverrides(modalities=["text"], instructions="本轮只回一句")
    rid, evs = await _turn_and_request(b, overrides=ov)
    assert not of(evs, EventType.RESPONSE_AUDIO_DELTA)
    assert of(evs, EventType.RESPONSE_DONE)[0].reply_modality is ReplyModality.TEXT_ONLY
    assert b.llm.calls[0]["messages"][-1].content == "本轮只回一句"
    b.llm.chunk_delay_ms = 1000  # make the next one slow
    assert (await b.up.request_response(T1, "rr-2")).ok
    assert (await b.up.request_response(T1, "rr-3")).code is AckCode.SLOT_BUSY


async def test_close_cancels_active_and_emits_session_closed() -> None:
    b = build("cascade", script=[MockReply(text="很长的一句话还没说完呢。")])
    await b.up.open(make_init())
    await send_frames(b.up)
    await b.up.commit_turn(T1, "commit")
    b.llm.chunk_delay_ms = 1000
    await b.up.request_response(T1, "rr-1")
    await settle(3)
    assert (await b.up.close("test")).ok
    evs = drain(b.up)
    assert of(evs, EventType.RESPONSE_DONE)[0].status is ResponseStatus.CANCELLED
    assert types(evs)[-1] == "session.closed"
    assert (await b.up.close("again")).ok
    assert (await b.up.send_audio(b"", AudioMeta(T1, 0, 0, 0, "x"))).detail == "closed"


async def test_history_formats_packed_and_tools_as_text() -> None:
    b = build("cascade")
    hist = [HistoryItem("tool_call", "[调用 get_time]", tool_call_id="c0"), HistoryItem("user", "hi")]
    await b.up.open(make_init(history=hist, history_format=HistoryFormat.PAIRS_TOOLS_AS_TEXT))
    assert b.up._history[0].role == "assistant" and b.up._history[0].tool_calls is None


# -- TTS lookahead (handbook first-audio §4) ----------------------------------


async def _run_with_clock(b, until_type=EventType.RESPONSE_DONE, step=50, max_ms=10_000):  # type: ignore[no-untyped-def]
    evs = []
    for _ in range(max_ms // step):
        await settle()
        evs.extend(drain(b.up))
        if any(e.type is until_type for e in evs):
            return evs
        await b.clock.advance(step)
    raise AssertionError(types(evs))


def _timed_tts(b):  # type: ignore[no-untyped-def]
    started: dict[str, int] = {}
    inner = b.tts.synthesize

    def synthesize(text, request_id):  # type: ignore[no-untyped-def]
        started[request_id] = b.clock.now_ms()
        return inner(text, request_id)

    b.tts.synthesize = synthesize
    b.tts.chunk_delay_ms = 100  # 100 ms of synthesis per 100 ms chunk: real-time factor 1
    return started


async def test_next_sentence_tts_starts_while_current_is_emitted_and_order_is_kept() -> None:
    b = build("cascade", script=[MockReply(text="第一句话说完了。第二句话还在说。第三句也在。")])
    started = _timed_tts(b)
    await b.up.open(make_init())
    await send_frames(b.up)
    await b.up.commit_turn(T1, "commit")
    drain(b.up)
    rid = (await b.up.request_response(T1, "rr-1")).data["response_id"]
    evs = await _run_with_clock(b)
    # sentence 0 needs 400 ms of synthesis; sentence 1 starts long before that, sentence 2 waits for a slot
    assert started[f"{rid}:0"] == 0 and started[f"{rid}:1"] < 400 and started[f"{rid}:2"] >= 400
    order = [(e.type, e.sentence_index) for e in evs
             if e.type in (EventType.RESPONSE_SENTENCE, EventType.RESPONSE_AUDIO_DELTA)]
    sentence_of = [int(i) for _t, i in order]
    assert sentence_of == sorted(sentence_of)  # sentence event, then its audio, then the next sentence
    assert [i for t, i in order if t is EventType.RESPONSE_SENTENCE] == [0, 1, 2]
    assert of(evs, EventType.RESPONSE_DONE)[0].status is ResponseStatus.COMPLETED


async def test_cancel_flushes_every_synthesis_in_flight() -> None:
    b = build("cascade", script=[MockReply(text="第一句话说完了。第二句话还在说。第三句也在。")])
    _timed_tts(b)
    await b.up.open(make_init())
    await send_frames(b.up)
    await b.up.commit_turn(T1, "commit")
    rid = (await b.up.request_response(T1, "rr-1")).data["response_id"]
    await _run_with_clock(b, until_type=EventType.RESPONSE_AUDIO_DELTA)
    assert (await b.up.cancel_response(rid)).ok
    assert {f"{rid}:0", f"{rid}:1"} <= set(b.tts.flushed)
    await b.clock.advance(2_000)
    await settle(60)
    assert not [e for e in drain(b.up) if e.response_id == rid and e.type is EventType.RESPONSE_AUDIO_DELTA]


async def test_tts_failure_on_a_later_sentence_fails_after_earlier_audio() -> None:
    b = build("cascade", script=[MockReply(text="第一句话说完了。第二句话还在说。")])
    inner = b.tts.synthesize

    def synthesize(text, request_id):  # type: ignore[no-untyped-def]
        if request_id.endswith(":1"):
            raise RuntimeError("tts down")
        return inner(text, request_id)

    b.tts.synthesize = synthesize
    _rid, evs = await _turn_and_request(b)
    audio = of(evs, EventType.RESPONSE_AUDIO_DELTA)
    assert audio and {a.item_id for a in audio} == {"0"}
    done = of(evs, EventType.RESPONSE_DONE)[0]
    assert done.status is ResponseStatus.FAILED and evs.index(done) > evs.index(audio[-1])
    assert of(evs, EventType.ERROR)


async def test_turn_start_warms_llm_and_tts_at_most_once_per_interval() -> None:
    b = build("cascade", script=[MockReply(text="好。"), MockReply(text="好。")])
    warmed: list[str] = []

    async def llm_warm() -> None:
        warmed.append("llm")

    async def tts_warm() -> None:
        warmed.append("tts")

    b.llm.warmup = llm_warm
    b.tts.warmup = tts_warm
    await b.up.open(make_init())
    assert warmed == []
    await send_frames(b.up)  # first frame of the turn: connect while the user is still talking
    await settle()
    assert sorted(warmed) == ["llm", "tts"]
    await b.up.commit_turn(T1, "commit")
    await send_frames(b.up, turn=T2)
    await settle()
    assert len(warmed) == 2  # within warmup_interval_ms
    await b.clock.advance(b.up.timeouts.warmup_interval_ms)
    await b.up.commit_turn(T2, "commit")
    await send_frames(b.up, turn=TurnId("c", 3))
    await settle()
    assert len(warmed) == 4


# -- text-stream TTS (one stream per reply, token deltas pushed) --------------


def _stream_tts(b, **kw):  # type: ignore[no-untyped-def]
    tts = MockStreamTTS(clock=b.clock, **kw)
    b.up._tts = tts
    return tts


async def test_text_stream_pushes_every_delta_and_sentences_follow_the_engine() -> None:
    b = build("cascade", script=[MockReply(text="好的，我在。请问需要什么帮助？")])
    tts = _stream_tts(b)
    rid, evs = await _turn_and_request(b)
    st = tts.streams[0]
    assert st.request_id == f"{rid}:stream" and st.ended and st.closed
    assert st.pushes == ["好的", "，我", "在。", "请问", "需要", "什么", "帮助", "？"]  # LLM deltas as they came
    assert not tts.requests  # no per-sentence synthesize
    sents = of(evs, EventType.RESPONSE_SENTENCE)
    assert [s.text for s in sents] == ["好的，我在。", "请问需要什么帮助？"]  # the engine's boundaries
    order = [int(e.sentence_index) for e in evs if e.type in (EventType.RESPONSE_SENTENCE, EventType.RESPONSE_AUDIO_DELTA)]
    assert order == sorted(order) and order[0] == 0
    assert {a.item_id for a in of(evs, EventType.RESPONSE_AUDIO_DELTA)} == {"0", "1"}
    done = of(evs, EventType.RESPONSE_DONE)[0]
    assert done.status is ResponseStatus.COMPLETED and done.reply_modality is ReplyModality.AUDIO
    assert b.up._history[-1].content == "好的，我在。请问需要什么帮助？"
    assert (await b.up.truncate("1", 0)).ok and b.up._history[-1].content == "好的，我在。"


async def test_text_stream_without_engine_marks_is_one_sentence() -> None:
    b = build("cascade", script=[MockReply(text="第一句话说完了。第二句。")])
    _stream_tts(b, marks=False)
    _rid, evs = await _turn_and_request(b)
    sents = of(evs, EventType.RESPONSE_SENTENCE)
    assert len(sents) == 1 and evs.index(sents[0]) < evs.index(of(evs, EventType.RESPONSE_AUDIO_DELTA)[0])
    assert {a.item_id for a in of(evs, EventType.RESPONSE_AUDIO_DELTA)} == {"0"}
    assert b.up._by_response[_rid].sentences == ["第一句话说完了。第二句。"]


async def test_text_stream_with_guardrail_pushes_only_approved_sentences() -> None:
    async def hook(rid, turn_id, idx, text, is_last):  # type: ignore[no-untyped-def]
        return GuardrailVerdict("block") if "禁词" in text else GuardrailVerdict("allow")

    b = build("cascade", script=[MockReply(text="好的，我在。这里有禁词。后面不该出声。")], guardrail=hook)
    tts = _stream_tts(b)
    _rid, evs = await _turn_and_request(b)
    st = tts.streams[0]
    assert st.pushes == ["好的，我在。"] and st.ended  # approved text plays out, nothing after the block
    done = of(evs, EventType.RESPONSE_DONE)[0]
    assert done.status is ResponseStatus.CANCELLED and done.reason == "guardrail"
    assert [s.text for s in of(evs, EventType.RESPONSE_SENTENCE)] == ["好的，我在。"]


async def test_text_stream_cancel_flushes_and_suppresses_audio() -> None:
    b = build("cascade", script=[MockReply(text="第一句话说完了。第二句话还在说。第三句。")])
    tts = _stream_tts(b, chunk_delay_ms=100)
    await b.up.open(make_init())
    await send_frames(b.up)
    await b.up.commit_turn(T1, "commit")
    rid = (await b.up.request_response(T1, "rr-1")).data["response_id"]
    await _run_with_clock(b, until_type=EventType.RESPONSE_AUDIO_DELTA)
    assert (await b.up.cancel_response(rid)).ok
    assert f"{rid}:stream" in tts.flushed and tts.streams[0].closed
    await b.clock.advance(2_000)
    await settle(60)
    assert not [e for e in drain(b.up) if e.response_id == rid and e.type is EventType.RESPONSE_AUDIO_DELTA]


async def test_text_stream_tool_call_only_releases_the_stream_without_audio() -> None:
    b = build("cascade", script=[MockReply(text="", tool_call=TOOL_CALL)])
    tts = _stream_tts(b)
    rid, evs = await _turn_and_request(b)
    assert of(evs, EventType.RESPONSE_DONE)[0].reply_modality is ReplyModality.NONE
    assert tts.streams[0].pushes == [] and tts.streams[0].closed and f"{rid}:stream" in tts.flushed


async def test_text_stream_open_failure_fails_the_response() -> None:
    b = build("cascade", script=[MockReply(text="好的，我在。")])
    _stream_tts(b, open_error=RuntimeError("tts down"))
    _rid, evs = await _turn_and_request(b)
    assert of(evs, EventType.RESPONSE_DONE)[0].status is ResponseStatus.FAILED
    assert of(evs, EventType.ERROR) and not of(evs, EventType.RESPONSE_AUDIO_DELTA)
