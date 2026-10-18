"""Local record → upstream history (spec 05 §3, pure function).

Rules enforced here, in this order:
- `system_event` and `source=cue` never project (spec 05 §2.1 rule 2);
- assistant text is `text_presented` only; `skipped` / nothing played → not
  projected; interrupted → natural-language marker (spec 05 §3.3);
- unsettled tool calls (no `closed_by`) are excluded (spec 05 §3.4 rule 4);
- tool entries follow `history.tool_calls`: keep / as_text / drop;
- history = [summary pair] + last K turns (spec 05 §5.5);
- `history.limits` trims whole turns (oldest first); `history.constraints`
  violations raise `ProjectionError` naming the rule (never silently fixed).
"""

from __future__ import annotations

from dataclasses import dataclass

from vak.core.defaults import ContextParams
from vak.core.types import (
    ContextEntry,
    EntrySource,
    HistoryFormat,
    HistoryItem,
    HistoryScope,
    PlaybackState,
    Role,
    Summary,
)
from vak.upstream.capabilities import Capabilities

INTERRUPT_MARKER = "（说到这里被用户打断）"  # spec 05 §3.3 rule 3, suggested wording
SUMMARY_PLACEHOLDER_USER = "（之前聊过的内容）"  # spec 05 §5.3 default placement
PACKED_HEADER = "以下是之前的对话记录。"
PACKED_TRAILER = "（这是之前的对话，只用于衔接，不是新请求。）"  # spec 05 §3.2
_ROLE_LABEL = {"user": "用户", "assistant": "助手"}


class ProjectionError(ValueError):
    """A capability constraint or limit cannot be met by trimming turns."""


@dataclass(slots=True)
class _Block:
    items: list[HistoryItem]
    is_summary: bool = False


def project_history(
    entries: list[ContextEntry],
    summary: Summary | None,
    caps: Capabilities,
    history_format: HistoryFormat,
    k_recent: int,
    params: ContextParams,
) -> tuple[list[HistoryItem], HistoryScope, int]:
    """Returns (items, history_scope, history_items)."""
    tools_mode = _tools_mode(caps, history_format)
    settled = {
        e.tool_call_id for e in entries if e.role is Role.TOOL_OUTPUT and e.tool_closed_by is not None
    }
    start = _summary_cut(entries, summary)
    visible = [e for e in entries[start:] if _projectable(e, settled, tools_mode)]
    turns = _group_turns(visible)
    if k_recent < 1:
        raise ValueError("k_recent must be >= 1")
    dropped, recent = turns[:-k_recent], turns[-k_recent:]

    scope = HistoryScope.FULL
    if summary is not None:
        scope = HistoryScope.SUMMARY_RECENT
    elif dropped:
        scope = HistoryScope.TRUNCATED

    blocks: list[_Block] = []
    if summary is not None:
        blocks.append(
            _Block(
                [HistoryItem("user", SUMMARY_PLACEHOLDER_USER), HistoryItem("assistant", summary.text)],
                is_summary=True,
            )
        )
    blocks.extend(_Block(_turn_items(t, tools_mode)) for t in recent)
    blocks = [b for b in blocks if b.items]

    limits = caps.get("history.limits") or {}
    while True:
        items = _render(blocks, history_format)
        over = _over_limit(items, limits)
        if over is None:
            break
        turn_blocks = [i for i, b in enumerate(blocks) if not b.is_summary]
        if not turn_blocks or over == "max_item_bytes":
            raise ProjectionError(f"history.limits: {over}")
        del blocks[turn_blocks[0]]
        if scope is HistoryScope.FULL:
            scope = HistoryScope.TRUNCATED

    _check_constraints(items, caps.get("history.constraints") or [])
    return items, scope, len(items)


# ---------------------------------------------------------------------------


def _tools_mode(caps: Capabilities, fmt: HistoryFormat) -> str:
    mode = caps.get("history.tool_calls")
    if mode == "drop":
        return "drop"
    if fmt is HistoryFormat.PAIRS_TOOLS_AS_TEXT or mode == "as_text":
        return "as_text"
    return "keep"


def _summary_cut(entries: list[ContextEntry], summary: Summary | None) -> int:
    """Index of the first entry after the summary's coverage."""
    if summary is None:
        return 0
    for i, e in enumerate(entries):
        if e.entry_id == summary.covers[1]:
            return i + 1
    return 0


