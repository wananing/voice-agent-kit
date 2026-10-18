"""State block: fixed-structure, versioned short text (spec 05 §2.2).

Fields and their order come from a schema; each field declares its layer
(`immediate` / `lazy`). Updates are queued and applied only between turns
(checkpoints: turn closed, before next turn, before a text turn). Channel
choice for immediate fields is a capability table lookup (spec 05 §6.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from vak.core.defaults import ContextParams
from vak.core.types import StateBlock, StateField, StateLayer
from vak.upstream.capabilities import Capabilities

Channel = Literal["per_response_override", "append", "config_update", "rebuild", "lazy"]

EMPTY_VALUE = "无"  # spec 05 §2.2: empty values are written as 无, never omitted


@dataclass(slots=True)
class StateSchema:
    schema_version: str
    fields: list[StateField]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StateSchema:
        fields = [
            StateField(name=str(f["name"]), label=str(f.get("label", f["name"])), layer=StateLayer(f["layer"]))
            for f in data.get("fields") or []
        ]
        names = [f.name for f in fields]
        if len(set(names)) != len(names):
            raise ValueError("state schema: duplicate field names")
        return cls(schema_version=str(data.get("schema_version", "1")), fields=fields)

    @classmethod
    def load(cls, path: str | Path) -> StateSchema:
        return cls.from_dict(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def layer_of(self, name: str) -> StateLayer:
        for f in self.fields:
            if f.name == name:
                return f.layer
        raise KeyError(f"state field not in schema: {name}")


class StateBlockStore:
    """Current values + `state_rev` + a queue of updates waiting for a checkpoint."""

    def __init__(self, schema: StateSchema, params: ContextParams) -> None:
        self._schema = schema
        self._params = params
        self._values: dict[str, str | None] = {f.name: None for f in schema.fields}
        self.state_rev = 0
        self._pending: dict[str, str] = {}

    @property
    def schema(self) -> StateSchema:
        return self._schema

    @property
    def version(self) -> str:
        return f"{self._schema.schema_version}.{self.state_rev}"

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def update(self, name: str, value: str | None) -> StateLayer:
        """Queue a value change; returns the field's layer so the caller
        knows whether an immediate injection is due (spec 05 §2.2)."""
        layer = self._schema.layer_of(name)
        self._pending[name] = "" if value is None else str(value)
        return layer

    def pending_layers(self) -> set[StateLayer]:
        return {self._schema.layer_of(n) for n in self._pending}

    def apply_pending(self) -> set[StateLayer]:
        """Checkpoint: apply queued updates. `state_rev` advances once per
        checkpoint that changed anything. Returns the layers that changed."""
        changed: set[StateLayer] = set()
        for name, value in self._pending.items():
            new = value or None
            if self._values[name] != new:
                self._values[name] = new
                changed.add(self._schema.layer_of(name))
        self._pending.clear()
        if changed:
            self.state_rev += 1
        return changed

    def values(self) -> dict[str, str | None]:
        return dict(self._values)

    def restore(self, values: dict[str, str | None], state_rev: int) -> None:
        for name in self._values:
            self._values[name] = values.get(name)
        self.state_rev = state_rev
        self._pending.clear()

    def block(self) -> StateBlock:
        return StateBlock(
            schema_version=self._schema.schema_version,
            state_rev=self.state_rev,
            fields=[StateField(f.name, f.label, f.layer, self._values[f.name]) for f in self._schema.fields],
        )

    def render(self) -> str:
        """Fixed-order natural short sentences, one per field (spec 05 §2.2:
        no JSON, empty written as 无)."""
        lines = [f"{f.label}：{self._values[f.name] or EMPTY_VALUE}。" for f in self._schema.fields]
        return "\n".join(lines)

    def check_length(self) -> list[str]:
        text = self.render()
        if len(text) > self._params.state_block_max_chars:
            return [f"state block too long: {len(text)} > {self._params.state_block_max_chars} chars"]
        return []


def choose_injection_channel(caps: Capabilities, layer: StateLayer = StateLayer.IMMEDIATE) -> Channel:
    """Spec 05 §6.2: lazy fields never touch the upstream; immediate fields
    take the first available of per-response override → append channel →
    live config update → rebuild. Table-driven by capabilities only."""
    if layer is StateLayer.LAZY:
        return "lazy"
    overrides = caps.get("response.overrides") or {}
    if isinstance(overrides, dict) and overrides.get("instructions") == "replace":
        return "per_response_override"
    for ch in caps.get("config.append_channels") or []:
        name = ch.get("name") if isinstance(ch, dict) else ch
        if name == "instructions":
            return "append"
    if caps.get("config.update.instructions") in ("live_full", "live_sparse"):
        return "config_update"
    return "rebuild"
