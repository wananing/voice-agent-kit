"""Spec 01 §3: hello parsing, rule 17 validation, builders match the spec JSON, negotiation."""

from __future__ import annotations

import pytest

from vak.audio.codec import opus_available
from vak.core.defaults import TurnTimers
from vak.core.types import Aec, ErrorCode, Mode, Profile, TurnId
from vak.transport import protocol as P
from vak.transport.protocol import ClientCaps, ProtocolError, parse_hello, validate_uplink

SPEC_HELLO = {
    "type": "hello",
    "proto": "va/1",
    "profile": "A",
    "client": {"model": "toy-s3", "fw": "1.4.2"},
    "caps": {
        "transport": "ws",
        "aec": "device",
        "modes": ["ptt", "open_mic"],
        "sources": ["button", "wake", "text"],
        "receipts": ["started", "ended", "interrupted"],
        "audio_up": {"codec": "opus", "rate": 16000, "channels": 1, "frame_ms": 20},
        "audio_down": {"codecs": ["opus"], "rates": [16000, 24000]},
        "play_buffer_max_ms": 1200,
        "preroll_ms": 250,
        "uninterruptible": True,
        "events": ["location", "nfc", "timer"],
        "commands": ["light", "volume"],
    },
    "resume": None,
}


def test_parse_spec_hello() -> None:
    h = parse_hello(SPEC_HELLO)
    assert h.profile is Profile.A and h.client["model"] == "toy-s3" and h.resume is None
    c = h.caps
    assert c.aec is Aec.DEVICE and c.modes == [Mode.PTT, Mode.OPEN_MIC]
    assert c.receipts == ["started", "ended", "interrupted"]
    assert c.audio_up.codec == "opus" and c.audio_up.rate == 16000 and c.audio_up.frame_ms == 20
    assert c.audio_down.codecs == ["opus"] and c.audio_down.rates == [16000, 24000]
    assert c.play_buffer_max_ms == 1200 and c.preroll_ms == 250 and c.uninterruptible is True
    assert c.events == ["location", "nfc", "timer"] and c.commands == ["light", "volume"]


def test_parse_hello_resume() -> None:
    msg = dict(SPEC_HELLO)
    msg["resume"] = {
        "session_id": "S-8f2c",
        "last_tx_seq": 48210,
        "last_rx_seq": 9021,
        "current_turn": "c42",
        "turn_state": "listening",
    }
    r = parse_hello(msg).resume
    assert r is not None and r.session_id == "S-8f2c" and r.last_tx_seq == 48210 and r.last_rx_seq == 9021
    assert r.current_turn == "c42" and r.turn_state == "listening"
    msg["resume"] = {"session_id": "S-1"}
    with pytest.raises(ProtocolError):
        parse_hello(msg)


def test_parse_hello_missing_fields_and_legacy_caps() -> None:
    with pytest.raises(ProtocolError):
        parse_hello({"type": "hello", "proto": "va/1"})
    with pytest.raises(ProtocolError):
        parse_hello({"type": "hello", "proto": "va/0", "profile": "A"})
    legacy = parse_hello({"type": "hello", "proto": "va/1", "profile": "A"})  # no caps: conservative defaults
    assert legacy.caps.aec is Aec.NONE and legacy.caps.receipts == [] and legacy.caps.modes == [Mode.PTT]


