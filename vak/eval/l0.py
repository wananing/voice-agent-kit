"""L0 decision evaluation (spec 06 §5.1).

In-process, millisecond-scale: given state, history and user text, does the
decision layer produce the right action / tool / args / final state?
Metrics are named separately (`action_hit`, `slot_hit`, `no_tool_correct`,
`write_false_trigger`, `final_state_match`) and reported for the original
text and for each `asr_variants[].kind` separately. Args are compared by an
alias table, never by an LLM. Retry once only on "timeout with no call and
no text".
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ASR_VARIANT_KINDS = {"homophone", "truncation", "reduplication", "accent", "term_error"}
METRICS = ("action_hit", "slot_hit", "no_tool_correct", "write_false_trigger", "final_state_match")
ORIGINAL = "original"


# --- case format ---------------------------------------------------------------


@dataclass(slots=True)
class ExpectedCall:
    name: str
    args_subset: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AsrVariant:
    kind: str
    text: str


@dataclass(slots=True)
class L0Turn:
    user: str
    asr_variants: list[AsrVariant] = field(default_factory=list)
    expected_calls: list[ExpectedCall] = field(default_factory=list)
    expect_no_tool: bool = False
    forbidden_calls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class L0Case:
    id: str
    set: str
    profile: list[str]
    state: dict[str, Any]
    turns: list[L0Turn]
    history: list[dict[str, str]] = field(default_factory=list)
    tags: dict[str, Any] = field(default_factory=dict)
    expected_final_state: dict[str, Any] | None = None
    response_quality: dict[str, Any] | None = None


def validate_case(raw: dict[str, Any]) -> list[str]:
    errs = []
    for k in ("id", "set", "profile", "state", "turns"):
        if k not in raw:
            errs.append(f"缺必填字段 {k}")
    if "profile" in raw and (not isinstance(raw["profile"], list) or not set(raw["profile"]) <= {"A", "B"}):
        errs.append("profile 必须是 ['A'] / ['B'] / ['A','B']")
    if "state" in raw and not isinstance(raw["state"], dict):
        errs.append("state 必须是对象")
    if "turns" in raw:
        if not isinstance(raw["turns"], list) or not raw["turns"]:
            errs.append("turns 必须是非空数组")
        else:
            for i, t in enumerate(raw["turns"]):
                if not isinstance(t, dict) or "user" not in t:
                    errs.append(f"turns[{i}] 缺 user")
                    continue
                for v in t.get("asr_variants", []):
                    if not isinstance(v, dict) or v.get("kind") not in ASR_VARIANT_KINDS or "text" not in v:
                        errs.append(f"turns[{i}].asr_variants 项需 kind ∈ {sorted(ASR_VARIANT_KINDS)} 且带 text")
                for c in t.get("expected_calls", []):
                    if not isinstance(c, dict) or "name" not in c:
                        errs.append(f"turns[{i}].expected_calls 项缺 name")
                if t.get("expect_no_tool") and t.get("expected_calls"):
                    errs.append(f"turns[{i}] expect_no_tool 与 expected_calls 冲突")
    return errs


def parse_case(raw: dict[str, Any]) -> L0Case:
    errs = validate_case(raw)
    if errs:
        raise ValueError(f"L0 用例 {raw.get('id')!r} 不合规：" + "；".join(errs))
    turns = [
        L0Turn(
            user=t["user"],
            asr_variants=[AsrVariant(v["kind"], v["text"]) for v in t.get("asr_variants", [])],
            expected_calls=[ExpectedCall(c["name"], dict(c.get("args_subset", {}))) for c in t.get("expected_calls", [])],
            expect_no_tool=bool(t.get("expect_no_tool", False)),
            forbidden_calls=list(t.get("forbidden_calls", [])),
        )
        for t in raw["turns"]
    ]
    return L0Case(
        id=raw["id"],
        set=raw["set"],
        profile=list(raw["profile"]),
        state=dict(raw["state"]),
        turns=turns,
        history=list(raw.get("history", [])),
        tags=dict(raw.get("tags", {})),
        expected_final_state=raw.get("expected_final_state"),
        response_quality=raw.get("response_quality"),
    )


def load_cases(path: str | Path) -> list[L0Case]:
    cases = []
    with Path(path).open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path} 第 {lineno} 行 JSON 解析失败：{e}") from e
            cases.append(parse_case(raw))
    return cases


# --- decision interface -----------------------------------------------------------


@dataclass(slots=True)
class CallSpec:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Decision:
    """What the decision layer produced for one turn. `state` is the state
    after the turn (None = unchanged). `timeout=True` with no calls and no
    text is the only condition that is retried once."""

    calls: list[CallSpec] = field(default_factory=list)
    text: str | None = None
    state: dict[str, Any] | None = None
    timeout: bool = False


DecideFn = Callable[[dict[str, Any], list[dict[str, str]], str], Decision]


# --- arg comparison by alias table ------------------------------------------------

AliasTable = dict[str, str]  # alias (normalised) -> canonical (normalised)


def normalize(value: Any, aliases: AliasTable | None = None) -> Any:
    """Rule-based normalisation: strip + casefold strings, full-width -> ASCII,
    digits-as-strings -> int, then alias lookup. Never an LLM."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else value
    if isinstance(value, str):
        s = "".join(chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch for ch in value)
        s = s.strip().casefold()
        if aliases and s in aliases:
            s = aliases[s]
        if s.lstrip("-").isdigit():
            return int(s)
        return s
    if isinstance(value, list):
        return [normalize(v, aliases) for v in value]
    if isinstance(value, dict):
        return {k: normalize(v, aliases) for k, v in value.items()}
    return value


