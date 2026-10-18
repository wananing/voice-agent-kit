"""Judge rubrics (spec 06 §6): YAML loader, prompt builder, judge runner.

Per-turn binary verdicts (`yes` / `no` / `not_applicable` / `continue`),
reason only on `no`, judge metadata (model, prompt_version, temperature 0)
in every output. `heard_text` is what the user actually heard: truncated at
`played_ms`, `[interrupted]` marker where cut. Quantifiable rubrics
(`brevity`) are rule-based.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

VERDICTS = ("yes", "no", "not_applicable", "continue")
RUBRIC_INPUTS = {"context", "latest_bot_reply", "tool_calls", "tool_results", "heard_text"}
RUBRICS_DIR = Path(__file__).resolve().parent / "rubrics"
PROMPT_VERSION = "judge@1"


@dataclass(slots=True)
class Rubric:
    rubric_id: str
    version: int
    criterion: str
    options: dict[str, str]
    applies_to: list[str] = field(default_factory=lambda: ["A", "B"])
    scope: str = "per_turn"  # per_turn | session
    inputs: list[str] = field(default_factory=lambda: ["context", "latest_bot_reply", "heard_text"])
    leniency: list[str] = field(default_factory=list)
    reason_required_for: list[str] = field(default_factory=lambda: ["no"])
    pair_with: str | None = None
    method: str = "judge"  # judge | rule
    rule: dict[str, Any] = field(default_factory=dict)


def _yaml_key(k: Any) -> Any:
    """YAML 1.1 reads bare `yes` / `no` as booleans; the spec template uses them as option keys."""
    if k is True:
        return "yes"
    if k is False:
        return "no"
    return k


def _normalize_raw(raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    if isinstance(out.get("options"), dict):
        out["options"] = {_yaml_key(k): v for k, v in out["options"].items()}
    if isinstance(out.get("reason_required_for"), list):
        out["reason_required_for"] = [_yaml_key(k) for k in out["reason_required_for"]]
    return out


def validate_rubric(raw: dict[str, Any]) -> list[str]:
    errs = []
    if not isinstance(raw, dict):
        return ["rubric 必须是映射"]
    raw = _normalize_raw(raw)
    for k in ("rubric_id", "version", "criterion", "options"):
        if k not in raw:
            errs.append(f"缺必填字段 {k}")
    if "options" in raw:
        if not isinstance(raw["options"], dict) or not {"yes", "no"} <= set(raw["options"]):
            errs.append("options 至少含 yes 与 no")
        elif not set(raw["options"]) <= set(VERDICTS):
            errs.append(f"options 只能含 {list(VERDICTS)}")
    if raw.get("scope", "per_turn") not in ("per_turn", "session"):
        errs.append("scope 只能取 per_turn / session")
    if not set(raw.get("inputs", [])) <= RUBRIC_INPUTS:
        errs.append(f"inputs 只能含 {sorted(RUBRIC_INPUTS)}")
    if raw.get("method", "judge") not in ("judge", "rule"):
        errs.append("method 只能取 judge / rule")
    if raw.get("method") == "rule" and not isinstance(raw.get("rule"), dict):
        errs.append("method=rule 需 rule 映射")
    if not set(raw.get("applies_to", ["A", "B"])) <= {"A", "B"}:
        errs.append("applies_to 只能含 A / B")
    return errs


def parse_rubric(raw: dict[str, Any]) -> Rubric:
    errs = validate_rubric(raw)
    if errs:
        raise ValueError(f"rubric {raw.get('rubric_id')!r} 不合规：" + "；".join(errs))
    raw = _normalize_raw(raw)
    return Rubric(
        rubric_id=raw["rubric_id"],
        version=int(raw["version"]),
        criterion=str(raw["criterion"]).strip(),
        options={k: str(v).strip() for k, v in raw["options"].items()},
        applies_to=list(raw.get("applies_to", ["A", "B"])),
        scope=raw.get("scope", "per_turn"),
        inputs=list(raw.get("inputs", ["context", "latest_bot_reply", "heard_text"])),
        leniency=list(raw.get("leniency", [])),
        reason_required_for=list(raw.get("reason_required_for", ["no"])),
        pair_with=raw.get("pair_with"),
        method=raw.get("method", "judge"),
        rule=dict(raw.get("rule") or {}),
    )


def load_rubric(path: str | Path) -> Rubric:
    with Path(path).open(encoding="utf-8") as f:
        return parse_rubric(yaml.safe_load(f))


def load_builtin_rubrics() -> dict[str, Rubric]:
    """The base set (spec 06 §6.3): understanding, honesty, recovery,
    efficiency, brevity, task_completion, tone_consistency."""
    out = {}
    for p in sorted(RUBRICS_DIR.glob("*.yaml")):
        r = load_rubric(p)
        out[r.rubric_id] = r
    return out


# --- inputs ---------------------------------------------------------------------------


@dataclass(slots=True)
class SpokenSegment:
    """One sentence as generated and as played; `played_ms=None` = unknown."""

    text: str
    generated_ms: int | None = None
    played_ms: int | None = None
    interrupted: bool = False


def heard_text(segments: list[SpokenSegment]) -> str:
    """spec 06 §5.3: text the user actually heard, truncated at played_ms
    (proportionally by character), `[interrupted]` where cut."""
    parts = []
    for seg in segments:
        text = seg.text
        if seg.played_ms is not None and seg.generated_ms:
            frac = max(0.0, min(1.0, seg.played_ms / seg.generated_ms))
            text = text[: int(round(len(text) * frac))]
        if seg.interrupted or (
            seg.played_ms is not None and seg.generated_ms is not None and seg.played_ms < seg.generated_ms
        ):
            text = text.rstrip() + " [interrupted]"
            parts.append(text)
            break
        if text:
            parts.append(text)
    return "".join(parts).strip()


@dataclass(slots=True)
class RubricInputs:
    context: list[dict[str, str]] = field(default_factory=list)  # prior turns [{user, assistant}]
    latest_bot_reply: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    heard_text: str = ""


@dataclass(slots=True)
class JudgeInfo:
    model: str
    prompt_version: str = PROMPT_VERSION
    temperature: float = 0.0  # fixed (spec 06 §6.1 rule 11)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Judge(Protocol):
    def judge(self, prompt: str) -> str: ...


def build_prompt(rubric: Rubric, inputs: RubricInputs) -> str:
    lines = [
        f"你是评审。按下面的标准对 latest_bot_reply 做逐轮二元判定（rubric={rubric.rubric_id} v{rubric.version}）。",
        "",
        "标准：",
        rubric.criterion,
        "",
        "可选结论（只能选一个）：",
    ]
    for k in VERDICTS:
        if k in rubric.options:
            lines.append(f"- {k}: {rubric.options[k]}")
    if rubric.leniency:
        lines += ["", "宽容规则："] + [f"- {x}" for x in rubric.leniency]
    lines += ["", "输入："]
    if "context" in rubric.inputs:
        ctx = "\n".join(f"  用户：{t.get('user', '')}\n  助手：{t.get('assistant', '')}" for t in inputs.context)
        lines.append("context:\n" + (ctx or "  （无）"))
    if "tool_calls" in rubric.inputs:
        lines.append(f"tool_calls: {inputs.tool_calls}")
    if "tool_results" in rubric.inputs:
        lines.append(f"tool_results: {inputs.tool_results}")
    if "heard_text" in rubric.inputs:
        lines.append(f"heard_text（用户实际听到的）: {inputs.heard_text}")
    lines.append(f"latest_bot_reply: {inputs.latest_bot_reply}")
    lines += [
        "",
        "输出两行，格式固定：",
        "verdict: <yes|no|not_applicable|continue>",
        "reason: <只有 verdict 为 no 时写一句理由，否则留空>",
    ]
    return "\n".join(lines)


_VERDICT_RE = re.compile(r"verdict\s*[:：]\s*(yes|no|not_applicable|continue)", re.IGNORECASE)
_REASON_RE = re.compile(r"reason\s*[:：]\s*(.*)", re.IGNORECASE | re.DOTALL)


def parse_verdict(text: str) -> tuple[str | None, str | None]:
    m = _VERDICT_RE.search(text)
    verdict = m.group(1).lower() if m else None
    r = _REASON_RE.search(text)
    reason = r.group(1).strip().splitlines()[0].strip() if r and r.group(1).strip() else None
    return verdict, reason


# --- rule-based: brevity ----------------------------------------------------------------

_SENTENCE_END = re.compile(r"[。！？!?；;…]+|\.(?=\s|$)|\n+")


def sentence_count(text: str) -> int:
    parts = [p for p in _SENTENCE_END.split(text.strip()) if p and p.strip()]
    return len(parts)


def _rule_verdict(rubric: Rubric, inputs: RubricInputs, profile: str) -> tuple[str, str | None]:
    if rubric.rubric_id == "brevity" or rubric.rule.get("kind") == "max_sentences":
        limits = rubric.rule.get("max_sentences", {"A": 2, "B": 3})  # spec 06 §6.3 suggested values
        limit = int(limits.get(profile, max(limits.values())))
        text = inputs.heard_text or inputs.latest_bot_reply
        if not text.strip():
            return "not_applicable", None
        n = sentence_count(text)
        if n <= limit:
            return "yes", None
        return "no", f"{n} 句 > {limit} 句（profile {profile}）"
    raise ValueError(f"rubric {rubric.rubric_id} 的规则未实现：{rubric.rule}")


# --- runner --------------------------------------------------------------------------------


def run_rubric(
    rubric: Rubric,
    inputs: RubricInputs,
    judge: Judge | None,
    judge_info: JudgeInfo | None,
    session_id: str,
    turn_id: str,
    profile: str = "A",
) -> dict[str, Any]:
    """One verdict record (spec 06 §6.2 output format)."""
    if profile not in rubric.applies_to:
        verdict, reason = "not_applicable", None
        info = JudgeInfo(model="rule", prompt_version=f"{rubric.rubric_id}@{rubric.version}")
    elif rubric.method == "rule":
        verdict, reason = _rule_verdict(rubric, inputs, profile)
        info = JudgeInfo(model="rule", prompt_version=f"{rubric.rubric_id}@{rubric.version}")
    else:
        if judge is None or judge_info is None:
            raise ValueError(f"rubric {rubric.rubric_id} 需要 judge")
        info = JudgeInfo(model=judge_info.model, prompt_version=judge_info.prompt_version, temperature=0.0)
        raw = judge.judge(build_prompt(rubric, inputs))
        verdict, reason = parse_verdict(raw)
        if verdict is None:
            verdict, reason = "no", f"judge 输出无法解析：{raw[:120]!r}"
    out: dict[str, Any] = {
        "session_id": session_id,
        "turn_id": turn_id,
        "rubric_id": rubric.rubric_id,
        "rubric_version": rubric.version,
        "verdict": verdict,
        "judge": info.as_dict(),
    }
    if verdict in rubric.reason_required_for:
        out["reason"] = reason or ""
    return out


__all__ = [
    "PROMPT_VERSION",
    "RUBRICS_DIR",
    "RUBRIC_INPUTS",
    "VERDICTS",
    "Judge",
    "JudgeInfo",
    "Rubric",
    "RubricInputs",
    "SpokenSegment",
    "build_prompt",
    "heard_text",
    "load_builtin_rubrics",
    "load_rubric",
    "parse_rubric",
    "parse_verdict",
    "run_rubric",
    "sentence_count",
    "validate_rubric",
]
