"""Tool registry, routing, gates, call closure and delegation (spec 03)."""

from vak.core.tools.decorator import Entity, tool
from vak.core.tools.delegate import (
    Delegate,
    DelegateMessage,
    DelegationRequest,
    SpeculationGate,
    TextLLMDelegate,
)
from vak.core.tools.executor import ToolLayer
from vak.core.tools.gates import GateContext, GateVerdict, register_checker, run_gates
from vak.core.tools.registry import ROUTE_TABLE, Registry, RegistryError, static_route
from vak.core.tools.voice_template import render

__all__ = [
    "ROUTE_TABLE",
    "Delegate",
    "DelegateMessage",
    "DelegationRequest",
    "Entity",
    "GateContext",
    "GateVerdict",
    "Registry",
    "RegistryError",
    "SpeculationGate",
    "TextLLMDelegate",
    "ToolLayer",
    "register_checker",
    "render",
    "run_gates",
    "static_route",
    "tool",
]
