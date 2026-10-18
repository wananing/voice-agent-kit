"""Validate turn traces (JSONL) and summarise the two first-audio metrics (spec 06 §2–§4).

Library: `check(paths) -> Report`, `summarize(records) -> Summary`,
`format_table(summary)`. CLI: `python -m vak.eval.trace_check <trace.jsonl>... [--json] [--quiet]`.

Checks per record: JSON Schema (schemas/trace.schema.json), (session_id,
turn_id) uniqueness, time monotonicity, first-audio semantics (content >=
sound; content_kind=none -> content_first null), terminal enums
(turn_closed_by per turn_terminal), hanging tool calls, derived-field
consistency. Summary groups by turn_kind x anchors.primary x clock_quality;
failed / discarded turns are excluded from the latency distribution and
reported as a rate; null counts as missing; "样本不足" when n is below
`TraceParams.min_n_p50` / `min_n_p90`.
"""

from __future__ import annotations

import json
import math
import sys
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from vak.core.defaults import DEFAULTS, TraceParams
from vak.core.trace.schema import validate as schema_validate

INTERRUPT_SOURCES = {"button", "vad", "wake", "text", "link", "critical", "guardrail"}
ERROR_CODES = {
    "no_speech", "asr_timeout", "upstream_unavailable", "upstream_timeout", "upstream_rejected", "tts_failed",
    "audio_gap", "link_lost", "protocol_error", "session_expired", "internal",
}
CLOSED_BY_FOR_TERMINAL = {
    "completed": {"playback_ended", "T_drain"},
    "discarded": {"short_press", "short_speech", "false_wake", "rule_silence"},
    "interrupted": INTERRUPT_SOURCES,
    "failed": ERROR_CODES,
}
PRIMARY_FIELD = {
    "release": "anchors.release_at_ms",
    "endpoint": "anchors.endpoint_at_ms",
    "audio_end": "anchors.audio_end_at_ms",
    "tap": "anchors.tap_at_ms",
    "event": "anchors.event_at_ms",
}
NOT_IN_DISTRIBUTION = {"failed", "discarded"}

_MISSING = object()


def get(rec: Any, path: str) -> Any:
    cur = rec
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return _MISSING
        cur = cur[key]
    return cur


def is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def num(rec: Any, path: str) -> int | None:
    v = get(rec, path)
    return v if is_int(v) else None


def turn_group(kind: Any) -> str:
    if kind == "chat":
        return "普通"
    if isinstance(kind, str) and kind.startswith("tool_"):
        return "工具"
    return "其他"


# --- validation ------------------------------------------------------------------


