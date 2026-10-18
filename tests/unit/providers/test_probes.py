"""Probe assets, the §7.4 decision rule, and the harness on a scripted Upstream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Literal

import numpy as np
import pytest

from vak.core.clock import FakeClock
from vak.core.types import ErrorClass, ErrorReason, ErrorScope, ReplyModality, TurnId
from vak.probes import (
    ProbeAssets,
    ProbeContext,
    apply_report,
    conservative,
    decide,
    run_probes,
    threshold_step,
)
from vak.probes.assets import SILENCE_STEPS_MS, SNIPPET_STEPS_MS, tone, tone_with_gap
from vak.upstream.base import (
    Ack,
    AudioMeta,
    ConfigPatch,
    EventType,
    ResponseOrigin,
    ResponseOverrides,
    ResponseStatus,
    SessionInit,
    UpstreamError,
    UpstreamEvent,
)
from vak.upstream.capabilities import Capabilities, Evidence

# ---------------------------------------------------------------------------
# decision rule + assets
# ---------------------------------------------------------------------------


def test_decision_rule_20_of_20_yes_0_no_else_flaky():
    assert decide(20, 20) == "yes"
    assert decide(0, 20) == "no"
    assert decide(7, 20) == "flaky(7/20)"
    assert decide(0, 0) == "unknown"
    assert conservative("tools.auto_reply_after_result", "flaky(7/20)") == "yes"
    assert conservative("turn.server_detection_disable", "flaky(1/20)") == "no"
    assert conservative("turn.server_detection_disable", "yes") == "yes"


def test_threshold_step_first_over_50_percent():
    assert threshold_step({200: 0.0, 300: 0.2, 400: 0.5, 500: 0.8, 700: 1.0}) == 500
    assert threshold_step({200: 0.0, 700: 0.1}) is None


def test_assets_sizes_and_silence_positions():
    a = ProbeAssets(sample_rate=16000)
    assert len(a.utterance(2000)) == 2 * 32000
    assert len(a.utterance_then_silence(2000, 1000)) == 2 * 48000
    gaps = a.gap_samples()
    assert set(gaps) == set(SILENCE_STEPS_MS)
    for g, pcm in gaps.items():
        assert len(pcm) == 2 * (16000 * (3000 + g) // 1000)
        x = np.frombuffer(pcm, dtype=np.int16)
        burst = 16000 * 1500 // 1000
        gap_n = 16000 * g // 1000
        assert not x[burst : burst + gap_n].any()  # inserted silence is digital zero
        assert np.abs(x[:burst]).max() > 5000 and np.abs(x[burst + gap_n :]).max() > 5000
    snippets = a.snippets()
    assert {ms: len(p) for ms, p in snippets.items()} == {ms: 2 * 16 * ms for ms in SNIPPET_STEPS_MS}
    assert len(a.noise(800)) == 2 * 12800
    frames = a.frames(a.utterance(100))
    assert len(frames) == 5 and all(len(f) == 640 for f in frames)
    assert tone(0) == b"" and len(tone_with_gap(400, 8000, burst_ms=100)) == 2 * (8000 * 600 // 1000)


def test_assets_speech_fallback_and_wav_loading(tmp_path):
    import wave

    with wave.open(str(tmp_path / "ask_tool.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x00" * 1600)
    a = ProbeAssets(speech_dir=tmp_path)
    assert a.has_speech("ask_tool") and not a.has_speech("ask_fact")
    assert a.speech("ask_tool") == b"\x01\x00" * 1600
    assert len(a.speech("ask_fact", fallback_ms=500)) == 2 * 8000  # tone stand-in


# ---------------------------------------------------------------------------
# a scripted upstream implementing the nine primitives
# ---------------------------------------------------------------------------


class ScriptedUpstream:
    """Deterministic Upstream whose behaviour is chosen per instance."""

    def __init__(
        self,
        clock: FakeClock,
        *,
        auto_reply_after_tool: bool = False,
        server_vad: bool = True,
        single_slot: bool = True,
        commit_min_ms: int = 100,
        turn_detection: Any = "default",
    ) -> None:
        self.clock = clock
        self.auto_reply = auto_reply_after_tool
        self.server_vad = server_vad if turn_detection != "__none__" else False
        self.turn_detection = turn_detection
        self.single_slot = single_slot
        self.commit_min_ms = commit_min_ms
        self.capabilities = Capabilities.all_unknown("scripted")
        self._q: asyncio.Queue[UpstreamEvent | None] = asyncio.Queue()
        self._seq = 0
        self._buffered_ms = 0
        self._active: str | None = None
        self._n = 0
        self.tool_results: list[tuple[str, bool]] = []

    def _emit(self, t: EventType, **f: Any) -> None:
        self._seq += 1
        self._q.put_nowait(UpstreamEvent(type=t, seq=self._seq, t_local_ms=self.clock.now_ms(), **f))

    async def open(self, init: SessionInit) -> Ack:
        self._emit(EventType.SESSION_OPENED, effective={})
        return Ack.success()

    async def send_audio(self, frame: bytes, meta: AudioMeta) -> Ack:
        self._buffered_ms += len(frame) // 2 * 1000 // meta.sample_rate
        loud = max(abs(int.from_bytes(frame[i : i + 2], "little", signed=True)) for i in range(0, min(len(frame), 200), 2))
        if self.server_vad and not loud and self._buffered_ms >= 2500 and not self._active:
            # 2 s tone then silence: server VAD segments and (create_response) replies
            self._emit(EventType.INPUT_SPEECH_STOPPED, source="server_vad")
            self._buffered_ms = 0
            td = self.turn_detection if isinstance(self.turn_detection, dict) else {}
            if td.get("create_response", True):
                self._start_response(ResponseOrigin.SERVER_VAD)
        return Ack.success()

    def _start_response(self, origin: ResponseOrigin, ref: str | None = None) -> str:
        self._n += 1
        rid = f"r{self._n}"
        self._active = rid
        self._emit(EventType.RESPONSE_STARTED, response_id=rid, response_ref=ref, origin=origin)
        self._emit(EventType.RESPONSE_AUDIO_DELTA, response_id=rid, item_id=f"i{self._n}", audio=bytes(960), sample_rate=24000, duration_ms=20)
        return rid

    async def commit_turn(self, turn_id: TurnId, mode: Literal["commit", "discard"]) -> Ack:
        if mode == "discard":
            self._buffered_ms = 0
            return Ack.success()
        if self._buffered_ms == 0:
            self._emit(EventType.ERROR, error=UpstreamError(ErrorClass.BENIGN, ErrorReason.EMPTY_COMMIT, ErrorScope.ITEM, raw_code="input_audio_buffer_commit_empty"))
            return Ack.success()
        if self._buffered_ms < self.commit_min_ms:
            self._emit(EventType.ERROR, error=UpstreamError(ErrorClass.BENIGN, ErrorReason.ITEM_REJECTED, ErrorScope.ITEM, raw_code="input_audio_buffer_commit_too_short"))
            return Ack.success()
        self._buffered_ms = 0
        self._emit(EventType.INPUT_COMMITTED, committed_by="client")
        return Ack.success()

    async def request_response(self, turn_id: TurnId, response_ref: str, overrides: ResponseOverrides | None = None) -> Ack:
        if self._active and self.single_slot:
            return Ack.slot_busy()
        rid = self._start_response(ResponseOrigin.CLIENT, response_ref)
        if overrides and overrides.tool_choice:
            self._emit(EventType.TOOL_CALL, response_id=rid, call_id="call_1", name=overrides.tool_choice, args={"text": "x"})
        asyncio.get_running_loop().create_task(self._finish_later(rid))
        return Ack.success(response_id=rid)

    async def _finish_later(self, rid: str) -> None:
        await self.clock.sleep(300)  # the slot stays busy for a while, like a real reply
        if self._active == rid:
            self._finish(rid)

    def _finish(self, rid: str) -> None:
        self._emit(EventType.RESPONSE_DONE, response_id=rid, status=ResponseStatus.COMPLETED, reply_modality=ReplyModality.AUDIO, confirmed=True)
        if self._active == rid:
            self._active = None

    async def cancel_response(self, response_id: str) -> Ack:
        self._emit(EventType.RESPONSE_DONE, response_id=response_id, status=ResponseStatus.CANCELLED, confirmed=True)
        self._active = None
        return Ack.success()

    async def truncate(self, item_id: str, audio_end_ms: int, played_text: str | None = None) -> Ack:
        return Ack.success()

    async def send_tool_result(self, call_id: str, result: dict[str, Any], reply_required: bool) -> Ack:
        self.tool_results.append((call_id, reply_required))
        if self.auto_reply:
            rid = self._start_response(ResponseOrigin.AUTO_TOOL)
            self._finish(rid)
        return Ack.success()

    async def update_config(self, patch: ConfigPatch, mode: Literal["live", "append", "next_session"]) -> Ack:
        return Ack.success()

    async def close(self, reason: str) -> Ack:
        self._emit(EventType.SESSION_CLOSED, reason=reason)
        await self._q.put(None)
        return Ack.success()

    def events(self) -> AsyncIterator[UpstreamEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[UpstreamEvent]:
        while True:
            ev = await self._q.get()
            if ev is None:
                return
            yield ev


async def drive(clock: FakeClock, coro):
    """Run `coro` while advancing the fake clock so probe timers elapse."""
    task = asyncio.create_task(coro)
    while not task.done():
        await clock.advance(100)
    return task.result()


def ctx_for(clock: FakeClock, n: int = 3, **up_kw) -> ProbeContext:
    async def factory(**kw):
        td = kw.get("turn_detection", "default")
        return ScriptedUpstream(clock, turn_detection="__none__" if ("turn_detection" in kw and td is None) else td, **up_kw)

    caps = Capabilities.all_unknown("scripted")
    return ProbeContext(upstream_factory=factory, assets=ProbeAssets(frame_ms=100), caps=caps, n=n, clock=clock)


# ---------------------------------------------------------------------------
# probes on the scripted upstream
# ---------------------------------------------------------------------------


async def test_p0_1_no_auto_reply_gives_no_and_by_omission():
    clock = FakeClock()
    ctx = ctx_for(clock)
    report = await drive(clock, run_probes(["P0-1"], ctx))
    by_field = {r.field: r for r in report.results}
    assert by_field["tools.auto_reply_after_result"].value == "no"
    assert by_field["tools.silent_close"].value == "by_omission"
    assert len(by_field["tools.auto_reply_after_result"].refs) == 3 and by_field["tools.auto_reply_after_result"].raw["n"] == 3


async def test_p0_1_auto_reply_gives_yes_and_unknown_silent_close():
    clock = FakeClock()
    ctx = ctx_for(clock, auto_reply_after_tool=True)
    report = await drive(clock, run_probes(["P0-1"], ctx))
    by_field = {r.field: r for r in report.results}
    assert by_field["tools.auto_reply_after_result"].value == "yes"
    assert by_field["tools.silent_close"].value == "unknown"


async def test_p0_1_flaky_written_back_as_conservative():
    clock = FakeClock()
    toggle = {"i": 0}

    async def factory(**kw):
        toggle["i"] += 1
        return ScriptedUpstream(clock, auto_reply_after_tool=toggle["i"] % 2 == 0)

    caps = Capabilities.all_unknown("scripted")
    ctx = ProbeContext(upstream_factory=factory, assets=ProbeAssets(frame_ms=100), caps=caps, n=4, clock=clock)
    report = await drive(clock, run_probes(["P0-1"], ctx))
    r = next(r for r in report.results if r.field == "tools.auto_reply_after_result")
    assert r.value == "flaky(2/4)"
    written = apply_report(caps, report, probed_at="2026-10-11")
    assert "tools.auto_reply_after_result" in written
    f = caps.raw("tools.auto_reply_after_result")
    assert f.value == "yes" and f.evidence is Evidence.PROBED and f.refs == r.refs
    assert caps.probed_at == "2026-10-11"


async def test_p0_2_detection_disable_and_segment_only():
    clock = FakeClock()
    ctx = ctx_for(clock)
    report = await drive(clock, run_probes(["P0-2"], ctx))
    by_field = {r.field: r for r in report.results}
    assert by_field["turn.server_detection_disable"].value == "yes"  # turn_detection=None -> no server events
    assert by_field["turn.segment_only"].value == "yes"  # create_response False -> segmented, no reply
    assert by_field["turn.server_interrupt_optional"].value == "yes"  # scripted upstream never cancels on noise


async def test_p0_4_commit_min_and_empty_commit():
    clock = FakeClock()
    ctx = ctx_for(clock, commit_min_ms=100)
    report = await drive(clock, run_probes(["P0-4"], ctx))
    by_field = {r.field: r for r in report.results}
    assert by_field["turn.commit_min_ms"].value == 100
    assert by_field["turn.empty_commit_behavior"].value == "error(input_audio_buffer_commit_empty)"
    assert by_field["turn.commit_min_ms"].raw["accepted"] == {50: 0, 100: 3, 150: 3}


async def test_p0_8_single_slot_detected_from_slot_busy():
    clock = FakeClock()
    ctx = ctx_for(clock, single_slot=True)
    report = await drive(clock, run_probes(["P0-8"], ctx))
    assert report.results[0].field == "response.single_slot" and report.results[0].value == "yes"


async def test_p0_9_and_14_skip_without_hooks_and_stubs_are_listed():
    clock = FakeClock()
    ctx = ctx_for(clock)
    report = await drive(clock, run_probes(["P0-9", "P0-14", "P0-5", "P0-16"], ctx))
    assert all(not r.applicable for r in report.results)
    skipped = {r.field: r.skipped for r in report.results}
    assert "check_text" in skipped["history.mid_session_effective"] and "check_text" in skipped["truncate.mode"]
    assert any(r.probe == "P0-5" and not r.implemented for r in report.results)
    assert apply_report(ctx.caps, report) == []
    assert "skipped" in report.summary()


async def test_unknown_probe_id_rejected_and_dump_roundtrip(tmp_path):
    clock = FakeClock()
    ctx = ctx_for(clock)
    with pytest.raises(KeyError):
        await run_probes(["P0-99"], ctx)
    report = await drive(clock, run_probes(["P0-8"], ctx))
    apply_report(ctx.caps, report)
    ctx.caps.dump(tmp_path / "out.yaml")
    back = Capabilities.load(tmp_path / "out.yaml")
    assert back.raw("response.single_slot").evidence is Evidence.PROBED and back.get("response.single_slot") == "yes"
