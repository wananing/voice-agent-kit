"""voice-agent-kit public API.

    from vak import tool, Agent, Profile, Mode, Pipeline
"""

from vak.core.tools.decorator import Entity, tool
from vak.core.types import Mode, Persona, Pipeline, Profile

__all__ = ["Agent", "Entity", "Mode", "Persona", "Pipeline", "Profile", "tool"]


def __getattr__(name: str):  # lazy: `Agent` pulls in every module
    if name == "Agent":
        from vak.agent import Agent

        return Agent
    raise AttributeError(name)
