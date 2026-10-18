"""Connection-level behaviour on LoopbackTransport with FakeClock (spec 01 §3, §4.3, §6).

Acceptance items covered: 9 (zero frames after flush.end), 15 (lead), 18 (seq
dedupe), 20 (event exactly-once + duplicate ack), 21 (cmd timeout, no resend),
rule 17 (protocol_error without disconnect), §6.2 resume ok / expired.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import pytest

from vak.core.clock import FakeClock
from vak.core.defaults import TurnTimers
from vak.core.ids import IdGen
from vak.core.types import Mode, TurnId
from vak.transport.base import HandshakeFailed, LoopbackTransport, ResumeState, TransportClosed
from vak.transport.framing import UplinkHeader, pack_uplink, unpack_downlink
from vak.transport.protocol import ResumeRequest

HELLO: dict[str, Any] = {
    "type": "hello",
    "proto": "va/1",
    "profile": "A",
    "client": {"model": "test", "fw": "0"},
    "caps": {
        "transport": "ws",
        "aec": "device",
        "modes": ["ptt", "open_mic"],
        "sources": ["button", "text"],
        "receipts": ["started", "ended", "interrupted"],
        "audio_up": {"codec": "pcm16", "rate": 16000, "channels": 1, "frame_ms": 20},
        "audio_down": {"codecs": ["pcm16"], "rates": [16000]},
        "play_buffer_max_ms": 1200,
        "preroll_ms": 250,
        "uninterruptible": True,
        "events": ["nfc"],
        "commands": ["light"],
    },
    "resume": None,
}
PCM = b"\x01\x00" * 320  # one 20 ms frame at 16 kHz
C1 = TurnId("c", 1)


def make(clock: FakeClock | None = None, **kw: Any) -> tuple[FakeClock, LoopbackTransport]:
    clock = clock or FakeClock()
    kw.setdefault("timers", TurnTimers())
    kw.setdefault("ids", IdGen())
    kw.setdefault("session_id", "S-1")
    return clock, LoopbackTransport(clock=clock, **kw)


async def handshake(t: LoopbackTransport, hello: dict[str, Any] | None = None) -> dict[str, Any]:
    t.client_send(hello or HELLO)
    await t.handshake()
    reply = await t.client_recv()
    assert isinstance(reply, dict)
    return reply


class Sink:
    def __init__(self) -> None:
        self.msgs: list[dict[str, Any]] = []
        self.audio: list[tuple[int, int, int, int, bytes]] = []

    async def on_message(self, m: dict[str, Any]) -> None:
        self.msgs.append(m)

    async def on_audio(self, seq: int, ts: int, tid: int, flags: int, pcm: bytes) -> None:
        self.audio.append((seq, ts, tid, flags, pcm))


async def start_run(t: LoopbackTransport) -> tuple[Sink, asyncio.Task[None]]:
    sink = Sink()
    task = asyncio.get_running_loop().create_task(t.run(sink.on_message, sink.on_audio))
    await asyncio.sleep(0)
    return sink, task


async def settle() -> None:
    # every send races the clock (T_write), which costs a few loop iterations per message
    for _ in range(100):
        await asyncio.sleep(0)


async def until(cond: Any, max_yields: int = 1000) -> None:
    for _ in range(max_yields):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


def uplink(seq: int, ts: int = 0, turn: TurnId | None = C1, **flags: bool) -> bytes:
    return pack_uplink(UplinkHeader(seq=seq, ts_samples=ts, turn_id=turn, **flags), PCM)


async def test_hello_reply() -> None:
    clock, t = make()
    reply = await handshake(t)
    assert reply["type"] == "hello" and reply["session_id"] == "S-1" and reply["mode"] == "ptt"
    assert reply["audio_down"] == {"codec": "pcm16", "rate": 16000, "frame_ms": 20}
    assert reply["lead_ms"] == 300 and reply["next_server_turn"] == 1 and reply["resume"] is None
    assert reply["server_time_ms"] == clock.epoch_ms()
    assert set(reply["timers"]) == {"T_hold", "T_asr", "T_filler", "T_respond", "T_link_client", "ping_interval", "resume_window"}
    assert t.hello is not None and t.sample_rate == 16000
    await t.close()


async def test_lead_capped_by_play_buffer_and_open_mic_negotiated() -> None:
    hello = json.loads(json.dumps(HELLO))
    hello["caps"]["play_buffer_max_ms"] = 400
    _, t = make(mode=Mode.OPEN_MIC)
    reply = await handshake(t, hello)
    assert reply["lead_ms"] == 200 and t.lead_ms == 200  # R4.3-4: lead <= play_buffer_max_ms - 200
    assert reply["mode"] == "open_mic" and t.mode is Mode.OPEN_MIC
    hello["caps"]["aec"] = "none"
    _, t2 = make(mode=Mode.OPEN_MIC)
    assert (await handshake(t2, hello))["mode"] == "ptt"


async def test_hello_missing_field_is_protocol_error_then_valid_hello_succeeds() -> None:
    _, t = make()
    t.client_send({"type": "hello", "proto": "va/1"})
    t.client_send({"type": "abort", "turn_id": "c1", "reason": "button"})  # known type before hello
    t.client_send({"type": "weird"})  # unknown: ignored
    t.client_send(HELLO)
    await t.handshake()
    out = t.sent_json()
    assert [m["type"] for m in out] == ["error", "error", "hello"]
    assert out[0]["code"] == "protocol_error" and out[0]["fatal"] is False
    assert not t.closed


async def test_handshake_timeout_T_hello() -> None:
    clock, t = make()
    task = asyncio.get_running_loop().create_task(t.handshake())
    await settle()
    await clock.advance(TurnTimers().T_hello)
    with pytest.raises(HandshakeFailed):
        await task


async def test_protocol_error_does_not_disconnect_and_unknown_type_ignored() -> None:
    _, t = make()
    await handshake(t)
    sink, task = await start_run(t)
    t.client_send({"type": "abort", "turn_id": "c1"})  # missing reason
    t.client_send({"type": "future.thing", "turn_id": "c1"})
    t.client_send("{not json")
    t.client_send({"type": "abort", "turn_id": "c1", "reason": "button"})
    await settle()
    errors = [m for m in t.sent_json() if m["type"] == "error"]
    assert [e["code"] for e in errors] == ["protocol_error", "protocol_error"]
    assert errors[0]["turn_id"] == "c1"
    assert sink.msgs == [{"type": "abort", "turn_id": "c1", "reason": "button"}]
    assert not task.done() and not t.closed
    t.client_close()
    await task


async def test_uplink_seq_dedupe_and_gap_detection() -> None:
    _, t = make()
    await handshake(t)
    sink, task = await start_run(t)
    for seq, ts in [(1, 0), (2, 320), (2, 320), (3, 640), (5, 1280)]:
        t.client_send(uplink(seq, ts, key_down=True))
    await settle()
    assert [a[0] for a in sink.audio] == [1, 2, 3, 5]  # acceptance 18: duplicate dropped
    assert t.uplink_dup_frames == 1 and t.uplink_gaps == 1 and t.uplink_lost_frames == 1
    seq, ts, tid, flags, pcm = sink.audio[0]
    assert ts == 0 and tid == C1.to_u32() and flags == 0x01 and pcm == PCM
    t.client_close()
    await task


async def test_ping_pong_clock_anchor() -> None:
    clock, t = make()
    await handshake(t)
    _, task = await start_run(t)
    t.client_send({"type": "ping", "client_mono_ms": 61000, "ts_samples_up": 1, "ts_samples_played": 2})
    await settle()
    pong = t.sent_json()[-1]
    assert pong == {"type": "pong", "client_mono_ms": 61000, "server_ms": clock.epoch_ms()}
    assert list(t.clock_anchors) == [(61000, clock.epoch_ms())]
    t.client_close()
    await task


async def test_event_ack_idempotent_by_id() -> None:
    _, t = make()
    await handshake(t)
    sink, task = await start_run(t)
    ev = {"type": "event", "id": "ev-1022", "name": "nfc", "ts_ms": 60012, "data": {"tag": "04A2"}, "expires_ms": 30000}
    t.client_send(ev)
    t.client_send(ev)  # client retry after a lost ack
    await settle()
    acks = [m for m in t.sent_json() if m["type"] == "event.ack"]
    assert acks == [
        {"type": "event.ack", "id": "ev-1022", "status": "accepted"},
        {"type": "event.ack", "id": "ev-1022", "status": "duplicate"},
    ]
    assert len(sink.msgs) == 1  # acceptance 20: handled exactly once
    assert "ev-1022" in t.resume_state().event_ids
    t.client_close()
    await task


async def test_resume_ok_continues_seq_and_dedupes_retransmit() -> None:
    state = ResumeState(
        session_id="S-old",
        last_rx_seq=48177,
        last_tx_seq=9021,
        turns=[(TurnId("c", 42), "listening")],
        event_ids={"ev-7"},
    )
    seen: list[ResumeRequest] = []

    def lookup(req: ResumeRequest) -> ResumeState | None:
        seen.append(req)
        return state

    _, t = make(session_id="S-new", resume_lookup=lookup)
    hello = json.loads(json.dumps(HELLO))
    hello["resume"] = {"session_id": "S-old", "last_tx_seq": 48210, "last_rx_seq": 9021, "current_turn": "c42", "turn_state": "listening"}
    reply = await handshake(t, hello)
    assert seen[0].session_id == "S-old" and seen[0].last_tx_seq == 48210
    assert reply["session_id"] == "S-old" and t.resumed
    assert reply["resume"] == {"ok": True, "last_rx_seq": 48177, "turns": [{"turn_id": "c42", "state": "listening"}]}
    sink, task = await start_run(t)
    for seq in (48176, 48177, 48178, 48179):  # client retransmits from its ring buffer
        t.client_send(uplink(seq, retransmit=True))
    t.client_send({"type": "event", "id": "ev-7", "name": "nfc"})  # event already handled before the drop
    await settle()
    assert [a[0] for a in sink.audio] == [48178, 48179] and t.uplink_dup_frames == 2
    assert t.sent_json()[-1] == {"type": "event.ack", "id": "ev-7", "status": "duplicate"} and sink.msgs == []
    t.set_generation(5)
    await t.send_audio(PCM, TurnId("c", 42), 0, 0, 5)
    await settle()
    hdr, _ = unpack_downlink(t.sent_frames()[0])
    assert hdr.seq == 9022  # §6.1: downlink seq continues, never restarts
    t.client_close()
    await task


async def test_resume_expired_is_fatal_error_then_fresh_hello() -> None:
    _, t = make(resume_lookup=lambda req: None)
    hello = json.loads(json.dumps(HELLO))
    hello["resume"] = {"session_id": "S-gone", "last_tx_seq": 1, "last_rx_seq": 1}
    t.client_send(hello)
    t.client_send(HELLO)
    await t.handshake()
    out = t.sent_json()
    assert out[0]["type"] == "error" and out[0]["code"] == "session_expired"
    assert out[0]["fatal"] is True and out[0]["turn_id"] is None
    assert out[1]["type"] == "hello" and out[1]["resume"] is None and out[1]["session_id"] == "S-1"
    assert not t.resumed and not t.closed


async def test_send_audio_paced_and_flush_same_tick() -> None:
    clock, t = make()
    await handshake(t)
    _, task = await start_run(t)
    t.set_generation(1)
    for i in range(30):
        assert await t.send_audio(PCM, C1, 0, i, 1)
    await until(lambda: len(t.sent_frames()) == 15)
    await settle()
    frames = t.sent_frames()
    assert len(frames) == 15  # one lead (300 ms) pre-buffered, rest paced (clock did not move)
    hdrs = [unpack_downlink(f)[0] for f in frames]
    assert [h.seq for h in hdrs] == list(range(1, 16))
    assert all(h.turn_id == C1 and h.sentence_id == 0 for h in hdrs)
    assert [h.frame_idx for h in hdrs] == list(range(15))
    assert unpack_downlink(frames[0])[1] == PCM  # pcm16 codec: payload is the raw frame
    flush_id = await t.flush([C1], "abort", "abort")
    assert flush_id == "f-1"
    tail = t.sent[-2:]
    assert [json.loads(s) for s in tail if isinstance(s, str)] == [
        {"type": "flush.start", "flush_id": "f-1", "turn_ids": ["c1"], "reason": "abort", "ack_of": "abort"},
        {"type": "flush.end", "flush_id": "f-1"},
    ]
    await clock.advance(2000)
    assert len(t.sent_frames()) == 15  # acceptance 9: zero frames after flush.end
    assert t.pacer.queued_frames == 0
    t.client_close()
    await task


async def test_send_audio_generation_filter() -> None:
    _, t = make()
    await handshake(t)
    t.set_generation(2)
    assert await t.send_audio(PCM, C1, 0, 0, 1) is False
    assert t.consumer_dropped_frames == 1
    assert await t.send_audio(PCM, C1, 0, 0, 2) is True
    await t.close()


@pytest.mark.parametrize("seed", range(20))
async def test_random_timing_flush_leaves_zero_frames_after_flush_end(seed: int) -> None:
    rng = random.Random(seed)
    clock, t = make()
    await handshake(t)
    _, task = await start_run(t)
    gen = 1
    t.set_generation(gen)
    turn = TurnId("c", gen)
    for i in range(rng.randint(1, 80)):
        await t.send_audio(PCM, turn, 0, i, gen)
    for _ in range(rng.randint(0, 5)):
        await clock.advance(rng.randint(0, 400))
    gen += 1
    t.set_generation(gen)
    await t.flush([turn], "abort", "abort")
    end_at = max(i for i, s in enumerate(t.sent) if isinstance(s, str) and json.loads(s)["type"] == "flush.end")
    new_turn = TurnId("c", gen)
    for i in range(rng.randint(0, 30)):
        await t.send_audio(PCM, new_turn, 0, i, gen)
    await clock.advance(rng.randint(0, 2000))
    late = [unpack_downlink(s)[0] for s in t.sent[end_at + 1 :] if isinstance(s, bytes)]
    assert all(h.turn_id == new_turn for h in late)
    t.client_close()
    await task


async def test_cmd_result_and_timeout_without_resend() -> None:
    clock, t = make()
    await handshake(t)
    _, task = await start_run(t)
    fut = asyncio.get_running_loop().create_task(t.cmd("light", {"pattern": "thinking"}))
    await settle()
    sent = t.sent_json()[-1]
    assert sent == {"type": "cmd", "id": "cmd-1", "name": "light", "args": {"pattern": "thinking"}, "timeout_ms": 3000}
    t.client_send({"type": "cmd.result", "id": "cmd-1", "ok": True})
    assert await fut == {"type": "cmd.result", "id": "cmd-1", "ok": True}
    fut2 = asyncio.get_running_loop().create_task(t.cmd("light", {"pattern": "off"}))
    await settle()
    await clock.advance(3000)
    assert await fut2 is None and t.cmd_timeouts == 1  # acceptance 21
    assert len([m for m in t.sent_json() if m["type"] == "cmd"]) == 2  # no automatic resend
    t.client_send({"type": "cmd.result", "id": "cmd-2", "ok": True})  # late result is ignored
    await settle()
    t.client_close()
    await task


async def test_write_timeout_marks_peer_dead() -> None:
    class Stuck(LoopbackTransport):
        async def _raw_send(self, data: str | bytes) -> None:
            if isinstance(data, str) and '"stt"' in data:
                await self.clock.sleep(10**9)
            await super()._raw_send(data)

    clock = FakeClock()
    t = Stuck(clock=clock, timers=TurnTimers(), ids=IdGen(), session_id="S-1")
    await handshake(t)
    fut = asyncio.get_running_loop().create_task(t.send_json({"type": "stt", "turn_id": "c1", "text": "", "final": True}))
    await settle()
    await clock.advance(TurnTimers().T_write)
    with pytest.raises(TransportClosed):
        await fut
    assert t.peer_dead and t.closed
