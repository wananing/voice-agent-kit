"""Trace exporters (spec 06 §4.3).

`JsonlExporter` writes one record per line; `MemoryExporter` keeps them for
tests; `OTelExporter` maps a record to one `voice.turn` span with
`voice.<field>` attributes and `gen_ai.usage.*` for usage, if the
OpenTelemetry API is importable.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)


class Exporter(Protocol):
    def export(self, record: dict[str, Any]) -> None: ...


class MemoryExporter:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def export(self, record: dict[str, Any]) -> None:
        self.records.append(record)


class JsonlExporter:
    """Append one JSON object per line. `fsync=True` flushes to disk per record."""

    def __init__(self, path: str | Path, fsync: bool = False) -> None:
        self.path = Path(path)
        self.fsync = fsync
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            if self.fsync:
                f.flush()
                os.fsync(f.fileno())


# --- OTel mapping -----------------------------------------------------------

_SKIP_IN_ATTRS = {"usage", "invariant_violations", "missing_fields"}


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}.{k}", v, out)
    elif isinstance(value, list):
        if all(isinstance(x, (str, int, float, bool)) for x in value):
            out[prefix] = list(value)
        else:
            out[prefix] = json.dumps(value, ensure_ascii=False)
    elif value is None:
        return
    else:
        out[prefix] = value


def _sum(responses: list[dict[str, Any]], key: str) -> int | None:
    vals = [r.get(key) for r in responses if isinstance(r.get(key), int)]
    return sum(vals) if vals else None


def span_attributes(record: dict[str, Any]) -> dict[str, Any]:
    """Pure mapping used by `OTelExporter` (spec 06 §4.3 table)."""
    attrs: dict[str, Any] = {}
    for k, v in record.items():
        if k in _SKIP_IN_ATTRS:
            continue
        _flatten(f"voice.{k}", v, attrs)
    if record.get("provider") is not None:
        attrs["gen_ai.provider.name"] = record["provider"]
    if record.get("model") is not None:
        attrs["gen_ai.request.model"] = record["model"]

    usage = record.get("usage") or {}
    responses = [r for r in (usage.get("responses") or []) if isinstance(r, dict)]
    in_text, in_audio = _sum(responses, "input_text_tokens"), _sum(responses, "input_audio_tokens")
    out_text, out_audio = _sum(responses, "output_text_tokens"), _sum(responses, "output_audio_tokens")
    cached, cached_text, cached_audio = (
        _sum(responses, "cached_input_tokens"),
        _sum(responses, "cached_text_tokens"),
        _sum(responses, "cached_audio_tokens"),
    )
    if in_text is not None or in_audio is not None:
        total_in = (in_text or 0) + (in_audio or 0)
        # `net` excludes cached tokens from input; restore the gross total. Never add cache back to `gross`.
        if usage.get("usage_semantics") == "net" and cached:
            total_in += cached
        attrs["gen_ai.usage.input_tokens"] = total_in
    if out_text is not None or out_audio is not None:
        attrs["gen_ai.usage.output_tokens"] = (out_text or 0) + (out_audio or 0)
    for key, val in (
        ("gen_ai.usage.audio.input_tokens", in_audio),
        ("gen_ai.usage.audio.output_tokens", out_audio),
        ("gen_ai.usage.audio.cache_read.input_tokens", cached_audio),
        ("gen_ai.usage.cache_read.input_tokens", cached),
        ("gen_ai.usage.text.input_tokens", in_text),
        ("gen_ai.usage.text.output_tokens", out_text),
        ("gen_ai.usage.text.cache_read.input_tokens", cached_text),
    ):
        if val is not None:
            attrs[key] = val
    interrupted_out = [
        (r.get("output_text_tokens") or 0) + (r.get("output_audio_tokens") or 0)
        for r in responses
        if r.get("interrupted")
    ]
    if interrupted_out:
        attrs["voice.usage.interrupted_output_tokens"] = sum(interrupted_out)
    for key in ("usage_semantics", "generated_audio_ms", "played_audio_ms", "input_audio_ms", "local_tts_chars"):
        if usage.get(key) is not None:
            attrs[f"voice.usage.{key}"] = usage[key]
    return attrs


def _turn_end_offset_ms(record: dict[str, Any]) -> int:
    """Latest known T0-relative point in the record, for the span end."""
    best = 0
    for o in record.get("outputs") or []:
        if isinstance(o, dict) and isinstance(o.get("sent_at_ms"), int):
            best = max(best, o["sent_at_ms"] + (o.get("played_ms") or 0))
    for c in record.get("tool_calls") or []:
        if isinstance(c, dict) and isinstance(c.get("settled_at_ms"), int):
            best = max(best, c["settled_at_ms"])
    lat = record.get("latency") or {}
    for k in ("content_first_at_ms", "sound_first_at_ms", "playback_ended_at_ms"):
        if isinstance(lat.get(k), int):
            best = max(best, lat[k])
    return best


class OTelExporter:
    """Stub: one `voice.turn` span per record plus `voice.tool.<name>` children.
    Skips gracefully (no-op with one warning) when `opentelemetry` is absent."""

    def __init__(self, tracer: Any | None = None) -> None:
        self._tracer = tracer
        self.available = tracer is not None
        if tracer is None:
            try:
                from opentelemetry import trace as ot  # noqa: PLC0415

                self._tracer = ot.get_tracer("vak.trace")
                self.available = True
            except ImportError:
                log.warning("opentelemetry not importable; OTelExporter is a no-op")

    def export(self, record: dict[str, Any]) -> None:
        if not self.available or self._tracer is None:
            return
        t0_ns = int(record.get("t0_epoch_ms") or 0) * 1_000_000
        span = self._tracer.start_span("voice.turn", start_time=t0_ns or None)
        try:
            for k, v in span_attributes(record).items():
                span.set_attribute(k, v)
            for call in record.get("tool_calls") or []:
                if not isinstance(call, dict) or not isinstance(call.get("started_at_ms"), int):
                    continue
                child = self._tracer.start_span(
                    f"voice.tool.{call.get('name')}", start_time=t0_ns + call["started_at_ms"] * 1_000_000
                )
                for k in ("call_id", "tool_route", "closed_by", "presentation", "tool_exec_ms"):
                    if call.get(k) is not None:
                        child.set_attribute(f"voice.tool.{k}", call[k])
                end = call.get("settled_at_ms") if isinstance(call.get("settled_at_ms"), int) else call["started_at_ms"]
                child.end(end_time=t0_ns + end * 1_000_000)
        finally:
            span.end(end_time=t0_ns + _turn_end_offset_ms(record) * 1_000_000 if t0_ns else None)
