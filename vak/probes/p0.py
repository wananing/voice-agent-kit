"""P0 probe harness (spec 04 §7).

Each `Probe` drives an `Upstream` (the nine-primitive contract, so the same
harness runs on any pipeline) through its primitives, records the
standardised events, judges the outcome with the §7.4 rule and writes the
result back into a `Capabilities` object with `evidence: probed`.

Implemented: P0-1, P0-2, P0-3, P0-4, P0-8 (single slot), P0-9 (needs the
`inject_fact` and `check_text` hooks), P0-14 (needs `check_text`). The other
items are registered as stubs that report `implemented=False` so a report
always lists the whole §7.3 table.

The factory must build the upstream with its *static* capability file: the
probes exercise primitives whose degradation table reads the declaration
(e.g. `send_tool_result(reply_required=False)` is only sent when
`tools.auto_reply_after_result` is declared `no`). Keyword arguments the
harness passes to the factory: `turn_detection` (dict | None),
`history` (list[HistoryItem]), `tools` (list[ToolDef]).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from vak.core.clock import Clock, MonotonicClock
from vak.core.types import HistoryFormat, HistoryItem, ReplyModality, TurnId
from vak.probes.assets import SILENCE_STEPS_MS, SNIPPET_STEPS_MS, ProbeAssets
from vak.upstream.base import (
    Ack,
    AckCode,
    AudioMeta,
    EventType,
    ResponseOverrides,
    ResponseStatus,
    SessionInit,
    ToolDef,
    Upstream,
    UpstreamEvent,
)
from vak.upstream.capabilities import FIELD_SPECS, Capabilities

log = logging.getLogger(__name__)

UpstreamFactory = Callable[..., Awaitable[Upstream]]
CheckText = Callable[[str, str], bool]  # (check name, transcript) -> passed
InjectFact = Callable[[Upstream, str], Awaitable[Ack]]

PROBE_TOOL = ToolDef(
    name="probe_echo",
    description="Probe tool. Call it with any text.",
    params={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
)
FACT = "The user's dog is called Biscuit."

# ---------------------------------------------------------------------------
# results and the §7.4 decision rule
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProbeResult:
    probe: str
    field: str
    value: Any
    evidence: str = "probed"
    refs: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    implemented: bool = True
    skipped: str | None = None  # reason when the probe could not run

    @property
    def applicable(self) -> bool:
        return self.implemented and self.skipped is None


def decide(k: int, n: int) -> str:
    """Behavioural fields (spec 04 §7.4): n/n -> yes, 0/n -> no, else flaky(k/n)."""
    if n <= 0:
        return "unknown"
    if k == n:
        return "yes"
    if k == 0:
        return "no"
    return f"flaky({k}/{n})"


def conservative(field_name: str, value: Any) -> Any:
    """A flaky verdict is written as the field's conservative value (§7.4)."""
    if isinstance(value, str) and value.startswith("flaky("):
        return FIELD_SPECS[field_name][1]
    return value


def threshold_step(rates: dict[int, float]) -> int | None:
    """Threshold fields: first step whose rate exceeds 50 % (§7.4)."""
    for step in sorted(rates):
        if rates[step] > 0.5:
            return step
    return None


# ---------------------------------------------------------------------------
# context: factory + assets + hooks + a thin session helper
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProbeContext:
    upstream_factory: UpstreamFactory
    assets: ProbeAssets
    caps: Capabilities
    n: int = 20
    clock: Clock = field(default_factory=MonotonicClock)
    check_text: CheckText | None = None
    inject_fact: InjectFact | None = None
    instructions: str = "You are a voice assistant used in a protocol probe. Answer briefly."
    poll_ms: int = 50
    _seq: int = 0

    def session_init(self, **over: Any) -> SessionInit:
        self._seq += 1
        base: dict[str, Any] = {
            "user_session_id": "probe",
            "session_seq": self._seq,
            "instructions": self.instructions,
            "state_block_version": "0.0",
            "tools": [],
            "history": [],
            "history_format": HistoryFormat.PAIRS,
        }
        base.update(over)
        return SessionInit(**base)


