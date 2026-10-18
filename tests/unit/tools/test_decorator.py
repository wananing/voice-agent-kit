"""@tool schema inference (architecture §6; spec 03 §2.1 `params`)."""

from __future__ import annotations

from typing import Annotated, Literal

import pytest

from vak.core.tools import Entity, tool
from vak.core.tools.decorator import TOOL_ATTR, WANTS_PROGRESS_ATTR, infer_params
from vak.core.types import InterruptPolicy, OnDuplicate, ToolSpec


def test_schema_inference_scalars_optional_list_dict_entity():
    @tool(presentation="verbatim", latency_class="ms", write=False, voice_template="x", timeout_ms=1000)
    async def f(a: str, b: int, c: float, d: bool, e: list[int], g: dict, opt: str | None = None,
                ent: Annotated[str, Entity()] = "", mode: Literal["brief", "more"] = "brief") -> dict:
        """Do a thing."""
        return {}

    spec: ToolSpec = getattr(f, TOOL_ATTR)
    p = spec.params["properties"]
    assert p["a"] == {"type": "string"}
    assert p["b"] == {"type": "integer"}
    assert p["c"] == {"type": "number"}
    assert p["d"] == {"type": "boolean"}
    assert p["e"] == {"type": "array", "items": {"type": "integer"}}
    assert p["g"] == {"type": "object"}
    assert p["opt"] == {"type": ["string", "null"]}
    assert p["ent"] == {"type": "string", "entity": True}
    assert p["mode"] == {"type": "string", "enum": ["brief", "more"]}
    assert spec.params["required"] == ["a", "b", "c", "d", "e", "g"]
    assert spec.params["additionalProperties"] is False
    assert spec.description == "Do a thing."
    assert spec.name == "f"


def test_defaults_for_write_and_read():
    @tool(presentation="verbatim", latency_class="ms", write=True, voice_template="x", gates=[{"kind": "intent"}],
          idempotency_key=["k"], timeout_ms=1000)
    async def w(k: str) -> dict:
        """w"""
        return {}

    @tool(presentation="silent", latency_class="ms", write=False, timeout_ms=1000, description="given")
    async def r() -> dict:
        return {}

    ws, rs = getattr(w, TOOL_ATTR), getattr(r, TOOL_ATTR)
    assert ws.on_duplicate is OnDuplicate.REJECT and ws.interrupt_policy is InterruptPolicy.FINISH_THEN_NOTIFY
    assert rs.on_duplicate is OnDuplicate.ALLOW and rs.interrupt_policy is InterruptPolicy.CANCEL
    assert rs.description == "given"
    assert rs.result_schema["required"] == ["ok", "summary"]


def test_progress_kwarg_excluded_from_schema():
    async def job(progress, label: str) -> dict:
        return {}

    schema, wants = infer_params(job)
    assert wants is True and list(schema["properties"]) == ["label"]

    @tool(presentation="verbatim", latency_class="background", write=False, voice_template="x",
          progress={"ack_template": "ok", "throttle": {"first_after_s": 30}}, timeout_ms=1)
    async def job2(progress, label: str) -> dict:
        """j"""
        return {}

    assert getattr(job2, WANTS_PROGRESS_ATTR) is True
    assert getattr(job2, TOOL_ATTR).progress.throttle_first_after_s == 30


def test_rejects_sync_fn_missing_hint_and_missing_description():
    with pytest.raises(TypeError):
        tool(presentation="silent", latency_class="ms", write=False)(lambda: None)  # type: ignore[arg-type]

    async def nohint(a):  # noqa: ANN001
        """x"""

    with pytest.raises(TypeError):
        tool(presentation="silent", latency_class="ms", write=False)(nohint)

    async def nodoc(a: str) -> dict:
        return {}

    with pytest.raises(TypeError):
        tool(presentation="silent", latency_class="ms", write=False)(nodoc)
