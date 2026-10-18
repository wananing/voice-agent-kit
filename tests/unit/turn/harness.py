"""Builds a TurnMachine over the fakes with a FakeClock and some wire helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tests.unit.turn.fakes import (
    FakeArbiter,
    FakeContext,
    FakeCues,
    FakeEndpointer,
    FakeTools,
    FakeTrace,
    FakeTransport,
    FakeUpstream,
)
from vak.core.bus import Bus
from vak.core.clock import FakeClock
from vak.core.defaults import EndpointingParams, TurnTimers
from vak.core.ids import IdGen
from vak.core.turn.machine import ClientCaps, TurnMachine
from vak.core.types import Mode, Profile, TurnId
from vak.upstream.base import EventType, ResponseOrigin, ResponseStatus, UpstreamEvent
from vak.upstream.capabilities import Capabilities, CapField

RATE = 16000


def ms_to_samples(ms: int) -> int:
    return ms * RATE // 1000


def caps_s2s() -> Capabilities:
    caps = Capabilities.all_unknown("fake")
    caps.fields["response.client_initiated"] = CapField("yes")
    caps.fields["cascade.user_transcript"] = CapField("n/a")
    caps.fields["turn.commit_mode"] = CapField("commit_create")
    return caps


FULL_CAPS = {
    "aec": "device",
    "modes": ["ptt", "open_mic"],
    "sources": ["button", "wake", "text"],
    "receipts": ["started", "ended", "interrupted"],
    "audio_up": {"codec": "pcm16", "rate": RATE, "channels": 1, "frame_ms": 20},
    "play_buffer_max_ms": 1200,
    "preroll_ms": 250,
    "uninterruptible": True,
}


@dataclass
class Harness:
    clock: FakeClock
    bus: Bus
    transport: FakeTransport
    arbiter: FakeArbiter
    upstream: FakeUpstream
    context: FakeContext
    tools: FakeTools
    trace: FakeTrace
    endpointer: FakeEndpointer | None
    machine: TurnMachine
    seq: int = 0
    _ev_seq: int = 0

    # ---- client → gateway
    async def start(self, tid: str, source: str = "button", ts: int = 0, **extra: Any) -> None:
        await self.machine.handle_message({"type": "turn.start", "turn_id": tid, "source": source, "ts_samples": ts, **extra})

    async def end(self, tid: str, ts: int, reason: str = "release", source: str = "button") -> None:
        await self.machine.handle_message({"type": "turn.end", "turn_id": tid, "source": source, "ts_samples": ts, "reason": reason})

    async def audio(self, ts: int, turn_u32: int = 0, n_ms: int = 20, flags: int = 0) -> None:
        self.seq += 1
        await self.machine.handle_audio(turn_u32, self.seq, ts, flags, b"\x00\x00" * ms_to_samples(n_ms))

    async def abort(self, tid: str, reason: str = "button") -> None:
        await self.machine.handle_message({"type": "abort", "turn_id": tid, "reason": reason})

    async def receipt(self, kind: str, tid: str, sentence_id: int = 0, played_ms: int = 0, reason: str | None = None) -> None:
        msg: dict[str, Any] = {"type": f"playback.{kind}", "turn_id": tid, "sentence_id": sentence_id,
                               "played_ms": played_ms, "ts_ms": self.clock.now_ms()}
        if reason:
            msg["reason"] = reason
        await self.machine.handle_message(msg)

    # ---- upstream → gateway
    def _ev(self, t: EventType, **kw: Any) -> UpstreamEvent:
        self._ev_seq += 1
        return UpstreamEvent(type=t, seq=self._ev_seq, t_local_ms=self.clock.now_ms(), **kw)

    async def ev(self, t: EventType, **kw: Any) -> None:
        await self.machine.on_upstream_event(self._ev(t, **kw))

    async def transcript_final(self, text: str = "hello", tid: str | None = None) -> None:
        await self.ev(EventType.TRANSCRIPT_USER_FINAL, text=text, is_final=True,
                      turn_id=TurnId.parse(tid) if tid else None)

    async def response_started(self, rid: str = "r1", origin: ResponseOrigin = ResponseOrigin.CLIENT) -> None:
        await self.ev(EventType.RESPONSE_STARTED, response_id=rid, item_id=f"item-{rid}", origin=origin)

    async def audio_delta(self, rid: str = "r1", ms: int = 200) -> None:
        await self.ev(EventType.RESPONSE_AUDIO_DELTA, response_id=rid, item_id=f"item-{rid}",
                      audio=b"\x00\x00" * ms_to_samples(ms), sample_rate=RATE)

    async def response_done(self, rid: str = "r1", status: ResponseStatus = ResponseStatus.COMPLETED) -> None:
        await self.ev(EventType.RESPONSE_DONE, response_id=rid, status=status)

    async def drained(self, tid: str) -> None:
        await self.bus.emit("turn.audio_drained", turn_id=TurnId.parse(tid))

    async def full_reply(self, tid: str, rid: str = "r1", text: str = "hello") -> None:
        """transcript → response with audio → done → receipts → drained."""
        await self.transcript_final(text)
        await self.response_started(rid)
        await self.audio_delta(rid)
        await self.response_done(rid)
        await self.receipt("started", tid)
        await self.receipt("ended", tid, played_ms=200)
        await self.drained(tid)

    # ---- assertions
    def closed(self, tid: str) -> list[dict[str, Any]]:
        return [m for m in self.transport.of_type("turn.closed") if m["turn_id"] == tid]

    def errors(self, tid: str | None = None) -> list[dict[str, Any]]:
        return [m for m in self.transport.of_type("error") if tid is None or m["turn_id"] == tid]

    def types(self) -> list[str]:
        return [m["type"] for m in self.transport.sent]


def make(
    *,
    mode: Mode = Mode.PTT,
    profile: Profile = Profile.A,
    endpointer: FakeEndpointer | None = None,
    client_caps: dict[str, Any] | None = None,
    upstream_caps: Capabilities | None = None,
    cues: bool = False,
    timers: TurnTimers | None = None,
) -> Harness:
    clock = FakeClock()
    bus = Bus()
    bus.record = True
    transport = FakeTransport()
    arbiter = FakeArbiter(transport)
    upstream = FakeUpstream(upstream_caps or caps_s2s())
    context = FakeContext()
    tools = FakeTools()
    trace = FakeTrace()
    machine = TurnMachine(
        profile=profile,
        mode=mode,
        timers=timers or TurnTimers(),
        endpointing=EndpointingParams(),
        clock=clock,
        bus=bus,
        transport=transport,
        arbiter=arbiter,
        upstream=upstream,
        context=context,
        tools=tools,
        trace=trace,
        ids=IdGen(),
        endpointer=endpointer,
        client_caps=ClientCaps.from_hello(FULL_CAPS if client_caps is None else client_caps),
        cues=FakeCues() if cues else None,
    )
    return Harness(clock, bus, transport, arbiter, upstream, context, tools, trace, endpointer, machine)