class _Session:
    """One opened upstream with an event recorder."""

    def __init__(self, ctx: ProbeContext, upstream: Upstream, trace_id: str) -> None:
        self.ctx = ctx
        self.up = upstream
        self.trace_id = trace_id
        self.events: list[UpstreamEvent] = []
        self._turn = 0
        self._seq = 0
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        try:
            async for ev in self.up.events():
                self.events.append(ev)
        except Exception as e:  # recorder must never kill a probe
            log.warning("%s: event pump ended: %s", self.trace_id, e)

    def new_turn(self) -> TurnId:
        self._turn += 1
        return TurnId("c", self._turn)

    async def send(self, pcm16: bytes, turn_id: TurnId) -> None:
        for frame in self.ctx.assets.frames(pcm16):
            self._seq += 1
            meta = AudioMeta(
                turn_id=turn_id,
                seq=self._seq,
                sample_count=len(frame) // 2,
                captured_at_ms=self.ctx.clock.now_ms(),
                source="button",
                sample_rate=self.ctx.assets.sample_rate,
            )
            await self.up.send_audio(frame, meta)
            await self.ctx.clock.sleep(self.ctx.assets.frame_ms)  # real-time pacing (uplink.* unknown)

    async def wait(self, pred: Callable[[UpstreamEvent], bool], timeout_ms: int, *, start: int = 0) -> UpstreamEvent | None:
        deadline = self.ctx.clock.now_ms() + timeout_ms
        while True:
            for ev in self.events[start:]:
                if pred(ev):
                    return ev
            if self.ctx.clock.now_ms() >= deadline:
                return None
            await self.ctx.clock.sleep(self.ctx.poll_ms)

    async def settle(self, ms: int) -> None:
        await self.ctx.clock.sleep(ms)

    def count(self, type_: EventType, *, start: int = 0) -> int:
        return sum(1 for ev in self.events[start:] if ev.type is type_)

    def mark(self) -> int:
        return len(self.events)

    async def close(self) -> None:
        try:
            await self.up.close("probe_done")
        finally:
            self._task.cancel()

    def raw(self) -> dict[str, Any]:
        return {"trace_id": self.trace_id, "events": [f"{e.seq}:{e.type.value}" for e in self.events]}


async def _open(ctx: ProbeContext, trace_id: str, init: SessionInit | None = None, **factory_kw: Any) -> _Session:
    up = await ctx.upstream_factory(**factory_kw)
    s = _Session(ctx, up, trace_id)
    ack = await up.open(init or ctx.session_init(tools=factory_kw.get("tools", []), history=factory_kw.get("history", [])))
    if not ack.ok:
        await s.close()
        raise RuntimeError(f"{trace_id}: open failed: {ack.code} {ack.detail}")
    return s


async def _ask(s: _Session, pcm16: bytes, overrides: ResponseOverrides | None = None) -> tuple[TurnId, Ack]:
    turn = s.new_turn()
    await s.send(pcm16, turn)
    await s.up.commit_turn(turn, "commit")
    ack = await s.up.request_response(turn, f"rr-{s.trace_id}-{turn.n}", overrides)
    return turn, ack


def _is(type_: EventType) -> Callable[[UpstreamEvent], bool]:
    return lambda ev: ev.type is type_


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------


class Probe(Protocol):
    id: str
    fields: tuple[str, ...]

    async def run(self, ctx: ProbeContext) -> list[ProbeResult]: ...


