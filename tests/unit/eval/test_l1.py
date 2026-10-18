"""L1 loader validation and runner over a fake SessionDriver; race pass-rate gating (spec 06 §5.2, §8.2)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from vak.eval import l1
from vak.eval.rubric import JudgeInfo, load_builtin_rubrics

SCRIPT = {
    "id": "interrupt_during_tool_reply_02",
    "profile": ["A"],
    "modality": ["text", "audio"],
    "race": True,
    "repeat": 10,
    "mock_backend": "fixtures/route_zoo_v3.json",
    "setup": {"state": {"stage": "touring", "current_stop": "熊猫馆"}},
    "turns": [
        {
            "user": "下一站怎么走",
            "audio": "fixtures/next_stop.wav",
            "expect": [
                {"event": "function_call", "name": "get_route", "args_subset": {"to": "海洋馆"}},
                {"event": "sound_first", "within_ms": 900},
                {"event": "content_first", "within_ms": 1100},
                {"event": "tool_closed", "closed_by": "result"},
            ],
        },
        {
            "user": "等等，厕所在哪",
            "send_after": {"event": "content_first", "delay_ms": 1500},
            "expect": [
                {"event": "bot_interrupted"},
                {"event": "function_call", "name": "practical_info", "args_subset": {"kind": "toilet"}},
                {"event": "response", "eval": ["brevity"]},
            ],
        },
        {
            "user": "嗯",
            "expect": [
                {"event": "response", "absent": True, "within_ms": 3000},
                {"event": "turn_terminal", "value": "discarded"},
            ],
        },
    ],
    "assert_session": {"stuck": 0, "double_voice_count": 0, "hanging_calls": 0},
}


class InstantClock:
    """Clock whose sleeps resolve immediately but are recorded (send_after delay)."""

    def __init__(self) -> None:
        self.slept: list[int] = []
        self._now = 0

    def now_ms(self) -> int:
        return self._now

    def epoch_ms(self) -> int:
        return self._now

    async def sleep(self, ms: int) -> None:
        self.slept.append(ms)
        self._now += ms


class FakeDriver:
    """Scripted event source. `events` maps event name -> list of payloads per
    successive wait; `latency` overrides at_ms for sound/content."""

    def __init__(self, overrides: dict[str, Any] | None = None, traces: list[dict[str, Any]] | None = None,
                 fail_start: bool = False) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.o = overrides or {}
        self._traces = traces if traces is not None else [{"stuck": False, "double_voice_count": 0, "tool_calls": [
            {"call_id": "c1", "closed_by": "result", "settled_at_ms": 816}]}]
        self.fail_start = fail_start
        self._waits: list[str] = []

    async def start(self) -> None:
        if self.fail_start:
            raise OSError("gateway unreachable")

    async def send_audio_file(self, path: str, realtime: bool = True) -> None:
        self.sent.append(("audio", (path, realtime)))

    async def send_text(self, text: str) -> None:
        self.sent.append(("text", text))

    async def press(self) -> None:
        self.sent.append(("press", None))

    async def release(self) -> None:
        self.sent.append(("release", None))

    async def wait_event(self, name: str, timeout_ms: int) -> dict[str, Any] | None:
        self._waits.append(name)
        if name in self.o:
            v = self.o[name]
            return v.pop(0) if isinstance(v, list) else v
        n_turn2_calls = self._waits.count("function_call")
        defaults: dict[str, Any] = {
            "function_call": {"name": "get_route", "args": {"to": "海洋馆"}} if n_turn2_calls <= 1
            else {"name": "practical_info", "args": {"kind": "toilet"}},
            "sound_first": {"at_ms": 830},
            "content_first": {"at_ms": 1080},
            "tool_closed": {"closed_by": "result"},
            "bot_interrupted": {},
            "response": None if self._waits.count("response") >= 2 else {"text": "厕所在左边。", "session_id": "s", "turn_id": "c2"},
            "turn_terminal": {"value": "discarded"},
        }
        return defaults.get(name)

    def traces(self) -> list[dict[str, Any]]:
        return self._traces


def test_loader_and_validation(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(SCRIPT, allow_unicode=True), encoding="utf-8")
    s = l1.load_script(p)
    assert s.id == SCRIPT["id"] and s.race and s.repeat == 10 and s.modality == ["text", "audio"]
    assert s.turns[1].send_after == l1.SendAfter("content_first", 1500)
    assert s.turns[2].expect[0].absent and s.turns[2].expect[0].within_ms == 3000
    assert s.turns[0].expect[0].args_subset == {"to": "海洋馆"}
    assert s.assert_session == {"stuck": 0, "double_voice_count": 0, "hanging_calls": 0}

    bad = copy.deepcopy(SCRIPT)
    bad["repeat"] = 3  # race needs N >= 10
    bad["turns"][1]["send_after"] = {"event": "tts_done"}
    bad["turns"][0]["expect"][1] = {"event": "sound_first"}  # needs within_ms
    bad["turns"][2]["expect"][0] = {"event": "response", "absent": True}  # absent needs within_ms
    bad["assert_session"] = {"stuck": 0, "latency": 1}
    errs = l1.validate_script(bad)
    assert len(errs) == 5
    with pytest.raises(ValueError):
        l1.parse_script(bad)
    sim = {"id": "sim_child_remove_stop_01", "profile": ["A"], "mode": "simulated", "race": True, "repeat": 10,
           "persona": {"id": "child_7y", "goal": "去掉爬行馆"}, "judge": {"per_turn": ["understanding"]}}
    assert l1.validate_script(sim) == [] and l1.parse_script(sim).mode == "simulated"
    assert l1.validate_script({**sim, "persona": {"id": "x"}})


async def test_runner_passes_race_script_and_gates_on_pass_rate() -> None:
    script = l1.parse_script(SCRIPT)
    drivers: list[FakeDriver] = []

    def factory() -> FakeDriver:
        d = FakeDriver()
        drivers.append(d)
        return d

    clock = InstantClock()
    res = await l1.run_l1(script, factory, judge_info=JudgeInfo(model="rule"), rubrics=load_builtin_rubrics(),
                          clock=clock, repeat=10)
    assert res.n == 10 and res.pass_rate == 1.0
    ok, msg = res.gate()
    assert ok and "100%" in msg
    assert drivers[0].sent == [("text", "下一站怎么走"), ("text", "等等，厕所在哪"), ("text", "嗯")]
    assert res.runs[0].verdicts[0]["rubric_id"] == "brevity" and res.runs[0].verdicts[0]["verdict"] == "yes"
    assert clock.slept == [1500] * 10  # send_after delay goes through the injected clock


async def test_runner_audio_modality_sends_file_realtime() -> None:
    script = l1.parse_script(SCRIPT)
    d = FakeDriver()
    res = await l1.run_l1(script, lambda: d, repeat=1, modality="audio", judge_info=JudgeInfo(model="rule"),
                          rubrics=load_builtin_rubrics(), clock=InstantClock())
    assert res.runs[0].passed
    assert d.sent[0] == ("audio", ("fixtures/next_stop.wav", True))
    with pytest.raises(ValueError):
        await l1.run_l1(l1.parse_script({**SCRIPT, "modality": ["text"]}), lambda: d, modality="audio")


@pytest.mark.parametrize(
    ("overrides", "kind"),
    [
        ({"function_call": [{"name": "get_route", "args": {"to": "熊猫馆"}}]}, "missing_function_call"),
        ({"function_call": [None]}, "missing_function_call"),
        ({"content_first": {"at_ms": 1900}}, "latency_budget"),
        ({"tool_closed": {"closed_by": "cancelled"}}, "unexpected_event"),
        ({"bot_interrupted": None}, "timeout"),
        ({"response": [{"text": "一。二。三。"}, {"text": "late"}]}, "judge_no"),
        ({"turn_terminal": {"value": "completed"}}, "unexpected_event"),
    ],
)
async def test_failure_classification(overrides: dict[str, Any], kind: str) -> None:
    script = l1.parse_script(SCRIPT)
    # a late "response" within the absent window is an unexpected event; the judge_no case feeds one
    res = await l1.run_l1(script, lambda: FakeDriver(copy.deepcopy(overrides)), repeat=1,
                          judge_info=JudgeInfo(model="rule"), rubrics=load_builtin_rubrics(), clock=InstantClock())
    kinds = [f.kind for f in res.runs[0].failures]
    assert kind in kinds, kinds
    assert set(kinds) <= set(l1.FAILURE_KINDS)


async def test_infra_failure_and_hard_metrics_fail_once() -> None:
    script = l1.parse_script(SCRIPT)
    res = await l1.run_l1(script, lambda: FakeDriver(fail_start=True), repeat=1, clock=InstantClock())
    assert res.runs[0].failures[0].kind == "infra"
    bad_traces = [{"stuck": True, "double_voice_count": 1, "tool_calls": [{"call_id": "x", "closed_by": None}]}]
    res = await l1.run_l1(script, lambda: FakeDriver(traces=bad_traces), repeat=10,
                          judge_info=JudgeInfo(model="rule"), rubrics=load_builtin_rubrics(), clock=InstantClock())
    assert res.pass_rate == 0.0
    assert set(res.runs[0].hard_violations) == {"stuck=1 > 0", "double_voice_count=1 > 0", "hanging_calls=1 > 0"}
    ok, msg = res.gate()
    assert not ok and "硬指标" in msg


async def test_race_pass_rate_threshold_and_min_n() -> None:
    script = l1.parse_script(SCRIPT)
    i = 0

    def factory() -> FakeDriver:
        nonlocal i
        i += 1
        return FakeDriver({"bot_interrupted": None} if i % 10 == 0 else None)

    res = await l1.run_l1(script, factory, repeat=10, judge_info=JudgeInfo(model="rule"),
                          rubrics=load_builtin_rubrics(), clock=InstantClock())
    assert res.pass_rate == 0.9 and res.gate()[0]
    assert res.failures_by_kind() == {"timeout": 1}
    i = 0
    res = await l1.run_l1(script, factory, repeat=20, judge_info=JudgeInfo(model="rule"),
                          rubrics=load_builtin_rubrics(), clock=InstantClock())
    assert res.pass_rate == 0.9 and res.gate(race_pass_rate=0.95)[0] is False
    short = await l1.run_l1(script, factory, repeat=3, judge_info=JudgeInfo(model="rule"),
                            rubrics=load_builtin_rubrics(), clock=InstantClock())
    ok, msg = short.gate()
    assert not ok and "不作门禁" in msg
    non_race = l1.parse_script({**SCRIPT, "race": False, "repeat": 1})
    res = await l1.run_l1(non_race, lambda: FakeDriver(), judge_info=JudgeInfo(model="rule"),
                          rubrics=load_builtin_rubrics(), clock=InstantClock())
    assert res.gate() == (True, "全部通过")


def test_check_session_counts() -> None:
    traces = [{"stuck": False, "double_voice_count": 0, "tool_calls": [{"closed_by": "result", "settled_at_ms": 1}]},
              {"stuck": True, "double_voice_count": 2, "tool_calls": [{"closed_by": "result", "settled_at_ms": None}]}]
    assert l1.check_session(traces, {"stuck": 0, "double_voice_count": 0, "hanging_calls": 0}) == [
        "stuck=1 > 0", "double_voice_count=2 > 0", "hanging_calls=1 > 0"]
    assert l1.check_session(traces, {"stuck": 1}) == []
