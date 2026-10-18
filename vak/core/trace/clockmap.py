"""Client clock -> gateway clock mapping (spec 06 §2.1 rule 2).

Ping anchors give (client_mono_ms, gateway receive ms, rtt). With at least
one anchor a client timestamp maps as `client + offset` -> `synced`; with
only an RTT estimate the frame receive time minus RTT/2 is used ->
`rx_minus_half_rtt`; with nothing the receive time itself -> `gateway_rx`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vak.core.types import ClockQuality


@dataclass(frozen=True, slots=True)
class ClockAnchor:
    client_mono_ms: int
    server_ms: int  # gateway clock when the pong carrying client_mono_ms arrived
    rtt_ms: int

    @property
    def offset_ms(self) -> int:
        # client stamped at roughly server_ms - rtt/2 on the gateway clock
        return (self.server_ms - self.rtt_ms // 2) - self.client_mono_ms


@dataclass(slots=True)
class ClockMap:
    max_anchors: int = 8
    _anchors: list[ClockAnchor] = field(default_factory=list)
    _rtt_ms: int | None = None

    def add_anchor(self, client_mono_ms: int, server_ms: int, rtt_ms: int) -> None:
        self._anchors.append(ClockAnchor(client_mono_ms, server_ms, rtt_ms))
        if len(self._anchors) > self.max_anchors:
            del self._anchors[0]
        self._rtt_ms = rtt_ms

    def note_rtt(self, rtt_ms: int) -> None:
        """RTT measured without a client timestamp (ping without anchor)."""
        self._rtt_ms = rtt_ms

    @property
    def rtt_ms(self) -> int | None:
        return self._rtt_ms

    @property
    def offset_ms(self) -> int | None:
        """Offset from the anchor with the smallest RTT (least uncertainty)."""
        if not self._anchors:
            return None
        return min(self._anchors, key=lambda a: a.rtt_ms).offset_ms

    def to_gateway_ms(self, client_ms: int | None, rx_ms: int) -> tuple[int, ClockQuality]:
        """Map a client time point to the gateway clock.

        `client_ms` is the client's own timestamp (None if the frame had
        none); `rx_ms` is when the gateway received the frame."""
        offset = self.offset_ms
        if client_ms is not None and offset is not None:
            return client_ms + offset, ClockQuality.SYNCED
        if self._rtt_ms is not None:
            return rx_ms - self._rtt_ms // 2, ClockQuality.RX_MINUS_HALF_RTT
        return rx_ms, ClockQuality.GATEWAY_RX
