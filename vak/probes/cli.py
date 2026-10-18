"""`vak probe <provider>`: run P0 probes against a real upstream and write
the results back into its capability declaration (spec 04 §6)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from vak.core.clock import MonotonicClock
from vak.probes.assets import ProbeAssets
from vak.probes.p0 import ProbeContext, probe_and_dump


def run_probes(*, provider: str, items: str, out: str | None, config: str | None, n: int) -> int:
    from vak.config import AgentConfig
    from vak.upstream.pipelines import build_upstream
    from vak.upstream.providers import PROVIDERS, build_provider, capabilities_for, spec_for

    options: dict = {}
    if provider == "config":
        if not config:
            print("vak probe config needs --config agent.yaml", file=sys.stderr)
            return 2
        cfg = AgentConfig.load(config)
        pc = cfg.providers.s2s or cfg.providers.full_duplex
        if pc is None:
            print("agent.yaml has no s2s / full_duplex provider to probe", file=sys.stderr)
            return 2
        provider, options = pc.type, pc.options()
    elif config:
        cfg = AgentConfig.load(config)
        for pc in (cfg.providers.s2s, cfg.providers.full_duplex, cfg.providers.audio_llm):
            if pc is not None and pc.type == provider:
                options = pc.options()
    if provider not in PROVIDERS:
        print(f"unknown provider {provider!r}; known: {sorted(PROVIDERS)}", file=sys.stderr)
        return 2
    spec = spec_for(provider)
    caps = capabilities_for(provider)
    if caps is None:
        print(f"provider {provider!r} has no capability declaration to probe", file=sys.stderr)
        return 2
    clock = MonotonicClock()
    pipeline = "s2s" if spec.kind == "s2s" else "full_duplex"

    def factory(**kw):
        key = "provider" if pipeline == "s2s" else "model"
        prov = build_provider(provider, options, clock=clock, **kw) if pipeline == "s2s" else build_provider(provider, options)
        return build_upstream(pipeline, {key: prov}, caps=caps, clock=clock)

    ctx = ProbeContext(upstream_factory=factory, assets=ProbeAssets(), caps=caps, n=n, clock=clock)
    ids = [s.strip() for s in items.split(",") if s.strip()] or None
    out_path = Path(out) if out else Path(__file__).resolve().parent.parent / "upstream" / "capabilities" / spec.capabilities_file
    report = asyncio.run(probe_and_dump(ids, ctx, out_path))
    print(report.summary())
    print(f"written: {out_path}")
    return 0
