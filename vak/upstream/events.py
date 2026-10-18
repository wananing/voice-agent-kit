"""Event emission and error classification shared by all pipelines (spec 04 §4).

`EventEmitter` stamps every `UpstreamEvent` with a session-monotonic `seq`
and `t_local_ms` from the injected clock, and hands them to a single
async consumer through `events()`. `ErrorMapper` turns raw upstream error
codes/messages into the four-class `UpstreamError` using the adapter's
`errors.code_map` first and the §4.6 default rules second.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

from vak.core.clock import Clock
from vak.core.types import ErrorClass, ErrorReason, ErrorScope, ReplyModality, TurnId
from vak.upstream.base import (
    EventType,
    ResponseOrigin,
    ResponseStatus,
    UpstreamError,
    UpstreamEvent,
    Usage,
)
from vak.upstream.capabilities import Capabilities

log = logging.getLogger(__name__)

# Event types that carry a response's output. These are what "zero deltas
# after cancel" (spec 04 rule 7) refers to.
DELTA_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.RESPONSE_TEXT_DELTA,
        EventType.RESPONSE_AUDIO_DELTA,
        EventType.RESPONSE_SENTENCE,
        EventType.TRANSCRIPT_ASSISTANT,
    }
)


class EventEmitter:
    """Single-consumer event stream with monotonically increasing `seq`."""

    def __init__(self, clock: Clock, session_id: str | None = None) -> None:
        self._clock = clock
        self.session_id = session_id
        self._seq = 0
        self.queue: asyncio.Queue[UpstreamEvent | None] = asyncio.Queue()
        self._closed = False

    @property
    def seq(self) -> int:
        return self._seq

    def emit(self, ev: UpstreamEvent) -> UpstreamEvent:
        """Stamp seq/t_local_ms/session_id and enqueue. Returns the event."""
        self._seq += 1
        ev.seq = self._seq
        ev.t_local_ms = self._clock.now_ms()
        if ev.session_id is None:
            ev.session_id = self.session_id
        if not self._closed:
            self.queue.put_nowait(ev)
        return ev

    def close(self) -> None:
        """End the stream; `events()` returns after draining what is queued."""
        if not self._closed:
            self._closed = True
            self.queue.put_nowait(None)

    async def events(self) -> AsyncIterator[UpstreamEvent]:
        while True:
            ev = await self.queue.get()
            if ev is None:
                return
            yield ev

    def drain(self) -> list[UpstreamEvent]:
        """Return everything currently queued without waiting (tests, probes)."""
        out: list[UpstreamEvent] = []
        while True:
            try:
                ev = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return out
            if ev is None:
                self.queue.put_nowait(None)
                return out
            out.append(ev)

    # -- convenience constructors (spec 04 §4.2–4.7); they build, not emit --

    @staticmethod
    def _ev(type_: EventType, **fields: Any) -> UpstreamEvent:
        return UpstreamEvent(type=type_, seq=0, t_local_ms=0, **fields)

    def speech_started(self, turn_id: TurnId | None, source: str = "server_vad") -> UpstreamEvent:
        return self._ev(EventType.INPUT_SPEECH_STARTED, turn_id=turn_id, source=source)

    def speech_stopped(self, turn_id: TurnId | None, source: str = "server_vad") -> UpstreamEvent:
        return self._ev(EventType.INPUT_SPEECH_STOPPED, turn_id=turn_id, source=source)

    def segmented(self, turn_id: TurnId | None, segment_index: int) -> UpstreamEvent:
        return self._ev(EventType.INPUT_SEGMENTED, turn_id=turn_id, segment_index=segment_index)

    def committed(self, turn_id: TurnId | None, by: Literal["client", "server"]) -> UpstreamEvent:
        return self._ev(EventType.INPUT_COMMITTED, turn_id=turn_id, committed_by=by)

    def transcript_user_partial(self, turn_id: TurnId | None, text: str) -> UpstreamEvent:
        return self._ev(EventType.TRANSCRIPT_USER_PARTIAL, turn_id=turn_id, text=text, is_final=False)

    def transcript_user_final(
        self, turn_id: TurnId | None, text: str, *, final_by_timeout: bool = False, late: bool = False
    ) -> UpstreamEvent:
        return self._ev(
            EventType.TRANSCRIPT_USER_FINAL,
            turn_id=turn_id,
            text=text,
            is_final=True,
            final_by_timeout=final_by_timeout,
            late=late,
        )

    def transcript_assistant(
        self, response_id: str, item_id: str | None, text: str, is_final: bool, turn_id: TurnId | None = None
    ) -> UpstreamEvent:
        return self._ev(
            EventType.TRANSCRIPT_ASSISTANT,
            response_id=response_id,
            item_id=item_id,
            text=text,
            is_final=is_final,
            turn_id=turn_id,
        )

    def item_echo(self, item_id: str, kind: str, local_id: str | None = None) -> UpstreamEvent:
        return self._ev(EventType.ITEM_ECHO, item_id=item_id, kind=kind, local_id=local_id)

    def response_started(
        self,
        response_id: str,
        origin: ResponseOrigin,
        response_ref: str | None = None,
        turn_id: TurnId | None = None,
    ) -> UpstreamEvent:
        return self._ev(
            EventType.RESPONSE_STARTED,
            response_id=response_id,
            response_ref=response_ref,
            origin=origin,
            turn_id=turn_id,
        )

    def text_delta(
        self, response_id: str, item_id: str, text: str, turn_id: TurnId | None = None, response_ref: str | None = None
    ) -> UpstreamEvent:
        return self._ev(
            EventType.RESPONSE_TEXT_DELTA,
            response_id=response_id,
            response_ref=response_ref,
            item_id=item_id,
            text=text,
            turn_id=turn_id,
        )

    def audio_delta(
        self,
        response_id: str,
        item_id: str,
        audio: bytes,
        sample_rate: int,
        duration_ms: int,
        turn_id: TurnId | None = None,
        response_ref: str | None = None,
        sentence_index: int | None = None,
    ) -> UpstreamEvent:
        return self._ev(
            EventType.RESPONSE_AUDIO_DELTA,
            response_id=response_id,
            response_ref=response_ref,
            item_id=item_id,
            audio=audio,
            sample_rate=sample_rate,
            duration_ms=duration_ms,
            turn_id=turn_id,
            sentence_index=sentence_index,
        )

    def sentence(
        self,
        response_id: str,
        sentence_index: int,
        text: str,
        audio_ms: int | None,
        turn_id: TurnId | None = None,
        response_ref: str | None = None,
        submitted_at_ms: int | None = None,
    ) -> UpstreamEvent:
        return self._ev(
            EventType.RESPONSE_SENTENCE,
            submitted_at_ms=submitted_at_ms,
            response_id=response_id,
            response_ref=response_ref,
            item_id=str(sentence_index),
            sentence_index=sentence_index,
            text=text,
            audio_ms=audio_ms,
            turn_id=turn_id,
        )

    def response_done(
        self,
        response_id: str,
        status: ResponseStatus,
        reply_modality: ReplyModality,
        *,
        confirmed: bool | None = None,
        reason: str | None = None,
        turn_id: TurnId | None = None,
        response_ref: str | None = None,
    ) -> UpstreamEvent:
        return self._ev(
            EventType.RESPONSE_DONE,
            response_id=response_id,
            response_ref=response_ref,
            status=status,
            reply_modality=reply_modality,
            confirmed=confirmed,
            reason=reason,
            turn_id=turn_id,
        )

    def tool_call(
        self,
        call_id: str,
        name: str,
        args: dict[str, Any],
        response_id: str | None,
        turn_id: TurnId | None,
        *,
        before_commit: bool = False,
        response_ref: str | None = None,
    ) -> UpstreamEvent:
        return self._ev(
            EventType.TOOL_CALL,
            call_id=call_id,
            name=name,
            args=args,
            response_id=response_id,
            response_ref=response_ref,
            turn_id=turn_id,
            before_commit=before_commit,
        )

    def tool_call_cancelled(self, call_id: str, reason: str) -> UpstreamEvent:
        return self._ev(EventType.TOOL_CALL_CANCELLED, call_id=call_id, reason=reason)

    def session_opened(self, session_id: str, effective: dict[str, Any]) -> UpstreamEvent:
        return self._ev(EventType.SESSION_OPENED, session_id=session_id, effective=effective)

    def session_resumable(self, handle: str) -> UpstreamEvent:
        return self._ev(EventType.SESSION_RESUMABLE, handle=handle)

    def session_expiring(self, remaining_s: int, source: str) -> UpstreamEvent:
        return self._ev(EventType.SESSION_EXPIRING, remaining_s=remaining_s, source=source)

    def session_closed(
        self, reason: str, by: Literal["client", "server"], final_usage: Usage | None = None
    ) -> UpstreamEvent:
        return self._ev(EventType.SESSION_CLOSED, reason=reason, source=by, usage=final_usage)

    def backpressure(self, buffered_ms: int, turn_id: TurnId | None = None) -> UpstreamEvent:
        return self._ev(EventType.UPLINK_BACKPRESSURE, buffered_ms=buffered_ms, turn_id=turn_id)

    def usage(self, usage: Usage) -> UpstreamEvent:
        return self._ev(EventType.USAGE, response_id=usage.response_id, usage=usage)

    def error(
        self,
        err: UpstreamError,
        *,
        turn_id: TurnId | None = None,
        response_id: str | None = None,
        item_id: str | None = None,
        call_id: str | None = None,
    ) -> UpstreamEvent:
        return self._ev(
            EventType.ERROR,
            error=err,
            turn_id=turn_id,
            response_id=response_id,
            item_id=item_id,
            call_id=call_id,
        )


# ---------------------------------------------------------------------------
# Error classification (spec 04 §4.6)
# ---------------------------------------------------------------------------

# Default rule tables: (substring, reason). Matched case-insensitively against
# "<raw_code> <raw_message>". Order inside a class does not matter; order
# between classes follows §4.6 rules 1–7.
_FATAL: tuple[tuple[str, ErrorReason], ...] = (
    ("unauthorized", ErrorReason.AUTH),
    ("unauthenticated", ErrorReason.AUTH),
    ("authentication", ErrorReason.AUTH),
    ("authorization", ErrorReason.AUTH),
    ("invalid_api_key", ErrorReason.AUTH),
    ("api key", ErrorReason.AUTH),
    ("forbidden", ErrorReason.AUTH),
    ("permission", ErrorReason.AUTH),
    ("quota", ErrorReason.QUOTA),
    ("billing", ErrorReason.BILLING),
    ("insufficient_funds", ErrorReason.BILLING),
    ("payment", ErrorReason.BILLING),
    ("account suspended", ErrorReason.BILLING),
    ("account_deactivated", ErrorReason.BILLING),
)
_CONTEXT_OVERFLOW: tuple[str, ...] = (
    "context_length",
    "context length",
    "context window",
    "maximum context",
    "too long",
    "overflow",
    "1007",
)
_TRANSIENT: tuple[tuple[str, ErrorReason], ...] = (
    ("disconnect", ErrorReason.SESSION_LOST),
    ("connection", ErrorReason.SESSION_LOST),
    ("session_expired", ErrorReason.SESSION_LOST),
    ("session ended", ErrorReason.SESSION_LOST),
    ("closed", ErrorReason.SESSION_LOST),
    ("go_away", ErrorReason.SESSION_LOST),
    ("timeout", ErrorReason.TIMEOUT),
    ("timed out", ErrorReason.TIMEOUT),
    ("rate_limit", ErrorReason.THROTTLED),
    ("rate limit", ErrorReason.THROTTLED),
    ("throttl", ErrorReason.THROTTLED),
    ("429", ErrorReason.THROTTLED),
    ("503", ErrorReason.THROTTLED),
    ("overloaded", ErrorReason.THROTTLED),
    ("not ready", ErrorReason.THROTTLED),
    ("notready", ErrorReason.THROTTLED),
)
_BENIGN: tuple[tuple[str, ErrorReason], ...] = (
    ("cancel_not_active", ErrorReason.CANCEL_RACE),
    ("already_has_active_response", ErrorReason.CANCEL_RACE),
    ("cancellation failed", ErrorReason.CANCEL_RACE),
    ("commit_empty", ErrorReason.EMPTY_COMMIT),
    ("buffer too small", ErrorReason.EMPTY_COMMIT),
    ("buffer is empty", ErrorReason.EMPTY_COMMIT),
    ("duplicate", ErrorReason.DUPLICATE),
    ("moderation", ErrorReason.MODERATION),
    ("content_filter", ErrorReason.MODERATION),
    ("safety", ErrorReason.MODERATION),
    ("tool response parsing", ErrorReason.TOOL_PARSE),
    ("tool_parse", ErrorReason.TOOL_PARSE),
    ("invalid_item", ErrorReason.ITEM_REJECTED),
    ("item_rejected", ErrorReason.ITEM_REJECTED),
)


class ErrorMapper:
    """`errors.code_map` lookup, then the §4.6 default rules in order."""

    def __init__(self, caps: Capabilities) -> None:
        self._caps = caps
        code_map = caps.get("errors.code_map") or {}
        self._code_map: dict[str, dict[str, str]] = {str(k): dict(v) for k, v in code_map.items()}
        self._stable = caps.get("errors.code_stability") == "stable_codes"

    def map(
        self,
        raw_code: str | None,
        raw_message: str | None,
        *,
        scope: ErrorScope = ErrorScope.SESSION,
        ref: dict[str, str] | None = None,
        session_open: bool = True,
        response_failed: bool = False,
    ) -> UpstreamError:
        ref = dict(ref or {})
        mapped = self._lookup(raw_code, raw_message)
        if mapped is not None:
            cls, reason = mapped
        else:
            cls, reason, scope = self._default(
                raw_code, raw_message, scope=scope, ref=ref, session_open=session_open, response_failed=response_failed
            )
        return UpstreamError(
            error_class=cls, reason=reason, scope=scope, ref=ref, raw_code=raw_code, raw_message=raw_message
        )

    def _lookup(self, raw_code: str | None, raw_message: str | None) -> tuple[ErrorClass, ErrorReason] | None:
        if not self._code_map:
            return None
        if self._stable:
            entry = self._code_map.get(raw_code or "")
        else:  # rule 8: message substring match
            hay = f"{raw_code or ''} {raw_message or ''}".lower()
            entry = next((v for k, v in self._code_map.items() if k.lower() in hay), None)
        if entry is None:
            return None
        return ErrorClass(entry["class"]), ErrorReason(entry.get("reason", "unknown"))

    @staticmethod
    def _default(
        raw_code: str | None,
        raw_message: str | None,
        *,
        scope: ErrorScope,
        ref: dict[str, str],
        session_open: bool,
        response_failed: bool,
    ) -> tuple[ErrorClass, ErrorReason, ErrorScope]:
        hay = f"{raw_code or ''} {raw_message or ''}".lower()
        for needle, reason in _FATAL:  # rule 1
            if needle in hay:
                return ErrorClass.FATAL, reason, scope
        if not session_open:  # rule 2
            return ErrorClass.FATAL, ErrorReason.INVALID_CONFIG, ErrorScope.SESSION
        if any(n in hay for n in _CONTEXT_OVERFLOW):  # rule 3
            return ErrorClass.CONTEXT_OVERFLOW, ErrorReason.UNKNOWN, ErrorScope.SESSION
        for needle, reason in _TRANSIENT:  # rule 4
            if needle in hay:
                return ErrorClass.TRANSIENT, reason, scope
        for needle, reason in _BENIGN:  # rule 5
            if needle in hay:
                return ErrorClass.BENIGN, reason, scope
        if ref.get("event_id") or ref.get("item_id"):  # rule 5: item-level error with correlation
            return ErrorClass.BENIGN, ErrorReason.ITEM_REJECTED, ErrorScope.ITEM
        if response_failed:  # rule 6
            return ErrorClass.BENIGN, ErrorReason.UNKNOWN, ErrorScope.RESPONSE
        log.warning("unmapped error code: code=%r message=%r", raw_code, raw_message)  # rule 7
        return ErrorClass.TRANSIENT, ErrorReason.UNKNOWN, scope


def timeout_error(reason_detail: str, scope: ErrorScope, *, error_class: ErrorClass = ErrorClass.TRANSIENT) -> UpstreamError:
    """Error payload for a primitive timeout (spec 04 §3.0, rule 8)."""
    return UpstreamError(
        error_class=error_class, reason=ErrorReason.TIMEOUT, scope=scope, raw_code=reason_detail, raw_message=None
    )
