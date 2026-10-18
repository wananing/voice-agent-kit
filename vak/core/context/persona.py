"""Persona: a sectioned checklist, not prose (spec 05 §2.3).

Sections in fixed order: role (who / how to self-address / background /
personality), register, hard rules, banned words, boundaries. The persona
carries a version and is rendered into the system prompt before the rules
and the state block; it never changes mid-session (spec 05 §2.3 rule 3).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from vak.core.types import Persona

# Section keys accepted in persona.yaml, in render order (spec 05 §2.3 table).
_ROLE_KEYS = ("role", "background", "personality")
_ROLE_LABELS = {"role": "角色", "background": "背景", "personality": "性格"}

# Heuristics for "background must not contain quotable facts" (spec 05 §2.3:
# no digits, dates, place names). Place-name detection is suffix based and
# deliberately conservative; extend `place_names` for product-specific checks.
_DIGIT_RE = re.compile(r"\d")
_CJK_DATE_RE = re.compile(r"[零一二三四五六七八九十百千两]+(?:年|月|日|号)")
_PLACE_RE = re.compile(r"[一-鿿]{2,4}(?:省|市|县|镇|村|大道|街道)")
_PLACE_FALSE_POSITIVES = {"城市", "都市", "超市", "集市", "市场", "农村", "乡村", "山村", "村庄", "小镇"}


def load_persona(src: str | Path | dict[str, Any]) -> Persona:
    """Load persona.yaml (or an already-parsed dict) into a `Persona`.

    `role`, `background` and `personality` are folded into `Persona.role`
    as labelled lines; the other sections map one to one."""
    data: dict[str, Any] = src if isinstance(src, dict) else yaml.safe_load(Path(src).read_text(encoding="utf-8"))
    role_lines = [f"{_ROLE_LABELS[k]}：{_as_text(data.get(k))}" for k in _ROLE_KEYS if data.get(k)]
    return Persona(
        version=str(data.get("version", "0")),
        role="\n".join(role_lines),
        register=_as_text(data.get("register")),
        hard_rules=[str(r) for r in (data.get("hard_rules") or [])],
        banned_words=[str(w) for w in (data.get("banned_words") or [])],
        boundaries=_as_text(data.get("boundaries")),
    )


def render_persona(persona: Persona) -> str:
    """Render the persona sections in the fixed order of spec 05 §2.3."""
    parts = [persona.role]
    if persona.register:
        parts.append(f"语域：{persona.register}")
    if persona.hard_rules:
        parts.append("硬规则：" + "".join(f"\n- {r}" for r in persona.hard_rules))
    if persona.banned_words:
        parts.append("不要用这些词：" + "、".join(persona.banned_words))
    if persona.boundaries:
        parts.append(f"边界：{persona.boundaries}")
    return "\n".join(p for p in parts if p)


def check_persona(persona: Persona, place_names: tuple[str, ...] = ()) -> list[str]:
    """Automatic checks (spec 05 §2.3, §10.1). Returns a list of problems;
    empty means the persona passes."""
    problems: list[str] = []
    background = persona.role
    if _DIGIT_RE.search(background):
        problems.append("background: contains digits")
    if _CJK_DATE_RE.search(background):
        problems.append("background: contains a date")
    for m in _PLACE_RE.finditer(background):
        if m.group(0) not in _PLACE_FALSE_POSITIVES:
            problems.append(f"background: contains a place name {m.group(0)!r}")
            break
    for name in place_names:
        if name and name in background:
            problems.append(f"background: contains a place name {name!r}")
            break
    if not persona.hard_rules:
        problems.append("hard_rules: missing")
    # banned words must not appear in the persona text itself (minus the list)
    text = "\n".join([persona.role, persona.register, *persona.hard_rules, persona.boundaries])
    for w in persona.banned_words:
        if w and w in text:
            problems.append(f"banned_words: {w!r} used in persona text")
    return problems


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return "、".join(str(x) for x in v)
    return str(v).strip()
