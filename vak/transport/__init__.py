"""va/1 wire layer: framing, JSON protocol, pacing, WebSocket and loopback transports."""

from . import protocol
from .base import (
    BaseTransport,
    HandshakeFailed,
    LoopbackTransport,
    OnAudio,
    OnMessage,
    ResumeLookup,
    ResumeState,
    TransportClosed,
)
from .framing import (
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
from .pacer import DownlinkPacer, PacedFrame
from .protocol import (
    AudioDownChoice,
    ClientCaps,
    ClientHello,
    ProtocolError,
    ResumeRequest,
    negotiate_audio_down,
    negotiate_audio_up,
    negotiate_mode,
    parse_hello,
    validate_uplink,
)
from .websocket import WebSocketTransport

__all__ = [
    "HEADER_LEN",
    "AudioDownChoice",
    "BaseTransport",
    "ClientCaps",
    "ClientHello",
    "DownlinkHeader",
    "DownlinkPacer",
    "FramingError",
    "HandshakeFailed",
    "LoopbackTransport",
    "OnAudio",
    "OnMessage",
    "PacedFrame",
    "ProtocolError",
    "ResumeLookup",
    "ResumeRequest",
    "ResumeState",
    "TransportClosed",
    "UplinkHeader",
    "WebSocketTransport",
    "negotiate_audio_down",
    "negotiate_audio_up",
    "negotiate_mode",
    "pack_downlink",
    "pack_uplink",
    "parse_hello",
    "protocol",
    "serial_gt",
    "unpack_downlink",
    "unpack_uplink",
    "validate_uplink",
]
