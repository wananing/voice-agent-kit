"""Tool registry: load, validate, route (spec 03 §2.1, §3.2, §8.2).

The registry is handed to the upstream whole, once (`tool_table()`); phase
and state restrictions are enforced by the executor, never by changing the
upstream tool table (spec 03 §2.1).
"""

from __future__ import annotations

import dataclasses
import importlib
import re
from collections.abc import Iterable

from vak.core.defaults import ToolParams
from vak.core.tools.decorator import TOOL_ATTR, WANTS_PROGRESS_ATTR, ToolFn, progress_spec
from vak.core.types import (
    CueLength,
    InterruptPolicy,
    LatencyClass,
    Presentation,
    Profile,
    ToolRoute,
    ToolSpec,
)
from vak.upstream.base import ToolDef


class RegistryError(ValueError):
    """A tool was rejected at registration time (spec 03 §2.1 validation rules)."""


_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")

# spec 03 §2.1: profile_overrides may only touch these fields.
ALLOWED_OVERRIDES = frozenset({"presentation", "voice_template", "phases", "timeout_ms", "progress"})

_P, _L = Presentation, LatencyClass

# spec 03 §3.2 decision table, keyed by (presentation, latency_class, write).
# Row 11 (silent + write) is deliberately absent: a miss is a RegistryError.
ROUTE_TABLE: dict[tuple[Presentation, LatencyClass, bool], ToolRoute] = {
    (_P.VERBATIM, _L.MS, False): ToolRoute.A1,  # row 1
    (_P.VERBATIM, _L.MS, True): ToolRoute.A1,  # row 2: A1 + gates
    (_P.VERBATIM, _L.SECONDS, False): ToolRoute.A3,  # row 3
    (_P.VERBATIM, _L.SECONDS, True): ToolRoute.A3,  # row 4: A3 + gates
    (_P.VERBATIM, _L.BACKGROUND, False): ToolRoute.A3,  # row 5
    (_P.VERBATIM, _L.BACKGROUND, True): ToolRoute.A3,  # row 5
    (_P.MODEL, _L.BACKGROUND, False): ToolRoute.A3,  # row 5
    (_P.MODEL, _L.BACKGROUND, True): ToolRoute.A3,  # row 5
    (_P.SCREEN, _L.BACKGROUND, False): ToolRoute.A3,  # row 5
    (_P.SCREEN, _L.BACKGROUND, True): ToolRoute.A3,  # row 5
    (_P.MODEL, _L.MS, False): ToolRoute.A2,  # row 6
    (_P.MODEL, _L.SECONDS, False): ToolRoute.A2,  # row 6
    (_P.MODEL, _L.MS, True): ToolRoute.A1,  # row 7: write part runs as row 2
    (_P.MODEL, _L.SECONDS, True): ToolRoute.A3,  # row 7: write part runs as row 4
    (_P.SCREEN, _L.MS, False): ToolRoute.A1,  # row 8: A1-screen
    (_P.SCREEN, _L.MS, True): ToolRoute.A1,  # row 8 with gates (as row 2)
    (_P.SCREEN, _L.SECONDS, False): ToolRoute.A3,  # row 9: A3-screen
    (_P.SCREEN, _L.SECONDS, True): ToolRoute.A3,  # row 9
    (_P.SILENT, _L.MS, False): ToolRoute.SILENT,  # row 10
    (_P.SILENT, _L.SECONDS, False): ToolRoute.SILENT,  # row 10
    (_P.SILENT, _L.BACKGROUND, False): ToolRoute.SILENT,  # row 10
}

# spec 03 §2.1 `cue` default by route.
DEFAULT_CUE: dict[ToolRoute, CueLength] = {
    ToolRoute.A1: CueLength.SHORT,
    ToolRoute.A2: CueLength.LONG,
    ToolRoute.A3: CueLength.LONG,
    ToolRoute.A0: CueLength.SHORT,
    ToolRoute.SILENT: CueLength.NONE,
}


def static_route(presentation: Presentation, latency_class: LatencyClass, write: bool) -> ToolRoute:
    """Spec 03 §3.2. Raises RegistryError for the forbidden row 11."""
    try:
        return ROUTE_TABLE[(presentation, latency_class, bool(write))]
    except KeyError:
        raise RegistryError(
            f"no route for presentation={presentation.value} latency_class={latency_class.value} write={write}"
            " (spec 03 §3.2 row 11: silent write operations are forbidden)"
        ) from None


def _copy(spec: ToolSpec) -> ToolSpec:
    return dataclasses.replace(
        spec,
        params=dict(spec.params),
        result_schema=dict(spec.result_schema),
        phases=spec.phases if spec.phases == "global" else list(spec.phases),
        idempotency_key=list(spec.idempotency_key),
        gates=list(spec.gates),
        visible_after=list(spec.visible_after),
        profile_overrides={k: dict(v) for k, v in spec.profile_overrides.items()},
    )


def apply_profile(spec: ToolSpec, profile: Profile) -> ToolSpec:
    """Return a copy of `spec` with `profile_overrides[profile]` applied (spec 03 §2.1)."""
    out = _copy(spec)
    for key, value in spec.profile_overrides.get(profile.value, {}).items():
        if key not in ALLOWED_OVERRIDES:
            raise RegistryError(f"{spec.name}: profile_overrides may not override {key!r}")
        if key == "presentation":
            out.presentation = Presentation(value)
        elif key == "progress":
            out.progress = progress_spec(value)
        elif key == "phases":
            out.phases = "global" if value == "global" else list(value)
        elif key == "timeout_ms":
            out.timeout_ms = int(value)
        else:
            out.voice_template = value
    return out


