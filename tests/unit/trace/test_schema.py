"""Spec 06 §10.1 item 1: schema validates the four sample turn kinds and rejects bad records."""

from __future__ import annotations

import copy

import pytest

from vak.core.trace import TRACE_SCHEMA_VERSION, allowed_keys, load_schema, missing_required, validate

from .conftest import SAMPLE_NAMES, load_sample


@pytest.mark.parametrize("name", SAMPLE_NAMES)
def test_sample_traces_validate_chat_tool_interrupted_error(name: str) -> None:
    rec = load_sample(name)
    assert validate(rec) == []
    assert rec["trace_schema_version"] == TRACE_SCHEMA_VERSION


def test_schema_is_draft_2020_12_and_closed() -> None:
    s = load_schema()
    assert s["$schema"].endswith("draft/2020-12/schema")
    assert s["additionalProperties"] is False
    assert "closed_by" in s["properties"]["tool_calls"]["items"]["properties"]


def test_rejects_bad_enum() -> None:
    rec = load_sample("chat")
    rec["turn_terminal"] = "silence"  # old draft value, not in spec 01 §2.4
    errs = validate(rec)
    assert any("turn_terminal" in e for e in errs)
    rec = load_sample("tool")
    rec["tool_calls"][0]["closed_by"] = "timeout"  # not one of the six
    assert any("closed_by" in e for e in validate(rec))


def test_rejects_missing_m_field() -> None:
    rec = load_sample("chat")
    del rec["turn_terminal"]
    errs = validate(rec)
    assert any("turn_terminal" in e and "required" in e for e in errs)
    assert ((), "turn_terminal") in missing_required(rec)


def test_null_allowed_for_unmeasured_m_field() -> None:
    rec = load_sample("chat")
    rec["latency"]["sound_first_at_ms"] = None  # receipt lost -> null, not omitted (spec 06 §2.1 r5)
    assert validate(rec) == []


def test_profile_conditionals_m_a_m_b() -> None:
    a = load_sample("chat")
    del a["device_id"]
    assert any("device_id" in e for e in validate(a))
    b = load_sample("interrupted")
    del b["route"]
    assert any("route" in e for e in validate(b))
    del b["latency"]["ui_feedback_first_at_ms"]
    assert any("ui_feedback_first_at_ms" in e for e in validate(b))


def test_conditional_c_fields() -> None:
    err = load_sample("error")
    del err["error_code"]  # C: turn_terminal=failed
    assert any("error_code" in e for e in validate(err))
    tool = load_sample("tool")
    del tool["speak_mode"]  # C: tool turn
    assert any("speak_mode" in e for e in validate(tool))


def test_rejects_unknown_top_level_and_nested_key() -> None:
    rec = load_sample("chat")
    rec["first_audio_ms"] = 1  # "首音" without saying which one is not a field
    assert any("first_audio_ms" in e for e in validate(rec))
    rec = load_sample("chat")
    rec["latency"]["ttfa_ms"] = 1
    assert any("ttfa_ms" in e for e in validate(rec))


def test_allowed_keys_tree() -> None:
    top = allowed_keys()
    assert top is not None and {"anchors", "latency", "usage", "outputs", "tool_calls"} <= top
    assert "seg" in (allowed_keys(("latency",)) or set())
    assert "closed_by" in (allowed_keys(("tool_calls",)) or set())
    assert "response_status" in (allowed_keys(("usage", "responses")) or set())
    assert allowed_keys(("slots",)) is None  # free-form


def test_deep_copy_sample_unchanged_by_validate() -> None:
    rec = load_sample("tool")
    before = copy.deepcopy(rec)
    validate(rec)
    assert rec == before
