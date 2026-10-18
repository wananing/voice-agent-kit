"""Turn state machine table and per-turn record (spec 01 §2.1, §2.4).

States come from `vak.core.types.TurnState`; every transition is a row in
`TRANSITIONS`, anything else raises `IllegalTransition` (architecture §7.1).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vak.core.types import (
    IllegalTransition,
    InterruptSource,
    PlaybackReceipt,
    TurnId,
    TurnInput,
    TurnSource,
    TurnState,
)

if TYPE_CHECKING:
    from vak.core.turn.watchdog import WatchdogSet


class TurnEvent(enum.StrEnum):
    START = "start"  # turn.start, user turn (spec 01 §2.1 idle → listening)
    START_EVENT = "start_event"  # turn.start{source:event}, begins at committed (R2.2-6)
    END_COMMIT = "end_commit"  # turn.end and P(done) ≥ τ, or no holding
    END_HOLD = "end_hold"  # turn.end and P(done) < τ, ptt only (§2.1 holding)
    MERGE = "merge"  # turn.merge: holding → listening, same turn (R2.6-4)
    HOLD_EXPIRE = "hold_expire"  # T_hold → committed (§2.5)
    SOUND_FIRST = "sound_first"  # first audible output → responding
    REPLY_DONE = "reply_done"  # tts.stop{done} → draining
    DRAINED = "drained"  # last playback.ended / T_drain → completed
    INTERRUPT = "interrupt"
    DISCARD = "discard"
    FAIL = "fail"
    CANCEL = "cancel"


_ACTIVE = (
    TurnState.LISTENING,
    TurnState.HOLDING,
    TurnState.COMMITTED,
    TurnState.RESPONDING,
    TurnState.DRAINING,
)

TRANSITIONS: dict[tuple[TurnState, TurnEvent], TurnState] = {
    (TurnState.IDLE, TurnEvent.START): TurnState.LISTENING,
    (TurnState.IDLE, TurnEvent.START_EVENT): TurnState.COMMITTED,
    (TurnState.LISTENING, TurnEvent.END_COMMIT): TurnState.COMMITTED,
    (TurnState.LISTENING, TurnEvent.END_HOLD): TurnState.HOLDING,
    (TurnState.HOLDING, TurnEvent.MERGE): TurnState.LISTENING,
    (TurnState.HOLDING, TurnEvent.HOLD_EXPIRE): TurnState.COMMITTED,
    (TurnState.COMMITTED, TurnEvent.SOUND_FIRST): TurnState.RESPONDING,
    (TurnState.RESPONDING, TurnEvent.REPLY_DONE): TurnState.DRAINING,
    # narration turns have no upstream reply.done; drained closes them directly
    (TurnState.RESPONDING, TurnEvent.DRAINED): TurnState.COMPLETED,
    (TurnState.DRAINING, TurnEvent.DRAINED): TurnState.COMPLETED,
    # any non-terminal state → terminal (spec 01 §2.1 last row)
    **{(s, TurnEvent.INTERRUPT): TurnState.INTERRUPTED for s in _ACTIVE},
    **{
        (s, TurnEvent.DISCARD): TurnState.DISCARDED
        for s in (TurnState.LISTENING, TurnState.HOLDING, TurnState.COMMITTED)
    },
    **{(s, TurnEvent.FAIL): TurnState.FAILED for s in _ACTIVE},
    **{(s, TurnEvent.CANCEL): TurnState.CANCELLED for s in _ACTIVE},
}


def transition(state: TurnState, event: TurnEvent) -> TurnState:
    try:
        return TRANSITIONS[(state, event)]
    except KeyError:
        raise IllegalTransition(f"turn: {state.value} --{event.value}--> ?") from None


@dataclass(slots=True)
class Turn:
    """One turn, from turn.start to turn.closed (spec 01 §1.3)."""

    turn_id: TurnId
    source: TurnSource
    watchdogs: WatchdogSet
    created_at_ms: int
    generation: int = 0
    input: TurnInput | None = None
    state: TurnState = TurnState.IDLE
    text: str | None = None
    ts_samples_start: int | None = None
    ts_samples_end: int | None = None
    t0_ms: int | None = None
    committed_at_ms: int | None = None
    closed_at_ms: int | None = None
    closed_by: str | None = None
    close_sent: bool = False
    closed_frames: list[dict[str, Any]] = field(default_factory=list)
    segments: int = 1
    server_vad_segments: int = 0
    merged_from: list[TurnId] = field(default_factory=list)
    heard_ms: int = 0
    receipts: list[PlaybackReceipt] = field(default_factory=list)
    receipt_interrupted: bool = False
    speech_ms: int = 0
    speech_start_ts: int | None = None
    audio_end_at_ms: int | None = None
    last_endpoint_prob: float | None = None
    response_id: str | None = None
    request_id: str | None = None
    audio_sent_ms: int = 0
    sound_first: bool = False
    speech_first: bool = False
    stt_final: bool = False
    reply_done: bool = False
    drained: bool = False
    awaiting_tool: bool = False
    was_playing: bool = False
    interrupt_source: InterruptSource | None = None
    interrupt_at_ms: int | None = None
    truncate_estimated: bool = False
    false_interrupt_filtered: int = 0
    # spec 06 §2.4 segment timing: local marks and the seg.* values already written
    seg_marks: dict[str, int] = field(default_factory=dict)
    seg: dict[str, int] = field(default_factory=dict)
    seg_response: str | None = None  # only the turn's first requested response is segmented

    def apply(self, event: TurnEvent) -> TurnState:
        self.state = transition(self.state, event)
        return self.state

    @property
    def terminal(self) -> bool:
        return self.state.terminal
