"""Capability yamls load and validate; registry builds providers lazily."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from vak.upstream.capabilities import FIELD_SPECS, Capabilities, Evidence
from vak.upstream.providers import PROVIDERS, UnknownProvider, build_provider, capabilities_for, spec_for

CAP_DIR = Path(__file__).resolve().parents[3] / "vak" / "upstream" / "capabilities"
MINE = ["openai_realtime.yaml", "dashscope_realtime.yaml", "moshi.yaml"]


@pytest.mark.parametrize("name", MINE)
def test_yaml_loads_validates_and_covers_every_field(name):
    caps = Capabilities.load(CAP_DIR / name)
    assert caps.validate() == []
    missing = sorted(set(FIELD_SPECS) - set(caps.fields))
    assert missing == [], f"{name} missing {missing}"
    extra = sorted(set(caps.fields) - set(FIELD_SPECS))
    assert extra == []
    assert caps.probed_at is None  # static fill only; vak probe sets it
    for fname, f in caps.fields.items():
        assert isinstance(f.evidence, Evidence)
        if f.evidence is not Evidence.UNKNOWN:
            assert f.refs, f"{name}:{fname} has evidence {f.evidence.value} but no refs"
        if f.value == "unknown":
            assert f.evidence is Evidence.UNKNOWN, f"{name}:{fname} unknown value must have unknown evidence"


def test_openai_realtime_declared_facts_from_brief():
    caps = Capabilities.load(CAP_DIR / "openai_realtime.yaml")
    assert caps.get("tools.auto_reply_after_result") == "no"
    assert caps.get("tools.silent_close") == "by_omission"
    assert caps.get("turn.server_detection_disable") == "yes"
    assert caps.get("truncate.mode") == "audio_ms"
    assert {"init_only", "per_item"} <= caps.get("history.injection")
    assert caps.raw("history.mid_session_effective").value == "unknown"
    assert caps.get("history.mid_session_effective") == "no"  # conservative substitute
    assert caps.get("usage.cache_reported") == "cached_tokens_by_modality"
    assert caps.get("response.single_slot") == "yes"  # unknown -> conservative yes
    assert caps.raw("response.single_slot").value == "unknown"
    assert caps.get("output.audio_format") == {"encoding": "pcm16", "sample_rate": 24000}


def test_dashscope_declared_facts():
    caps = Capabilities.load(CAP_DIR / "dashscope_realtime.yaml")
    assert caps.raw("turn.server_detection_disable").value == "unknown"
    assert caps.get("turn.server_detection_disable") == "no"
    assert caps.get("response.single_slot") == "yes" and caps.raw("response.single_slot").evidence is Evidence.CODE_READ
    assert caps.get("tools.auto_reply_after_result") == "no"
    assert caps.get("errors.code_stability") == "message_match"
    assert caps.get("uplink.audio_format")["sample_rate"] == 16000
    assert caps.get("history.item_id_echo") == "server_id"


def test_moshi_declared_as_full_duplex_degradation():
    caps = Capabilities.load(CAP_DIR / "moshi.yaml")
    assert caps.get("tools.calling") == "none" and caps.get("truncate.mode") == "none"
    assert caps.get("response.client_initiated") == "no" and caps.get("turn.server_detection_disable") == "no"
    assert caps.get("history.injection") == set() and caps.get("turn.commit_mode") == "none"
    assert caps.get("session.max_secs") == 360 and caps.get("output.text_lead_ms") == 80


def test_registry_lists_the_eight_types_plus_text_wrapper():
    assert {"openai_realtime", "dashscope_realtime", "funasr", "openai_stt", "openai_compat", "cosyvoice", "openai_tts", "moshi"} <= set(PROVIDERS)
    kinds = {k: v.kind for k, v in PROVIDERS.items()}
    assert kinds["funasr"] == "stt" and kinds["openai_compat"] == "llm" and kinds["cosyvoice"] == "tts"
    assert kinds["openai_realtime"] == "s2s" and kinds["moshi"] == "full_duplex" and kinds["openai_realtime_text"] == "audio_llm"
    for name, spec in PROVIDERS.items():
        assert spec.type_name == name
        if spec.capabilities_file:
            assert (CAP_DIR / spec.capabilities_file).exists()


STREAMING_STT = {
    "volcengine_stt": {"app_key": "a", "access_key": "k"},
    "aliyun_stt": {"api_key": "k"},
    "openai_realtime_stt": {"api_key": "k"},
    "deepgram_stt": {"api_key": "k"},
    "assemblyai_stt": {"api_key": "k"},
}


@pytest.mark.parametrize("name", sorted(STREAMING_STT))
def test_streaming_stt_registered_and_constructible(name):
    from vak.upstream.providers.base import STT

    spec = PROVIDERS[name]
    assert spec.kind == "stt"
    stt = spec.factory(STREAMING_STT[name])
    assert isinstance(stt, STT) and stt.sample_rate == 16000


@pytest.mark.parametrize(
    "name,config",
    [("volcengine_tts", {"app_key": "a", "access_key": "k"}), ("aliyun_tts", {"api_key": "k"})],
)
def test_streaming_tts_registered_and_constructible(name, config):
    from vak.upstream.providers.base import TTS

    spec = PROVIDERS[name]
    assert spec.kind == "tts"
    tts = spec.factory(config)
    assert isinstance(tts, TTS) and tts.sample_rate == 24000 and callable(tts.warmup)


def test_capabilities_for():
    assert capabilities_for("openai_realtime").adapter == "openai_realtime"
    assert capabilities_for("moshi").adapter == "moshi"
    assert capabilities_for("funasr") is None
    with pytest.raises(UnknownProvider):
        capabilities_for("nope")
    assert spec_for("cosyvoice").kind == "tts"


def test_build_provider_strips_type_and_passes_kwargs():
    tts = build_provider("cosyvoice", {"type": "cosyvoice", "url": "http://x", "voice": "v"}, client=httpx.AsyncClient())
    assert type(tts).__name__ == "CosyVoiceTTS" and tts.voice == "v" and "type" not in tts.config
    llm = build_provider("openai_compat", {"model": "m", "base_url": "http://x"}, client=httpx.AsyncClient())
    assert llm.model == "m"
    stt = build_provider("funasr", {"url": "ws://x"})
    assert stt.sample_rate == 16000
    with pytest.raises(UnknownProvider, match="known"):
        build_provider("whisper_cpp", {})
