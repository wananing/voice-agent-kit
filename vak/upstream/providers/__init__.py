"""Provider registry: type name -> factory + capability file.

Pipelines resolve `agent.yaml` `providers: {stt: {type: funasr, ...}}` through
`build_provider`. Provider modules are imported lazily so an optional
dependency of one provider never breaks the others.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from vak.upstream.capabilities import Capabilities
from vak.upstream.providers.base import ProviderSpec

_CAP_DIR = Path(__file__).resolve().parent.parent / "capabilities"


def _lazy(module: str, cls: str) -> Any:
    def factory(config: dict[str, Any], **kw: Any) -> Any:
        return getattr(importlib.import_module(module), cls)(config, **kw)

    factory.__name__ = cls
    return factory


PROVIDERS: dict[str, ProviderSpec] = {
    "openai_realtime": ProviderSpec(
        kind="s2s",
        type_name="openai_realtime",
        factory=_lazy("vak.upstream.providers.openai_realtime", "OpenAIRealtime"),
        capabilities_file="openai_realtime.yaml",
        extras=[],
    ),
    "openai_realtime_text": ProviderSpec(
        kind="audio_llm",
        type_name="openai_realtime_text",
        factory=_lazy("vak.upstream.providers.openai_realtime", "OpenAIRealtimeAudioLLM"),
        capabilities_file="openai_realtime.yaml",
        extras=[],
    ),
    "dashscope_realtime": ProviderSpec(
        kind="s2s",
        type_name="dashscope_realtime",
        factory=_lazy("vak.upstream.providers.dashscope_realtime", "DashScopeRealtime"),
        capabilities_file="dashscope_realtime.yaml",
        extras=[],
    ),
    "funasr": ProviderSpec(
        kind="stt", type_name="funasr", factory=_lazy("vak.upstream.providers.stt.funasr", "FunASRStreamingSTT")
    ),
    "volcengine_stt": ProviderSpec(
        kind="stt",
        type_name="volcengine_stt",
        factory=_lazy("vak.upstream.providers.stt.volcengine", "VolcengineStreamingSTT"),
    ),
    "aliyun_stt": ProviderSpec(
        kind="stt", type_name="aliyun_stt", factory=_lazy("vak.upstream.providers.stt.aliyun", "AliyunStreamingSTT")
    ),
    "openai_realtime_stt": ProviderSpec(
        kind="stt",
        type_name="openai_realtime_stt",
        factory=_lazy("vak.upstream.providers.stt.openai_realtime_stt", "OpenAIRealtimeSTT"),
    ),
    "deepgram_stt": ProviderSpec(
        kind="stt", type_name="deepgram_stt", factory=_lazy("vak.upstream.providers.stt.deepgram", "DeepgramStreamingSTT")
    ),
    "assemblyai_stt": ProviderSpec(
        kind="stt",
        type_name="assemblyai_stt",
        factory=_lazy("vak.upstream.providers.stt.assemblyai", "AssemblyAIStreamingSTT"),
    ),
    "openai_stt": ProviderSpec(
        kind="stt",
        type_name="openai_stt",
        factory=_lazy("vak.upstream.providers.stt.openai_stt", "OpenAITranscribeSTT"),
    ),
    "openai_compat": ProviderSpec(
        kind="llm",
        type_name="openai_compat",
        factory=_lazy("vak.upstream.providers.llm.openai_compat", "OpenAICompatLLM"),
    ),
    "ark": ProviderSpec(kind="llm", type_name="ark", factory=_lazy("vak.upstream.providers.llm.vendors", "ArkLLM")),
    "bailian": ProviderSpec(
        kind="llm", type_name="bailian", factory=_lazy("vak.upstream.providers.llm.vendors", "BailianLLM")
    ),
    "cosyvoice": ProviderSpec(
        kind="tts", type_name="cosyvoice", factory=_lazy("vak.upstream.providers.tts.cosyvoice", "CosyVoiceTTS")
    ),
    "volcengine_tts": ProviderSpec(
        kind="tts",
        type_name="volcengine_tts",
        factory=_lazy("vak.upstream.providers.tts.volcengine", "VolcengineStreamingTTS"),
    ),
    "aliyun_tts": ProviderSpec(
        kind="tts", type_name="aliyun_tts", factory=_lazy("vak.upstream.providers.tts.aliyun", "AliyunStreamingTTS")
    ),
    "openai_tts": ProviderSpec(
        kind="tts", type_name="openai_tts", factory=_lazy("vak.upstream.providers.tts.openai_tts", "OpenAITTS")
    ),
    "moshi": ProviderSpec(
        kind="full_duplex",
        type_name="moshi",
        factory=_lazy("vak.upstream.providers.moshi", "MoshiClient"),
        capabilities_file="moshi.yaml",
        extras=["opus"],
    ),
}


class UnknownProvider(KeyError):
    pass


def spec_for(type_name: str) -> ProviderSpec:
    try:
        return PROVIDERS[type_name]
    except KeyError as e:
        raise UnknownProvider(f"unknown provider type {type_name!r}; known: {sorted(PROVIDERS)}") from e


def build_provider(type_name: str, config: dict[str, Any], **kwargs: Any) -> Any:
    """Instantiate the provider registered under `type_name` (the `type` key
    of agent.yaml). `config` is the rest of that mapping. Extra keyword
    arguments (clock, is_committed, client, codec) go to the constructor."""
    spec = spec_for(type_name)
    cfg = {k: v for k, v in config.items() if k != "type"}
    return spec.factory(cfg, **kwargs)


def capabilities_for(type_name: str) -> Capabilities | None:
    """Static capability declaration of a provider, or None for cascade
    building blocks (STT/LLM/TTS), whose declaration is the pipeline's."""
    spec = spec_for(type_name)
    if spec.capabilities_file is None:
        return None
    return Capabilities.load(_CAP_DIR / spec.capabilities_file)


__all__ = ["PROVIDERS", "ProviderSpec", "UnknownProvider", "build_provider", "capabilities_for", "spec_for"]
