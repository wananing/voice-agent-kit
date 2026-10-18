"""S2S pipeline: the §2.3 degradation table against MockRealtimeS2S."""

from __future__ import annotations

import asyncio

import pytest

from vak.core.types import ErrorClass, ErrorReason, HistoryFormat, HistoryItem, ReplyModality
from vak.upstream.base import (
    AckCode,
    AudioMeta,
    ConfigPatch,
    EventType,
    ResponseOrigin,
    ResponseOverrides,
    ResponseStatus,
)
from vak.upstream.providers.mock import MockReply

from .helpers import T1, TOOL_CALL, build, drain, frame, make_init, of, send_frames, settle, types

HIST = [HistoryItem("user", "我叫小明", local_entry_id="e1"), HistoryItem("assistant", "你好小明", local_entry_id="e2")]


async def opened(history: list[HistoryItem] | None = None, **kw):  # type: ignore[no-untyped-def]
    b = build("s2s", **kw)
    ack = await b.up.open(make_init(history=HIST if history is None else history))
    assert ack.ok, ack
    await settle()
    drain(b.up)
    return b


async def test_open_seeds_history_and_mirror_confirms_on_echo() -> None:
    b = build("s2s")
    ack = await b.up.open(make_init(history=HIST))
    assert ack.ok and ack.data["effective"] == {"history_items_sent": 2, "tools_sent": 1}
    assert b.provider.config["history_injection"] == "init_only" and len(b.provider.config["history"]) == 2
    await settle()
    evs = drain(b.up)
    assert types(evs)[0] == "session.opened" and len(of(evs, EventType.ITEM_ECHO)) == 2
    assert all(it.confirmed and it.item_id for it in b.up.mirror.items)
    assert b.up.resume_handle == "mock-handle-1" and of(evs, EventType.SESSION_RESUMABLE)


@pytest.mark.parametrize(
    ("overrides", "init_kw", "code", "detail"),
    [
        ({"session.resumption": "none"}, {"resume_handle": "h"}, AckCode.UNSUPPORTED, "session.resumption"),
        ({"history.tool_calls": "drop"}, {"history": [HistoryItem("tool_call", "", tool_call_id="c")]}, AckCode.UNSUPPORTED, "history.tool_calls"),
        ({"history.tool_calls": "as_text"}, {"history": [HistoryItem("tool_output", "x", tool_call_id="c")]}, AckCode.UNSUPPORTED, "history.tool_calls"),
        ({"history.injection": set()}, {"history": HIST}, AckCode.UNSUPPORTED, "history.injection"),
        ({"history.limits": {"max_items": 1}}, {"history": HIST}, AckCode.INVALID_STATE, "history_over_limit:max_items=1"),
        ({"history.constraints": ["starts_with_user"]}, {"history": [HistoryItem("assistant", "x")]}, AckCode.INVALID_STATE, "history_constraint:starts_with_user"),
        ({"history.constraints": ["ends_with_user"]}, {"history": HIST}, AckCode.INVALID_STATE, "history_constraint:ends_with_user"),
    ],
)
async def test_open_degradation_rejects_instead_of_fixing(overrides, init_kw, code, detail) -> None:  # type: ignore[no-untyped-def]
    b = build("s2s", caps_overrides=overrides)
    ack = await b.up.open(make_init(**init_kw))
    assert ack.code is code and ack.detail == detail
    assert not b.provider.connected  # nothing sent, nothing trimmed


async def test_open_as_text_format_accepted_when_declared() -> None:
    b = build("s2s", caps_overrides={"history.tool_calls": "as_text"})
    hist = [HistoryItem("tool_output", "x", tool_call_id="c"), HistoryItem("user", "hi")]
    assert (await b.up.open(make_init(history=hist, history_format=HistoryFormat.PAIRS_TOOLS_AS_TEXT))).ok


async def test_open_connect_timeout_closes_half_built() -> None:
    b = build("s2s")

    async def hang(config):  # type: ignore[no-untyped-def]
        await asyncio.Event().wait()

    b.provider.connect = hang  # type: ignore[method-assign]
    task = asyncio.create_task(b.up.open(make_init()))
    await settle()
    await b.clock.advance(b.up.timeouts.open_connect_ms + 1)
    ack = await task
    assert ack.code is AckCode.TIMEOUT and b.provider.disconnected
    assert of(drain(b.up), EventType.ERROR)[0].error.reason is ErrorReason.TIMEOUT


