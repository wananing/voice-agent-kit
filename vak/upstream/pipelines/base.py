"""Scaffolding shared by the four pipelines (spec 04 §3.0, rules 6–8).

- `PipelineBase` owns the emitter, error mapper, mirror, the lock that
  serialises open/close/update_config, the cancelled-response set that
  makes "zero deltas after cancel" structural, and the clock-driven
  timeout helper that reports every primitive timeout as `Ack.timeout`
  plus an `error` event.
- `ResponseTracker` guarantees every `response.started` has exactly one
  `response.done`.

No retries, no reconnects, no timers that recycle sessions live here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable
from typing import Any, TypeVar

from vak.core.clock import Clock, MonotonicClock
from vak.core.defaults import DEFAULTS, UpstreamTimeouts
from vak.core.ids import IdGen
from vak.core.types import ErrorClass, ErrorScope, ReplyModality, TurnId
from vak.upstream.base import Ack, ResponseOrigin, ResponseOverrides, ResponseStatus, UpstreamEvent
from vak.upstream.capabilities import Capabilities
from vak.upstream.events import DELTA_TYPES, ErrorMapper, EventEmitter, timeout_error
from vak.upstream.mirror import Mirror

log = logging.getLogger(__name__)

T = TypeVar("T")


class _Timeout:
    """Sentinel returned by `PipelineBase._with_timeout`."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "TIMEOUT"


TIMEOUT = _Timeout()


class ResponseTracker:
    """Started/done pairing (spec 04 rule 8). `done` is idempotent per id."""

    def __init__(self, emit: Any) -> None:
        self._emit = emit  # PipelineBase._emit
        self._active: dict[str, UpstreamEvent] = {}  # response_id -> started event
        self._done: set[str] = set()

    def start(self, started: UpstreamEvent) -> bool:
        rid = started.response_id
        assert rid is not None
        if rid in self._active or rid in self._done:
            return False
        self._active[rid] = started
        self._emit(started)
        return True

    def done(self, done: UpstreamEvent) -> bool:
        rid = done.response_id
        assert rid is not None
        started = self._active.pop(rid, None)
        if started is None:
            return False
        self._done.add(rid)
        if done.turn_id is None:
            done.turn_id = started.turn_id
        if done.response_ref is None:
            done.response_ref = started.response_ref
        self._emit(done)
        return True

    def is_active(self, response_id: str) -> bool:
        return response_id in self._active

    def active_ids(self) -> list[str]:
        return list(self._active)

    def started_event(self, response_id: str) -> UpstreamEvent | None:
        return self._active.get(response_id)

    def is_done(self, response_id: str) -> bool:
        return response_id in self._done

    def bind_ref(self, response_id: str, response_ref: str) -> None:
        st = self._active.get(response_id)
        if st is not None and st.response_ref is None:
            st.response_ref = response_ref

    def ref_of(self, response_id: str) -> str | None:
        st = self._active.get(response_id)
        return st.response_ref if st else None