class P0_1:
    """Tool output without response.create: does the upstream speak?"""

    id = "P0-1"
    fields = ("tools.auto_reply_after_result", "tools.silent_close")

    async def run(self, ctx: ProbeContext) -> list[ProbeResult]:
        spoke = 0
        refs: list[str] = []
        raw: dict[str, Any] = {"trials": []}
        for i in range(ctx.n):
            s = await _open(ctx, f"{self.id}-{i}", tools=[PROBE_TOOL])
            refs.append(s.trace_id)
            try:
                _turn, _ = await _ask(s, ctx.assets.speech("ask_tool"), ResponseOverrides(tool_choice=PROBE_TOOL.name))
                call = await s.wait(_is(EventType.TOOL_CALL), 5_000)
                if call is None:
                    raw["trials"].append({**s.raw(), "note": "no tool.call"})
                    continue
                mark = s.mark()
                ack = await s.up.send_tool_result(call.call_id or "", {"ok": True, "text": "pong"}, reply_required=False)
                if not ack.ok:
                    raw["trials"].append({**s.raw(), "note": f"send_tool_result refused: {ack.code} {ack.detail}"})
                    continue
                started = await s.wait(_is(EventType.RESPONSE_STARTED), 5_000, start=mark)
                spoke += started is not None
                raw["trials"].append({**s.raw(), "spoke": started is not None})
            finally:
                await s.close()
        n = len([t for t in raw["trials"] if "spoke" in t])
        auto = decide(spoke, n)
        silent = "by_omission" if auto == "no" else "unknown"
        return [
            ProbeResult(self.id, "tools.auto_reply_after_result", auto, refs=refs, raw={"spoke": spoke, "n": n, **raw}),
            ProbeResult(self.id, "tools.silent_close", silent, refs=refs, raw={"derived_from": auto}),
        ]


class P0_2:
    """Can server turn detection be disabled / limited to segmentation / kept from interrupting?"""

    id = "P0-2"
    fields = ("turn.server_detection_disable", "turn.segment_only", "turn.server_interrupt_optional")

    async def run(self, ctx: ProbeContext) -> list[ProbeResult]:
        quiet = 0  # trials with no server-side segmentation or response while turn_detection=None
        seg_only = 0
        not_interrupted = 0
        refs: list[str] = []
        raw: dict[str, Any] = {"disable": [], "segment_only": [], "interrupt": []}
        sample = ctx.assets.utterance_then_silence(2000, 1000)
        auto_types = (EventType.INPUT_SPEECH_STOPPED, EventType.INPUT_SEGMENTED, EventType.RESPONSE_STARTED)
        for i in range(ctx.n):
            s = await _open(ctx, f"{self.id}-disable-{i}", turn_detection=None)
            refs.append(s.trace_id)
            try:
                await s.send(sample, s.new_turn())
                await s.settle(3_000)
                hits = sum(s.count(t) for t in auto_types)
                quiet += hits == 0
                raw["disable"].append({**s.raw(), "auto_events": hits})
            finally:
                await s.close()
        for i in range(ctx.n):
            s = await _open(
                ctx,
                f"{self.id}-segment-{i}",
                turn_detection={"type": "server_vad", "create_response": False, "interrupt_response": False},
            )
            refs.append(s.trace_id)
            try:
                await s.send(sample, s.new_turn())
                await s.settle(3_000)
                segmented = s.count(EventType.INPUT_SPEECH_STOPPED) + s.count(EventType.INPUT_SEGMENTED) > 0
                replied = s.count(EventType.RESPONSE_STARTED) > 0
                seg_only += segmented and not replied
                raw["segment_only"].append({**s.raw(), "segmented": segmented, "replied": replied})
            finally:
                await s.close()
        for i in range(ctx.n):
            s = await _open(ctx, f"{self.id}-interrupt-{i}", turn_detection={"type": "server_vad", "interrupt_response": False})
            refs.append(s.trace_id)
            try:
                await _ask(s, ctx.assets.speech("ask_long"))
                first = await s.wait(_is(EventType.RESPONSE_AUDIO_DELTA), 5_000)
                if first is None:
                    raw["interrupt"].append({**s.raw(), "note": "no audio"})
                    continue
                rid = first.response_id
                await s.send(ctx.assets.noise(800), s.new_turn())
                done = await s.wait(lambda ev, rid=rid: ev.type is EventType.RESPONSE_DONE and ev.response_id == rid, 8_000)
                cancelled = done is not None and done.status is ResponseStatus.CANCELLED
                not_interrupted += not cancelled
                raw["interrupt"].append({**s.raw(), "cancelled": cancelled})
            finally:
                await s.close()
        n_int = len([t for t in raw["interrupt"] if "cancelled" in t])
        return [
            ProbeResult(self.id, "turn.server_detection_disable", decide(quiet, ctx.n), refs=refs, raw={"quiet": quiet, "n": ctx.n, "trials": raw["disable"]}),
            ProbeResult(self.id, "turn.segment_only", decide(seg_only, ctx.n), refs=refs, raw={"segment_only": seg_only, "n": ctx.n, "trials": raw["segment_only"]}),
            ProbeResult(self.id, "turn.server_interrupt_optional", decide(not_interrupted, n_int), refs=refs, raw={"not_interrupted": not_interrupted, "n": n_int, "trials": raw["interrupt"]}),
        ]