async def test_open_ready_ack_timeout() -> None:
    b = build("s2s", emit_opened=False)
    task = asyncio.create_task(b.up.open(make_init()))
    await settle()
    await b.clock.advance(b.up.timeouts.open_item_ack_ms + 1)
    ack = await task
    assert ack.code is AckCode.TIMEOUT and ack.detail == "open_ready" and b.provider.disconnected


async def test_open_timeout_ready_signal_waits_fixed_then_opens() -> None:
    b = build("s2s", emit_opened=False, caps_overrides={"session.init_ready_signal": "timeout"})
    task = asyncio.create_task(b.up.open(make_init(history=[])))
    await settle()
    await b.clock.advance(b.up.timeouts.open_ready_wait_ms + 1)
    assert (await task).ok
    assert of(drain(b.up), EventType.SESSION_OPENED)


async def test_commit_create_commit_and_ack() -> None:
    b = await opened()
    await send_frames(b.up, n=4)
    assert len(b.provider.appended) == 4
    assert (await b.up.send_audio(frame(), AudioMeta(T1, 0, 1, 0, "button"))).data == {"duplicate": True}
    ack = await b.up.commit_turn(T1, "commit")
    assert ack.ok and b.provider.commits == 1
    assert (await b.up.commit_turn(T1, "commit")) is ack
    assert of(drain(b.up), EventType.INPUT_COMMITTED)[0].turn_id == T1


async def test_commit_too_short_and_discard() -> None:
    b = await opened()
    await send_frames(b.up, n=1, ms=50)
    assert (await b.up.commit_turn(T1, "commit")).detail == "too_short"
    assert (await b.up.commit_turn(T1, "discard")).ok and b.provider.clears == 1


async def test_commit_ack_timeout_is_ack_timeout_plus_error_event() -> None:
    b = await opened(ack_commit=False)
    await send_frames(b.up)
    task = asyncio.create_task(b.up.commit_turn(T1, "commit"))
    await settle()
    await b.clock.advance(b.up.timeouts.commit_ack_ms + 1)
    ack = await task
    assert ack.code is AckCode.TIMEOUT
    err = of(drain(b.up), EventType.ERROR)[0]
    assert err.error.reason is ErrorReason.TIMEOUT and err.turn_id == T1


async def test_commit_server_only_and_none() -> None:
    b = await opened(caps_overrides={"turn.commit_mode": "server_only"})
    await send_frames(b.up)
    ack = await b.up.commit_turn(T1, "commit")
    assert ack.ok and ack.data == {"committed_by_server": True} and b.provider.commits == 0
    b2 = await opened(caps_overrides={"turn.commit_mode": "none"})
    assert (await b2.up.commit_turn(T1, "discard")).detail == "turn.commit_mode"


async def test_request_response_binds_id_and_events_carry_ref_turn() -> None:
    b = await opened(script=[MockReply(text="你好", tool_call=TOOL_CALL)])
    await send_frames(b.up)
    await b.up.commit_turn(T1, "commit")
    ack = await b.up.request_response(T1, "rr-1", ResponseOverrides(instructions="短一点"))
    assert ack.ok and ack.data["response_id"] == "mr-1"
    assert b.provider.created[0] == {"instructions": "短一点"}
    await settle(40)
    evs = drain(b.up)
    st = of(evs, EventType.RESPONSE_STARTED)[0]
    assert st.origin is ResponseOrigin.CLIENT and st.response_ref == "rr-1" and st.turn_id == T1
    assert all(e.response_ref == "rr-1" for e in evs if e.response_id == "mr-1")
    call = of(evs, EventType.TOOL_CALL)[0]
    assert call.turn_id == T1 and not call.before_commit
    done = of(evs, EventType.RESPONSE_DONE)[0]
    assert done.status is ResponseStatus.COMPLETED and done.reply_modality is ReplyModality.AUDIO
    assert of(evs, EventType.USAGE)[0].usage.cached_input_tokens == 4
    assert (await b.up.request_response(T1, "rr-1")).data["response_id"] == "mr-1"


