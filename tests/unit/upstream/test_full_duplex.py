"""Full-duplex pipeline: synthesised turn/response events (architecture §3)."""

from __future__ import annotations

from vak.core.types import HistoryItem
from vak.upstream.base import AckCode, AudioMeta, ConfigPatch, EventType, ResponseOrigin, ResponseStatus
from vak.upstream.providers.mock import silence_pcm, tone_pcm

from .helpers import T1, TOOL, build, drain, make_init, of, settle, types


async def test_open_rejects_history_tools_and_resume() -> None:
    b = build("full_duplex")
    assert (await b.up.open(make_init(history=[HistoryItem("user", "x")], tools=[]))).detail == "history.injection"
    assert (await b.up.open(make_init(tools=[TOOL]))).detail == "tools.calling"
    assert (await b.up.open(make_init(tools=[], resume_handle="h"))).detail == "session.resumption"
    assert (await b.up.open(make_init(tools=[]))).ok and b.model.connected


async def test_model_bursts_become_started_deltas_done() -> None:
    b = build("full_duplex", bursts=[(400, 800, "hi there"), (160, 800, None)])
    await b.up.open(make_init(tools=[]))
    await settle(80)
    evs = drain(b.up)
    started = of(evs, EventType.RESPONSE_STARTED)
    done = of(evs, EventType.RESPONSE_DONE)
    assert len(started) == 2 and len(done) == 2
    assert started[0].origin is ResponseOrigin.SERVER_VAD and started[0].response_id != started[1].response_id
    assert all(d.status is ResponseStatus.COMPLETED for d in done)
    deltas = of(evs, EventType.RESPONSE_AUDIO_DELTA)
    assert all(d.response_id and d.item_id and d.duration_ms == 80 and d.sample_rate == 24000 for d in deltas)
    # done comes after 700 ms of silence (9 silent frames), before the second burst starts
    first_done = evs.index(done[0])
    silent_before = [e for e in evs[:first_done] if e.type is EventType.RESPONSE_AUDIO_DELTA][5:]
    assert len(silent_before) == 9
    ta = of(evs, EventType.TRANSCRIPT_ASSISTANT)[0]
    assert ta.text == "hi there" and ta.response_id == started[0].response_id


async def test_uplink_energy_synthesises_speech_started_stopped() -> None:
    b = build("full_duplex", bursts=[])
    await b.up.open(make_init(tools=[]))
    for i in range(3):
        assert (await b.up.send_audio(tone_pcm(80, 24000), AudioMeta(None, i, 1920, 0, "vad", 24000))).ok
    for i in range(10):
        await b.up.send_audio(silence_pcm(80, 24000), AudioMeta(None, 10 + i, 1920, 0, "vad", 24000))
    evs = drain(b.up)
    assert [e.type for e in evs if e.type.value.startswith("input")] == [
        EventType.INPUT_SPEECH_STARTED,
        EventType.INPUT_SPEECH_STOPPED,
    ]
    assert of(evs, EventType.INPUT_SPEECH_STARTED)[0].source == "adapter_vad"
    assert len(b.model.sent) == 13


async def test_degraded_primitives() -> None:
    b = build("full_duplex", bursts=[(800, 80, None)])
    await b.up.open(make_init(tools=[]))
    assert (await b.up.commit_turn(T1, "commit")).data == {"committed_by_server": True}
    assert (await b.up.commit_turn(T1, "discard")).detail == "turn.commit_mode"
    assert (await b.up.request_response(T1, "rr")).detail == "response.client_initiated"
    assert (await b.up.truncate("i", 0)).detail == "truncate.mode"
    assert (await b.up.send_tool_result("c", {}, True)).detail == "tools.calling"
    assert (await b.up.update_config(ConfigPatch(instructions="x"), "live")).detail == "config.update.instructions"
    assert (await b.up.update_config(ConfigPatch(voice="x"), "live")).detail == "config.update.voice"
    assert (await b.up.update_config(ConfigPatch(instructions="x"), "next_session")).ok
    await settle(3)
    rid = of(drain(b.up), EventType.RESPONSE_STARTED)[0].response_id
    ack = await b.up.cancel_response(rid)
    assert ack.ok and ack.data == {"local_only": True}
    await settle(30)
    evs = drain(b.up)
    done = of(evs, EventType.RESPONSE_DONE)
    assert done[0].status is ResponseStatus.CANCELLED and done[0].response_id == rid
    assert not [e for e in evs if e.type is EventType.RESPONSE_AUDIO_DELTA and e.response_id == rid]


async def test_close_finishes_active_response_and_disconnects() -> None:
    b = build("full_duplex", bursts=[(8000, 80, None)])
    await b.up.open(make_init(tools=[]))
    await settle(5)
    assert (await b.up.close("bye")).ok and b.model.disconnected
    evs = drain(b.up)
    assert of(evs, EventType.RESPONSE_DONE)[0].status is ResponseStatus.CANCELLED
    assert types(evs)[-1] == "session.closed"
    assert (await b.up.commit_turn(T1, "commit")).code is AckCode.INVALID_STATE