class P0_3:
    """Server VAD silence threshold from the 200..700 ms gap ladder."""

    id = "P0-3"
    fields = ("turn.server_silence_ms",)

    async def run(self, ctx: ProbeContext) -> list[ProbeResult]:
        rates: dict[int, float] = {}
        refs: list[str] = []
        raw: dict[str, Any] = {"per_gap": {}}
        for gap, pcm in ctx.assets.gap_samples().items():
            split = 0
            for i in range(ctx.n):
                s = await _open(ctx, f"{self.id}-{gap}-{i}", turn_detection={"type": "server_vad", "create_response": False})
                refs.append(s.trace_id)
                try:
                    await s.send(pcm, s.new_turn())
                    await s.settle(2_000)
                    stops = s.count(EventType.INPUT_SPEECH_STOPPED) + s.count(EventType.INPUT_SEGMENTED)
                    split += stops >= 2  # one stop at the gap, one at the end
                finally:
                    await s.close()
            rates[gap] = split / ctx.n
            raw["per_gap"][gap] = {"split": split, "n": ctx.n}
        thr = threshold_step(rates)
        value: Any = thr if thr is not None else f">{max(SILENCE_STEPS_MS)}"
        return [ProbeResult(self.id, "turn.server_silence_ms", value, refs=refs, raw={"rates": rates, **raw})]


class P0_4:
    """Empty / short commits and clearing the buffer."""

    id = "P0-4"
    fields = ("turn.commit_min_ms", "turn.empty_commit_behavior")

    async def run(self, ctx: ProbeContext) -> list[ProbeResult]:
        refs: list[str] = []
        raw: dict[str, Any] = {"empty": [], "snippets": {}, "discard": []}
        empty_codes: list[str] = []
        s = await _open(ctx, f"{self.id}-empty", turn_detection=None)
        refs.append(s.trace_id)
        try:
            for _ in range(ctx.n):
                mark = s.mark()
                ack = await s.up.commit_turn(s.new_turn(), "commit")
                err = await s.wait(_is(EventType.ERROR), 1_000, start=mark)
                code = (err.error.raw_code if err and err.error else None) or (ack.code.value if not ack.ok and ack.code else None)
                empty_codes.append(code or "ignored")
                raw["empty"].append({"ack_ok": ack.ok, "code": code})
        finally:
            await s.close()
        behaviour = "ignored" if all(c == "ignored" for c in empty_codes) else f"error({max(set(empty_codes), key=empty_codes.count)})"
        accepted: dict[int, int] = {}
        for ms, pcm in ctx.assets.snippets().items():
            ok = 0
            for i in range(ctx.n):
                s = await _open(ctx, f"{self.id}-{ms}ms-{i}", turn_detection=None)
                refs.append(s.trace_id)
                try:
                    turn = s.new_turn()
                    await s.send(pcm, turn)
                    mark = s.mark()
                    ack = await s.up.commit_turn(turn, "commit")
                    if not ack.ok:
                        continue
                    committed = await s.wait(_is(EventType.INPUT_COMMITTED), 1_000, start=mark)
                    err = await s.wait(_is(EventType.ERROR), 200, start=mark)
                    ok += committed is not None and err is None
                finally:
                    await s.close()
            accepted[ms] = ok
            raw["snippets"][ms] = {"accepted": ok, "n": ctx.n}
        min_ms: Any = next((ms for ms in sorted(accepted) if accepted[ms] == ctx.n), f">{max(SNIPPET_STEPS_MS)}")
        s = await _open(ctx, f"{self.id}-discard", turn_detection=None)
        refs.append(s.trace_id)
        try:
            turn = s.new_turn()
            await s.send(ctx.assets.snippets()[150], turn)
            ack = await s.up.commit_turn(turn, "discard")
            raw["discard"].append({"ack_ok": ack.ok, "code": ack.code.value if ack.code else None})
        finally:
            await s.close()
        return [
            ProbeResult(self.id, "turn.commit_min_ms", min_ms, refs=refs, raw={"accepted": accepted, **raw}),
            ProbeResult(self.id, "turn.empty_commit_behavior", behaviour, refs=refs, raw={"codes": empty_codes}),
        ]


