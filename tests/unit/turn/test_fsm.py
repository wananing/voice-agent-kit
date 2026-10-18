"""Spec 01 §2.1 transition table; illegal transitions raise."""

from __future__ import annotations

import pytest

from vak.core.clock import FakeClock
from vak.core.turn.fsm import TRANSITIONS, Turn, TurnEvent, transition
from vak.core.turn.watchdog import WatchdogSet
from vak.core.types import TERMINAL_TURN_STATES, IllegalTransition, TurnId, TurnSource, TurnState


def test_happy_path_follows_table():
    s = TurnState.IDLE
    for ev in (TurnEvent.START, TurnEvent.END_COMMIT, TurnEvent.SOUND_FIRST, TurnEvent.REPLY_DONE, TurnEvent.DRAINED):
        s = transition(s, ev)
    assert s is TurnState.COMPLETED


def test_holding_merge_is_same_turn_not_new():
    assert transition(TurnState.LISTENING, TurnEvent.END_HOLD) is TurnState.HOLDING
    assert transition(TurnState.HOLDING, TurnEvent.MERGE) is TurnState.LISTENING
    assert transition(TurnState.HOLDING, TurnEvent.HOLD_EXPIRE) is TurnState.COMMITTED


@pytest.mark.parametrize("state", sorted(TERMINAL_TURN_STATES, key=lambda s: s.value))
@pytest.mark.parametrize("event", list(TurnEvent))
def test_terminal_states_have_no_exits(state, event):
    with pytest.raises(IllegalTransition):
        transition(state, event)


@pytest.mark.parametrize(
    "state,event",
    [
        (TurnState.IDLE, TurnEvent.END_COMMIT),
        (TurnState.LISTENING, TurnEvent.SOUND_FIRST),
        (TurnState.COMMITTED, TurnEvent.START),
        (TurnState.COMMITTED, TurnEvent.MERGE),
        (TurnState.RESPONDING, TurnEvent.DISCARD),  # discarded means zero output
        (TurnState.DRAINING, TurnEvent.END_HOLD),
        (TurnState.RESPONDING, TurnEvent.START),
    ],
)
def test_illegal_transitions_raise(state, event):
    with pytest.raises(IllegalTransition):
        transition(state, event)


def test_every_active_state_can_fail_cancel_interrupt():
    for s in (TurnState.LISTENING, TurnState.HOLDING, TurnState.COMMITTED, TurnState.RESPONDING, TurnState.DRAINING):
        assert TRANSITIONS[(s, TurnEvent.FAIL)] is TurnState.FAILED
        assert TRANSITIONS[(s, TurnEvent.CANCEL)] is TurnState.CANCELLED
        assert TRANSITIONS[(s, TurnEvent.INTERRUPT)] is TurnState.INTERRUPTED


async def test_turn_record_apply_raises_on_illegal():
    async def noop(_: str) -> None: ...

    t = Turn(TurnId("c", 1), TurnSource.BUTTON, WatchdogSet(FakeClock(), noop), created_at_ms=0)
    assert t.apply(TurnEvent.START) is TurnState.LISTENING
    with pytest.raises(IllegalTransition):
        t.apply(TurnEvent.DRAINED)
    assert t.state is TurnState.LISTENING
