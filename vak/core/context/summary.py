"""Background summariser for long sessions (spec 05 §5).

Input is only `text_user`, `text_presented` and settled `tool_output`
summaries — never `text_generated`, never a previous summary (spec 05
§5.2, §5.6). Runs as a task off the hot path; if the covered entries
changed while the LLM was working, the result is discarded and redone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from vak.core.clock import Clock
from vak.core.defaults import ContextParams
from vak.core.types import ContextEntry, EntrySource, Role, Summary
from vak.upstream.providers.base import LLM, ChatMessage

if TYPE_CHECKING:
    from vak.core.context.store import ContextStore

log = logging.getLogger(__name__)

SUMMARY_SYSTEM_PROMPT = (
    "把下面的对话压成一段简短的前情摘要，供助手在新会话里衔接。"
    "保留关键事实、用户偏好、未决事项和已知的工具结果；省略寒暄。"
    "只用对话里出现过的信息，不要补充。用自然短句，不要列表。"
)
_MAX_REDO = 2


class Summarizer:
    def __init__(self, llm: LLM | None, params: ContextParams, clock: Clock) -> None:
        self.llm = llm
        self.params = params
        self.clock = clock
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self.llm is not None

    def should_trigger(self, turns_since_summary: int, estimated_chars: int, max_tokens: int | None) -> bool:
        """Spec 05 §5.1 rule 2: turns since last summary reach
        `turn_limit_threshold - 6`, or projected length reaches 70 % of the
        upstream history limit."""
        if not self.enabled:
            return False
        turn_trigger = max(1, self.params.turn_limit_threshold - self.params.summary_trigger_turns_before_limit)
        if turns_since_summary >= turn_trigger:
            return True
        return max_tokens is not None and estimated_chars >= 0.7 * int(max_tokens)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def schedule(self, context: ContextStore) -> asyncio.Task[None] | None:
        """Start a background summary of everything older than the recent window."""
        if not self.enabled or self.running:
            return None

        async def run() -> None:
            summary = await self.summarize(lambda: context.entries_before_recent())
            if summary is not None:
                context.set_summary(summary)

        self._task = asyncio.get_running_loop().create_task(run())
        return self._task

    async def summarize(self, entries_provider: Callable[[], list[ContextEntry]]) -> Summary | None:
        """Summarise the entries returned by `entries_provider`, re-reading
        them afterwards to detect changes (spec 05 §5.1 rule 3)."""
        if self.llm is None:
            return None
        for _ in range(_MAX_REDO):
            entries = _cut_at_unsettled(entries_provider())
            if not entries:
                return None
            before = _fingerprint(entries)
            text = await self._call_llm(_lines(entries))
            after = _fingerprint(_cut_at_unsettled(entries_provider()))
            if before == after:
                return Summary(
                    is_summary=True,
                    covers=(entries[0].entry_id, entries[-1].entry_id),
                    text=text.strip(),
                    created_at_ms=self.clock.now_ms(),
                )
            log.info("summary input changed while summarising; redoing")
        return None

    async def _call_llm(self, lines: list[str]) -> str:
        assert self.llm is not None
        messages = [
            ChatMessage("system", SUMMARY_SYSTEM_PROMPT),
            ChatMessage("user", "\n".join(lines)),
        ]
        out: list[str] = []
        async for delta in self.llm.stream(messages):
            if delta.text:
                out.append(delta.text)
        return "".join(out)


def _cut_at_unsettled(entries: list[ContextEntry]) -> list[ContextEntry]:
    """Never cut through an open tool call: stop before the first one (spec 05 §5.2)."""
    settled = {e.tool_call_id for e in entries if e.role is Role.TOOL_OUTPUT and e.tool_closed_by is not None}
    for i, e in enumerate(entries):
        if e.role is Role.TOOL_CALL and e.tool_call_id not in settled:
            return entries[:i]
    return entries


def _lines(entries: list[ContextEntry]) -> list[str]:
    lines: list[str] = []
    for e in entries:
        if e.source is EntrySource.CUE or e.role is Role.SYSTEM_EVENT:
            continue
        if e.role is Role.USER and e.text_user:
            lines.append(f"用户：{e.text_user}")
        elif e.role is Role.ASSISTANT and e.text_presented:
            lines.append(f"助手：{e.text_presented}" + ("（被打断）" if e.interrupted else ""))
        elif e.role is Role.TOOL_OUTPUT and e.tool_closed_by is not None and e.tool_summary:
            lines.append(f"已知信息（工具 {e.tool_name}）：{e.tool_summary}")
    return lines


def _fingerprint(entries: list[ContextEntry]) -> tuple[tuple[str, str | None, str | None], ...]:
    return tuple((e.entry_id, e.text_presented, e.playback_state.value if e.playback_state else None) for e in entries)