def test_validate_uplink_rule_17() -> None:
    assert validate_uplink({"type": "something.new", "x": 1}) is None  # unknown type: ignored
    assert validate_uplink({"type": "abort", "turn_id": "c1", "reason": "button", "extra": 1}) == "abort"
    with pytest.raises(ProtocolError) as ei:
        validate_uplink({"type": "abort", "turn_id": "c1"})
    assert ei.value.turn_id == "c1" and "reason" in ei.value.message
    with pytest.raises(ProtocolError):
        validate_uplink({"no": "type"})
    with pytest.raises(ProtocolError):
        validate_uplink("not an object")
    # turn.start: text source needs text, others need ts_samples
    assert validate_uplink({"type": "turn.start", "turn_id": "c1", "source": "text", "text": "hi"}) == "turn.start"
    with pytest.raises(ProtocolError):
        validate_uplink({"type": "turn.start", "turn_id": "c1", "source": "text"})
    with pytest.raises(ProtocolError):
        validate_uplink({"type": "turn.start", "turn_id": "c1", "source": "button"})
    assert validate_uplink({"type": "ping", "client_mono_ms": 1}) == "ping"
    with pytest.raises(ProtocolError):
        validate_uplink({"type": "playback.ended", "turn_id": "c1", "sentence_id": 0, "played_ms": 1})


def test_protocol_error_message_shape() -> None:
    msg = P.protocol_error(ProtocolError("abort: missing reason", turn_id="c1"))
    assert msg == {
        "type": "error",
        "turn_id": "c1",
        "code": "protocol_error",
        "fatal": False,
        "message": "abort: missing reason",
        "retry_after_ms": None,
    }


def test_hello_reply_matches_spec() -> None:
    reply = P.hello_reply(
        session_id="S-8f2c",
        mode=Mode.PTT,
        audio_down=P.AudioDownChoice("opus", 16000),
        lead_ms=300,
        timers=TurnTimers(),
        profile=Profile.A,
        next_server_turn=1,
        server_time_ms=1790000000123,
    )
    assert reply == {
        "type": "hello",
        "session_id": "S-8f2c",
        "mode": "ptt",
        "audio_down": {"codec": "opus", "rate": 16000, "frame_ms": 20},
        "lead_ms": 300,
        "timers": {
            "T_hold": 600,
            "T_asr": 3000,
            "T_filler": 2000,
            "T_respond": 8000,
            "T_link_client": 15000,
            "ping_interval": 5000,
            "resume_window": 30000,
        },
        "next_server_turn": 1,
        "resume": None,
        "server_time_ms": 1790000000123,
    }
    assert P.timers_for(TurnTimers(), Profile.B)["T_filler"] == 2500
    assert P.resume_ok(48177, [(TurnId("c", 42), "listening")]) == {
        "ok": True,
        "last_rx_seq": 48177,
        "turns": [{"turn_id": "c42", "state": "listening"}],
    }