async def test_request_response_unsupported_cases_and_slot_busy() -> None:
    b = await opened(caps_overrides={"response.client_initiated": "no"})
    assert (await b.up.request_response(T1, "r")).detail == "response.client_initiated"
    b = await opened(caps_overrides={"response.overrides": {"instructions": "none"}})
    ack = await b.up.request_response(T1, "r", ResponseOverrides(instructions="x"))
    assert ack.detail == "response.overrides.instructions" and not b.provider.created
    ack = await b.up.request_response(T1, "r", ResponseOverrides(modalities=["text"]))
    assert ack.detail == "response.overrides.modalities"
    b = await opened(delta_interval_ms=500)
    assert (await b.up.request_response(T1, "r1")).ok
    assert (await b.up.request_response(T1, "r2")).code is AckCode.SLOT_BUSY


async def test_server_started_response_is_bound_with_its_origin() -> None:
    b = await opened()
    rid = b.provider.start_server_response()
    await settle(2)
    ack = await b.up.request_response(T1, "rr-1")
    assert ack.ok and ack.data == {"response_id": rid, "origin": "server_vad"}
    await settle(40)
    evs = drain(b.up)
    assert of(evs, EventType.RESPONSE_STARTED)[0].origin is ResponseOrigin.SERVER_VAD
    assert of(evs, EventType.RESPONSE_DONE)[0].response_ref == "rr-1"


async def test_response_started_timeout_and_first_delta_watchdog() -> None:
    b = await opened(emit_started=False)
    task = asyncio.create_task(b.up.request_response(T1, "rr-1"))
    await settle()
    await b.clock.advance(b.up.timeouts.response_started_ms + 1)
    ack = await task
    assert ack.code is AckCode.TIMEOUT and ack.detail == "response_started"
    assert of(drain(b.up), EventType.ERROR)[0].error.reason is ErrorReason.TIMEOUT
    b = await opened(script=[MockReply(text="", audio_deltas=1)], delta_interval_ms=10_000)
    assert (await b.up.request_response(T1, "rr-2")).ok
    await settle()
    await b.clock.advance(b.up.timeouts.first_delta_ms + 1)
    errs = of(drain(b.up), EventType.ERROR)
    assert errs and errs[0].response_id == "mr-1" and errs[0].error.reason is ErrorReason.TIMEOUT


async def test_cancel_zero_deltas_after_done_even_with_late_deltas() -> None:
    b = await opened(late=True, delta_interval_ms=100)
    ack = await b.up.request_response(T1, "rr-1")
    rid = ack.data["response_id"]
    await b.clock.advance(150)
    before = drain(b.up)
    assert of(before, EventType.RESPONSE_AUDIO_DELTA)
    ack = await b.up.cancel_response(rid)
    await settle()
    assert ack.ok and ack.data["confirmed"] is True and b.provider.cancelled == [rid]
    after = drain(b.up)
    assert [e.type for e in after if e.response_id == rid] == [EventType.RESPONSE_DONE]
    assert after[-1].status is ResponseStatus.CANCELLED
    await b.clock.advance(1000)
    assert not [e for e in drain(b.up) if e.response_id == rid]
    assert b.up.suppressed == 2
    assert (await b.up.cancel_response(rid)).ok


async def test_cancel_ack_timeout_marks_unconfirmed() -> None:
    b = await opened(delta_interval_ms=500)
    rid = (await b.up.request_response(T1, "rr-1")).data["response_id"]
    await settle()

    async def no_ack(_rid):  # type: ignore[no-untyped-def]
        pass

    b.provider.cancel_response = no_ack  # type: ignore[method-assign]
    task = asyncio.create_task(b.up.cancel_response(rid))
    await settle()
    await b.clock.advance(b.up.timeouts.cancel_ack_ms + 1)
    ack = await task
    assert ack.ok and ack.data == {"confirmed": False}
    evs = drain(b.up)
    done = of(evs, EventType.RESPONSE_DONE)[0]
    assert done.status is ResponseStatus.CANCELLED and done.confirmed is False
    assert of(evs, EventType.ERROR)[0].error.error_class is ErrorClass.BENIGN


