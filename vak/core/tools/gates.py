"""Write-operation gate chain (spec 03 §8.4): intent → utterance → backend_state.

Backend-state checkers are registered by name and looked up by
`Gate.check`. A checker returns `True`/`False` or a dict
`{"ok": bool, "alternatives": [...], "message": str}`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from vak.core.types import Gate, GateKind, TurnId

REASON_INTENT = "intent_mismatch"
REASON_UTTERANCE = "utterance_not_explicit"
REASON_BACKEND = "backend_state"
REASON_PHASE = "not_available_in_phase"

GATE_REASONS = frozenset({REASON_INTENT, REASON_UTTERANCE, REASON_BACKEND})

# spec 03 §8.4: gates run in this order regardless of list order.
GATE_ORDER: tuple[GateKind, ...] = (GateKind.INTENT, GateKind.UTTERANCE, GateKind.BACKEND_STATE)


@dataclass(slots=True)
class GateContext:
    """What the gates need from the turn (spec 03 §8.2 inputs)."""

    turn_id: TurnId | None = None
    generation: int = 0
    utterance: str = ""  # the user's own words (ASR final)
    action: str | None = None  # final action from layers ③/④; None → the tool name is used
    confirmed_by_ui: bool = False  # B profile tap "confirm" satisfies gates 1 and 2 (spec 03 §8.4)


@dataclass(slots=True)
class GateVerdict:
    passed: bool
    gate: GateKind | None = None
    reason_code: str | None = None
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    message: str | None = None


Checker = Callable[[dict[str, Any], GateContext], Awaitable[bool | dict[str, Any]]]

_CHECKERS: dict[str, Checker] = {}


def register_checker(name: str, fn: Checker) -> None:
    """Register a backend_state checker under `name` (referenced by `Gate.check`)."""
    _CHECKERS[name] = fn


def unregister_checker(name: str) -> None:
    _CHECKERS.pop(name, None)


def get_checker(name: str) -> Checker | None:
    return _CHECKERS.get(name)


def clear_checkers() -> None:
    _CHECKERS.clear()


async def _check(gate: Gate, args: dict[str, Any], ctx: GateContext, tool_name: str) -> GateVerdict:
    if gate.kind is GateKind.INTENT:
        if ctx.confirmed_by_ui:
            return GateVerdict(True, gate.kind)
        action = ctx.action if ctx.action is not None else tool_name
        if gate.allowed_actions and action not in gate.allowed_actions:
            return GateVerdict(False, gate.kind, REASON_INTENT)
        return GateVerdict(True, gate.kind)
    if gate.kind is GateKind.UTTERANCE:
        if ctx.confirmed_by_ui:
            return GateVerdict(True, gate.kind)
        text = ctx.utterance.lower()
        if gate.must_match and not any(m.lower() in text for m in gate.must_match):
            return GateVerdict(False, gate.kind, REASON_UTTERANCE)
        if any(m.lower() in text for m in gate.must_not_match if m):
            return GateVerdict(False, gate.kind, REASON_UTTERANCE)
        return GateVerdict(True, gate.kind)
    # backend_state: re-read the backend right now
    checker = get_checker(gate.check or "")
    if checker is None:
        return GateVerdict(False, gate.kind, REASON_BACKEND, message=f"checker {gate.check!r} not registered")
    outcome = await checker(args, ctx)
    if isinstance(outcome, dict):
        ok = bool(outcome.get("ok", False))
        alts = list(outcome.get("alternatives") or [])
        return GateVerdict(ok, gate.kind, None if ok else REASON_BACKEND, alts, outcome.get("message"))
    return GateVerdict(bool(outcome), gate.kind, None if outcome else REASON_BACKEND)


async def run_gates(
    gates: list[Gate],
    args: dict[str, Any],
    ctx: GateContext,
    *,
    tool_name: str = "",
    only: set[GateKind] | frozenset[GateKind] | None = None,
) -> GateVerdict:
    """Run the chain in canonical order; stop at the first failure.

    `only` restricts to a subset of kinds (the executor re-runs
    `backend_state` right before the backend call, spec 03 §5.2 rule 1).
    """
    for kind in GATE_ORDER:
        if only is not None and kind not in only:
            continue
        for gate in gates:
            if gate.kind is not kind:
                continue
            verdict = await _check(gate, args, ctx, tool_name)
            if not verdict.passed:
                return verdict
    return GateVerdict(True)


__all__ = [
    "GATE_ORDER",
    "GATE_REASONS",
    "REASON_BACKEND",
    "REASON_INTENT",
    "REASON_PHASE",
    "REASON_UTTERANCE",
    "Checker",
    "GateContext",
    "GateVerdict",
    "clear_checkers",
    "get_checker",
    "register_checker",
    "run_gates",
    "unregister_checker",
]
