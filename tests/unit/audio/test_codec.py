"""Spec 01 §4.1 / architecture §7 item 7: pcm16 identity, opus optional, frame sizes."""

from __future__ import annotations

import math

import numpy as np
import pytest

from vak.audio.codec import OpusCodec, Pcm16Codec, frame_bytes, gateway_codecs, make_codec, opus_available


def tone(rate: int, ms: int = 20, freq: float = 440.0) -> bytes:
    t = np.arange(rate * ms // 1000) / rate
    return (0.3 * 32767 * np.sin(2 * math.pi * freq * t)).astype(np.int16).tobytes()


def test_frame_bytes_20ms() -> None:
    assert frame_bytes(16000) == 640
    assert frame_bytes(24000) == 960


def test_pcm16_identity_and_gateway_codecs() -> None:
    c = Pcm16Codec(16000)
    assert c.name == "pcm16" and c.encode(b"ab") == b"ab" and c.decode(b"ab") == b"ab"
    assert "pcm16" in gateway_codecs()
    assert isinstance(make_codec("pcm16", 24000), Pcm16Codec)
    with pytest.raises(ValueError):
        make_codec("g722", 16000)


@pytest.mark.skipif(not opus_available(), reason="opuslib not installed")
@pytest.mark.parametrize("rate", [16000, 24000])
def test_opus_roundtrip(rate: int) -> None:
    c = OpusCodec(rate)
    pcm = tone(rate)
    packet = c.encode(pcm)
    assert 0 < len(packet) < len(pcm)
    out = c.decode(packet)
    assert len(out) == frame_bytes(rate)
    with pytest.raises(ValueError):
        c.encode(pcm[:100])


def test_opus_unavailable_is_clear_error() -> None:
    if opus_available():
        pytest.skip("opuslib installed")
    from vak.audio.codec import CodecUnavailable

    with pytest.raises(CodecUnavailable):
        OpusCodec(16000)
