"""Output arbiter (spec 02): one microphone, one bounded queue, one generation.

Who may speak, how much, and what happens after an interruption. Every
output (model reply, verbatim tool result, cue, narration) enters as an
`OutputRequest`; only the holder of the current lease sends audio frames,
and every frame carries its generation (spec 02 §2.5: compared at enqueue,
at grant and per frame). All timers go through the injected clock.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from vak.audio.resample import resample_pcm16
from vak.audio.silence import first_nonsilent_ms
from vak.audio.text import flush_remainder, split_sentences
from vak.core.arbiter.state import (
    ArbiterEvent,
    ArbiterState,
    RequestEvent,
    arbiter_next,
    request_next,
)
from vak.core.bus import Bus
from vak.core.clock import Clock
from vak.core.defaults import ArbiterParams
from vak.core.ids import IdGen
from vak.core.ports import ContextPort, CuePort, TracePort, TransportPort
from vak.core.types import (
    PRIORITY_ORDER,
    ContentRole,
    ContentType,
    ContextPolicy,
    DropReason,
    InterruptSource,
    Kind,
    Lease,
    Origin,
    OutputContent,
    OutputRequest,
    OutputStatus,
    PlaybackReceipt,
    PlaybackState,
    Priority,
    Profile,
    ReceiptMode,
    RequestStatus,
    TurnId,
    TurnSource,
)
from vak.upstream.base import Upstream
from vak.upstream.providers.base import TTS

log = logging.getLogger(__name__)

# spec 02 §2.2: origin -> (default kind, default priority). delegated_result is
# speech inside the turn and narration after it; the factory below picks by turn_id.
ORIGIN_DEFAULTS: dict[Origin, tuple[Kind, Priority]] = {
    Origin.MODEL: (Kind.SPEECH, Priority.NORMAL),
    Origin.TOOL_RESULT: (Kind.SPEECH, Priority.NORMAL),
    Origin.DELEGATED_RESULT: (Kind.SPEECH, Priority.NORMAL),
    Origin.CUE: (Kind.CUE, Priority.HIGH),
    Origin.TASK_PROGRESS: (Kind.NARRATION, Priority.LOW),
    Origin.TASK_RESULT: (Kind.NARRATION, Priority.NORMAL),
    Origin.REMINDER: (Kind.NARRATION, Priority.NORMAL),
    Origin.DEVICE_EVENT: (Kind.NARRATION, Priority.NORMAL),
    Origin.SYSTEM_NOTICE: (Kind.SPEECH, Priority.HIGH),
    Origin.SYSTEM_EXIT: (Kind.SPEECH, Priority.CRITICAL),
}

# spec 02 §2.5: interruptible=false only for whitelisted origins, ≤ 3 s.
UNINTERRUPTIBLE_ORIGINS = frozenset({Origin.SYSTEM_NOTICE, Origin.SYSTEM_EXIT})
_UNINTERRUPTIBLE_MAX_MS = 3_000  # spec 02 §4; belongs in ArbiterParams (reported as a contract gap)

CAUSE_USER = "user_interruption"
CAUSE_CRITICAL = "critical"
CAUSE_RECEIPT_TIMEOUT = "receipt_timeout"
CAUSE_DIRECTED = "directed_flush"


def build_request(
    ids: IdGen,
    origin: Origin,
    content: OutputContent,
    *,
    generation: int | None = None,
    turn_id: TurnId | None = None,
    kind: Kind | None = None,
    priority: Priority | None = None,
    **overrides: Any,
) -> OutputRequest:
    """Build an OutputRequest with the §2.2 defaults for kind and priority."""
    d_kind, d_prio = ORIGIN_DEFAULTS[origin]
    if kind is None:
        kind = d_kind
        if origin == Origin.DELEGATED_RESULT and turn_id is None:
            kind = Kind.NARRATION
    if kind == Kind.CUE:
        overrides.setdefault("context_policy", ContextPolicy.NONE)
        overrides.setdefault("content_role", ContentRole.FILLER)
    if kind == Kind.NARRATION:
        generation = None  # stamped at grant (spec 02 §2.5)
    return OutputRequest(
        request_id=ids.new("req"),
        origin=origin,
        kind=kind,
        content=content,
        priority=priority or d_prio,
        generation=generation,
        turn_id=turn_id,
        **overrides,
    )


@dataclass(slots=True)
class Sentence:
    """One downlink sentence: `(turn_id, sentence_id)` is the receipt key."""

    sentence_id: int
    turn_id: TurnId
    text: str | None
    offset_ms: int  # start on the turn's downlink timeline (spec 01 §3.2.3)
    item_id: str | None = None
    duration_ms: int = 0  # accumulated as frames are sent
    frame_idx: int = 0
    carry: bytes = b""
    sample_rate: int = 16000
    first_sent_at_ms: int | None = None
    started: bool = False
    ended: bool = False
    played_ms: int = 0

    @property
    def key(self) -> tuple[TurnId, int]:
        return (self.turn_id, self.sentence_id)


@dataclass(slots=True)
class Request:
    """Internal lifecycle record (spec 02 §2.4) for one OutputRequest."""

    req: OutputRequest
    seq: int
    enqueued_at_ms: int
    status: RequestStatus = RequestStatus.QUEUED
    retries: int = 0
    lease: Lease | None = None
    turn_id: TurnId | None = None  # bound turn (narration binds at grant)
    sentences: list[Sentence] = field(default_factory=list)
    ready_at_ms: int | None = None  # content first frame reached the arbiter (spec 06 §2.3)
    granted_at_ms: int | None = None
    sent_at_ms: int | None = None
    send_complete_at_ms: int | None = None
    stream_done: bool = False
    pending_audio: list[tuple[bytes, int, str | None]] = field(default_factory=list)
    draining: bool = False
    task: asyncio.Task[None] | None = None
    revoked: bool = False
    cause: str | None = None
    drop_reason: DropReason | None = None
    played_ms: int = 0
    estimated: bool = False
    superseded: bool = False
    context_pending_sent: bool = False
    context_played_sent: bool = False
    flush_id: str | None = None
    uninterruptible: bool = False
    frames_sent: int = 0
    truncate_pending: bool = False
    first_sound_pending: bool = True
    finalize_pending: bool = False

    @property
    def active(self) -> bool:
        return self.status in (RequestStatus.GRANTED, RequestStatus.SENT, RequestStatus.PLAYING)

    @property
    def terminal(self) -> bool:
        return self.status in (RequestStatus.DONE, RequestStatus.DROPPED)


@dataclass(slots=True)
class _TurnStats:
    """Per-turn arbiter counters written to the trace (spec 06 §3.8)."""

    drops_by_reason: dict[str, int] = field(default_factory=lambda: {r.value: 0 for r in DropReason})
    receipt_timeouts: dict[str, int] = field(default_factory=lambda: {"started": 0, "ended": 0})
    flush_ms_max: int = 0
    queue_truncated: bool = False
    truncate_estimated: bool = False
    sound_first_at_ms: int | None = None
    content_first_at_ms: int | None = None


class Arbiter:
    """Implements `ArbiterPort` (spec 02 §2.9)."""

    def __init__(
        self,
        *,
        profile: Profile,
        params: ArbiterParams,
        clock: Clock,
        bus: Bus,
        transport: TransportPort,
        context: ContextPort,
        trace: TracePort,
        ids: IdGen,
        upstream: Upstream,
        tts: TTS | None,
        cues: CuePort | None,
        frame_ms: int = 20,
        client_receipts: set[str] | None = None,
    ) -> None:
        self.profile = profile
        self.params = params
        self.clock = clock
        self.bus = bus
        self.transport = transport
        self.context = context
        self.trace = trace
        self.ids = ids
        self.upstream = upstream
        self.tts = tts
        self.cues = cues
        self.frame_ms = frame_ms
        self.client_receipts = (
            set(client_receipts) if client_receipts is not None else {"started", "ended", "interrupted"}
        )
        # spec 02 §2.6 last paragraph: no `ended` receipts -> estimated playback end
        self.receipt_mode = ReceiptMode.RECEIPTS if "ended" in self.client_receipts else ReceiptMode.ESTIMATED
        self.queue_max = params.queue_max_A if profile == Profile.A else params.queue_max_B

        self._g = 0
        self._barge_gen: int | None = None  # generation of the last barge-in not yet followed by turn.start
        self._state = ArbiterState.IDLE
        self._holder: Request | None = None
        self._queue: list[Request] = []
        self._seq = 0
        self._by_id: dict[str, Request] = {}
        self._by_response: dict[str, str] = {}
        self._cancelled_responses: set[str] = set()
        self._item_text: dict[tuple[str, str], str] = {}  # (response_id, item_id) -> sentence text
        self._delivered_keys: set[str] = set()
        # window conditions (spec 02 §2.6)
        self._user_speaking = False
        self._endpointing = False
        self._turn_pending: set[TurnId] = set()
        self._awaiting_tool: set[TurnId] = set()
        self._unplayed: set[tuple[TurnId, int]] = set()
        self._playing: set[tuple[TurnId, int]] = set()
        self._window_open_since: int | None = None
        # turn bookkeeping
        self._current_turn: TurnId | None = None
        self._reply_done: set[TurnId] = set()
        self._drained: set[TurnId] = set()
        self._flushed_turns: set[TurnId] = set()
        self._next_sid: dict[TurnId, int] = {}
        self._turn_offset_ms: dict[TurnId, int] = {}
        self._sentence_owner: dict[tuple[TurnId, int], str] = {}
        self._stats: dict[TurnId, _TurnStats] = {}
        self._flush_pending = 0
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._flush_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ props

    @property
    def generation(self) -> int:
        return self._g

    @property
    def state(self) -> ArbiterState:
        return self._state

    @property
    def holder(self) -> Request | None:
        return self._holder

    @property
    def queue(self) -> list[Request]:
        return list(self._queue)

    def record(self, request_id: str) -> Request | None:
        return self._by_id.get(request_id)

    def sentence_owner(self, turn_id: TurnId, sentence_id: int) -> str | None:
        return self._sentence_owner.get((turn_id, sentence_id))

    def note_item_text(self, response_id: str | None, item_id: str | None, text: str) -> None:
        """Text of an upstream item, for `tts.sentence_start{text}` (spec 01 §2.6). Cascade
        and half-cascade emit `response.sentence` before the item's audio."""
        if response_id is None or item_id is None or not text:
            return
        key = (response_id, item_id)
        self._item_text[key] = text if key not in self._item_text else self._item_text[key] + text

    def request_for_response(self, response_id: str) -> str | None:
        return self._by_response.get(response_id)

    def window_open(self) -> bool:
        return not self._window_closed()

    # ---------------------------------------------------------------- intake

    async def submit(self, req: OutputRequest) -> None:
        self._validate(req)
        now = self.clock.now_ms()
        self._seq += 1
        rec = Request(req=req, seq=self._seq, enqueued_at_ms=now, uninterruptible=not req.interruptible)
        if req.content.type != ContentType.UPSTREAM_STREAM:
            rec.ready_at_ms = now
        self._by_id[req.request_id] = rec
        if req.content.type == ContentType.UPSTREAM_STREAM and req.content.response_id:
            self._by_response[req.content.response_id] = req.request_id

        if req.priority == Priority.CRITICAL:  # spec 02 §2.3: critical pre-empts
            await self._preempt(rec)
            return
        # generation check 1/3: enqueue (spec 02 §2.5)
        if req.kind != Kind.NARRATION and (req.generation or 0) < self._g:
            await self._drop(rec, DropReason.STALE)
            return
        if req.expires_at_ms is not None and req.expires_at_ms <= now:
            await self._drop(rec, DropReason.EXPIRED)
            return
        if req.supersedes is not None:  # spec 02 §3.8: replace queued, not-started same-key requests
            for old in [q for q in self._queue if q.req.dedupe_key == req.supersedes]:
                old.superseded = True
                await self._drop(old, DropReason.DUPLICATE)
        if req.dedupe_key is not None and self._dedupe_hit(req):  # spec 02 §2.8 / §3.8
            await self._drop(rec, DropReason.DUPLICATE)
            return
        if req.context_policy == ContextPolicy.BEFORE_SPEAK:
            self._notify_pending(rec, req.turn_id or self._current_turn)
        self._queue.append(rec)
        await self._enforce_queue_bound()
        await self._schedule()

    def _validate(self, req: OutputRequest) -> None:
        c = req.content
        if req.kind == Kind.NARRATION:
            if req.generation is not None:
                raise ValueError("narration must not carry a generation at submit (stamped at grant)")
        else:
            if req.generation is None or req.turn_id is None:
                raise ValueError(f"{req.kind.value} needs generation and turn_id")
        if c.type == ContentType.PRERECORDED:
            if c.audio is None:
                hit = self.cues.get(c.text or "") if self.cues else None
                if hit is None:
                    raise ValueError(f"prerecorded content without audio and no cue {c.text!r}")
                c.audio, c.sample_rate, c.duration_ms = hit
            elif c.duration_ms is None:
                c.duration_ms = (len(c.audio) // 2) * 1000 // c.sample_rate
        elif c.type == ContentType.TEXT_TTS:
            if not c.text:
                raise ValueError("text_tts content needs text")
            if self.tts is None:
                raise ValueError("text_tts content but no TTS configured")
        elif c.type == ContentType.UPSTREAM_STREAM:
            if not c.response_id:
                raise ValueError("upstream_stream content needs response_id")
        if not req.interruptible:
            too_long = c.duration_ms is not None and c.duration_ms > _UNINTERRUPTIBLE_MAX_MS
            if req.origin not in UNINTERRUPTIBLE_ORIGINS or too_long:
                log.warning(
                    "request %s: interruptible=false not allowed (origin=%s, duration=%s); treated as true",
                    req.request_id,
                    req.origin.value,
                    c.duration_ms,
                )
                req.interruptible = True
        if req.on_interrupt == "retry" and req.max_retries == 0:  # spec 02 §4 defaults
            req.max_retries = (
                self.params.max_retries_progress
                if req.origin == Origin.TASK_PROGRESS
                else self.params.max_retries_result
            )

    def _dedupe_hit(self, req: OutputRequest) -> bool:
        key = req.dedupe_key
        if key in self._delivered_keys:
            return True
        for other in self._by_id.values():
            if other.req is req or other.req.dedupe_key != key:
                continue
            if other.status in (RequestStatus.QUEUED, RequestStatus.RETRY_WAIT) or other.active:
                return True
        return False

    async def _enforce_queue_bound(self) -> None:
        # spec 02 §2.3: overflow drops the lowest priority, earliest enqueued
        while len(self._queue) > self.queue_max:
            victim = min(self._queue, key=lambda r: (-PRIORITY_ORDER[r.req.priority], r.seq))
            self._turn_stats(self._trace_turn(victim)).queue_truncated = True
            self._trace_write(self._trace_turn(victim), {"queue_truncated": True})
            await self._drop(victim, DropReason.OVERFLOW)

    # --------------------------------------------------- turn machine inputs

    async def on_turn_start(self, turn_id: TurnId, source: TurnSource, merged: bool = False) -> int:
        if merged:  # spec 02 §2.5: merged into a holding turn, no new generation
            return self._g
        if self._barge_gen == self._g and self._holder is None:
            # abort immediately followed by turn.start (spec 01 §2.8 steps 3–4): one +1 only
            self._barge_gen = None
        else:
            self._bump_generation()
        self._current_turn = turn_id
        self._turn_pending = {turn_id}
        self._user_speaking = True
        self._turn_stats(turn_id)
        await self._flush_holder(reason="turn_start", cause=CAUSE_USER, ack_of=None)
        await self._drop_stale_queued()
        await self._schedule()
        return self._g

    async def on_barge_in(self, turn_id: TurnId, source: InterruptSource) -> int:
        self._bump_generation()
        self._barge_gen = self._g
        self._turn_pending.discard(turn_id)
        ack_of = (
            "abort"
            if source in (InterruptSource.BUTTON, InterruptSource.TEXT, InterruptSource.WAKE)
            else None
        )
        await self._flush_holder(reason="abort", cause=CAUSE_USER, ack_of=ack_of)
        await self._drop_stale_queued()
        await self._schedule()
        return self._g

    def set_user_speaking(self, speaking: bool) -> None:
        self._user_speaking = speaking
        self._kick()

    def set_endpointing(self, turn_id: TurnId, active: bool) -> None:
        self._endpointing = active
        self._kick()

    def set_awaiting_tool(self, turn_id: TurnId, awaiting: bool) -> None:
        if awaiting:
            self._awaiting_tool.add(turn_id)
        else:
            self._awaiting_tool.discard(turn_id)
        self._kick()

    async def on_reply_done(self, turn_id: TurnId, outcome: str) -> None:
        self._reply_done.add(turn_id)
        self._trace_write(turn_id, {"generation": self._g})  # spec 06 §3.2
        if not any(r.turn_id == turn_id or r.req.turn_id == turn_id for r in self._by_id.values()):
            await self.bus.emit("turn.no_output", turn_id=turn_id)  # spec 02 §2.9
        await self._recheck_turns()
        await self._schedule()

    def _kick(self) -> None:
        """Re-evaluate the window from a sync entry point."""
        asyncio.get_running_loop().create_task(self._after_sync_change())

    async def _after_sync_change(self) -> None:
        await self._recheck_turns()
        await self._schedule()

    def _bump_generation(self) -> None:
        self._g += 1
        self._barge_gen = None
        self.transport.set_generation(self._g)

    # --------------------------------------------------------------- receipts

    async def on_receipt(self, receipt: PlaybackReceipt) -> None:
        key = (receipt.turn_id, receipt.sentence_id)
        rid = self._sentence_owner.get(key)
        if rid is None:
            log.warning("receipt for unknown sentence %s/%s", receipt.turn_id, receipt.sentence_id)
            return
        receipt.request_id = rid
        rec = self._by_id[rid]
        s = next((x for x in rec.sentences if x.key == key), None)
        if receipt.kind == "started":
            others = {self._sentence_owner[k] for k in self._playing if self._sentence_owner.get(k) != rid}
            if others:  # spec 06 §3.8 double_voice_count
                self._trace_incr(receipt.turn_id, "double_voice_count")
                log.error("double voice: %s started while %s playing", rid, others)
            self._playing.add(key)
            if s is not None:
                s.started = True
            if rec.status == RequestStatus.SENT:
                rec.status = request_next(rec.status, RequestEvent.STARTED)
                self._disarm(f"started:{rid}")
        elif receipt.kind == "ended":
            self._playing.discard(key)
            self._unplayed.discard(key)
            if s is not None and not s.ended:
                s.ended = True
                s.played_ms = receipt.played_ms
                rec.played_ms += receipt.played_ms
            await self._maybe_complete(rec)
        else:  # interrupted: the client stopped this sentence; no more ended for the turn
            self._playing.discard(key)
            if s is not None and not s.ended:
                s.ended = True
                s.played_ms = receipt.played_ms
                rec.played_ms += receipt.played_ms
            for x in rec.sentences:
                self._unplayed.discard(x.key)
            if rec.truncate_pending and s is not None:
                self._disarm(f"truncate:{rid}")
                await self._truncate(rec, s, receipt.played_ms, estimated=False)
            if rec.active:  # client-local stop (A-class button): user interruption
                if self._holder is rec:
                    self._release_holder(rec)
                await self._interrupt(rec, CAUSE_USER)
            if rec.finalize_pending:
                self._finalize_now(rec)
        await self._recheck_turns()
        await self._schedule()

    async def _maybe_complete(self, rec: Request) -> None:
        if not rec.active or not rec.stream_done or rec.send_complete_at_ms is None:
            return
        if all(s.ended for s in rec.sentences):
            await self._finish_done(rec, estimated=False)

    # -------------------------------------------------------- upstream audio

    async def feed_upstream_audio(
        self, request_id: str, pcm16: bytes, sample_rate: int, response_id: str, item_id: str, generation: int
    ) -> None:
        rec = self._by_id.get(request_id)
        if rec is None:
            rid = self._by_response.get(response_id)
            rec = self._by_id.get(rid) if rid else None
        turn = rec.turn_id or rec.req.turn_id if rec else self._current_turn
        if response_id in self._cancelled_responses:
            self._trace_incr(turn, "suppressed_frames")  # spec 02 §2.5: counted, not a filter
        if (
            rec is None
            or rec.terminal
            or rec.revoked
            or generation < self._g
            or (rec.req.generation or 0) < self._g
        ):
            self._trace_incr(turn, "stale_frames_dropped")
            return
        if rec.ready_at_ms is None:
            rec.ready_at_ms = self.clock.now_ms()
        rec.pending_audio.append((pcm16, sample_rate, item_id))
        if self._holder is rec and rec.task is not None and rec.task.done():
            await self._drain_upstream(rec)

    async def end_upstream_stream(self, request_id: str) -> None:
        rec = self._by_id.get(request_id)
        if rec is None or rec.terminal:
            return
        rec.stream_done = True
        if self._holder is rec and rec.task is not None and rec.task.done() and not rec.draining:
            await self._drain_upstream(rec)

    async def _drain_upstream(self, rec: Request) -> None:
        if rec.draining:
            return
        rec.draining = True
        try:
            while rec.pending_audio and not rec.revoked:
                pcm, rate, item_id = rec.pending_audio.pop(0)
                s = rec.sentences[-1] if rec.sentences and rec.sentences[-1].item_id == item_id else None
                if s is None:
                    if rec.sentences and not await self._send_pcm(
                        rec, rec.sentences[-1], b"", rate, final=True
                    ):
                        return
                    rid = rec.req.content.response_id or ""
                    s = self._new_sentence(rec, self._item_text.get((rid, item_id or "")), item_id)
                    await self._send_sentence_start(rec, s, None)
                if not await self._send_pcm(rec, s, pcm, rate, final=False):
                    return
            if rec.stream_done and not rec.revoked and rec.send_complete_at_ms is None:
                if rec.sentences and not await self._send_pcm(
                    rec, rec.sentences[-1], b"", rec.sentences[-1].sample_rate, final=True
                ):
                    return
                await self._send_complete(rec)
        finally:
            rec.draining = False

    def params_downlink_rate(self, rec: Request) -> int:
        return rec.sentences[0].sample_rate if rec.sentences else rec.req.content.sample_rate

    # ----------------------------------------------------------- scheduling

    async def _schedule(self) -> None:
        self._update_window()
        await self._try_grant()
        self._ensure_wakeup()

    def _window_closed(self) -> bool:
        # spec 02 §2.6: user speaking (incl. endpointing), turn pending, unplayed audio
        return self._user_speaking or self._endpointing or bool(self._turn_pending) or bool(self._unplayed)

    def _update_window(self) -> None:
        if self._window_closed():
            self._window_open_since = None
        elif self._window_open_since is None:
            self._window_open_since = self.clock.now_ms()

    def _narration_ready(self, rec: Request, now: int) -> bool:
        if self._window_open_since is None:
            return False
        waited = now - self._window_open_since
        if waited < self.params.narration_window_silence_ms:
            self._arm("silence", self.params.narration_window_silence_ms - waited, self._schedule)
            return False
        if rec.req.origin == Origin.TASK_PROGRESS:  # spec 02 §2.6: stricter, nothing in flight
            if self._holder is not None or any(q.req.kind != Kind.NARRATION for q in self._queue):
                return False
        return True

    def _ensure_wakeup(self) -> None:
        # spec 02 §2.3: blocked narration re-checks every 1 s, independent of events
        if "wakeup" not in self._timers and any(q.req.kind == Kind.NARRATION for q in self._queue):
            self._arm("wakeup", self.params.narration_wakeup_ms, self._schedule)

    async def _try_grant(self) -> None:
        if self._state != ArbiterState.IDLE:
            return
        now = self.clock.now_ms()
        for rec in sorted(self._queue, key=lambda r: (PRIORITY_ORDER[r.req.priority], r.seq)):
            req = rec.req
            # generation check 2/3: grant (spec 02 §2.5)
            if req.kind != Kind.NARRATION and (req.generation or 0) < self._g:
                await self._drop(rec, DropReason.STALE)
                continue
            if req.expires_at_ms is not None and req.expires_at_ms <= now:
                await self._drop(rec, DropReason.EXPIRED)
                continue
            if req.kind == Kind.NARRATION and not self._narration_ready(rec, now):
                continue  # skipped narration stays queued and does not block cue / speech
            await self._grant(rec, ArbiterEvent.GRANT)
            return

    async def _grant(self, rec: Request, event: ArbiterEvent) -> None:
        self._state = arbiter_next(self._state, event)
        if rec in self._queue:
            self._queue.remove(rec)
        now = self.clock.now_ms()
        if rec.req.kind == Kind.NARRATION:
            rec.req.generation = self._g  # stamped at grant (spec 02 §2.5)
            rec.turn_id = rec.req.turn_id or await self._new_event_turn()
        else:
            rec.turn_id = rec.req.turn_id
        rec.lease = Lease(self.ids.new("lease"), self._g)
        rec.status = request_next(rec.status, RequestEvent.GRANT)
        rec.granted_at_ms = now
        self._holder = rec
        self._turn_stats(rec.turn_id)
        if rec.req.context_policy == ContextPolicy.AFTER_PLAYED:
            self._notify_pending(rec, rec.turn_id)
        rec.task = asyncio.get_running_loop().create_task(self._run(rec))

    async def _new_event_turn(self) -> TurnId:
        """Narration without a turn binds to a fresh gateway event turn (spec 01 §3.2.6)."""
        t = TurnId("s", self.ids.new_turn_n())
        self._reply_done.add(t)  # nothing else will speak in it
        await self.transport.send_json(
            {"type": "turn.start", "turn_id": str(t), "source": "event", "kind": "narration"}
        )
        return t

    async def _preempt(self, rec: Request) -> None:
        # spec 02 §2.3: critical = a system-initiated interruption, then grant itself
        self._bump_generation()
        rec.req.generation = self._g
        await self._flush_holder(reason="critical", cause=CAUSE_CRITICAL, ack_of=None, include_cue=True)
        await self._drop_stale_queued()
        event = ArbiterEvent.CRITICAL if self._state == ArbiterState.FLUSHING else ArbiterEvent.GRANT
        if self._state == ArbiterState.GRANTED:  # cannot happen: flush_holder revoked any holder
            raise RuntimeError("critical pre-emption with a holder still granted")
        await self._grant(rec, event)

    # ---------------------------------------------------------------- flush

    async def _flush_holder(
        self, *, reason: str, cause: str, ack_of: str | None, include_cue: bool = False
    ) -> bool:
        """Spec 02 §2.7 action sequence for the current holder. Returns True if a flush started."""
        rec = self._holder
        if rec is None:
            return False
        if not include_cue and (rec.req.kind == Kind.CUE or rec.uninterruptible):
            return False  # cue / uninterruptible in play finishes under the new generation
        self._state = arbiter_next(self._state, ArbiterEvent.FLUSH)
        self._holder = None
        self._flush_pending += 1
        turn = rec.turn_id
        if turn is not None:
            self._flushed_turns.add(turn)
        await self._interrupt(rec, cause)
        task = asyncio.get_running_loop().create_task(self._flush_run(rec, reason, ack_of))
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)
        self._arm("flush_timeout", self.params.flush_wait_max_ms, self._on_flush_timeout)
        return True

    async def _flush_run(self, rec: Request, reason: str, ack_of: str | None) -> None:
        t0 = self.clock.now_ms()
        turn = rec.turn_id
        rid = rec.req.request_id
        c = rec.req.content
        self._trace_incr(turn, "flush_count")
        try:
            if c.type == ContentType.UPSTREAM_STREAM and c.response_id:
                self._cancelled_responses.add(c.response_id)
                await self.upstream.cancel_response(c.response_id)
                self._arm_truncate(rec)
            elif c.type == ContentType.TEXT_TTS and self.tts is not None:
                await self.tts.flush(rid)  # returns at the TTS flush_end
            if turn is not None:
                rec.flush_id = await self.transport.flush([turn], reason, ack_of)
                await self.transport.send_json(
                    {"type": "tts.stop", "turn_id": str(turn), "reason": "interrupted"}
                )
        finally:
            ms = self.clock.now_ms() - t0
            st = self._turn_stats(turn)
            if ms > st.flush_ms_max:
                st.flush_ms_max = ms
                self._trace_write(turn, {"flush_ms_max": ms})
            await self._on_flush_done()

    async def _on_flush_done(self) -> None:
        self._flush_pending = max(0, self._flush_pending - 1)
        if self._flush_pending == 0 and self._state == ArbiterState.FLUSHING:
            self._disarm("flush_timeout")
            self._state = arbiter_next(self._state, ArbiterEvent.FLUSH_END)
            await self._schedule()

    async def _on_flush_timeout(self) -> None:
        if self._state != ArbiterState.FLUSHING:
            return
        self._trace_incr(self._current_turn, "flush_timeouts")
        self._flush_pending = 0
        self._state = arbiter_next(self._state, ArbiterEvent.FLUSH_END)
        await self._schedule()

    async def flush_origin(
        self, origin: str, turn_id: TurnId | None = None, dedupe_key: str | None = None
    ) -> None:
        """Directed flush (spec 02 §2.7): drop queued matches, revoke a matching speech/narration holder."""

        def match(r: Request) -> bool:
            if r.req.origin.value != origin:
                return False
            if turn_id is not None and r.req.turn_id != turn_id and r.turn_id != turn_id:
                return False
            return dedupe_key is None or r.req.dedupe_key == dedupe_key

        for q in [q for q in self._queue if match(q)]:
            await self._drop(q, None)
        h = self._holder
        if h is not None and match(h):
            await self._flush_holder(reason="origin", cause=CAUSE_DIRECTED, ack_of=None)
        await self._schedule()

    async def _drop_stale_queued(self) -> None:
        # spec 02 §2.7: queued speech / cue below the new generation drop; narration stays
        for q in [
            q for q in self._queue if q.req.kind != Kind.NARRATION and (q.req.generation or 0) < self._g
        ]:
            await self._drop(q, DropReason.STALE)

    def _arm_truncate(self, rec: Request) -> None:
        rec.truncate_pending = True
        if "interrupted" not in self.client_receipts:
            asyncio.get_running_loop().create_task(self._truncate_estimated(rec))
            return
        # exact position arrives with playback.interrupted; estimate after the flush wait
        self._arm(
            f"truncate:{rec.req.request_id}",
            self.params.flush_wait_max_ms,
            lambda: self._truncate_estimated(rec),
        )

    async def _truncate_estimated(self, rec: Request) -> None:
        if not rec.truncate_pending:
            return
        s = next((x for x in reversed(rec.sentences) if x.first_sent_at_ms is not None), None)
        if s is None:
            rec.truncate_pending = False
            return
        elapsed = self.clock.now_ms() - (s.first_sent_at_ms or 0) - self.transport.lead_ms
        played = max(0, min(s.duration_ms, elapsed))
        await self._truncate(rec, s, played, estimated=True)

    async def _truncate(self, rec: Request, s: Sentence, played_ms: int, *, estimated: bool) -> None:
        rec.truncate_pending = False
        item_id = s.item_id or rec.req.content.item_id
        if item_id is None:
            return
        # spec 02 §2.9: audio_end_ms = offset_ms(sentence) + played_ms, relative to the item start
        item_base = min(
            x.offset_ms for x in rec.sentences if (x.item_id or rec.req.content.item_id) == item_id
        )
        audio_end_ms = s.offset_ms - item_base + played_ms
        if estimated:
            self._turn_stats(rec.turn_id).truncate_estimated = True
            self._trace_write(rec.turn_id, {"truncate_estimated": True})
        ack = await self.upstream.truncate(item_id, audio_end_ms, None)
        if not ack.ok:
            log.info("truncate unsupported/failed for %s: %s", item_id, ack.detail)

    # ----------------------------------------------------- interrupt / retry

    async def _interrupt(self, rec: Request, cause: str, *, keep_unplayed: bool = False) -> None:
        rec.revoked = True
        rec.cause = cause
        if rec.task is not None and not rec.task.done():
            rec.task.cancel()
        self._disarm(f"started:{rec.req.request_id}")
        if not keep_unplayed:
            self._disarm(f"ended:{rec.req.request_id}")
            for s in rec.sentences:
                self._unplayed.discard(s.key)
                self._playing.discard(s.key)
        rec.status = request_next(rec.status, RequestEvent.INTERRUPT)
        await self._resolve_interrupted(rec)

    async def _resolve_interrupted(self, rec: Request) -> None:
        """Spec 02 §2.8 decision table, first match wins."""
        req = rec.req
        if rec.cause == CAUSE_DIRECTED:  # removal by its owner, outside the table
            await self._drop(rec, None)
            return
        if rec.cause == CAUSE_USER:  # row 1: delivered, never replayed
            rec.status = request_next(rec.status, RequestEvent.DELIVERED)
            self._mark_delivered(rec)
            self._finalize(rec)
            return
        if req.kind in (Kind.CUE, Kind.SPEECH):  # row 2
            await self._drop(rec, None)
            return
        if req.on_interrupt == "drop":  # row 3
            await self._drop(rec, None)
            return
        if req.expires_at_ms is not None and req.expires_at_ms <= self.clock.now_ms():  # row 4
            await self._drop(rec, DropReason.EXPIRED)
            return
        if rec.superseded or (req.dedupe_key is not None and req.dedupe_key in self._delivered_keys):  # row 5
            await self._drop(rec, DropReason.DUPLICATE)
            return
        if rec.retries >= req.max_retries:  # row 6
            await self._drop(rec, DropReason.RETRY_EXHAUSTED)
            return
        # row 7: requeue, retry count +1
        rec.status = request_next(rec.status, RequestEvent.RETRY)
        rec.retries += 1
        self._reset_attempt(rec)
        self._seq += 1
        rec.seq = self._seq
        rec.status = request_next(rec.status, RequestEvent.REQUEUE)
        self._queue.append(rec)

    def _reset_attempt(self, rec: Request) -> None:
        rec.req.generation = None if rec.req.kind == Kind.NARRATION else rec.req.generation
        rec.lease = None
        rec.turn_id = None
        rec.sentences = []
        rec.granted_at_ms = None
        rec.sent_at_ms = None
        rec.send_complete_at_ms = None
        rec.stream_done = False
        rec.pending_audio = []
        rec.task = None
        rec.revoked = False
        rec.cause = None
        rec.played_ms = 0
        rec.frames_sent = 0
        rec.first_sound_pending = True

    async def _drop(self, rec: Request, reason: DropReason | None) -> None:
        if rec in self._queue:
            self._queue.remove(rec)
        rec.status = request_next(rec.status, RequestEvent.DROP)
        rec.drop_reason = reason
        turn = self._trace_turn(rec)
        if reason is not None:
            st = self._turn_stats(turn)
            st.drops_by_reason[reason.value] += 1
            self._trace_write(turn, {"drops_by_reason": dict(st.drops_by_reason)})
        self._finalize(rec)

    async def _finish_done(self, rec: Request, *, estimated: bool) -> None:
        rec.status = request_next(rec.status, RequestEvent.ENDED)
        rec.estimated = rec.estimated or estimated
        self._disarm(f"started:{rec.req.request_id}")
        self._disarm(f"ended:{rec.req.request_id}")
        for s in rec.sentences:
            self._unplayed.discard(s.key)
            self._playing.discard(s.key)
        self._mark_delivered(rec)
        if self._holder is rec:
            self._release_holder(rec)
        self._finalize(rec)

    def _release_holder(self, rec: Request) -> None:
        self._holder = None
        if self._state == ArbiterState.GRANTED:
            self._state = arbiter_next(self._state, ArbiterEvent.RELEASE)

    def _mark_delivered(self, rec: Request) -> None:
        if rec.req.dedupe_key is not None:
            self._delivered_keys.add(rec.req.dedupe_key)

    def _finalize(self, rec: Request) -> None:
        """Request reached a terminal status: trace outputs[] and context notification.
        A flushed request whose audio was in play waits for the client's
        `playback.interrupted` (it carries the real `played_ms`), capped by the flush wait."""
        if rec.finalize_pending:
            return
        in_play = any(s.first_sent_at_ms is not None and not s.ended for s in rec.sentences)
        if (
            in_play
            and rec.revoked
            and rec.cause != CAUSE_RECEIPT_TIMEOUT
            and "interrupted" in self.client_receipts
        ):
            rec.finalize_pending = True
            self._arm(
                f"final:{rec.req.request_id}", self.params.flush_wait_max_ms, lambda: self._finalize_late(rec)
            )
            return
        self._finalize_now(rec)

    async def _finalize_late(self, rec: Request) -> None:
        self._finalize_now(rec)

    def _finalize_now(self, rec: Request) -> None:
        rec.finalize_pending = False
        self._disarm(f"final:{rec.req.request_id}")
        turn = self._trace_turn(rec)
        status = self._output_status(rec)
        first_sid = rec.sentences[0].sentence_id if rec.sentences else None
        self.trace.append(  # spec 06 §3.8 outputs[]
            turn,
            "outputs",
            {
                "request_id": rec.req.request_id,
                "origin": rec.req.origin.value,
                "kind": rec.req.kind.value,
                "content_role": rec.req.content_role.value,
                "generation": rec.req.generation,
                "sentence_id": first_sid,
                "sent_at_ms": rec.sent_at_ms,
                "played_ms": rec.played_ms,
                "status": status.value,
            },
        ) if turn is not None else None
        self._notify_played(rec, status)

    def _output_status(self, rec: Request) -> OutputStatus:
        if rec.status == RequestStatus.DROPPED and rec.drop_reason is not None:
            return OutputStatus(f"dropped_{rec.drop_reason.value}")
        if rec.status == RequestStatus.DONE and rec.cause is None:
            return OutputStatus.PLAYED
        return OutputStatus.TRUNCATED if rec.played_ms > 0 else OutputStatus.FLUSHED

    # ---------------------------------------------------------------- context

    def _notify_pending(self, rec: Request, turn: TurnId | None) -> None:
        if rec.context_pending_sent or rec.req.context_policy == ContextPolicy.NONE:
            return
        c = rec.req.content
        if c.type == ContentType.UPSTREAM_STREAM or not c.text or turn is None:
            return  # upstream text is recorded by the turn machine from transcript events
        rec.context_pending_sent = True
        self.context.record_assistant_pending(turn, rec.req.request_id, 0, c.text, "local")

    def _notify_played(self, rec: Request, status: OutputStatus) -> None:
        # spec 02 §2.1 context_policy; one notification per request, retries do not repeat
        if rec.context_played_sent or rec.req.context_policy == ContextPolicy.NONE:
            return
        if rec.req.context_policy == ContextPolicy.BEFORE_SPEAK and not rec.context_pending_sent:
            return
        if (
            rec.req.context_policy == ContextPolicy.AFTER_PLAYED
            and rec.granted_at_ms is None
            and rec.sent_at_ms is None
        ):
            return  # never spoke, context never heard of it
        if status == OutputStatus.PLAYED:
            state = PlaybackState.COMPLETED
        elif rec.played_ms > 0:
            state = PlaybackState.INTERRUPTED
        else:
            state = PlaybackState.SKIPPED
        rec.context_played_sent = True
        sid = rec.sentences[0].sentence_id if rec.sentences else 0
        self.context.record_assistant_playback(
            rec.req.request_id, sid, state.value, rec.played_ms, rec.estimated
        )

    # --------------------------------------------------------------- sending

    async def _run(self, rec: Request) -> None:
        turn = rec.turn_id
        assert turn is not None
        c = rec.req.content
        try:
            await self.transport.send_json(
                {"type": "tts.start", "turn_id": str(turn), "kind": rec.req.kind.value}
            )
            if c.type == ContentType.UPSTREAM_STREAM:
                await self._drain_upstream(rec)
                return
            if c.type == ContentType.PRERECORDED:
                s = self._new_sentence(rec, c.text, None)
                await self._send_sentence_start(rec, s, c.duration_ms)
                if not await self._send_pcm(rec, s, c.audio or b"", c.sample_rate, final=True):
                    return
            else:  # text_tts, one synth call per sentence
                assert self.tts is not None
                sentences, rest = split_sentences(c.text or "", first=True)
                sentences += flush_remainder(rest)
                for text in sentences:
                    s = self._new_sentence(rec, text, None)
                    await self._send_sentence_start(rec, s, None)
                    async for chunk in self.tts.synthesize(text, rec.req.request_id):
                        if rec.revoked:
                            return
                        if not await self._send_pcm(rec, s, chunk.pcm16, chunk.sample_rate, final=False):
                            return
                    if not await self._send_pcm(rec, s, b"", s.sample_rate, final=True):
                        return
            await self._send_complete(rec)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("output %s failed", rec.req.request_id)
            if rec.active:
                if self._holder is rec:
                    self._release_holder(rec)
                await self._interrupt(rec, "error")
                await self._schedule()

    def _new_sentence(self, rec: Request, text: str | None, item_id: str | None) -> Sentence:
        turn = rec.turn_id
        assert turn is not None
        sid = self._next_sid.get(turn, 0)  # unified numbering per turn across kinds (spec 01 §1.3)
        self._next_sid[turn] = sid + 1
        s = Sentence(sid, turn, text, offset_ms=self._turn_offset_ms.get(turn, 0), item_id=item_id)
        rec.sentences.append(s)
        self._sentence_owner[s.key] = rec.req.request_id
        return s

    async def _send_sentence_start(self, rec: Request, s: Sentence, duration_ms: int | None) -> None:
        msg: dict[str, Any] = {
            "type": "tts.sentence_start",
            "turn_id": str(s.turn_id),
            "sentence_id": s.sentence_id,
            "offset_ms": s.offset_ms,
            "interruptible": not rec.uninterruptible,
        }
        if s.text is not None:
            msg["text"] = s.text
        if duration_ms is not None:
            msg["duration_ms"] = duration_ms
        await self.transport.send_json(msg)

    async def _send_pcm(self, rec: Request, s: Sentence, pcm: bytes, rate: int, *, final: bool) -> bool:
        target = getattr(self.transport, "sample_rate", None)
        if isinstance(target, int) and target > 0 and rate != target and pcm:
            pcm = resample_pcm16(pcm, rate, target)
            rate = target
        elif isinstance(target, int) and target > 0:
            rate = target
        data = s.carry + pcm
        s.carry = b""
        s.sample_rate = rate
        frame_bytes = max(2, rate * self.frame_ms // 1000 * 2)
        sound_off = first_nonsilent_ms(data, rate) if (rec.first_sound_pending and data) else None
        pos = 0
        while pos < len(data):
            frame = data[pos : pos + frame_bytes]
            if len(frame) < frame_bytes and not final:
                s.carry = frame
                break
            pos += len(frame)
            if not await self._send_frame(rec, s, frame, sound_off):
                return False
            sound_off = None
        return True

    async def _send_frame(self, rec: Request, s: Sentence, frame: bytes, sound_off: int | None) -> bool:
        if rec.revoked or rec.lease is None:
            return False
        gen = rec.lease.generation
        if rec.req.kind == Kind.CUE or rec.uninterruptible:
            gen = self._g  # finishes under the new generation (spec 02 §2.5)
        elif gen < self._g:  # generation check 3/3: per frame
            self._trace_incr(s.turn_id, "stale_frames_dropped")
            return False
        if rec.uninterruptible and s.offset_ms + s.duration_ms > _UNINTERRUPTIBLE_MAX_MS:
            log.warning(
                "request %s exceeds the uninterruptible limit; remaining frames interruptible",
                rec.req.request_id,
            )
            rec.uninterruptible = False
        ok = await self.transport.send_audio(
            frame, s.turn_id, s.sentence_id, s.frame_idx, gen, rec.uninterruptible
        )
        if not ok:
            log.warning("transport dropped frame of %s (consumer filter)", rec.req.request_id)
        now = self.clock.now_ms()
        if s.frame_idx == 0:
            s.first_sent_at_ms = now
            self._unplayed.add(s.key)
        s.frame_idx += 1
        dur = (len(frame) // 2) * 1000 // s.sample_rate
        s.duration_ms += dur
        self._turn_offset_ms[s.turn_id] = self._turn_offset_ms.get(s.turn_id, 0) + dur
        rec.frames_sent += 1
        if rec.sent_at_ms is None:
            rec.sent_at_ms = now
            rec.status = request_next(rec.status, RequestEvent.FIRST_FRAME)
        if sound_off is not None and rec.first_sound_pending:
            rec.first_sound_pending = False
            self._record_first_sound(rec, now + sound_off)
        return True

    def _record_first_sound(self, rec: Request, at_ms: int) -> None:
        """Spec 06 §2.3: sound-first on the first non-silent frame of the turn; content-first
        for the first answer/progress output. Absolute clock ms; the collector subtracts T0."""
        turn = rec.turn_id
        st = self._turn_stats(turn)
        latency: dict[str, Any] = {}
        if st.sound_first_at_ms is None:
            st.sound_first_at_ms = at_ms
            latency["sound_first_at_ms"] = at_ms
        if st.content_first_at_ms is None and rec.req.content_role in (
            ContentRole.ANSWER,
            ContentRole.PROGRESS,
        ):
            st.content_first_at_ms = at_ms
            latency["content_first_at_ms"] = at_ms
            if rec.ready_at_ms is not None:
                latency["content_first_ready_at_ms"] = rec.ready_at_ms
                latency["queue_wait_ms"] = max(0, (rec.sent_at_ms or at_ms) - rec.ready_at_ms)
        if latency:
            self._trace_write(turn, {"latency": latency})

    async def _send_complete(self, rec: Request) -> None:
        turn = rec.turn_id
        assert turn is not None
        rec.stream_done = True
        wait_sent = getattr(self.transport, "wait_sent", None)
        if wait_sent is not None:
            await wait_sent()  # the stop frame must follow the last audio frame (spec 01 §2.6)
            if rec.revoked:
                return  # flushed meanwhile: the flush path sent tts.stop{interrupted}
        now = self.clock.now_ms()
        rec.send_complete_at_ms = now
        await self.transport.send_json({"type": "tts.stop", "turn_id": str(turn), "reason": "done"})
        if rec.frames_sent == 0:
            await self._finish_done(rec, estimated=False)
            await self._recheck_turns()
            await self._schedule()
            return
        rid = rec.req.request_id
        remaining = sum(s.duration_ms for s in rec.sentences if not s.ended)
        # spec 02 §2.6 / §4: ended-receipt timeout = send complete + remaining + 2 s (= T_drain)
        keys = [s.key for s in rec.sentences]  # captured now: a retry resets rec.sentences
        self._arm(
            f"ended:{rid}",
            remaining + self.params.ended_receipt_extra_ms,
            lambda: self._on_ended_timeout(rec, keys),
        )
        if (
            self.receipt_mode == ReceiptMode.RECEIPTS
            and "started" in self.client_receipts
            and not any(s.started for s in rec.sentences)
        ):
            self._arm(
                f"started:{rid}",
                self.params.started_receipt_timeout_ms,
                lambda: self._on_started_timeout(rec),
            )
        await self._maybe_complete(rec)
        await self._recheck_turns()
        await self._schedule()

    # --------------------------------------------------------------- timeouts

    async def _on_ended_timeout(self, rec: Request, keys: list[tuple[TurnId, int]]) -> None:
        for key in keys:  # keys of the attempt that armed this timer, not of a later retry
            self._unplayed.discard(key)
            self._playing.discard(key)
        if rec.active:
            if self.receipt_mode == ReceiptMode.RECEIPTS:
                st = self._turn_stats(rec.turn_id)
                st.receipt_timeouts["ended"] += 1
                self._trace_write(rec.turn_id, {"receipt_timeouts": dict(st.receipt_timeouts)})
            for s in rec.sentences:
                if not s.ended:
                    s.ended = True
                    s.played_ms = s.duration_ms
                    rec.played_ms += s.duration_ms
            await self._finish_done(rec, estimated=True)
        await self._recheck_turns()
        await self._schedule()

    async def _on_started_timeout(self, rec: Request) -> None:
        if rec.status != RequestStatus.SENT:
            return
        st = self._turn_stats(rec.turn_id)
        st.receipt_timeouts["started"] += 1
        self._trace_write(rec.turn_id, {"receipt_timeouts": dict(st.receipt_timeouts)})
        if self._holder is rec:
            self._release_holder(rec)
        # treated as not delivered; its audio stays "unplayed" until the ended timeout so a retry
        # can never overlap with a device that is actually playing it (spec 02 §2.6 bullet 2)
        await self._interrupt(rec, CAUSE_RECEIPT_TIMEOUT, keep_unplayed=True)
        await self._recheck_turns()
        await self._schedule()

    # -------------------------------------------------------- turn bookkeeping

    def _turn_audio_done(self, turn: TurnId) -> bool:
        for r in self._by_id.values():
            if (r.turn_id == turn or r.req.turn_id == turn) and not r.terminal:
                return False
        return not any(k[0] == turn for k in self._unplayed)

    def _turn_granted(self, turn: TurnId) -> bool:
        return any(r.turn_id == turn and r.granted_at_ms is not None for r in self._by_id.values())

    async def _recheck_turns(self) -> None:
        for turn in list(self._reply_done):
            if not self._turn_audio_done(turn):
                continue
            if turn not in self._awaiting_tool:
                self._turn_pending.discard(turn)  # spec 02 §2.6 condition 2
            if turn in self._flushed_turns or turn in self._drained or not self._turn_granted(turn):
                continue
            self._drained.add(turn)
            await self.bus.emit("turn.audio_drained", turn_id=turn)  # spec 02 §2.9

    # ------------------------------------------------------------------ trace

    def _trace_turn(self, rec: Request) -> TurnId | None:
        return rec.turn_id or rec.req.turn_id or self._current_turn

    def _turn_stats(self, turn: TurnId | None) -> _TurnStats:
        if turn is None:
            return _TurnStats()
        st = self._stats.get(turn)
        if st is None:
            st = self._stats[turn] = _TurnStats()
            self._trace_write(turn, {"receipt_mode": self.receipt_mode.value, "queue_truncated": False})
        return st

    def _trace_write(self, turn: TurnId | None, fragment: dict[str, Any]) -> None:
        if turn is not None:
            self.trace.write(turn, fragment)

    def _trace_incr(self, turn: TurnId | None, counter: str) -> None:
        if turn is not None:
            self.trace.incr(turn, counter)

    # ----------------------------------------------------------------- timers

    def _arm(self, key: str, ms: int, fn: Callable[[], Awaitable[None]]) -> None:
        self._disarm(key)

        async def run() -> None:
            await self.clock.sleep(ms)
            self._timers.pop(key, None)
            await fn()

        self._timers[key] = asyncio.get_running_loop().create_task(run())

    def _disarm(self, key: str) -> None:
        t = self._timers.pop(key, None)
        if t is not None and not t.done():
            t.cancel()

    async def close(self) -> None:
        for key in list(self._timers):
            self._disarm(key)
        for r in self._by_id.values():
            if r.task is not None and not r.task.done():
                r.task.cancel()
