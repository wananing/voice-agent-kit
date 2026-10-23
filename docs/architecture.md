# voice-agent-kit 架构

> 状态：v0.1，里程碑状态见 §8，集成时定下的补充约定见 §11。
> 上位文档：[`docs/specs/`](specs/) 下的规范 00–06（下称"规范 0x"），以及 [voice-agent-handbook](https://github.com/wananing/voice-agent-handbook) 的 [`02-architectures/`](https://github.com/wananing/voice-agent-handbook/tree/master/02-architectures)（四种链路形态的取舍）。本文只讲脚手架怎么把它们变成代码，不重复规范内容。

## 0. 一句话

脚手架是一个实现了规范 01–06 的**网关运行时**（Python 包 `vak`），四种链路形态（级联、半级联、端到端 S2S、全双工）只是其中"上游适配层"的四种插件。开发者基于它做一个 voice agent 时只写三样东西：agent 配置（人设、链路、profile）、工具函数、业务后端对接；回合状态机、设备协议、输出仲裁、工具收口、上下文与会话重建、trace 与评测全部由脚手架提供，不再重写。

## 1. 边界：开发者写什么，脚手架给什么

| 开发者写 | 脚手架给 |
|---|---|
| `persona.yaml`：人设分节清单（规范 05 §2.4） | 人设渲染、版本号、禁用词自动检查 |
| 工具函数 + `@tool(...)` 元数据（规范 03 §2.1 的注册表字段） | 注册表校验、A0–A3 分流、门槛链、收口、打断矩阵、委派协议 |
| `state_schema.yaml`：状态块字段与生效层 | 状态块渲染、注入通道选择、懒 / 立即生效 |
| `agent.yaml`：选链路、选 provider、选 profile 与模式 | 回合状态机、WebSocket 协议、打断时序、断线续接、看门狗 |
| 业务后端 client（任意 HTTP/gRPC） | 写操作幂等键、执行前查代际、`superseded` 收口 |
| 可选：自定义 provider（实现 STT / LLM / TTS / S2S 接口之一） | 四条链路的组合器、能力声明加载、P0 探针 harness |
| 可选：预合成 cue 音频 | cue 库、沉默填充、失败话术兜底 |
| 可选：L0 用例、L1 脚本 | trace schema、收集器、校验器、L0/L1 runner、rubric judge |

验收口径：开发者的 agent 目录里不出现 `turn_id`、`generation`、`flush`、`response.create` 这些词。

## 2. 分层

```
┌─ 客户端 ──────────────────────────────────────────────────────────────┐
│ 浏览器参考客户端 (clients/web)  ·  硬件 / App 按 va/1 协议自己实现        │
└───────────────▲ WebSocket: JSON 控制帧 + 16 字节头音频帧 (规范 01) ──────┘
┌─ vak.transport ─┴─────────────────────────────────────────────────────┐
│ 协议编解码 · 帧头 · 下行限速 (lead_ms) · seq / 续接补发 · ping 时钟锚点     │
└───────────────▲───────────────────────────────────────────────────────┘
┌─ vak.core（链路无关层，策略全在这里）─────────────────────────────────────┐
│ turn/     回合状态机 · turn_id · 看门狗 · 两种模式 (ptt / open_mic)        │
│ arbiter/  输出仲裁 · 代际 · 租约 · 插话窗口 · 冲刷                          │
│ tools/    注册表 · A0–A3 分流 · 门槛链 · 调用生命周期 · 委派                 │
│ context/  本地记录 · 状态块 · 人设 · 历史投影 · 会话重建器 · 快照            │
│ trace/    trace schema · 收集器 · 导出 (JSONL / OTel)                     │
│ session.py  把以上五件按一个连接接起来；gateway.py 管多连接                  │
└───────────────▲ 九个原语 ↓ · 标准化事件 ↑ (规范 04) ───────────────────────┘
┌─ vak.upstream（链路适配层，只翻译不决策）──────────────────────────────────┐
│ base.py  Upstream 接口 · Ack · 事件信封 · 错误四分类                        │
│ capabilities/  能力声明 schema · 保守值 · 各 provider 的 yaml               │
│ pipelines/  cascade · half_cascade · s2s · full_duplex  ← 四种链路在这里    │
│ providers/  openai_realtime · dashscope_realtime · funasr · cosyvoice ·    │
│             openai_compat_llm · openai_stt / openai_tts · moshi             │
└───────────────────────────────────────────────────────────────────────┘
┌─ vak.audio ─┐ ┌─ vak.cue ─┐ ┌─ vak.eval ─┐ ┌─ vak.probes ─┐ ┌─ vak.cli ─┐
│ VAD · 判停   │ │ 预合成库   │ │ L0 L1 judge │ │ P0 探针      │ │ new run    │
│ 重采样 · 静音 │ │ 沉默填充   │ │ trace_check │ │ 写回能力声明  │ │ probe trace│
└─────────────┘ └───────────┘ └────────────┘ └─────────────┘ └───────────┘
```

三条硬边界，来自上位架构文档 §1 和规范 04 §3：

1. **设备协议与链路无关。** `vak.transport` 和 `vak.core.turn` 不知道上游是哪种链路。换链路不改协议。
2. **链路只做 I/O。** `vak.upstream` 里没有重试、没有重连、没有回合计数、没有按厂商名的分支。做不到就返回 `unsupported(<能力字段>)`。
3. **后端是唯一真相。** 上游会话是镜像，只写不读；本地上下文从不从镜像恢复。

## 3. 四种链路如何落到适配层

四种链路都实现同一个 `Upstream` 接口（九个原语、标准化事件流），差别用能力声明表达。`vak.core` 读能力声明分支，不读链路名。

| | 级联 `cascade` | 半级联 `half_cascade` | 端到端 S2S `s2s` | 原生全双工 `full_duplex` |
|---|---|---|---|---|
| 组合 | `STT + LLM + TTS` 三个 provider 接口 | `AudioLLM(text out) + TTS` | 一个 `RealtimeS2S` provider | 一个 `FullDuplexModel` provider |
| 判停 | 网关自控（`vak.audio`） | 网关自控；`uplink.streaming_input=whole_utterance` 时整段提交 | 看能力声明：能关就关，网关判停后 `commit_turn` | 无；网关只做 `open_mic` 上行，判停 / 打断事件由适配器从音频活动**合成** |
| 文本先于语音 | 是，`cascade.pre_speech_hook=sentence` | 是 | 否，`output.text_lead_ms` 几十毫秒 | 否，无护栏窗口 |
| 用户转写 | STT 原生 | 并行 STT 支路（`cascade.user_transcript=side_request`） | 上游 `transcript.user.*` | 无（`none`） |
| 工具 | `tools.calling=native`（文本 LLM） | 同级联 | 看声明：`auto_reply_after_result`、`silent_close` | `tools.calling=none` |
| 截断 | `truncate.mode=local_only`（按 TTS 播放位置） | `local_only` | `audio_ms` / `none` 看声明 | `none` |
| 历史 | 每次请求自带，注入即生效 | 同级联 | `history.injection={init_only}`，重灌 | 无 |
| TTS 音色 | 适配器内置，与本地直念**同一声音配置** | 同级联 | 上游出音频；本地直念另有音色（已知坑，规范 02） | 上游出音频 |

**双工方式与链路正交。** `ptt` / `open_mic` 是回合状态机的两种配置（规范 01 §2.3），对级联、半级联、S2S 都可用；`open_mic` 的前提是客户端 `caps.aec != none`。开放麦可打断（手册里的"组装式全双工"）就是 `open_mic` 模式，不是第五种链路。

**原生全双工的降级映射。** moshi 类模型没有回合、没有工具、没有截断。适配器把它接进来的方式：

- 能力声明里 `tools.calling=none`、`truncate.mode=none`、`response.client_initiated=no`、`turn.server_detection_disable=no`、`history.injection={}`。
- 上行：网关在 `open_mic` 下持续送帧；`commit_turn` 返回 `ok` + `committed_by_server`，`request_response` 返回 `unsupported(response.client_initiated)`。
- 下行：适配器检测上游音频从静音到非静音，合成 `response.started{origin: server}` 并分配 `response_id`；连续静音超过阈值合成 `response.done{completed}`。回合由网关按 `turn.start{source:"vad"}` 从上游的 `input.speech_started`（适配器从上行音频 VAD 合成）生成。
- 仲裁器照常工作：模型输出走 `upstream_stream`，本地 cue / narration / 错误话术仍由网关控制扬声器。这是全双工接进脚手架唯一真正得到的东西：设备协议、trace、本地播报、会话管理不用另写。
- 文档里明确标注：此模式下规范 01 的"打断时序"、规范 03 全部、规范 05 的历史相关条款**不适用**，trace 的对应字段写 `null`。

## 4. 核心对象与模块接口

所有模块共享 `vak/core/types.py`，枚举和字段名逐字取自规范 00 术语表。模块之间只通过 `vak/core/ports.py` 里的接口（Protocol）互相调用，便于单测时替换。

### 4.1 共享类型（节选）

| 类型 | 规范出处 | 关键字段 |
|---|---|---|
| `TurnId` | 01 §2.2 | `c<n>` / `s<n>` ↔ `uint32` 编解码 |
| `TurnState` | 01 §2.1 | `idle listening holding committed responding draining` + 五个终态 |
| `TurnSource` / `TurnInput` | 01 §2.3 | `button wake vad text event` / `typed tap test` |
| `OutputRequest` | 02 §2.1 | `request_id origin kind priority generation turn_id interruptible on_interrupt dedupe_key supersedes context_policy content` |
| `Origin` / `Kind` / `ContentRole` | 02 §2.2、06 §2.3 | 十个 origin；`cue speech narration`；`filler answer progress error` |
| `ToolSpec` | 03 §2.1 | 注册表全部字段；加载时校验 |
| `ToolCall` | 03 §2.5 | `call_id tool turn_id generation route settled closed_by interrupt_phase` |
| `ClosedBy` | 03 §2.6 | 六值，不得增删 |
| `Capabilities` | 04 §2.2 | 每字段 `{value, evidence, refs}`；`unknown` 取保守值 |
| `Ack` / `UpstreamEvent` / `UpstreamError` | 04 §2.3–2.5 | 原语返回值、事件信封、错误四分类 |
| `ContextEntry` | 05 §2.2 | `text_generated` 与 `text_presented` 分开存 |
| `StateBlock` / `Persona` | 05 §2.3–2.4 | `schema_version state_rev`；分节 |
| `TurnTrace` | 06 §2.5 | JSON Schema 同仓；收集器按 `(session_id, turn_id)` 合并 |

### 4.2 Upstream 接口（规范 04 §2.3 九个原语）

```python
class Upstream(Protocol):
    capabilities: Capabilities
    async def open(self, init: SessionInit) -> Ack
    async def send_audio(self, frame: bytes, meta: AudioMeta) -> Ack
    async def commit_turn(self, turn_id: TurnId, mode: Literal["commit","discard"]) -> Ack
    async def request_response(self, turn_id: TurnId, response_ref: str, overrides: ResponseOverrides | None) -> Ack
    async def cancel_response(self, response_id: str) -> Ack
    async def truncate(self, item_id: str, audio_end_ms: int, played_text: str | None) -> Ack
    async def send_tool_result(self, call_id: str, result: dict, reply_required: bool) -> Ack
    async def update_config(self, patch: ConfigPatch, mode: Literal["live","append","next_session"]) -> Ack
    async def close(self, reason: str) -> Ack
    def events(self) -> AsyncIterator[UpstreamEvent]
```

级联 / 半级联 / 全双工是 `Upstream` 的**组合实现**：`CascadeUpstream(stt: STT, llm: LLM, tts: TTS)` 内部把三个 provider 接口组合成九个原语。provider 接口（`STT` / `LLM` / `TTS` / `RealtimeS2S` / `FullDuplexModel`）在 `vak/upstream/providers/base.py`，是开发者扩展 provider 时实现的东西。

### 4.3 模块间端口（`vak/core/ports.py`）

| 端口 | 提供方 | 调用方 | 主要方法 |
|---|---|---|---|
| `ArbiterPort` | arbiter | turn、tools、context、session | `submit(OutputRequest)`、`on_turn_start`、`on_barge_in`、`on_receipt`、`flush(scope)` |
| `ContextPort` | context | arbiter、tools、upstream 适配 | `record_user`、`record_assistant_presented(played_ms)`、`project_history(caps)`、`state_block()` |
| `ToolPort` | tools | upstream 事件、session | `on_tool_call(event)`、`on_barge_in(gen)`、`on_session_rebuilt()` |
| `TracePort` | trace | 所有模块 | `write(turn_id, fragment: dict)`，各组件只写自己的字段 |
| `UpstreamPort` | upstream | turn、arbiter、tools、context | 即 `Upstream` 接口 |
| `TransportPort` | transport | turn、arbiter | `send_json`、`send_audio(frame, turn_id, sentence_id, gen)`、`flush(turn_ids)` |

### 4.4 一个连接内的对象图

```
Gateway ──1:N──► Session(conn)
                  ├── Transport (va/1 WS)
                  ├── TurnMachine (01)        ── 读 Capabilities 决定 commit / 判停策略
                  ├── Arbiter (02)             ── 唯一能向 Transport 写音频的对象
                  ├── ToolLayer (03)           ── 持有 Registry；通过 Arbiter 出声，通过 Upstream 回传
                  ├── ContextStore (05)        ── 本地真相；SessionRebuilder 是会话级单例
                  ├── TraceCollector (06)
                  └── Upstream (04)            ── 由 agent.yaml 的 pipeline + providers 构造
```

内部事件用 `vak/core/bus.py` 的进程内异步总线，事件名与规范一致（`turn.start`、`user.barge_in`、`turn.endpointing`、`turn.awaiting_tool`、`turn.reply_done`、`turn.audio_drained`、`turn.no_output`）。

## 5. 仓库布局

```
voice-agent-kit/
  pyproject.toml                 # 包名 voice-agent-kit，import 名 vak，Python ≥ 3.11
  vak/
    __init__.py                  # 公开 API：Agent, tool, Persona, Profile, serve
    agent.py                     # Agent：读 agent.yaml，组装 Session 工厂
    core/   types.py ports.py bus.py clock.py session.py gateway.py
            turn/ arbiter/ tools/ context/ trace/
    upstream/ base.py capabilities.py mirror.py events.py
              pipelines/ cascade.py half_cascade.py s2s.py full_duplex.py
              providers/ base.py mock.py openai_realtime.py dashscope_realtime.py
                         stt/ llm/ tts/ moshi.py
              capabilities/ *.yaml      # 每个 provider 一份，字段全、未探针的写 unknown
    transport/ base.py websocket.py protocol.py framing.py pacer.py
    audio/ vad.py endpointing.py resample.py silence.py codec.py
    cue/ library.py
    eval/ l0.py l1.py rubric.py trace_check.py
    probes/ p0.py
    cli.py                       # vak new | run | probe | trace | eval
  templates/agent/               # `vak new` 生成的项目模板
  examples/
    ptt_cascade_zh/              # 按键 · 级联 · FunASR + OpenAI 兼容 LLM + CosyVoice
    cloud_cascade/               # 开放麦 · 级联 · 云 STT/TTS + OpenAI 兼容 LLM（最简）
    open_mic_s2s/                # 开放麦 · S2S · OpenAI Realtime / DashScope 切换
    half_cascade/                # 半级联 · OpenAI Realtime 文本模态 + 本地 TTS
    full_duplex_moshi/           # 原生全双工 · moshi
  clients/web/                   # 浏览器参考客户端：ptt + open_mic，AEC 三项约束全开
  schemas/ trace.schema.json capabilities.schema.json tool_registry.schema.json
  tests/ unit/ integration/ conformance/   # conformance = 规范验收清单可自动化的部分
  docs/ architecture.md（本文） getting-started.md providers.md protocol.md
```

## 6. 开发者 API 示意

```python
# my_agent/tools.py
from vak import tool

@tool(presentation="verbatim", latency_class="ms", write=False,
      voice_template="现在是 {hour} 点 {minute} 分。", phases="global",
      interrupt_policy="cancel", on_duplicate="replace", timeout_ms=1000)
async def get_time() -> dict:
    now = datetime.now()
    return {"ok": True, "summary": "报了当前时间", "data": {"hour": now.hour, "minute": now.minute}}

@tool(presentation="verbatim", latency_class="seconds", write=True,
      idempotency_key=["option_id"], gates=[...], phases=["confirm"],
      interrupt_policy="finish_then_notify", on_duplicate="reject", timeout_ms=15000,
      progress={"ack_template": "好，已经提交，正在处理。"})
async def submit_request(option_id: str) -> dict: ...
```

```yaml
# my_agent/agent.yaml
profile: A            # A 无屏设备 / B 有屏 App
mode: ptt             # ptt / open_mic
pipeline: cascade     # cascade / half_cascade / s2s / full_duplex
providers:
  stt:  { type: funasr,        url: ws://localhost:10095 }
  llm:  { type: openai_compat, base_url: ${LLM_BASE_URL}, model: ${LLM_MODEL} }
  tts:  { type: cosyvoice,     url: http://localhost:50000, voice: default }
persona: persona.yaml
state_schema: state_schema.yaml
tools: my_agent.tools
cues: cues/            # 预合成 cue 音频目录
```

```bash
vak new my_agent --pipeline cascade --profile A     # 生成上面这套文件
vak run my_agent/agent.yaml                          # 起网关，默认 ws://0.0.0.0:8765
vak probe openai_realtime --items P0-1,P0-2          # 跑探针，写回能力声明
vak trace summarize traces/*.jsonl                   # 两种首音 P50 / P90，按 turn_kind 分组
vak eval l0 cases/*.jsonl                            # 进程内决策评测
```

## 7. 对规范的实现约束（给实现者）

照各规范"产出骨架时的注意事项"一节执行，另加以下脚手架层面的约束：

1. **状态机都是显式枚举 + 转移表**，非法转移抛 `IllegalTransition`。回合状态机、仲裁器状态、请求生命周期、调用生命周期四个都这样写。
2. **代际在三处比较**：入队、授权、每帧发出。`TransportPort.send_audio` 的签名强制带 `generation`，Transport 自己也维护 `current_generation` 做二次过滤并计数。
3. **链路无关层禁词**：`vak/core/` 和 `vak/transport/` 下不得出现 `openai`、`dashscope`、`qwen`、`moshi`、`funasr`、`cosyvoice` 等任何厂商或模型名。CI 用 grep 检查。
4. **适配器禁词**：`vak/upstream/` 下不得出现 `retry`、`reconnect`、`sleep(` 开头的重试循环。重建预算只在 `SessionRebuilder` 里。
5. **原语不"尽量做"**：每个原语的降级表写成显式分支，末尾兜底 `Ack(ok=False, code="unsupported", detail="<字段名>")`。
6. **trace 字段只由定义方写**：`TracePort.write(turn_id, {...})` 的 key 必须在 `schemas/trace.schema.json` 里；收集器拒绝未知 key。
7. **音频编码可协商**：规范 01 以 Opus 为准，脚手架 `hello` 另接受 `pcm16`，便于没有 libopus 的开发环境跑通；默认模板用 Opus（`opuslib`）。帧头布局不变。
8. **所有"建议值"集中在 `vak/core/defaults.py`**，带规范出处注释；不散落在各模块。
9. **测试替身是一等公民**：`providers/mock.py` 能按脚本产生事件（含 cancel 后迟到 delta、工具调用、文本模态翻转），规范验收清单里能自动化的项全部在 `tests/conformance/` 中对它运行。
10. **测试先于 provider**：真实 provider 的接入以通过 `tests/conformance/` 的共用行为测试为完成标准（规范 04 §7 第 5 条）。

## 8. v1 范围与里程碑

| 里程碑 | 交付 | 完成标准 |
|---|---|---|
| **M1 内核** | `vak.core` 五个模块 + `vak.transport` + `vak.audio` 基础 + 测试替身上游 + conformance 测试 | 规范 01/02/03 验收清单里不依赖真实上游和真实设备的项全部自动化并通过；跑通按键和开放麦两种模式的完整回合、打断、工具 A1/A3、断线续接 |
| **M2 四条链路** | `pipelines/` 四个 + providers：openai_realtime（S2S 与半级联文本模态）、dashscope_realtime、funasr、cosyvoice、openai_compat_llm、openai_stt / openai_tts、moshi；每个 provider 一份能力声明；P0 探针 harness | 每个 provider 过 conformance；能力声明关键字段至少 `code_read`；`examples/` 五个示例能跑 |
| **M3 开发者面** | `vak new` 模板、`clients/web` 参考客户端、`vak trace` / `vak eval`、getting-started 文档 | 新开发者按 getting-started 在 30 分钟内于本地跑通，再换一个真实 provider 跑通 |

**状态（v0.1）**：M1 完成；M2 代码完成，provider 在 M2 列表之外还接了流式 STT（火山、百炼、OpenAI Realtime 转写、Deepgram、AssemblyAI）、方舟 / 百炼 LLM、火山 / 百炼流式 TTS，但真实 provider 都只有协议级单测，未对真实上游跑 conformance 和探针；M3 完成（`vak new/run/probe/trace/eval`、浏览器客户端、getting-started）。`vak eval l1` 目前只校验脚本格式，跑脚本要自己绑 `SessionDriver`（`tests/integration/harness.py` 是现成的 loopback 版本）。

v1 不做：WebRTC 传输（协议层已为其留位，见规范 01 §7）、有屏 profile 的 UI 同步规范 07、后台委派 A2 的后台 agent 本体（接口和协议留好，提供一个最简文本 LLM 后台实现）、硬件客户端固件。

## 9. 需实测的点

脚手架本身不替用户回答这些，但 `vak probe` 把它们做成可执行项，结果写回能力声明：

- 各 S2S 上游服务端判停能否关（P0-2）：决定按键设备能否用 `s2s`。
- 上游回传工具结果后有无静默收口（P0-1）：决定 A1 / A2 在 S2S 上成不成立。
- cancel 后是否推迟到 delta（P0-8 的一部分）：决定迟到帧过滤是否只是保险。
- 自托管 TTS 单并发首块 P50（规范 06 口径）：决定自托管级联首音预算。

## 10. 与现有文档的关系

- 规范 00–06 是契约，本文是实现蓝图。规范改了，本文和代码一起改；本文不复述规范条文。
- 手册 [`02-architectures/`](https://github.com/wananing/voice-agent-handbook/tree/master/02-architectures) 四页是四种链路的取舍依据，本文 §3 只讲它们怎么落到同一个接口。
- 规范和代码注释里的出处简写（KM、HC、`notes/…`、`handbook <主题>`）按 [`docs/specs/README.md`](specs/README.md#出处简写对照) 的对照表查手册。

## 11. 集成时定下的补充约定

各模块并行实现后在 `Session` 里接起来时补的几条，和规范不冲突，但规范没写：

1. **代理对象。** 模块持有 `UpstreamProxy` / `TransportProxy`（`vak/core/proxy.py`），会话重建换上游、断线重连换传输都只换代理的目标，模块引用不变。`UpstreamProxy.events()` 把前后几个真实上游的事件流接成一条。
2. **文本回合有自己的原语。** `Upstream.send_text(turn_id, text)`（级联 / 半级联追加 user 消息，S2S 走 `RealtimeS2S.add_user_text`，全双工 `unsupported(uplink.text_input)`）。回合状态机对 `turn.start{source:"text"}` 调它，不调 `commit_turn`。
3. **工具阶段结束的信号。** 工具层每次收口后下一 tick 发总线事件 `tool.call_closed{turn_id, call_id, closed_by}`；回合状态机在 `awaiting_tool` 且该回合无在途调用、也没有已回传且 `reply_required` 的调用时，结束回复（A1 / A3 / silent / 打断路线）。A0 仍由再生成的 `response.started` 结束。
4. **abort 紧跟 turn.start 只 +1 一次。** 仲裁器记住最近一次 barge-in 的代际，紧接着的 `on_turn_start` 若期间没有授权过任何输出就不再 +1（规范 02 §2.5）。
5. **`tts.stop{done}` 在最后一帧之后。** 仲裁器发 `tts.stop{done}` 前等传输的 pacer 排空（`BaseTransport.wait_sent`），设备可以把它当"该句音频已全部到达"。
6. **句文本进 `tts.sentence_start`。** 回合状态机把 `response.sentence` / `transcript.assistant` 的文本交给 `Arbiter.note_item_text`，仲裁器为该 item 的句子填 `text`。
7. **trace 基线。** 会话在 `turn.start` 时写一份必填字段的基线（计数器 0、`rebuild_reason: none`、`clock_quality: gateway_rx` 等），组件随后覆盖自己测到的值；`turn.closed` 时补 `user_text`、`consumer_dropped_frames`、`usage.played_audio_ms`。收集器对 `tool_calls[]` 按 `call_id` 取最后一条记录。
8. **不能调工具的上游不给工具表。** `tools.calling == none`（原生全双工）时 `SessionInit.tools` 为空，重建器同样。
9. **按键模式下 S2S 建会话带 `turn_detection: None`。** `SessionInit.turn_detection` 由会话按模式填，S2S 适配器透传给 provider 的 connect 配置（P0-2 关不掉时由探针结果决定对策）。
10. **级联 / 半级联的 TTS 预取一句。** `TextOutPipeline` 在发第 *i* 句音频的同时合成第 *i*+1 句（`upstream.tts_lookahead`，默认 1），事件和音频仍按句序发出；打断时所有在途合成都 `flush`。前一句的 TTS 合成不再挡住后一句的首包，也不再挡住读 LLM 流。护栏 `block` 时已放行的句子照常播完。
11. **回合开始即预连 LLM 和 TTS。** 级联在本回合第一帧音频到达时调用 provider 的可选 `warmup()`（OpenAI 兼容 LLM 和 OpenAI TTS 是 `GET /models`），`upstream.warmup_interval_ms` 内最多一次；HTTP provider 的空闲连接保留 60 s（httpx 默认只有 5 s）。目的是让松键后的第一个请求不付 TCP + TLS。
12. **TTS 接受增量文本时，一轮回复一个文本流。** provider 声明 `supports_text_stream=True` 并实现 `open_text_stream(request_id) -> TTSTextStream`（`push` / `end` / `audio()` / `close`，`vak/upstream/providers/base.py`）时，`TextOutPipeline` 在回复开始就打开文本流，握手和 LLM 首 token 重叠。没有护栏时，LLM 的每个文本增量原样 `push`，由引擎自己切句；有护栏时，改为推送护栏放行的整句，规范 04 §6.4 仍然成立。对外仍按句：引擎报告的每个句子（`TTSChunk.sentence`）发一个 `response.sentence`，其音频带这个句序号，截断和打断照旧。引擎不报句界时（火山 `seed-tts-2.0`）整轮回复算一句。打断时 `flush("<response_id>:stream")`。这种模式下不做 TTS 预取（第 10 条），由引擎自己流水。目前只有 `volcengine_tts` 走这条路。
13. **trace 分段与回合类型。** 回合状态机根据本回合首次回复请求的发起时刻，以及上游事件自带的时间戳（`UpstreamEvent.t_local_ms`，句事件另带 `submitted_at_ms`），写入规范 06 §2.4 的 `latency.seg.*`；适配层本身不持有 trace 端口。会话在 `turn.start` 时写入基线 `turn_kind`（文本回合为 `ui_input`，事件回合为 `event`，其余为 `chat`），工具回合由工具层覆盖。按键模式下网关没有本地 VAD，此时 `anchors.audio_end_at_ms` 退化为松键时刻，`ptt_tail_silence_ms` 记为 0。`latency.pre_tool_ms` 由收集器统一换算为相对 T0 的值。

