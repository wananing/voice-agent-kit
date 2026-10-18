"""Capability declaration (spec 04 §2.1–2.2).

Each field is value + evidence + refs. `unknown` is resolved to the
field's conservative value at read time, so policy code can branch on
`caps.get("tools.silent_close")` without caring whether it was probed.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class Evidence(enum.StrEnum):
    PROBED = "probed"
    DOC = "doc"
    CODE_READ = "code_read"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class CapField:
    value: Any
    evidence: Evidence = Evidence.UNKNOWN
    refs: list[str] = field(default_factory=list)


# Field name -> (allowed values or type hint, conservative value). Values
# are written exactly as in spec 04 §2.2. `None` as conservative value means
# "no conservative substitute; stays unknown".
FIELD_SPECS: dict[str, tuple[Any, Any]] = {
    # turn
    "turn.server_detection_disable": ({"yes", "no", "unknown"}, "no"),
    "turn.segment_only": ({"yes", "partial", "no", "unknown"}, "no"),
    "turn.server_interrupt_optional": ({"yes", "no", "unknown"}, "no"),
    "turn.server_silence_ms": ("int|range|unknown", None),
    "turn.sensitivity_levels": ("list|none", "none"),
    "turn.commit_mode": (
        {"commit_create", "activity_window", "whole_utterance", "server_only", "none"},
        "none",
    ),
    "turn.commit_min_ms": ("int|unknown", None),
    "turn.empty_commit_behavior": ("error(code)|ignored|unknown", None),
    "turn.input_mute": ({"yes", "no", "unknown"}, "no"),
    # history
    "history.injection": ("set", {"init_only"}),
    "history.mid_session_effective": ({"yes", "no", "unknown"}, "no"),
    "history.limits": ("dict", {}),
    "history.constraints": ("list", []),
    "history.text_flips_modality": ({"yes", "no", "unknown"}, "yes"),
    "history.tool_calls": ({"keep", "as_text", "drop", "unknown"}, "drop"),
    "history.item_id_echo": ({"server_id", "client_id", "none"}, "none"),
    "history.item_ack": ({"yes", "no", "unknown"}, "no"),
    # config
    "config.update.instructions": ({"live_full", "live_sparse", "append_only", "next_session"}, "next_session"),
    "config.update.tools": ({"live_full", "live_sparse", "append_only", "next_session"}, "next_session"),
    "config.update.voice": ({"live_full", "live_sparse", "append_only", "next_session"}, "next_session"),
    "config.update_invalidates_cache": ({"yes", "no", "unknown"}, "yes"),
    "config.append_channels": ("list", []),
    "config.update_ack": ({"yes", "no"}, "no"),
    # response
    "response.client_initiated": ({"yes", "no"}, "no"),
    "response.overrides": ("dict", {}),
    "response.single_slot": ({"yes", "no", "unknown"}, "yes"),
    "response.say": ({"native", "via_local_tts", "none"}, "none"),
    # tools
    "tools.calling": ({"native", "prompted", "none"}, "none"),
    "tools.auto_reply_after_result": ({"yes", "no", "unknown"}, "yes"),
    "tools.silent_close": ({"by_omission", "flag", "none", "unknown"}, "none"),
    "tools.placeholder_accepted": ({"yes", "no", "unknown"}, "no"),
    "tools.parallel_calls": ({"yes", "no", "unknown"}, "no"),
    "tools.args_streaming": ({"complete", "incremental"}, "complete"),
    # truncate / session
    "truncate.mode": ({"audio_ms", "text_rewrite", "local_only", "none"}, "none"),
    "session.resumption": ({"none", "handle"}, "none"),
    "session.max_secs": ("int|unknown", None),
    "session.max_turns_retained": ("int|unknown", None),
    "session.expiry_notice": ({"yes", "no"}, "no"),
    "session.idle_timeout_secs": ("int|unknown", None),
    "session.server_compaction": ({"none", "sliding_window"}, "none"),
    "session.init_ready_signal": ({"ack_event", "timeout"}, "timeout"),
    # usage / errors
    "usage.cache_reported": ({"none", "cached_tokens", "cached_tokens_by_modality"}, "none"),
    "usage.semantics": ({"net", "gross"}, "net"),
    "errors.code_map": ("dict", {}),
    "errors.code_stability": ({"stable_codes", "message_match"}, "message_match"),
    "errors.event_correlation": ({"yes", "no"}, "no"),
    # output / uplink
    "output.modalities": ("set", {"audio"}),
    "output.modality_switch": ({"per_response", "per_session_live", "session_fixed"}, "session_fixed"),
    "output.transcript": ({"yes", "no"}, "no"),
    "output.text_lead_ms": ("int|unknown", None),
    "output.audio_format": ("dict", {"encoding": "pcm16", "sample_rate": 16000}),
    "output.voice_prompt": ({"yes", "no", "unknown"}, "no"),
    "uplink.audio_format": ("dict", {"encoding": "pcm16", "sample_rate": 16000}),
    "uplink.faster_than_realtime": ({"tolerated", "degrades", "unknown"}, "degrades"),
    "uplink.gaps": ({"tolerated", "degrades", "unknown"}, "degrades"),
    "uplink.streaming_input": ({"streaming", "whole_utterance"}, "whole_utterance"),
    # cascade-only (others: "n/a")
    "cascade.user_transcript": ({"native", "side_request", "none", "n/a"}, "none"),
    "cascade.pre_speech_hook": ({"sentence", "none", "n/a"}, "none"),
    "cascade.prefix_cache": ({"yes", "no", "unknown", "n/a"}, "no"),
}


class UnknownCapability(KeyError):
    pass


@dataclass(slots=True)
class Capabilities:
    adapter: str
    upstream: dict[str, str]  # vendor, model, api_version
    fields: dict[str, CapField]
    probed_at: str | None = None

    def raw(self, name: str) -> CapField:
        if name not in FIELD_SPECS:
            raise UnknownCapability(name)
        return self.fields.get(name, CapField(value="unknown"))

    def get(self, name: str) -> Any:
        """Value with `unknown` replaced by the conservative value."""
        f = self.raw(name)
        if f.value == "unknown" or f.value is None:
            return FIELD_SPECS[name][1]
        return f.value

    def is_(self, name: str, value: Any) -> bool:
        return self.get(name) == value

    def set_probed(self, name: str, value: Any, refs: list[str] | None = None) -> None:
        if name not in FIELD_SPECS:
            raise UnknownCapability(name)
        self.fields[name] = CapField(value=value, evidence=Evidence.PROBED, refs=refs or [])

    def validate(self) -> list[str]:
        """Return a list of problems (unknown field names, illegal enum values)."""
        problems: list[str] = []
        for name, f in self.fields.items():
            if name not in FIELD_SPECS:
                problems.append(f"unknown field {name}")
                continue
            allowed = FIELD_SPECS[name][0]
            if isinstance(allowed, set) and f.value not in allowed and f.value != "unknown":
                problems.append(f"{name}: {f.value!r} not in {sorted(allowed)}")
        return problems

    @classmethod
    def all_unknown(cls, adapter: str, upstream: dict[str, str] | None = None) -> Capabilities:
        return cls(adapter=adapter, upstream=upstream or {}, fields={k: CapField("unknown") for k in FIELD_SPECS})

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Capabilities:
        fields: dict[str, CapField] = {}
        for name, spec in (d.get("fields") or {}).items():
            if isinstance(spec, dict) and "value" in spec:
                value = spec["value"]
                if isinstance(value, list) and isinstance(FIELD_SPECS.get(name, (None,))[0], str) and FIELD_SPECS[name][0] == "set":
                    value = set(value)
                fields[name] = CapField(
                    value=value,
                    evidence=Evidence(spec.get("evidence", "unknown")),
                    refs=list(spec.get("refs") or []),
                )
            else:
                fields[name] = CapField(value=spec)
        caps = cls(
            adapter=d["adapter"],
            upstream=dict(d.get("upstream") or {}),
            fields=fields,
            probed_at=d.get("probed_at"),
        )
        problems = caps.validate()
        if problems:
            raise ValueError("invalid capabilities: " + "; ".join(problems))
        return caps

    @classmethod
    def load(cls, path: str | Path) -> Capabilities:
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh))

    def to_dict(self) -> dict[str, Any]:
        out_fields: dict[str, Any] = {}
        for name, f in self.fields.items():
            value = sorted(f.value) if isinstance(f.value, set) else f.value
            out_fields[name] = {"value": value, "evidence": f.evidence.value, "refs": list(f.refs)}
        return {
            "adapter": self.adapter,
            "upstream": dict(self.upstream),
            "probed_at": self.probed_at,
            "fields": out_fields,
        }

    def dump(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(self.to_dict(), fh, allow_unicode=True, sort_keys=False)
