"""`vak` command line: new | run | probe | trace | eval."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "agent"

PROVIDER_BLOCKS: dict[str, dict[str, str]] = {
    "cascade": {
        "mock": """  stt:
    type: mock
    transcript: 今天天气怎么样
  llm:
    type: mock
  tts:
    type: mock""",
        "cloud": """  stt:
    type: openai_realtime_stt
    api_key: "${OPENAI_API_KEY}"
    model: gpt-4o-transcribe
  llm:
    type: openai_compat
    base_url: "${LLM_BASE_URL:-https://api.openai.com/v1}"
    api_key: "${OPENAI_API_KEY}"
    model: "${LLM_MODEL:-gpt-4o-mini}"
  tts:
    type: openai_tts
    api_key: "${OPENAI_API_KEY}"
    model: gpt-4o-mini-tts
    voice: alloy""",
        "selfhost_zh": """  stt:
    type: funasr
    url: "${FUNASR_URL:-ws://localhost:10095}"
  llm:
    type: openai_compat
    base_url: "${LLM_BASE_URL}"
    api_key: "${LLM_API_KEY:-none}"
    model: "${LLM_MODEL}"
  tts:
    type: cosyvoice
    url: "${COSYVOICE_URL:-http://localhost:50000}"
    voice: 中文女""",
        "volcengine": """  stt:
    type: volcengine_stt
    app_key: "${VOLC_ASR_APP_KEY}"
    access_key: "${VOLC_ASR_ACCESS_KEY}"
  llm:
    type: ark
    api_key: "${ARK_API_KEY}"
    model: "${LLM_MODEL:-doubao-seed-2-0-mini-260428}"
  tts:
    type: volcengine_tts
    app_key: "${VOLC_ASR_APP_KEY}"
    access_key: "${VOLC_ASR_ACCESS_KEY}"
    voice: zh_female_shuangkuaisisi_moon_bigtts""",
        "aliyun": """  stt:
    type: aliyun_stt
    api_key: "${DASHSCOPE_API_KEY}"
  llm:
    type: bailian
    api_key: "${DASHSCOPE_API_KEY}"
    model: "${LLM_MODEL:-qwen-flash}"
  tts:
    type: aliyun_tts
    api_key: "${DASHSCOPE_API_KEY}"
    model: cosyvoice-v2
    voice: longxiaochun_v2""",
    },
    "half_cascade": {
        "mock": """  audio_llm:
    type: mock
  tts:
    type: mock""",
        "cloud": """  audio_llm:
    type: openai_realtime_text
    api_key: "${OPENAI_API_KEY}"
    model: gpt-4o-realtime-preview
  tts:
    type: openai_tts
    api_key: "${OPENAI_API_KEY}"
    model: gpt-4o-mini-tts
    voice: alloy
  side_stt:
    type: openai_realtime_stt
    api_key: "${OPENAI_API_KEY}"
    model: gpt-4o-mini-transcribe""",
    },
    "s2s": {
        "mock": """  s2s:
    type: mock
  local_tts:
    type: mock""",
        "openai": """  s2s:
    type: openai_realtime
    api_key: "${OPENAI_API_KEY}"
    model: gpt-4o-realtime-preview
    voice: alloy
  local_tts:
    type: openai_tts
    api_key: "${OPENAI_API_KEY}"
    model: gpt-4o-mini-tts
    voice: alloy""",
        "dashscope": """  s2s:
    type: dashscope_realtime
    api_key: "${DASHSCOPE_API_KEY}"
    model: qwen-omni-turbo-realtime
    voice: Cherry
  local_tts:
    type: cosyvoice
    url: "${COSYVOICE_URL:-http://localhost:50000}"
    voice: 中文女""",
    },
    "full_duplex": {
        "mock": """  full_duplex:
    type: mock
  local_tts:
    type: mock""",
        "moshi": """  full_duplex:
    type: moshi
    url: "${MOSHI_URL:-ws://localhost:8998}"
  local_tts:
    type: mock""",
    },
}


def cmd_new(args: argparse.Namespace) -> int:
    dest = Path(args.name)
    if dest.exists() and any(dest.iterdir()):
        print(f"refusing to write into non-empty directory {dest}", file=sys.stderr)
        return 2
    if args.mode is None:
        args.mode = "open_mic" if args.pipeline == "full_duplex" else "ptt"
    blocks = PROVIDER_BLOCKS[args.pipeline]
    preset = args.providers or "mock"
    if preset not in blocks:
        print(f"unknown provider preset {preset!r} for {args.pipeline}; choose from {sorted(blocks)}", file=sys.stderr)
        return 2
    subst = {
        "{{name}}": dest.name,
        "{{profile}}": args.profile,
        "{{mode}}": args.mode,
        "{{pipeline}}": args.pipeline,
        "{{providers}}": blocks[preset],
    }
    for src in TEMPLATE_DIR.rglob("*"):
        rel = src.relative_to(TEMPLATE_DIR)
        out = dest / rel
        if src.is_dir():
            out.mkdir(parents=True, exist_ok=True)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix in {".yaml", ".py", ".md"}:
            text = src.read_text(encoding="utf-8")
            for k, v in subst.items():
                text = text.replace(k, v)
            out.write_text(text, encoding="utf-8")
        else:
            shutil.copy2(src, out)
    print(f"created {dest}/ (pipeline={args.pipeline}, profile={args.profile}, mode={args.mode}, providers={preset})")
    print(f"next: vak run {dest}/agent.yaml")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from vak.agent import Agent

    agent = Agent.from_yaml(args.config)
    if args.port:
        agent.config.server.port = args.port
    try:
        asyncio.run(agent.serve())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from vak.probes.cli import run_probes

    return run_probes(provider=args.provider, items=args.items, out=args.out, config=args.config, n=args.n)


def cmd_trace(args: argparse.Namespace) -> int:
    from vak.eval.trace_check import main as trace_main

    extra = ["--quiet"] if args.action == "summarize" else []
    if args.json:
        extra.append("--json")
    return trace_main([*args.paths, *extra])


def cmd_eval(args: argparse.Namespace) -> int:
    if args.layer == "l0":
        return _eval_l0(args)
    return _eval_l1(args)


def _eval_l0(args: argparse.Namespace) -> int:
    """L0 decision eval: cases → the agent's tool registry and a decide function.

    The decide function comes from the agent project (`eval_decide` in its tools
    module) because the kit does not know how the business decides without a
    model; the mock-free default only scores cases that expect no tool call."""
    from vak.eval.l0 import Decision, load_cases, run_l0

    decide = None
    if args.config:
        from vak.config import AgentConfig

        cfg = AgentConfig.load(args.config)
        if cfg.tools:
            import importlib

            if str(cfg.base_dir) not in sys.path:
                sys.path.insert(0, str(cfg.base_dir))
            mod = importlib.import_module(cfg.tools)
            decide = getattr(mod, "eval_decide", None)
    if decide is None:
        print("no eval_decide(state, history, text) -> Decision found in the tools module; scoring 'no tool' cases only")

        def decide(state, history, text):  # noqa: ANN001
            return Decision(calls=[], text="", final_state=dict(state))

    cases = [c for path in args.paths for c in load_cases(path)]
    result = run_l0(cases, decide)
    print(result.to_json())
    return 0


def _eval_l1(args: argparse.Namespace) -> int:
    """L1 dialogue scripts need a SessionDriver bound to a running agent; the kit
    ships the loopback driver used by its own integration tests."""
    import yaml

    from vak.eval.l1 import load_script, validate_script

    problems = 0
    for path in args.paths:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        errs = validate_script(raw)
        if errs:
            problems += 1
            print(f"{path}: invalid")
            for e in errs:
                print("  - " + e)
        else:
            s = load_script(path)
            print(f"{path}: ok (id={s.id}, race={s.race}, repeat={s.repeat})")
    if problems:
        return 1
    print("scripts validated; running them needs a SessionDriver, see vak.eval.l1.run_l1 and tests/integration")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from vak.eval.bench import main as bench_main

    return bench_main(config=args.config, stages=args.only, out=args.out, site=args.site, runs=args.runs)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vak", description="voice-agent-kit")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="create an agent project from the template")
    n.add_argument("name")
    n.add_argument("--pipeline", choices=sorted(PROVIDER_BLOCKS), default="cascade")
    n.add_argument("--profile", choices=["A", "B"], default="A")
    n.add_argument("--mode", choices=["ptt", "open_mic"], default=None, help="default: ptt, open_mic for full_duplex")
    n.add_argument("--providers", help="preset: mock | cloud | selfhost_zh | volcengine | aliyun | openai | dashscope | moshi")
    n.set_defaults(fn=cmd_new)

    r = sub.add_parser("run", help="run the gateway for an agent.yaml")
    r.add_argument("config")
    r.add_argument("--port", type=int)
    r.set_defaults(fn=cmd_run)

    pr = sub.add_parser("probe", help="run P0 capability probes against an upstream and write back capabilities")
    pr.add_argument("provider", help="provider type name or 'config' to use agent.yaml's s2s provider")
    pr.add_argument("--config", help="agent.yaml (for provider options)")
    pr.add_argument("--items", default="P0-1,P0-2", help="comma-separated probe ids")
    pr.add_argument("--out", help="capabilities yaml to write (default: alongside the provider's yaml)")
    pr.add_argument("-n", type=int, default=20)
    pr.set_defaults(fn=cmd_probe)

    t = sub.add_parser("trace", help="check / summarize trace jsonl files")
    t.add_argument("action", choices=["check", "summarize"])
    t.add_argument("paths", nargs="+")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_trace)

    e = sub.add_parser("eval", help="run L0 / L1 evaluations")
    e.add_argument("layer", choices=["l0", "l1"])
    e.add_argument("paths", nargs="+")
    e.add_argument("--config", help="agent.yaml")
    e.set_defaults(fn=cmd_eval)

    b = sub.add_parser("bench", help="per-stage latency of cascade candidates (STT / LLM / TTS), see docs/bench.md")
    b.add_argument("config", help="bench.yaml listing candidates per stage")
    b.add_argument("--only", default="stt,llm,tts", help="comma-separated stages")
    b.add_argument("--runs", type=int, help="override runs per candidate")
    b.add_argument("--site", help="where the test ran (city / ISP / cloud region); recorded in the report")
    b.add_argument("--out", help="write the full JSON report here")
    b.set_defaults(fn=cmd_bench)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