def args_match(expected_subset: dict[str, Any], actual: dict[str, Any], aliases: AliasTable | None = None) -> bool:
    for k, v in expected_subset.items():
        if k not in actual:
            return False
        if normalize(v, aliases) != normalize(actual[k], aliases):
            return False
    return True


def state_match(expected_subset: dict[str, Any], actual: dict[str, Any], aliases: AliasTable | None = None) -> bool:
    return args_match(expected_subset, actual, aliases)


# --- metrics ----------------------------------------------------------------------


@dataclass(slots=True)
class Ratio:
    num: int = 0
    den: int = 0

    def add(self, hit: bool) -> None:
        self.den += 1
        self.num += int(hit)

    @property
    def value(self) -> float | None:
        return None if self.den == 0 else self.num / self.den


@dataclass(slots=True)
class L0Metrics:
    action_hit: Ratio = field(default_factory=Ratio)
    slot_hit: Ratio = field(default_factory=Ratio)
    no_tool_correct: Ratio = field(default_factory=Ratio)
    write_false_trigger: Ratio = field(default_factory=Ratio)  # denominator: turns that must not trigger a write
    final_state_match: Ratio = field(default_factory=Ratio)

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {m: asdict(getattr(self, m)) for m in METRICS}


@dataclass(slots=True)
class TurnOutcome:
    case_id: str
    turn_index: int
    variant: str
    text: str
    decision: Decision | None
    retried: bool = False
    infra_error: str | None = None
    failures: list[str] = field(default_factory=list)


@dataclass(slots=True)
class L0Result:
    by_variant: dict[str, L0Metrics]  # "original" plus each asr_variants[].kind
    outcomes: list[TurnOutcome]
    infra_errors: int = 0
    retries: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "by_variant": {k: v.as_dict() for k, v in self.by_variant.items()},
                "infra_errors": self.infra_errors,
                "retries": self.retries,
                "failures": [
                    {"case": o.case_id, "turn": o.turn_index, "variant": o.variant, "failures": o.failures}
                    for o in self.outcomes
                    if o.failures or o.infra_error
                ],
            },
            ensure_ascii=False,
            indent=2,
        )


def _decide_with_retry(decide: DecideFn, state: dict[str, Any], history: list[dict[str, str]], text: str
                       ) -> tuple[Decision, bool]:
    d = decide(state, history, text)
    if d.timeout and not d.calls and not d.text:  # spec 06 §5.1 retry policy
        return decide(state, history, text), True
    return d, False


def run_l0(
    cases: list[L0Case],
    decide: DecideFn,
    aliases: AliasTable | None = None,
    profile: str | None = None,
) -> L0Result:
    """Run every case for the original text and once per ASR-variant kind
    present in the case (variant text substituted on turns that have it)."""
    by_variant: dict[str, L0Metrics] = {ORIGINAL: L0Metrics()}
    outcomes: list[TurnOutcome] = []
    infra = retries = 0

    for case in cases:
        if profile is not None and profile not in case.profile:
            continue
        kinds = [ORIGINAL] + sorted({v.kind for t in case.turns for v in t.asr_variants})
        for kind in kinds:
            metrics = by_variant.setdefault(kind, L0Metrics())
            state = dict(case.state)
            history = [dict(h) for h in case.history]
            aborted = False
            for i, turn in enumerate(case.turns):
                variant_text = next((v.text for v in turn.asr_variants if v.kind == kind), None)
                counts = kind == ORIGINAL or variant_text is not None
                text = variant_text if variant_text is not None else turn.user
                outcome = TurnOutcome(case.id, i, kind, text, None)
                try:
                    decision, retried = _decide_with_retry(decide, state, history, text)
                except Exception as e:  # noqa: BLE001 - infra failure recorded separately from behaviour
                    outcome.infra_error = f"{type(e).__name__}: {e}"
                    infra += 1
                    outcomes.append(outcome)
                    aborted = True
                    break
                outcome.decision, outcome.retried = decision, retried
                retries += int(retried)
                if counts:
                    _score_turn(turn, decision, metrics, outcome, aliases)
                outcomes.append(outcome)
                if decision.state is not None:
                    state = dict(decision.state)
                history.append({"user": text, "assistant": decision.text or ""})
            if case.expected_final_state is not None and not aborted and (kind == ORIGINAL or any(
                any(v.kind == kind for v in t.asr_variants) for t in case.turns
            )):
                hit = state_match(case.expected_final_state, state, aliases)
                metrics.final_state_match.add(hit)
                if not hit:
                    outcomes[-1].failures.append(f"final_state {state} != expected {case.expected_final_state}")
    return L0Result(by_variant=by_variant, outcomes=outcomes, infra_errors=infra, retries=retries)


