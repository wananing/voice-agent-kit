from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vak.core.trace import JsonlExporter, MemoryExporter, OTelExporter, span_attributes

from .conftest import load_sample


def test_jsonl_exporter_appends_one_line_per_turn(tmp_path: Path) -> None:
    p = tmp_path / "out" / "trace.jsonl"
    exp = JsonlExporter(p, fsync=True)
    exp.export(load_sample("chat"))
    exp.export(load_sample("tool"))
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["turn_id"] == "c42"


def test_memory_exporter() -> None:
    exp = MemoryExporter()
    exp.export({"a": 1})
    assert exp.records == [{"a": 1}]


def test_span_attributes_gen_ai_and_voice_namespaces() -> None:
    attrs = span_attributes(load_sample("tool"))
    # spec 06 §4.3: usage under gen_ai.usage.*, everything else voice.<field>
    assert attrs["gen_ai.usage.input_tokens"] == 3120 + 932  # gross: cache NOT added back
    assert attrs["gen_ai.usage.output_tokens"] == 18
    assert attrs["gen_ai.usage.audio.input_tokens"] == 932
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == 3840
    assert attrs["gen_ai.usage.audio.cache_read.input_tokens"] == 790
    assert attrs["gen_ai.provider.name"] == "mock" and attrs["gen_ai.request.model"] == "mock-realtime"
    assert attrs["voice.latency.content_first_at_ms"] == 1120
    assert attrs["voice.turn_kind"] == "tool_verbatim"
    assert attrs["voice.anchors.primary"] == "release"
    assert "voice.usage.responses" not in attrs
    assert not any(k.startswith("gen_ai.usage.cost") for k in attrs)
    # null values are dropped, not emitted as 0
    assert "voice.latency.content_first_played_at_ms" not in attrs


def test_span_attributes_net_semantics_restores_gross_input_and_interrupted_tokens() -> None:
    rec = load_sample("interrupted")
    rec["usage"]["usage_semantics"] = "net"
    rec["usage"]["responses"][0]["cached_input_tokens"] = 500
    attrs = span_attributes(rec)
    assert attrs["gen_ai.usage.input_tokens"] == 1500 + 700 + 500
    assert attrs["voice.usage.interrupted_output_tokens"] == 120 + 900


class _Span:
    def __init__(self, name: str, start: Any) -> None:
        self.name, self.start, self.attrs, self.end_time = name, start, {}, None

    def set_attribute(self, k: str, v: Any) -> None:
        self.attrs[k] = v

    def end(self, end_time: Any = None) -> None:
        self.end_time = end_time


class _Tracer:
    def __init__(self) -> None:
        self.spans: list[_Span] = []

    def start_span(self, name: str, start_time: Any = None) -> _Span:
        s = _Span(name, start_time)
        self.spans.append(s)
        return s


def test_otel_exporter_with_injected_tracer_and_graceful_skip() -> None:
    tracer = _Tracer()
    exp = OTelExporter(tracer=tracer)
    exp.export(load_sample("tool"))
    names = [s.name for s in tracer.spans]
    assert names == ["voice.turn", "voice.tool.get_route"]
    turn, tool = tracer.spans
    assert turn.attrs["gen_ai.usage.input_tokens"] == 4052
    assert tool.start == 1790000020000 * 1_000_000 + 805 * 1_000_000
    assert tool.end_time == 1790000020000 * 1_000_000 + 816 * 1_000_000
    assert turn.end_time is not None and turn.end_time > turn.start
    # without the otel package the exporter is a no-op, never an error
    noop = OTelExporter()
    noop.export(load_sample("chat"))
    assert noop.available in (True, False)