def _projectable(e: ContextEntry, settled: set[str | None], tools_mode: str) -> bool:
    if e.role is Role.SYSTEM_EVENT or e.source is EntrySource.CUE:
        return False
    if e.role is Role.USER:
        return bool(e.text_user)
    if e.role is Role.ASSISTANT:
        if e.playback_state in (PlaybackState.SKIPPED, PlaybackState.PENDING, None):
            return False
        return bool(e.text_presented)
    # tool_call / tool_output
    if tools_mode == "drop":
        return False
    return e.tool_call_id in settled


def _group_turns(entries: list[ContextEntry]) -> list[list[ContextEntry]]:
    order: list[str] = []
    by_turn: dict[str, list[ContextEntry]] = {}
    for e in entries:
        key = str(e.turn_id)
        if key not in by_turn:
            order.append(key)
            by_turn[key] = []
        by_turn[key].append(e)
    return [by_turn[k] for k in order]


def _turn_items(turn: list[ContextEntry], tools_mode: str) -> list[HistoryItem]:
    """One user item, assistant runs coalesced, tool items in place."""
    items: list[HistoryItem] = []
    run: list[str] = []
    run_ids: list[str] = []

    def flush() -> None:
        if run:
            items.append(HistoryItem("assistant", "\n".join(run), local_entry_id=run_ids[0]))
            run.clear()
            run_ids.clear()

    last_kind = ""
    for e in turn:
        if e.role is Role.USER:
            flush()
            items.append(HistoryItem("user", e.text_user or "", local_entry_id=e.entry_id))
            last_kind = "user"
        elif e.role is Role.ASSISTANT:
            text = (e.text_presented or "") + (INTERRUPT_MARKER if e.interrupted else "")
            if run and last_kind == "speech":
                run[-1] += text  # sentences of one reply join without separator
            else:
                run.append(text)
                run_ids.append(e.entry_id)
            last_kind = "speech"
        elif e.role is Role.TOOL_CALL:
            last_kind = "tool"
            if tools_mode == "keep":
                flush()
                items.append(
                    HistoryItem(
                        "tool_call", "", tool_call_id=e.tool_call_id, tool_name=e.tool_name,
                        tool_args=e.tool_args, local_entry_id=e.entry_id,
                    )
                )
            else:
                run.append(f"（调用了工具 {e.tool_name}，参数 {e.tool_args or {}}）")
                run_ids.append(e.entry_id)
        elif e.role is Role.TOOL_OUTPUT:
            last_kind = "tool"
            closed = e.tool_closed_by.value if e.tool_closed_by else ""
            if tools_mode == "keep":
                flush()
                items.append(
                    HistoryItem(
                        "tool_output", e.tool_summary or "", tool_call_id=e.tool_call_id,
                        tool_name=e.tool_name, local_entry_id=e.entry_id,
                    )
                )
            else:
                run.append(f"（工具 {e.tool_name} 返回：{e.tool_summary or ''}，收口 {closed}）")
                run_ids.append(e.entry_id)
    flush()
    return items


def _render(blocks: list[_Block], fmt: HistoryFormat) -> list[HistoryItem]:
    items = [it for b in blocks for it in b.items]
    if fmt is not HistoryFormat.PACKED or not items:
        return items
    lines = [PACKED_HEADER]
    for it in items:
        if it.role in ("user", "assistant"):
            lines.append(f"{_ROLE_LABEL[it.role]}：{it.text}")
        elif it.role == "tool_call":
            lines.append(f"助手：（调用了工具 {it.tool_name}，参数 {it.tool_args or {}}）")
        else:
            lines.append(f"助手：（工具 {it.tool_name} 返回：{it.text}）")
    lines.append(PACKED_TRAILER)
    return [HistoryItem("user", "\n".join(lines), local_entry_id=items[0].local_entry_id)]


def _over_limit(items: list[HistoryItem], limits: dict) -> str | None:
    max_items = limits.get("max_items")
    if max_items is not None and len(items) > int(max_items):
        return "max_items"
    max_tokens = limits.get("max_tokens")
    if max_tokens is not None and sum(len(it.text) for it in items) > int(max_tokens):
        # 1 char ≈ 1 token is the conservative estimate for CJK text
        return "max_tokens"
    max_item_bytes = limits.get("max_item_bytes")
    if max_item_bytes is not None and any(len(it.text.encode("utf-8")) > int(max_item_bytes) for it in items):
        return "max_item_bytes"
    return None


def _check_constraints(items: list[HistoryItem], constraints: list) -> None:
    if not items:
        return
    for rule in constraints:
        name = rule.get("rule") if isinstance(rule, dict) else str(rule)
        if name == "start_with_user" and items[0].role != "user":
            raise ProjectionError("history.constraints: start_with_user")
        if name == "end_with_user" and items[-1].role != "user":
            raise ProjectionError("history.constraints: end_with_user")
