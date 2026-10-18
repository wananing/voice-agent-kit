"""agent.yaml schema (architecture §6).

This is the whole developer-facing configuration surface. Anything not
here is a Defaults value (vak/core/defaults.py) overridable under the
`tuning:` section by dotted path, e.g. `tuning: {turn.T_filler_A: 1800}`.
"""

from __future__ import annotations

import os
import re
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from vak.core.defaults import DEFAULTS, Defaults
from vak.core.types import Mode, Pipeline, Profile

_ENV = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise KeyError(f"environment variable {name} is not set (referenced in agent.yaml)")

        return _ENV.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class ProviderConfig(BaseModel):
    type: str
    model_config = {"extra": "allow"}

    def options(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if k != "type"}


class ProvidersConfig(BaseModel):
    stt: ProviderConfig | None = None
    llm: ProviderConfig | None = None
    tts: ProviderConfig | None = None
    audio_llm: ProviderConfig | None = None
    s2s: ProviderConfig | None = None
    full_duplex: ProviderConfig | None = None
    # local TTS used for verbatim speech / cues when the pipeline's own voice is upstream (s2s, full_duplex)
    local_tts: ProviderConfig | None = None
    # optional side STT for half_cascade user transcripts
    side_stt: ProviderConfig | None = None


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8765
    serve_web_client: bool = True
    trace_dir: str = "traces"
    snapshot_dir: str | None = None


class AgentConfig(BaseModel):
    name: str = "agent"
    profile: Profile = Profile.A
    mode: Mode = Mode.PTT
    pipeline: Pipeline = Pipeline.CASCADE
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    persona: str = "persona.yaml"
    state_schema: str = "state_schema.yaml"
    tools: str | None = None  # dotted module path, e.g. "my_agent.tools"
    cues: str | None = None  # directory of wav files
    capabilities: str | None = None  # override capability yaml for the chosen upstream
    language: str = "zh"
    voice: str = "default"
    server: ServerConfig = Field(default_factory=ServerConfig)
    tuning: dict[str, Any] = Field(default_factory=dict)
    base_dir: Path = Field(default=Path("."), exclude=True)

    @model_validator(mode="after")
    def _check_pipeline_providers(self) -> AgentConfig:
        p = self.providers
        need: dict[Pipeline, list[str]] = {
            Pipeline.CASCADE: ["stt", "llm", "tts"],
            Pipeline.HALF_CASCADE: ["audio_llm", "tts"],
            Pipeline.S2S: ["s2s"],
            Pipeline.FULL_DUPLEX: ["full_duplex"],
        }
        missing = [k for k in need[self.pipeline] if getattr(p, k) is None]
        if missing:
            raise ValueError(f"pipeline {self.pipeline.value} needs providers: {', '.join(missing)}")
        return self

    def resolve(self, rel: str | None) -> Path | None:
        if rel is None:
            return None
        path = Path(rel)
        return path if path.is_absolute() else (self.base_dir / path)

    def defaults(self) -> Defaults:
        """DEFAULTS with `tuning:` overrides applied by dotted path."""
        import copy

        d = copy.deepcopy(DEFAULTS)
        for dotted, value in self.tuning.items():
            obj: Any = d
            parts = dotted.split(".")
            for part in parts[:-1]:
                obj = getattr(obj, part)
            leaf = parts[-1]
            if not (is_dataclass(obj) and leaf in {f.name for f in fields(obj)}):
                raise ValueError(f"unknown tuning key: {dotted}")
            setattr(obj, leaf, value)
        return d

    @classmethod
    def load(cls, path: str | Path) -> AgentConfig:
        path = Path(path)
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        raw = _expand_env(raw)
        cfg = cls.model_validate(raw)
        cfg.base_dir = path.parent.resolve()
        return cfg


ProviderKind = Literal["stt", "llm", "tts", "audio_llm", "s2s", "full_duplex", "local_tts", "side_stt"]
