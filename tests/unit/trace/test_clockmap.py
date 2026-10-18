"""Spec 06 §2.1 rule 2: synced -> rx_minus_half_rtt -> gateway_rx."""

from __future__ import annotations

from vak.core.trace import ClockMap
from vak.core.types import ClockQuality


def test_gateway_rx_when_nothing_known() -> None:
    cm = ClockMap()
    assert cm.to_gateway_ms(client_ms=12345, rx_ms=5000) == (5000, ClockQuality.GATEWAY_RX)
    assert cm.to_gateway_ms(client_ms=None, rx_ms=5000) == (5000, ClockQuality.GATEWAY_RX)


def test_rx_minus_half_rtt_when_only_rtt_known() -> None:
    cm = ClockMap()
    cm.note_rtt(80)
    assert cm.to_gateway_ms(client_ms=12345, rx_ms=5000) == (5000 - 40, ClockQuality.RX_MINUS_HALF_RTT)
    assert cm.rtt_ms == 80


def test_synced_with_anchor_uses_offset() -> None:
    cm = ClockMap()
    # client clock at 1000 when the gateway (at 9040, rtt 80) received the pong -> stamped ~9000
    cm.add_anchor(client_mono_ms=1000, server_ms=9040, rtt_ms=80)
    assert cm.offset_ms == 8000
    assert cm.to_gateway_ms(client_ms=1500, rx_ms=99999) == (9500, ClockQuality.SYNCED)
    # a frame without a client timestamp still falls back to rx - rtt/2
    assert cm.to_gateway_ms(client_ms=None, rx_ms=9600) == (9560, ClockQuality.RX_MINUS_HALF_RTT)


def test_min_rtt_anchor_wins_and_window_is_bounded() -> None:
    cm = ClockMap(max_anchors=3)
    cm.add_anchor(1000, 9040, 80)  # offset 8000
    cm.add_anchor(2000, 10300, 600)  # noisy: offset 8000? no: 10300-300-2000 = 8000 as well; make it differ
    cm.add_anchor(3000, 11070, 20)  # offset 8060, best rtt
    assert cm.offset_ms == 8060
    cm.add_anchor(4000, 12100, 200)
    assert len(cm._anchors) == 3  # oldest evicted
    cm2 = ClockMap()
    cm2.add_anchor(0, 1000, 1000)  # offset 500, rtt 1000
    cm2.add_anchor(0, 600, 100)  # offset 550, rtt 100 -> wins
    assert cm2.offset_ms == 550
