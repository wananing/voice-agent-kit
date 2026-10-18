"""Agent: turns an `agent.yaml` into the shared `SessionParts` and runs the
gateway. This is the only place that knows provider type names.
"""

from __future__ import annotations

import datetime as _dt
import importlib
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vak.audio.endpointing import Endpointer, NullSemantic
from vak.audio.vad import EnergyVad
from vak.config import AgentConfig, ProviderConfig
from vak.core.clock import Clock, MonotonicClock
from vak.core.context import StateBlockStore, StateSchema, check_persona, load_persona
from vak.core.defaults import Defaults
from vak.core.gateway import Gateway
from vak.core.session import SessionParts
from vak.core.tools import Registry
from vak.core.trace.exporter import Exporter, JsonlExporter, MemoryExporter
from vak.core.types import Mode, Pipeline, Profile
from vak.cue import CueLibrary
from vak.upstream.base import Upstream
from vak.upstream.capabilities import Capabilities
from vak.upstream.pipelines import build_upstream, load_builtin_caps
from vak.upstream.providers import build_provider, capabilities_for
from vak.upstream.providers.mock import (
    MockAudioLLM,
    MockFullDuplex,
    MockLLM,
    MockRealtimeS2S,
    MockReply,
    MockStreamTTS,
    MockSTT,
    MockTTS,
    mock_caps,
)

log = logging.getLogger(__name__)

BUILTIN_CAPS = {
    Pipeline.CASCADE: "cascade_builtin",
    Pipeline.HALF_CASCADE: "half_cascade_builtin",
    Pipeline.FULL_DUPLEX: "full_duplex_builtin",
}

DEFAULT_MOCK_SCRIPT = [
    MockReply(text="现在的时间我帮你看一下。", tool_call={"id": "call-1", "name": "get_time", "arguments": {}}),
    MockReply(text="好的，还有别的吗？我在听。"),
]