def validate_record(rec: dict[str, Any]) -> list[str]:
    """Schema + spec 06 §3 rule checks. Empty list means the record is sound."""
    errs = schema_validate(rec)

    # terminal enums: turn_closed_by must match turn_terminal (spec 06 §3.9)
    term, tcb = rec.get("turn_terminal"), rec.get("turn_closed_by")
    if term in CLOSED_BY_FOR_TERMINAL and tcb is not None and tcb not in CLOSED_BY_FOR_TERMINAL[term]:
        errs.append(f"turn_terminal={term} 时 turn_closed_by={tcb!r} 不合规，应取 {sorted(CLOSED_BY_FOR_TERMINAL[term])}")

    # hanging calls (spec 06 §3.5 / rule 6)
    calls = rec.get("tool_calls") or []
    if isinstance(calls, list):
        for i, c in enumerate(calls):
            if not isinstance(c, dict):
                continue
            if c.get("closed_by") is None or not is_int(c.get("settled_at_ms")):
                errs.append(f"tool_calls[{i}] 悬挂：缺 closed_by 或 settled_at_ms")
            s, d, st = c.get("started_at_ms"), c.get("done_at_ms"), c.get("settled_at_ms")
            if is_int(s) and is_int(d) and s > d:
                errs.append(f"tool_calls[{i}] started_at_ms > done_at_ms")
            if is_int(d) and is_int(st) and d > st:
                errs.append(f"tool_calls[{i}] done_at_ms > settled_at_ms")
        if turn_group(rec.get("turn_kind")) == "工具" and not calls:
            errs.append("工具回合 tool_calls 为空")

    # first-audio semantics (spec 06 §2.3)
    sf, cf = get(rec, "latency.sound_first_at_ms"), get(rec, "latency.content_first_at_ms")
    if is_int(sf) and is_int(cf) and cf < sf:
        errs.append(f"content_first_at_ms({cf}) < sound_first_at_ms({sf})")
    if get(rec, "latency.content_kind") == "none" and cf is not _MISSING and cf is not None:
        errs.append("content_kind=none 时 content_first_at_ms 应为 null")
    if is_int(sf) and sf < 0:
        errs.append(f"sound_first_at_ms({sf}) 早于 T0")

    # time monotonicity (spec 06 §2.1, §2.4)
    primary = get(rec, "anchors.primary")
    if primary in PRIMARY_FIELD:
        v = get(rec, PRIMARY_FIELD[primary])
        if v is _MISSING:
            errs.append(f"缺主锚点字段 {PRIMARY_FIELD[primary]}")
        elif is_int(v) and v != 0:
            errs.append(f"主锚点 {PRIMARY_FIELD[primary]} 应为 0，实际 {v}")
    if rec.get("turn_source") in ("button", "wake", "vad") and get(rec, "anchors.audio_end_at_ms") is _MISSING:
        errs.append("有音频输入的回合缺 anchors.audio_end_at_ms")

    def le(a_path: str, b_path: str) -> None:
        a, b = num(rec, a_path), num(rec, b_path)
        if a is not None and b is not None and a > b:
            errs.append(f"时间不单调：{a_path}({a}) > {b_path}({b})")

    le("anchors.audio_end_at_ms", "anchors.release_at_ms")
    le("anchors.audio_end_at_ms", "anchors.endpoint_at_ms")
    le("latency.content_first_ready_at_ms", "latency.content_first_at_ms")
    le("latency.pre_tool_ms", "latency.tool_done_at_ms")
    le("latency.tool_done_at_ms", "latency.output_sent_at_ms")
    le("latency.tool_done_at_ms", "latency.content_first_at_ms")
    le("latency.sound_first_at_ms", "latency.sound_first_played_at_ms")
    le("latency.content_first_at_ms", "latency.content_first_played_at_ms")
    le("handoff_start_at_ms", "handoff_done_at_ms")

    # derived-field consistency (spec 06 §2.2, §2.3)
    def derived(name: str, a_path: str, b_path: str) -> None:
        got, a, b = num(rec, name), num(rec, a_path), num(rec, b_path)
        if got is not None and a is not None and b is not None and got != a - b:
            errs.append(f"{name}={got} 与 {a_path} - {b_path} = {a - b} 不一致")

    derived("latency.ptt_tail_silence_ms", "anchors.release_at_ms", "anchors.audio_end_at_ms")
    derived("latency.endpoint_ms", "anchors.endpoint_at_ms", "anchors.audio_end_at_ms")
    derived("latency.queue_wait_ms", "latency.content_first_at_ms", "latency.content_first_ready_at_ms")
    derived("latency.post_tool_ms", "latency.content_first_at_ms", "latency.tool_done_at_ms")
    return errs


@dataclass(slots=True)
class Invalid:
    source: str
    lineno: int
    turn_id: str | None
    errors: list[str]


@dataclass(slots=True)
class Report:
    total: int = 0
    valid: list[dict[str, Any]] = field(default_factory=list)
    invalid: list[Invalid] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.invalid


def iter_jsonl(path: str | Path) -> list[tuple[int, str]]:
    with Path(path).open(encoding="utf-8") as f:
        return [(n, line.strip()) for n, line in enumerate(f, 1) if line.strip() and not line.lstrip().startswith("#")]


def check_records(records: list[dict[str, Any]], source: str = "<memory>") -> Report:
    report = Report()
    seen: set[tuple[Any, Any]] = set()
    for i, rec in enumerate(records, 1):
        report.total += 1
        errs = validate_record(rec) if isinstance(rec, dict) else ["一行必须是一个 JSON 对象"]
        if isinstance(rec, dict):
            key = (rec.get("session_id"), rec.get("turn_id"))
            if key in seen:
                errs.append(f"(session_id, turn_id)={key!r} 重复")
            seen.add(key)
        if errs:
            report.invalid.append(Invalid(source, i, rec.get("turn_id") if isinstance(rec, dict) else None, errs))
        else:
            report.valid.append(rec)
    return report


