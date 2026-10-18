"""Render `voice_template` strings with result data (spec 03 §2.1, §3.1).

`{field}` placeholders are filled from the result; every number is turned
into spoken Chinese so the local TTS never reads digits (handbook KM 2.7
"数字逐位念" for codes; cardinal reading for quantities).

  23        → 二十三        3.5     → 三点五
  10005     → 一万零五      "14:30" → 十四点三十
  "13800138000" → 幺三八零零幺三八零零零 (per digit, ≥ 5 digits)
  "2024年"  → 二零二四年   "007"   → 零零七
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_DIGITS = "零一二三四五六七八九"
# per-digit reading uses 幺 for 1 (phone numbers, codes) as spoken Chinese does
_DIGITS_SPOKEN = "零幺二三四五六七八九"
_UNITS = ("", "十", "百", "千")
_BIG = ("", "万", "亿", "万亿")

_TIME = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _group(g: int) -> str:
    """0 < g < 10000 → 千百十 reading with collapsed internal zeros."""
    out = ""
    zero_pending = False
    for i in (3, 2, 1, 0):
        d = (g // 10**i) % 10
        if d == 0:
            zero_pending = bool(out)
            continue
        if zero_pending:
            out += "零"
            zero_pending = False
        out += _DIGITS[d] + _UNITS[i]
    return out


def read_integer(n: int) -> str:
    """Cardinal reading: 0 → 零, 10 → 十, 115 → 一百一十五, 10005 → 一万零五."""
    if n == 0:
        return "零"
    neg = n < 0
    n = abs(n)
    groups: list[int] = []
    while n:
        groups.append(n % 10000)
        n //= 10000
    out = ""
    for idx in range(len(groups) - 1, -1, -1):
        g = groups[idx]
        if g == 0:
            continue
        part = _group(g)
        if out and g < 1000:
            part = "零" + part
        out += part + _BIG[idx]
    if out.startswith("一十"):
        out = out[1:]
    return ("负" if neg else "") + out


def read_digits(s: str) -> str:
    """Per-digit reading for phone numbers, codes, years: '2024' → 二零二四."""
    return "".join(_DIGITS_SPOKEN[int(c)] if c.isdigit() else c for c in s)


def read_decimal(s: str) -> str:
    """'3.5' → 三点五, '0.25' → 零点二五, '-1.05' → 负一点零五."""
    neg = s.startswith("-")
    s = s.lstrip("+-")
    whole, _, frac = s.partition(".")
    out = read_integer(int(whole or "0"))
    if frac:
        out += "点" + "".join(_DIGITS[int(c)] for c in frac)
    return ("负" if neg else "") + out


def read_time(hh: str, mm: str) -> str:
    """'14:30' → 十四点三十, '14:05' → 十四点零五, '14:00' → 十四点."""
    out = read_integer(int(hh)) + "点"
    m = int(mm)
    if m == 0:
        return out
    if m < 10:
        return out + "零" + _DIGITS[m]
    return out + read_integer(m)


def read_number(value: int | float | str) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return read_integer(value)
    if isinstance(value, float):
        text = repr(value)
        if "e" in text or "E" in text:
            text = f"{value:.10f}".rstrip("0").rstrip(".")
        return read_decimal(text)
    return _spoken_text(str(value))


def _spoken_run(m: re.Match[str], text: str) -> str:
    run = m.group(0)
    if "." in run:
        return read_decimal(run)
    follows_year = text[m.end() : m.end() + 1] == "年"
    if len(run) >= 5 or follows_year or (len(run) > 1 and run[0] == "0"):
        return read_digits(run)
    return read_integer(int(run))


def _spoken_text(text: str) -> str:
    text = _TIME.sub(lambda m: read_time(m.group(1), m.group(2)), text)
    return _NUMBER.sub(lambda m: _spoken_run(m, text), text)


def spoken(value: Any) -> str:
    """Spoken form of any field value."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return read_number(value)
    if isinstance(value, (list, tuple)):
        return "、".join(spoken(v) for v in value)
    return _spoken_text(str(value))


def render(template: str, data: Mapping[str, Any]) -> str:
    """Fill `{field}` placeholders with spoken values; missing fields render empty.

    Literal text in the template is left untouched (the developer wrote it
    to be read aloud); only substituted values are converted.
    """
    return _FIELD.sub(lambda m: spoken(data.get(m.group(1))), template)


__all__ = ["read_decimal", "read_digits", "read_integer", "read_number", "read_time", "render", "spoken"]
