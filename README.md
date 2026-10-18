# voice-agent-kit

English · [中文](README.zh-CN.md)

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Tests](https://img.shields.io/badge/tests-1055%20passing-brightgreen.svg)

**A runtime for real-time voice agents, implemented against written contracts.** voice-agent-kit (Python package `vak`) provides turn-taking, barge-in handling, output arbitration, tool-call closure, session reconstruction and tracing. Developers supply a persona, tool functions and an `agent.yaml`. The same agent runs on four pipeline architectures: cascade (STT → LLM → TTS), half-cascade, end-to-end speech-to-speech (S2S) and native full-duplex. Changing the pipeline requires no change to the device protocol or to application code.

> **Status: v0.1.** All four pipelines run end to end against mock upstreams; 1055 automated tests cover that path. Integrations with Volcengine, Aliyun, OpenAI, Deepgram, AssemblyAI and other vendors are in place, but they have been tested only at the protocol level, against simulated servers. **They have not yet been validated against live services.** See [Status and limitations](#status-and-limitations).
>
> The documentation under `docs/` is written in Chinese. Code, docstrings and comments are in English.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/architecture-dark.svg">
  <img alt="Layered structure of vak: a client connects over protocol va/1 to the vak runtime (transport, core modules, upstream adapters for four pipelines); developers write agent.yaml, persona.yaml, tools.py and state_schema.yaml; vendor providers plug in at the bottom" src="docs/assets/architecture-light.svg" width="100%">
</picture>

## Motivation

In practice, the hard engineering problems in a voice agent are seldom the choice of ASR or TTS vendor. They are problems such as these:

- **Barge-in.** When the user interrupts, the device must stop playback within roughly 100 ms. No audio from the previous reply may carry over into the new turn.
- **Tool calls.** A tool call issued mid-reply must reach a defined outcome even if the user interrupts. There must be a clear rule for who announces the result and when, and write operations must not be executed twice.
- **Session recovery.** After a network failure or an expired vendor session, the context must be restored from the application's own record rather than from the vendor's copy.
- **First-audio latency.** When the first audible response takes three seconds, it must be possible to determine which stage is responsible.

None of these problems depends on the choice of model, yet most projects implement them anew, often repeatedly. voice-agent-kit first specifies the required behavior in [seven specifications](docs/specs/), then implements those specifications once, as a runtime shared by all pipelines.

## Learning path

This repository is a companion to [voice-agent-handbook](https://github.com/wananing/voice-agent-handbook) (in Chinese). The handbook explains the design rationale; this repository provides an implementation that can be run and measured. The following sequence is recommended:

1. **Foundations.** Read the handbook's [01-foundations](https://github.com/wananing/voice-agent-handbook/tree/master/01-foundations). Its six short chapters cover audio fundamentals, ASR, TTS, VAD versus endpointing, transport, and the [latency budget](https://github.com/wananing/voice-agent-handbook/blob/master/01-foundations/latency-budget.md).
2. **Hands-on.** Follow the [Quick start](#quick-start) to run an agent against mock upstreams. You can converse with it and interrupt it in the browser, with no API keys.
3. **Mechanisms.** For a specific question, read the corresponding handbook chapter ([turn detection](https://github.com/wananing/voice-agent-handbook/blob/master/03-mechanisms/turn-detection.md), [interruption](https://github.com/wananing/voice-agent-handbook/blob/master/03-mechanisms/interruption.md), [first audio](https://github.com/wananing/voice-agent-handbook/blob/master/03-mechanisms/first-audio.md), [tool calls](https://github.com/wananing/voice-agent-handbook/blob/master/03-mechanisms/tool-calls.md)), then the specification in `docs/specs/` that defines it.
4. **Measurement.** Use the [first-audio lab guide](docs/first-audio-lab.md) to break latency down by stage and to evaluate each optimization in isolation. Once real vendors are connected, use `vak bench` to compare candidates on your own network.

## What developers write

A generated project contains `agent.yaml`, `persona.yaml`, `state_schema.yaml`, `tools.py` and a `cues/` directory. The pipeline and providers are configured in `agent.yaml`:

```yaml
# agent.yaml
profile: A          # A: screenless device; B: application with a screen
mode: ptt           # ptt: push-to-talk; open_mic: hands-free (requires client echo cancellation)
pipeline: cascade   # cascade | half_cascade | s2s | full_duplex
providers:
  stt: { type: volcengine_stt, app_key: "${VOLC_ASR_APP_KEY}", access_key: "${VOLC_ASR_ACCESS_KEY}" }
  llm: { type: ark, api_key: "${ARK_API_KEY}", model: doubao-seed-2-0-mini-260428 }
  tts: { type: volcengine_tts, app_key: "${VOLC_ASR_APP_KEY}", access_key: "${VOLC_ASR_ACCESS_KEY}" }
```

Tools are ordinary asynchronous functions. The decorator declares the tool's properties; the runtime decides how the result is spoken:

```python
@tool(
    presentation="verbatim",              # speak a fixed template; no model regeneration
    latency_class="seconds",              # slow enough to warrant an acknowledgement first
    write=True,                           # a write operation: gates and an idempotency key are mandatory
    voice_template="好，{item} 已经记下了。",
    idempotency_key=["item"],
    gates=[{"kind": "utterance", "must_match": ["记", "加"], "must_not_match": ["怎么记"]}],
    interrupt_policy="finish_then_notify",
    progress={"ack_template": "好，我记一下。"},
)
async def add_note(item: str) -> dict:
    """Record an item the user mentioned."""
    return {"ok": True, "summary": f"noted: {item}", "data": {"item": item}}
```

From these declarations the runtime:

- **Selects the speaking route.** It speaks the template directly, speaks an acknowledgement followed by the result, or passes the result back to the model to regenerate the reply.
- **Evaluates the gates** against the user's actual words.
- **Deduplicates calls** by `idempotency_key`.
- **Closes every call** in one of six terminal states: `result`, `progress`, `interrupted`, `cancelled`, `superseded` or `duplicate`.

A write tool without gates is rejected at startup. Application code never refers to `turn_id`, `generation` or `flush`.

## What the runtime provides

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/barge-in-dark.svg">
  <img alt="Barge-in sequence: the device stops playback locally and sends playback.interrupted, abort and turn.start; vak increments the generation and flushes, cancels and truncates the upstream reply to the played position, discards late audio from the old generation, and closes the old turn as interrupted" src="docs/assets/barge-in-light.svg" width="100%">
</picture>

- **Turn protocol** ([spec 01](docs/specs/01-turn-model-and-client-protocol.md)). Every audio frame carries a `turn_id`, and every turn ends in an explicit terminal state. Push-to-talk and open-mic are two configurations of a single state machine. A dropped connection can resume within 30 s.
- **Output arbitration** ([spec 02](docs/specs/02-output-arbiter.md)). The arbiter determines what may be spoken and in which order. Generation numbers are checked at enqueue, at grant and on every frame sent, so stale audio is discarded on both the server and the device. What the user actually heard is established from client playback receipts, not from the server finishing transmission.
- **Tool closure** ([spec 03](docs/specs/03-tool-registry-and-call-closure.md)). A registry, four speaking routes, gate chains, idempotency keys and six terminal states together ensure that no call is left unresolved.
- **Upstream contract** ([spec 04](docs/specs/04-upstream-adapter-capabilities.md)). Each pipeline maps onto nine primitives. Vendor differences are expressed as capability declarations rather than vendor-specific branches, and `vak probe` can measure the key capabilities and record the results.
- **Context and session** ([spec 05](docs/specs/05-context-and-session.md)). The local record is the single source of truth. The upstream session is a mirror and can be rebuilt from that record at any time; this is how the runtime recovers from disconnects, session timeouts and pipeline switches.
- **Tracing and evaluation** ([spec 06](docs/specs/06-trace-and-evaluation.md)). Each turn produces one trace record. First audio is reported as two metrics, first sound and first content, and latency is broken down by stage: STT, endpointing, LLM, segmentation and TTS. `vak eval` provides decision-level and dialogue-level evaluation.

For first audio on the cascade pipeline, the runtime also moves work off the critical path:

- **Streaming STT.** Recognition is bidirectional and streaming, and the connection is opened at turn start.
- **Pre-connection.** LLM and TTS connections are opened at turn start and kept alive for 60 s.
- **Lookahead synthesis.** The next sentence is synthesized while the current one plays.
- **Token-level TTS input.** Volcengine TTS receives LLM tokens as they are generated, with one session per reply.
- **Thinking disabled.** The thinking mode is off by default for Ark and Bailian.

The [first-audio lab guide](docs/first-audio-lab.md) describes how to measure each of these and compare it against a baseline.

## Quick start

```bash
git clone https://github.com/wananing/voice-agent-kit && cd voice-agent-kit
uv sync --extra dev
uv run vak new my_agent --pipeline cascade --profile A      # mock providers; no API key required
uv run vak run my_agent/agent.yaml                           # open http://localhost:8765/ and hold the button to speak
uv run vak trace summarize my_agent/traces/*.jsonl           # first-audio P50 / P90 and per-stage latency
```

There are two ways to switch to real vendors: regenerate the project with a preset (for example `--providers volcengine`), or edit `providers:` in `agent.yaml`. A complete walkthrough is in [docs/getting-started.md](docs/getting-started.md).

## Pipelines

| Pipeline | Composition | Appropriate when |
|---|---|---|
| `cascade` | streaming STT → text LLM → streaming TTS | Control, a pre-speech guardrail and replaceable models are required; the default choice for production in Chinese |
| `half_cascade` | audio-understanding model (text output) → TTS | Some paralinguistic information should be retained while keeping a text layer for review before speech |
| `s2s` | OpenAI Realtime, Qwen-Omni Realtime | First-audio latency takes priority; the degree of control depends on the vendor's capabilities |
| `full_duplex` | moshi-style models | Research and demonstration |

Push-to-talk and open-mic are available on every pipeline. Interruptible open-mic operation is an interaction mode, not a fifth pipeline.

## Providers

| Category | Providers |
|---|---|
| Streaming STT | `volcengine_stt` (bigmodel_async), `aliyun_stt` (paraformer-realtime-v2), `openai_realtime_stt`, `deepgram_stt`, `assemblyai_stt`, `funasr` (self-hosted); non-streaming `openai_stt` |
| LLM | `openai_compat` (any OpenAI-compatible endpoint), `ark` (Volcengine Ark; thinking disabled and `service_tier: fast` by default), `bailian` (Aliyun Bailian; thinking disabled by default) |
| Streaming TTS | `volcengine_tts` (bidirectional, token-level input), `aliyun_tts` (CosyVoice cloud service), `openai_tts`, `cosyvoice` (self-hosted) |
| Speech-to-speech | `openai_realtime`, `dashscope_realtime` |
| Full-duplex | `moshi` |

Cascade presets available through `vak new --providers`:

| Preset | Composition | Credentials required |
|---|---|---|
| `volcengine` | Volcengine speech recognition, Ark and Volcengine speech synthesis | One set of Volcengine credentials |
| `aliyun` | Bailian speech recognition, Bailian LLM and CosyVoice | One DashScope API key |
| `cloud` | OpenAI for all three stages | OpenAI API key |
| `selfhost_zh` | FunASR, CosyVoice and a self-hosted LLM | None |

A new provider requires implementing one small interface; see [contributing §5](docs/contributing.md#5-加一个-provider). Configuration keys, environment variables and known issues for each provider are documented in [docs/providers.md](docs/providers.md).

## Command-line interface

| Command | Function |
|---|---|
| `vak new` | Generate an agent project from the template |
| `vak run` | Start the WebSocket gateway, including the browser reference client |
| `vak probe` | Run P0 capability probes against an S2S upstream and record the results in its capability declaration |
| `vak trace check / summarize` | Validate traces; report first-audio latency and its per-stage breakdown |
| `vak eval l0 / l1` | Decision-level and dialogue-level evaluation |
| `vak bench` | Measure STT, LLM and TTS candidates for the cascade on the local network and rank their combinations ([docs/bench.md](docs/bench.md)) |

## Relation to pipecat, LiveKit Agents and TEN

Those projects are general-purpose frameworks with extensive plugin ecosystems, WebRTC transport and telephony support. voice-agent-kit has a narrower scope and is not built on any of them. It is a gateway runtime whose behavior is governed by written contracts, centered on:

- a device protocol for resource-constrained hardware;
- output arbitration;
- tool-call closure;
- capability-based degradation across four pipeline architectures.

Projects that need WebRTC rooms, telephone lines or a large catalog of ready-made integrations are better served by those frameworks. voice-agent-kit is intended for projects in which barge-in, tool and session behavior must be precisely specified, tested and portable to firmware.

The design decisions derive from an analysis of 28 open-source voice projects, including the three frameworks above, published as [voice-agent-handbook](https://github.com/wananing/voice-agent-handbook). Source abbreviations in the specifications and code comments (KM, HC, `notes/…`) can be resolved to handbook pages through the table in [docs/specs/README.md](docs/specs/README.md#出处简写对照).

## Documentation

| Document | Contents |
|---|---|
| [getting-started](docs/getting-started.md) | Running on mock upstreams, then switching to real providers |
| [architecture](docs/architecture.md) | Layering, how four pipelines share one interface, module ports, integration conventions |
| [specs/](docs/specs/) | Specifications 00–06 (the contracts); every "spec 0X §y" in code comments refers to these |
| [first-audio-lab](docs/first-audio-lab.md) | Per-stage first-audio measurement, location of each optimization, controlled experiments |
| [protocol](docs/protocol.md) | Device protocol va/1 and the steps to integrate a new client or firmware |
| [providers](docs/providers.md) | Provider configuration, environment variables, known issues, items pending live validation |
| [bench](docs/bench.md) | Benchmark methodology and reference data with attributed sources |
| [contributing](docs/contributing.md) | Contract files, code and test conventions, adding a provider |
| [examples/](examples/) | Six example projects and a benchmark configuration |
| [clients/web/](clients/web/) | Dependency-free browser reference client; `protocol.js` is portable to firmware |

## Status and limitations

- **Verified**:
  - 1055 Python tests and 17 Node tests for the browser client pass.
  - On mock upstreams, all four pipelines are covered for complete turns, barge-in, text turns, open-mic endpointing and WebSocket resumption.
  - All four pipelines pass a shared upstream conformance suite.
- **Not yet validated on live services**:
  - Every real provider has been tested only against simulated WebSocket and HTTP servers.
  - No P0 probe has been run against a live upstream, so key capability fields remain `code_read` or `unknown`.
  - Open items for each provider are listed in [docs/providers.md](docs/providers.md).
- **Not yet calibrated**: parameters marked in the specifications as "suggested value, requires measurement".
- **Not yet implemented**:
  - Speculative generation in open-mic mode.
  - First-chunk streaming for self-hosted CosyVoice (its official server does not enable streaming).
  - Token-level input for `aliyun_tts`.
  - WebRTC transport.

At this stage, the most valuable contributions are measurements from real upstreams, such as `vak bench` output, probe results or traces, accompanied by a description of the network conditions.

## Contributing

Please read [docs/contributing.md](docs/contributing.md) first. Before submitting changes, run:

```bash
uv run pytest -q && uv run ruff check vak tests && node --test clients/web/
```

None of these checks requires network access or API keys.

## License

voice-agent-kit is released under the [MIT](LICENSE) license. [voice-agent-handbook](https://github.com/wananing/voice-agent-handbook) is licensed separately under CC BY 4.0.
