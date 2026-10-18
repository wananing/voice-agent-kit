from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.unit.tools import sample_tools
from tests.unit.tools.fakes import (
    FakeArbiter,
    FakeContext,
    FakeCues,
    FakeTrace,
    FakeUpstream,
    make_caps,
    settle,
    tool_call_event,
)
from vak.core.bus import Bus
from vak.core.clock import FakeClock
from vak.core.defaults import ToolParams
from vak.core.ids import IdGen
from vak.core.tools import Registry, ToolLayer
from vak.core.tools.gates import clear_checkers, register_checker
from vak.core.types import ToolCall, TurnId


@dataclass
class Harness:
    clock: FakeClock
    bus: Bus
    arbiter: FakeArbiter
    upstream: FakeUpstream
    context: FakeContext
    trace: FakeTrace
    cues: FakeCues
    ids: IdGen
    registry: Registry
    params: ToolParams
    layer: ToolLayer
    phase: list[str] = field(default_factory=lambda: ["global"])
    knobs: dict[str, Any] = field(default_factory=dict)

    async def call(self, call_id: str, name: str, args: dict | None = None, turn: str = "c1", *,
                   before_commit: bool = False) -> ToolCall:
        await self.layer.on_tool_call(tool_call_event(call_id, name, args, TurnId.parse(turn), before_commit=before_commit))
        await settle()
        return self.layer.calls[call_id]

    def sent(self, call_id: str) -> list[tuple[dict, bool]]:
        return self.upstream.for_call(call_id)

    async def barge_in(self) -> int:
        self.arbiter.generation += 1
        await self.layer.on_barge_in(self.arbiter.generation)
        await settle()
        return self.arbiter.generation


def build(profile: str = "A", silent_close: str = "by_omission", delegate: Any = None, cues: bool = True,
          phase: str = "global") -> Harness:
    clock = FakeClock()
    sample_tools.reset_knobs(clock)
    params = ToolParams()
    registry = Registry(sample_tools.ALL, profile, params=params, registry_version="test.1")
    arbiter = FakeArbiter(generation=1)
    upstream = FakeUpstream(make_caps(silent_close))
    phase_box = [phase]
    h = Harness(
        clock=clock, bus=Bus(), arbiter=arbiter, upstream=upstream, context=FakeContext(), trace=FakeTrace(),
        cues=FakeCues(), ids=IdGen(), registry=registry, params=params, layer=None,  # type: ignore[arg-type]
        phase=phase_box, knobs=sample_tools.KNOBS,
    )
    h.layer = ToolLayer(
        registry=registry, profile=profile, params=params, clock=clock, bus=h.bus, arbiter=arbiter, upstream=upstream,
        context=h.context, trace=h.trace, ids=h.ids, cues=h.cues if cues else None, caps=upstream.capabilities,
        delegate=delegate, phase_provider=lambda: phase_box[0],
    )
    return h


@pytest.fixture(autouse=True)
def _checkers():
    clear_checkers()

    async def ok(args, ctx):
        return True

    register_checker("flag_ok", ok)
    register_checker("no_active_request", ok)
    yield
    clear_checkers()


@pytest.fixture
def h() -> Harness:
    return build()
