"""Gate chain (spec 03 §8.4): intent → utterance → backend_state, exact reason codes."""

from __future__ import annotations

import pytest

from vak.core.tools.gates import (
    REASON_BACKEND,
    REASON_INTENT,
    REASON_UTTERANCE,
    GateContext,
    clear_checkers,
    register_checker,
    run_gates,
)
from vak.core.types import Gate, GateKind

GATES = [
    Gate(GateKind.BACKEND_STATE, check="no_active"),  # listed out of order on purpose
    Gate(GateKind.INTENT, allowed_actions=["confirm_submit"]),
    Gate(GateKind.UTTERANCE, must_match=["开始", "提交", "确认"], must_not_match=["怎么提交", "提交什么"]),
]


@pytest.fixture(autouse=True)
def _clean():
    clear_checkers()
    yield
    clear_checkers()


async def test_chain_order_and_pass():
    order: list[str] = []

    async def ok(args, ctx):
        order.append("backend")
        return True

    register_checker("no_active", ok)
    v = await run_gates(GATES, {"option_id": "o1"}, GateContext(utterance="确认提交", action="confirm_submit"))
    assert v.passed and v.reason_code is None and order == ["backend"]


async def test_intent_mismatch_stops_before_utterance_and_backend():
    called = []

    async def ok(args, ctx):
        called.append(1)
        return True

    register_checker("no_active", ok)
    v = await run_gates(GATES, {}, GateContext(utterance="确认提交", action="chitchat"))
    assert not v.passed and v.reason_code == REASON_INTENT and v.gate is GateKind.INTENT and not called


async def test_utterance_not_explicit():
    v = await run_gates(GATES, {}, GateContext(utterance="好的", action="confirm_submit"))
    assert v.reason_code == REASON_UTTERANCE
    v = await run_gates(GATES, {}, GateContext(utterance="怎么提交啊", action="confirm_submit"))
    assert v.reason_code == REASON_UTTERANCE
    v = await run_gates(GATES, {}, GateContext(utterance="", action="confirm_submit"))
    assert v.reason_code == REASON_UTTERANCE


async def test_backend_state_bool_dict_and_missing_checker():
    async def no(args, ctx):
        return {"ok": False, "alternatives": [{"label": "换方案二", "action": "select_option"}], "message": "有进行中的请求"}

    register_checker("no_active", no)
    ctx = GateContext(utterance="确认提交", action="confirm_submit")
    v = await run_gates(GATES, {"option_id": "o1"}, ctx)
    assert not v.passed and v.reason_code == REASON_BACKEND and v.alternatives[0]["label"] == "换方案二"
    assert v.message == "有进行中的请求"

    async def false(args, ctx):
        return False

    register_checker("no_active", false)
    assert (await run_gates(GATES, {}, ctx)).reason_code == REASON_BACKEND
    clear_checkers()
    v = await run_gates(GATES, {}, ctx)
    assert v.reason_code == REASON_BACKEND and "not registered" in (v.message or "")


async def test_ui_confirm_satisfies_gates_1_and_2_only():
    seen = []

    async def chk(args, ctx):
        seen.append(ctx.confirmed_by_ui)
        return True

    register_checker("no_active", chk)
    v = await run_gates(GATES, {}, GateContext(utterance="", action="other", confirmed_by_ui=True))
    assert v.passed and seen == [True]


async def test_only_subset_and_tool_name_as_default_action():
    register_checker("no_active", lambda a, c: _true())
    v = await run_gates(GATES, {}, GateContext(utterance="什么"), tool_name="x", only={GateKind.BACKEND_STATE})
    assert v.passed  # utterance gate skipped
    v = await run_gates([Gate(GateKind.INTENT, allowed_actions=["submit_request"])], {}, GateContext(), tool_name="submit_request")
    assert v.passed


async def _true():
    return True
