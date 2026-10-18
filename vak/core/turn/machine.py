"""Gateway-side turn authority for one session (spec 01 §2, §3, §5, §6.3).

Pipeline-independent: branches on `upstream.capabilities`, never on the
adapter name. Timestamps written to trace are absolute gateway clock ms
plus `t0_ms`; the trace collector subtracts T0 (spec 06 §2.2).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal, Protocol

from vak.core.bus import Bus
from vak.core.clock import Clock
from vak.core.defaults import EndpointingParams, TurnTimers
from vak.core.ids import IdGen
from vak.core.ports import ArbiterPort, ContextPort, CuePort, ToolPort, TracePort, TransportPort
from vak.core.turn.fsm import Turn, TurnEvent
from vak.core.turn.watchdog import WatchdogSet
from vak.core.types import (
    AbortReason,
    Aec,
    Anchor,
    ContentRole,
    ContentType,
    ErrorClass,
    ErrorCode,
    ErrorScope,
    IllegalTransition,
    InterruptSource,
    Kind,
    Mode,
    Origin,
    OutputContent,
    OutputRequest,
    PlaybackReceipt,
    Profile,
    TurnId,
    TurnInput,
    TurnSource,
    TurnState,
    Watchdog,
)
from vak.upstream.base import (
    Ack,
    AckCode,
    AudioMeta,
    EventType,
    ResponseOrigin,
    ResponseStatus,
    Upstream,
    UpstreamEvent,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local contracts (implemented by vak.audio / vak.transport to this shape)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EndpointEvent:
    kind: Literal["speech_start", "speech_end", "endpoint"]
    ts_samples: int
    prob: float | None = None


class EndpointerPort(Protocol):
    """Gateway VAD + semantic endpointing (spec 01 §2.3 open_mic row)."""

    def feed(self, pcm16: bytes, ts_samples: int, now_ms: int) -> list[EndpointEvent]: ...


@dataclass(slots=True)
class ClientCaps:
    """`hello.caps` (spec 01 §3.1.1). Defaults are the v1-client degradation."""

    aec: Aec = Aec.NONE
    modes: list[Mode] = field(default_factory=lambda: [Mode.PTT])
    sources: list[TurnSource] = field(default_factory=lambda: [TurnSource.BUTTON, TurnSource.TEXT])
    receipts: frozenset[str] = frozenset()
    play_buffer_max_ms: int = 1200
    preroll_ms: int = 0
    uninterruptible: bool = False
    audio_up: dict[str, Any] = field(
        default_factory=lambda: {"codec": "opus", "rate": 16000, "channels": 1, "frame_ms": 20}
    )
    audio_down: dict[str, Any] = field(default_factory=lambda: {"codecs": ["opus"], "rates": [16000]})

    @classmethod
    def from_hello(cls, caps: dict[str, Any] | None) -> ClientCaps:
        caps = caps or {}
        out = cls()
        if "aec" in caps:
            out.aec = Aec(caps["aec"])
        if "modes" in caps:
            out.modes = [Mode(m) for m in caps["modes"] if m in Mode._value2member_map_]
        if "sources" in caps:
            out.sources = [TurnSource(s) for s in caps["sources"] if s in TurnSource._value2member_map_]
        out.receipts = frozenset(caps.get("receipts") or ())
        out.play_buffer_max_ms = int(caps.get("play_buffer_max_ms", out.play_buffer_max_ms))
        out.preroll_ms = int(caps.get("preroll_ms", out.preroll_ms))
        out.uninterruptible = bool(caps.get("uninterruptible", False))
        if isinstance(caps.get("audio_up"), dict):
            out.audio_up = {**out.audio_up, **caps["audio_up"]}
        if isinstance(caps.get("audio_down"), dict):
            out.audio_down = {**out.audio_down, **caps["audio_down"]}
        return out

    @property
    def uplink_rate(self) -> int:
        return int(self.audio_up.get("rate", 16000))

    def has_receipt(self, kind: str) -> bool:
        return kind in self.receipts


@dataclass(slots=True)
class ResumeInfo:
    """What the transport tells the machine after a hello/resume handshake (spec 01 §6.2)."""

    ok: bool
    disconnected_at_ms: int


class _ProtocolError(Exception):
    def __init__(self, message: str, turn_id: str | None = None) -> None:
        super().__init__(message)
        self.turn_id = turn_id


_INTERRUPT_BY: dict[TurnSource, InterruptSource] = {
    TurnSource.BUTTON: InterruptSource.BUTTON,
    TurnSource.WAKE: InterruptSource.WAKE,
    TurnSource.VAD: InterruptSource.VAD,
    TurnSource.TEXT: InterruptSource.TEXT,
}

# spec 01 §2.5 lists T_hold, but spec 06 §3.9 `watchdog_fired` (types.Watchdog) does not:
# holding → committed is a normal transition, so the timer is keyed locally by name.
T_HOLD = "T_hold"

_PLAYING = (TurnState.RESPONDING, TurnState.DRAINING)
_PRE_COMMIT = (TurnState.LISTENING, TurnState.HOLDING)
_HARD_ACK_FAILURES = (AckCode.UPSTREAM_ERROR, AckCode.TIMEOUT)


class TurnMachine:
    """Authority for turn state, turn_id, watchdogs and the two modes."""

    def __init__(
        self,
        *,
        profile: Profile,
        mode: Mode,
        timers: TurnTimers,
        endpointing: EndpointingParams,
        clock: Clock,
        bus: Bus,
        transport: TransportPort,
        arbiter: ArbiterPort,
        upstream: Upstream,
        context: ContextPort,
        tools: ToolPort,
        trace: TracePort,
        ids: IdGen,
        endpointer: EndpointerPort | None,
        client_caps: ClientCaps,
        cues: CuePort | None = None,
    ) -> None:
        if mode is Mode.OPEN_MIC and client_caps.aec is Aec.NONE:
            raise ValueError("open_mic requires caps.aec != none")  # spec 01 §3.1.1
        self.profile = profile
        self._mode = mode
        self.timers = timers
        self.endpointing = endpointing
        self.clock = clock
        self.bus = bus
        self.transport = transport
        self.arbiter = arbiter
        self.upstream = upstream
        self.context = context
        self.tools = tools
        self.trace = trace
        self.ids = ids
        self.endpointer = endpointer
        self.client_caps = client_caps
        self.cues = cues

        self._turns: dict[str, Turn] = {}
        self._aliases: dict[str, str] = {}  # merged `from` → `into` (R2.6-4)
        self._by_response: dict[str, Turn] = {}
        self._current: Turn | None = None
        self._pending_interrupts: dict[str, Turn] = {}
        self._listen_open = mode is Mode.PTT  # ptt clients may omit listen.* (§3.1.3)
        self._session_start_ms = clock.now_ms()
        self._last_ts_samples = 0
        self._barge_candidate: tuple[Turn, int] | None = None
        self._endpointing_active = False
        self._event_ids: set[str] = set()
        self._closed = False

        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
            "listen.open": self._on_listen_open,
            "listen.close": self._on_listen_close,
            "turn.start": self._on_turn_start_msg,
            "turn.end": self._on_turn_end_msg,
            "abort": self._on_abort,
            "playback.started": self._on_playback,
            "playback.ended": self._on_playback,
            "playback.interrupted": self._on_playback,
            "event": self._on_event,
        }
        self._bus_subs: list[tuple[str, Callable[[dict[str, Any]], Awaitable[None]]]] = [
            ("turn.audio_drained", self._on_audio_drained),
            ("turn.no_output", self._on_no_output),
            ("turn.sound_first", self._on_sound_first),
            ("tool.call_closed", self._on_tool_call_closed),
        ]
        for name, handler in self._bus_subs:
            bus.on(name, handler)

    # ------------------------------------------------------------------ props

    @property
    def current_turn(self) -> Turn | None:
        return self._current

    @property
    def state(self) -> TurnState:
        return self._current.state if self._current is not None else TurnState.IDLE

    @property
    def mode(self) -> Mode:
        return self._mode

    def turn(self, turn_id: TurnId | str) -> Turn | None:
        key = str(turn_id)
        return self._turns.get(self._aliases.get(key, key))

    async def set_mode(self, mode: Mode) -> None:
        """Spec 01 rule 9 / R2.3-2: only between turns."""
        if mode is self._mode:
            return
        if self.state is not TurnState.IDLE:
            raise IllegalTransition(f"mode switch while {self.state.value}")
        if mode is Mode.OPEN_MIC and self.client_caps.aec is Aec.NONE:
            raise ValueError("open_mic requires caps.aec != none")
        self._mode = mode

    # ------------------------------------------------------------ client JSON

    async def handle_message(self, msg: dict[str, Any]) -> None:
        if self._closed:
            return
        handler = self._handlers.get(str(msg.get("type")))
        if handler is None:
            return  # unknown type: ignore (spec 01 rule 17)
        try:
            await handler(msg)
        except _ProtocolError as e:
            await self._send_error(e.turn_id, ErrorCode.PROTOCOL_ERROR, str(e))

    async def _on_listen_open(self, msg: dict[str, Any]) -> None:
        self._listen_open = True
        raw = msg.get("mode")
        if raw is None:
            return
        try:
            mode = Mode(raw)
        except ValueError:
            raise _ProtocolError(f"unknown mode {raw!r}") from None
        try:
            await self.set_mode(mode)
        except (IllegalTransition, ValueError) as e:
            raise _ProtocolError(str(e)) from None

    async def _on_listen_close(self, msg: dict[str, Any]) -> None:
        self._listen_open = False
        cur = self._current
        if cur is not None and cur.state in _PRE_COMMIT:
            await self._discard_input(cur)
            await self._close(cur, TurnEvent.CANCEL, "listen.close")  # spec 01 §2.4 cancelled

    async def _on_turn_start_msg(self, msg: dict[str, Any]) -> None:
        turn_id = self._require_turn_id(msg)
        try:
            source = TurnSource(msg.get("source"))
        except ValueError:
            raise _ProtocolError("turn.start needs source", str(turn_id)) from None
        if source is TurnSource.EVENT:
            raise _ProtocolError("source=event is downlink only", str(turn_id))
        if self.turn(turn_id) is not None:
            raise _ProtocolError("duplicate turn_id", str(turn_id))

        cur = self._current
        if cur is not None and not cur.terminal:
            if cur.state is TurnState.HOLDING and source is TurnSource.BUTTON:
                await self._merge(cur, turn_id)  # R2.6-4
                return
            if cur.state is TurnState.LISTENING and source is TurnSource.BUTTON and cur.source in (
                TurnSource.VAD,
                TurnSource.WAKE,
            ):
                # R2.3-1: button outranks vad/wake; same turn, client id aliased to ours
                cur.source = TurnSource.BUTTON
                cur.ts_samples_start = cur.ts_samples_start or msg.get("ts_samples")
                self._aliases[str(turn_id)] = str(cur.turn_id)
                await self._send({"type": "turn.merge", "from": str(turn_id), "into": str(cur.turn_id)})
                self.trace.write(cur.turn_id, {"turn_source": cur.source.value})  # spec 06 §3.2
                return

        text: str | None = None
        input_: TurnInput | None = None
        if source is TurnSource.TEXT:
            text = msg.get("text")
            if not isinstance(text, str):
                raise _ProtocolError("turn.start{text} needs text", str(turn_id))
            try:
                input_ = TurnInput(msg.get("input", "typed"))
            except ValueError:
                raise _ProtocolError("bad input", str(turn_id)) from None

        turn = await self._start_user_turn(turn_id, source, old=cur, ts_samples=msg.get("ts_samples"))
        turn.input = input_
        if text is not None:
            turn.text = text
            now = self.clock.now_ms()
            turn.t0_ms = now
            turn.ts_samples_end = turn.ts_samples_start
            self.arbiter.set_user_speaking(False)
            self.trace.write(turn.turn_id, {"turn_input": input_.value if input_ else None})  # spec 06 §3.2
            await self._commit(turn, TurnEvent.END_COMMIT, anchor=Anchor.TAP, anchor_ms=now)

    async def _on_turn_end_msg(self, msg: dict[str, Any]) -> None:
        turn_id = self._require_turn_id(msg)
        turn = self.turn(turn_id)
        if turn is None or turn.terminal or turn is not self._current or turn.state is not TurnState.LISTENING:
            return  # late or duplicate turn.end (R2.2-4)
        reason = msg.get("reason", "release")
        ts = msg.get("ts_samples")
        if isinstance(ts, int):
            turn.ts_samples_end = ts
            self._last_ts_samples = max(self._last_ts_samples, ts)
        self.arbiter.set_user_speaking(False)
        if reason == "cancel":
            await self._discard_input(turn)
            await self._close(turn, TurnEvent.CANCEL, "cancel")
            return
        await self._end_listening(turn, release_ms=self.clock.now_ms())

    async def _end_listening(self, turn: Turn, *, release_ms: int) -> None:
        """Key-up (or T_listen_max): short-press filter, holding or commit (spec 01 §2.1, §2.6b)."""
        now = release_ms
        if turn.speech_start_ts is not None or (self.endpointer is None and turn.audio_end_at_ms is None):
            # speech still running at key-up ends at the release (spec 06 §2.2 T_ae). Without a gateway
            # VAD (ptt) there is no last-voiced-frame measurement, so T_ae falls back to T_rel and the
            # tail silence counts as speech (latency.ptt_tail_silence_ms = 0).
            turn.audio_end_at_ms = now
            if turn.speech_start_ts is not None and turn.ts_samples_end is not None:
                turn.speech_ms += self._samples_to_ms(max(0, turn.ts_samples_end - turn.speech_start_ts))
                turn.speech_start_ts = None
        press_ms = self._press_ms(turn, now)
        self.trace.write(turn.turn_id, {"press_ms": press_ms})  # spec 06 §3.7
        if turn.source is TurnSource.BUTTON and press_ms < self.timers.short_press_ms:
            self.trace.write(turn.turn_id, {"dropped_short_press": True})  # spec 06 §3.7
            await self._discard(turn, "short_press")
            return
        if self.endpointer is not None and turn.speech_ms < self.timers.short_speech_ms:
            await self._discard(turn, "short_speech")
            return
        if (
            self._mode is Mode.PTT
            and turn.last_endpoint_prob is not None
            and turn.last_endpoint_prob < self.endpointing.semantic_threshold
        ):
            # spec 01 §2.1 holding: P(done) < τ → supplement window, T_hold is its only timer
            turn.apply(TurnEvent.END_HOLD)
            turn.t0_ms = now
            turn.watchdogs.cancel_all()
            turn.watchdogs.start(T_HOLD, min(self.timers.T_hold, self.timers.T_hold_max))
            self.trace.write(
                turn.turn_id, {"supplement_window_hit": False, "smart_turn_prob": turn.last_endpoint_prob}
            )  # spec 06 §3.7
            return
        turn.t0_ms = now
        await self._commit(turn, TurnEvent.END_COMMIT, anchor=Anchor.RELEASE, anchor_ms=now)

    async def _merge(self, cur: Turn, new_id: TurnId) -> None:
        self._aliases[str(new_id)] = str(cur.turn_id)
        cur.merged_from.append(new_id)
        cur.watchdogs.cancel_all()
        cur.apply(TurnEvent.MERGE)
        cur.t0_ms = None
        await self._send({"type": "turn.merge", "from": str(new_id), "into": str(cur.turn_id)})
        await self.arbiter.on_turn_start(cur.turn_id, TurnSource.BUTTON, merged=True)  # no +1 (02 §3.2)
        self.arbiter.set_user_speaking(True)
        self.trace.write(cur.turn_id, {"supplement_window_hit": True})  # spec 06 §3.7
        cur.watchdogs.start(Watchdog.T_LISTEN_MAX, self._t_listen_max())

    async def _on_abort(self, msg: dict[str, Any]) -> None:
        turn_id = self._require_turn_id(msg)
        try:
            reason = AbortReason(msg.get("reason", "button"))
        except ValueError:
            raise _ProtocolError("bad abort.reason", str(turn_id)) from None
        turn = self.turn(turn_id)
        if turn is None or turn.terminal:
            return  # late abort for a closed turn: nothing to flush (R2.2-4)
        if reason is AbortReason.USER_CANCEL:
            # spec 01 §3.1.5: user_cancel without a following turn.start → cancelled
            await self._interrupt(turn, InterruptSource.BUTTON, bump=True, terminal=TurnEvent.CANCEL, by="user_cancel")
        else:
            await self._interrupt(turn, InterruptSource(reason.value), bump=True)

    async def _on_playback(self, msg: dict[str, Any]) -> None:
        kind = str(msg["type"]).split(".", 1)[1]
        turn_id = self._require_turn_id(msg)
        if not isinstance(msg.get("sentence_id"), int) or not isinstance(msg.get("played_ms"), int):
            raise _ProtocolError("receipt needs sentence_id and played_ms", str(turn_id))
        turn = self.turn(turn_id)
        if turn is None:
            return
        receipt = PlaybackReceipt(
            kind=kind,  # type: ignore[arg-type]
            turn_id=turn.turn_id,
            sentence_id=msg["sentence_id"],
            played_ms=msg["played_ms"],
            ts_ms=int(msg.get("ts_ms", 0)),
            reason=msg.get("reason"),
        )
        turn.receipts.append(receipt)
        if kind == "ended" or kind == "interrupted":
            turn.heard_ms += receipt.played_ms
        if kind == "interrupted":
            turn.receipt_interrupted = True
        await self.arbiter.on_receipt(receipt)  # arbiter fills request_id from (turn_id, sentence_id)
        if kind == "interrupted" and str(turn.turn_id) in self._pending_interrupts:
            turn.watchdogs.cancel(Watchdog.T_INTR_RECEIPT)
            await self._finish_interrupt(turn)

    async def _on_event(self, msg: dict[str, Any]) -> None:
        ev_id = msg.get("id")
        if not isinstance(ev_id, str):
            raise _ProtocolError("event needs id")
        status = "duplicate" if ev_id in self._event_ids else "accepted"  # spec 01 §3.1.7 idempotent by id
        self._event_ids.add(ev_id)
        await self._send({"type": "event.ack", "id": ev_id, "status": status})

    # ----------------------------------------------------------- uplink audio

    async def handle_audio(self, turn_id_u32: int, seq: int, ts_samples: int, flags: int, pcm16: bytes) -> None:
        if self._closed:
            return
        now = self.clock.now_ms()
        n = len(pcm16) // 2
        self._last_ts_samples = ts_samples + n
        hint = TurnId.from_u32(turn_id_u32)
        if self._mode is Mode.PTT:
            cur = self._current
            if cur is None or cur.state not in _PRE_COMMIT:
                return  # audio outside a turn is not forwarded in ptt
            if hint is not None and self.turn(hint) is not cur:
                return  # frame of a closed turn (R2.2-4)
            if self.endpointer is not None:
                for ev in self.endpointer.feed(pcm16, ts_samples, now):
                    await self._on_endpoint_event(ev, ts_samples, n, now)
            target: Turn | None = cur
        else:
            if not self._listen_open:
                return
            if self.endpointer is not None:
                for ev in self.endpointer.feed(pcm16, ts_samples, now):
                    await self._on_endpoint_event(ev, ts_samples, n, now)
            await self._check_barge_candidate(ts_samples + n)
            cur = self._current
            # R2.2-5: attribution by ts_samples window, header turn_id is only a hint
            target = cur if cur is not None and cur.state in _PRE_COMMIT else None
        meta = AudioMeta(
            turn_id=target.turn_id if target is not None else None,
            seq=seq,
            sample_count=n,
            captured_at_ms=now,
            source=target.source.value if target is not None else self._mode.value,
            sample_rate=self.client_caps.uplink_rate,
        )
        if target is not None:
            target.seg_marks.setdefault("first_frame", now)
        await self.upstream.send_audio(pcm16, meta)

    async def _on_endpoint_event(self, ev: EndpointEvent, frame_ts: int, n: int, now: int) -> None:
        ev_ms = now - self._samples_to_ms(max(0, frame_ts + n - ev.ts_samples))
        cur = self._current
        if ev.kind == "speech_start":
            if cur is not None and not cur.terminal:
                cur.speech_start_ts = ev.ts_samples
            if self._mode is Mode.PTT:
                return
            if cur is None or cur.terminal:
                await self._start_vad_turn(ev.ts_samples)
                return
            if cur.state is TurnState.LISTENING:
                await self._set_endpointing(cur, False)
                self.arbiter.set_user_speaking(True)
                return
            # user speaks while agent is thinking/playing: barge-in candidate (spec 01 §5.2 filter)
            if now - self._session_start_ms < self.timers.aec_warmup_ms:
                cur.false_interrupt_filtered += 1
                self.trace.write(cur.turn_id, {"false_interrupt_filtered": cur.false_interrupt_filtered})
                return
            self._barge_candidate = (cur, ev.ts_samples)
        elif ev.kind == "speech_end":
            if cur is not None and not cur.terminal and cur.speech_start_ts is not None:
                cur.speech_ms += self._samples_to_ms(max(0, ev.ts_samples - cur.speech_start_ts))
                cur.speech_start_ts = None
                cur.audio_end_at_ms = ev_ms
            if self._barge_candidate is not None:
                cand, _ = self._barge_candidate
                self._barge_candidate = None
                cand.false_interrupt_filtered += 1  # shorter than barge_in_min_ms
                self.trace.write(cand.turn_id, {"false_interrupt_filtered": cand.false_interrupt_filtered})
            if self._mode is Mode.PTT:
                return
            if cur is not None and cur.state is TurnState.LISTENING:
                self.arbiter.set_user_speaking(False)
                await self._set_endpointing(cur, True)  # R2.3-4
        else:  # endpoint
            if cur is None or cur.terminal:
                return
            cur.last_endpoint_prob = ev.prob
            if self._mode is Mode.PTT or cur.state is not TurnState.LISTENING:
                return
            await self._set_endpointing(cur, False)
            self.arbiter.set_user_speaking(False)
            cur.ts_samples_end = ev.ts_samples
            if cur.speech_ms < self.timers.short_speech_ms:
                await self._discard(cur, "short_speech")
                return
            cur.t0_ms = ev_ms
            if ev.prob is not None:
                self.trace.write(cur.turn_id, {"smart_turn_prob": ev.prob})
            await self._send(
                {
                    "type": "turn.end",
                    "turn_id": str(cur.turn_id),
                    "source": "vad",
                    "ts_samples": ev.ts_samples,
                    "reason": "endpoint",
                }
            )
            await self._commit(cur, TurnEvent.END_COMMIT, anchor=Anchor.ENDPOINT, anchor_ms=ev_ms)

    async def _check_barge_candidate(self, ts_end: int) -> None:
        if self._barge_candidate is None:
            return
        old, start_ts = self._barge_candidate
        if old.terminal:
            self._barge_candidate = None
            return
        if self._samples_to_ms(ts_end - start_ts) < self.timers.barge_in_min_ms:
            return
        self._barge_candidate = None
        new = await self._start_vad_turn(start_ts, old=old)
        new.speech_start_ts = start_ts

    async def _start_vad_turn(self, ts_samples: int, old: Turn | None = None) -> Turn:
        turn_id = TurnId("s", self.ids.new_turn_n())
        turn = await self._start_user_turn(turn_id, TurnSource.VAD, old=old, ts_samples=ts_samples, announce=True)
        turn.speech_start_ts = ts_samples  # speech is already running when the turn is born
        return turn

    # ------------------------------------------------------- upstream events

    async def on_upstream_event(self, ev: UpstreamEvent) -> None:
        if self._closed:
            return
        t = ev.type
        if t in (EventType.RESPONSE_TEXT_DELTA, EventType.RESPONSE_SENTENCE, EventType.RESPONSE_AUDIO_DELTA):
            self._note_segments(ev)  # spec 06 §2.4
        if t is EventType.TRANSCRIPT_USER_PARTIAL:
            await self._on_transcript(ev, final=False)
        elif t is EventType.TRANSCRIPT_USER_FINAL:
            await self._on_transcript(ev, final=True)
        elif t is EventType.INPUT_SPEECH_STARTED:
            # R2.1-2 / rule 6: ptt only trusts the button. open_mic without a local
            # endpointer (full duplex) takes the adapter's synthesised VAD instead.
            if self._mode is Mode.OPEN_MIC and self.endpointer is None:
                await self._on_endpoint_event(
                    EndpointEvent("speech_start", self._last_ts_samples), self._last_ts_samples, 0, self.clock.now_ms()
                )
        elif t is EventType.INPUT_SPEECH_STOPPED:
            if self._mode is Mode.OPEN_MIC and self.endpointer is None:
                now = self.clock.now_ms()
                await self._on_endpoint_event(EndpointEvent("speech_end", self._last_ts_samples), self._last_ts_samples, 0, now)
                await self._on_endpoint_event(EndpointEvent("endpoint", self._last_ts_samples), self._last_ts_samples, 0, now)
        elif t is EventType.INPUT_SEGMENTED:
            turn = self._turn_for(ev)
            if turn is not None and not turn.terminal:
                turn.segments += 1  # R2.6-1: a segment is not a new turn
        elif t is EventType.RESPONSE_STARTED:
            await self._on_response_started(ev)
        elif t is EventType.RESPONSE_AUDIO_DELTA:
            await self._on_audio_delta(ev)
        elif t in (EventType.RESPONSE_TEXT_DELTA, EventType.RESPONSE_SENTENCE, EventType.TRANSCRIPT_ASSISTANT):
            turn = self._by_response.get(ev.response_id or "") or self._turn_for(ev)
            if turn is None or turn.terminal or turn.request_id is None or not ev.text:
                return
            self.context.record_assistant_pending(
                turn.turn_id, turn.request_id, ev.sentence_index or 0, ev.text, source="model"
            )
            if t is not EventType.RESPONSE_TEXT_DELTA and hasattr(self.arbiter, "note_item_text"):
                self.arbiter.note_item_text(ev.response_id, ev.item_id, ev.text)
        elif t is EventType.RESPONSE_DONE:
            await self._on_response_done(ev)
        elif t is EventType.ERROR:
            await self._on_upstream_error(ev)

    # -- spec 06 §2.4 segment timing ------------------------------------------

    def _seg(self, turn: Turn, name: str, value: int) -> None:
        """Write `latency.seg.<name>` once per turn."""
        if name in turn.seg or value < 0:
            return
        turn.seg[name] = value
        self.trace.write(turn.turn_id, {"latency": {"seg": {name: value}}})

    def _note_segments(self, ev: UpstreamEvent) -> None:
        """seg.llm_ms / first_segment_ms / tts_start_ms from the first requested
        response's event timestamps (stamped by the adapter's clock)."""
        turn = self._by_response.get(ev.response_id or "") or self._turn_for(ev)
        if turn is None or turn.seg_response is None or ev.response_id != turn.seg_response:
            return
        marks, at = turn.seg_marks, ev.t_local_ms
        t = ev.type
        if t is EventType.RESPONSE_TEXT_DELTA and ev.text and "first_text" not in marks:
            marks["first_text"] = at
            self._seg(turn, "llm_ms", at - marks["request"])
        elif t is EventType.RESPONSE_SENTENCE and ev.submitted_at_ms is not None and "first_text" in marks:
            marks.setdefault("first_submit", ev.submitted_at_ms)
            self._seg(turn, "first_segment_ms", ev.submitted_at_ms - marks["first_text"])
        elif t is EventType.RESPONSE_AUDIO_DELTA and "first_audio" not in marks:
            marks["first_audio"] = at
            self._seg(turn, "llm_ms", at - marks["request"])  # S2S: the first audio delta is the first output
            if "first_submit" in marks:  # cascade route only (spec 06 §2.4: omitted for S2S)
                self._seg(turn, "tts_start_ms", at - marks["first_text"])

    def _turn_for(self, ev: UpstreamEvent) -> Turn | None:
        if ev.turn_id is not None:
            turn = self.turn(ev.turn_id)
            if turn is not None:
                return turn
        return self._current

    async def _on_transcript(self, ev: UpstreamEvent, *, final: bool) -> None:
        turn = self._turn_for(ev)
        if turn is None or turn.terminal:
            return  # late transcript for a closed turn is dropped (R2.2-4)
        text = ev.text or ""
        if text.strip() and "first_frame" in turn.seg_marks:
            self._seg(turn, "stt_first_word_ms", ev.t_local_ms - turn.seg_marks["first_frame"])
        frame: dict[str, Any] = {"type": "stt", "turn_id": str(turn.turn_id), "text": text, "final": final}
        if not final:
            frame["segment"] = ev.segment_index if ev.segment_index is not None else turn.segments - 1
            await self._send(frame)
            return
        await self._send(frame)
        now = self.clock.now_ms()
        if not turn.stt_final:
            turn.stt_final = True
            turn.watchdogs.cancel(Watchdog.T_ASR)
            if not turn.speech_first and turn.state is TurnState.COMMITTED:
                turn.watchdogs.start(Watchdog.T_RESPOND, self.timers.T_respond)  # 定稿 → first speech
            self.trace.write(turn.turn_id, {"latency": {"stt_final_at_ms": now}})  # spec 06 §3.4
        if text.strip():
            self.context.record_user(
                turn.turn_id, text, turn.created_at_ms, turn.t0_ms if turn.t0_ms is not None else now, source="user_voice"
            )
        elif turn.state is TurnState.COMMITTED:
            await self._fail(turn, ErrorCode.NO_SPEECH, "empty transcript")  # spec 01 §8 item 4

    async def _on_response_started(self, ev: UpstreamEvent) -> None:
        turn = self._turn_for(ev)
        if turn is None or turn.terminal or turn.state in _PRE_COMMIT:
            # R2.6-2 / rule 8: a reply the upstream started on its own before commit is
            # cancelled and dropped by generation; counted as a server-side segment.
            if ev.response_id:
                await self.upstream.cancel_response(ev.response_id)
            if turn is not None and not turn.terminal and ev.origin is not ResponseOrigin.CLIENT:
                turn.server_vad_segments += 1
                self.trace.write(turn.turn_id, {"server_vad_segments_in_turn": turn.server_vad_segments})  # 06 §3.7
            return
        if ev.response_id is None:
            return
        turn.response_id = ev.response_id
        self._by_response[ev.response_id] = turn
        if turn.awaiting_tool:
            turn.awaiting_tool = False
            self.arbiter.set_awaiting_tool(turn.turn_id, False)
            await self.bus.emit("turn.awaiting_tool", turn_id=turn.turn_id, awaiting=False)
            if not turn.speech_first:
                turn.watchdogs.start(Watchdog.T_RESPOND, self.timers.T_respond)
        req = OutputRequest(
            request_id=self.ids.new("req"),
            origin=Origin.MODEL,
            kind=Kind.SPEECH,
            content=OutputContent(
                type=ContentType.UPSTREAM_STREAM,
                response_id=ev.response_id,
                item_id=ev.item_id,
                sample_rate=ev.sample_rate,
            ),
            generation=turn.generation,
            turn_id=turn.turn_id,
            content_role=ContentRole.ANSWER,
        )
        turn.request_id = req.request_id
        await self.arbiter.submit(req)

    async def _on_audio_delta(self, ev: UpstreamEvent) -> None:
        turn = self._by_response.get(ev.response_id or "")
        if turn is None or turn.terminal or ev.audio is None:
            return  # late delta for a closed turn: never reaches the wire (R2.2-4)
        request_id = self.arbiter.request_for_response(ev.response_id or "")
        if request_id is None:
            return
        await self.arbiter.feed_upstream_audio(
            request_id=request_id,
            pcm16=ev.audio,
            sample_rate=ev.sample_rate,
            response_id=ev.response_id or "",
            item_id=ev.item_id or "",
            generation=turn.generation,
        )
        turn.audio_sent_ms += (len(ev.audio) // 2) * 1000 // max(1, ev.sample_rate)
        await self._mark_sound(turn, Kind.SPEECH)

    async def _on_response_done(self, ev: UpstreamEvent) -> None:
        turn = self._by_response.get(ev.response_id or "") or self._turn_for(ev)
        request_id = self.arbiter.request_for_response(ev.response_id or "") if ev.response_id else None
        if request_id is not None:
            await self.arbiter.end_upstream_stream(request_id)
        if turn is None or turn.terminal or turn.response_id != ev.response_id:
            return
        if self.tools.has_open_calls(turn.turn_id):
            turn.awaiting_tool = True
            turn.watchdogs.cancel(Watchdog.T_RESPOND)  # tool layer owns the timeout meanwhile (03 §4.1)
            self.arbiter.set_awaiting_tool(turn.turn_id, True)
            await self.bus.emit("turn.awaiting_tool", turn_id=turn.turn_id, awaiting=True)
            return
        await self._finish_reply(turn, ev.status or ResponseStatus.COMPLETED)

    async def _finish_reply(self, turn: Turn, status: ResponseStatus) -> None:
        await self._reply_done(turn, status.value)
        if status is ResponseStatus.FAILED:
            await self._fail(turn, ErrorCode.UPSTREAM_REJECTED, "response failed")
            return
        if turn.state is TurnState.RESPONDING:
            turn.apply(TurnEvent.REPLY_DONE)
            if turn.drained:
                await self._close(turn, TurnEvent.DRAINED, "playback_ended")
            else:
                turn.watchdogs.start(Watchdog.T_DRAIN, self._t_drain(turn))
        # still committed (no audio): arbiter will report turn.no_output

    async def _on_tool_call_closed(self, payload: dict[str, Any]) -> None:
        """Spec 03: routes without regeneration (A1 / A3 / silent / interrupted) end the
        tool phase here; A0 ends it when the regenerated response starts."""
        turn = self.turn(payload["turn_id"])
        if turn is None or turn.terminal or not turn.awaiting_tool:
            return
        if self.tools.has_open_calls(turn.turn_id):
            return
        calls = getattr(self.tools, "calls", {})
        regen_pending = any(
            c.turn_id == turn.turn_id and c.reply_required and c.output_sent for c in calls.values()
        )
        if regen_pending:
            if not turn.speech_first:
                turn.watchdogs.start(Watchdog.T_RESPOND, self.timers.T_respond)  # the regeneration must arrive
            return
        turn.awaiting_tool = False
        self.arbiter.set_awaiting_tool(turn.turn_id, False)
        await self.bus.emit("turn.awaiting_tool", turn_id=turn.turn_id, awaiting=False)
        await self._finish_reply(turn, ResponseStatus.COMPLETED)

    async def _on_upstream_error(self, ev: UpstreamEvent) -> None:
        err = ev.error
        turn = self._by_response.get(ev.response_id or "") or self._current
        if err is None or err.error_class is ErrorClass.BENIGN or turn is None or turn.terminal:
            return
        code = ErrorCode.UPSTREAM_UNAVAILABLE if err.scope is ErrorScope.SESSION else ErrorCode.UPSTREAM_REJECTED
        await self._fail(turn, code, err.raw_message or err.reason.value)

    # ------------------------------------------------------------- bus events

    async def _on_audio_drained(self, payload: dict[str, Any]) -> None:
        turn = self.turn(payload["turn_id"])
        if turn is None or turn.terminal:
            return
        turn.drained = True
        if turn.state in _PLAYING:
            await self._close(turn, TurnEvent.DRAINED, "playback_ended")

    async def _on_no_output(self, payload: dict[str, Any]) -> None:
        turn = self.turn(payload["turn_id"])
        if turn is None or turn.terminal or turn.awaiting_tool:
            return
        if turn.state is TurnState.COMMITTED and turn.reply_done:
            # reply finished without a single authorised output (spec 02 §4.1 turn.no_output)
            await self._fail(turn, ErrorCode.UPSTREAM_REJECTED, "no output")

    async def _on_sound_first(self, payload: dict[str, Any]) -> None:
        turn = self.turn(payload["turn_id"])
        if turn is None or turn.terminal:
            return
        kind = payload.get("kind", Kind.SPEECH)
        await self._mark_sound(turn, Kind(kind) if not isinstance(kind, Kind) else kind)

    # ------------------------------------------------------ event / narration

    async def start_event_turn(self, kind: str) -> TurnId:
        """turn.start{source:"event"} (R2.2-6): gateway id, starts at committed, no +1 (02 §3.2)."""
        if self._current is not None and not self._current.terminal:
            raise IllegalTransition(f"event turn while {self._current.state.value}")
        now = self.clock.now_ms()
        turn_id = TurnId("s", self.ids.new_turn_n())
        turn = self._new_turn(turn_id, TurnSource.EVENT, now)
        turn.generation = self.arbiter.generation
        turn.apply(TurnEvent.START_EVENT)
        turn.t0_ms = now
        turn.committed_at_ms = now
        self._current = turn
        await self._send({"type": "turn.start", "turn_id": str(turn_id), "source": "event", "kind": kind})
        self.trace.write(
            turn.turn_id,
            {
                "turn_source": "event",
                "generation": turn.generation,
                "watchdog_fired": Watchdog.NONE.value,
                "t0_ms": now,
                "anchors": {"primary": Anchor.EVENT.value, "event_at_ms": now},
            },
        )
        await self.bus.emit("turn.start", turn_id=turn_id, source=TurnSource.EVENT, generation=turn.generation)
        await self.bus.emit("turn.committed", turn_id=turn_id)
        turn.watchdogs.start(Watchdog.T_RESPOND, self.timers.T_respond)
        return turn_id

    # ------------------------------------------------------- resume / session

    async def on_resume(self, info: ResumeInfo) -> list[dict[str, Any]]:
        """Per-turn consequences of a reconnect (spec 01 §6.3)."""
        if not info.ok:
            await self.close_session("session_expired")
            return []
        # holding / committed row: terminal reached during the gap → resend error + turn.closed
        for turn in list(self._turns.values()):
            if turn.closed_at_ms is not None and turn.closed_at_ms >= info.disconnected_at_ms:
                for frame in turn.closed_frames:
                    await self._send(frame)
        cur = self._current
        if cur is not None and cur.state in _PLAYING:
            # responding / draining row: audio already sent is stale → interrupted{by:link}
            await self._interrupt(cur, InterruptSource.LINK, bump=True)
        return [
            {"turn_id": str(t.turn_id), "state": t.state.value} for t in self._turns.values() if not t.terminal
        ]

    async def on_uplink_gap(self, gap_ms: int) -> None:
        """Reconnect gap rule (spec 01 §4.5); the gap itself is measured by the transport."""
        cur = self._current
        if cur is None or cur.terminal or gap_ms <= self.timers.plc_gap_max_ms:
            return
        if gap_ms <= self.timers.damaged_gap_max_ms or cur.state is not TurnState.LISTENING:
            self.trace.write(cur.turn_id, {"audio_damaged": True})  # spec 06 §3.9
            return
        await self._fail(cur, ErrorCode.AUDIO_GAP, f"uplink gap {gap_ms} ms")

    async def close_session(self, reason: str) -> None:
        """R2.4-1: outside the resume window every open turn is failed{link_lost} via
        session.closed (no per-turn frames); any other reason cancels them normally."""
        expired = reason in ("session_expired", "link_lost")
        for turn in list(self._turns.values()):
            if turn.terminal and not turn.close_sent:
                await self._finish_interrupt(turn, send_frames=not expired)
                continue
            if turn.terminal:
                continue
            if expired:
                turn.apply(TurnEvent.FAIL)
                await self._emit_closed(turn, "link_lost", send_frames=False)
            else:
                await self._discard_input(turn)
                await self._close(turn, TurnEvent.CANCEL, reason)
        self._closed = True
        for name, handler in self._bus_subs:
            self.bus.off(name, handler)

    # ------------------------------------------------------------- internals

    def _new_turn(self, turn_id: TurnId, source: TurnSource, now: int) -> Turn:
        turn = Turn(
            turn_id=turn_id,
            source=source,
            watchdogs=WatchdogSet(self.clock, partial(self._on_watchdog_for, str(turn_id))),
            created_at_ms=now,
        )
        self._turns[str(turn_id)] = turn
        return turn

    async def _start_user_turn(
        self,
        turn_id: TurnId,
        source: TurnSource,
        *,
        old: Turn | None,
        ts_samples: int | None,
        announce: bool = False,
    ) -> Turn:
        now = self.clock.now_ms()
        turn = self._new_turn(turn_id, source, now)
        turn.ts_samples_start = ts_samples if isinstance(ts_samples, int) else None
        # one +1 for the new turn (spec 02 §3.2); the arbiter flushes the old turn here
        gen = await self.arbiter.on_turn_start(turn_id, source)
        turn.generation = gen
        if announce:
            await self._send({"type": "turn.start", "turn_id": str(turn_id), "source": source.value, "ts_samples": ts_samples})
        if old is not None and not old.terminal:
            await self._interrupt(old, _INTERRUPT_BY[source], bump=False)  # R2.1-1
        turn.apply(TurnEvent.START)
        self._current = turn
        self.arbiter.set_user_speaking(True)
        self.trace.write(
            turn.turn_id,
            {"turn_source": source.value, "generation": gen, "watchdog_fired": Watchdog.NONE.value},
        )
        await self.bus.emit("turn.start", turn_id=turn_id, source=source, generation=gen)
        turn.watchdogs.start(Watchdog.T_LISTEN_MAX, self._t_listen_max())
        return turn

    async def _commit(self, turn: Turn, event: TurnEvent, *, anchor: Anchor | None = None, anchor_ms: int | None = None) -> None:
        turn.apply(event)
        now = self.clock.now_ms()
        turn.committed_at_ms = now
        if turn.t0_ms is None:
            turn.t0_ms = now
        turn.watchdogs.cancel_all()
        turn.watchdogs.start(Watchdog.T_FILLER, self._t_filler())
        if self._expects_transcript():
            turn.watchdogs.start(Watchdog.T_ASR, self.timers.T_asr)
        else:
            turn.watchdogs.start(Watchdog.T_RESPOND, self.timers.T_respond)
        anchors: dict[str, Any] = {}
        if anchor is not None:
            anchors["primary"] = anchor.value
            anchors[f"{anchor.value}_at_ms"] = anchor_ms if anchor_ms is not None else turn.t0_ms
        if turn.audio_end_at_ms is not None:
            anchors["audio_end_at_ms"] = turn.audio_end_at_ms
        frag: dict[str, Any] = {"t0_ms": turn.t0_ms, "speech_ms": turn.speech_ms}  # spec 06 §2.2, §3.7
        if anchors:
            frag["anchors"] = anchors
        self.trace.write(turn.turn_id, frag)
        await self.bus.emit("turn.committed", turn_id=turn.turn_id)
        if turn.text is not None:
            self.context.record_user(turn.turn_id, turn.text, now, now, source="user_ui")
            ack = await self.upstream.send_text(turn.turn_id, turn.text)
        else:
            ack = await self.upstream.commit_turn(turn.turn_id, "commit")
        if self._hard_failure(ack):
            await self._fail(turn, ErrorCode.UPSTREAM_UNAVAILABLE, ack.detail or "commit failed")
            return
        if self.upstream.capabilities.get("response.client_initiated") == "yes":
            requested = self.clock.now_ms()
            ack = await self.upstream.request_response(turn.turn_id, self.ids.new("resp"), None)
            if self._hard_failure(ack):
                await self._fail(turn, ErrorCode.UPSTREAM_UNAVAILABLE, ack.detail or "request_response failed")
                return
            turn.seg_marks["request"] = requested
            turn.seg_response = (ack.data or {}).get("response_id")
            if turn.audio_end_at_ms is not None:
                self._seg(turn, "vad_ms", requested - turn.audio_end_at_ms)

    async def _mark_sound(self, turn: Turn, kind: Kind) -> None:
        if turn.terminal:
            return
        if not turn.sound_first:
            turn.sound_first = True
            turn.watchdogs.cancel(Watchdog.T_FILLER)
        if kind is not Kind.CUE and not turn.speech_first:
            turn.speech_first = True
            turn.watchdogs.cancel(Watchdog.T_RESPOND)
        if turn.state is TurnState.COMMITTED:
            turn.apply(TurnEvent.SOUND_FIRST)

    async def _reply_done(self, turn: Turn, outcome: str) -> None:
        if turn.reply_done:
            return
        turn.reply_done = True
        await self.arbiter.on_reply_done(turn.turn_id, outcome)
        await self.bus.emit("turn.reply_done", turn_id=turn.turn_id, outcome=outcome)

    async def _interrupt(
        self,
        turn: Turn,
        source: InterruptSource,
        *,
        bump: bool,
        terminal: TurnEvent = TurnEvent.INTERRUPT,
        by: str | None = None,
    ) -> None:
        """Spec 01 §5: flush (via arbiter) → close. `bump` is False when a new
        turn.start already took the single +1 (spec 02 §3.2)."""
        if turn.terminal:
            return
        now = self.clock.now_ms()
        turn.was_playing = turn.state in _PLAYING
        turn.interrupt_source = source
        turn.interrupt_at_ms = now
        turn.watchdogs.cancel_all()
        if bump:
            gen = await self.arbiter.on_barge_in(turn.turn_id, source)
            await self.bus.emit("user.barge_in", turn_id=turn.turn_id, source=source, generation=gen)
        if turn.state in _PRE_COMMIT:
            await self._discard_input(turn)
        turn.apply(terminal)
        turn.closed_by = by or source.value
        if self._current is turn:
            self._current = None
        if turn.was_playing and not turn.receipt_interrupted and self.client_caps.has_receipt("interrupted"):
            # wait for playback.interrupted up to T_intr_receipt (spec 01 §2.5, §5.2)
            self._pending_interrupts[str(turn.turn_id)] = turn
            turn.watchdogs.start(Watchdog.T_INTR_RECEIPT, self.timers.T_intr_receipt)
            return
        if turn.was_playing and not turn.receipt_interrupted:
            turn.truncate_estimated = True  # spec 01 §5.5, 06 §3.8
        await self._finish_interrupt(turn)

    async def _finish_interrupt(self, turn: Turn, *, send_frames: bool = True) -> None:
        if turn.close_sent:
            return
        self._pending_interrupts.pop(str(turn.turn_id), None)
        if turn.was_playing and send_frames:
            frame = {"type": "tts.stop", "turn_id": str(turn.turn_id), "reason": "interrupted"}
            turn.closed_frames.append(frame)
            await self._send(frame)
        await self._emit_closed(turn, turn.closed_by or "interrupted", send_frames=send_frames)

    async def _discard_input(self, turn: Turn) -> None:
        if turn.state in _PRE_COMMIT:
            await self.upstream.commit_turn(turn.turn_id, "discard")
        elif turn.response_id is not None and not turn.reply_done:
            await self.upstream.cancel_response(turn.response_id)

    async def _discard(self, turn: Turn, reason: str) -> None:
        """Zero output (spec 01 §2.4 discarded)."""
        await self._discard_input(turn)
        await self._close(turn, TurnEvent.DISCARD, reason)

    async def _fail(self, turn: Turn, code: ErrorCode, message: str) -> None:
        """Spec 01 §3.2.4: error then turn.closed{failed}, two frames back to back."""
        if turn.terminal:
            return
        await self._discard_input(turn)
        frame = {
            "type": "error",
            "turn_id": str(turn.turn_id),
            "code": code.value,
            "fatal": False,
            "message": message,
            "retry_after_ms": None,
        }
        turn.closed_frames.append(frame)
        await self._send(frame)
        await self._close(turn, TurnEvent.FAIL, code.value)

    async def _close(self, turn: Turn, event: TurnEvent, by: str, extra: dict[str, Any] | None = None) -> None:
        if turn.terminal:
            return
        turn.apply(event)
        await self._emit_closed(turn, by, extra=extra)

    async def _emit_closed(
        self, turn: Turn, by: str, *, send_frames: bool = True, extra: dict[str, Any] | None = None
    ) -> None:
        now = self.clock.now_ms()
        turn.closed_by = by
        turn.closed_at_ms = now
        turn.close_sent = True
        turn.watchdogs.cancel_all()
        self._pending_interrupts.pop(str(turn.turn_id), None)
        if self._barge_candidate is not None and self._barge_candidate[0] is turn:
            self._barge_candidate = None
        if self._current is turn:
            self._current = None
            if self._endpointing_active:
                await self._set_endpointing(turn, False)
            self.arbiter.set_user_speaking(False)
        if turn.state in (TurnState.FAILED, TurnState.CANCELLED, TurnState.DISCARDED):
            await self._reply_done(turn, turn.state.value)
        frag: dict[str, Any] = {  # spec 06 §3.7–3.9
            "turn_terminal": turn.state.value,
            "turn_closed_by": by,
            "heard_ms": turn.heard_ms,
            "interrupted": turn.interrupt_source is not None,
            "generation": turn.generation,
        }
        if turn.interrupt_source is not None:
            frag["interrupt_source"] = turn.interrupt_source.value
            frag["interrupt_at_ms"] = turn.interrupt_at_ms
            frag["truncate_estimated"] = turn.truncate_estimated
        if turn.state is TurnState.FAILED:
            frag["error_code"] = by
        self.trace.write(turn.turn_id, frag)
        frame: dict[str, Any] = {
            "type": "turn.closed",
            "turn_id": str(turn.turn_id),
            "state": turn.state.value,
            "by": by,
            "heard_ms": turn.heard_ms,
            "segments": turn.segments,
        }
        if extra:
            frame.update(extra)
        turn.closed_frames.append(frame)
        if send_frames:
            await self._send(frame)
        await self.bus.emit("turn.closed", turn_id=turn.turn_id, state=turn.state, by=by)
        await self.tools.on_turn_closed(turn.turn_id)

    async def _on_watchdog_for(self, key: str, which: str) -> None:
        turn = self._turns.get(key)
        if turn is None:
            return
        await self._on_watchdog(turn, which)

    async def _on_watchdog(self, turn: Turn, which: str) -> None:
        """The single expiry path (spec 01 §2.5, §7 notes)."""
        if turn.close_sent:
            return
        if which != T_HOLD:
            self.trace.write(turn.turn_id, {"watchdog_fired": Watchdog(which).value})  # spec 06 §3.9
        if which is Watchdog.T_INTR_RECEIPT:
            turn.truncate_estimated = True
            await self._finish_interrupt(turn)
            return
        if turn.terminal:
            return
        if which is Watchdog.T_LISTEN_MAX:
            now = self.clock.now_ms()
            self.arbiter.set_user_speaking(False)
            turn.ts_samples_end = self._last_ts_samples
            await self._send(
                {
                    "type": "turn.end",
                    "turn_id": str(turn.turn_id),
                    "source": turn.source.value,
                    "ts_samples": self._last_ts_samples,
                    "reason": "max_duration",
                }
            )
            if self._endpointing_active:
                await self._set_endpointing(turn, False)
            turn.t0_ms = now
            anchor = Anchor.RELEASE if turn.source is TurnSource.BUTTON else Anchor.ENDPOINT
            await self._commit(turn, TurnEvent.END_COMMIT, anchor=anchor, anchor_ms=now)
        elif which == T_HOLD:
            await self._commit(turn, TurnEvent.HOLD_EXPIRE, anchor=Anchor.RELEASE, anchor_ms=turn.t0_ms)
        elif which is Watchdog.T_ASR:
            await self._fail(turn, ErrorCode.ASR_TIMEOUT, "recognition timed out")
        elif which is Watchdog.T_FILLER:
            await self._insert_filler(turn)
        elif which is Watchdog.T_RESPOND:
            await self._fail(turn, ErrorCode.UPSTREAM_TIMEOUT, "upstream did not reply")
        elif which is Watchdog.T_DRAIN:
            if turn.state is TurnState.RESPONDING:
                turn.apply(TurnEvent.REPLY_DONE)
            await self._close(turn, TurnEvent.DRAINED, "T_drain", extra={"receipt_missing": True})

    async def _insert_filler(self, turn: Turn) -> None:
        """T_filler: a cue, no state change (spec 01 §2.5). Needs a cue library."""
        if self.cues is None:
            return
        cue = self.cues.get("filler")
        if cue is None:
            return
        pcm16, rate, duration_ms = cue
        await self.arbiter.submit(
            OutputRequest(
                request_id=self.ids.new("req"),
                origin=Origin.CUE,
                kind=Kind.CUE,
                content=OutputContent(type=ContentType.PRERECORDED, audio=pcm16, sample_rate=rate, duration_ms=duration_ms),
                generation=turn.generation,
                turn_id=turn.turn_id,
                content_role=ContentRole.FILLER,
                dedupe_key=f"filler:{turn.turn_id}",
            )
        )

    async def _set_endpointing(self, turn: Turn, active: bool) -> None:
        if self._endpointing_active == active:
            return
        self._endpointing_active = active
        self.arbiter.set_endpointing(turn.turn_id, active)
        await self.bus.emit("turn.endpointing", turn_id=turn.turn_id, active=active)  # R2.3-4

    async def _send(self, msg: dict[str, Any]) -> None:
        await self.transport.send_json(msg)

    async def _send_error(self, turn_id: str | None, code: ErrorCode, message: str) -> None:
        await self._send(
            {"type": "error", "turn_id": turn_id, "code": code.value, "fatal": False, "message": message, "retry_after_ms": None}
        )

    @staticmethod
    def _require_turn_id(msg: dict[str, Any]) -> TurnId:
        raw = msg.get("turn_id")
        if not isinstance(raw, str):
            raise _ProtocolError("missing turn_id")
        try:
            return TurnId.parse(raw)
        except ValueError:
            raise _ProtocolError(f"bad turn_id {raw!r}") from None

    def _expects_transcript(self) -> bool:
        # T_asr only makes sense when the upstream echoes a user transcript (spec 01 §2.5)
        return self.upstream.capabilities.get("cascade.user_transcript") != "none"

    @staticmethod
    def _hard_failure(ack: Ack) -> bool:
        return not ack.ok and ack.code in _HARD_ACK_FAILURES

    def _press_ms(self, turn: Turn, now: int) -> int:
        if turn.ts_samples_start is not None and turn.ts_samples_end is not None:
            return self._samples_to_ms(max(0, turn.ts_samples_end - turn.ts_samples_start))
        return now - turn.created_at_ms

    def _samples_to_ms(self, samples: int) -> int:
        return samples * 1000 // self.client_caps.uplink_rate

    def _t_listen_max(self) -> int:
        return self.timers.T_listen_max_A if self.profile is Profile.A else self.timers.T_listen_max_B

    def _t_filler(self) -> int:
        return self.timers.T_filler_A if self.profile is Profile.A else self.timers.T_filler_B

    def _t_drain(self, turn: Turn) -> int:
        return max(0, turn.audio_sent_ms - turn.heard_ms) + self.timers.T_drain_extra