def test_downlink_builders_match_spec_json() -> None:
    c42 = TurnId("c", 42)
    assert P.stt(c42, "明天几点", False, 0) == {"type": "stt", "turn_id": "c42", "text": "明天几点", "final": False, "segment": 0}
    assert P.stt(c42, "明天几点出发", True) == {"type": "stt", "turn_id": "c42", "text": "明天几点出发", "final": True}
    assert P.tts_start(c42, "speech", "opus", 16000) == {
        "type": "tts.start", "turn_id": "c42", "kind": "speech", "codec": "opus", "rate": 16000,
    }
    assert P.tts_sentence_start(c42, 1, "请牵好大人的手。", 1320, 1480, False) == {
        "type": "tts.sentence_start",
        "turn_id": "c42",
        "sentence_id": 1,
        "text": "请牵好大人的手。",
        "offset_ms": 1320,
        "duration_ms": 1480,
        "interruptible": False,
    }
    assert "duration_ms" not in P.tts_sentence_start(c42, 0, "x", 0)  # streaming TTS may omit it
    assert P.tts_stop(c42, "done") == {"type": "tts.stop", "turn_id": "c42", "reason": "done"}
    assert P.error(c42, ErrorCode.ASR_TIMEOUT, "recognition timed out") == {
        "type": "error",
        "turn_id": "c42",
        "code": "asr_timeout",
        "fatal": False,
        "message": "recognition timed out",
        "retry_after_ms": None,
    }
    assert P.error(None, ErrorCode.SESSION_EXPIRED, "x", fatal=True)["turn_id"] is None
    assert P.turn_closed(c42, "failed", "asr_timeout", 0, 1) == {
        "type": "turn.closed", "turn_id": "c42", "state": "failed", "by": "asr_timeout", "heard_ms": 0, "segments": 1,
    }
    assert P.flush_start("f-77", [TurnId("c", 41)], "abort", "abort") == {
        "type": "flush.start", "flush_id": "f-77", "turn_ids": ["c41"], "reason": "abort", "ack_of": "abort",
    }
    assert P.flush_end("f-77") == {"type": "flush.end", "flush_id": "f-77"}
    assert P.turn_start(TurnId("s", 17), "vad", 2211840) == {
        "type": "turn.start", "turn_id": "s17", "source": "vad", "ts_samples": 2211840,
    }
    assert P.turn_end(TurnId("s", 17), "vad", 2260000, "endpoint") == {
        "type": "turn.end", "turn_id": "s17", "source": "vad", "ts_samples": 2260000, "reason": "endpoint",
    }
    assert P.turn_start(TurnId("s", 18), "event", kind="narration") == {
        "type": "turn.start", "turn_id": "s18", "source": "event", "kind": "narration",
    }
    assert P.turn_merge(TurnId("c", 43), c42) == {"type": "turn.merge", "from": "c43", "into": "c42"}
    assert P.cmd("cmd-310", "light", {"pattern": "thinking"}, 3000) == {
        "type": "cmd", "id": "cmd-310", "name": "light", "args": {"pattern": "thinking"}, "timeout_ms": 3000,
    }
    assert P.pong(61000, 1790000061042) == {"type": "pong", "client_mono_ms": 61000, "server_ms": 1790000061042}
    assert P.event_ack("ev-1022", "accepted") == {"type": "event.ack", "id": "ev-1022", "status": "accepted"}
    assert P.event_ack("ev-1022", "rejected", "internal") == {
        "type": "event.ack", "id": "ev-1022", "status": "rejected", "code": "internal",
    }


def _caps(codecs: list[str], rates: list[int]) -> ClientCaps:
    c = ClientCaps()
    c.audio_down.codecs = codecs
    c.audio_down.rates = rates
    return c


def test_codec_negotiation() -> None:
    # architecture §7 item 7: opus and pcm16 both accepted; first client codec the gateway supports wins
    assert P.negotiate_audio_down(_caps(["pcm16"], [16000])) == P.AudioDownChoice("pcm16", 16000)
    choice = P.negotiate_audio_down(_caps(["opus", "pcm16"], [24000, 16000]))
    assert choice is not None
    assert choice.codec == ("opus" if opus_available() else "pcm16")
    assert choice.rate == 16000  # 16 kHz preferred when offered
    assert P.negotiate_audio_down(_caps(["pcm16"], [24000])) == P.AudioDownChoice("pcm16", 24000)
    assert P.negotiate_audio_down(_caps(["g722"], [16000])) is None
    assert P.negotiate_audio_down(_caps(["pcm16"], [48000])) is None
    up = ClientCaps()
    up.audio_up.codec = "pcm16"
    assert P.negotiate_audio_up(up) is up.audio_up
    up.audio_up.channels = 2
    assert P.negotiate_audio_up(up) is None


def test_mode_negotiation() -> None:
    c = ClientCaps()
    c.modes = [Mode.PTT, Mode.OPEN_MIC]
    c.aec = Aec.NONE
    assert P.negotiate_mode(c, Mode.OPEN_MIC) is Mode.PTT  # aec none forbids open_mic
    c.aec = Aec.DEVICE
    assert P.negotiate_mode(c, Mode.OPEN_MIC) is Mode.OPEN_MIC
    assert P.negotiate_mode(c, Mode.PTT) is Mode.PTT
    c.modes = [Mode.PTT]
    assert P.negotiate_mode(c, Mode.OPEN_MIC) is Mode.PTT
