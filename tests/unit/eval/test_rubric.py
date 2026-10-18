"""Rubric loader, judge output format, heard_text, brevity rule (spec 06 §6)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vak.eval import rubric as rb


class FakeJudge:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def judge(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


def test_builtin_set_loads_with_spec_fields() -> None:
    rs = rb.load_builtin_rubrics()
    assert set(rs) == {"understanding", "honesty", "recovery", "efficiency", "brevity", "task_completion",
                       "tone_consistency"}
    rec = rs["recovery"]
    assert rec.version == 2 and rec.pair_with == "honesty" and rec.scope == "per_turn"
    assert set(rec.options) == {"yes", "no", "not_applicable", "continue"}  # YAML yes/no keys kept as strings
    assert rec.reason_required_for == ["no"]
    assert "tool_results" in rs["honesty"].inputs  # judge must see tool results (§6.1 rule 7)
    assert rs["task_completion"].scope == "session"
    assert rs["brevity"].method == "rule"


def test_loader_validation(tmp_path: Path) -> None:
    raw = {"rubric_id": "x", "version": 1, "criterion": "c", "options": {"yes": "y", "no": "n"}}
    assert rb.validate_rubric(raw) == []
    assert rb.validate_rubric({**raw, "options": {"yes": "y"}})
    assert rb.validate_rubric({**raw, "options": {"yes": "y", "no": "n", "maybe": "m"}})
    assert rb.validate_rubric({**raw, "inputs": ["audio"]})
    assert rb.validate_rubric({**raw, "scope": "global"})
    assert rb.validate_rubric({**raw, "method": "rule"})  # rule needs a rule mapping
    p = tmp_path / "r.yaml"
    p.write_text(yaml.safe_dump({**raw, "reason_required_for": ["no"]}), encoding="utf-8")
    assert rb.load_rubric(p).rubric_id == "x"
    with pytest.raises(ValueError):
        rb.parse_rubric({**raw, "applies_to": ["C"]})


def test_judge_output_format_and_reason_only_on_no() -> None:
    rs = rb.load_builtin_rubrics()
    inputs = rb.RubricInputs(context=[{"user": "去掉爬行馆", "assistant": "好的"}], latest_bot_reply="没有不经过那里的路线。",
                             tool_calls=[{"name": "adjust_itinerary"}], tool_results=[{"ok": False}],
                             heard_text="没有不经过那里的路线。")
    judge = FakeJudge("verdict: no\nreason: 没有给任何替代")
    out = rb.run_rubric(rs["recovery"], inputs, judge, rb.JudgeInfo(model="judge-x", prompt_version="judge@5"),
                        "s_7f3a", "c43")
    assert out == {"session_id": "s_7f3a", "turn_id": "c43", "rubric_id": "recovery", "rubric_version": 2,
                   "verdict": "no", "reason": "没有给任何替代",
                   "judge": {"model": "judge-x", "prompt_version": "judge@5", "temperature": 0.0}}
    prompt = judge.prompts[0]
    assert "tool_results" in prompt and "adjust_itinerary" in prompt and "heard_text" in prompt
    assert "continue" in prompt and "not_applicable" in prompt

    yes = rb.run_rubric(rs["recovery"], inputs, FakeJudge("Verdict: YES\nreason:"), rb.JudgeInfo(model="j"), "s", "c")
    assert yes["verdict"] == "yes" and "reason" not in yes
    cont = rb.run_rubric(rs["understanding"], inputs, FakeJudge("verdict: continue"), rb.JudgeInfo(model="j"), "s", "c")
    assert cont["verdict"] == "continue"
    garbage = rb.run_rubric(rs["honesty"], inputs, FakeJudge("I think it's fine"), rb.JudgeInfo(model="j"), "s", "c")
    assert garbage["verdict"] == "no" and "无法解析" in garbage["reason"]
    # temperature is always 0 regardless of what the caller passes
    hot = rb.run_rubric(rs["honesty"], inputs, FakeJudge("verdict: yes"), rb.JudgeInfo(model="j", temperature=0.7), "s", "c")
    assert hot["judge"]["temperature"] == 0.0
    with pytest.raises(ValueError):
        rb.run_rubric(rs["honesty"], inputs, None, None, "s", "c")


def test_brevity_rule_a_le_2_b_le_3() -> None:
    brevity = rb.load_builtin_rubrics()["brevity"]
    two = rb.RubricInputs(latest_bot_reply="往前走。到了海洋馆！")
    three = rb.RubricInputs(latest_bot_reply="往前走。然后左转。到了海洋馆。")
    assert rb.run_rubric(brevity, two, None, None, "s", "c", profile="A")["verdict"] == "yes"
    out = rb.run_rubric(brevity, three, None, None, "s", "c", profile="A")
    assert out["verdict"] == "no" and "3 句 > 2 句" in out["reason"]
    assert out["judge"] == {"model": "rule", "prompt_version": "brevity@1", "temperature": 0.0}
    assert rb.run_rubric(brevity, three, None, None, "s", "c", profile="B")["verdict"] == "yes"
    four = rb.RubricInputs(latest_bot_reply="一。二。三？四！")
    assert rb.run_rubric(brevity, four, None, None, "s", "c", profile="B")["verdict"] == "no"
    assert rb.run_rubric(brevity, rb.RubricInputs(latest_bot_reply=""), None, None, "s", "c")["verdict"] == "not_applicable"
    # heard_text wins over the generated reply: user only heard one sentence
    assert rb.run_rubric(brevity, rb.RubricInputs(latest_bot_reply="一。二。三。", heard_text="一。 [interrupted]"),
                         None, None, "s", "c", profile="A")["verdict"] == "yes"
    assert rb.sentence_count("Hello there. How are you?\nFine") == 3


def test_applies_to_gives_not_applicable() -> None:
    r = rb.parse_rubric({"rubric_id": "b_only", "version": 1, "criterion": "c", "applies_to": ["B"],
                         "options": {"yes": "y", "no": "n"}})
    out = rb.run_rubric(r, rb.RubricInputs(), None, None, "s", "c", profile="A")
    assert out["verdict"] == "not_applicable"


def test_heard_text_truncates_at_played_ms_with_marker() -> None:
    segs = [
        rb.SpokenSegment("第一句话说完了。", generated_ms=1000, played_ms=1000),
        rb.SpokenSegment("第二句只播了一半", generated_ms=1000, played_ms=500, interrupted=True),
        rb.SpokenSegment("第三句没播", generated_ms=800, played_ms=0),
    ]
    assert rb.heard_text(segs) == "第一句话说完了。第二句只 [interrupted]"
    assert rb.heard_text([rb.SpokenSegment("全部播完", 500, 500)]) == "全部播完"
    assert rb.heard_text([rb.SpokenSegment("未知播放", None, None)]) == "未知播放"
    assert rb.heard_text([rb.SpokenSegment("部分", 1000, 600)]) == "部 [interrupted]"  # 2 chars x 0.6 -> 1