def check(paths: list[str | Path] | str | Path) -> Report:
    if isinstance(paths, (str, Path)):
        paths = [paths]
    report = Report()
    seen: set[tuple[Any, Any]] = set()
    for p in paths:
        for lineno, line in iter_jsonl(p):
            report.total += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                report.invalid.append(Invalid(str(p), lineno, None, [f"JSON 解析失败：{e}"]))
                continue
            errs = validate_record(rec) if isinstance(rec, dict) else ["一行必须是一个 JSON 对象"]
            tid = None
            if isinstance(rec, dict):
                tid = rec.get("turn_id")
                key = (rec.get("session_id"), tid)
                if key in seen:
                    errs.append(f"(session_id, turn_id)={key!r} 重复")
                seen.add(key)
            if errs:
                report.invalid.append(Invalid(str(p), lineno, tid, errs))
            else:
                report.valid.append(rec)
    return report


# --- summary ---------------------------------------------------------------------


def percentile(values: list[int], p: float) -> int:
    """Nearest-rank percentile."""
    s = sorted(values)
    return s[max(0, math.ceil(p / 100.0 * len(s)) - 1)]


@dataclass(slots=True)
class MetricRow:
    turn_group: str
    primary: str | None
    clock_quality: str | None
    metric: str
    turns: int
    failed_rate: float
    n: int
    missing: int
    missing_rate: float
    p50_ms: int | None
    p90_ms: int | None
    p50_insufficient: bool
    p90_insufficient: bool

    @property
    def notes(self) -> str:
        parts = []
        if self.p50_insufficient:
            parts.append("P50 样本不足")
        if self.p90_insufficient:
            parts.append("P90 样本不足")
        return "，".join(parts)


@dataclass(slots=True)
class Summary:
    rows: list[MetricRow]
    hard_metrics: dict[str, int]  # stuck_turns, double_voice_turns, hanging_calls (spec 06 §8 gate items)
    total: int
    segments: list[dict[str, Any]] = field(default_factory=list)  # spec 06 §2.4 seg.* over normal turns

    def to_json(self) -> str:
        return json.dumps(
            {
                "total": self.total,
                "hard_metrics": self.hard_metrics,
                "rows": [{**asdict(r), "notes": r.notes} for r in self.rows],
                "segments": self.segments,
            },
            ensure_ascii=False,
            indent=2,
        )


def summarize(records: list[dict[str, Any]], params: TraceParams = DEFAULTS.trace) -> Summary:
    groups: OrderedDict[tuple[str, Any, Any], list[dict[str, Any]]] = OrderedDict()
    order = {"普通": 0, "工具": 1, "其他": 2}
    for rec in sorted(records, key=lambda r: order[turn_group(r.get("turn_kind"))]):
        key = (turn_group(rec.get("turn_kind")), get(rec, "anchors.primary"), get(rec, "anchors.clock_quality"))
        key = (key[0], None if key[1] is _MISSING else key[1], None if key[2] is _MISSING else key[2])
        groups.setdefault(key, []).append(rec)

    rows: list[MetricRow] = []
    for (grp, anchor, quality), recs in groups.items():
        total = len(recs)
        failed = sum(1 for r in recs if r.get("turn_terminal") == "failed")
        eligible = [r for r in recs if r.get("turn_terminal") not in NOT_IN_DISTRIBUTION]
        for label, path, skip_none in (
            ("有声音", "latency.sound_first_at_ms", False),
            ("有内容", "latency.content_first_at_ms", True),
        ):
            pool = [r for r in eligible if not (skip_none and get(r, "latency.content_kind") == "none")]
            vals = [get(r, path) for r in pool]
            ok = [v for v in vals if is_int(v)]
            missing = len(vals) - len(ok)
            rows.append(
                MetricRow(
                    turn_group=grp,
                    primary=anchor,
                    clock_quality=quality,
                    metric=label,
                    turns=total,
                    failed_rate=failed / total if total else 0.0,
                    n=len(ok),
                    missing=missing,
                    missing_rate=missing / len(vals) if vals else 0.0,
                    p50_ms=percentile(ok, 50) if ok else None,
                    p90_ms=percentile(ok, 90) if ok else None,
                    p50_insufficient=len(ok) < params.min_n_p50,
                    p90_insufficient=len(ok) < params.min_n_p90,
                )
            )

    hard = {
        "stuck_turns": sum(1 for r in records if r.get("stuck") is True),
        "double_voice_turns": sum(1 for r in records if is_int(r.get("double_voice_count")) and r["double_voice_count"] > 0),
        "hanging_calls": sum(
            1
            for r in records
            for c in (r.get("tool_calls") or [])
            if isinstance(c, dict) and (c.get("closed_by") is None or not is_int(c.get("settled_at_ms")))
        ),
    }
    return Summary(rows=rows, hard_metrics=hard, total=len(records), segments=_segments(records))


