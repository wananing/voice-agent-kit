"""SessionRebuilder: the single place that builds a new upstream session
(spec 05 §4, §7.2). Disconnects, upstream errors, overflow, proactive
swaps and modality anomalies all go through `rebuild(reason)`.

Inside: idle-gap detection (spec 05 §4.3), per-turn budget with
exponential backoff (§7.2), handoff audio buffer and replay (§4.7), the
"delivered" registry (§4.6), last-turn user replay rule (§4.5), trace
fields of spec 06 §3.6 / §3.9 for the current turn.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vak.core.bus import Bus
from vak.core.clock import Clock
from vak.core.context.store import ContextStore
from vak.core.context.summary import Summarizer
from vak.core.defaults import ContextParams
from vak.core.ports import ToolPort, TracePort
from vak.core.types import ErrorClass, HistoryItem, RebuildReason, TurnId
from vak.upstream.base import AudioMeta, SessionInit, ToolDef, Upstream, UpstreamError
from vak.upstream.capabilities import Capabilities

log = logging.getLogger(__name__)

# spec 05 §4.2: immediate (no old session to wait for) vs wait-for-idle-gap
IMMEDIATE_REASONS = frozenset(
    {RebuildReason.DISCONNECT, RebuildReason.UPSTREAM_ERROR, RebuildReason.CONTEXT_OVERFLOW, RebuildReason.APP_RESUME}
)
# spec 05 §7.2: proactive swaps do not consume the error budget
PROACTIVE_REASONS = frozenset(
    {RebuildReason.STATE_CHANGE, RebuildReason.TURN_LIMIT, RebuildReason.SCHEDULED, RebuildReason.MODALITY}
)
_UPLINK_SAMPLE_RATE = 16000


def backoff_ms(retry_no: int, params: ContextParams) -> int:
    """Exponential backoff for the n-th retry (1-based), capped (spec 05 §7.2)."""
    return min(params.rebuild_backoff_first_ms * (2 ** (retry_no - 1)), params.rebuild_backoff_cap_ms)


@dataclass(slots=True)
class DeliveredRegistry:
    """Tool results the current upstream session already has (spec 05 §4.6)."""

    seeded: set[str] = field(default_factory=set)
    live: set[str] = field(default_factory=set)

    def is_delivered(self, call_id: str) -> bool:
        return call_id in self.seeded or call_id in self.live

    def mark_delivered(self, call_id: str) -> None:
        self.live.add(call_id)


@dataclass(slots=True)
class RebuildResult:
    ok: bool
    reason: RebuildReason
    session_seq: int
    delivered_registry: DeliveredRegistry
    handoff_start_ms: int
    handoff_done_ms: int
    error_class: ErrorClass | None = None
    replayed_user_text: str | None = None
    replay_skipped: str | None = None  # "write_executed" | "write_unknown" | "partial_reply"
    replayed_audio_frames: int = 0


class SessionRebuilder:
    def __init__(
        self,
        *,
        context: ContextStore,
        upstream_factory: Callable[[], Upstream] | None,
        upstream: Upstream | None,
        caps: Capabilities,
        clock: Clock,
        bus: Bus,
        params: ContextParams,
        trace: TracePort,
        tools: ToolPort,
        registry_tools: Callable[[], list[ToolDef]],
        summarizer: Summarizer | None = None,
    ) -> None:
        self.context = context
        self._factory = upstream_factory
        self.upstream = upstream
        self.caps = caps
        self.clock = clock
        self.bus = bus
        self.params = params
        self.trace = trace
        self.tools = tools
        self._registry_tools = registry_tools
        self.summarizer = summarizer
        self.delivered_registry = DeliveredRegistry()
        # budget (spec 05 §7.2)
        self.attempts_in_turn = 0
        self.fatal = False
        self._stable_since_ms: int | None = None
        # turn / idle tracking (spec 05 §4.3)
        self._open_turn_id: TurnId | None = None
        self._current_turn_id: TurnId | None = None
        self._user_speaking = False
        self._last_activity_ms = clock.now_ms()
        self._turn_closed = asyncio.Event()
        self._waiting_task: asyncio.Task[Any] | None = None  # the task inside _wait_for_idle
        self._tick_ms = min(100, params.idle_gap_ms)
        # handoff buffer (spec 05 §4.7)
        self._handoff_active = False
        self._uplink: list[bytes] = []
        self._uplink_ms = 0.0
        self._uplink_full = False
        # single flight
        self._lock = asyncio.Lock()
        self._urgent: RebuildReason | None = None
        self.last_result: RebuildResult | None = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self.bus.on("turn.start", self._on_turn_start)
        self.bus.on("turn.closed", self._on_turn_closed)
        self.bus.on("turn.audio_drained", self._on_activity)

    def stop(self) -> None:
        self.bus.off("turn.start", self._on_turn_start)
        self.bus.off("turn.closed", self._on_turn_closed)
        self.bus.off("turn.audio_drained", self._on_activity)

    async def _on_turn_start(self, payload: dict[str, Any]) -> None:
        tid = _turn_id(payload)
        self._open_turn_id = tid
        self._current_turn_id = tid or self._current_turn_id
        self._last_activity_ms = self.clock.now_ms()
        # budget is per turn; a new user-initiated turn may start over (spec 05 §7.2)
        self.attempts_in_turn = 0
        self.fatal = False

    async def _on_turn_closed(self, payload: dict[str, Any]) -> None:
        tid = _turn_id(payload)
        if tid is not None:
            self._current_turn_id = tid
        self._open_turn_id = None
        self._last_activity_ms = self.clock.now_ms()
        self._turn_closed.set()
        self._maybe_summarize()

    async def _on_activity(self, _payload: dict[str, Any]) -> None:
        self._last_activity_ms = self.clock.now_ms()

    def set_user_speaking(self, speaking: bool) -> None:
        self._user_speaking = speaking
        if speaking:
            self._last_activity_ms = self.clock.now_ms()

    def note_downlink_audio(self) -> None:
        """Call on every downlink audio frame; feeds the 1 s quiet rule."""
        self._last_activity_ms = self.clock.now_ms()

    # --------------------------------------------------------- error routing

    async def on_upstream_error(self, error: UpstreamError) -> RebuildResult | None:
        """Spec 05 §7.1: route by `error.class` only; never parse raw codes."""
        if error.error_class is ErrorClass.FATAL:
            return await self._give_up(RebuildReason.UPSTREAM_ERROR, ErrorClass.FATAL)
        if error.error_class is ErrorClass.TRANSIENT:
            return await self.rebuild(RebuildReason.UPSTREAM_ERROR)
        if error.error_class is ErrorClass.CONTEXT_OVERFLOW:
            return await self.rebuild(RebuildReason.CONTEXT_OVERFLOW)
        log.info("benign upstream error %s; session continues", error.reason.value)
        return None

    # -------------------------------------------------------------- rebuild

    async def rebuild(self, reason: RebuildReason) -> RebuildResult:
        requested_seq = self.context.session_seq
        if reason in IMMEDIATE_REASONS:
            self._urgent = reason
            if self._waiting_task is not None:
                self._waiting_task.cancel()  # a waiting proactive swap adopts the urgent reason
            self._begin_handoff()  # old session is gone: buffer uplink from now on
        async with self._lock:
            if self.context.session_seq > requested_seq and self.last_result is not None:
                # another caller rebuilt while we waited for the lock
                return self.last_result
            if reason not in IMMEDIATE_REASONS:
                await self._wait_for_idle()
                if self._urgent is not None:
                    reason = self._urgent  # the urgent reason takes over the pending swap
            self._urgent = None
            result = await self._execute(reason)
            self.last_result = result
            return result

    async def _execute(self, reason: RebuildReason) -> RebuildResult:
        proactive = reason in PROACTIVE_REASONS
        self._maybe_reset_budget()
        tries = 0
        while True:
            if self.fatal or self.attempts_in_turn >= self.params.rebuild_budget_per_turn:
                return await self._give_up(reason, ErrorClass.FATAL)
            if tries > 0:
                await self.clock.sleep(backoff_ms(tries, self.params))
            tries += 1
            if not proactive:
                self.attempts_in_turn += 1
            result = await self._attempt(reason)
            if result.ok:
                self._stable_since_ms = self.clock.now_ms()
                return result
            if result.error_class is ErrorClass.FATAL:
                return await self._give_up(reason, ErrorClass.FATAL)
            if proactive:
                self.attempts_in_turn += 1  # a failed swap counts by its error class (spec 05 §7.2)
            self._write_trace({"error_class": (result.error_class or ErrorClass.TRANSIENT).value})

    async def _attempt(self, reason: RebuildReason) -> RebuildResult:
        handoff_start = self.clock.now_ms()
        self._begin_handoff()
        ctx = self.context
        if reason is RebuildReason.CONTEXT_OVERFLOW:
            ctx.shrink_history()  # spec 05 §7.1: never replay the same history
        ctx.state.apply_pending()  # spec 05 §4.3 rule 5: carry queued updates

        replay_text, replay_turn, skipped = self._replay_decision()
        exclude = {str(replay_turn)} if replay_text and replay_turn else None
        items, fmt, scope, n_items = ctx.project_history(self.caps, exclude_turn_ids=exclude)
        history = list(items)
        if replay_text:
            # first-turn input goes after the history (spec 05 §4.7 order)
            history.append(HistoryItem("user", replay_text))

        old = self.upstream
        if old is not None:
            try:
                await old.close(f"rebuild:{reason.value}")
            except Exception:  # noqa: BLE001
                log.exception("closing old upstream failed; continuing")
        new = self._factory() if self._factory is not None else old
        if new is None:
            raise RuntimeError("SessionRebuilder needs an upstream or an upstream_factory")
        new_seq = ctx.session_seq + 1
        init = SessionInit(
            user_session_id=ctx.user_session_id,
            session_seq=new_seq,
            instructions=ctx.render_instructions(),
            state_block_version=ctx.state.version,
            tools=self._registry_tools(),
            history=history,
            history_format=fmt,
        )
        ack = await new.open(init)
        if not ack.ok:
            return RebuildResult(
                ok=False, reason=reason, session_seq=ctx.session_seq, delivered_registry=self.delivered_registry,
                handoff_start_ms=handoff_start, handoff_done_ms=self.clock.now_ms(),
                error_class=ack.error_class or ErrorClass.TRANSIENT,
            )

        self.upstream = new
        ctx.begin_upstream_session(new_seq)
        ctx.mark_seeded([it.local_entry_id for it in items if it.local_entry_id])
        self.delivered_registry = DeliveredRegistry(seeded={it.tool_call_id for it in items if it.tool_call_id})
        await self.tools.on_session_rebuilt()  # old in-flight calls are closed locally (spec 05 §4.6)

        if replay_text and replay_turn is not None and self.caps.get("response.client_initiated") == "yes":
            await new.request_response(replay_turn, f"rebuild-{new_seq}")
        frames = self._end_handoff()
        for i, frame in enumerate(frames):
            meta = AudioMeta(
                turn_id=self._open_turn_id, seq=i, sample_count=len(frame) // 2,
                captured_at_ms=self.clock.now_ms(), source="handoff_replay", sample_rate=_UPLINK_SAMPLE_RATE,
            )
            await new.send_audio(frame, meta)
        handoff_done = self.clock.now_ms()

        self._write_trace(
            {
                # spec 06 §3.6 session fields; absolute ms, the collector subtracts T0
                "session_seq": new_seq,
                "rebuild_reason": reason.value,
                "history_format": fmt.value,
                "history_scope": scope.value,
                "history_items": n_items,
                "handoff_start_at_ms": handoff_start,
                "handoff_done_at_ms": handoff_done,
                "restart_attempts_in_turn": self.attempts_in_turn,  # spec 06 §3.9
            }
        )
        await self.bus.emit("session.rebuilt", session_seq=new_seq, reason=reason.value)
        return RebuildResult(
            ok=True, reason=reason, session_seq=new_seq, delivered_registry=self.delivered_registry,
            handoff_start_ms=handoff_start, handoff_done_ms=handoff_done,
            replayed_user_text=replay_text, replay_skipped=skipped, replayed_audio_frames=len(frames),
        )

    async def _give_up(self, reason: RebuildReason, error_class: ErrorClass) -> RebuildResult:
        """Spec 05 §7.2: budget exhausted (or fatal) → stop; the session plays
        the local fallback cue and ends the turn with an error."""
        self.fatal = True
        self._end_handoff()
        now = self.clock.now_ms()
        self._write_trace({"error_class": error_class.value, "restart_attempts_in_turn": self.attempts_in_turn})
        await self.bus.emit("session.rebuild_failed", reason=reason.value, error_class=error_class.value)
        return RebuildResult(
            ok=False, reason=reason, session_seq=self.context.session_seq,
            delivered_registry=self.delivered_registry, handoff_start_ms=now, handoff_done_ms=now,
            error_class=error_class,
        )

    # ---------------------------------------------------------- replay rule

    def _replay_decision(self) -> tuple[str | None, TurnId | None, str | None]:
        """Spec 05 §4.5: an unterminated last turn replays its user text unless
        a write executed (or is unknown) or part of the reply was already heard."""
        turn = self._open_turn_id
        if turn is None:
            return None, None, None
        text = self.context.last_user_text(turn)
        if not text:
            return None, None, None
        if self.context.has_presented_output(turn):
            return None, turn, "partial_reply"  # stays in history with the marker, not re-said
        write = self.context.write_state_in_turn(turn)
        if write == "executed":
            return None, turn, "write_executed"
        if write == "unknown":
            return None, turn, "write_unknown"
        return text, turn, None

    # ------------------------------------------------------------- idle gap

    def is_idle(self) -> bool:
        """Spec 05 §4.3: assistant done, no open tool calls, ~1 s quiet, user silent."""
        if self._open_turn_id is not None or self._user_speaking:
            return False
        if self._current_turn_id is not None and self.tools.has_open_calls(self._current_turn_id):
            return False
        return self.clock.now_ms() - self._last_activity_ms >= self.params.idle_gap_ms

    async def _wait_for_idle(self) -> None:
        """Poll the idle condition on the injected clock. An urgent rebuild
        cancels the sleep; at the wait cap (spec 05 §4.3) the swap is forced
        at the next turn end, never mid-speech."""
        self._waiting_task = asyncio.current_task()
        try:
            deadline = self.clock.now_ms() + self.params.idle_wait_max_ms
            while self._urgent is None and not self.is_idle():
                remaining = deadline - self.clock.now_ms()
                if remaining <= 0:
                    self._turn_closed.clear()
                    if self._open_turn_id is None:
                        return  # nothing is being said: force now
                    await self._turn_closed.wait()
                    return
                await self.clock.sleep(min(self._tick_ms, remaining))
        except asyncio.CancelledError:
            if self._urgent is None:
                raise
        finally:
            self._waiting_task = None

    # ------------------------------------------------------- handoff buffer

    @property
    def handoff_active(self) -> bool:
        return self._handoff_active

    def buffer_uplink(self, pcm16: bytes) -> bool:
        """During a handoff, keep user audio locally (spec 05 §4.7). Returns
        True when the frame was taken (caller must not send it upstream).
        Over the cap the frame is dropped and `session.handoff_buffer_full`
        is emitted once so the session can play a cue."""
        if not self._handoff_active:
            return False
        frame_ms = len(pcm16) / 2 / _UPLINK_SAMPLE_RATE * 1000
        if self._uplink_ms + frame_ms > self.params.handoff_buffer_max_ms:
            if not self._uplink_full:
                self._uplink_full = True
                self.bus.emit_soon("session.handoff_buffer_full", buffered_ms=int(self._uplink_ms))
            return True
        self._uplink.append(pcm16)
        self._uplink_ms += frame_ms
        return True

    def _begin_handoff(self) -> None:
        self._handoff_active = True

    def _end_handoff(self) -> list[bytes]:
        frames, self._uplink = self._uplink, []
        self._uplink_ms = 0.0
        self._uplink_full = False
        self._handoff_active = False
        return frames

    # --------------------------------------------------------------- misc

    def _maybe_reset_budget(self) -> None:
        """Spec 05 §7.2: counter clears after 10 s of stable connection."""
        if (
            self._stable_since_ms is not None
            and self.clock.now_ms() - self._stable_since_ms >= self.params.rebuild_stable_reset_ms
        ):
            self.attempts_in_turn = 0
            self.fatal = False
            self._stable_since_ms = None

    def _maybe_summarize(self) -> None:
        if self.summarizer is None:
            return
        limits = self.caps.get("history.limits") or {}
        if self.summarizer.should_trigger(
            self.context.turns_since_summary(), self.context.estimate_history_chars(), limits.get("max_tokens")
        ):
            self.summarizer.schedule(self.context)

    def _write_trace(self, fragment: dict[str, Any]) -> None:
        if self._current_turn_id is None:
            log.debug("no current turn; dropping trace fragment %s", sorted(fragment))
            return
        self.trace.write(self._current_turn_id, fragment)


def _turn_id(payload: dict[str, Any]) -> TurnId | None:
    v = payload.get("turn_id")
    if isinstance(v, TurnId):
        return v
    if isinstance(v, str):
        return TurnId.parse(v)
    return None