class P0_8:
    """Concurrent response requests: one slot or not. Collects raw error codes."""

    id = "P0-8"
    fields = ("response.single_slot",)

    async def run(self, ctx: ProbeContext) -> list[ProbeResult]:
        busy = 0
        refs: list[str] = []
        codes: dict[str, int] = {}
        trials: list[dict[str, Any]] = []
        for i in range(ctx.n):
            s = await _open(ctx, f"{self.id}-{i}")
            refs.append(s.trace_id)
            try:
                turn = s.new_turn()
                await s.send(ctx.assets.speech("ask_long"), turn)
                await s.up.commit_turn(turn, "commit")
                mark = s.mark()
                a1 = await s.up.request_response(turn, f"rr-{s.trace_id}-a", None)
                a2 = await s.up.request_response(turn, f"rr-{s.trace_id}-b", None)
                refused = (not a2.ok and a2.code is AckCode.SLOT_BUSY) or not a1.ok
                err = None
                if not refused:
                    err = await s.wait(lambda ev: ev.type is EventType.ERROR and ev.error is not None and ev.error.reason.value == "cancel_race", 1_500, start=mark)
                    await s.settle(500)
                for ev in s.events[mark:]:
                    if ev.type is EventType.ERROR and ev.error and ev.error.raw_code:
                        codes[ev.error.raw_code] = codes.get(ev.error.raw_code, 0) + 1
                two_started = s.count(EventType.RESPONSE_STARTED, start=mark) >= 2
                is_busy = refused or err is not None or not two_started
                busy += is_busy
                trials.append({**s.raw(), "refused_locally": refused, "server_error": err is not None, "two_started": two_started})
            finally:
                await s.close()
        return [ProbeResult(self.id, "response.single_slot", decide(busy, ctx.n), refs=refs, raw={"busy": busy, "n": ctx.n, "error_codes": codes, "trials": trials})]


