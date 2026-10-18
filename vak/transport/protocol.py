"""va/1 JSON control messages (spec 01 §3).

Uplink: `parse_hello` and `validate_uplink` (rule 17: unknown `type` is
ignored, a missing required field is `error{protocol_error}` and never a
disconnect). Downlink: one builder per message, field names exactly as in
spec 01 §3.2 / handbook turn-protocol §2.6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vak.audio.codec import FRAME_MS, SUPPORTED_RATES, gateway_codecs
from vak.core.defaults import TurnTimers
from vak.core.types import Aec, ErrorCode, Mode, Profile, TurnId

PROTO = "va/1"


class ProtocolError(Exception):
    """A known message is missing a required field or has a bad value.

    The transport answers `error{code:"protocol_error"}` and keeps the connection.
    """

    def __init__(self, message: str, turn_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.turn_id = turn_id


# ---------------------------------------------------------------------------
# hello (spec 01 §3.1.1, §6.2)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AudioUp:
    codec: str = "opus"
    rate: int = 16000
    channels: int = 1
    frame_ms: int = FRAME_MS


@dataclass(slots=True)
class AudioDown:
    codecs: list[str] = field(default_factory=lambda: ["opus"])
    rates: list[int] = field(default_factory=lambda: [16000])


@dataclass(slots=True)
class ClientCaps:
    """`hello.caps`. Defaults are the conservative values for a legacy client
    that sends no caps (handbook turn-protocol §7: old clients must still run)."""

    transport: str = "ws"
    aec: Aec = Aec.NONE
    modes: list[Mode] = field(default_factory=lambda: [Mode.PTT])
    sources: list[str] = field(default_factory=lambda: ["button"])
    receipts: list[str] = field(default_factory=list)
    audio_up: AudioUp = field(default_factory=AudioUp)
    audio_down: AudioDown = field(default_factory=AudioDown)
    play_buffer_max_ms: int = 1200
    preroll_ms: int = 0
    uninterruptible: bool = False
    events: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ResumeRequest:
    session_id: str
    last_tx_seq: int
    last_rx_seq: int
    current_turn: str | None = None
    turn_state: str | None = None


@dataclass(slots=True)
class ClientHello:
    proto: str
    profile: Profile
    client: dict[str, Any]
    caps: ClientCaps
    resume: ResumeRequest | None = None


@dataclass(slots=True)
class AudioDownChoice:
    codec: str
    rate: int
    frame_ms: int = FRAME_MS


def _require(msg: dict[str, Any], *keys: str) -> None:
    missing = [k for k in keys if k not in msg]
    if missing:
        tid = msg.get("turn_id")
        raise ProtocolError(
            f"{msg.get('type', '?')}: missing required field(s) {', '.join(missing)}",
            turn_id=tid if isinstance(tid, str) else None,
        )


def _enum(cls: type, value: Any, what: str) -> Any:
    try:
        return cls(value)
    except ValueError as e:
        raise ProtocolError(f"bad {what}: {value!r}") from e


def parse_caps(raw: dict[str, Any] | None) -> ClientCaps:
    if not raw:
        return ClientCaps()
    caps = ClientCaps()
    caps.transport = str(raw.get("transport", caps.transport))
    if "aec" in raw:
        caps.aec = _enum(Aec, raw["aec"], "caps.aec")
    if "modes" in raw:
        caps.modes = [_enum(Mode, m, "caps.modes") for m in raw["modes"]]
    caps.sources = list(raw.get("sources", caps.sources))
    caps.receipts = list(raw.get("receipts", caps.receipts))
    up = raw.get("audio_up") or {}
    caps.audio_up = AudioUp(
        codec=str(up.get("codec", "opus")),
        rate=int(up.get("rate", 16000)),
        channels=int(up.get("channels", 1)),
        frame_ms=int(up.get("frame_ms", FRAME_MS)),
    )
    down = raw.get("audio_down") or {}
    caps.audio_down = AudioDown(
        codecs=[str(c) for c in down.get("codecs", ["opus"])],
        rates=[int(r) for r in down.get("rates", [16000])],
    )
    caps.play_buffer_max_ms = int(raw.get("play_buffer_max_ms", caps.play_buffer_max_ms))
    caps.preroll_ms = int(raw.get("preroll_ms", caps.preroll_ms))
    caps.uninterruptible = bool(raw.get("uninterruptible", caps.uninterruptible))
    caps.events = list(raw.get("events", caps.events))
    caps.commands = list(raw.get("commands", caps.commands))
    return caps


def parse_hello(msg: dict[str, Any]) -> ClientHello:
    _require(msg, "proto", "profile")
    if msg["proto"] != PROTO:
        raise ProtocolError(f"unsupported proto {msg['proto']!r}, want {PROTO}")
    profile = _enum(Profile, msg["profile"], "profile")
    caps_raw = msg.get("caps")
    if caps_raw is not None and not isinstance(caps_raw, dict):
        raise ProtocolError("hello.caps must be an object")
    caps = parse_caps(caps_raw)
    resume = None
    r = msg.get("resume")
    if r is not None:
        if not isinstance(r, dict):
            raise ProtocolError("hello.resume must be an object or null")
        for k in ("session_id", "last_tx_seq", "last_rx_seq"):
            if k not in r:
                raise ProtocolError(f"hello.resume: missing required field {k}")
        resume = ResumeRequest(
            session_id=str(r["session_id"]),
            last_tx_seq=int(r["last_tx_seq"]),
            last_rx_seq=int(r["last_rx_seq"]),
            current_turn=r.get("current_turn"),
            turn_state=r.get("turn_state"),
        )
    client = msg.get("client") or {}
    return ClientHello(proto=PROTO, profile=profile, client=dict(client), caps=caps, resume=resume)


# ---------------------------------------------------------------------------
# negotiation (spec 01 §3.1.1, §4.1; architecture §7 item 7)
# ---------------------------------------------------------------------------


def negotiate_audio_down(caps: ClientCaps, prefer_rate: int = 16000) -> AudioDownChoice | None:
    """First client codec the gateway supports; prefer 16 kHz, else the first common rate."""
    ours = gateway_codecs()
    codec = next((c for c in caps.audio_down.codecs if c in ours), None)
    if codec is None:
        return None
    common = [r for r in caps.audio_down.rates if r in SUPPORTED_RATES]
    if not common:
        return None
    rate = prefer_rate if prefer_rate in common else common[0]
    return AudioDownChoice(codec=codec, rate=rate)


def negotiate_audio_up(caps: ClientCaps) -> AudioUp | None:
    up = caps.audio_up
    if up.codec not in gateway_codecs() or up.rate not in SUPPORTED_RATES or up.channels != 1:
        return None
    return up


def negotiate_mode(caps: ClientCaps, wanted: Mode) -> Mode:
    """`open_mic` needs client support and AEC != none (spec 01 §3.1.1); else `ptt`."""
    if wanted is Mode.OPEN_MIC and Mode.OPEN_MIC in caps.modes and caps.aec is not Aec.NONE:
        return Mode.OPEN_MIC
    return Mode.PTT


# ---------------------------------------------------------------------------
# uplink validation (spec 01 §3.1; rule 17)
# ---------------------------------------------------------------------------

_REQUIRED: dict[str, tuple[str, ...]] = {
    "hello": ("proto", "profile"),
    "listen.open": ("mode",),
    "listen.close": (),
    "turn.start": ("turn_id", "source"),
    "turn.end": ("turn_id", "source", "ts_samples"),
    "abort": ("turn_id", "reason"),
    "playback.started": ("turn_id", "sentence_id", "played_ms", "ts_ms"),
    "playback.ended": ("turn_id", "sentence_id", "played_ms", "ts_ms"),
    "playback.interrupted": ("turn_id", "sentence_id", "played_ms", "ts_ms"),
    "event": ("id", "name"),
    "ping": ("client_mono_ms",),
    "cmd.result": ("id", "ok"),
}

UPLINK_TYPES = frozenset(_REQUIRED)


def validate_uplink(msg: Any) -> str | None:
    """Return the message type if it is known and complete, None if the type
    is unknown (ignore it). Raises ProtocolError for a missing required field."""
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("message must be an object with a string `type`")
    t: str = msg["type"]
    req = _REQUIRED.get(t)
    if req is None:
        return None
    _require(msg, *req)
    if t == "turn.start":
        # text input carries the whole utterance; other sources carry the boundary sample count
        _require(msg, "text" if msg["source"] == "text" else "ts_samples")
    return t


# ---------------------------------------------------------------------------
# downlink builders (spec 01 §3.2)
# ---------------------------------------------------------------------------


def _tid(turn_id: TurnId | str | None) -> str | None:
    return None if turn_id is None else str(turn_id)


def timers_for(timers: TurnTimers, profile: Profile) -> dict[str, int]:
    """`hello.timers` (spec 01 §2.5): the effective values the client must use."""
    return {
        "T_hold": timers.T_hold,
        "T_asr": timers.T_asr,
        "T_filler": timers.T_filler_A if profile is Profile.A else timers.T_filler_B,
        "T_respond": timers.T_respond,
        "T_link_client": timers.T_link_client,
        "ping_interval": timers.ping_interval,
        "resume_window": timers.resume_window,
    }


def resume_ok(last_rx_seq: int, turns: list[tuple[TurnId | str, str]]) -> dict[str, Any]:
    return {"ok": True, "last_rx_seq": last_rx_seq, "turns": [{"turn_id": str(t), "state": s} for t, s in turns]}


def hello_reply(
    *,
    session_id: str,
    mode: Mode,
    audio_down: AudioDownChoice,
    lead_ms: int,
    timers: TurnTimers,
    profile: Profile,
    next_server_turn: int,
    server_time_ms: int,
    resume: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "hello",
        "session_id": session_id,
        "mode": mode.value,
        "audio_down": {"codec": audio_down.codec, "rate": audio_down.rate, "frame_ms": audio_down.frame_ms},
        "lead_ms": lead_ms,
        "timers": timers_for(timers, profile),
        "next_server_turn": next_server_turn,
        "resume": resume,
        "server_time_ms": server_time_ms,
    }


def stt(turn_id: TurnId | str, text: str, final: bool, segment: int | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"type": "stt", "turn_id": str(turn_id), "text": text, "final": final}
    if segment is not None:
        msg["segment"] = segment
    return msg


def tts_start(turn_id: TurnId | str, kind: str, codec: str, rate: int) -> dict[str, Any]:
    return {"type": "tts.start", "turn_id": str(turn_id), "kind": kind, "codec": codec, "rate": rate}


def tts_sentence_start(
    turn_id: TurnId | str,
    sentence_id: int,
    text: str,
    offset_ms: int,
    duration_ms: int | None = None,
    interruptible: bool = True,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "type": "tts.sentence_start",
        "turn_id": str(turn_id),
        "sentence_id": sentence_id,
        "text": text,
        "offset_ms": offset_ms,
        "interruptible": interruptible,
    }
    if duration_ms is not None:
        msg["duration_ms"] = duration_ms
    return msg


def tts_stop(turn_id: TurnId | str, reason: str) -> dict[str, Any]:
    return {"type": "tts.stop", "turn_id": str(turn_id), "reason": reason}


def error(
    turn_id: TurnId | str | None,
    code: ErrorCode | str,
    message: str,
    fatal: bool = False,
    retry_after_ms: int | None = None,
) -> dict[str, Any]:
    """`turn_id = None` is a session-level error (spec 01 §3.2.4)."""
    return {
        "type": "error",
        "turn_id": _tid(turn_id),
        "code": code.value if isinstance(code, ErrorCode) else code,
        "fatal": fatal,
        "message": message,
        "retry_after_ms": retry_after_ms,
    }


def protocol_error(exc: ProtocolError) -> dict[str, Any]:
    return error(exc.turn_id, ErrorCode.PROTOCOL_ERROR, exc.message)


def turn_closed(turn_id: TurnId | str, state: str, by: str | None, heard_ms: int, segments: int) -> dict[str, Any]:
    return {
        "type": "turn.closed",
        "turn_id": str(turn_id),
        "state": state,
        "by": by,
        "heard_ms": heard_ms,
        "segments": segments,
    }


def flush_start(
    flush_id: str, turn_ids: list[TurnId | str], reason: str, ack_of: str | None = None
) -> dict[str, Any]:
    return {
        "type": "flush.start",
        "flush_id": flush_id,
        "turn_ids": [str(t) for t in turn_ids],
        "reason": reason,
        "ack_of": ack_of,
    }


def flush_end(flush_id: str) -> dict[str, Any]:
    return {"type": "flush.end", "flush_id": flush_id}


def turn_start(
    turn_id: TurnId | str, source: str, ts_samples: int | None = None, kind: str | None = None
) -> dict[str, Any]:
    """Gateway-side `turn.start` (vad boundary or event narration), spec 01 §3.2.6."""
    msg: dict[str, Any] = {"type": "turn.start", "turn_id": str(turn_id), "source": source}
    if ts_samples is not None:
        msg["ts_samples"] = ts_samples
    if kind is not None:
        msg["kind"] = kind
    return msg


def turn_end(turn_id: TurnId | str, source: str, ts_samples: int, reason: str) -> dict[str, Any]:
    return {"type": "turn.end", "turn_id": str(turn_id), "source": source, "ts_samples": ts_samples, "reason": reason}


def turn_merge(from_turn: TurnId | str, into: TurnId | str) -> dict[str, Any]:
    return {"type": "turn.merge", "from": str(from_turn), "into": str(into)}


def cmd(cmd_id: str, name: str, args: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    return {"type": "cmd", "id": cmd_id, "name": name, "args": args, "timeout_ms": timeout_ms}


def pong(client_mono_ms: int, server_ms: int) -> dict[str, Any]:
    return {"type": "pong", "client_mono_ms": client_mono_ms, "server_ms": server_ms}


def event_ack(event_id: str, status: str, code: str | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"type": "event.ack", "id": event_id, "status": status}
    if code is not None:
        msg["code"] = code
    return msg
