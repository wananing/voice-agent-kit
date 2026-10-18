"""Audio codecs for the wire (spec 01 §4.1; architecture §7 item 7).

Opus is the spec default; `pcm16` is accepted so a dev box without libopus
can run the kit. Both work on 20 ms mono PCM16 frames at 16 kHz or 24 kHz.
`opuslib` is imported lazily and only by `OpusCodec`.
"""

from __future__ import annotations

import importlib.util
from typing import Any, Protocol

FRAME_MS = 20  # spec 01 §4.1
SUPPORTED_RATES = (16000, 24000)


class CodecUnavailable(RuntimeError):
    """Raised when the optional codec library is not installed."""


class AudioCodec(Protocol):
    name: str
    sample_rate: int
    frame_ms: int

    def encode(self, pcm16: bytes) -> bytes: ...

    def decode(self, data: bytes) -> bytes: ...


def frame_bytes(sample_rate: int, frame_ms: int = FRAME_MS) -> int:
    """Bytes of PCM16 mono in one frame."""
    return sample_rate * frame_ms // 1000 * 2


def opus_available() -> bool:
    return importlib.util.find_spec("opuslib") is not None


def gateway_codecs() -> tuple[str, ...]:
    """Codecs this gateway can negotiate, in preference order."""
    return ("opus", "pcm16") if opus_available() else ("pcm16",)


class Pcm16Codec:
    """Identity codec: the wire payload is raw little-endian PCM16 mono."""

    name = "pcm16"

    def __init__(self, sample_rate: int = 16000, frame_ms: int = FRAME_MS) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms

    def encode(self, pcm16: bytes) -> bytes:
        return pcm16

    def decode(self, data: bytes) -> bytes:
        return data


class OpusCodec:
    """Opus mono, DTX off, FEC off (spec 01 §4.1). One call = one frame."""

    name = "opus"

    def __init__(self, sample_rate: int = 16000, frame_ms: int = FRAME_MS, bitrate: int = 20_000) -> None:
        if sample_rate not in SUPPORTED_RATES:
            raise ValueError(f"opus sample rate must be one of {SUPPORTED_RATES}, got {sample_rate}")
        try:
            import opuslib  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise CodecUnavailable(
                "OpusCodec needs the optional dependency `opuslib` (pip install 'voice-agent-kit[opus]'); "
                "negotiate `pcm16` instead or install libopus."
            ) from e
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self._frame_samples = sample_rate * frame_ms // 1000
        self._enc: Any = opuslib.Encoder(sample_rate, 1, opuslib.APPLICATION_VOIP)
        self._enc.bitrate = bitrate
        self._dec: Any = opuslib.Decoder(sample_rate, 1)

    def encode(self, pcm16: bytes) -> bytes:
        if len(pcm16) != self._frame_samples * 2:
            raise ValueError(f"opus frame must be {self._frame_samples * 2} bytes, got {len(pcm16)}")
        return self._enc.encode(pcm16, self._frame_samples)

    def decode(self, data: bytes) -> bytes:
        return self._dec.decode(data, self._frame_samples)


def make_codec(name: str, sample_rate: int, frame_ms: int = FRAME_MS) -> AudioCodec:
    if name == "pcm16":
        return Pcm16Codec(sample_rate, frame_ms)
    if name == "opus":
        return OpusCodec(sample_rate, frame_ms)
    raise ValueError(f"unknown codec {name!r}")