class P0_9:
    """Text history at open: modality flip; fact injected mid-session: is it used?"""

    id = "P0-9"
    fields = ("history.text_flips_modality", "history.mid_session_effective")

    async def run(self, ctx: ProbeContext) -> list[ProbeResult]:
        if ctx.check_text is None or ctx.inject_fact is None or not ctx.assets.has_speech("ask_fact"):
            why = "needs check_text + inject_fact hooks and a speech asset 'ask_fact'"
            return [ProbeResult(self.id, f, "unknown", refs=[], skipped=why) for f in self.fields]
        refs: list[str] = []
        flipped = 0
        remembered = 0
        trials_a: list[dict[str, Any]] = []
        trials_b: list[dict[str, Any]] = []
        history = [
            HistoryItem(role="user", text="Let me tell you something about my pet."),
            HistoryItem(role="assistant", text="Sure, go ahead."),
            HistoryItem(role="user", text=FACT),
            HistoryItem(role="assistant", text="Got it, I will remember that."),
        ]
        for i in range(ctx.n):
            s = await _open(ctx, f"{self.id}-flip-{i}", history=history)
            refs.append(s.trace_id)
            try:
                modalities: list[str] = []
                for _ in range(3):
                    mark = s.mark()
                    await _ask(s, ctx.assets.speech("chat"))
                    done = await s.wait(_is(EventType.RESPONSE_DONE), 15_000, start=mark)
                    modalities.append(done.reply_modality.value if done and done.reply_modality else "none")
                flip = any(m == ReplyModality.TEXT_ONLY.value for m in modalities)
                flipped += flip
                trials_a.append({**s.raw(), "reply_modalities": modalities})
            finally:
                await s.close()
        for i in range(ctx.n):
            s = await _open(ctx, f"{self.id}-mid-{i}")
            refs.append(s.trace_id)
            try:
                ack = await ctx.inject_fact(s.up, FACT)
                if not ack.ok:
                    trials_b.append({**s.raw(), "note": f"inject refused: {ack.code} {ack.detail}"})
                    continue
                mark = s.mark()
                await _ask(s, ctx.assets.speech("ask_fact"))
                await s.wait(_is(EventType.RESPONSE_DONE), 15_000, start=mark)
                text = "".join(
                    ev.text or "" for ev in s.events[mark:] if ev.type is EventType.TRANSCRIPT_ASSISTANT and ev.is_final
                )
                hit = ctx.check_text("mid_session_fact", text)
                remembered += hit
                trials_b.append({**s.raw(), "transcript": text, "remembered": hit})
            finally:
                await s.close()
        n_b = len([t for t in trials_b if "remembered" in t])
        return [
            ProbeResult(self.id, "history.text_flips_modality", decide(flipped, ctx.n), refs=refs, raw={"flipped": flipped, "n": ctx.n, "trials": trials_a}),
            ProbeResult(self.id, "history.mid_session_effective", decide(remembered, n_b), refs=refs, raw={"remembered": remembered, "n": n_b, "trials": trials_b}),
        ]


class P0_14:
    """After truncate(audio_end_ms), does the model know where it was cut?"""

    id = "P0-14"
    fields = ("truncate.mode",)

    async def run(self, ctx: ProbeContext) -> list[ProbeResult]:
        if ctx.check_text is None or not ctx.assets.has_speech("ask_last"):
            why = "needs check_text hook and speech assets 'ask_long', 'ask_last'"
            return [ProbeResult(self.id, "truncate.mode", "unknown", skipped=why)]
        declared = ctx.caps.get("truncate.mode")
        aware = 0
        refs: list[str] = []
        trials: list[dict[str, Any]] = []
        for i in range(ctx.n):
            s = await _open(ctx, f"{self.id}-{i}")
            refs.append(s.trace_id)
            try:
                await _ask(s, ctx.assets.speech("ask_long"))
                first = await s.wait(_is(EventType.RESPONSE_AUDIO_DELTA), 5_000)
                if first is None or not first.item_id:
                    trials.append({**s.raw(), "note": "no audio delta"})
                    continue
                heard = 0
                deadline = ctx.clock.now_ms() + 5_000
                while heard < 1_500 and ctx.clock.now_ms() < deadline:  # let ~1.5 s of audio arrive
                    heard = sum(
                        ev.duration_ms or 0
                        for ev in s.events
                        if ev.type is EventType.RESPONSE_AUDIO_DELTA and ev.item_id == first.item_id
                    )
                    await s.settle(100)
                await s.up.cancel_response(first.response_id or "")
                ack = await s.up.truncate(first.item_id, 1_000, None)
                if not ack.ok:
                    trials.append({**s.raw(), "note": f"truncate refused: {ack.code} {ack.detail}"})
                    continue
                mark = s.mark()
                await _ask(s, ctx.assets.speech("ask_last"))
                await s.wait(_is(EventType.RESPONSE_DONE), 15_000, start=mark)
                text = "".join(ev.text or "" for ev in s.events[mark:] if ev.type is EventType.TRANSCRIPT_ASSISTANT and ev.is_final)
                hit = ctx.check_text("truncate_aware", text)
                aware += hit
                trials.append({**s.raw(), "transcript": text, "aware": hit})
            finally:
                await s.close()
        n = len([t for t in trials if "aware" in t])
        verdict = decide(aware, n)
        value = declared if verdict == "yes" else "none"
        return [ProbeResult(self.id, "truncate.mode", value, refs=refs, raw={"verdict": verdict, "aware": aware, "n": n, "declared": declared, "trials": trials})]