class PipelineBase:
    """Common state and helpers. Subclasses implement the nine primitives."""

    capabilities: Capabilities

    def __init__(
        self,
        caps: Capabilities,
        clock: Clock | None = None,
        timeouts: UpstreamTimeouts | None = None,
        ids: IdGen | None = None,
    ) -> None:
        self.capabilities = caps
        self.clock: Clock = clock or MonotonicClock()
        self.timeouts = timeouts or DEFAULTS.upstream
        self.ids = ids or IdGen()
        self._emitter = EventEmitter(self.clock)
        self._errors = ErrorMapper(caps)
        self._mirror = Mirror()
        self._lock = asyncio.Lock()  # open / close / update_config (spec 04 §3.0)
        self._cancelled: set[str] = set()
        self.suppressed = 0  # deltas dropped after cancel (rule 7)
        self._tracker = ResponseTracker(self._emit)
        self._open = False
        self._closed = False
        self.session_id: str | None = None

    # -- events --------------------------------------------------------------

    def events(self) -> AsyncIterator[UpstreamEvent]:
        return self._emitter.events()

    @property
    def mirror(self) -> Mirror:
        return self._mirror

    def _emit(self, ev: UpstreamEvent) -> UpstreamEvent | None:
        """Emit unless it is a delta of a cancelled response (rule 7)."""
        if ev.response_id is not None and ev.response_id in self._cancelled and ev.type in DELTA_TYPES:
            self.suppressed += 1
            return None
        self._mirror.on_event(ev)
        return self._emitter.emit(ev)

    def _emit_error(
        self,
        err_class: ErrorClass,
        reason_detail: str,
        scope: ErrorScope,
        *,
        turn_id: TurnId | None = None,
        response_id: str | None = None,
        item_id: str | None = None,
        call_id: str | None = None,
    ) -> None:
        err = timeout_error(reason_detail, scope, error_class=err_class)
        self._emit(
            self._emitter.error(err, turn_id=turn_id, response_id=response_id, item_id=item_id, call_id=call_id)
        )

    # -- timeouts (spec 04 §3.0: every timeout is reported as an event) ------

    async def _with_timeout(
        self,
        aw: Awaitable[T],
        ms: int,
        detail: str,
        scope: ErrorScope,
        *,
        error_class: ErrorClass = ErrorClass.TRANSIENT,
        turn_id: TurnId | None = None,
        response_id: str | None = None,
        item_id: str | None = None,
        call_id: str | None = None,
    ) -> T | _Timeout:
        """Race `aw` against `clock.sleep(ms)`. On timeout: cancel `aw`,
        emit `error{reason: timeout}` and return TIMEOUT."""
        task = asyncio.ensure_future(aw)
        sleeper = asyncio.ensure_future(self.clock.sleep(ms))
        try:
            done, _ = await asyncio.wait({task, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            task.cancel()
            sleeper.cancel()
            raise
        if task in done:
            sleeper.cancel()
            return task.result()
        task.cancel()
        self._emit_error(
            error_class, detail, scope, turn_id=turn_id, response_id=response_id, item_id=item_id, call_id=call_id
        )
        return TIMEOUT

    # -- responses -----------------------------------------------------------

    def _start_response(
        self,
        response_id: str,
        origin: ResponseOrigin,
        response_ref: str | None,
        turn_id: TurnId | None,
    ) -> None:
        self._tracker.start(self._emitter.response_started(response_id, origin, response_ref, turn_id))

    def _finish_response(
        self,
        response_id: str,
        status: ResponseStatus,
        reply_modality: ReplyModality,
        *,
        confirmed: bool | None = None,
        reason: str | None = None,
    ) -> bool:
        return self._tracker.done(
            self._emitter.response_done(response_id, status, reply_modality, confirmed=confirmed, reason=reason)
        )

    def _mark_cancelled(self, response_id: str) -> None:
        self._cancelled.add(response_id)

    def _cancel_all_active(self, reason: str) -> None:
        """Used by close: in-flight responses end as cancelled (spec 04 §3.9)."""
        for rid in self._tracker.active_ids():
            self._mark_cancelled(rid)
            self._finish_response(rid, ResponseStatus.CANCELLED, ReplyModality.NONE, reason=reason)

    # -- small helpers -------------------------------------------------------

    @staticmethod
    def _unsupported(field: str) -> Ack:
        return Ack.unsupported(field)

    def _check_overrides(self, overrides: ResponseOverrides | None) -> Ack | None:
        """Spec 04 §3.4: any unsupported override fails the whole request with
        `unsupported(response.overrides.<field>)`; nothing is dropped or rewritten."""
        if overrides is None:
            return None
        allowed = self.capabilities.get("response.overrides") or {}
        if overrides.instructions is not None and allowed.get("instructions") != "replace":
            return Ack.unsupported("response.overrides.instructions")
        if overrides.modalities is not None and not allowed.get("modalities"):
            return Ack.unsupported("response.overrides.modalities")
        if overrides.tools is not None and not allowed.get("tools"):
            return Ack.unsupported("response.overrides.tools")
        if overrides.tool_choice is not None and not allowed.get("tool_choice"):
            return Ack.unsupported("response.overrides.tool_choice")
        return None

    async def send_text(self, turn_id: TurnId, text: str) -> Ack:
        """Default: no text item on this upstream (overridden by text-out and s2s)."""
        return Ack.unsupported("uplink.text_input")

    def _require_open(self) -> Ack | None:
        if self._closed:
            return Ack.invalid_state("closed")
        if not self._open:
            return Ack.invalid_state("not_open")
        return None
