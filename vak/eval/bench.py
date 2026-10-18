"""`vak bench`: per-stage latency of cascade candidates (STT / LLM / TTS).

Each stage is timed the way the cascade uses it, which is where this differs
from xiaozhi-esp32-server's performance_tester (see docs/bench.md §2):

- STT: real 16 kHz speech fed at real-time pace in 20 ms frames, connection
  opened before the first frame (the cascade starts STT at turn start).
  Headline metric `final_ms` = finish() -> final transcript, i.e. the time
  from end of speech to the text the LLM can use. `connect_ms` is separate.
- LLM: requests are sequential, carry the system prompt and tool table, and
  run after `warmup()` (the cascade pre-connects at turn start).
  `first_token_ms` = first text or tool call; `first_segment_ms` = the first
  segment the cascade would send to TTS (vak.audio.text.split_sentences).
- TTS: after `warmup()`, `first_audio_ms` = first non-empty PCM chunk (never a
  control frame: providers only yield audio).

Every report carries a `source` block (who/where/when/what audio) so numbers
are never quoted without their conditions. The combination ranking is the sum
of per-stage P50s, an estimate, not an end-to-end measurement.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import itertools
import json
import logging
import platform
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from vak.audio.text import split_sentences
from vak.upstream.base import ToolDef
from vak.upstream.providers.base import LLM, STT, TTS, ChatMessage

log = logging.getLogger(__name__)

FRAME_MS = 20
STT_FINAL_TIMEOUT_S = 10.0
REQUEST_TIMEOUT_S = 30.0

DEFAULT_SYSTEM = (
    "你是一个语音助手，回答会被直接念给用户听。"
    "用口语，一两句话，先说结论，不要用 markdown、列表和表情。"
)
DEFAULT_PROMPTS = ["今天北京天气怎么样？", "帮我定一个明天早上七点的闹钟。", "给我讲一个很短的笑话。"]
DEFAULT_SENTENCES = ["好的，已经帮你定好了明天早上七点的闹钟。", "北京今天晴，最高气温二十三度。"]
DEFAULT_TOOLS = [
    ToolDef(
        name="get_weather",
        description="查询城市天气",
        params={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    ),
    ToolDef(
        name="set_alarm",
        description="设定闹钟",
        params={"type": "object", "properties": {"time": {"type": "string", "description": "HH:MM"}}, "required": ["time"]},
    ),
]

# headline metric per stage, used for ranking and the combination estimate
HEADLINE = {"stt": "final_ms", "llm": "first_segment_ms", "tts": "first_audio_ms"}


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Result:
    stage: str
    name: str
    samples: list[dict[str, float]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: str | None = None
    notes: list[str] = field(default_factory=list)  # e.g. transcripts, for a sanity look

    def stats(self, metric: str) -> dict[str, float] | None:
        vals = sorted(s[metric] for s in self.samples if metric in s)
        if not vals:
            return None
        return {
            "n": len(vals),
            "p50": round(_pct(vals, 50), 1),
            "p90": round(_pct(vals, 90), 1),
            "min": round(vals[0], 1),
            "max": round(vals[-1], 1),
        }

    def metrics(self) -> list[str]:
        seen: list[str] = []
        for s in self.samples:
            seen += [k for k in s if k not in seen]
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "name": self.name,
            "skipped": self.skipped,
            "stats": {m: self.stats(m) for m in self.metrics()},
            "errors": self.errors,
            "notes": self.notes,
            "samples": self.samples,
        }


def _pct(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile of an already sorted list."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _ms(t0: float, t1: float) -> float:
    return (t1 - t0) * 1000


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------


async def bench_stt(
    name: str,
    factory: Callable[[], STT],
    pcm16: bytes,
    *,
    runs: int,
    warmup: int = 1,
    realtime: bool = True,
    timer: Callable[[], float] = time.perf_counter,
) -> Result:
    """One fresh STT per run (the cascade builds one per turn)."""
    res = Result("stt", name)
    for i in range(warmup + runs):
        stt = factory()
        frame = stt.sample_rate * FRAME_MS // 1000 * 2
        first_partial: float | None = None
        final: asyncio.Future[tuple[float, str]] = asyncio.get_running_loop().create_future()

        async def collect(stt: STT = stt, final: asyncio.Future[tuple[float, str]] = final) -> None:
            nonlocal first_partial
            async for tr in stt.results():
                if first_partial is None and tr.text:
                    first_partial = timer()
                if tr.final and not final.done():
                    final.set_result((timer(), tr.text))
                    return

        try:
            t0 = timer()
            await asyncio.wait_for(stt.start(), REQUEST_TIMEOUT_S)
            t_conn = timer()
            pump = asyncio.create_task(collect())
            t_speech = timer()
            for k, off in enumerate(range(0, len(pcm16), frame)):
                await stt.feed(pcm16[off : off + frame])
                if realtime:  # pace against the start, so slow feeds do not accumulate drift
                    await asyncio.sleep(max(0.0, t_speech + (k + 1) * FRAME_MS / 1000 - timer()))
            t_end = timer()
            await stt.finish()
            t_final, text = await asyncio.wait_for(asyncio.shield(final), STT_FINAL_TIMEOUT_S)
            pump.cancel()
            if i < warmup:
                continue
            sample = {"connect_ms": _ms(t0, t_conn), "final_ms": _ms(t_end, t_final)}
            if first_partial is not None:
                sample["first_partial_ms"] = _ms(t_speech, first_partial)
            res.samples.append(sample)
            if text not in res.notes:
                res.notes.append(text)
        except Exception as e:  # a failed run is reported, never fatal
            res.errors.append(f"run {i}: {type(e).__name__}: {e}")
        finally:
            await _quiet(stt.cancel())
    return res


async def bench_llm(
    name: str,
    llm: LLM,
    prompts: list[str],
    *,
    runs: int,
    warmup: int = 1,
    system: str = DEFAULT_SYSTEM,
    tools: list[ToolDef] | None = None,
    timer: Callable[[], float] = time.perf_counter,
) -> Result:
    """Sequential requests; `runs` passes over `prompts`, the first `warmup`
    requests are discarded. `tools=None` sends DEFAULT_TOOLS."""
    res = Result("llm", name)
    tools = DEFAULT_TOOLS if tools is None else tools  # [] benchmarks without a tool table
    await _warm(llm)
    jobs = [prompts[0]] * warmup + [p for _ in range(runs) for p in prompts]
    for i, prompt in enumerate(jobs):
        msgs = [ChatMessage(role="system", content=system), ChatMessage(role="user", content=prompt)]
        try:
            sample = await asyncio.wait_for(_one_llm(llm, msgs, tools, timer), REQUEST_TIMEOUT_S)
        except Exception as e:
            res.errors.append(f"request {i}: {type(e).__name__}: {e}")
            continue
        if i >= warmup:
            res.samples.append(sample)
    await _close(llm)
    return res


async def _one_llm(llm: LLM, msgs: list[ChatMessage], tools: list[ToolDef] | None, timer: Callable[[], float]) -> dict[str, float]:
    t0 = timer()
    first: float | None = None
    segment: float | None = None
    text = ""
    async for d in llm.stream(msgs, tools=tools or None):
        if first is None and (d.text or d.tool_call):
            first = timer()
        if d.text:
            text += d.text
            if segment is None and split_sentences(text, first=True)[0]:
                segment = timer()
        if d.tool_call and segment is None:
            segment = timer()  # a tool call ends the wait for speakable text
    t1 = timer()
    if first is None:
        raise RuntimeError("empty response")
    return {
        "first_token_ms": _ms(t0, first),
        "first_segment_ms": _ms(t0, segment if segment is not None else t1),
        "total_ms": _ms(t0, t1),
    }


async def bench_tts(
    name: str,
    tts: TTS,
    sentences: list[str],
    *,
    runs: int,
    warmup: int = 1,
    timer: Callable[[], float] = time.perf_counter,
) -> Result:
    res = Result("tts", name)
    await _warm(tts)
    jobs = [sentences[0]] * warmup + [s for _ in range(runs) for s in sentences]
    for i, text in enumerate(jobs):
        try:
            sample = await asyncio.wait_for(_one_tts(tts, text, f"bench-{i}", timer), REQUEST_TIMEOUT_S)
        except Exception as e:
            res.errors.append(f"request {i}: {type(e).__name__}: {e}")
            continue
        if i >= warmup:
            res.samples.append(sample)
    await _close(tts)
    return res


async def _one_tts(tts: TTS, text: str, rid: str, timer: Callable[[], float]) -> dict[str, float]:
    t0 = timer()
    first: float | None = None
    audio_ms = 0.0
    async for chunk in tts.synthesize(text, rid):
        if chunk.pcm16:
            if first is None:
                first = timer()
            audio_ms += len(chunk.pcm16) / 2 / chunk.sample_rate * 1000
    t1 = timer()
    if first is None:
        raise RuntimeError("no audio")
    sample = {"first_audio_ms": _ms(t0, first), "total_ms": _ms(t0, t1)}
    if audio_ms:
        sample["rtf"] = _ms(t0, t1) / audio_ms
    return sample


async def _warm(provider: Any) -> None:
    fn = getattr(provider, "warmup", None)
    if fn is not None:
        await _quiet(fn())


async def _close(provider: Any) -> None:
    fn = getattr(provider, "close", None)
    if fn is not None:
        await _quiet(fn())


async def _quiet(aw: Any) -> None:
    try:
        await aw
    except Exception as e:
        log.debug("bench: ignored %s", e)


# ---------------------------------------------------------------------------
# combinations and report
# ---------------------------------------------------------------------------


def rank_combinations(results: list[Result]) -> list[dict[str, Any]]:
    """Every STT x LLM x TTS triple with all three headline P50s, fastest first.
    Missing stages are left out of the product (e.g. an LLM-only run)."""
    per_stage: dict[str, list[tuple[str, float]]] = {}
    for r in results:
        s = r.stats(HEADLINE[r.stage])
        if s is not None:
            per_stage.setdefault(r.stage, []).append((r.name, s["p50"]))
    stages = [st for st in ("stt", "llm", "tts") if st in per_stage]
    if len(stages) < 2:
        return []
    combos = []
    for picks in itertools.product(*(per_stage[st] for st in stages)):
        combos.append(
            {
                **{st: name for st, (name, _) in zip(stages, picks, strict=True)},
                "estimate_ms": round(sum(v for _, v in picks), 1),
            }
        )
    return sorted(combos, key=lambda c: c["estimate_ms"])


@dataclass(slots=True)
class Report:
    source: dict[str, Any]
    results: list[Result]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "metrics": {
                "stt.final_ms": "finish() (end of speech) -> final transcript; connection opened beforehand",
                "stt.first_partial_ms": "first audio frame -> first non-empty partial",
                "stt.connect_ms": "start() duration, not part of the turn latency",
                "llm.first_token_ms": "request -> first text or tool call (system prompt + tools, sequential, warmed)",
                "llm.first_segment_ms": "request -> first segment the cascade would send to TTS",
                "tts.first_audio_ms": "request -> first non-empty PCM chunk (warmed connection)",
                "combinations.estimate_ms": "sum of headline P50s; an estimate, not an end-to-end measurement",
            },
            "results": [r.to_dict() for r in self.results],
            "combinations": rank_combinations(self.results),
        }

    def table(self) -> str:
        src = self.source
        lines = [
            "source: vak bench {ver} | {at} | site: {site} | audio: {audio} | runs={runs} warmup={warmup}".format(
                ver=src.get("vak_version"), at=src.get("started_at"), site=src.get("site"),
                audio=src.get("audio") or "-", runs=src.get("runs"), warmup=src.get("warmup"),
            )
        ]
        for stage in ("stt", "llm", "tts"):
            rows = [r for r in self.results if r.stage == stage]
            if not rows:
                continue
            head = HEADLINE[stage]
            rows.sort(key=lambda r: (r.stats(head) or {"p50": float("inf")})["p50"])
            lines.append(f"\n[{stage}] sorted by {head} P50 (ms)")
            for r in rows:
                if r.skipped:
                    lines.append(f"  {r.name:<28} skipped: {r.skipped}")
                    continue
                cols = []
                for m in r.metrics():
                    s = r.stats(m)
                    if s is not None:
                        cols.append(f"{m} p50={s['p50']:g} p90={s['p90']:g}")
                ok = f"{len(r.samples)} ok"
                if r.errors:
                    ok += f", {len(r.errors)} failed ({r.errors[0]})"
                lines.append(f"  {r.name:<28} {' | '.join(cols) or '-'}  [{ok}]")
                if stage == "stt" and r.notes:
                    lines.append(f"  {'':<28} text: {r.notes[0]}")
        combos = rank_combinations(self.results)
        if combos:
            lines.append("\n[combinations] estimate = sum of headline P50s, not measured end to end")
            for c in combos[:10]:
                parts = " + ".join(c[st] for st in ("stt", "llm", "tts") if st in c)
                lines.append(f"  {c['estimate_ms']:>8g} ms  {parts}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# config file + entry point
# ---------------------------------------------------------------------------


def load_audio(path: str | Path) -> tuple[bytes, int]:
    """Mono PCM16 WAV -> (pcm, sample_rate). Real speech; never resampled here."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"{path}: need a mono PCM16 WAV")
        return w.readframes(w.getnframes()), w.getframerate()


