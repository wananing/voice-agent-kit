"""moshi frame parsing and Ogg/Opus framing (identity codec, no libopus)."""

from __future__ import annotations

import asyncio

import pytest

from vak.core.clock import FakeClock
from vak.upstream.providers._ogg_opus import OggOpusReader, OggOpusWriter, build_page, ogg_crc
from vak.upstream.providers.moshi import MT_AUDIO, MT_HANDSHAKE, MT_TEXT, MoshiClient, MoshiError

from .fakes import FakeWS, IdentityCodec


def make() -> MoshiClient:
    return MoshiClient({"url": "ws://moshi:8998"}, clock=FakeClock(), codec=(IdentityCodec(), IdentityCodec()))


def test_ogg_crc_known_vector():
    # RFC 3533 CRC of an all-zero header page body is deterministic; check self-consistency
    page = build_page(b"abc", serial=1, seq=0, granule=0)
    assert page[:4] == b"OggS" and page[26] == 1 and page[27] == 3 and page[-3:] == b"abc"
    crc_field = int.from_bytes(page[22:26], "little")
    zeroed = page[:22] + b"\0\0\0\0" + page[26:]
    assert ogg_crc(zeroed) == crc_field


def test_ogg_writer_reader_roundtrip_with_arbitrary_chunking():
    w = OggOpusWriter(IdentityCodec(), 24000, frame_ms=20)
    r = OggOpusReader(IdentityCodec(), 24000)
    pcm = bytes(range(256)) * 30  # 7680 bytes = 3840 samples = 160 ms -> 8 frames
    stream = w.append_pcm(pcm)
    assert stream.startswith(b"OggS") and b"OpusHead" in stream and b"OpusTags" in stream
    out = b""
    for i in range(0, len(stream), 13):  # feed in odd pieces
        out += r.append_bytes(stream[i : i + 13])
    assert out == pcm
    # partial frame stays buffered until completed
    assert w.append_pcm(b"\0" * 10) == b""
    assert len(r.append_bytes(w.append_pcm(b"\0" * (960 - 10)))) == 960


def test_ogg_packets_over_255_bytes_use_continued_lacing():
    w = OggOpusWriter(IdentityCodec(), 24000, frame_ms=20)
    r = OggOpusReader(IdentityCodec(), 24000)
    pcm = b"\x01" * 960  # one 20 ms frame = 960 bytes > 255
    page_stream = w.append_pcm(pcm)
    assert r.append_bytes(page_stream) == pcm


def test_parse_frames_audio_text_error_and_ignored():
    c = make()
    c._reader = OggOpusReader(IdentityCodec(), 24000)
    w = OggOpusWriter(IdentityCodec(), 24000)
    audio_payload = w.append_pcm(b"\x02" * 960)
    f = c.parse_frame(bytes([MT_AUDIO]) + audio_payload)
    assert f is not None and f.pcm16 == b"\x02" * 960 and f.sample_rate == 24000 and f.text is None
    t = c.parse_frame(bytes([MT_TEXT]) + b"hello")
    assert t is not None and t.text == "hello" and t.pcm16 is None
    assert c.parse_frame(bytes([MT_HANDSHAKE]) + b"\0" * 8) is None
    assert c.parse_frame(bytes([4]) + b"{}") is None  # metadata
    assert c.parse_frame(b"") is None
    with pytest.raises(MoshiError, match="boom"):
        c.parse_frame(bytes([5]) + b"boom")


async def test_connect_waits_for_handshake_then_streams(monkeypatch):
    c = make()
    ws = FakeWS([bytes([MT_HANDSHAKE]) + b"\0" * 8])

    async def fake_connect(url, headers):
        assert url == "ws://moshi:8998/api/chat"
        return ws

    monkeypatch.setattr(c, "_ws_connect", fake_connect)
    await c.connect({})
    await c.send(b"\x03" * 960)
    assert ws.sent and ws.sent[0][0] == MT_AUDIO and ws.sent[0][1:5] == b"OggS"
    srv = OggOpusWriter(IdentityCodec(), 24000)
    ws.feed(bytes([MT_AUDIO]) + srv.append_pcm(b"\x04" * 960), bytes([MT_TEXT]) + b"hi")
    ws.end()
    frames = [f async for f in c.receive()]
    assert [(f.pcm16, f.text) for f in frames] == [(b"\x04" * 960, None), (None, "hi")]
    await c.disconnect()
    assert ws.closed


async def test_connect_handshake_timeout(monkeypatch):
    clock = FakeClock()
    c = MoshiClient({"connect_timeout_ms": 100}, clock=clock, codec=(IdentityCodec(), IdentityCodec()))
    ws = FakeWS()

    async def fake_connect(url, headers):
        return ws

    monkeypatch.setattr(c, "_ws_connect", fake_connect)
    task = asyncio.create_task(c.connect({}))
    for _ in range(5):  # let the clock.sleep waiter register before advancing
        await asyncio.sleep(0)
    await clock.advance(200)
    with pytest.raises(TimeoutError):
        await task


def test_missing_opuslib_is_a_clear_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "opuslib":
            raise ImportError("no opuslib")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(__import__("sys").modules, "opuslib", raising=False)
    c = MoshiClient({}, clock=FakeClock())
    with pytest.raises(Exception, match="opuslib"):
        c._make_codec()
