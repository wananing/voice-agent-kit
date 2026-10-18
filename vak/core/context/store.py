"""ContextStore: the local record, the only truth (spec 05 §2.1).

Append-only entries. `text_presented` is settled only from playback
receipts; `text_generated` is kept for trace and never projected. The
store also owns the persona, the state block store, the current summary
and `session_seq`; the rebuilder and the snapshot store read from it.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from vak.core.clock import Clock
from vak.core.context.persona import render_persona
from vak.core.context.projection import INTERRUPT_MARKER, project_history
from vak.core.context.state_block import StateBlockStore
from vak.core.defaults import ContextParams
from vak.core.ids import IdGen
from vak.core.ports import TracePort
from vak.core.types import (
    ContextEntry,
    EntrySource,
    HistoryFormat,
    HistoryItem,
    HistoryScope,
    InterruptAt,
    Persona,
    PlaybackState,
    PresentedVia,
    Profile,
    Role,
    StateBlock,
    StateLayer,
    Summary,
    ToolCall,
    TurnId,
    UpstreamSynced,
    WriteEffect,
)
from vak.upstream.capabilities import Capabilities

log = logging.getLogger(__name__)

# Fallback speaking rate when no sentence duration is known: Mandarin ≈ 4
# chars/s (handbook LK-B §6.1 "per-character uniform rate", error unverified).
_EST_MS_PER_CHAR = 250

WriteState = Literal["none", "executed", "unknown"]


class ContextStore:
    """Implements `ContextPort` (vak/core/ports.py)."""

    def __init__(
        self,
        *,
        user_session_id: str,
        profile: Profile,
        params: ContextParams,
        clock: Clock,
        ids: IdGen,
        persona: Persona,
        state: StateBlockStore,
        trace: TracePort,
        tools_version: str,
        rules: str = "",
        history_format: HistoryFormat | None = None,
    ) -> None:
        self.user_session_id = user_session_id
        self.profile = profile
        self.params = params
        self.clock = clock
        self.ids = ids
        self.persona = persona
        self.state = state
        self.trace = trace
        self.tools_version = tools_version
        self.rules = rules
        self._history_format = history_format
        self.entries: list[ContextEntry] = []
        self.summary: Summary | None = None
        self.session_seq = 1
        self._k_effective = params.recent_turns_k
        self._segments: dict[str, int] = {}  # turn → user segments merged (spec 05 §2.1 rule 4)

    # ------------------------------------------------------------------ record

    def record_user(
        self, turn_id: TurnId, text: str, t_start_ms: int, t_end_ms: int, source: str = "user_voice"
    ) -> ContextEntry:
        """One user entry per turn: segments of the same turn merge (spec 05 §2.1 rule 4)."""
        existing = next((e for e in self._turn_entries(turn_id) if e.role is Role.USER), None)
        if existing is not None:
            sep = " " if existing.text_user and existing.text_user[-1].isascii() else ""
            existing.text_user = (existing.text_user or "") + sep + text
            existing.t_end_ms = max(existing.t_end_ms or 0, t_end_ms)
            self._segments[str(turn_id)] = self._segments.get(str(turn_id), 1) + 1
            return existing
        self._segments[str(turn_id)] = 1
        return self._append(
            ContextEntry(
                entry_id=self.ids.new("ent"),
                user_session_id=self.user_session_id,
                session_seq=self.session_seq,
                turn_id=turn_id,
                role=Role.USER,
                source=EntrySource(source),
                t_start_ms=t_start_ms,
                t_end_ms=t_end_ms,
                text_user=text,
            )
        )

    def user_segments(self, turn_id: TurnId) -> int:
        return self._segments.get(str(turn_id), 0)

    def record_assistant_pending(
        self,
        turn_id: TurnId,
        request_id: str,
        sentence_id: int,
        text_generated: str,
        source: str,
        presented_via: PresentedVia = PresentedVia.VOICE,
    ) -> ContextEntry:
        return self._append(
            ContextEntry(
                entry_id=self.ids.new("ent"),
                user_session_id=self.user_session_id,
                session_seq=self.session_seq,
                turn_id=turn_id,
                role=Role.ASSISTANT,
                source=EntrySource(source),
                t_start_ms=self.clock.now_ms(),
                sentence_id=sentence_id,
                presented_via=presented_via,
                text_generated=text_generated,
                text_presented="",
                playback_state=PlaybackState.PENDING,
                request_id=request_id,
                upstream_synced=UpstreamSynced.LIVE if source == "model" else UpstreamSynced.NONE,
            )
        )

    def record_assistant_playback(
        self,
        request_id: str,
        sentence_id: int,
        state: str,
        played_ms: int,
        estimated: bool = False,
        duration_ms: int | None = None,
    ) -> ContextEntry | None:
        """Settle `text_presented` from a receipt (spec 05 §2.1 rule 1).
        `state` is a receipt kind (started/ended/interrupted) or a
        `PlaybackState` value. `duration_ms` is the sentence's audio length
        when known; otherwise it is estimated from the text and the offset
        is flagged `estimated`."""
        entry = next(
            (e for e in reversed(self.entries)
             if e.role is Role.ASSISTANT and e.request_id == request_id and e.sentence_id == sentence_id),
            None,
        )
        if entry is None:
            log.warning("playback receipt for unknown sentence %s/%s", request_id, sentence_id)
            return None
        text = entry.text_generated or ""
        now = self.clock.now_ms()
        if state in ("started", "playing"):
            entry.playback_state = PlaybackState.PLAYING
            entry.t_start_ms = now
        elif state in ("ended", "completed"):
            entry.playback_state = PlaybackState.COMPLETED
            entry.text_presented = text
            entry.t_end_ms = now
        elif state == "skipped":
            entry.playback_state = PlaybackState.SKIPPED
            entry.text_presented = ""
            entry.t_end_ms = now
        elif state == "interrupted":
            if duration_ms is None:
                duration_ms = max(1, len(text) * _EST_MS_PER_CHAR)
                estimated = True
            offset = min(len(text), max(0, round(len(text) * played_ms / max(1, duration_ms))))
            entry.playback_state = PlaybackState.INTERRUPTED if offset > 0 else PlaybackState.SKIPPED
            entry.interrupted = offset > 0
            entry.text_presented = text[:offset]
            entry.interrupt_at = InterruptAt(played_ms=played_ms, char_offset=offset, estimated=estimated)
            entry.t_end_ms = now
        else:
            raise ValueError(f"unknown playback state {state!r}")
        return entry

    def record_tool_call(self, call: ToolCall, write_effect: WriteEffect | None = None) -> ContextEntry:
        return self._append(
            ContextEntry(
                entry_id=self.ids.new("ent"),
                user_session_id=self.user_session_id,
                session_seq=self.session_seq,
                turn_id=call.turn_id,
                role=Role.TOOL_CALL,
                source=EntrySource.MODEL,
                t_start_ms=call.received_at_ms,
                tool_call_id=call.call_id,
                tool_name=call.name,
                tool_args=dict(call.args),
                write_effect=write_effect,
                upstream_synced=UpstreamSynced.LIVE,
            )
        )

    def set_write_effect(self, call_id: str, write_effect: WriteEffect) -> None:
        """Settle the write effect of an earlier `tool_call` entry (spec 05 §4.5)."""
        for e in reversed(self.entries):
            if e.role is Role.TOOL_CALL and e.tool_call_id == call_id:
                e.write_effect = write_effect
                return
        raise KeyError(f"no tool_call entry for {call_id}")

    def record_tool_output(self, call: ToolCall) -> ContextEntry:
        return self._append(
            ContextEntry(
                entry_id=self.ids.new("ent"),
                user_session_id=self.user_session_id,
                session_seq=self.session_seq,
                turn_id=call.turn_id,
                role=Role.TOOL_OUTPUT,
                source=EntrySource.LOCAL,
                t_start_ms=call.done_at_ms or self.clock.now_ms(),
                t_end_ms=call.closed_at_ms,
                tool_call_id=call.call_id,
                tool_name=call.name,
                tool_closed_by=call.closed_by,
                tool_summary=call.result.summary if call.result else None,
                upstream_synced=UpstreamSynced.LIVE if call.output_sent else UpstreamSynced.NONE,
            )
        )

    def record_system_event(self, turn_id: TurnId, text: str) -> ContextEntry:
        return self._append(
            ContextEntry(
                entry_id=self.ids.new("ent"),
                user_session_id=self.user_session_id,
                session_seq=self.session_seq,
                turn_id=turn_id,
                role=Role.SYSTEM_EVENT,
                source=EntrySource.LOCAL,
                t_start_ms=self.clock.now_ms(),
                text_user=text,
            )
        )

    # ------------------------------------------------------------------- query

    def presented_text(self, turn_id: TurnId) -> str:
        """What the user actually heard this turn (spec 05 §1.3), with the
        interruption marker when playback was cut. Cues are excluded."""
        parts: list[str] = []
        for e in sorted(
            (e for e in self._turn_entries(turn_id) if e.role is Role.ASSISTANT and e.source is not EntrySource.CUE),
            key=lambda e: e.sentence_id or 0,
        ):
            if e.text_presented:
                parts.append(e.text_presented)
            if e.interrupted:
                parts.append(INTERRUPT_MARKER)
                break
        return "".join(parts)

    def turn_ids(self) -> list[TurnId]:
        seen: dict[str, TurnId] = {}
        for e in self.entries:
            seen.setdefault(str(e.turn_id), e.turn_id)
        return list(seen.values())

    def last_user_text(self, turn_id: TurnId) -> str | None:
        return next((e.text_user for e in self._turn_entries(turn_id) if e.role is Role.USER), None)

    def has_presented_output(self, turn_id: TurnId) -> bool:
        return any(
            e.role is Role.ASSISTANT and e.source is not EntrySource.CUE and e.text_presented
            for e in self._turn_entries(turn_id)
        )

    def write_state_in_turn(self, turn_id: TurnId) -> WriteState:
        """Spec 05 §4.5 replay precondition: did this turn execute a write?"""
        state: WriteState = "none"
        for e in self._turn_entries(turn_id):
            if e.role is not Role.TOOL_CALL or e.write_effect is None:
                continue
            if e.write_effect.executed:
                return "executed"
            if not self._settled(e.tool_call_id):
                state = "unknown"  # write in flight when the session was lost
        return state

    def executed_writes(self) -> list[dict[str, Any]]:
        return [
            {"idempotency_key": e.write_effect.idempotency_key, "result": e.write_effect.result_ref}
            for e in self.entries
            if e.role is Role.TOOL_CALL and e.write_effect is not None and e.write_effect.executed
        ]

    def pending_tool_calls(self) -> list[str]:
        return [
            e.tool_call_id for e in self.entries
            if e.role is Role.TOOL_CALL and e.tool_call_id and not self._settled(e.tool_call_id)
        ]

    # -------------------------------------------------------------- projection

    def history_format(self, caps: Capabilities) -> HistoryFormat:
        if self._history_format is not None:
            return self._history_format
        return HistoryFormat.PAIRS_TOOLS_AS_TEXT if caps.get("history.tool_calls") == "as_text" else HistoryFormat.PAIRS

    def project_history(
        self, caps: Capabilities, exclude_turn_ids: set[str] | None = None
    ) -> tuple[list[HistoryItem], HistoryFormat, HistoryScope, int]:
        """Returns (items, history_format, history_scope, history_items)."""
        fmt = self.history_format(caps)
        entries = self.entries
        if exclude_turn_ids:
            entries = [e for e in entries if str(e.turn_id) not in exclude_turn_ids]
        items, scope, n = project_history(entries, self.summary, caps, fmt, self._k_effective, self.params)
        return items, fmt, scope, n

    def shrink_history(self) -> int:
        """Spec 05 §7.1 context_overflow: the cached summary (if any) is
        already in use, so halve the recent window. Returns the effective K;
        `set_summary` restores it."""
        self._k_effective = max(1, self._k_effective // 2)
        return self._k_effective

    def set_summary(self, summary: Summary) -> None:
        self.summary = summary
        self._k_effective = self.params.recent_turns_k

    def entries_before_recent(self) -> list[ContextEntry]:
        """Summary input: everything older than the recent-K window (spec 05 §5.2)."""
        turns = self.turn_ids()
        if len(turns) <= self.params.recent_turns_k:
            return []
        keep = {str(t) for t in turns[-self.params.recent_turns_k:]}
        return [e for e in self.entries if str(e.turn_id) not in keep]

    def turns_since_summary(self) -> int:
        if self.summary is None:
            return len(self.turn_ids())
        cut = next((i for i, e in enumerate(self.entries) if e.entry_id == self.summary.covers[1]), -1)
        return len({str(e.turn_id) for e in self.entries[cut + 1:]})

    def estimate_history_chars(self) -> int:
        return sum(len(e.text_user or "") + len(e.text_presented or "") for e in self.entries)

    # ------------------------------------------------------------- session seq

    def begin_upstream_session(self, session_seq: int) -> None:
        self.session_seq = session_seq

    def mark_seeded(self, entry_ids: list[str]) -> None:
        """Entries carried into a new upstream session by the rebuild (spec 05 §4.6)."""
        wanted = set(entry_ids)
        for e in self.entries:
            if e.entry_id in wanted:
                e.upstream_synced = UpstreamSynced.SEEDED

    def mark_live(self, entry_id: str) -> None:
        for e in self.entries:
            if e.entry_id == entry_id:
                e.upstream_synced = UpstreamSynced.LIVE
                return

    # ------------------------------------------------------------ state block

    def state_block(self) -> StateBlock:
        return self.state.block()

    def update_state(self, name: str, value: str) -> Literal["immediate", "lazy"]:
        layer = self.state.update(name, value)
        return "immediate" if layer is StateLayer.IMMEDIATE else "lazy"

    def render_system_prefix(self) -> str:
        """Persona → rules (spec 05 §2.3 rule 2), without the state block."""
        parts = [render_persona(self.persona)]
        if self.rules:
            parts.append(self.rules)
        return "\n\n".join(parts)

    def render_instructions(self) -> str:
        """S2S system prompt: persona → rules → state block at the end."""
        return self.render_system_prefix() + "\n\n" + self.render_messages_tail()

    def render_messages_tail(self) -> str:
        """Cascade / text LLM: the state block goes at the message tail so
        the prefix stays cacheable (spec 05 §2.2 position rule)."""
        return "当前状态（版本 " + self.state.version + "）：\n" + self.state.render()

    def prompt_version(self, prefix: str) -> str:
        """Spec 06 §3.2 `prompt_version` suffix: persona, tools, state versions."""
        return f"{prefix}/persona@{self.persona.version}+tools@{self.tools_version}+state@{self.state.version}"

    # ----------------------------------------------------------------- helpers

    def _append(self, e: ContextEntry) -> ContextEntry:
        self.entries.append(e)
        return e

    def _turn_entries(self, turn_id: TurnId) -> list[ContextEntry]:
        key = str(turn_id)
        return [e for e in self.entries if str(e.turn_id) == key]

    def _settled(self, call_id: str | None) -> bool:
        return any(
            e.role is Role.TOOL_OUTPUT and e.tool_call_id == call_id and e.tool_closed_by is not None
            for e in self.entries
        )