# spec 06 §2.4: where first audio goes, normal turns only (tool turns add pre/post-tool time)
SEGMENTS = (
    ("stt_first_word_ms", "首个语音帧 → 首个转写词"),
    ("vad_ms", "说完 → 发起回复请求（判停）"),
    ("llm_ms", "发起请求 → LLM / S2S 首个输出"),
    ("first_segment_ms", "LLM 首词 → 首段送进 TTS"),
    ("tts_start_ms", "LLM 首词 → 首个音频包"),
)


def _segments(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normal = [r for r in records if turn_group(r.get("turn_kind")) == "普通" and r.get("turn_terminal") not in NOT_IN_DISTRIBUTION]
    out = []
    for name, label in SEGMENTS:
        vals = [v for r in normal if is_int(v := get(r, f"latency.seg.{name}"))]
        if vals:
            out.append({"segment": name, "label": label, "n": len(vals), "p50_ms": percentile(vals, 50), "p90_ms": percentile(vals, 90)})
    return out


def _width(s: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)


def format_table(summary: Summary) -> str:
    header = ["类型", "锚点", "时钟", "指标", "回合", "失败率", "n", "缺失", "P50(ms)", "P90(ms)", "备注"]
    rows = [
        [
            r.turn_group,
            str(r.primary),
            str(r.clock_quality),
            r.metric,
            str(r.turns),
            f"{100.0 * r.failed_rate:.0f}%",
            str(r.n),
            str(r.missing),
            "-" if r.p50_ms is None else str(r.p50_ms),
            "-" if r.p90_ms is None else str(r.p90_ms),
            r.notes,
        ]
        for r in summary.rows
    ]
    cols = list(zip(*([header] + rows), strict=True))
    widths = [max(_width(c) for c in col) for col in cols]
    lines = []
    for i, r in enumerate([header] + rows):
        lines.append("  ".join(c + " " * (w - _width(c)) for c, w in zip(r, widths, strict=True)).rstrip())
        if i == 0:
            lines.append("  ".join("-" * w for w in widths))
    if summary.segments:
        lines.append("")
        lines.append("分段（普通回合；tts_start − first_segment ≈ TTS 首包）")
        lw = max(_width(sg["label"]) for sg in summary.segments)
        for sg in summary.segments:
            lines.append(
                f"  {sg['label']}{' ' * (lw - _width(sg['label']))}  n={sg['n']:<4} P50={sg['p50_ms']:<6} P90={sg['p90_ms']}"
            )
    h = summary.hard_metrics
    lines.append("")
    lines.append(
        f"硬指标：stuck 回合 {h['stuck_turns']}，双声回合 {h['double_voice_turns']}，悬挂调用 {h['hanging_calls']}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    paths = [a for a in argv if not a.startswith("--")]
    quiet, as_json = "--quiet" in argv, "--json" in argv
    if not paths:
        print("用法：python -m vak.eval.trace_check <trace.jsonl>... [--json] [--quiet]", file=sys.stderr)
        return 2
    try:
        report = check(paths)
    except OSError as e:
        print(f"读取失败：{e}", file=sys.stderr)
        return 2
    summary = summarize(report.valid)
    if as_json:
        print(
            json.dumps(
                {
                    "total": report.total,
                    "valid": len(report.valid),
                    "invalid": [asdict(i) for i in report.invalid],
                    "summary": json.loads(summary.to_json()),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        if not quiet:
            for inv in report.invalid:
                print(f"{inv.source} 第 {inv.lineno} 行（turn_id={inv.turn_id}）：")
                for e in inv.errors:
                    print("  - " + e)
        print(f"校验：{report.total} 条 trace，通过 {len(report.valid)}，不通过 {len(report.invalid)}")
        if report.valid:
            print()
            print("首音汇总（仅通过校验的 trace；failed / discarded 不进分布；最近秩分位数）")
            print(format_table(summary))
    return 1 if report.invalid else 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "Invalid",
    "MetricRow",
    "Report",
    "Summary",
    "check",
    "check_records",
    "format_table",
    "main",
    "percentile",
    "summarize",
    "validate_record",
]
