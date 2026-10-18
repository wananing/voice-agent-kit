"""L1 dialogue scripts (spec 06 §5.2): YAML loader / validator and a runner
over a `SessionDriver`.

Assertions: `race` / `repeat`, `send_after {event, delay_ms}`, `absent: true`
+ `within_ms`, `sound_first` / `content_first` `within_ms` (T0-relative per
`anchors.primary`), `function_call` + `args_subset`, `tool_closed.closed_by`,
`turn_terminal.value`, `response` + `eval: [rubric_id]`, `persona` / `goal`,
`assert_session {stuck, double_voice_count, hanging_calls}`. Failures are
classified as `timeout / judge_no / missing_function_call / latency_budget /
unexpected_event / infra`. Race scripts gate on pass rate (>= 90 %, N >= 10);
hard metrics fail on a single occurrence (spec 06 §8.2 rule 5).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from vak.core.clock import Clock, MonotonicClock
from vak.eval.l0 import AliasTable, args_match
from vak.eval.rubric import Judge, JudgeInfo, Rubric, RubricInputs, run_rubric

log = logging.getLogger(__name__)

FAILURE_KINDS = ("timeout", "judge_no", "missing_function_call", "latency_budget", "unexpected_event", "infra")
SEND_AFTER_EVENTS = {"llm_started", "sound_first", "content_first", "fc_received", "playback_started"}
HARD_METRICS = ("stuck", "double_voice_count", "hanging_calls")
RACE_PASS_RATE = 0.9  # spec 06 §8.2 suggested
RACE_MIN_REPEAT = 10  # spec 06 §4.2 pass rate N >= 10


# --- script format --------------------------------------------------------------------


@dataclass(slots=True)
class Expect:
    event: str
    name: str | None = None
    args_subset: dict[str, Any] = field(default_factory=dict)
    within_ms: int | None = None
    absent: bool = False
    closed_by: str | None = None
    value: str | None = None
    eval: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SendAfter:
    event: str
    delay_ms: int = 0


@dataclass(slots=True)
class L1Turn:
    user: str | None = None
    audio: str | None = None  # audio file for modality=audio
    send_after: SendAfter | None = None
    press: bool = False  # ptt: press before sending, release after
    expect: list[Expect] = field(default_factory=list)


@dataclass(slots=True)
class L1Script:
    id: str
    profile: list[str]
    turns: list[L1Turn] = field(default_factory=list)
    modality: list[str] = field(default_factory=lambda: ["text"])
    race: bool = False
    repeat: int = 1
    mock_backend: str | None = None
    setup: dict[str, Any] = field(default_factory=dict)
    assert_session: dict[str, int] = field(default_factory=dict)
    mode: str = "scripted"  # scripted | simulated
    persona: dict[str, Any] | None = None
    judge: dict[str, list[str]] | None = None
    report: str | None = None


def validate_script(raw: dict[str, Any]) -> list[str]:
    errs = []
    if not isinstance(raw, dict):
        return ["脚本必须是一个映射"]
    for k in ("id", "profile"):
        if k not in raw:
            errs.append(f"缺必填字段 {k}")
    if "profile" in raw and (not isinstance(raw["profile"], list) or not set(raw["profile"]) <= {"A", "B"}):
        errs.append("profile 必须是 [A] / [B] / [A, B]")
    if "modality" in raw and not set(raw["modality"]) <= {"text", "audio"}:
        errs.append("modality 只能取 text / audio")
    if raw.get("race") and int(raw.get("repeat", 1)) < RACE_MIN_REPEAT:
        errs.append(f"race 脚本 repeat 须 ≥ {RACE_MIN_REPEAT}")
    mode = raw.get("mode", "scripted")
    if mode == "simulated":
        if not isinstance(raw.get("persona"), dict) or not {"id", "goal"} <= set(raw["persona"]):
            errs.append("simulated 脚本需 persona{id, goal}")
    elif not isinstance(raw.get("turns"), list) or not raw.get("turns"):
        errs.append("scripted 脚本需非空 turns")
    for i, t in enumerate(raw.get("turns") or []):
        if not isinstance(t, dict):
            errs.append(f"turns[{i}] 必须是映射")
            continue
        if "user" not in t and "audio" not in t:
            errs.append(f"turns[{i}] 缺 user 或 audio")
        sa = t.get("send_after")
        if sa is not None and (not isinstance(sa, dict) or sa.get("event") not in SEND_AFTER_EVENTS):
            errs.append(f"turns[{i}].send_after.event 须 ∈ {sorted(SEND_AFTER_EVENTS)}")
        for j, e in enumerate(t.get("expect") or []):
            if not isinstance(e, dict) or "event" not in e:
                errs.append(f"turns[{i}].expect[{j}] 缺 event")
                continue
            if e.get("absent") and e.get("within_ms") is None:
                errs.append(f"turns[{i}].expect[{j}] absent 需配 within_ms")
            if e["event"] in ("sound_first", "content_first") and not e.get("absent") and e.get("within_ms") is None:
                errs.append(f"turns[{i}].expect[{j}] {e['event']} 需 within_ms")
            if e["event"] == "function_call" and not e.get("name"):
                errs.append(f"turns[{i}].expect[{j}] function_call 需 name")
            if e["event"] == "tool_closed" and not e.get("closed_by"):
                errs.append(f"turns[{i}].expect[{j}] tool_closed 需 closed_by")
            if e["event"] == "turn_terminal" and not e.get("value"):
                errs.append(f"turns[{i}].expect[{j}] turn_terminal 需 value")
    a = raw.get("assert_session")
    if a is not None and (not isinstance(a, dict) or not set(a) <= set(HARD_METRICS)):
        errs.append(f"assert_session 只能含 {list(HARD_METRICS)}")
    return errs


def parse_script(raw: dict[str, Any]) -> L1Script:
    errs = validate_script(raw)
    if errs:
        raise ValueError(f"L1 脚本 {raw.get('id')!r} 不合规：" + "；".join(errs))
    turns = []
    for t in raw.get("turns") or []:
        sa = t.get("send_after")
        turns.append(
            L1Turn(
                user=t.get("user"),
                audio=t.get("audio"),
                send_after=SendAfter(sa["event"], int(sa.get("delay_ms", 0))) if sa else None,
                press=bool(t.get("press", False)),
                expect=[
                    Expect(
                        event=e["event"],
                        name=e.get("name"),
                        args_subset=dict(e.get("args_subset") or {}),
                        within_ms=e.get("within_ms"),
                        absent=bool(e.get("absent", False)),
                        closed_by=e.get("closed_by"),
                        value=e.get("value"),
                        eval=list(e.get("eval") or []),
                    )
                    for e in t.get("expect") or []
                ],
            )
        )
    return L1Script(
        id=raw["id"],
        profile=list(raw["profile"]),
        turns=turns,
        modality=list(raw.get("modality", ["text"])),
        race=bool(raw.get("race", False)),
        repeat=int(raw.get("repeat", 1)),
        mock_backend=raw.get("mock_backend"),
        setup=dict(raw.get("setup") or {}),
        assert_session={k: int(v) for k, v in (raw.get("assert_session") or {}).items()},
        mode=raw.get("mode", "scripted"),
        persona=raw.get("persona"),
        judge=raw.get("judge"),
        report=raw.get("report"),
    )


def load_script(path: str | Path) -> L1Script:
    with Path(path).open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return parse_script(raw)


# --- driver ------------------------------------------------------------------------------


class SessionDriver(Protocol):
    """A simulated client bound to one gateway session. Events returned by
    `wait_event` are dicts with at least `name`; latency events carry `at_ms`
    (T0-relative per anchors.primary); `function_call` carries `name`/`args`;
    `tool_closed` carries `closed_by`; `turn_terminal` carries `value`;
    `response` carries `text` (and optionally `heard_text`, `tool_calls`, `tool_results`)."""

    async def start(self) -> None: ...

    async def send_audio_file(self, path: str, realtime: bool = True) -> None: ...

    async def send_text(self, text: str) -> None: ...

    async def press(self) -> None: ...

    async def release(self) -> None: ...

    async def wait_event(self, name: str, timeout_ms: int) -> dict[str, Any] | None: ...

    def traces(self) -> list[dict[str, Any]]: ...


# --- results -----------------------------------------------------------------------------


@dataclass(slots=True)
class Failure:
    kind: str  # one of FAILURE_KINDS
    turn_index: int | None
    detail: str


@dataclass(slots=True)
class RunResult:
    failures: list[Failure] = field(default_factory=list)
    hard_violations: list[str] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures and not self.hard_violations


@dataclass(slots=True)
class L1Result:
    script_id: str
    race: bool
    runs: list[RunResult]
    modality: str

    @property
    def n(self) -> int:
        return len(self.runs)

    @property
    def pass_rate(self) -> float:
        return sum(1 for r in self.runs if r.passed) / self.n if self.n else 0.0

    @property
    def hard_violations(self) -> list[str]:
        return [v for r in self.runs for v in r.hard_violations]

    def failures_by_kind(self) -> Counter[str]:
        return Counter(f.kind for r in self.runs for f in r.failures)

    def gate(self, race_pass_rate: float = RACE_PASS_RATE, min_repeat: int = RACE_MIN_REPEAT) -> tuple[bool, str]:
        """spec 06 §8.2: race scripts by pass rate (N >= 10, >= 90 %); others
        all runs pass; hard metrics fail on a single occurrence."""
        if self.hard_violations:
            return False, "硬指标违规：" + "; ".join(self.hard_violations)
        if self.race:
            if self.n < min_repeat:
                return False, f"竞态脚本 N={self.n} < {min_repeat}，无通过率不作门禁"
            ok = self.pass_rate >= race_pass_rate
            return ok, f"通过率 {self.pass_rate:.0%}（门槛 {race_pass_rate:.0%}，N={self.n}）"
        ok = all(r.passed for r in self.runs)
        return ok, "全部通过" if ok else f"{sum(1 for r in self.runs if not r.passed)}/{self.n} 次失败"


# --- runner ---------------------------------------------------------------------------------


async def run_l1(
    script: L1Script,
    driver_factory: Callable[[], SessionDriver],
    repeat: int | None = None,
    modality: str = "text",
    judge: Judge | None = None,
    judge_info: JudgeInfo | None = None,
    rubrics: dict[str, Rubric] | None = None,
    aliases: AliasTable | None = None,
    default_timeout_ms: int = 10_000,
    clock: Clock | None = None,
) -> L1Result:
    if script.mode == "simulated":
        raise NotImplementedError("simulated-user scripts need an LLM user; use mode=scripted here")
    if modality not in script.modality:
        raise ValueError(f"脚本 {script.id} 不含 modality={modality}")
    clock = clock or MonotonicClock()
    n = repeat if repeat is not None else script.repeat
    runs: list[RunResult] = []
    for _ in range(n):
        driver = driver_factory()
        run = RunResult()
        try:
            await driver.start()
            for i, turn in enumerate(script.turns):
                await _run_turn(script, i, turn, driver, run, modality, judge, judge_info, rubrics or {},
                                aliases, default_timeout_ms, clock)
            run.hard_violations = check_session(driver.traces(), script.assert_session)
        except Exception as e:  # noqa: BLE001 - infra failure, not behaviour
            run.failures.append(Failure("infra", None, f"{type(e).__name__}: {e}"))
        runs.append(run)
    return L1Result(script_id=script.id, race=script.race, runs=runs, modality=modality)


async def _run_turn(
    script: L1Script,
    index: int,
    turn: L1Turn,
    driver: SessionDriver,
    run: RunResult,
    modality: str,
    judge: Judge | None,
    judge_info: JudgeInfo | None,
    rubrics: dict[str, Rubric],
    aliases: AliasTable | None,
    default_timeout_ms: int,
    clock: Clock,
) -> None:
    if turn.send_after is not None:
        anchor = await driver.wait_event(turn.send_after.event, default_timeout_ms)
        if anchor is None:
            run.failures.append(Failure("timeout", index, f"send_after 锚点 {turn.send_after.event} 未出现"))
            return
        if turn.send_after.delay_ms > 0:
            await clock.sleep(turn.send_after.delay_ms)
    if turn.press:
        await driver.press()
    if modality == "audio" and turn.audio:
        await driver.send_audio_file(turn.audio, realtime=True)  # spec 06 §7.4: real-time pacing
    elif turn.user is not None:
        await driver.send_text(turn.user)
    if turn.press:
        await driver.release()

    for exp in turn.expect:
        timeout = exp.within_ms if exp.within_ms is not None else default_timeout_ms
        ev = await driver.wait_event(exp.event, timeout)
        if exp.absent:
            if ev is not None:
                run.failures.append(Failure("unexpected_event", index, f"{exp.event} 在 {timeout} ms 内不应出现"))
            continue
        if ev is None:
            kind = "missing_function_call" if exp.event == "function_call" else "timeout"
            run.failures.append(Failure(kind, index, f"{exp.event} 在 {timeout} ms 内未出现"))
            continue
        if exp.event == "function_call":
            if exp.name and ev.get("name") != exp.name:
                run.failures.append(Failure("missing_function_call", index, f"期望 {exp.name}，得到 {ev.get('name')}"))
            elif exp.args_subset and not args_match(exp.args_subset, ev.get("args") or {}, aliases):
                run.failures.append(
                    Failure("missing_function_call", index, f"{exp.name} 参数 {ev.get('args')} ⊉ {exp.args_subset}")
                )
        elif exp.event in ("sound_first", "content_first"):
            at = ev.get("at_ms")
            if not isinstance(at, int) or at > exp.within_ms:  # type: ignore[operator]
                run.failures.append(Failure("latency_budget", index, f"{exp.event}={at} ms > {exp.within_ms} ms"))
        elif exp.event == "tool_closed":
            if ev.get("closed_by") != exp.closed_by:
                run.failures.append(
                    Failure("unexpected_event", index, f"tool_closed.closed_by={ev.get('closed_by')} ≠ {exp.closed_by}")
                )
        elif exp.event == "turn_terminal":
            if ev.get("value") != exp.value:
                run.failures.append(Failure("unexpected_event", index, f"turn_terminal={ev.get('value')} ≠ {exp.value}"))
        elif exp.event == "response" and exp.eval:
            inputs = RubricInputs(
                context=ev.get("context") or [],
                latest_bot_reply=ev.get("text") or "",
                tool_calls=ev.get("tool_calls") or [],
                tool_results=ev.get("tool_results") or [],
                heard_text=ev.get("heard_text") or ev.get("text") or "",
            )
            for rid in exp.eval:
                rubric = rubrics.get(rid)
                if rubric is None:
                    run.failures.append(Failure("infra", index, f"未知 rubric {rid}"))
                    continue
                if rubric.method == "judge" and (judge is None or judge_info is None):
                    run.failures.append(Failure("infra", index, f"rubric {rid} 需要 judge 但未提供"))
                    continue
                verdict = run_rubric(rubric, inputs, judge, judge_info, ev.get("session_id", ""),
                                     ev.get("turn_id", ""), script.profile[0])
                run.verdicts.append(verdict)
                if verdict["verdict"] == "no":
                    run.failures.append(Failure("judge_no", index, f"{rid}: {verdict.get('reason')}"))


def check_session(traces: list[dict[str, Any]], asserts: dict[str, int]) -> list[str]:
    """`assert_session` against trace records (spec 06 §5.2, §8.1 hard metrics)."""
    actual = {
        "stuck": sum(1 for t in traces if t.get("stuck") is True),
        "double_voice_count": sum(int(t.get("double_voice_count") or 0) for t in traces),
        "hanging_calls": sum(
            1
            for t in traces
            for c in (t.get("tool_calls") or [])
            if isinstance(c, dict) and (c.get("closed_by") is None or c.get("settled_at_ms") is None)
        ),
    }
    return [f"{k}={actual[k]} > {v}" for k, v in asserts.items() if actual[k] > v]


__all__ = [
    "FAILURE_KINDS",
    "HARD_METRICS",
    "RACE_MIN_REPEAT",
    "RACE_PASS_RATE",
    "SEND_AFTER_EVENTS",
    "Expect",
    "Failure",
    "L1Result",
    "L1Script",
    "L1Turn",
    "RunResult",
    "SendAfter",
    "SessionDriver",
    "check_session",
    "load_script",
    "parse_script",
    "run_l1",
    "validate_script",
]