async def test_cancel_local_only_when_no_cancel_api() -> None:
    b = await opened(caps_overrides={"response.client_initiated": "no"})
    rid = b.provider.start_server_response()
    await settle(2)
    ack = await b.up.cancel_response(rid)
    assert ack.ok and ack.data == {"local_only": True} and not b.provider.cancelled


async def test_truncate_modes() -> None:
    b = await opened()
    ack = await b.up.truncate("item-1", 1200)
    assert ack.ok and b.provider.truncated == [("item-1", 1200)]
    assert (await b.up.truncate("item-1", 1500)).ok and len(b.provider.truncated) == 1  # not smaller: not sent
    assert (await b.up.truncate("item-1", 0)).ok and b.provider.truncated[-1] == ("item-1", 0)
    assert b.up.mirror.item("item-1").audio_end_ms == 0  # updated only after the echo
    b = await opened(caps_overrides={"truncate.mode": "none"})
    assert (await b.up.truncate("i", 1)).detail == "truncate.mode"
    b = await opened(caps_overrides={"truncate.mode": "text_rewrite"})
    assert (await b.up.truncate("i", 1)).code is AckCode.INVALID_STATE
    b = await opened(caps_overrides={"truncate.mode": "local_only"})
    assert (await b.up.truncate("i", 1)).data == {"local_only": True}


async def test_truncate_ack_timeout_is_benign() -> None:
    b = await opened(ack_items=False, history=[])
    task = asyncio.create_task(b.up.truncate("item-1", 100))
    await settle()
    await b.clock.advance(b.up.timeouts.truncate_ack_ms + 1)
    ack = await task
    assert ack.code is AckCode.TIMEOUT
    err = of(drain(b.up), EventType.ERROR)[0]
    assert err.error.error_class is ErrorClass.BENIGN and err.item_id == "item-1"


async def _with_call(**kw):  # type: ignore[no-untyped-def]
    b = await opened(script=[MockReply(text="", tool_call=TOOL_CALL, audio_deltas=0)], **kw)
    await b.up.request_response(T1, "rr-1")
    await settle(20)
    drain(b.up)
    return b


async def test_tool_result_table_row1_auto_no() -> None:
    b = await _with_call()
    assert (await b.up.send_tool_result("call-x", {}, False)).detail == "unknown_call"
    ack = await b.up.send_tool_result("call-1", {"ok": True}, False)
    assert ack.ok and b.provider.tool_outputs[0][0] == "call-1" and len(b.provider.created) == 1
    assert (await b.up.send_tool_result("call-1", {}, True)).data == {"duplicate": True}
    b = await _with_call()
    ack = await b.up.send_tool_result("call-1", {"ok": True}, True)
    assert ack.ok and ack.data["response_id"] == "mr-2" and len(b.provider.created) == 2
    await settle(40)
    assert of(drain(b.up), EventType.RESPONSE_STARTED)[0].response_ref == "tool:call-1"


async def test_tool_result_table_row2_and_row3_auto_yes() -> None:
    b = await _with_call(caps_overrides={"tools.auto_reply_after_result": "yes", "tools.silent_close": "flag"})
    assert (await b.up.send_tool_result("call-1", {}, False)).ok and b.provider.tool_outputs
    b = await _with_call(caps_overrides={"tools.auto_reply_after_result": "yes", "tools.silent_close": "none"})
    ack = await b.up.send_tool_result("call-1", {}, False)
    assert ack.detail == "tools.silent_close" and not b.provider.tool_outputs  # not sent
    b = await _with_call(caps_overrides={"tools.auto_reply_after_result": "unknown"}, auto_reply_after_result=True)
    assert (await b.up.send_tool_result("call-1", {}, False)).detail == "tools.silent_close"  # unknown -> yes
    ack = await b.up.send_tool_result("call-1", {}, True)
    assert ack.ok and "response_id" not in ack.data and len(b.provider.created) == 1
    await settle(40)
    st = of(drain(b.up), EventType.RESPONSE_STARTED)[0]
    assert st.origin is ResponseOrigin.AUTO_TOOL


