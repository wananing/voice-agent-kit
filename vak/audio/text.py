"""Sentence splitting for streaming text (spec 01/02 first-segment rule;
handbook first-audio: first segment ≥ 6 chars cut at punctuation, forced
at 12–15 chars; later segments cut at sentence punctuation).

Works for Chinese and English. Never splits inside a number like "3.5".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_STRONG = "。！？!?；;…\n"
_WEAK = "，,、：:"
_NUM_DOT = re.compile(r"\d\.\d")


@dataclass(slots=True)
class SplitParams:
    first_min_chars: int = 6
    first_force_chars: int = 14  # 12–15
    later_min_chars: int = 2
    later_force_chars: int = 60


def split_sentences(buffer: str, *, first: bool, params: SplitParams | None = None) -> tuple[list[str], str]:
    """Return (complete_sentences, remainder).

    `first=True` applies the short first-segment rule so the first TTS
    request can start early. Callers keep `remainder` and call again with
    more text appended.
    """
    p = params or SplitParams()
    out: list[str] = []
    rest = buffer
    while rest:
        cut = _find_cut(rest, first=first and not out, p=p)
        if cut is None:
            break
        out.append(rest[:cut].strip())
        rest = rest[cut:]
    return [s for s in out if s], rest.lstrip()


def _find_cut(text: str, *, first: bool, p: SplitParams) -> int | None:
    min_chars = p.first_min_chars if first else p.later_min_chars
    force = p.first_force_chars if first else p.later_force_chars
    for i, ch in enumerate(text):
        n = i + 1
        if ch in _STRONG and n >= min_chars and not _inside_number(text, i):
            return n
        if first and ch in _WEAK and n >= min_chars and not _inside_number(text, i):
            return n
    if len(text) >= force:
        # prefer the last weak punctuation before the force point
        window = text[:force]
        for j in range(len(window) - 1, -1, -1):
            if window[j] in _WEAK + _STRONG and not _inside_number(text, j):
                return j + 1
        return force
    return None


def _inside_number(text: str, i: int) -> bool:
    return bool(_NUM_DOT.match(text[max(0, i - 1) : i + 2]))


def flush_remainder(remainder: str) -> list[str]:
    """At end of stream, whatever is left is one sentence."""
    r = remainder.strip()
    return [r] if r else []
