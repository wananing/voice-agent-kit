"""Load `schemas/trace.schema.json` and expose its key tree (spec 06 §3.1).

The schema file is the single source of truth for field names; the collector
derives its allowed-key tree from it so unknown keys are rejected at write
time (architecture §7 rule 6).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

TRACE_SCHEMA_VERSION = "06-v0.1"  # spec 06 §3.2 `trace_schema_version`

_HERE = Path(__file__).resolve()
SCHEMA_PATH = next(
    p
    for p in (
        _HERE.parents[3] / "schemas" / "trace.schema.json",  # repo checkout
        _HERE.parents[2] / "schemas" / "trace.schema.json",  # installed wheel (vak/schemas)
    )
    if p.exists()
)


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def validator() -> Draft202012Validator:
    return Draft202012Validator(load_schema())


def validate(record: dict[str, Any]) -> list[str]:
    """Return human-readable schema errors (empty list = valid)."""
    errs = []
    for e in sorted(validator().iter_errors(record), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(p) for p in e.absolute_path) or "<root>"
        errs.append(f"{path}: {e.message}")
    return errs


def missing_required(record: dict[str, Any]) -> list[tuple[tuple[Any, ...], str]]:
    """(path-to-parent, key) for every `required` violation, including those
    under if/then and inside array items."""
    out: list[tuple[tuple[Any, ...], str]] = []
    for e in validator().iter_errors(record):
        if e.validator != "required" or not isinstance(e.instance, dict):
            continue
        parent = tuple(e.absolute_path)
        for key in e.validator_value:
            if key not in e.instance:
                out.append((parent, key))
    return out


def _node_properties(node: dict[str, Any]) -> dict[str, Any] | None:
    if "properties" in node:
        return node["properties"]
    return None


def allowed_keys(path: tuple[str, ...] = ()) -> set[str] | None:
    """Allowed child keys of the object at `path` (top level when empty).
    Returns None when the object at `path` is free-form (e.g. `slots`, `args`)."""
    node: dict[str, Any] = load_schema()
    for key in path:
        props = _node_properties(node)
        if props is None or key not in props:
            return None
        node = props[key]
        if "items" in node:
            node = node["items"]
    props = _node_properties(node)
    if props is None:
        return None
    return set(props)
