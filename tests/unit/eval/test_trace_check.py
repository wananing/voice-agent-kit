"""trace_check: schema + rule validation, summary grouping, 样本不足 marks (spec 06 §4.2, §10.1 item 8)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from vak.core.defaults import TraceParams
from vak.eval import trace_check as tc

from ..trace.conftest import SAMPLE_NAMES, load_sample


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return path


def test_samples_pass_validation() -> None:
    for name in SAMPLE_NAMES:
        assert tc.validate_record(load_sample(name)) == [], name


def test_validate_catches_rule_violations() -> None:
    rec = load_sample("chat")
    rec["turn_closed_by"] = "short_press"  # completed turn cannot close by a discard reason
    assert any("turn_closed_by" in e for e in tc.validate_record(rec))

    rec = load_sample("tool")
    rec["tool_calls"][0]["closed_by"] = None
    assert any("悬挂" in e for e in tc.validate_record(rec))

    rec = load_sample("chat")
    rec["latency"]["content_first_at_ms"] = 700  # < sound_first 880
    assert any("content_first_at_ms(700) < sound_first_at_ms(880)" in e for e in tc.validate_record(rec))

    rec = load_sample("chat")
    rec["anchors"]["release_at_ms"] = 5  # primary anchor must be 0
    assert any("主锚点" in e for e in tc.validate_record(rec))

    rec = load_sample("tool")
    rec["latency"]["post_tool_ms"] = 1  # derived mismatch
    assert any("post_tool_ms" in e for e in tc.validate_record(rec))

    rec = load_sample("chat")
    rec["latency"]["sound_first_played_at_ms"] = 10  # played before sent
    assert any("时间不单调" in e for e in tc.validate_record(rec))

    rec = load_sample("error")
    rec["latency"]["content_first_at_ms"] = 3100  # content_kind=none
    assert any("content_kind=none" in e for e in tc.validate_record(rec))


def test_check_files_duplicates_and_bad_json(tmp_path: Path) -> None:
    a, b = load_sample("chat"), load_sample("tool")
    p = _write_jsonl(tmp_path / "t.jsonl", [a, b, a])
    with p.open("a", encoding="utf-8") as f:
        f.write("# comment\n{not json\n")
    report = tc.check([p])
    assert report.total == 4 and len(report.valid) == 2
    kinds = [e for inv in report.invalid for e in inv.errors]
    assert any("重复" in e for e in kinds) and any("JSON 解析失败" in e for e in kinds)
    assert report.invalid[0].lineno == 3


def test_summary_groups_and_sample_insufficiency() -> None:
    chat = load_sample("chat")
    recs = []
    for i in range(25):
        r = copy.deepcopy(chat)
        r["turn_id"] = f"c{i}"
        r["latency"]["sound_first_at_ms"] = 800 + i * 10
        r["latency"]["content_first_at_ms"] = 800 + i * 10
        recs.append(r)
    # one failed chat turn with an error cue; one with lost receipt (null) ; one tool; one from a synced clock
    failed = load_sample("error")
    failed["turn_kind"] = "chat"
    failed["anchors"]["clock_quality"] = "rx_minus_half_rtt"
    recs.append(failed)
    lost = copy.deepcopy(chat)
    lost["turn_id"] = "c99"
    lost["latency"]["sound_first_at_ms"] = None
    lost["latency"]["content_first_at_ms"] = None
    recs.append(lost)
    recs.append(load_sample("tool"))
    synced = copy.deepcopy(chat)
    synced["turn_id"] = "c100"
    synced["anchors"]["clock_quality"] = "synced"
    recs.append(synced)

    s = tc.summarize(recs, TraceParams(min_n_p50=20, min_n_p90=50))
    rows = {(r.turn_group, r.primary, r.clock_quality, r.metric): r for r in s.rows}
    chat_sound = rows[("普通", "release", "rx_minus_half_rtt", "有声音")]
    # 25 ok + 1 failed + 1 lost-receipt: failed excluded from the distribution, null counted as missing
    assert chat_sound.turns == 27 and chat_sound.n == 25 and chat_sound.missing == 1
    assert abs(chat_sound.failed_rate - 1 / 27) < 1e-9
    assert chat_sound.p50_ms == 800 + 12 * 10 and chat_sound.p90_ms == 800 + 22 * 10
    assert chat_sound.p50_insufficient is False and chat_sound.p90_insufficient is True
    assert chat_sound.notes == "P90 样本不足"
    tool_content = rows[("工具", "release", "rx_minus_half_rtt", "有内容")]
    assert tool_content.n == 1 and tool_content.notes == "P50 样本不足，P90 样本不足"
    assert ("普通", "release", "synced", "有声音") in rows  # clock quality is never mixed
    assert s.hard_metrics == {"stuck_turns": 1, "double_voice_turns": 0, "hanging_calls": 0}

    table = tc.format_table(s)
    assert "样本不足" in table and "硬指标" in table and "P50(ms)" in table
    js = json.loads(s.to_json())
    assert js["total"] == len(recs) and js["rows"][0]["notes"]


def test_percentile_nearest_rank() -> None:
    assert tc.percentile([5, 1, 3], 50) == 3
    assert tc.percentile(list(range(1, 101)), 90) == 90


def test_cli_exit_codes(tmp_path: Path, capsys) -> None:  # noqa: ANN001
    ok = _write_jsonl(tmp_path / "ok.jsonl", [load_sample("chat")])
    assert tc.main([str(ok)]) == 0
    out = capsys.readouterr().out
    assert "通过 1" in out and "样本不足" in out
    bad = load_sample("chat")
    bad["turn_terminal"] = "nope"
    badp = _write_jsonl(tmp_path / "bad.jsonl", [bad])
    assert tc.main([str(badp), "--json"]) == 1
    js = json.loads(capsys.readouterr().out)
    assert js["invalid"][0]["turn_id"] == "c41"
    assert tc.main([]) == 2
    assert tc.main([str(tmp_path / "missing.jsonl")]) == 2


def test_summarize_segment_breakdown_normal_turns_only():
    def rec(kind: str, seg: dict) -> dict:
        return {"turn_kind": kind, "turn_terminal": "completed", "latency": {"seg": seg}}

    recs = [rec("chat", {"llm_ms": v, "first_segment_ms": 100, "tts_start_ms": 300}) for v in (400, 500, 600)]
    recs.append(rec("tool_a1", {"llm_ms": 9000}))  # tool turns are not mixed in
    s = tc.summarize(recs)
    by = {sg["segment"]: sg for sg in s.segments}
    assert by["llm_ms"]["n"] == 3 and by["llm_ms"]["p50_ms"] == 500
    assert by["tts_start_ms"]["p50_ms"] == 300 and "vad_ms" not in by
    table = tc.format_table(s)
    assert "LLM 首词 → 首段送进 TTS" in table
    assert '"segments"' in s.to_json()
