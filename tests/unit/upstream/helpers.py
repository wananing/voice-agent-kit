"""Shared helpers for upstream tests: build pipelines on mocks, drive turns,
collect events. Only depends on contract files and vak.upstream."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from vak.core.clock import FakeClock
from vak.core.types import HistoryFormat, HistoryItem, TurnId
from vak.upstream.base import AudioMeta, EventType, SessionInit, ToolDef, UpstreamEvent
from vak.upstream.pipelines.cascade import CascadeUpstream
from vak.upstream.pipelines.full_duplex import FullDuplexUpstream
from vak.upstream.pipelines.half_cascade import HalfCascadeUpstream
from vak.upstream.pipelines.s2s import S2SUpstream
from vak.upstream.providers.mock import (
    MockAudioLLM,
    MockFullDuplex,
    MockLLM,
    MockRealtimeS2S,
    MockReply,
    MockSTT,
    MockTTS,
    mock_caps,
    tone_pcm,
)

T1 = TurnId("c", 1)
T2 = TurnId("c", 2)
TOOL = ToolDef(name="get_time", description="time", params={"type": "object", "properties": {}})
TOOL_CALL = {"id": "call-1", "name": "get_time", "arguments": {}}


async def settle(n: int = 30) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def until(up: Any, t: EventType, max_ticks: int = 200) -> list[UpstreamEvent]:
    """Tick the loop until an event of type `t` has been emitted; returns everything drained so far."""
    got: list[UpstreamEvent] = []
    for _ in range(max_ticks):
        got.extend(drain(up))
        if any(e.type is t for e in got):
            return got
        await asyncio.sleep(0)
    raise AssertionError(f"no {t.value} within {max_ticks} ticks")


def drain(up: Any) -> list[UpstreamEvent]:
    return up._emitter.drain()


def types(evs: list[UpstreamEvent]) -> list[str]:
    return [e.type.value for e in evs]


def of(evs: list[UpstreamEvent], t: EventType) -> list[UpstreamEvent]:
    return [e for e in evs if e.type is t]


def make_init(history: list[HistoryItem] | None = None, tools: list[ToolDef] | None = None, **kw: Any) -> SessionInit:
    return SessionInit(
        user_session_id="u1",
        session_seq=1,
        instructions="你是向导。",
        state_block_version="1.0",
        tools=tools if tools is not None else [TOOL],
        history=history or [],
        history_format=kw.pop("history_format", HistoryFormat.PAIRS),
        **kw,
    )


def frame(ms: int = 100, sr: int = 16000) -> bytes:
    return tone_pcm(ms, sr)


async def send_frames(up: Any, turn: TurnId = T1, n: int = 4, ms: int = 100, sr: int = 16000) -> None:
    for i in range(n):
        ack = await up.send_audio(frame(ms, sr), AudioMeta(turn, i, sr * ms // 1000, i * ms, "button", sr))
        assert ack.ok, ack


class Built:
    """A pipeline plus the mocks behind it."""

    def __init__(self, name: str, up: Any, clock: FakeClock, **mocks: Any) -> None:
        self.name = name
        self.up = up
        self.clock = clock
        self.mocks = mocks

    def __getattr__(self, item: str) -> Any:
        return self.mocks[item]


def build(
    name: str,
    clock: FakeClock | None = None,
    script: list[MockReply] | None = None,
    guardrail: Any = None,
    caps_overrides: dict[str, Any] | None = None,
    **mock_kw: Any,
) -> Built:
    clock = clock or FakeClock()
    script = list(script or [])
    if name == "cascade":
        stt_kw = {k: v for k, v in mock_kw.items() if k in ("final_on_finish", "transcript")}
        stts: list[MockSTT] = []

        def stt_factory() -> MockSTT:
            s = MockSTT(**stt_kw)
            stts.append(s)
            return s

        llm = MockLLM(script, clock, late_delta_after_cancel=mock_kw.get("late", False))
        tts = MockTTS(clock=clock, late_chunk_after_flush=mock_kw.get("late", False))
        up = CascadeUpstream(stt_factory, llm, tts, clock=clock, guardrail=guardrail)
        return Built(name, up, clock, llm=llm, tts=tts, stts=stts)
    if name == "half_cascade":
        stt_kw = {k: v for k, v in mock_kw.items() if k in ("final_on_finish", "transcript")}
        stts = []

        def side_factory() -> MockSTT:
            s = MockSTT(**stt_kw)
            stts.append(s)
            return s

        allm = MockAudioLLM(script, streaming_input=mock_kw.get("streaming_input", False), clock=clock,
                            late_delta_after_cancel=mock_kw.get("late", False))
        tts = MockTTS(clock=clock, late_chunk_after_flush=mock_kw.get("late", False))
        factory = None if mock_kw.get("no_side_stt") else side_factory
        up = HalfCascadeUpstream(allm, tts, factory, clock=clock, guardrail=guardrail,
                                 backpressure_ms=mock_kw.get("backpressure_ms", 30_000))
        return Built(name, up, clock, llm=allm, tts=tts, stts=stts)
    if name == "s2s":
        allowed = inspect.signature(MockRealtimeS2S.__init__).parameters
        prov_kw = {k: v for k, v in mock_kw.items() if k in allowed}
        if mock_kw.get("late"):
            prov_kw["late_deltas_after_cancel"] = True
        provider = MockRealtimeS2S(clock, script=script, **prov_kw)
        up = S2SUpstream(provider, mock_caps(**(caps_overrides or {})), clock=clock)
        return Built(name, up, clock, provider=provider)
    model = MockFullDuplex(clock=clock, **{k: v for k, v in mock_kw.items() if k in ("bursts",)})
    up = FullDuplexUpstream(model, clock=clock)
    return Built(name, up, clock, model=model)
