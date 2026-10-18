"""The four pipeline shapes behind one `Upstream` contract (architecture §3).

`build_upstream` is the factory the session/agent layer uses; `load_builtin_caps`
loads a declaration shipped under `vak/upstream/capabilities/`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vak.core.clock import Clock
from vak.core.defaults import UpstreamTimeouts
from vak.core.ids import IdGen
from vak.core.types import Pipeline
from vak.upstream.base import GuardrailHook, Upstream
from vak.upstream.capabilities import Capabilities

CAPS_DIR = Path(__file__).resolve().parent.parent / "capabilities"


def load_builtin_caps(name: str) -> Capabilities:
    """Load `vak/upstream/capabilities/<name>.yaml` (fresh instance each call)."""
    return Capabilities.load(CAPS_DIR / f"{name}.yaml")


def build_upstream(
    pipeline: Pipeline | str,
    providers: dict[str, Any],
    caps: Capabilities | None = None,
    clock: Clock | None = None,
    timeouts: UpstreamTimeouts | None = None,
    ids: IdGen | None = None,
    guardrail: GuardrailHook | None = None,
    **options: Any,
) -> Upstream:
    """Compose providers into one pipeline.

    `providers` keys by pipeline:
    - cascade: `stt_factory`, `llm`, `tts`
    - half_cascade: `audio_llm`, `tts`, optional `side_stt_factory`
    - s2s: `provider` (a `RealtimeS2S`)
    - full_duplex: `model` (a `FullDuplexModel`)
    `options` are passed to the pipeline constructor (e.g. `silence_ms`).
    """
    p = Pipeline(pipeline)
    if p is Pipeline.CASCADE:
        return CascadeUpstream(
            providers["stt_factory"], providers["llm"], providers["tts"],
            caps=caps, clock=clock, timeouts=timeouts, guardrail=guardrail, ids=ids, **options,
        )
    if p is Pipeline.HALF_CASCADE:
        return HalfCascadeUpstream(
            providers["audio_llm"], providers["tts"], providers.get("side_stt_factory"),
            caps=caps, clock=clock, timeouts=timeouts, guardrail=guardrail, ids=ids, **options,
        )
    if p is Pipeline.S2S:
        if caps is None:
            raise ValueError("s2s needs the provider's capability declaration")
        return S2SUpstream(providers["provider"], caps, clock=clock, timeouts=timeouts, ids=ids, **options)
    return FullDuplexUpstream(providers["model"], caps=caps, clock=clock, timeouts=timeouts, ids=ids, **options)


# imported after load_builtin_caps so the pipelines can use it as their default
from vak.upstream.pipelines.base import PipelineBase, ResponseTracker  # noqa: E402
from vak.upstream.pipelines.cascade import CascadeUpstream  # noqa: E402
from vak.upstream.pipelines.full_duplex import FullDuplexUpstream  # noqa: E402
from vak.upstream.pipelines.half_cascade import HalfCascadeUpstream  # noqa: E402
from vak.upstream.pipelines.s2s import S2SUpstream  # noqa: E402

__all__ = [
    "CAPS_DIR",
    "CascadeUpstream",
    "FullDuplexUpstream",
    "HalfCascadeUpstream",
    "PipelineBase",
    "ResponseTracker",
    "S2SUpstream",
    "build_upstream",
    "load_builtin_caps",
]
