"""Spec 01 §6.3 resume table rows, as seen from the turn machine."""

from __future__ import annotations

from tests.unit.turn.harness import make
from tests.unit.turn.harness import ms_to_samples as S
from vak.core.defaults import TurnTimers
from vak.core.turn.machine import ResumeInfo
from vak.core.types import TurnState


async def test_row_listening_continues():
    h = make()
    await h.start("c1")
    t = h.clock.now_ms()
    await h.clock.advance(2000)
    open_turns = await h.machine.on_resume(ResumeInfo(ok=True, disconnected_at_ms=t))
    assert open_turns == [{"turn_id": "c1", "state": "listening"}]
    assert h.machine.state is TurnState.LISTENING
    await h.end("c1", ts=S(3000))  # key released during the gap: client replays turn.end
    assert h.machine.state is TurnState.COMMITTED


async def test_row_committed_expired_watchdog_is_resent():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    t = h.clock.now_ms()
    await h.clock.advance(TurnTimers().T_asr)  # fired while disconnected
    before = len(h.transport.sent)
    await h.machine.on_resume(ResumeInfo(ok=True, disconnected_at_ms=t))
    resent = [m["type"] for m in h.transport.sent[before:]]
    assert resent == ["error", "turn.closed"]
    assert len(h.closed("c1")) == 2 and h.closed("c1")[1]["by"] == "asr_timeout"


async def test_row_responding_is_interrupted_by_link():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.transcript_final()
    await h.response_started("r1")
    await h.audio_delta("r1", ms=1000)
    t = h.clock.now_ms()
    await h.machine.on_resume(ResumeInfo(ok=True, disconnected_at_ms=t))
    assert h.closed("c1") == []
    await h.receipt("interrupted", "c1", played_ms=300, reason="link")
    closed = h.closed("c1")[0]
    assert closed["state"] == "interrupted" and closed["by"] == "link" and closed["heard_ms"] == 300
    assert h.trace.merged("c1")["interrupt_source"] == "link"
    # audio sent before the gap is never resent: nothing after turn.closed
    idx = h.transport.log.index(("json", closed))
    assert [k for k, _ in h.transport.log[idx:] if k == "audio"] == []


async def test_row_draining_without_receipt_estimates():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    await h.transcript_final()
    await h.response_started("r1")
    await h.audio_delta("r1")
    await h.response_done("r1")
    assert h.machine.state is TurnState.DRAINING
    await h.machine.on_resume(ResumeInfo(ok=True, disconnected_at_ms=h.clock.now_ms()))
    await h.clock.advance(TurnTimers().T_intr_receipt)
    assert h.closed("c1")[0]["by"] == "link" and h.trace.merged("c1")["truncate_estimated"] is True


async def test_resume_failed_fails_all_open_turns_without_frames():
    h = make()
    await h.start("c1")
    await h.end("c1", ts=S(1000))
    before = len(h.transport.sent)
    await h.machine.on_resume(ResumeInfo(ok=False, disconnected_at_ms=0))
    assert h.transport.sent[before:] == []  # session.closed settles them (R2.4-1)
    tr = h.trace.merged("c1")
    assert tr["turn_terminal"] == "failed" and tr["turn_closed_by"] == "link_lost"
    assert h.machine.state is TurnState.IDLE
    await h.start("c2")  # machine is closed: ignored
    assert h.machine.state is TurnState.IDLE


async def test_row_idle_no_action():
    h = make()
    assert await h.machine.on_resume(ResumeInfo(ok=True, disconnected_at_ms=0)) == []
    assert h.transport.sent == []


async def test_uplink_gap_rules():
    h = make()
    await h.start("c1")
    await h.machine.on_uplink_gap(80)
    assert "audio_damaged" not in h.trace.merged("c1")
    await h.machine.on_uplink_gap(500)
    assert h.trace.merged("c1")["audio_damaged"] is True
    await h.machine.on_uplink_gap(2000)
    assert h.errors("c1")[0]["code"] == "audio_gap" and h.closed("c1")[0]["state"] == "failed"