class _Stub:
    def __init__(self, id_: str, fields: tuple[str, ...], note: str) -> None:
        self.id = id_
        self.fields = fields
        self.note = note

    async def run(self, ctx: ProbeContext) -> list[ProbeResult]:
        return [ProbeResult(self.id, f, "unknown", implemented=False, skipped=self.note) for f in self.fields]


PROBES: dict[str, Probe] = {
    p.id: p
    for p in (
        P0_1(),
        P0_2(),
        P0_3(),
        P0_4(),
        _Stub("P0-5", ("response.overrides", "output.modality_switch", "config.append_channels"), "not implemented: needs content judge"),
        _Stub("P0-6", ("session.resumption", "session.max_secs", "config.update.instructions", "config.update_invalidates_cache"), "not implemented: long-running"),
        _Stub("P0-7", ("usage.cache_reported", "usage.semantics"), "not implemented"),
        P0_8(),
        P0_9(),
        _Stub("P0-10", ("uplink.faster_than_realtime", "uplink.gaps"), "not implemented"),
        _Stub("P0-11", ("turn.input_mute",), "not implemented: vendor-specific levers"),
        _Stub("P0-12", ("tools.placeholder_accepted",), "not implemented"),
        _Stub("P0-13", ("response.say",), "not implemented: needs verbatim judge"),
        P0_14(),
        _Stub("P0-15", ("cascade.pre_speech_hook",), "not implemented: half-cascade only"),
        _Stub("P0-16", (), "not implemented: no capability field yet (spec 04 §7.3)"),
    )
}


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProbeReport:
    adapter: str
    results: list[ProbeResult]
    started_at: str
    finished_at: str | None = None

    def applied(self) -> list[ProbeResult]:
        return [r for r in self.results if r.applicable]

    def not_run(self) -> list[ProbeResult]:
        return [r for r in self.results if not r.applicable]

    def summary(self) -> str:
        lines = [f"probe report for {self.adapter} ({self.started_at})"]
        for r in self.results:
            if r.applicable:
                lines.append(f"  {r.probe:6} {r.field:36} = {r.value!r}  [{r.evidence}, {len(r.refs)} traces]")
            else:
                lines.append(f"  {r.probe:6} {r.field:36}   skipped: {r.skipped}")
        return "\n".join(lines)


async def run_probes(ids: list[str] | None, ctx: ProbeContext) -> ProbeReport:
    """Run the selected probes (all when `ids` is None), in §7.3 order."""
    wanted = list(PROBES) if ids is None else ids
    unknown = [i for i in wanted if i not in PROBES]
    if unknown:
        raise KeyError(f"unknown probe ids {unknown}; known: {list(PROBES)}")
    report = ProbeReport(adapter=ctx.caps.adapter, results=[], started_at=_dt.datetime.now(_dt.UTC).isoformat())
    for pid in wanted:
        try:
            report.results.extend(await PROBES[pid].run(ctx))
        except Exception as e:  # one failing probe must not lose the others
            log.exception("%s failed", pid)
            report.results.extend(
                ProbeResult(pid, f, "unknown", skipped=f"failed: {e}") for f in PROBES[pid].fields
            )
    report.finished_at = _dt.datetime.now(_dt.UTC).isoformat()
    return report


def apply_report(caps: Capabilities, report: ProbeReport, *, probed_at: str | None = None) -> list[str]:
    """Write applicable results into `caps` (evidence: probed, refs = trace
    ids). Flaky verdicts become the conservative value. Returns the fields
    written."""
    written: list[str] = []
    for r in report.applied():
        caps.set_probed(r.field, conservative(r.field, r.value), refs=list(r.refs))
        written.append(r.field)
    if written:
        caps.probed_at = probed_at or _dt.date.today().isoformat()
    return written


async def probe_and_dump(ids: list[str] | None, ctx: ProbeContext, out_path: str | Path) -> ProbeReport:
    report = await run_probes(ids, ctx)
    apply_report(ctx.caps, report)
    ctx.caps.dump(out_path)
    return report
