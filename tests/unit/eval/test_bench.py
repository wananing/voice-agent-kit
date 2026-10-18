"""vak bench: stage timing semantics on in-process fakes (no network)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from vak.eval.bench import Report, Result, bench_llm, bench_stt, bench_tts, rank_combinations, run_bench
from vak.upstream.providers.base import LLMDelta, Transcript, TTSChunk


class Tick:
    """Deterministic timer: each fake step advances it explicitly."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def add(self, ms: float) -> None:
        self.t += ms / 1000


class FakeSTT:
    sample_rate = 16000
    language = "zh"

    def __init__(self, tick: Tick) -> None:
        self.tick = tick
        self.q: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self.fed = 0
        self.cancelled = False

    async def start(self) -> None:
        self.tick.add(50)  # connect

    async def feed(self, pcm16: bytes) -> None:
        self.fed += 1
        if self.fed == 2:
            self.q.put_nowait(Transcript("今天", final=False))

    async def finish(self) -> None:
        self.tick.add(300)  # end of speech -> final
        self.q.put_nowait(Transcript("今天天气", final=True))

    async def cancel(self) -> None:
        self.cancelled = True

    async def results(self):
        while (t := await self.q.get()) is not None:
            yield t


async def test_stt_final_ms_excludes_connect_and_warmup_is_dropped():
    tick = Tick()
    made: list[FakeSTT] = []

    def factory() -> FakeSTT:
        made.append(FakeSTT(tick))
        return made[-1]

    r = await bench_stt("fake", factory, bytes(640 * 5), runs=3, warmup=1, realtime=False, timer=tick)
    assert len(made) == 4  # one fresh STT per run, warmup included
    assert all(s.cancelled for s in made)
    assert len(r.samples) == 3
    assert r.stats("final_ms")["p50"] == 300
    assert r.stats("connect_ms")["p50"] == 50
    assert r.notes == ["今天天气"]


async def test_stt_missing_final_is_an_error_not_a_crash(monkeypatch):
    import vak.eval.bench as bench

    monkeypatch.setattr(bench, "STT_FINAL_TIMEOUT_S", 0.01)

    class NoFinal(FakeSTT):
        async def finish(self) -> None:
            pass

    r = await bench_stt("x", lambda: NoFinal(Tick()), bytes(640), runs=2, warmup=0, realtime=False)
    assert r.samples == [] and len(r.errors) == 2


class FakeLLM:
    model = "m"

    def __init__(self, tick: Tick, chunks: list[LLMDelta]) -> None:
        self.tick, self.chunks = tick, chunks
        self.warmed = self.closed = False
        self.calls: list[tuple] = []

    async def warmup(self) -> None:
        self.warmed = True

    async def close(self) -> None:
        self.closed = True

    async def stream(self, messages, tools=None, tool_choice=None, max_tokens=None):
        self.calls.append((messages, tools))
        for d in self.chunks:
            self.tick.add(100)
            yield d

    async def cancel(self) -> None:
        pass


async def test_llm_first_segment_uses_cascade_splitter_and_sends_tools():
    tick = Tick()
    # first segment rule: >= 6 chars cut at punctuation, so "好的，" is too short
    llm = FakeLLM(tick, [LLMDelta(text="好的，"), LLMDelta(text="北京今天晴，"), LLMDelta(text="很暖和。"), LLMDelta(done=True)])
    r = await bench_llm("fake", llm, ["a", "b"], runs=2, warmup=1, timer=tick)
    assert llm.warmed and llm.closed
    assert len(llm.calls) == 5 and len(r.samples) == 4
    assert all(c[1] for c in llm.calls)  # tool table always sent
    assert llm.calls[0][0][0].role == "system"
    s = r.samples[0]
    assert (s["first_token_ms"], s["first_segment_ms"], s["total_ms"]) == pytest.approx((100, 200, 400))


async def test_llm_tool_call_counts_as_first_token():
    tick = Tick()
    llm = FakeLLM(tick, [LLMDelta(tool_call={"id": "1", "name": "get_weather", "arguments": {}}), LLMDelta(done=True)])
    r = await bench_llm("fake", llm, ["a"], runs=1, warmup=0, timer=tick)
    assert r.samples[0]["first_token_ms"] == pytest.approx(100) == r.samples[0]["first_segment_ms"]


class FakeTTS:
    sample_rate = 16000
    supports_text_stream = False
    voice = "v"

    def __init__(self, tick: Tick) -> None:
        self.tick = tick

    async def synthesize(self, text, request_id):
        self.tick.add(80)
        yield TTSChunk(pcm16=b"", sample_rate=16000)  # an empty chunk is not first audio
        self.tick.add(40)
        yield TTSChunk(pcm16=bytes(3200), sample_rate=16000)  # 100 ms audio
        self.tick.add(30)
        yield TTSChunk(pcm16=bytes(3200), sample_rate=16000, is_last=True)

    async def flush(self, request_id) -> None:
        pass


async def test_tts_first_audio_skips_empty_chunks_and_reports_rtf():
    tick = Tick()
    r = await bench_tts("fake", FakeTTS(tick), ["一句话。"], runs=2, warmup=1, timer=tick)
    assert len(r.samples) == 2
    s = r.samples[0]
    assert s["first_audio_ms"] == pytest.approx(120) and s["total_ms"] == pytest.approx(150)
    assert abs(s["rtf"] - 0.75) < 1e-9


def _res(stage: str, name: str, metric: str, v: float) -> Result:
    return Result(stage, name, samples=[{metric: v}])


def test_combinations_sum_headline_p50s_fastest_first():
    rs = [
        _res("stt", "s1", "final_ms", 300),
        _res("stt", "s2", "final_ms", 200),
        _res("llm", "l1", "first_segment_ms", 500),
        _res("tts", "t1", "first_audio_ms", 150),
        _res("tts", "t2", "first_audio_ms", 100),
        Result("tts", "t3", skipped="no key"),
    ]
    combos = rank_combinations(rs)
    assert len(combos) == 4
    assert combos[0] == {"stt": "s2", "llm": "l1", "tts": "t2", "estimate_ms": 800}
    assert combos[-1]["estimate_ms"] == 950


def test_report_carries_source_and_metric_definitions():
    rep = Report({"tool": "vak bench", "site": "test", "started_at": "x"}, [_res("llm", "l1", "first_segment_ms", 5)])
    d = rep.to_dict()
    assert d["source"]["site"] == "test"
    assert "llm.first_segment_ms" in d["metrics"]
    assert "site: test" in rep.table()
    json.dumps(d, ensure_ascii=False)


async def test_run_bench_skips_unconfigured_candidates(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    spec = {
        "runs": 1,
        "llm": [{"type": "ark", "api_key": "${ARK_API_KEY}"}],
        "stt": [{"type": "volcengine_stt"}],
    }
    rep = await run_bench(spec, base_dir=tmp_path, stages={"stt", "llm"}, site="here")
    by = {(r.stage, r.name): r for r in rep.results}
    assert by[("llm", "ark")].skipped and "ARK_API_KEY" in by[("llm", "ark")].skipped
    assert by[("stt", "*")].skipped  # no audio configured
    assert rep.source["site"] == "here" and rep.source["runs"] == 1
