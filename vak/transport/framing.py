"""16-byte audio frame header (spec 01 §3.1.2 uplink, §3.2.1 downlink).

Byte layout (network byte order, big-endian; the spec fixes the field
semantics and leaves the layout as a recommendation, this is ours):

    offset  uplink (kind 0x01)         downlink (kind 0x02)
    0       ver u8 = 1                 ver u8 = 1
    1       kind u8                    kind u8
    2       flags u8                   flags u8
    3       reserved u8 = 0            reserved u8 = 0
    4       seq u32                    seq u32
    8       ts_samples u32             turn_id u32
    12      turn_id u32                sentence_id u16 | frame_idx u16

`turn_id` on the wire is `TurnId.to_u32()` (MSB = gateway side), 0 = no turn.
`seq` and `ts_samples` wrap at 2**32; compare them with `serial_gt`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from vak.core.types import TurnId

HEADER_LEN = 16
VERSION = 1
KIND_UPLINK = 0x01
KIND_DOWNLINK = 0x02

# uplink flags (spec 01 §3.1.2)
FLAG_KEY_DOWN = 0x01
FLAG_ECHO_TAIL = 0x02
FLAG_RETRANSMIT = 0x04
# downlink flags (spec 01 §3.2.1)
FLAG_UNINTERRUPTIBLE = 0x01

_UP = struct.Struct(">BBBBIII")
_DOWN = struct.Struct(">BBBBIIHH")
U32 = 1 << 32
U16 = 1 << 16


class FramingError(ValueError):
    pass


def serial_gt(a: int, b: int, bits: int = 32) -> bool:
    """RFC 1982 serial-number comparison: True if `a` is after `b` modulo 2**bits."""
    half = 1 << (bits - 1)
    mod = 1 << bits
    return a != b and ((a - b) % mod) < half


@dataclass(frozen=True, slots=True)
class UplinkHeader:
    seq: int
    ts_samples: int
    turn_id: TurnId | None
    key_down: bool = False
    echo_tail: bool = False
    retransmit: bool = False

    @property
    def flags(self) -> int:
        return (
            (FLAG_KEY_DOWN if self.key_down else 0)
            | (FLAG_ECHO_TAIL if self.echo_tail else 0)
            | (FLAG_RETRANSMIT if self.retransmit else 0)
        )


@dataclass(frozen=True, slots=True)
class DownlinkHeader:
    seq: int
    turn_id: TurnId | None
    sentence_id: int
    frame_idx: int
    uninterruptible: bool = False

    @property
    def flags(self) -> int:
        return FLAG_UNINTERRUPTIBLE if self.uninterruptible else 0


def _u32(v: int, name: str) -> int:
    if not (0 <= v < U32):
        raise FramingError(f"{name} out of u32 range: {v}")
    return v


def _u16(v: int, name: str) -> int:
    if not (0 <= v < U16):
        raise FramingError(f"{name} out of u16 range: {v}")
    return v


def pack_uplink(h: UplinkHeader, payload: bytes = b"") -> bytes:
    tid = h.turn_id.to_u32() if h.turn_id is not None else 0
    return (
        _UP.pack(VERSION, KIND_UPLINK, h.flags, 0, _u32(h.seq, "seq"), _u32(h.ts_samples, "ts_samples"), tid)
        + payload
    )


def pack_downlink(h: DownlinkHeader, payload: bytes = b"") -> bytes:
    tid = h.turn_id.to_u32() if h.turn_id is not None else 0
    return (
        _DOWN.pack(
            VERSION,
            KIND_DOWNLINK,
            h.flags,
            0,
            _u32(h.seq, "seq"),
            tid,
            _u16(h.sentence_id, "sentence_id"),
            _u16(h.frame_idx, "frame_idx"),
        )
        + payload
    )


def _check(data: bytes, kind: int) -> None:
    if len(data) < HEADER_LEN:
        raise FramingError(f"frame shorter than header: {len(data)} bytes")
    if data[0] != VERSION:
        raise FramingError(f"unsupported frame version {data[0]}")
    if data[1] != kind:
        raise FramingError(f"expected frame kind {kind:#04x}, got {data[1]:#04x}")


def unpack_uplink(data: bytes) -> tuple[UplinkHeader, bytes]:
    _check(data, KIND_UPLINK)
    _, _, flags, _, seq, ts, tid = _UP.unpack_from(data)
    h = UplinkHeader(
        seq=seq,
        ts_samples=ts,
        turn_id=TurnId.from_u32(tid),
        key_down=bool(flags & FLAG_KEY_DOWN),
        echo_tail=bool(flags & FLAG_ECHO_TAIL),
        retransmit=bool(flags & FLAG_RETRANSMIT),
    )
    return h, data[HEADER_LEN:]


def unpack_downlink(data: bytes) -> tuple[DownlinkHeader, bytes]:
    _check(data, KIND_DOWNLINK)
    _, _, flags, _, seq, tid, sid, fidx = _DOWN.unpack_from(data)
    h = DownlinkHeader(
        seq=seq,
        turn_id=TurnId.from_u32(tid),
        sentence_id=sid,
        frame_idx=fidx,
        uninterruptible=bool(flags & FLAG_UNINTERRUPTIBLE),
    )
    return h, data[HEADER_LEN:]