def _score_turn(turn: L0Turn, d: Decision, m: L0Metrics, out: TurnOutcome, aliases: AliasTable | None) -> None:
    names = [c.name for c in d.calls]
    if turn.expected_calls:
        hit = all(any(c.name == e.name for c in d.calls) for e in turn.expected_calls)
        m.action_hit.add(hit)
        if not hit:
            out.failures.append(f"action: got {names}, expected {[e.name for e in turn.expected_calls]}")
        for e in turn.expected_calls:
            if not e.args_subset:
                continue
            matched = [c for c in d.calls if c.name == e.name]
            slot_ok = any(args_match(e.args_subset, c.args, aliases) for c in matched)
            m.slot_hit.add(slot_ok)
            if not slot_ok:
                out.failures.append(f"slots for {e.name}: got {[c.args for c in matched]}, expected ⊇ {e.args_subset}")
    if turn.expect_no_tool:
        ok = not d.calls
        m.no_tool_correct.add(ok)
        if not ok:
            out.failures.append(f"expected no tool, got {names}")
    if turn.forbidden_calls:
        triggered = [n for n in names if n in turn.forbidden_calls]
        m.write_false_trigger.add(bool(triggered))
        if triggered:
            out.failures.append(f"forbidden call(s) triggered: {triggered}")


# --- CI delta check ------------------------------------------------------------------


@dataclass(slots=True)
class CompareResult:
    ok: bool
    failures: list[str]
    deltas_pp: dict[str, dict[str, float]]  # variant -> metric -> (current - baseline) in percentage points


def _metrics_table(result: L0Result | dict[str, Any]) -> dict[str, dict[str, float | None]]:
    if isinstance(result, L0Result):
        return {k: {m: getattr(v, m).value for m in METRICS} for k, v in result.by_variant.items()}
    table: dict[str, dict[str, float | None]] = {}
    for k, metrics in result.items():
        table[k] = {}
        for m in METRICS:
            r = metrics.get(m)
            if isinstance(r, dict):
                table[k][m] = None if not r.get("den") else r["num"] / r["den"]
            else:
                table[k][m] = r
    return table


def compare(baseline: L0Result | dict[str, Any], current: L0Result | dict[str, Any], max_drop_pp: float = 2.0
            ) -> CompareResult:
    """CI gate (spec 06 §8.1): each named metric may drop at most `max_drop_pp`
    percentage points per variant; `write_false_trigger` must not rise at all."""
    base, cur = _metrics_table(baseline), _metrics_table(current)
    failures: list[str] = []
    deltas: dict[str, dict[str, float]] = {}
    for variant, bm in base.items():
        cm = cur.get(variant)
        if cm is None:
            continue
        for metric in METRICS:
            b, c = bm.get(metric), cm.get(metric)
            if b is None or c is None:
                continue
            delta = (c - b) * 100.0
            deltas.setdefault(variant, {})[metric] = round(delta, 3)
            if metric == "write_false_trigger":
                if delta > 1e-9:
                    failures.append(f"{variant}.write_false_trigger 上升 {delta:.1f} pp（不允许上升）")
            elif delta < -max_drop_pp - 1e-9:
                failures.append(f"{variant}.{metric} 下降 {-delta:.1f} pp（> {max_drop_pp} pp）")
    return CompareResult(ok=not failures, failures=failures, deltas_pp=deltas)


__all__ = [
    "ASR_VARIANT_KINDS",
    "METRICS",
    "ORIGINAL",
    "AliasTable",
    "AsrVariant",
    "CallSpec",
    "CompareResult",
    "DecideFn",
    "Decision",
    "ExpectedCall",
    "L0Case",
    "L0Metrics",
    "L0Result",
    "L0Turn",
    "Ratio",
    "TurnOutcome",
    "args_match",
    "compare",
    "load_cases",
    "normalize",
    "parse_case",
    "run_l0",
    "state_match",
    "validate_case",
]