async def test_tool_result_ack_timeout_benign() -> None:
    b = await _with_call(ack_items=False, history=[])
    task = asyncio.create_task(b.up.send_tool_result("call-1", {}, False))
    await settle()
    await b.clock.advance(b.up.timeouts.tool_result_ack_ms + 1)
    ack = await task
    assert ack.code is AckCode.TIMEOUT
    err = of(drain(b.up), EventType.ERROR)[0]
    assert err.error.error_class is ErrorClass.BENIGN and err.call_id == "call-1"


async def test_update_config_live_full_sends_whole_config_and_acks() -> None:
    b = await opened()
    ack = await b.up.update_config(ConfigPatch(instructions="新", state_block_version="2.0"), "live")
    assert ack.ok
    sent = b.provider.updates[0]
    assert sent["instructions"] == "新" and "tools" in sent and sent["state_block_version"] == "2.0"
    assert (await b.up.update_config(ConfigPatch(instructions="x", state_block_version="2.0"), "live")).data == {"duplicate": True}
    b = await opened(caps_overrides={"config.update.instructions": "live_sparse"})
    assert (await b.up.update_config(ConfigPatch(instructions="新"), "live")).ok
    assert b.provider.updates[0] == {"instructions": "新"}


async def test_update_config_next_session_and_append_unsupported_never_reconnect() -> None:
    b = await opened(caps_overrides={"config.update.tools": "next_session"})
    ack = await b.up.update_config(ConfigPatch(instructions="a", tools=[]), "live")
    assert ack.detail == "config.update.tools" and not b.provider.updates
    assert b.provider.connected and not b.provider.disconnected
    assert (await b.up.update_config(ConfigPatch(tools=[]), "next_session")).data == {"deferred": True}
    assert (await b.up.update_config(ConfigPatch(instructions="a"), "append")).detail == "config.append_channels"
    b = await opened(caps_overrides={"config.append_channels": [{"name": "instructions", "max_tokens": 2}]})
    assert (await b.up.update_config(ConfigPatch(instructions="x" * 40), "append")).detail == "too_long"
    assert (await b.up.update_config(ConfigPatch(instructions="ok"), "append")).ok
    assert b.provider.updates[0] == {"append": {"channel": "instructions", "text": "ok"}}


async def test_update_config_ack_timeout() -> None:
    b = await opened(ack_session_update=False)
    task = asyncio.create_task(b.up.update_config(ConfigPatch(voice="v"), "live"))
    await settle()
    await b.clock.advance(b.up.timeouts.update_config_ack_ms + 1)
    assert (await task).code is AckCode.TIMEOUT
    assert of(drain(b.up), EventType.ERROR)


async def test_errors_are_reclassified_through_code_map_and_defaults() -> None:
    b = await opened()
    b.provider.inject_error("rate_limit_exceeded", "slow down")
    b.provider.inject_error("something_new", "???")
    b.provider.inject_error("context_length_exceeded", "too many tokens")
    await settle()
    errs = [e.error for e in of(drain(b.up), EventType.ERROR)]
    assert [e.error_class for e in errs] == [ErrorClass.TRANSIENT, ErrorClass.TRANSIENT, ErrorClass.CONTEXT_OVERFLOW]
    assert errs[0].reason is ErrorReason.THROTTLED and errs[1].reason is ErrorReason.UNKNOWN


async def test_close_cancels_in_flight_then_session_closed() -> None:
    b = await opened(delta_interval_ms=500)
    rid = (await b.up.request_response(T1, "rr-1")).data["response_id"]
    await settle()
    drain(b.up)
    assert (await b.up.close("bye")).ok and b.provider.disconnected
    evs = drain(b.up)
    assert of(evs, EventType.RESPONSE_DONE)[0].response_id == rid
    assert of(evs, EventType.RESPONSE_DONE)[0].status is ResponseStatus.CANCELLED
    assert types(evs)[-1] == "session.closed" and evs[-1].source == "client"
    assert (await b.up.close("bye")).ok
    assert (await b.up.request_response(T1, "x")).detail == "closed"
