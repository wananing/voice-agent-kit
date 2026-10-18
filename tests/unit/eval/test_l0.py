"""L0 runner: named metrics, ASR variants reported separately, alias-table args, retry rule, CI delta (spec 06 §5.1, §8.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from vak.eval import l0

CASES = [
    {
        "id": "route_next_003", "set": "blind-zh-v1", "profile": ["A", "B"],
        "state": {"stage": "touring", "itinerary_version": 4, "task_active": False},
        "history": [{"user": "我们接下来去哪", "assistant": "下一站是海洋馆。"}],
        "turns": [{
            "user": "那个怎么走",
            "asr_variants": [{"kind": "homophone", "text": "那个怎么奏"}, {"kind": "truncation", "text": "那个怎"}],
            "expected_calls": [{"name": "get_route", "args_subset": {"to": "海洋馆"}}],
            "forbidden_calls": ["start_task", "adjust_itinerary"],
        }],
        "expected_final_state": {"itinerary_version": 4, "task_active": False},
    },
    {
        "id": "neg_write_ok_011", "set": "blind-zh-v1", "profile": ["A"],
        "state": {"stage": "plan_confirm", "task_active": False},
        "turns": [{"user": "好的", "expect_no_tool": True, "forbidden_calls": ["start_task"]}],
        "expected_final_state": {"task_active": False},
    },
    {
        "id": "start_task_001", "set": "train-zh-v1", "profile": ["A"],
        "state": {"stage": "plan_confirm", "task_active": False},
        "turns": [{"user": "开始吧", "expected_calls": [{"name": "start_task"}]}],
        "expected_final_state": {"task_active": True},
    },
]


def decide_good(state: dict[str, Any], history: list[dict[str, str]], text: str) -> l0.Decision:
    if "怎么走" in text or "怎么奏" in text:
        return l0.Decision(calls=[l0.CallSpec("get_route", {"to": "海洋馆 "})], text="往前走")
    if text == "开始吧":
        return l0.Decision(calls=[l0.CallSpec("start_task", {})], state={**state, "task_active": True})
    if text == "好的":
        return l0.Decision(text="好")
    return l0.Decision(text="没听清")


def test_case_loader_and_validation(tmp_path: Path) -> None:
    p = tmp_path / "cases.jsonl"
    p.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in CASES) + "\n# tail\n", encoding="utf-8")
    cases = l0.load_cases(p)
    assert [c.id for c in cases] == ["route_next_003", "neg_write_ok_011", "start_task_001"]
    assert cases[0].turns[0].asr_variants[1].kind == "truncation"
    bad = {**CASES[0], "turns": [{"user": "x", "asr_variants": [{"kind": "typo", "text": "y"}]}]}
    assert any("asr_variants" in e for e in l0.validate_case(bad))
    with pytest.raises(ValueError):
        l0.parse_case({**CASES[1], "profile": ["C"]})


def test_metrics_original_vs_variants_separately() -> None:
    cases = [l0.parse_case(c) for c in CASES]
    res = l0.run_l0(cases, decide_good)
    orig = res.by_variant[l0.ORIGINAL]
    assert orig.action_hit.num == 2 and orig.action_hit.den == 2
    assert orig.slot_hit.num == 1 and orig.slot_hit.den == 1  # "海洋馆 " normalises to 海洋馆
    assert orig.no_tool_correct.num == 1 and orig.no_tool_correct.den == 1
    assert orig.write_false_trigger.num == 0 and orig.write_false_trigger.den == 2
    assert orig.final_state_match.num == 3 and orig.final_state_match.den == 3
    # homophone variant still hits; truncation ("那个怎") misses -> reported separately
    assert res.by_variant["homophone"].action_hit.value == 1.0
    assert res.by_variant["truncation"].action_hit.value == 0.0
    assert res.by_variant["truncation"].action_hit.den == 1  # only turns that have that variant count
    assert res.infra_errors == 0 and res.retries == 0
    assert json.loads(res.to_json())["by_variant"]["truncation"]["action_hit"] == {"num": 0, "den": 1}


def test_write_false_trigger_and_profile_filter() -> None:
    def trigger_happy(state: dict[str, Any], history: list[dict[str, str]], text: str) -> l0.Decision:
        return l0.Decision(calls=[l0.CallSpec("start_task", {})], state={**state, "task_active": True})

    cases = [l0.parse_case(c) for c in CASES]
    res = l0.run_l0(cases, trigger_happy, profile="B")  # only route_next_003 applies to B
    m = res.by_variant[l0.ORIGINAL]
    assert m.write_false_trigger.den == 1 and m.write_false_trigger.num == 1
    assert m.action_hit.value == 0.0 and m.final_state_match.value == 0.0
    assert any("forbidden" in f for o in res.outcomes for f in o.failures)


def test_alias_table_and_normalisation() -> None:
    aliases = {"ocean hall": "海洋馆", "海洋世界": "海洋馆"}
    assert l0.args_match({"to": "海洋馆"}, {"to": "Ocean Hall"}, aliases)
    assert l0.args_match({"to": "海洋馆"}, {"to": "海洋世界", "extra": 1}, aliases)
    assert not l0.args_match({"to": "海洋馆"}, {"to": "熊猫馆"}, aliases)
    assert not l0.args_match({"to": "海洋馆"}, {}, aliases)
    assert l0.normalize("１２") == 12 and l0.normalize("12") == 12 and l0.normalize(12.0) == 12
    assert l0.normalize(True) is True


def test_retry_only_on_timeout_without_call_or_text() -> None:
    calls: list[str] = []

    def flaky(state: dict[str, Any], history: list[dict[str, str]], text: str) -> l0.Decision:
        calls.append(text)
        if len(calls) == 1:
            return l0.Decision(timeout=True)  # retried once
        if len(calls) == 2:
            return l0.Decision(calls=[l0.CallSpec("start_task")], state={**state, "task_active": True})
        return l0.Decision(timeout=True, text="部分回复")  # timeout WITH text: no retry

    cases = [l0.parse_case(CASES[2]), l0.parse_case(CASES[1])]
    res = l0.run_l0(cases, flaky)
    assert res.retries == 1
    assert calls == ["开始吧", "开始吧", "好的"]
    assert res.by_variant[l0.ORIGINAL].action_hit.value == 1.0


def test_infra_errors_counted_separately() -> None:
    def boom(state: dict[str, Any], history: list[dict[str, str]], text: str) -> l0.Decision:
        raise ConnectionError("upstream down")

    res = l0.run_l0([l0.parse_case(CASES[2])], boom)
    assert res.infra_errors == 1
    assert res.by_variant[l0.ORIGINAL].action_hit.den == 0  # behaviour not scored
    assert res.outcomes[0].infra_error is not None and "ConnectionError" in res.outcomes[0].infra_error


def test_compare_ci_delta() -> None:
    base = {"original": {"action_hit": {"num": 90, "den": 100}, "write_false_trigger": {"num": 2, "den": 100},
                         "slot_hit": {"num": 80, "den": 100}}}
    cur_ok = {"original": {"action_hit": {"num": 89, "den": 100}, "write_false_trigger": {"num": 2, "den": 100},
                           "slot_hit": {"num": 95, "den": 100}}}
    r = l0.compare(base, cur_ok, max_drop_pp=2)
    assert r.ok and r.deltas_pp["original"]["action_hit"] == -1.0
    cur_bad = {"original": {"action_hit": {"num": 87, "den": 100}, "write_false_trigger": {"num": 3, "den": 100},
                            "slot_hit": {"num": 80, "den": 100}}}
    r = l0.compare(base, cur_bad)
    assert not r.ok
    assert any("action_hit 下降 3.0 pp" in f for f in r.failures)
    assert any("write_false_trigger 上升" in f for f in r.failures)
    # L0Result objects are accepted too
    cases = [l0.parse_case(c) for c in CASES]
    a = l0.run_l0(cases, decide_good)
    assert l0.compare(a, a).ok
