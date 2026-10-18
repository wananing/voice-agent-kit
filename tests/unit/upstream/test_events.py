"""EventEmitter and ErrorMapper (spec 04 §4.1, §4.6)."""

from __future__ import annotations

import logging

import pytest

from vak.core.clock import FakeClock
from vak.core.types import ErrorClass, ErrorReason, ErrorScope
from vak.upstream.events import ErrorMapper, EventEmitter
from vak.upstream.providers.mock import mock_caps


async def test_seq_monotonic_and_t_local_from_clock() -> None:
    clock = FakeClock(start_ms=100)
    em = EventEmitter(clock, session_id="s1")
    a = em.emit(em.speech_started(None))
    await clock.advance(50)
    b = em.emit(em.speech_stopped(None))
    assert (a.seq, b.seq) == (1, 2)
    assert (a.t_local_ms, b.t_local_ms) == (100, 150)
    assert a.session_id == "s1"


async def test_events_iterator_ends_on_close() -> None:
    em = EventEmitter(FakeClock())
    em.emit(em.speech_started(None))
    em.close()
    got = [e async for e in em.events()]
    assert len(got) == 1


def test_constructors_set_required_fields() -> None:
    em = EventEmitter(FakeClock())
    d = em.audio_delta("r1", "i1", b"\0\0", 16000, 10)
    assert (d.response_id, d.item_id, d.duration_ms, d.sample_rate) == ("r1", "i1", 10, 16000)
    s = em.sentence("r1", 2, "你好", None)
    assert s.item_id == "2" and s.sentence_index == 2


# -- error mapper: default rules 1–8 ----------------------------------------


def _mapper(**over: object) -> ErrorMapper:
    return ErrorMapper(mock_caps(**{"errors.code_map": {}, **over}))


@pytest.mark.parametrize(
    ("code", "msg", "cls", "reason"),
    [
        ("invalid_api_key", None, ErrorClass.FATAL, ErrorReason.AUTH),  # rule 1
        ("x", "insufficient quota", ErrorClass.FATAL, ErrorReason.QUOTA),
        ("x", "billing hard limit", ErrorClass.FATAL, ErrorReason.BILLING),
        ("1007", "context window exceeded", ErrorClass.CONTEXT_OVERFLOW, ErrorReason.UNKNOWN),  # rule 3
        ("server_error", "connection closed", ErrorClass.TRANSIENT, ErrorReason.SESSION_LOST),  # rule 4
        ("x", "request timed out", ErrorClass.TRANSIENT, ErrorReason.TIMEOUT),
        ("429", "rate limit", ErrorClass.TRANSIENT, ErrorReason.THROTTLED),
        ("response_cancel_not_active", None, ErrorClass.BENIGN, ErrorReason.CANCEL_RACE),  # rule 5
        ("input_audio_buffer_commit_empty", None, ErrorClass.BENIGN, ErrorReason.EMPTY_COMMIT),
        ("item_create_duplicate_item_id", None, ErrorClass.BENIGN, ErrorReason.DUPLICATE),
        ("x", "moderation blocked", ErrorClass.BENIGN, ErrorReason.MODERATION),
        ("x", "Tool Response parsing error", ErrorClass.BENIGN, ErrorReason.TOOL_PARSE),
        ("totally_new_code", "???", ErrorClass.TRANSIENT, ErrorReason.UNKNOWN),  # rule 7
    ],
)
def test_default_rules(code: str, msg: str | None, cls: ErrorClass, reason: ErrorReason) -> None:
    err = _mapper().map(code, msg)
    assert (err.error_class, err.reason) == (cls, reason)
    assert err.raw_code == code


def test_rule2_not_open_is_fatal_invalid_config() -> None:
    err = _mapper().map("bad_param", "unknown parameter", session_open=False)
    assert (err.error_class, err.reason) == (ErrorClass.FATAL, ErrorReason.INVALID_CONFIG)


def test_rule5_item_level_error_with_correlation_is_benign() -> None:
    err = _mapper().map("weird", "whatever", ref={"event_id": "e9"})
    assert (err.error_class, err.scope) == (ErrorClass.BENIGN, ErrorScope.ITEM)


def test_rule6_failed_response_not_fatal_is_benign_response_scope() -> None:
    err = _mapper().map("weird", "whatever", response_failed=True)
    assert (err.error_class, err.scope) == (ErrorClass.BENIGN, ErrorScope.RESPONSE)


def test_rule7_logs_unmapped(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="vak.upstream.events"):
        _mapper().map("brand_new", "nothing known")
    assert "unmapped error code" in caplog.text


def test_code_map_takes_precedence_stable_codes() -> None:
    m = ErrorMapper(mock_caps(**{"errors.code_map": {"weird": {"class": "benign", "reason": "duplicate"}}}))
    err = m.map("weird", "connection closed")  # default rule would say transient
    assert (err.error_class, err.reason) == (ErrorClass.BENIGN, ErrorReason.DUPLICATE)
    assert m.map("WEIRD", None).error_class is ErrorClass.TRANSIENT  # stable codes: exact match only


def test_rule8_message_match_substring() -> None:
    m = ErrorMapper(
        mock_caps(**{
            "errors.code_stability": "message_match",
            "errors.code_map": {"quota": {"class": "fatal", "reason": "quota"}},
        })
    )
    assert m.map("E1", "Your Quota is gone").reason is ErrorReason.QUOTA
