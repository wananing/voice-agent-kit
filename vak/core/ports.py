"""Interfaces between the five core modules (architecture §4.3).

Modules call each other only through these Protocols so each can be unit
tested with a fake on the other side. Session wires concrete instances.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from vak.core.types import (
    ContextEntry,
    HistoryItem,
    InterruptSource,
    OutputRequest,
    PlaybackReceipt,
    StateBlock,
    ToolCall,
    TurnId,
    TurnSource,
)
from vak.upstream.base import UpstreamEvent
from vak.upstream.capabilities import Capabilities


class TracePort(Protocol):
    """Each component writes only its own fields (spec 06 §7)."""

    def write(self, turn_id: TurnId, fragment: dict[str, Any]) -> None: ...

    def append(self, turn_id: TurnId, list_field: str, item: dict[str, Any]) -> None: ...

    def incr(self, turn_id: TurnId, counter: str, by: int = 1) -> None: ...

    def now_ms(self) -> int: ...

    def mark_complete(self, turn_id: TurnId, what: str) -> None:
        """Signal that `usage` or the last `receipt` for the turn has arrived
        so the collector can finalize before its wait deadline (spec 06 §2.5)."""
        ...

    def on_usage(self, turn_id: TurnId, usage: Any, interrupted: bool, response_status: str | None = None) -> None: ...


class TransportPort(Protocol):
    """What the arbiter and turn machine need from the wire (spec 01 §2.6, §2.7)."""

    async def send_json(self, msg: dict[str, Any]) -> None: ...

    async def send_audio(
        self, pcm16: bytes, turn_id: TurnId, sentence_id: int, frame_idx: int, generation: int,
        uninterruptible: bool = False,
    ) -> bool:
        """Return False if the frame was dropped by the transport's own
        generation filter (counted as consumer_dropped_frames)."""
        ...

    async def flush(self, turn_ids: list[TurnId], reason: str, ack_of: str | None) -> str:
        """Clear the send queue, emit flush.start/flush.end in the same tick.
        Returns flush_id."""
        ...

    def set_generation(self, generation: int) -> None: ...

    @property
    def lead_ms(self) -> int: ...

    @property
    def sample_rate(self) -> int:
        """Negotiated downlink sample rate; callers resample before `send_audio`."""
        ...


class ArbiterPort(Protocol):
    """Spec 02 §2.9."""

    @property
    def generation(self) -> int: ...

    async def submit(self, req: OutputRequest) -> None: ...

    async def on_turn_start(self, turn_id: TurnId, source: TurnSource, merged: bool = False) -> int:
        """Returns the new generation."""
        ...

    async def on_barge_in(self, turn_id: TurnId, source: InterruptSource) -> int: ...

    def set_user_speaking(self, speaking: bool) -> None: ...

    def set_endpointing(self, turn_id: TurnId, active: bool) -> None: ...

    def set_awaiting_tool(self, turn_id: TurnId, awaiting: bool) -> None: ...

    async def on_reply_done(self, turn_id: TurnId, outcome: str) -> None: ...

    async def on_receipt(self, receipt: PlaybackReceipt) -> None: ...

    async def flush_origin(self, origin: str, turn_id: TurnId | None = None, dedupe_key: str | None = None) -> None: ...

    async def feed_upstream_audio(
        self, request_id: str, pcm16: bytes, sample_rate: int, response_id: str, item_id: str, generation: int
    ) -> None:
        """Deliver one upstream audio delta to the request that owns it."""
        ...

    async def end_upstream_stream(self, request_id: str) -> None: ...

    def request_for_response(self, response_id: str) -> str | None: ...


class ContextPort(Protocol):
    """Spec 05 §2. Local record is the only truth."""

    def record_user(self, turn_id: TurnId, text: str, t_start_ms: int, t_end_ms: int, source: str = "user_voice") -> ContextEntry: ...

    def record_assistant_pending(
        self, turn_id: TurnId, request_id: str, sentence_id: int, text_generated: str, source: str
    ) -> ContextEntry: ...

    def record_assistant_playback(
        self,
        request_id: str,
        sentence_id: int,
        state: str,
        played_ms: int,
        estimated: bool = False,
        duration_ms: int | None = None,
    ) -> ContextEntry | None: ...

    def record_tool_call(self, call: ToolCall) -> ContextEntry: ...

    def record_tool_output(self, call: ToolCall) -> ContextEntry: ...

    def record_system_event(self, turn_id: TurnId, text: str) -> ContextEntry: ...

    def presented_text(self, turn_id: TurnId) -> str: ...

    def project_history(self, caps: Capabilities) -> tuple[list[HistoryItem], Any, Any, int]:
        """Returns (items, HistoryFormat, HistoryScope, history_items)."""
        ...

    def state_block(self) -> StateBlock: ...

    def render_instructions(self) -> str: ...

    def update_state(self, name: str, value: str) -> Literal["immediate", "lazy"]: ...


class ToolPort(Protocol):
    """Spec 03. Executes tool calls; speaks via the arbiter; returns via upstream."""

    async def on_tool_call(self, event: UpstreamEvent) -> None: ...

    async def on_barge_in(self, generation: int) -> None: ...

    async def on_turn_closed(self, turn_id: TurnId) -> None: ...

    async def on_session_rebuilt(self) -> None: ...

    def has_open_calls(self, turn_id: TurnId) -> bool: ...

    def tool_table(self) -> list[Any]: ...


class CuePort(Protocol):
    def get(self, cue_id: str) -> tuple[bytes, int, int] | None:
        """Returns (pcm16, sample_rate, duration_ms) or None."""
        ...


EventHandler = Callable[[dict[str, Any]], Awaitable[None]]
