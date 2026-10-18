"""Minimal Ogg/Opus framing (RFC 3533 pages, RFC 7845 headers).

moshi's wire protocol carries "ogg frames containing opus encoded audio"
(moshi/rust/protocol.md). Its own clients use `sphn`, which hides the Ogg
container; with plain `opuslib` we must mux/demux pages ourselves. The codec
is injected (anything with opuslib's `encode(pcm, frame_size)` /
`decode(packet, frame_size)` shape) so the framing is testable without libopus.
"""

from __future__ import annotations

import struct
from typing import Protocol

_OGG_MAGIC = b"OggS"
_HEADER_LEN = 27


class OpusEncoderLike(Protocol):
    def encode(self, pcm_data: bytes, frame_size: int) -> bytes: ...


class OpusDecoderLike(Protocol):
    def decode(self, opus_data: bytes, frame_size: int, decode_fec: bool = False) -> bytes: ...


def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else (r << 1)
        table.append(r & 0xFFFFFFFF)
    return table


_CRC = _crc_table()


def ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) & 0xFF) ^ b]
    return crc


def _lacing(length: int) -> bytes:
    full, rest = divmod(length, 255)
    return bytes([255] * full + [rest])


def build_page(payload: bytes, *, serial: int, seq: int, granule: int, header_type: int = 0) -> bytes:
    lacing = _lacing(len(payload))
    head = struct.pack("<4sBBqIIIB", _OGG_MAGIC, 0, header_type, granule, serial, seq, 0, len(lacing))
    page = bytearray(head + lacing + payload)
    struct.pack_into("<I", page, 22, ogg_crc(bytes(page)))
    return bytes(page)


def opus_head(sample_rate: int, channels: int = 1, pre_skip: int = 0) -> bytes:
    return b"OpusHead" + struct.pack("<BBHIhB", 1, channels, pre_skip, sample_rate, 0, 0)


def opus_tags(vendor: bytes = b"vak") -> bytes:
    return b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)


class OggOpusWriter:
    """PCM16 mono in, Ogg/Opus bytes out. Header pages are emitted with the
    first audio page."""

    def __init__(self, encoder: OpusEncoderLike, sample_rate: int, *, frame_ms: int = 20, serial: int = 0x5641) -> None:
        self._enc = encoder
        self._rate = sample_rate
        self._frame = sample_rate * frame_ms // 1000
        self._serial = serial
        self._seq = 0
        self._granule = 0  # 48 kHz units per RFC 7845
        self._buf = bytearray()
        self._started = False

    def _page(self, payload: bytes, header_type: int = 0) -> bytes:
        page = build_page(payload, serial=self._serial, seq=self._seq, granule=self._granule, header_type=header_type)
        self._seq += 1
        return page

    def append_pcm(self, pcm16: bytes) -> bytes:
        self._buf.extend(pcm16)
        out = bytearray()
        if not self._started:
            self._started = True
            out += self._page(opus_head(self._rate), header_type=0x02)
            out += self._page(opus_tags())
        frame_bytes = self._frame * 2
        while len(self._buf) >= frame_bytes:
            frame = bytes(self._buf[:frame_bytes])
            del self._buf[:frame_bytes]
            packet = self._enc.encode(frame, self._frame)
            self._granule += self._frame * 48000 // self._rate
            out += self._page(packet)
        return bytes(out)


class OggOpusReader:
    """Ogg/Opus bytes in, PCM16 mono out. Tolerates arbitrary chunking."""

    def __init__(self, decoder: OpusDecoderLike, sample_rate: int) -> None:
        self._dec = decoder
        self._max_frame = sample_rate * 120 // 1000
        self._buf = bytearray()
        self._packet = bytearray()
        self._headers_seen = 0

    def append_bytes(self, data: bytes) -> bytes:
        self._buf.extend(data)
        pcm = bytearray()
        for packet in self._drain_packets():
            if self._headers_seen < 2:
                if packet[:8] in (b"OpusHead", b"OpusTags"):
                    self._headers_seen += 1
                    continue
                self._headers_seen = 2  # stream without headers: treat as audio
            pcm += self._dec.decode(packet, self._max_frame)
        return bytes(pcm)

    def _drain_packets(self) -> list[bytes]:
        packets: list[bytes] = []
        while True:
            idx = self._buf.find(_OGG_MAGIC)
            if idx < 0:
                del self._buf[: max(0, len(self._buf) - 3)]  # keep a possibly split magic
                return packets
            if idx:
                del self._buf[:idx]
            if len(self._buf) < _HEADER_LEN:
                return packets
            n_seg = self._buf[26]
            if len(self._buf) < _HEADER_LEN + n_seg:
                return packets
            lacing = self._buf[_HEADER_LEN : _HEADER_LEN + n_seg]
            body_len = sum(lacing)
            total = _HEADER_LEN + n_seg + body_len
            if len(self._buf) < total:
                return packets
            body = bytes(self._buf[_HEADER_LEN + n_seg : total])
            del self._buf[:total]
            pos = 0
            for lv in lacing:
                self._packet += body[pos : pos + lv]
                pos += lv
                if lv < 255:
                    packets.append(bytes(self._packet))
                    self._packet.clear()