def _label(entry: dict[str, Any]) -> str:
    return str(entry.get("name") or f"{entry['type']}:{entry.get('model') or entry.get('voice') or ''}".rstrip(":"))


def _options(entry: dict[str, Any]) -> dict[str, Any]:
    from vak.config import _expand_env

    return {k: v for k, v in _expand_env(entry).items() if k not in ("type", "name")}


async def run_bench(
    spec: dict[str, Any],
    *,
    base_dir: Path,
    stages: set[str],
    site: str | None = None,
    runs: int | None = None,
) -> Report:
    from vak.upstream.providers import build_provider

    runs = int(runs or spec.get("runs", 5))
    warmup = int(spec.get("warmup", 1))
    audio_path = spec.get("audio")
    source = {
        "tool": "vak bench",
        "vak_version": _version(),
        "started_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "site": site or spec.get("site") or "unspecified",
        "audio": str(audio_path) if audio_path else None,
        "runs": runs,
        "warmup": warmup,
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
    }
    results: list[Result] = []

    def build(entry: dict[str, Any], stage: str) -> tuple[str, Any]:
        name = _label(entry)
        try:
            opts = _options(entry)
            return name, (lambda: build_provider(entry["type"], opts)) if stage == "stt" else build_provider(entry["type"], opts)
        except Exception as e:  # missing key/env/dependency: skip like a placeholder key
            results.append(Result(stage, name, skipped=f"{type(e).__name__}: {e}"))
            return name, None

    if "stt" in stages and spec.get("stt"):
        pcm, rate = b"", 0
        if not audio_path:
            results.append(Result("stt", "*", skipped="no `audio:` (16 kHz mono PCM16 WAV of real speech)"))
        else:
            try:
                pcm, rate = load_audio(base_dir / audio_path)
            except (OSError, ValueError, wave.Error) as e:
                results.append(Result("stt", "*", skipped=f"audio: {e}"))
        if pcm:
            source["audio_ms"] = len(pcm) // 2 * 1000 // rate
            for entry in spec["stt"]:
                name, factory = build({"sample_rate": rate, **entry}, "stt")
                if factory is not None:
                    try:
                        factory()  # construct once so config errors are a skip, not N failures
                    except Exception as e:
                        results.append(Result("stt", name, skipped=f"{type(e).__name__}: {e}"))
                        continue
                    results.append(await bench_stt(name, factory, pcm, runs=runs, warmup=warmup))
    if "llm" in stages:
        tools = [ToolDef(**t) for t in spec["tools"]] if "tools" in spec else DEFAULT_TOOLS
        for entry in spec.get("llm") or []:
            name, llm = build(entry, "llm")
            if llm is not None:
                results.append(
                    await bench_llm(
                        name, llm, spec.get("prompts") or DEFAULT_PROMPTS, runs=runs, warmup=warmup,
                        system=spec.get("system_prompt") or DEFAULT_SYSTEM, tools=tools,
                    )
                )
    if "tts" in stages:
        for entry in spec.get("tts") or []:
            name, tts = build(entry, "tts")
            if tts is not None:
                results.append(await bench_tts(name, tts, spec.get("sentences") or DEFAULT_SENTENCES, runs=runs, warmup=warmup))
    return Report(source, results)


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("voice-agent-kit")
    except Exception:
        return "unknown"


def main(*, config: str, stages: str, out: str | None, site: str | None, runs: int | None) -> int:
    path = Path(config)
    spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    wanted = {s.strip() for s in stages.split(",") if s.strip()}
    report = asyncio.run(run_bench(spec, base_dir=path.parent, stages=wanted, site=site, runs=runs))
    print(report.table())
    if out:
        Path(out).write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwritten: {out}")
    return 0