class Agent:
    """Build once, serve many sessions."""

    def __init__(self, config: AgentConfig, *, clock: Clock | None = None, in_memory_traces: bool = False) -> None:
        self.config = config
        self.clock = clock or MonotonicClock()
        self.defaults: Defaults = config.defaults()
        self.in_memory_traces = in_memory_traces
        self.memory_exporter = MemoryExporter() if in_memory_traces else None
        self._mock_script = list(DEFAULT_MOCK_SCRIPT)
        self.parts = self._build_parts()

    @classmethod
    def from_yaml(cls, path: str | Path, **kw: Any) -> Agent:
        return cls(AgentConfig.load(path), **kw)

    # ------------------------------------------------------------------ parts

    def _build_parts(self) -> SessionParts:
        cfg = self.config
        d = self.defaults
        base = cfg.base_dir

        persona = load_persona(cfg.resolve(cfg.persona) or Path("persona.yaml"))
        for problem in check_persona(persona):
            log.warning("persona check: %s", problem)
        schema = StateSchema.load(cfg.resolve(cfg.state_schema) or Path("state_schema.yaml"))
        registry = self._load_registry()
        cues = CueLibrary.with_synth_defaults()
        cues_dir = cfg.resolve(cfg.cues)
        if cues_dir is not None and cues_dir.is_dir():
            loaded = cues.load_dir(cues_dir)
            log.info("loaded %d cue files from %s", len(loaded), cues_dir)
        caps = self._capabilities()
        provider_name, model_name = self._provider_label()

        shared = self._shared_providers()

        def upstream_factory(is_committed: Callable[[], bool]) -> Upstream:
            return self._make_upstream(shared, is_committed, caps)

        def local_tts_factory() -> Any:
            if cfg.pipeline in (Pipeline.CASCADE, Pipeline.HALF_CASCADE):
                return shared["tts"]  # same voice configuration as the pipeline (spec 04 §6.1)
            return shared.get("local_tts")

        def delegate_llm_factory() -> Any:
            return shared.get("llm")

        def endpointer_factory(sample_rate: int) -> Endpointer:
            vad = EnergyVad()
            return Endpointer(d.endpointing, vad, NullSemantic(), sample_rate)

        def exporter_factory(session_id: str) -> Exporter:
            if self.memory_exporter is not None:
                return self.memory_exporter
            day = _dt.date.today().isoformat()
            path = (base / cfg.server.trace_dir / f"{day}.jsonl") if not Path(cfg.server.trace_dir).is_absolute() else Path(cfg.server.trace_dir) / f"{day}.jsonl"
            return JsonlExporter(path)

        return SessionParts(
            profile=Profile(cfg.profile),
            mode=Mode(cfg.mode),
            pipeline=Pipeline(cfg.pipeline),
            defaults=d,
            registry=registry,
            persona=persona,
            rules="",
            state_store_factory=lambda: StateBlockStore(schema, d.context),
            cues=cues,
            caps=caps,
            upstream_factory=upstream_factory,
            local_tts_factory=local_tts_factory,
            delegate_llm_factory=delegate_llm_factory,
            endpointer_factory=endpointer_factory,
            exporter_factory=exporter_factory,
            provider_name=provider_name,
            model_name=model_name,
            snapshot_dir=str(base / cfg.server.snapshot_dir) if cfg.server.snapshot_dir else None,
            extra={"voice": cfg.voice, "language": cfg.language},
        )

    def _load_registry(self) -> Registry:
        cfg = self.config
        if not cfg.tools:
            return Registry([], cfg.profile)
        if str(cfg.base_dir) not in sys.path:
            sys.path.insert(0, str(cfg.base_dir))
        module = importlib.import_module(cfg.tools)
        fns = [v for v in vars(module).values() if hasattr(v, "__vak_tool__")]
        version = getattr(module, "REGISTRY_VERSION", "")
        return Registry(fns, cfg.profile, params=self.defaults.tools, registry_version=version)

    # ------------------------------------------------------------------ providers

    def _pcfg(self, kind: str) -> ProviderConfig | None:
        return getattr(self.config.providers, kind)

    def _capabilities(self) -> Capabilities:
        cfg = self.config
        override = cfg.resolve(cfg.capabilities)
        if override is not None:
            return Capabilities.load(override)
        if cfg.pipeline is Pipeline.S2S:
            s2s = self._pcfg("s2s")
            assert s2s is not None
            if s2s.type == "mock":
                return mock_caps()
            caps = capabilities_for(s2s.type)
            if caps is None:
                raise ValueError(f"no capability declaration for s2s provider {s2s.type!r}")
            return caps
        if cfg.pipeline is Pipeline.FULL_DUPLEX:
            fd = self._pcfg("full_duplex")
            assert fd is not None
            if fd.type != "mock" and (caps := capabilities_for(fd.type)) is not None:
                return caps
        return load_builtin_caps(BUILTIN_CAPS[cfg.pipeline])

    def _provider_label(self) -> tuple[str, str]:
        p = self.config.providers
        if self.config.pipeline is Pipeline.CASCADE:
            parts = [x.type for x in (p.stt, p.llm, p.tts) if x]
            model = p.llm.options().get("model", "") if p.llm else ""
            return "+".join(parts), str(model)
        if self.config.pipeline is Pipeline.HALF_CASCADE:
            model = p.audio_llm.options().get("model", "") if p.audio_llm else ""
            return "+".join(x.type for x in (p.audio_llm, p.tts) if x), str(model)
        main = p.s2s if self.config.pipeline is Pipeline.S2S else p.full_duplex
        return (main.type if main else "none"), str(main.options().get("model", "") if main else "")

    def _shared_providers(self) -> dict[str, Any]:
        """Providers that live for the whole agent (stateless HTTP clients) or
        whose factories are called per session/turn."""
        p = self.config.providers
        out: dict[str, Any] = {}
        if p.llm is not None:
            out["llm"] = self._make("llm", p.llm)
        if p.tts is not None:
            out["tts"] = self._make("tts", p.tts)
        if p.local_tts is not None:
            out["local_tts"] = self._make("tts", p.local_tts)
        if p.audio_llm is not None:
            out["audio_llm"] = self._make("audio_llm", p.audio_llm)
        return out

    def _make(self, kind: str, pc: ProviderConfig, **kw: Any) -> Any:
        if pc.type == "mock":
            return self._mock(kind, pc.options(), **kw)
        return build_provider(pc.type, pc.options(), clock=self.clock, **kw) if kind in ("s2s", "audio_llm") else build_provider(pc.type, pc.options())

    def _mock(self, kind: str, opts: dict[str, Any], **kw: Any) -> Any:
        if kind == "stt":
            return MockSTT(opts.get("transcript", "现在几点了"))
        if kind == "llm":  # chunk_delay_ms: pacing per streamed piece, so also the first-token time
            return MockLLM(list(self._mock_script), self.clock, chunk_delay_ms=int(opts.get("chunk_delay_ms", 0)))
        if kind == "audio_llm":
            return MockAudioLLM(list(self._mock_script), clock=self.clock)
        if kind == "tts":  # text_stream: one stream per reply (like volcengine_tts); chunk_delay_ms per 100 ms chunk
            cls = MockStreamTTS if opts.get("text_stream") else MockTTS
            return cls(clock=self.clock, chunk_delay_ms=int(opts.get("chunk_delay_ms", 0)))
        if kind == "s2s":
            return MockRealtimeS2S(self.clock, script=list(self._mock_script))
        if kind == "full_duplex":
            return MockFullDuplex(clock=self.clock)
        raise ValueError(kind)

    def _make_upstream(self, shared: dict[str, Any], is_committed: Callable[[], bool], caps: Capabilities) -> Upstream:
        cfg = self.config
        p = cfg.providers
        t = self.defaults.upstream
        if cfg.pipeline is Pipeline.CASCADE:
            assert p.stt is not None
            stt_cfg = p.stt
            providers = {"stt_factory": lambda: self._make("stt", stt_cfg), "llm": shared["llm"], "tts": shared["tts"]}
        elif cfg.pipeline is Pipeline.HALF_CASCADE:
            side = p.side_stt
            providers = {
                "audio_llm": shared["audio_llm"],
                "tts": shared["tts"],
                "side_stt_factory": (lambda: self._make("stt", side)) if side is not None else None,
            }
        elif cfg.pipeline is Pipeline.S2S:
            assert p.s2s is not None
            providers = {"provider": self._make("s2s", p.s2s, is_committed=is_committed)}
        else:
            assert p.full_duplex is not None
            providers = {"model": self._make("full_duplex", p.full_duplex)}
        return build_upstream(cfg.pipeline, providers, caps=caps, clock=self.clock, timeouts=t)

    # ------------------------------------------------------------------ serve

    def gateway(self, **kw: Any) -> Gateway:
        s = self.config.server
        return Gateway(
            self.parts,
            host=kw.pop("host", s.host),
            port=kw.pop("port", s.port),
            clock=self.clock,
            serve_web_client=kw.pop("serve_web_client", s.serve_web_client),
            **kw,
        )

    async def serve(self) -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        gw = self.gateway()
        log.info("agent %s: pipeline=%s profile=%s mode=%s", self.config.name, self.config.pipeline.value, self.config.profile.value, self.config.mode.value)
        await gw.serve()
