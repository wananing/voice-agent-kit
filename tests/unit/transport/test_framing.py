"""Spec 01 §3.1.2 / §3.2.1: 16-byte header round-trips, MSB turn_id, wrap compare."""

from __future__ import annotations

import pytest

from vak.core.types import TurnId
from vak.transport.framing import (
    HEADER_LEN,
    DownlinkHeader,
    FramingError,
    UplinkHeader,
    pack_downlink,
    pack_uplink,
    serial_gt,
    unpack_downlink,
    unpack_uplink,
)


def test_uplink_layout_is_big_endian_16_bytes() -> None:
    data = pack_uplink(UplinkHeader(seq=1, ts_samples=2, turn_id=TurnId("c", 3), key_down=True), b"\xaa")
    assert len(data) == HEADER_LEN + 1
    assert data[:16] == bytes([1, 0x01, 0x01, 0, 0, 0, 0, 1, 0, 0, 0, 2, 0, 0, 0, 3])


def test_uplink_roundtrip_with_flags_and_gateway_turn_id_msb() -> None:
    h = UplinkHeader(seq=0xFFFF_FFFE, ts_samples=4_000_000_000, turn_id=TurnId("s", 17), echo_tail=True, retransmit=True)
    data = pack_uplink(h, b"opus")
    assert data[12:16] == (17 | 0x8000_0000).to_bytes(4, "big")  # R2.2-2: MSB = gateway side
    back, payload = unpack_uplink(data)
    assert back == h
    assert payload == b"opus"


def test_uplink_turn_id_zero_means_no_turn() -> None:
    data = pack_uplink(UplinkHeader(seq=1, ts_samples=0, turn_id=None))
    back, _ = unpack_uplink(data)
    assert back.turn_id is None
    assert data[12:16] == b"\x00\x00\x00\x00"


def test_downlink_roundtrip() -> None:
    h = DownlinkHeader(seq=9021, turn_id=TurnId("c", 42), sentence_id=3, frame_idx=65535, uninterruptible=True)
    data = pack_downlink(h, b"\x01\x02")
    assert data[1] == 0x02 and data[2] == 0x01
    back, payload = unpack_downlink(data)
    assert back == h and payload == b"\x01\x02"
    # client-side turn id (MSB clear) and gateway-side both survive
    back2, _ = unpack_downlink(pack_downlink(DownlinkHeader(seq=1, turn_id=TurnId("s", 1), sentence_id=0, frame_idx=0)))
    assert back2.turn_id == TurnId("s", 1)


def test_errors() -> None:
    with pytest.raises(FramingError):
        unpack_uplink(b"\x01\x01\x00")
    with pytest.raises(FramingError):  # downlink frame handed to the uplink parser
        unpack_uplink(pack_downlink(DownlinkHeader(seq=1, turn_id=None, sentence_id=0, frame_idx=0)))
    with pytest.raises(FramingError):
        pack_downlink(DownlinkHeader(seq=1, turn_id=None, sentence_id=70000, frame_idx=0))
    with pytest.raises(FramingError):
        pack_uplink(UplinkHeader(seq=1 << 32, ts_samples=0, turn_id=None))


def test_serial_gt_wraps() -> None:
    assert serial_gt(2, 1)
    assert not serial_gt(1, 2)
    assert not serial_gt(5, 5)
    assert serial_gt(0, 0xFFFF_FFFF)  # just after wrap
    assert not serial_gt(0xFFFF_FFFF, 0)
