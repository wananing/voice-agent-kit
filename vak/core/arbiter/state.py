"""Explicit state tables for the arbiter (spec 02 §2.3) and the request
lifecycle (spec 02 §2.4). Illegal transitions raise `IllegalTransition`
(architecture §7.1)."""

from __future__ import annotations

import enum

from vak.core.types import IllegalTransition, RequestStatus


class ArbiterState(enum.StrEnum):
    IDLE = "idle"
    GRANTED = "granted"
    FLUSHING = "flushing"


class ArbiterEvent(enum.StrEnum):
    GRANT = "grant"  # a grantable request found while IDLE
    RELEASE = "release"  # last sentence ended / receipt timeout / interrupted by client
    FLUSH = "flush"  # generation +1 / directed flush / critical arrival
    FLUSH_END = "flush_end"  # local flush_end or 1 s timeout
    CRITICAL = "critical"  # critical request granted while FLUSHING


# spec 02 §2.3
ARBITER_TRANSITIONS: dict[tuple[ArbiterState, ArbiterEvent], ArbiterState] = {
    (ArbiterState.IDLE, ArbiterEvent.GRANT): ArbiterState.GRANTED,
    (ArbiterState.GRANTED, ArbiterEvent.RELEASE): ArbiterState.IDLE,
    (ArbiterState.GRANTED, ArbiterEvent.FLUSH): ArbiterState.FLUSHING,
    (ArbiterState.FLUSHING, ArbiterEvent.FLUSH): ArbiterState.FLUSHING,  # merged into one flush
    (ArbiterState.FLUSHING, ArbiterEvent.FLUSH_END): ArbiterState.IDLE,
    (ArbiterState.FLUSHING, ArbiterEvent.CRITICAL): ArbiterState.GRANTED,
}


class RequestEvent(enum.StrEnum):
    GRANT = "grant"
    DROP = "drop"
    FIRST_FRAME = "first_frame"
    STARTED = "started"
    ENDED = "ended"  # last sentence ended (or ended-receipt timeout)
    INTERRUPT = "interrupt"  # flush / generation expired / playback.interrupted / started timeout
    DELIVERED = "delivered"  # interrupted by the user: counts as delivered
    RETRY = "retry"
    REQUEUE = "requeue"


# spec 02 §2.4
REQUEST_TRANSITIONS: dict[tuple[RequestStatus, RequestEvent], RequestStatus] = {
    (RequestStatus.QUEUED, RequestEvent.GRANT): RequestStatus.GRANTED,
    (RequestStatus.QUEUED, RequestEvent.DROP): RequestStatus.DROPPED,
    (RequestStatus.GRANTED, RequestEvent.FIRST_FRAME): RequestStatus.SENT,
    (RequestStatus.GRANTED, RequestEvent.ENDED): RequestStatus.DONE,  # empty content
    (RequestStatus.GRANTED, RequestEvent.INTERRUPT): RequestStatus.INTERRUPTED,
    (RequestStatus.SENT, RequestEvent.STARTED): RequestStatus.PLAYING,
    (RequestStatus.SENT, RequestEvent.ENDED): RequestStatus.DONE,  # started receipt lost
    (RequestStatus.SENT, RequestEvent.INTERRUPT): RequestStatus.INTERRUPTED,
    (RequestStatus.PLAYING, RequestEvent.ENDED): RequestStatus.DONE,
    (RequestStatus.PLAYING, RequestEvent.INTERRUPT): RequestStatus.INTERRUPTED,
    (RequestStatus.INTERRUPTED, RequestEvent.DELIVERED): RequestStatus.DONE,
    (RequestStatus.INTERRUPTED, RequestEvent.DROP): RequestStatus.DROPPED,
    (RequestStatus.INTERRUPTED, RequestEvent.RETRY): RequestStatus.RETRY_WAIT,
    (RequestStatus.RETRY_WAIT, RequestEvent.REQUEUE): RequestStatus.QUEUED,
}


def arbiter_next(state: ArbiterState, event: ArbiterEvent) -> ArbiterState:
    try:
        return ARBITER_TRANSITIONS[(state, event)]
    except KeyError:
        raise IllegalTransition(f"arbiter: {state.value} + {event.value}") from None


def request_next(status: RequestStatus, event: RequestEvent) -> RequestStatus:
    try:
        return REQUEST_TRANSITIONS[(status, event)]
    except KeyError:
        raise IllegalTransition(f"request: {status.value} + {event.value}") from None