def validate(spec: ToolSpec, profile: Profile) -> None:
    """Spec 03 §2.1 registration-time rejection rules."""
    n = spec.name
    if not _SNAKE.match(n):
        raise RegistryError(f"{n!r}: tool names must be snake_case")
    if not spec.description:
        raise RegistryError(f"{n}: description is required")
    if spec.write and not spec.gates:
        raise RegistryError(f"{n}: write=true requires gates")
    if spec.write and not spec.idempotency_key:
        raise RegistryError(f"{n}: write=true requires idempotency_key")
    if spec.write and spec.interrupt_policy is InterruptPolicy.CANCEL:
        raise RegistryError(f"{n}: write=true cannot use interrupt_policy=cancel")
    if spec.presentation in (Presentation.VERBATIM, Presentation.SCREEN) and not spec.voice_template:
        raise RegistryError(f"{n}: presentation={spec.presentation.value} requires voice_template")
    if spec.latency_class is not LatencyClass.MS and spec.progress is None:
        raise RegistryError(f"{n}: latency_class={spec.latency_class.value} requires progress")
    if spec.presentation is Presentation.SCREEN and profile is Profile.A:
        raise RegistryError(f"{n}: presentation=screen is not accepted in profile A (give a verbatim profile_overrides.A)")
    if spec.timeout_ms < 0:
        raise RegistryError(f"{n}: timeout_ms must be >= 0")
    # row 11 (silent + write) is rejected by the route table miss
    static_route(spec.presentation, spec.latency_class, spec.write)


def _load_module(path: str) -> list[ToolFn]:
    mod = importlib.import_module(path)
    fns = [v for v in vars(mod).values() if callable(v) and hasattr(v, TOOL_ATTR)]
    # deterministic order: definition order is dict order in module globals
    return fns


class Registry:
    """Validated, profile-resolved tool table for one agent (spec 03 §2)."""

    def __init__(
        self,
        tools: Iterable[ToolFn] | str,
        profile: Profile | str,
        *,
        params: ToolParams | None = None,
        registry_version: str = "",
    ) -> None:
        self.profile = Profile(profile)
        self.registry_version = registry_version
        self._params = params or ToolParams()
        self._specs: dict[str, ToolSpec] = {}
        self._fns: dict[str, ToolFn] = {}
        self._wants_progress: set[str] = set()
        fns = _load_module(tools) if isinstance(tools, str) else list(tools)
        for fn in fns:
            self._register(fn)

    def _register(self, fn: ToolFn) -> None:
        base: ToolSpec | None = getattr(fn, TOOL_ATTR, None)
        if base is None:
            raise RegistryError(f"{getattr(fn, '__name__', fn)!r} is not decorated with @tool")
        spec = apply_profile(base, self.profile)
        validate(spec, self.profile)
        if spec.name in self._specs:
            raise RegistryError(f"{spec.name}: duplicate tool name")
        if spec.timeout_ms == 0:
            spec.timeout_ms = self._params.timeout_ms_default[spec.latency_class.value]
        spec.route = static_route(spec.presentation, spec.latency_class, spec.write)
        if spec.cue is None:
            spec.cue = DEFAULT_CUE[spec.route]
        if spec.cancellable_by_user is None:
            spec.cancellable_by_user = spec.latency_class is LatencyClass.BACKGROUND
        self._specs[spec.name] = spec
        self._fns[spec.name] = fn
        if getattr(fn, WANTS_PROGRESS_ATTR, False):
            self._wants_progress.add(spec.name)

    # -- lookup ------------------------------------------------------------

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __iter__(self):
        return iter(self._specs.values())

    def __len__(self) -> int:
        return len(self._specs)

    @property
    def names(self) -> list[str]:
        return list(self._specs)

    def get(self, name: str) -> ToolSpec:
        return self._specs[name]

    def fn(self, name: str) -> ToolFn:
        return self._fns[name]

    def wants_progress(self, name: str) -> bool:
        return name in self._wants_progress

    def readonly_names(self) -> list[str]:
        """Tools a delegate may call (spec 03 §6.1 `allowed_tools`: write=false only)."""
        return [s.name for s in self._specs.values() if not s.write]

    # -- visibility (spec 03 §8.2 ②, §8.3) ----------------------------------

    def is_visible(self, name: str, phase: str, completed_tools: Iterable[str]) -> bool:
        spec = self._specs.get(name)
        if spec is None:
            return False
        if spec.phases != "global" and phase not in spec.phases:
            return False
        done = set(completed_tools)
        return all(pre in done for pre in spec.visible_after)

    def visible_tools(self, phase: str, completed_tools: Iterable[str]) -> list[str]:
        """Visible set = phase tools ∪ global tools ∪ tools whose `visible_after` is satisfied."""
        done = set(completed_tools)
        return [n for n in self._specs if self.is_visible(n, phase, done)]

    # -- upstream ------------------------------------------------------------

    def tool_table(self) -> list[ToolDef]:
        """The whole table, once. Never filtered by phase (spec 03 §2.1)."""
        return [ToolDef(name=s.name, description=s.description, params=dict(s.params)) for s in self._specs.values()]


def route_of(spec: ToolSpec) -> ToolRoute:
    """Convenience for callers holding a validated spec."""
    return spec.route or static_route(spec.presentation, spec.latency_class, spec.write)


__all__ = [
    "ALLOWED_OVERRIDES",
    "DEFAULT_CUE",
    "ROUTE_TABLE",
    "Registry",
    "RegistryError",
    "apply_profile",
    "route_of",
    "static_route",
    "validate",
]

