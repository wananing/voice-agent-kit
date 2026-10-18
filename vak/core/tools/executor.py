"""Tool call executor: `ToolLayer` implements `ToolPort` (spec 03 §4–§8).

One `_Entry` per function call. The lifecycle is an explicit transition
table (`TRANSITIONS`); the interrupt matrix (spec 03 §5.1) is an explicit
branch per `InterruptPhase`. Every callback checks `settled` first, then
the generation. All timers go through the injected clock.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vak.core.bus import Bus
from vak.core.clock import Clock
from vak.core.defaults import DEFAULTS, ToolParams
from vak.core.ids import IdGen
from vak.core.ports import ArbiterPort, ContextPort, CuePort, TracePort
from vak.core.tools.delegate import (
    DELEGATE_CONSTRAINTS,
    Delegate,
    DelegateMessage,
    DelegationRequest,
    SpeculationGate,
)
from vak.core.tools.gates import GATE_REASONS, REASON_PHASE, GateContext, GateVerdict, run_gates
from vak.core.tools.registry import Registry
from vak.core.tools.voice_template import render
from vak.core.types import (
    CallState,
    ClosedBy,
    ContentRole,
    ContentType,
    CueLength,
    GateKind,
    IllegalTransition,
    InterruptPhase,
    InterruptPolicy,
    Kind,
    LatencyClass,
    OnDuplicate,
    Origin,
    OutputContent,
    OutputRequest,
    Presentation,
    Profile,
    SpeakMode,
    ToolCall,
    ToolResult,
    ToolRoute,
    ToolSpec,
    TurnId,
    TurnKind,
)
from vak.upstream.base import Upstream, UpstreamEvent
from vak.upstream.capabilities import Capabilities

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifecycle (spec 03 §4.1) as an explicit table
# ---------------------------------------------------------------------------


class CallEvent(enum.StrEnum):
    START = "start"
    REJECT = "reject"  # phase invisible / gate rejected → closed{result, ok=false}
    DUPLICATE = "duplicate"
    CANCEL = "cancel"
    INTERRUPT = "interrupt"
    PROGRESS = "progress"
    RESULT = "result"
    SUPERSEDE = "supersede"


TRANSITIONS: dict[tuple[CallState, CallEvent], CallState] = {
    (CallState.RECEIVED, CallEvent.START): CallState.RUNNING,
    (CallState.RECEIVED, CallEvent.REJECT): CallState.CLOSED,
    (CallState.RECEIVED, CallEvent.DUPLICATE): CallState.CLOSED,
    (CallState.RECEIVED, CallEvent.CANCEL): CallState.CLOSED,
    (CallState.RECEIVED, CallEvent.SUPERSEDE): CallState.CLOSED,
    (CallState.RUNNING, CallEvent.PROGRESS): CallState.CLOSED,
    (CallState.RUNNING, CallEvent.RESULT): CallState.CLOSED,
    (CallState.RUNNING, CallEvent.CANCEL): CallState.CLOSED,
    (CallState.RUNNING, CallEvent.INTERRUPT): CallState.CLOSED,
    (CallState.RUNNING, CallEvent.SUPERSEDE): CallState.CLOSED,
}

_CLOSE_EVENT: dict[ClosedBy, CallEvent] = {
    ClosedBy.RESULT: CallEvent.RESULT,
    ClosedBy.INTERRUPTED: CallEvent.INTERRUPT,
    ClosedBy.CANCELLED: CallEvent.CANCEL,
    ClosedBy.PROGRESS: CallEvent.PROGRESS,
    ClosedBy.SUPERSEDED: CallEvent.SUPERSEDE,
    ClosedBy.DUPLICATE: CallEvent.DUPLICATE,
}

# Local phrases (spec 03 §4.2, §6.4, §7.3, §8.4). Text, not tunables.
PHRASES: dict[str, str] = {
    "timeout": "这个暂时没弄好，稍后再试试。",
    "task_timeout": "刚才的任务没有完成。",
    REASON_PHASE: "现在还不能做这一步。",
    "intent_mismatch": "我没太听明白，你再说一遍好吗？",
    "utterance_not_explicit": "请再确认一下，要现在执行吗？",
    "backend_state": "现在做不了这个。",
    "delegation": "这个我没查到，要不要换个问法？",
}
NOTE_TOLD = "已经告诉用户了，不要重复"
NOTE_IN_PROGRESS = "任务仍在进行，不要编造结果、不要再次调用"
NOTE_SILENT = "已记录，不需要告诉用户"
DUPLICATE_MESSAGE = "同一请求已在处理或已完成，不要再次调用"
CONFIRM_MESSAGE = "同样的请求刚才已经提过一次，请向用户确认是否再来一次"
PROGRESS_SUMMARY = "已提交，正在处理"

CUE_IDS: dict[CueLength, str] = {CueLength.SHORT: "thinking_short", CueLength.LONG: "thinking_long"}

_SILENT_CLOSE_OK = ("by_omission", "flag")


@dataclass(slots=True)
class _Delegation:
    request: DelegationRequest
    gate: SpeculationGate
    task: asyncio.Task[None] | None = None
    entry: _Entry | None = None
    spoken: list[str] = field(default_factory=list)
    terminal: str | None = None
    stale_dropped: int = 0


@dataclass(slots=True)
class _Entry:
    call: ToolCall
    spec: ToolSpec
    phase: str
    run_task: asyncio.Task[None] | None = None
    exec_task: asyncio.Task[Any] | None = None
    watchdog: asyncio.Task[None] | None = None
    commit_event: asyncio.Event | None = None
    cue_request_id: str | None = None
    speaking: bool = False  # local speech / model reply in flight (row 4 window)
    stale: bool = False  # a barge-in landed while running (rows 2/3): deliver as interrupted
    delegation: _Delegation | None = None
    progress_last_ms: int | None = None
    narration_submitted: bool = False
    speculation_hit: bool | None = None

    @property
    def exec_running(self) -> bool:
        return self.exec_task is not None and not self.exec_task.done()


def _coerce(raw: Any) -> ToolResult:
    if isinstance(raw, ToolResult):
        return raw
    if isinstance(raw, dict):
        known = {k: raw[k] for k in ("ok", "summary", "reason_code", "data", "display", "alternatives", "escalation") if k in raw}
        known.setdefault("ok", True)
        known.setdefault("summary", "")
        return ToolResult(**known)
    return ToolResult(ok=True, summary=str(raw) if raw is not None else "")


class ToolLayer:
    """Spec 03 executor. Speaks via the arbiter, returns via the upstream."""

    def __init__(
        self,
        *,
        registry: Registry,
        profile: Profile | str,
        params: ToolParams,
        clock: Clock,
        bus: Bus,
        arbiter: ArbiterPort,
        upstream: Upstream,
        context: ContextPort,
        trace: TracePort,
        ids: IdGen,
        cues: CuePort | None,
        caps: Capabilities,
        delegate: Delegate | None,
        phase_provider: Callable[[], str],
    ) -> None:
        self.registry = registry
        self.profile = Profile(profile)
        self.params = params
        self.clock = clock
        self.bus = bus
        self.arbiter = arbiter
        self.upstream = upstream
        self.context = context
        self.trace = trace
        self.ids = ids
        self.cues = cues
        self.caps = caps
        self.delegate = delegate
        self.phase_provider = phase_provider

        self._entries: dict[str, _Entry] = {}
        self._completed: set[str] = set()  # tools that returned ok (spec 03 §8.2 `visible_after`)
        self._first_write: dict[str, str] = {}  # turn → call_id (spec 03 §5.2 rule 2)
        self._first_delegation: dict[str, str] = {}  # turn → call_id (spec 03 §6.4)
        self._committed: set[str] = set()
        self._gate_ctx: dict[str, GateContext] = {}
        self._speculations: dict[str, _Delegation] = {}
        self.late_result_dropped = 0
        self._has_a2 = any(s.route is ToolRoute.A2 for s in registry)
        bus.on("turn.committed", self._on_turn_committed)

    # ------------------------------------------------------------------ ToolPort

    def tool_table(self) -> list[Any]:
        return self.registry.tool_table()

    def has_open_calls(self, turn_id: TurnId) -> bool:
        return any(e.call.turn_id == turn_id and not e.call.settled for e in self._entries.values())

    @property
    def calls(self) -> dict[str, ToolCall]:
        return {k: e.call for k, e in self._entries.items()}

    async def on_tool_call(self, event: UpstreamEvent) -> None:
        now = self.clock.now_ms()
        if event.call_id is None or event.name is None or event.turn_id is None:
            log.warning("tool.call without call_id/name/turn_id ignored: %r", event)
            return
        generation = self.arbiter.generation
        turn_id = event.turn_id
        spec = self.registry.get(event.name) if event.name in self.registry else None
        if spec is None:
            # Not in the table at all: close as not available, never hang (spec 03 §8.2).
            spec = ToolSpec(
                name=event.name, description="", params={}, presentation=Presentation.SILENT,
                latency_class=LatencyClass.MS, write=False, phases=[], interrupt_policy=InterruptPolicy.CANCEL,
                on_duplicate=OnDuplicate.ALLOW, timeout_ms=self.params.timeout_ms_default["ms"],
                route=ToolRoute.SILENT, cue=CueLength.NONE,
            )
        route, fallback = self._runtime_route(spec)
        call = ToolCall(
            call_id=event.call_id, name=event.name, args=dict(event.args or {}), turn_id=turn_id,
            generation=generation, route=route, received_at_ms=now, before_commit=event.before_commit,
            route_fallback_reason=fallback,
        )
        entry = _Entry(call=call, spec=spec, phase=self.phase_provider())
        self._entries[call.call_id] = entry
        self.trace.write(turn_id, {"latency": {"fc_received_at_ms": now}})  # spec 06 §2.4
        self.context.record_tool_call(call)
        if event.before_commit and str(turn_id) not in self._committed:
            entry.commit_event = asyncio.Event()
        await self._enqueue_cue(entry)
        entry.watchdog = asyncio.create_task(self._watchdog(entry))
        entry.run_task = asyncio.create_task(self._run(entry))

    async def on_barge_in(self, generation: int) -> None:
        """Spec 03 §5.1 interrupt matrix; the arbiter has already bumped to `generation`."""
        for entry in list(self._entries.values()):
            call = entry.call
            if call.generation >= generation:
                continue
            phase = self._phase_of(entry)
            if phase is InterruptPhase.N_A:
                continue
            call.interrupt_phase = phase
            match phase:
                case InterruptPhase.RECEIVED:  # row 1: not executed, backend untouched
                    await self._close(entry, ClosedBy.CANCELLED)
                    await self._send_after_interrupt(entry, {"cancelled": True, "reason": "interrupt"})
                    self._abort_work(entry)
                case InterruptPhase.RUNNING:  # row 2
                    if entry.spec.write or entry.spec.interrupt_policy is not InterruptPolicy.CANCEL:
                        entry.stale = True  # backend request not withdrawn; `_run` closes on completion
                    else:
                        await self._close(entry, ClosedBy.CANCELLED)
                        await self._send_after_interrupt(entry, {"cancelled": True, "reason": "interrupt"})
                        self._abort_work(entry)
                case InterruptPhase.DONE_NOT_SENT:  # row 3: `_run` branches on tools.silent_close
                    entry.stale = True
                case InterruptPhase.SENT_SPEAKING:  # row 4: stop speaking, supplement the output
                    entry.speaking = False
                    call.closed_by = ClosedBy.INTERRUPTED
                    await self._send_after_interrupt(entry, {"interrupted": True})
                    self._trace_call(entry)
                case InterruptPhase.PROGRESS_CLOSED:  # row 5: closed_by stays progress
                    if not entry.spec.write and entry.spec.interrupt_policy is InterruptPolicy.CANCEL:
                        self._abort_work(entry)
                    self._trace_call(entry)
                case InterruptPhase.NARRATION:  # row 6: counts as delivered, no replay
                    self._trace_call(entry)
                case InterruptPhase.DELEGATING:  # row 7
                    await self._interrupt_delegation(entry)
        for d in list(self._speculations.values()):
            if d.request.generation < generation:
                self._discard_speculation(d)

    async def on_turn_closed(self, turn_id: TurnId) -> None:
        for entry in self._entries.values():
            if entry.call.turn_id == turn_id:
                entry.speaking = False
        d = self._speculations.get(str(turn_id))
        if d is not None and d.entry is None:
            self._discard_speculation(d)  # spec 03 §6.3 item 3: no delegation this turn

    async def on_session_rebuilt(self) -> None:
        """Spec 03 §4.1 invariant 4: in-flight calls close locally as superseded; nothing is sent."""
        for entry in list(self._entries.values()):
            if not entry.call.settled:
                await self._close(entry, ClosedBy.SUPERSEDED)
            self._abort_work(entry)
            if entry.watchdog is not None:
                entry.watchdog.cancel()
        for d in list(self._speculations.values()):
            self._discard_speculation(d)

    # ------------------------------------------------------------------ session hooks

    def on_user_final(
        self, turn_id: TurnId, text: str, *, action: str | None = None, confirmed_by_ui: bool = False
    ) -> None:
        """ASR final for `turn_id`: feeds the gate chain and starts speculation (spec 03 §6.3)."""
        self._gate_ctx[str(turn_id)] = GateContext(
            turn_id=turn_id, generation=self.arbiter.generation, utterance=text, action=action,
            confirmed_by_ui=confirmed_by_ui,
        )
        if self.delegate is None or not self._has_a2 or str(turn_id) in self._speculations:
            return
        req = self._delegation_request(turn_id, self.arbiter.generation, None, None, {}, None, text)
        d = _Delegation(request=req, gate=SpeculationGate(turn_id, req.generation))
        d.task = asyncio.create_task(self._consume(d))
        self._speculations[str(turn_id)] = d

    async def _on_turn_committed(self, payload: dict[str, Any]) -> None:
        tid = payload.get("turn_id")
        if tid is None:
            return
        self._committed.add(str(tid))
        for entry in self._entries.values():
            if str(entry.call.turn_id) == str(tid) and entry.commit_event is not None:
                entry.commit_event.set()

    # ------------------------------------------------------------------ run pipeline

    async def _run(self, entry: _Entry) -> None:
        call, spec = entry.call, entry.spec
        try:
            if entry.commit_event is not None:  # before_commit: hold until the turn is committed
                await entry.commit_event.wait()
                if call.settled:
                    return
            # ② phase visibility (spec 03 §8.2)
            if not self.registry.is_visible(call.name, entry.phase, self._completed):
                await self._reject(entry, GateVerdict(False, None, REASON_PHASE))
                return
            # on_duplicate / first write / first delegation (spec 03 §4.2, §5.2 rule 2, §6.4)
            if await self._duplicate_check(entry):
                return
            # ⑥ gate chain (write only, spec 03 §8.4)
            if spec.write:
                verdict = await run_gates(spec.gates, call.args, self._gate_context(call), tool_name=call.name)
                if call.settled:
                    return
                if not verdict.passed:
                    await self._reject(entry, verdict)
                    return
                call.gate_result = "passed"
            self._transition(entry, CallEvent.START)
            call.started_at_ms = self.clock.now_ms()
            self.trace.write(call.turn_id, {"latency": {"pre_tool_ms": call.started_at_ms}})  # spec 06 §2.4
            if spec.write:
                # spec 03 §5.2 rule 1: re-check generation and backend state right before the backend call
                if self.arbiter.generation != call.generation:
                    await self._cancel(entry, "interrupt")
                    return
                verdict = await run_gates(
                    spec.gates, call.args, self._gate_context(call), tool_name=call.name,
                    only={GateKind.BACKEND_STATE},
                )
                if call.settled:
                    return
                if not verdict.passed or self.arbiter.generation != call.generation:
                    await self._cancel(entry, "interrupt")
                    return
            if call.route is ToolRoute.A2:
                await self._run_delegated(entry)
                return
            if call.route is ToolRoute.A3:
                await self._progress_close(entry)
            result = await self._execute(entry)
            if result is None:
                return
            if call.closed_by is ClosedBy.PROGRESS:
                await self._deliver_narration(entry, result)
                return
            if call.settled:
                self._late_drop(entry, result)
                return
            if entry.stale or self.arbiter.generation != call.generation:
                await self._deliver_interrupted(entry, result)
                return
            await self._deliver(entry, result)
        except asyncio.CancelledError:
            if not call.settled:
                await self._cancel(entry, "interrupt")
        except Exception:  # noqa: BLE001
            log.exception("tool call %s failed in the executor", call.call_id)
            if not call.settled:
                await self._deliver(entry, ToolResult(ok=False, reason_code="internal_error", summary="工具层内部错误"))

    async def _execute(self, entry: _Entry) -> ToolResult | None:
        call, spec = entry.call, entry.spec
        kwargs = dict(call.args)
        if self.registry.wants_progress(call.name):
            kwargs["progress"] = lambda summary: self._on_progress(entry, summary)
        entry.exec_task = asyncio.create_task(self.registry.fn(call.name)(**kwargs))
        try:
            raw = await entry.exec_task
        except asyncio.CancelledError:
            if call.settled:
                return None  # cancelled by whoever closed us
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("tool %s raised: %s", call.name, exc)
            raw = ToolResult(ok=False, reason_code="tool_error", summary=f"{call.name} 执行失败")
        result = _coerce(raw)
        if spec.presentation is not Presentation.SCREEN:
            result.display = None  # spec 03 §2.3: `display` only for screen
        result.alternatives = result.alternatives[: self.params.alternatives_max]
        call.done_at_ms = self.clock.now_ms()
        call.result = result
        self.trace.write(call.turn_id, {"latency": {"tool_done_at_ms": call.done_at_ms}})  # spec 06 §2.4
        return result

    # ------------------------------------------------------------------ rejection / duplicates

    async def _reject(self, entry: _Entry, verdict: GateVerdict) -> None:
        """Phase-invisible or gate-rejected: closed{result, ok=false}, local phrase, no regeneration."""
        call = entry.call
        code = verdict.reason_code or REASON_PHASE
        call.result = ToolResult(
            ok=False, reason_code=code, summary=verdict.message or PHRASES.get(code, PHRASES["backend_state"]),
            alternatives=verdict.alternatives[: self.params.alternatives_max],
        )
        call.gate_result = "blocked" if code in GATE_REASONS else "n_a"
        self._transition(entry, CallEvent.REJECT)
        await self._close(entry, ClosedBy.RESULT, event_done=True)
        await self._send(entry, self._result_body(entry, call.result, spoken=""), reply_required=False)
        await self._withdraw_cue(entry)
        await self._notice(entry, self._failure_phrase(call.result, PHRASES["backend_state"]))

    def _dup_key(self, spec: ToolSpec, args: dict[str, Any]) -> str:
        if spec.write and spec.idempotency_key:
            return json.dumps([args.get(k) for k in spec.idempotency_key], ensure_ascii=False, sort_keys=True, default=str)
        return json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)

    def _in_scope(self, spec: ToolSpec, me: ToolCall, other: _Entry) -> bool:
        if spec.duplicate_scope.value == "turn":
            return other.call.turn_id == me.turn_id
        if spec.duplicate_scope.value == "in_flight":
            return not other.call.settled or other.exec_running
        return True

    async def _duplicate_check(self, entry: _Entry) -> bool:
        call, spec = entry.call, entry.spec
        tid = str(call.turn_id)
        if spec.write:  # spec 03 §5.2 rule 2: one write per user turn
            first = self._first_write.setdefault(tid, call.call_id)
            if first != call.call_id:
                await self._close_duplicate(entry, confirm=False)
                return True
        if call.route is ToolRoute.A2:  # spec 03 §6.4: one delegation per turn
            first = self._first_delegation.setdefault(tid, call.call_id)
            if first != call.call_id:
                await self._close_duplicate(entry, confirm=False)
                return True
        if spec.on_duplicate is OnDuplicate.ALLOW:
            return False
        key = self._dup_key(spec, call.args)
        prior = [
            e for e in self._entries.values()
            if e is not entry and e.call.name == call.name and e.call.received_at_ms <= call.received_at_ms
            and self._dup_key(spec, e.call.args) == key and self._in_scope(spec, call, e)
        ]
        if not prior:
            return False
        if spec.on_duplicate is OnDuplicate.REPLACE:
            for p in prior:
                if not p.call.settled:
                    await self._close(p, ClosedBy.SUPERSEDED)
                    await self._send(p, {"superseded": True}, reply_required=False)
                    self._abort_work(p)
            return False
        await self._close_duplicate(entry, confirm=spec.on_duplicate is OnDuplicate.CONFIRM)
        return True

    async def _close_duplicate(self, entry: _Entry, *, confirm: bool) -> None:
        self._transition(entry, CallEvent.DUPLICATE)
        await self._close(entry, ClosedBy.DUPLICATE, event_done=True)
        await self._withdraw_cue(entry)
        if confirm:  # spec 03 §4.2: the model asks the user → the one duplicate case that regenerates
            await self._send(
                entry, {"duplicate": True, "needs_confirmation": True, "message": CONFIRM_MESSAGE}, reply_required=True
            )
            entry.speaking = True
        else:
            await self._send(entry, {"duplicate": True, "message": DUPLICATE_MESSAGE}, reply_required=False)

    # ------------------------------------------------------------------ delivery per route

    async def _deliver(self, entry: _Entry, result: ToolResult) -> None:
        """Spec 03 §3.1 / §7.1: closed{result}; regeneration only on A0."""
        call, spec = entry.call, entry.spec
        if call.route is ToolRoute.SILENT:
            await self._close(entry, ClosedBy.RESULT)
            await self._send(entry, self._result_body(entry, result, spoken="", note=NOTE_SILENT), reply_required=False)
            return
        if call.route is ToolRoute.A0:
            await self._close(entry, ClosedBy.RESULT)
            await self._send(entry, self._result_body(entry, result, spoken=""), reply_required=True)
            entry.speaking = True
            return
        # A1 (incl. A1-screen): render, withdraw the cue, speak, return immediately (review B-2)
        text = self._spoken_text(spec, result)
        await self._withdraw_cue(entry)
        await self._speak(entry, text, Origin.TOOL_RESULT)
        entry.speaking = True
        await self._close(entry, ClosedBy.RESULT)
        body = self._result_body(entry, result, spoken=text, note=NOTE_TOLD)
        body["delivery"] = "in_progress"
        await self._send(entry, body, reply_required=False)

    async def _deliver_interrupted(self, entry: _Entry, result: ToolResult) -> None:
        """Rows 2 (write finished after barge-in) and 3 (done, not sent)."""
        call, spec = entry.call, entry.spec
        if call.interrupt_phase is InterruptPhase.N_A:
            call.interrupt_phase = InterruptPhase.DONE_NOT_SENT
        await self._withdraw_cue(entry)
        await self._close(entry, ClosedBy.INTERRUPTED)
        body = self._result_body(entry, result, spoken="")
        body["not_spoken"] = True
        await self._send_after_interrupt(entry, body)
        if spec.write and spec.interrupt_policy is InterruptPolicy.FINISH_THEN_NOTIFY:
            # spec 03 §5.2 rule 3: tell the user at the next idle window, not replayed if interrupted
            await self._narrate(entry, Origin.TASK_RESULT, self._spoken_text(spec, result))

    async def _progress_close(self, entry: _Entry) -> None:
        """A3: first progress closes the call (spec 03 §4.2 `progress`)."""
        call, spec = entry.call, entry.spec
        ack = render(spec.progress.ack_template, call.args) if spec.progress else PROGRESS_SUMMARY
        await self._withdraw_cue(entry)
        await self._speak(entry, ack, Origin.TOOL_RESULT)
        await self._close(entry, ClosedBy.PROGRESS)
        await self._send(
            entry,
            {"status": "in_progress", "summary": PROGRESS_SUMMARY, "spoken_to_user": ack, "note": NOTE_IN_PROGRESS},
            reply_required=False,
        )

    async def _deliver_narration(self, entry: _Entry, result: ToolResult) -> None:
        """A3 final result → narration `task_result`, superseding any pending progress."""
        call = entry.call
        call.result = result
        self.context.record_tool_output(call)
        await self._narrate(entry, Origin.TASK_RESULT, self._spoken_text(entry.spec, result))
        if result.ok:
            self._completed.add(call.name)
        self._trace_call(entry)

    async def _on_progress(self, entry: _Entry, summary: str) -> None:
        """Tool-reported progress after the progress close (spec 03 §2.1 `progress.throttle`)."""
        call, spec = entry.call, entry.spec
        if call.closed_by is not ClosedBy.PROGRESS or spec.progress is None or call.started_at_ms is None:
            return
        now = self.clock.now_ms()
        if now - call.started_at_ms < spec.progress.throttle_first_after_s * 1000:
            return
        if entry.progress_last_ms is not None and now - entry.progress_last_ms < spec.progress.throttle_min_interval_s * 1000:
            return
        entry.progress_last_ms = now
        await self._narrate(entry, Origin.TASK_PROGRESS, summary)

    # ------------------------------------------------------------------ delegation (A2)

    async def _run_delegated(self, entry: _Entry) -> None:
        call = entry.call
        tid = str(call.turn_id)
        spec_d = self._speculations.pop(tid, None)
        if spec_d is not None and not spec_d.gate.discarded and spec_d.request.generation == call.generation:
            # speculation hit: release the gate, flush what was held (spec 03 §6.3 item 2)
            entry.speculation_hit = True
            spec_d.entry = entry
            spec_d.request.call_id = call.call_id
            spec_d.gate.release(call.turn_id, call.call_id)
            entry.delegation = spec_d
            await self._withdraw_cue(entry)
            for msg in spec_d.gate.drain():
                await self._dispatch_delegate_message(spec_d, msg)
            if spec_d.terminal is not None:
                await self._finish_delegation(spec_d)
            return
        if spec_d is not None:
            self._discard_speculation(spec_d)
        entry.speculation_hit = False if self.delegate is not None else None
        if self.delegate is None:
            # no background: fall back to A0 (spec 03 §3.3)
            call.route = ToolRoute.A0
            call.route_fallback_reason = "no_delegate"
            result = await self._execute(entry)
            if result is None:
                return
            if call.settled:
                self._late_drop(entry, result)
            elif entry.stale or self.arbiter.generation != call.generation:
                await self._deliver_interrupted(entry, result)
            else:
                await self._deliver(entry, result)
            return
        result = await self._execute(entry)
        if result is None or call.settled:
            return
        if entry.stale or self.arbiter.generation != call.generation:
            await self._deliver_interrupted(entry, result)
            return
        req = self._delegation_request(
            call.turn_id, call.generation, call.call_id, call.name, call.args, result,
            self._gate_ctx.get(tid, GateContext()).utterance,
        )
        d = _Delegation(request=req, gate=SpeculationGate.open(call.turn_id, call.generation, call.call_id), entry=entry)
        entry.delegation = d
        d.task = asyncio.create_task(self._consume(d))

    def _delegation_request(
        self, turn_id: TurnId, generation: int, call_id: str | None, tool_name: str | None,
        args: dict[str, Any], tool_result: ToolResult | None, transcript: str,
    ) -> DelegationRequest:
        items, _fmt, _scope, _n = self.context.project_history(self.caps)
        k = self.params.recent_turns_k
        timeout = self.registry.get(tool_name).timeout_ms if tool_name and tool_name in self.registry else (
            self.params.timeout_ms_default["seconds"]
        )
        return DelegationRequest(
            delegation_id=self.ids.new("dlg"), call_id=call_id, turn_id=turn_id, generation=generation,
            intent_hint=tool_name, last_utterance_gist=transcript, transcript=transcript or None,
            state_block=self.context.state_block(), recent_turns=items[-2 * k :],
            allowed_tools=self.registry.readonly_names(), constraints=list(DELEGATE_CONSTRAINTS),
            deadline_ms=self.clock.now_ms() + timeout, tool_name=tool_name, args=dict(args), tool_result=tool_result,
        )

    async def _consume(self, d: _Delegation) -> None:
        assert self.delegate is not None
        try:
            async for msg in self.delegate.run(d.request):
                if self.arbiter.generation != d.request.generation:
                    d.stale_dropped += 1  # spec 03 §6.2: speak channel is generation-stamped, stale → drop
                    break
                if d.gate.discarded:
                    break
                if d.gate.hold(msg):
                    continue
                await self._dispatch_delegate_message(d, msg)
                if msg.channel == "terminal":
                    break
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("delegation %s failed: %s", d.request.delegation_id, exc)
            d.terminal = "failed"
        if d.gate.discarded or self.arbiter.generation != d.request.generation:
            await self.delegate.cancel(d.request.delegation_id)
            if d.entry is not None and not d.entry.call.settled:
                d.entry.call.interrupt_phase = InterruptPhase.DELEGATING
                await self._interrupt_delegation(d.entry)
            return
        if d.terminal is None:
            d.terminal = "done" if d.spoken else "empty"
        if d.entry is not None:
            await self._finish_delegation(d)

    async def _dispatch_delegate_message(self, d: _Delegation, msg: DelegateMessage) -> None:
        if msg.channel == "speak" and msg.text:
            d.spoken.append(msg.text)
            if d.entry is not None:
                await self._speak(d.entry, msg.text, Origin.DELEGATED_RESULT)
                d.entry.speaking = True
        elif msg.channel == "context":
            if msg.tool_call is not None:
                self.context.record_tool_call(msg.tool_call)
                self.context.record_tool_output(msg.tool_call)
            elif msg.text:
                self.context.record_system_event(d.request.turn_id, msg.text)
        elif msg.channel == "terminal":
            d.terminal = msg.terminal

    async def _finish_delegation(self, d: _Delegation) -> None:
        entry = d.entry
        assert entry is not None
        call = entry.call
        if call.settled:
            return
        spoken = self.context.presented_text(call.turn_id) or "".join(d.spoken)
        if d.terminal == "done" and spoken:
            summary = call.result.summary if call.result is not None and call.result.summary else spoken[:80]
            result = call.result or ToolResult(ok=True, summary=summary)
            result.summary = summary
            call.result = result
            await self._close(entry, ClosedBy.RESULT)
            await self._send(entry, self._result_body(entry, result, spoken=spoken, note=NOTE_TOLD), reply_required=False)
            return
        reason = d.terminal if d.terminal in ("failed", "timeout", "empty") else "empty"
        # spec 03 §6.4: failure / timeout / empty → one local sentence, no regeneration
        await self._cancel(entry, reason, phrase=PHRASES["delegation"])

    async def _interrupt_delegation(self, entry: _Entry) -> None:
        """Row 7: cancel the background task; output only what was played."""
        d = entry.delegation
        call = entry.call
        if d is not None:
            d.gate.discard()
            if d.task is not None and d.task is not asyncio.current_task():
                d.task.cancel()
            if self.delegate is not None:
                await self.delegate.cancel(d.request.delegation_id)
        spoken = self.context.presented_text(call.turn_id) or ("".join(d.spoken) if d else "")
        if spoken:
            await self._close(entry, ClosedBy.INTERRUPTED)
            body = {"interrupted": True, "status": "interrupted", "spoken_to_user": spoken}
            await self._send_after_interrupt(entry, body)
        else:
            await self._close(entry, ClosedBy.CANCELLED)
            await self._send_after_interrupt(entry, {"cancelled": True, "reason": "interrupt"})

    def _discard_speculation(self, d: _Delegation) -> None:
        d.gate.discard()
        self._speculations.pop(str(d.request.turn_id), None)
        if d.task is not None and not d.task.done():
            d.task.cancel()
        if self.delegate is not None:
            asyncio.get_running_loop().create_task(self.delegate.cancel(d.request.delegation_id))
        log.debug("speculation for %s discarded (miss)", d.request.turn_id)

    # ------------------------------------------------------------------ closing

    def _transition(self, entry: _Entry, event: CallEvent) -> None:
        call = entry.call
        try:
            call.state = TRANSITIONS[(call.state, event)]
        except KeyError:
            raise IllegalTransition(f"{call.call_id}: {call.state.value} --{event.value}--> ?") from None

    async def _close(self, entry: _Entry, closed_by: ClosedBy, *, event_done: bool = False) -> bool:
        """Enter the terminal state. Returns False if already settled (late)."""
        call = entry.call
        if call.settled:
            return False
        if not event_done:
            self._transition(entry, _CLOSE_EVENT[closed_by])
        call.settled = True
        call.closed_by = closed_by
        call.closed_at_ms = self.clock.now_ms()
        if call.reply_required is None:
            call.reply_required = False
        if entry.commit_event is not None:
            entry.commit_event.set()
        if entry.watchdog is not None and closed_by is not ClosedBy.PROGRESS:
            entry.watchdog.cancel()
        self.context.record_tool_output(call)
        if closed_by is ClosedBy.RESULT and call.result is not None and call.result.ok:
            self._completed.add(call.name)
        self._trace_call(entry)
        # spec 02 §2.9 `turn.awaiting_tool`: the turn machine learns the tool phase is over.
        # Emitted on the next tick so `reply_required` / `output_sent` are final by then.
        self.bus.emit_soon("tool.call_closed", turn_id=call.turn_id, call_id=call.call_id, closed_by=closed_by.value)
        return True

    async def _cancel(self, entry: _Entry, reason: str, *, phrase: str | None = None) -> None:
        """closed{cancelled}: `{"cancelled": true, "reason": ...}`, local phrase when given."""
        if not await self._close(entry, ClosedBy.CANCELLED):
            return
        self._abort_work(entry)
        await self._withdraw_cue(entry)
        payload = {"cancelled": True, "reason": reason}
        if reason == "interrupt":
            await self._send_after_interrupt(entry, payload)
        else:
            await self._send(entry, payload, reply_required=False)
        if phrase:
            await self._notice(entry, phrase)

    def _abort_work(self, entry: _Entry) -> None:
        if entry.exec_running:
            entry.exec_task.cancel()  # type: ignore[union-attr]
        if entry.delegation is not None:
            d = entry.delegation
            d.gate.discard()
            if d.task is not None and d.task is not asyncio.current_task():
                d.task.cancel()
            if self.delegate is not None:
                asyncio.get_running_loop().create_task(self.delegate.cancel(d.request.delegation_id))
        if entry.commit_event is not None:
            entry.commit_event.set()

    def _late_drop(self, entry: _Entry, result: ToolResult) -> None:
        """Spec 03 §4.1 invariant 2/3: a result after `settled` is refused and counted."""
        call, spec = entry.call, entry.spec
        call.late_result_dropped += 1
        self.late_result_dropped += 1
        log.info("late result for %s dropped (closed_by=%s)", call.call_id, call.closed_by)
        if spec.write and result.ok:
            # side effect happened: tell the user at the next idle window (rule 3), still no return
            asyncio.get_running_loop().create_task(
                self._narrate(entry, Origin.TASK_RESULT, self._spoken_text(spec, result))
            )
        self._trace_call(entry)

    async def _watchdog(self, entry: _Entry) -> None:
        """Spec 03 §4.1 invariant 1: every call closes within timeout_ms (+ margin)."""
        call, spec = entry.call, entry.spec
        try:
            await self.clock.sleep(spec.timeout_ms)
        except asyncio.CancelledError:
            return
        if not call.settled:
            await self._cancel(entry, "timeout", phrase=PHRASES["timeout"])
            return
        if call.closed_by is ClosedBy.PROGRESS and entry.exec_running:
            self._abort_work(entry)
            await self._narrate(entry, Origin.TASK_RESULT, PHRASES["task_timeout"])
            self._trace_call(entry)

    # ------------------------------------------------------------------ output helpers

    def _runtime_route(self, spec: ToolSpec) -> tuple[ToolRoute, str | None]:
        """Spec 03 §3.3 item 1: no silent close upstream → A1/A2 fall back to A0."""
        route = spec.route or ToolRoute.A0
        if route in (ToolRoute.A1, ToolRoute.A2) and self.caps.get("tools.silent_close") == "none":
            return ToolRoute.A0, "tools.silent_close=none"
        return route, None

    def _gate_context(self, call: ToolCall) -> GateContext:
        ctx = self._gate_ctx.get(str(call.turn_id))
        if ctx is None:
            ctx = GateContext(turn_id=call.turn_id, generation=call.generation)
        return ctx

    def _spoken_text(self, spec: ToolSpec, result: ToolResult) -> str:
        if not result.ok:
            return self._failure_phrase(result, PHRASES["backend_state"])
        if spec.voice_template:
            return render(spec.voice_template, {**result.data, "summary": result.summary})
        return result.summary

    def _failure_phrase(self, result: ToolResult, default: str) -> str:
        """Spec 03 §7.4: 不能 + ≤ 2 options + escalation exit."""
        parts = [result.summary or default]
        alts = [a.get("label", "") for a in result.alternatives[: self.params.alternatives_max] if a.get("label")]
        if len(alts) == 2:
            parts.append(f"可以{alts[0]}，或者{alts[1]}，你想要哪个？")
        elif len(alts) == 1:
            parts.append(f"可以{alts[0]}，要吗？")
        if result.escalation and result.escalation.get("label") and self.profile is Profile.B:
            parts.append(f"要不要{result.escalation['label']}？")
        text = "".join(p if p.endswith(("。", "？", "！")) else p + "。" for p in parts if p)
        return text

    def _result_body(self, entry: _Entry, result: ToolResult, *, spoken: str, note: str | None = None) -> dict[str, Any]:
        """Spec 03 §7.1 output body."""
        body: dict[str, Any] = {
            "status": "ok" if result.ok else "failed",
            "summary": result.summary,
            "spoken_to_user": spoken,
        }
        if entry.spec.presentation is Presentation.SCREEN and result.display:
            body["shown_to_user"] = result.display.get("card_id") or result.display.get("summary") or "card"
        if not result.ok:
            body["reason_code"] = result.reason_code
            body["alternatives"] = result.alternatives[: self.params.alternatives_max]
            if result.escalation:
                body["escalation"] = result.escalation
        if note:
            body["note"] = note
        return body

    def _fit(self, body: dict[str, Any]) -> dict[str, Any]:
        """Spec 03 §7.1: output ≤ output_max_chars; trim spoken_to_user then summary."""
        limit = self.params.output_max_chars
        for key in ("spoken_to_user", "summary"):
            while len(json.dumps(body, ensure_ascii=False)) > limit and body.get(key):
                body[key] = body[key][: max(0, len(body[key]) - 20)]
        return body

    async def _send(self, entry: _Entry, payload: dict[str, Any], *, reply_required: bool) -> bool:
        call = entry.call
        call.reply_required = reply_required
        ack = await self.upstream.send_tool_result(call.call_id, self._fit(dict(payload)), reply_required)
        if ack.ok:
            call.output_sent = True
            self.trace.write(call.turn_id, {"latency": {"output_sent_at_ms": self.clock.now_ms()}})  # spec 06 §2.4
        else:
            log.warning("send_tool_result(%s) refused: %s %s", call.call_id, ack.code, ack.detail)
        self._trace_call(entry)
        return ack.ok

    async def _send_after_interrupt(self, entry: _Entry, payload: dict[str, Any]) -> bool:
        """Spec 03 §5.1 row 3 branch on `tools.silent_close`: return only if the upstream can
        take an output without talking; otherwise the call is closed locally only."""
        if self.caps.get("tools.silent_close") in _SILENT_CLOSE_OK:
            return await self._send(entry, payload, reply_required=False)
        entry.call.reply_required = False
        log.info("%s: not returned after interrupt (tools.silent_close=none)", entry.call.call_id)
        self._trace_call(entry)
        return False

    async def _speak(self, entry: _Entry, text: str, origin: Origin) -> None:
        call = entry.call
        if not text:
            return
        await self.arbiter.submit(
            OutputRequest(
                request_id=self.ids.new("req"), origin=origin, kind=Kind.SPEECH,
                content=OutputContent(ContentType.TEXT_TTS, text=text), generation=call.generation,
                turn_id=call.turn_id, content_role=ContentRole.ANSWER,
            )
        )

    async def _notice(self, entry: _Entry, text: str) -> None:
        call = entry.call
        await self.arbiter.submit(
            OutputRequest(
                request_id=self.ids.new("req"), origin=Origin.SYSTEM_NOTICE, kind=Kind.SPEECH,
                content=OutputContent(ContentType.TEXT_TTS, text=text), generation=call.generation,
                turn_id=call.turn_id, content_role=ContentRole.ERROR,
            )
        )

    async def _narrate(self, entry: _Entry, origin: Origin, text: str) -> None:
        """A3 progress / result and write notifications go in as narration (spec 03 §4.1, §5.2)."""
        call, spec = entry.call, entry.spec
        if not text:
            return
        is_progress = origin is Origin.TASK_PROGRESS
        replace = spec.progress.replace_older if spec.progress else True
        await self.arbiter.submit(
            OutputRequest(
                request_id=self.ids.new("req"), origin=origin, kind=Kind.NARRATION,
                content=OutputContent(ContentType.TEXT_TTS, text=text), generation=None, turn_id=call.turn_id,
                content_role=ContentRole.PROGRESS if is_progress else ContentRole.ANSWER,
                on_interrupt="drop",  # spec 03 §5.1 row 6: interrupted narration counts as delivered
                max_retries=DEFAULTS.arbiter.max_retries_progress if is_progress else DEFAULTS.arbiter.max_retries_result,
                dedupe_key=f"{call.call_id}:{'progress' if is_progress else 'result'}",
                supersedes=f"{call.call_id}:progress" if replace else None,
            )
        )
        if not is_progress:
            entry.narration_submitted = True

    async def _enqueue_cue(self, entry: _Entry) -> None:
        """Spec 03 §2.1 `cue`: enqueue on function call; withdrawn if the result is ready first."""
        spec, call = entry.spec, entry.call
        if self.cues is None or spec.cue in (None, CueLength.NONE) or call.route is ToolRoute.SILENT:
            return
        got = self.cues.get(CUE_IDS[spec.cue])
        if got is None:
            return
        pcm, rate, duration = got
        entry.cue_request_id = self.ids.new("req")
        await self.arbiter.submit(
            OutputRequest(
                request_id=entry.cue_request_id, origin=Origin.CUE, kind=Kind.CUE,
                content=OutputContent(ContentType.PRERECORDED, audio=pcm, sample_rate=rate, duration_ms=duration),
                generation=call.generation, turn_id=call.turn_id, content_role=ContentRole.FILLER,
            )
        )

    async def _withdraw_cue(self, entry: _Entry) -> None:
        """Directed flush of this turn's ungranted cue (spec 02 §3.5; the arbiter keeps a cue already playing)."""
        if entry.cue_request_id is None:
            return
        entry.cue_request_id = None
        await self.arbiter.flush_origin("cue", entry.call.turn_id)

    # ------------------------------------------------------------------ trace (spec 06 §3.3, §3.5)

    def _phase_of(self, entry: _Entry) -> InterruptPhase:
        call = entry.call
        if call.state is CallState.RECEIVED:
            return InterruptPhase.RECEIVED
        if call.state is CallState.RUNNING:
            if entry.delegation is not None:
                return InterruptPhase.DELEGATING
            return InterruptPhase.DONE_NOT_SENT if call.done_at_ms is not None else InterruptPhase.RUNNING
        if call.closed_by is ClosedBy.PROGRESS:
            return InterruptPhase.PROGRESS_CLOSED if entry.exec_running else InterruptPhase.NARRATION
        if call.closed_by is ClosedBy.RESULT and entry.speaking:
            return InterruptPhase.SENT_SPEAKING
        if call.closed_by is ClosedBy.DUPLICATE and entry.speaking:
            return InterruptPhase.SENT_SPEAKING  # duplicate(confirm): the model was asking
        return InterruptPhase.N_A

    def _gate_result_field(self, entry: _Entry) -> str:
        call = entry.call
        code = call.result.reason_code if call.result is not None else None
        if code in GATE_REASONS or code == REASON_PHASE:
            return str(code)
        return "pass" if call.gate_result == "passed" else "n_a"

    def _call_record(self, entry: _Entry) -> dict[str, Any]:
        call, spec = entry.call, entry.spec
        rec: dict[str, Any] = {
            "call_id": call.call_id,
            "name": call.name,
            "args": dict(call.args),
            "presentation": spec.presentation.value,
            "is_write": spec.write,
            "latency_class": spec.latency_class.value,
            "tool_route": call.route.value,
            "generation": call.generation,
            "gate_result": self._gate_result_field(entry),
            "late_result_dropped": call.late_result_dropped,
            "reply_required": bool(call.reply_required),
            "closed_by": call.closed_by.value if call.closed_by else None,
            "settled_at_ms": call.closed_at_ms,
            "started_at_ms": call.started_at_ms,
            "done_at_ms": call.done_at_ms,
        }
        if call.started_at_ms is not None and call.done_at_ms is not None:
            rec["tool_exec_ms"] = call.done_at_ms - call.started_at_ms
        if call.route_fallback_reason:
            rec["route_fallback_reason"] = call.route_fallback_reason
        if entry.phase:
            rec["phase"] = entry.phase
        if entry.speculation_hit is not None:
            rec["speculation_hit"] = entry.speculation_hit
        if call.interrupt_phase is not InterruptPhase.N_A:
            rec["interrupt_phase"] = call.interrupt_phase.value
        return rec

    def _trace_call(self, entry: _Entry) -> None:
        """Append the full per-call record (spec 06 §3.5) and the turn-level tool fields (§3.3).

        Appended at close and again on later mutations (row 4 relabel, late drops); the
        collector keeps the latest record per `call_id`.
        """
        call, spec = entry.call, entry.spec
        if not call.settled:
            return
        self.trace.append(call.turn_id, "tool_calls", self._call_record(entry))
        if spec.write:
            turn_kind = TurnKind.TOOL_WRITE
        elif spec.presentation is Presentation.MODEL:
            turn_kind = TurnKind.TOOL_COMPOSE
        else:
            turn_kind = TurnKind.TOOL_VERBATIM
        self.trace.write(
            call.turn_id,
            {
                "turn_kind": turn_kind.value,
                "speak_mode": self._speak_mode(entry).value,
                "write_gate": call.gate_result if spec.write else "n_a",
                "action": call.name,
                "decision_layer": "gate" if call.gate_result == "blocked" else "model",
            },
        )

    def _speak_mode(self, entry: _Entry) -> SpeakMode:
        call, spec = entry.call, entry.spec
        if call.closed_by in (ClosedBy.CANCELLED, ClosedBy.DUPLICATE, ClosedBy.SUPERSEDED) and not entry.speaking:
            return SpeakMode.NONE
        if call.route is ToolRoute.SILENT:
            return SpeakMode.NONE
        if call.route is ToolRoute.A0:
            return SpeakMode.MODEL
        if call.route is ToolRoute.A2:
            return SpeakMode.DELEGATED
        return SpeakMode.SCREEN if spec.presentation is Presentation.SCREEN else SpeakMode.VERBATIM


__all__ = ["PHRASES", "TRANSITIONS", "CallEvent", "ToolLayer"]
