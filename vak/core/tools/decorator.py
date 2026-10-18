"""`@tool(...)` decorator (architecture §6; spec 03 §2.1).

Attaches a `ToolSpec` to an async function. The parameters JSON Schema is
inferred from the signature and type hints; `Annotated[str, Entity()]`
marks a parameter `entity: true`. The description defaults to the
docstring. Registry-time validation and routing happen in `registry.py`.
"""

from __future__ import annotations

import inspect
import types
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints

from vak.core.types import (
    CueLength,
    DuplicateScope,
    Gate,
    GateKind,
    InterruptPolicy,
    LatencyClass,
    OnDuplicate,
    Presentation,
    ProgressSpec,
    ToolSpec,
)

TOOL_ATTR = "__vak_tool__"
WANTS_PROGRESS_ATTR = "__vak_wants_progress__"
PROGRESS_PARAM = "progress"  # a tool taking this kwarg receives an async progress reporter (spec 03 §4.1)

# spec 03 §2.3 common result fields; the default `result_schema`.
COMMON_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "reason_code": {"type": "string"},
        "summary": {"type": "string"},
        "data": {"type": "object"},
        "display": {"type": "object"},
        "alternatives": {"type": "array", "maxItems": 2},
        "escalation": {"type": "object"},
    },
    "required": ["ok", "summary"],
}

ToolFn = Callable[..., Coroutine[Any, Any, Any]]


@dataclass(frozen=True, slots=True)
class Entity:
    """Marker for `Annotated[<type>, Entity()]`: the parameter is resolved by
    the tool layer against the user's own words (spec 03 §2.1, §8.2 ⑤)."""


_SCALARS: dict[Any, str] = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _schema_for(tp: Any) -> dict[str, Any]:
    origin = get_origin(tp)
    if origin is Annotated:
        base, *extras = get_args(tp)
        schema = _schema_for(base)
        if any(isinstance(e, Entity) for e in extras):
            schema["entity"] = True
        return schema
    if origin is Union or origin is types.UnionType:
        members = [a for a in get_args(tp) if a is not type(None)]
        if len(members) != 1:
            raise TypeError(f"unsupported union parameter type {tp!r}")
        schema = _schema_for(members[0])
        if len(members) != len(get_args(tp)):
            schema["type"] = [schema["type"], "null"]
        return schema
    if origin is Literal:
        values = list(get_args(tp))
        return {"type": _SCALARS.get(type(values[0]), "string"), "enum": values}
    if origin is list or tp is list:
        args = get_args(tp)
        schema = {"type": "array"}
        if args:
            schema["items"] = _schema_for(args[0])
        return schema
    if origin is dict or tp is dict:
        return {"type": "object"}
    if tp in _SCALARS:
        return {"type": _SCALARS[tp]}
    if tp is Any:
        return {}
    raise TypeError(f"unsupported parameter type {tp!r}")


def infer_params(fn: Callable[..., Any]) -> tuple[dict[str, Any], bool]:
    """Return (JSON Schema for the parameters, wants_progress)."""
    hints = get_type_hints(fn, include_extras=True)
    props: dict[str, Any] = {}
    required: list[str] = []
    wants_progress = False
    for pname, p in inspect.signature(fn).parameters.items():
        if pname == PROGRESS_PARAM:
            wants_progress = True
            continue
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            raise TypeError(f"{fn.__name__}: *args/**kwargs are not supported in tool signatures")
        if pname not in hints:
            raise TypeError(f"{fn.__name__}: parameter {pname!r} needs a type hint")
        props[pname] = _schema_for(hints[pname])
        if p.default is inspect.Parameter.empty:
            required.append(pname)
    schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema, wants_progress


def _gate(g: Gate | dict[str, Any]) -> Gate:
    if isinstance(g, Gate):
        return g
    return Gate(
        kind=GateKind(g["kind"]),
        allowed_actions=list(g.get("allowed_actions") or []),
        must_match=list(g.get("must_match") or []),
        must_not_match=list(g.get("must_not_match") or []),
        check=g.get("check"),
    )


def progress_spec(p: ProgressSpec | dict[str, Any] | None) -> ProgressSpec | None:
    if p is None or isinstance(p, ProgressSpec):
        return p
    throttle = p.get("throttle") or {}
    return ProgressSpec(
        ack_template=p["ack_template"],
        throttle_first_after_s=int(throttle.get("first_after_s", 60)),
        throttle_min_interval_s=int(throttle.get("min_interval_s", 60)),
        replace_older=bool(throttle.get("replace_older", True)),
    )


def tool(
    *,
    presentation: str,
    latency_class: str,
    write: bool,
    name: str | None = None,
    description: str | None = None,
    params: dict[str, Any] | None = None,
    phases: str | list[str] = "global",
    interrupt_policy: str | None = None,
    on_duplicate: str | None = None,
    timeout_ms: int = 0,
    result_schema: dict[str, Any] | None = None,
    voice_template: str | None = None,
    cue: str | None = None,
    idempotency_key: list[str] | None = None,
    gates: list[Gate | dict[str, Any]] | None = None,
    visible_after: list[str] | None = None,
    cancellable_by_user: bool | None = None,
    duplicate_scope: str = "turn",
    progress: ProgressSpec | dict[str, Any] | None = None,
    profile_overrides: dict[str, dict[str, Any]] | None = None,
) -> Callable[[ToolFn], ToolFn]:
    """Attach a `ToolSpec` (spec 03 §2.1) to an async function.

    `timeout_ms=0` means "use the default for the latency class" (filled by
    the registry from `ToolParams`). `on_duplicate` defaults to `reject` for
    writes and `allow` otherwise; `interrupt_policy` defaults to
    `finish_then_notify` for writes and `cancel` otherwise.
    """

    def deco(fn: ToolFn) -> ToolFn:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"{fn.__name__}: tools must be `async def`")
        if params is None:
            schema, wants_progress = infer_params(fn)
        else:
            schema = dict(params)
            wants_progress = PROGRESS_PARAM in inspect.signature(fn).parameters
        desc = description if description is not None else (inspect.getdoc(fn) or "")
        if not desc:
            raise TypeError(f"{fn.__name__}: description is required (argument or docstring)")
        is_write = bool(write)
        spec = ToolSpec(
            name=name or fn.__name__,
            description=desc,
            params=schema,
            presentation=Presentation(presentation),
            latency_class=LatencyClass(latency_class),
            write=is_write,
            phases="global" if phases == "global" else list(phases),
            interrupt_policy=InterruptPolicy(
                interrupt_policy or (InterruptPolicy.FINISH_THEN_NOTIFY if is_write else InterruptPolicy.CANCEL)
            ),
            on_duplicate=OnDuplicate(on_duplicate or (OnDuplicate.REJECT if is_write else OnDuplicate.ALLOW)),
            timeout_ms=int(timeout_ms),
            result_schema=dict(result_schema) if result_schema is not None else dict(COMMON_RESULT_SCHEMA),
            voice_template=voice_template,
            cue=CueLength(cue) if cue is not None else None,
            idempotency_key=list(idempotency_key or []),
            gates=[_gate(g) for g in (gates or [])],
            visible_after=list(visible_after or []),
            cancellable_by_user=cancellable_by_user,
            duplicate_scope=DuplicateScope(duplicate_scope),
            progress=progress_spec(progress),
            profile_overrides={str(k): dict(v) for k, v in (profile_overrides or {}).items()},
        )
        setattr(fn, TOOL_ATTR, spec)
        setattr(fn, WANTS_PROGRESS_ATTR, wants_progress)
        return fn

    return deco
