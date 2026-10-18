# 工具注册表与函数调用收口规范

> **开源说明**：本文是 voice-agent-kit 的契约规范之一，正文保留设计期原貌。代码注释里的"spec 0X §y"指本目录的规范。文中的出处简写（架构 / 两类架构、KM、BP、总结、HC、PC-B、LK-B、UM、`notes/*.md` 等）和 `../../knowledge-map.md` 一类路径指向作者未公开的调研笔记，对应内容已整理进 [voice-agent-handbook](https://github.com/wananing/voice-agent-handbook)，按 [README 的对照表](README.md#出处简写对照) 查。

> 状态：草案 v0.1（2026-09-29）。适用于 A 类（无界面硬件）和 B 类（有界面移动端）两个 profile。
> 定位：`../voice-agent-two-architectures.md` §4 把"工具注册表 schema、`closed_by` 六个终态"列为**不允许分叉的契约**。本文就是这份契约的展开：两个团队可以各自实现工具执行器，但注册表字段、分流规则、终态语义、回传格式必须按本文。
> 抛开具体业务，文中工具名都是示意。

**引用约定**

| 简称 | 指 |
|---|---|
| 架构 §x | `docs/design/voice-agent-two-architectures.md` |
| KM x.y | `docs/knowledge-map.md` |
| BP A.x / A-Ex | `docs/bottleneck-proposals.md` 的方案 / 实验 |
| 总结 §x | `realtime-voice-agent-lessons-2026-09.md`（项目实测和现有实现） |
| LK / PC / ORA / QW / FE / XZ | `docs/notes/` 下 livekit-agents / pipecat / openai-realtime-agents / qwen-audio-agent / flows-and-evals / xiaozhi 笔记 |
| 00 / 01 / 02 / 04 / 05 / 06 | 同目录 `specs/` 下的术语总表、回合与客户端协议、输出仲裁、链路适配、上下文与会话、trace 与评测规范（06 是 trace 字段权威表） |

**标记约定**：**【实测】** 是我们自己测过的；**【仓库】** 是读代码得出、未跑；**【推断】** 来自笔记里的推断；**【建议值，需实测】** 是本文给的初值，上线前要用 BP A.4 的实验定。没有标记的条目是规范性要求（MUST）。

---

## 1. 目的、范围与术语

### 1.1 目的

1. **每个函数调用都有终态，不悬挂。** 悬挂的调用会让模型在后续几轮反复调同一个工具，闲聊时约 21%，打断后更高【实测，总结 §5.2】。
2. **收口和再生成分开决定。** 结果回传给模型是为了收口；要不要让模型再说一遍，由工具的"呈现方式 + 执行时长 + 是否写操作"决定（架构 §1 原则 1；KM 2.8 原则 2）。省掉"回传 → 再生成"这一段约 1.2 s【实测，BP A.0】是工具回合提速的主要来源。
3. **执行权在我们。** 模型只提议；写操作的门槛、阶段可见性、取消与去重由工具层执行，不交给前台或后台模型（架构 §1 末尾"两条我们总结里有的原则"（决策分层）；KM 3.6）。
4. **事实走通道。** 工具结果按呈现方式到达用户；做不到时替代方案来自数据、随结果下发（架构 §1 末尾"两条我们总结里有的原则"（事实走通道）；KM 3.7）。

### 1.2 范围

- 包含：工具注册表字段；工具回合分流（A0–A3）；调用生命周期和六个终态；打断处理；前台到后台的委派协议；结果回到对话的格式；决策分层与门槛的接口和顺序；A / B 两个 profile 的差异；验收指标。
- 不包含：回合状态机与看门狗本身（01）、输出仲裁器的内部实现（02）、会话重建器（05）、trace 字段（06 是权威表；本文需要的工具字段已并入 06 §3.5，见 §10.1）。本文引用它们的接口语义。

### 1.3 术语

| 术语 | 定义 |
|---|---|
| **呈现方式**（presentation） | 工具结果怎么到达用户，是注册表属性。四个取值：`verbatim` 直念（按模板渲染，由本地 TTS 或预合成念出，不经模型组织）；`model` 模型组织（需要组织语言，由文本 LLM 生成正文）；`screen` 上屏（结构化内容显示在屏幕上，语音只说要点，仅 B 类）；`silent` 只记（写进上下文和状态，不出声）。对应小智的 RESPONSE / REQLLM / RECORD（XZ §2.4），`screen` 是 B 类新增 |
| **收口**（closure） | 让一个调用在上游会话和本地登记表里都进入终态：向模型回传一条 function output（或按规则明确不回传），并在本地置 `settled`。收口是防重复调用的必要条件 |
| **再生成**（regeneration） | 回传后请求模型再生成一轮回复（S2S 上即发 `response.create`，级联上即再跑一次 LLM）。收口不蕴含再生成 |
| **终态**（`closed_by`） | 调用关闭的原因，六个值：`result` / `interrupted` / `cancelled` / `progress` / `superseded` / `duplicate`（KM 2.12，全文统一，不得增删）。语义以本文 §4.2 为准，trace 记法见 06 §3.5。与回合终态（01 §2.4 五值）是两套，不混用 |
| **门槛**（gate） | 写操作执行前必须依次通过的检查链：意图 → 原话校验 → 后端状态（总结 §3.1） |
| **委派**（delegation） | 前台（S2S 或前台 LLM）把"需要组织语言或多步"的工作交给后台文本 LLM；后台流式产出正文，由本地 TTS 出声（BP A2） |
| **进度**（progress） | 秒级 / 后台工具启动后立即回传的"已提交、进行中"结果；首条进度即作为原调用的 output 收口（LK Q3 `ctx.update()`） |
| **占位**（placeholder） | 调用在途时，向模型上下文注入"仍在进行中"的假 output，告诉模型别再调。与收口互补：收口是事后兜底，占位是在跑时预防（KM 3.5）。仅级联可用，S2S 待探针 |
| **代际**（generation） | 每次打断或新回合 +1 的整数（另见 02 §3.2 的 critical 抢占）；输出和结果都带代际，低代际一律丢弃（KM 3.3；01 §1.3） |
| **`settled` 位** | 调用对象上的终态位，一旦置位，后到的结果、进度、取消一律拒绝（PC，KM 2.8 原则 1） |
| **路线 A0–A3** | 工具回合的四种出声路线，见 §3。trace 字段名 `tool_route`（06 §3.5），不要与 B 类选路 `route`（s2s / cascade / half_cascade，06 §3.2）混淆 |

---

## 2. 工具注册表 schema

### 2.1 字段

每个工具一条注册项。注册表在会话开始时整体下发给模型（S2S 改工具表要重连或触发重新预填，KM 3.4），**按阶段、按状态的限制在工具层执行**，不改上游工具表。

| 字段 | 类型 / 取值 | 必填 | 说明与出处 |
|---|---|---|---|
| `name` | 字符串，snake_case | 是 | 全局唯一；进 trace |
| `description` | 字符串 | 是 | 给模型看的用途说明；不写事实（架构 §1 末尾"两条我们总结里有的原则"（事实走通道）） |
| `params` | JSON Schema | 是 | 参数 schema；实体类参数标 `entity: true`，由工具层做实体解析，模型给出的实体与用户原话不一致时以原话为准（总结 §4.1） |
| `presentation` | `verbatim` / `model` / `screen` / `silent` | 是 | 见 §1.3。`screen` 必须同时给 `voice_template`（语音要点），A profile 不接受 `screen` |
| `voice_template` | 模板字符串或模板 id | `verbatim`、`screen` 必填 | 直念话术；A 类一次一个问题、最多两个选项（架构 §2.4） |
| `latency_class` | `ms` / `seconds` / `background` | 是 | `ms`：P90 ≤ 200 ms；`seconds`：P90 ≤ 15 s；`background`：可能超过 15 s 或需要多步【建议值，需实测】。总结里状态类工具 1–50 ms【实测，BP A.0】；15 s 取自总结 §3.1 规划等待的退路时限 |
| `cue` | `long` / `short` / `none` | 否，默认按路线：A1（`verbatim` + `ms`，含 A1-屏）为 `short`，A2 / A3 为 `long` | 工具回合开始（收到 function call）时由工具层选择并入队的提示语。`long`：预合成整句（如"我查一下路线。"，约 0.7–0.8 s）；`short`：≤ 300 ms 的语气词或提示音【建议值，需实测】；`none`：不播。**工具在 cue 开播（授权）前已完成、结果已入队时不播 cue**，结果直接出声，未授权的 cue 由工具层按 02 §3.5 定向冲刷撤回。cue 进仲裁器后仍按 02 §2.4、§3.5"不冲刷、不被同回合结果抢占"处理；cue 的长短由工具层选择，仲裁规则不变（review B-1）。取值由 A-E2 的长 cue / 短 cue / 无 cue 三组对照校准 |
| `write` | 布尔 | 是 | 是否有副作用。`true` 时 `gates` 必填、`idempotency_key` 必填 |
| `idempotency_key` | 参数名列表 | `write=true` 必填 | 用于 `duplicate` 判定和后端去重 |
| `gates` | 有序列表，元素见 §8.4 | `write=true` 必填 | 门槛链：`intent` → `utterance` → `backend_state`（总结 §3.1） |
| `phases` | `global` 或阶段名列表 | 是 | 哪些阶段对模型可见（事前收窄，FE §1.4）。`global` 表示任何阶段可用 |
| `visible_after` | 工具名列表 | 否 | 前置调用成功后才可见（LK `AgentTask` 的确认工具只在前置步骤后注入，FE §1.4） |
| `interrupt_policy` | `cancel` / `continue` / `finish_then_notify` | 是 | 被打断时：取消 / 继续在后台跑（异步，PC `cancel_on_interruption=False`）/ 已开始执行的写操作跑完、下次空闲补一句（总结 §5.3）。`write=true` 只能取后两者 |
| `cancellable_by_user` | 布尔 | 否，默认 `latency_class=background` 时 `true` | 用户可说"算了"取消；对应 LK `ToolFlag.CANCELLABLE`、PC `cancellable_by_llm`（LK Q3、PC Q3） |
| `on_duplicate` | `allow` / `reject` / `replace` / `confirm` | 是 | 重复策略（LK Q3）。`write=true` 默认 `reject` |
| `duplicate_scope` | `turn` / `in_flight` / `session` | 否，默认 `turn` | 在什么范围内算重复；写操作同一用户回合只认第一个调用（QW §2.1） |
| `timeout_ms` | 整数 | 是 | 超时后 `closed_by=cancelled`、`reason=timeout`，并按 §7.3 出声。初值：`ms` 1000、`seconds` 15000、`background` 由 `progress` 负责，最终结果 600000【建议值，需实测】；小智统一 30 s（XZ §2.4），PC 超时取消后让模型告知用户（PC Q2） |
| `progress` | 对象 | `seconds`/`background` 必填 | `ack_template`（进度收口时回传和念的承接句）、`throttle`（首条 ≥ 60 s、全局间隔 ≥ 60 s、新覆盖旧，QW 参数，KM 2.8）；B 类可另设屏幕进度 |
| `result_schema` | JSON Schema | 是 | 必须包含 §2.3 的公共字段 |
| `profile_overrides` | `{A: {...}, B: {...}}` | 否 | profile 差异只能覆盖 `presentation`、`voice_template`、`phases`、`timeout_ms`、`progress`，不能覆盖 `write`、`gates` |

**校验规则（注册时拒绝）**：`write=true` 且无 `gates`；`presentation=screen` 且无 `voice_template`；`write=true` 且 `interrupt_policy=cancel`；`latency_class≠ms` 且无 `progress`；A profile 下出现 `screen`。

### 2.2 示例（YAML，示意）

```yaml
registry_version: "2026-09-29.1"      # 进 trace 的 prompt_version（06 §3.2）
tools:
  - name: get_status                   # 查状态：逐字念、毫秒级、只读 → A1
    description: 查询当前任务的进度和剩余项。
    params: { type: object, properties: {}, additionalProperties: false }
    presentation: verbatim
    voice_template: status.v3          # "已经完成 {done} 个，还剩 {left} 个。"
    latency_class: ms
    write: false
    phases: global
    interrupt_policy: cancel
    on_duplicate: replace
    timeout_ms: 1000
    result_schema: { $ref: "#/common/result" }

  - name: explain_topic                # 讲解：需要组织语言 → A2 委派
    description: 讲解用户问到的对象，分层讲，先讲要点。
    params:
      type: object
      properties:
        target: { type: string, entity: true }
        depth:  { type: string, enum: [brief, more] }
      required: [target]
    presentation: model
    latency_class: seconds
    write: false
    phases: global
    interrupt_policy: cancel
    cancellable_by_user: true
    on_duplicate: replace
    timeout_ms: 8000
    progress: { ack_template: ack.lookup.v1 }
    result_schema: { $ref: "#/common/result" }

  - name: submit_request               # 写操作：调后端、秒级 → A3
    description: 提交用户已确认的请求。
    params:
      type: object
      properties: { option_id: { type: string, entity: true } }
      required: [option_id]
    presentation: verbatim
    voice_template: submit.done.v2
    latency_class: seconds
    write: true
    idempotency_key: [option_id]
    gates:
      - { kind: intent, allowed_actions: [confirm_submit] }
      - kind: utterance
        must_match: [开始, 提交, 确认]
        must_not_match: [怎么提交, 提交什么]
      - { kind: backend_state, check: no_active_request_and_option_selected }
    phases: [confirm]
    visible_after: [show_options]
    interrupt_policy: finish_then_notify
    on_duplicate: reject
    duplicate_scope: turn
    timeout_ms: 15000
    progress:
      ack_template: submit.ack.v1      # "好，已经提交，正在处理。"
      throttle: { first_after_s: 60, min_interval_s: 60, replace_older: true }
    result_schema: { $ref: "#/common/result" }

  - name: show_options                 # 上屏：B 类
    description: 在屏幕上列出可选方案。
    params: { type: object, properties: { query: { type: string } } }
    presentation: screen
    voice_template: options.voice.v1   # "给你列了三个方案，看屏幕，也可以直接说第几个。"
    latency_class: seconds
    write: false
    phases: [analyze, propose]
    interrupt_policy: cancel
    on_duplicate: replace
    timeout_ms: 8000
    progress: { ack_template: ack.lookup.v1 }
    profile_overrides:
      A: { presentation: verbatim, voice_template: options.voice_top2.v1 }  # A 类只念前两个
    result_schema: { $ref: "#/common/result" }

  - name: remember_preference          # 只记
    description: 记下用户表达的偏好。
    params: { type: object, properties: { key: { type: string }, value: { type: string } } }
    presentation: silent
    latency_class: ms
    write: false                       # 只写会话状态，不写业务后端
    phases: global
    interrupt_policy: cancel
    on_duplicate: replace
    timeout_ms: 1000
    result_schema: { $ref: "#/common/result" }
```

### 2.3 结果公共字段（JSON，示意）

每个工具的 `result_schema` 必须包含以下字段；工具可在 `data` 里放自己的结构化结果。

```json
{
  "ok": false,
  "reason_code": "not_supported",
  "summary": "给模型看的一句话结果摘要，不含 ID、URL",
  "data": { },
  "display": { "card_id": "c-17" },
  "alternatives": [
    { "label": "换成方案二", "action": "select_option", "params": { "option_id": "o2" } },
    { "label": "只看不改",   "action": "keep_current",  "params": { } }
  ],
  "escalation": { "label": "转人工", "action": "transfer_to_human" }
}
```

- `ok=false` 时 `reason_code` 必填；`alternatives` 最多两项，来自数据而不是模型临场发挥（FE §5 模式 1、2；pipecat-flows 餐厅示例的 `alternative_times`）。
- `escalation` 在 B profile 下对高风险阶段必填（架构 §3.4 原则 6）；A profile 可省略，或给"换个说法 / 稍后再试"这类出口。
- `display` 只在 `screen` 呈现时有；A profile 忽略。

---

## 3. 分流规则：A0 / A1 / A2 / A3

### 3.1 四条路线

| 路线 | 做法 | 预期有内容首音 | 出处 |
|---|---|---|---|
| **A1 收口直念** | 执行工具 → 按 `voice_template` 渲染 → 本地 TTS / 预合成，排在短提示语（`cue: short`，§2.1）之后播（仲裁器 speech 类，受代际约束；工具在 cue 开播前已完成则不播 cue）→ 同时回传 output（§7.1），**不再生成**。`voice_template` 的固定前缀预合成，藏住本地 TTS 首包（约 245 ms） | 有声音 ≤ 0.9 s（短 cue 约 0.83 s 开播）；有内容 ≈ 1.1 s（0.83 s + 短 cue ≤ 0.3 s，估算）；A-E2 门槛 P50 ≤ 1.1 s | BP A1；LK Q2 `ToolResult(reply_required=False)` |
| **A2 委派后台流式** | 前台只调委派工具 → 后台文本 LLM 带只读工具流式出正文 → 首句切出交本地 TTS → output 回传**实际播出的文本**，不再生成；识别定稿即投机起跑（§6） | 不投机 1.4–1.8 s，投机 1.0–1.3 s（估算）；A-E4 门槛 P50 ≤ 1.3 s、P90 ≤ 2.0 s | BP A2；PC Q6 commentary / thinking |
| **A3 进度即收口** | 工具一启动就回传进度并收口（`closed_by=progress`），前台说一句有信息量的承接；最终结果作 narration 等空闲窗口播报 | 从"工具耗时 + 1.2 s"降到 1.2–2.0 s（估算） | BP A3；LK Q3 `ctx.update()`；PC Q3 async |
| **A0 回传再生成（兜底）** | 回传结果并请求模型再生成。只在 A1/A2 前提不成立时使用 | 约 2.1 s（S2S 实测） | 总结 §3.4；KM 1.1 |

A0 的使用条件：上游探针 A-E0 证明"回传不发 `response.create` 时上游会自己开口且无静默选项"（例如 GPT-Live 在 LK 插件里做不到静默，LK Q2）；或低风险闲聊类工具、产品明确接受 A0 延迟。A0 不是默认路线。

### 3.2 决策表

输入是注册表的 `presentation`、`latency_class`、`write` 三个字段（KM 1.1："分流依据不只是呈现方式，还要看执行时长和是否写操作"）。路线在注册时静态算出、进 trace；运行时只有 §3.3 的两种情况会改路线。

| # | presentation | latency_class | write | 路线 | 说明 |
|---|---|---|---|---|---|
| 1 | verbatim | ms | 否 | **A1** | 典型：状态、进度、位置 |
| 2 | verbatim | ms | 是 | **A1 + 门槛** | 门槛通过后执行，结果直念；执行前再查代际（§5.2） |
| 3 | verbatim | seconds | 否 | **A3** | 先念承接，结果作 narration |
| 4 | verbatim | seconds | 是 | **A3 + 门槛** | 典型：调后端的提交类写操作。总结里 `start_task` 呈现方式是逐字念，但要调后端、有副作用，归 A3（BP A.3） |
| 5 | verbatim / model / screen | background | 任意 | **A3** | 长任务；进度按 `progress.throttle` 节流 |
| 6 | model | ms / seconds | 否 | **A2** | 讲解、推荐、需要组织语言的查询 |
| 7 | model | 任意 | 是 | **拆分**：写操作按 #2 / #4 执行并收口，讲解部分另起 A2 | **写操作不委派给后台模型执行**；规则短路和门槛留在委派之前（BP A2 约束） |
| 8 | screen | ms | 否 | **A1-屏**：上屏 + 直念 `voice_template` | B 类；卡片与语音按句对齐推送（架构 §3.2） |
| 9 | screen | seconds | 任意 | **A3-屏**：屏幕先显示"正在查 + 已知信息"，承接句直念，结果上屏后念要点 | B 类工具回合 ≤ 2 s 有内容（架构 §3.3） |
| 10 | silent | 任意 | 否 | **静默收口**：回传结果，不再生成、不出声 | 对应小智 RECORD（XZ §2.4） |
| 11 | silent | 任意 | 是 | 禁止 | 有副作用的操作必须让用户知道（A 类"所有状态必须可听"，架构 §2.4） |

### 3.3 运行时改路线

1. **上游能力不足**：能力声明（04 §2，字段 `tools.auto_reply_after_result`、`tools.silent_close`；由 BP §0 P0 探针产出）标明当前上游不支持静默回传时，A1 / A2 退到 A0，或改走半级联（架构 §3.2）。改路线写 trace `route_fallback_reason`（06 §3.5）。
2. **高风险回合（B 类）**：分流规则把回合判为高风险时，`model` 呈现的正文必须走文本先于语音，并在进 TTS 前过出声前护栏（架构 §3.2）。A2 天然满足；A0 在 S2S 直出上不满足，因此高风险回合禁用 A0。

---

## 4. 调用生命周期状态机

### 4.1 状态

```
received ──(门槛/阶段检查)──▶ running ──▶ (progress)* ──▶ closed{closed_by}
    │                            │
    └────────── 任一状态可直接进入 closed（打断、取消、重复、过期）─────┘
```

| 状态 | 进入条件 | 本地登记表记录 |
|---|---|---|
| `received` | 收到模型的 function call（或委派请求）。立即登记 `call_id, tool, turn_id, generation, route, received_at` | 此刻起看门狗计时 `timeout_ms` |
| `running` | 通过阶段可见性、`on_duplicate`、门槛链（§8），开始执行 | `started_at` |
| `progress` | 执行中产出进度。**首条进度即收口**（`closed_by=progress`），之后的进度和最终结果不再挂在原 `call_id` 上，改作 narration 和上下文条目（LK Q3：后续用新 call id 回帖） | `progress_seq` |
| `closed` | 六个终态之一；置 `settled=true` | `closed_by, closed_at, reply_required, output_sent` |

**不变式**
1. 每个 `received` 的调用，在 `timeout_ms + 看门狗余量` 内必然进入 `closed`。余量初值 2 s【建议值，需实测】。
2. `settled` 置位后，任何结果、进度、取消都被拒绝，记 `late_result_dropped`（PC，KM 2.8）。
3. 迟到结果如果属于写操作且副作用已发生，不回传给模型，但要走 §5.2 的"补一句"。
4. 会话重建后重建登记表；重建前在途的调用由本地收口（`closed_by=superseded`），不向新会话回传（KM 3.4、3.5）。

### 4.2 六个终态

| `closed_by` | 触发条件 | 回传给模型的 output | 触发再生成？ |
|---|---|---|---|
| `result` | 工具正常完成（含 `ok=false` 的业务失败、门槛拒绝、阶段不可见），且回传时调用所在代际仍有效 | §7.1 的结果体：`summary` + `spoken_to_user` +（失败时）`alternatives` / `escalation` | 仅 A0 路线是；A1 / A2 / 静默收口否。门槛拒绝和阶段不可见也不再生成，由本地直念拒绝话术 |
| `interrupted` | 用户打断（含新回合打断旧回合，01 R2.1-1）时调用已完成（未回传，或已回传且模型 / 本地正在念） | 已回传：补 `{"interrupted": true}`（补极短结果 20/20 出声，不补 15/20【实测，总结 §5.3】）；是否附 `played_text` 由 A-E1 定（§7.1）。未回传：见 §5.1 行 3 | 否 |
| `cancelled` | 调用在跑时被取消：打断且 `interrupt_policy=cancel`；用户说"算了"；超时（`reason=timeout`）；委派被取消 | `{"cancelled": true, "reason": "interrupt" / "user" / "timeout"}` | 否。超时由本地直念兜底话术（§7.3）；A 类沉默即故障（架构 §2.4） |
| `progress` | `seconds` / `background` 工具启动后首条进度 | `{"status": "in_progress", "summary": "已提交，正在处理", "spoken_to_user": "<承接句>"}`，附"任务仍在进行，不要编造结果、不要再次调用"（LK `UPDATE_TEMPLATE` 同义） | 承接句由本地直念时否；A3 原设计允许前台再生成一句有信息量的承接（BP A3），此时是，且只一轮 |
| `superseded` | 会话被重建（§4.1 不变式 4），或被 `on_duplicate=replace` 的新调用替换。**不用于打断**：新回合打断旧回合、用户打断一律按 §5.1 记 `interrupted` / `cancelled`（review B-7-1） | 会话重建：不回传（旧上游会话已放弃）；被替换：`{"superseded": true}` | 否 |
| `duplicate` | `on_duplicate=reject/confirm` 命中；同一用户回合的第二个写操作调用；同一回合第二个委派（QW §2.1） | `reject`：`{"duplicate": true, "message": "同一请求已在处理或已完成，不要再次调用"}`；`confirm`：`{"duplicate": true, "needs_confirmation": true, "message": "<要向用户确认的问题>"}` | `reject` 否；`confirm` 是（让模型向用户确认），或由本地直念确认问题 |

### 4.3 在途占位

- **做法**：上一轮还在跑的调用，在下一轮推理前向模型上下文注入一对"调用 + 仍在进行中"的占位 output，推理完再剥掉；配合 `on_duplicate=reject`（LK Q3，`generation.py:66-106`【仓库】）。
- **适用范围**：**只在级联路径启用**。LK 只在级联路径注入占位；S2S 上"假 output"是否被接受、是否引发自动回复、之后能否再回传真实结果，待 BP §0 P0 探针。探针通过前，S2S 路径靠 A3 的"进度即收口"达到同样效果，不做占位。
- A3 路线的调用在首条进度时已经收口，不需要占位；占位主要用于 `latency_class=seconds` 但走 A1 / A2 的调用。

---

## 5. 打断矩阵

### 5.1 按调用所处阶段

打断指用户开口（开放麦 VAD / 按键 / 触控）或规则触发的高优先级输出。打断发生时仲裁器代际 +1（KM 2.5；02 §3.2），工具层按下表处理。trace 的 `interrupt_phase`（06 §3.5）取下表的阶段名。

| # | 调用所处阶段 | 只读工具 | 写操作 | `closed_by` | 依据 |
|---|---|---|---|---|---|
| 1 | `received`，门槛检查中、未执行 | 放弃执行 | 放弃执行，**不写后端** | `cancelled` | §5.2 |
| 2 | `running`，同步执行中 | `interrupt_policy=cancel`：取消任务 | 已向后端发出请求：不撤回，等完成；`finish_then_notify`：下次空闲补一句结果 | 只读 `cancelled`；写操作完成后 `interrupted` | 总结 §5.3；LK 同步工具等 5 s 才强制取消会拖住下一轮【推断，LK Q5】，所以只读工具不等 |
| 3 | 已完成、**未回传** | 按 04 能力字段 `tools.silent_close` 分支（不按厂商写）：**有静默收口**（`by_omission` / `flag`）→ 回传真实结果 + `"not_spoken": true`，不再生成（`send_tool_result(reply_required=false)`，04 §3.7；LK Q5），模型能答"刚才查到什么了"，上游与本地登记一致；**否则**（`none` / `unknown`）→ 不回传，本地置 `interrupted`（现行做法【实测：40 次打断卡死 0、重复调用 0，总结 §5.3】）。A-E3 以"追问召回"为判据复核 | 同左；另按 §5.2 补一句 | `interrupted` | A-E3；review B-4 |
| 4 | 已回传、模型在生成（A0）或本地在念（A1 / A2） | 取消生成 / 停播；S2S 按播放位置 `truncate`；补 `{"interrupted": true}`（是否附 `played_text` 由 A-E1 定；补发通道待 04 P0-16） | 同左；写操作已完成，结果已在 output 里，不需补一句 | `interrupted` | 总结 §5.3【实测 20/20】；LK Q5 截断 |
| 5 | 已 `progress` 收口，后台在跑（A3） | `interrupt_policy=continue`：继续；完成后等空闲窗口作 narration | 同左 | 已是 `progress`，不变 | LK Q3；PC Q3 |
| 6 | narration 正在播最终结果时被打断 | 视为已送达，不重播 | 同左 | — | QW §2.3（被用户打断的播报视为已送达） |
| 7 | 委派进行中（A2） | 取消后台任务；丢弃未播出的流式文本；output 只回已播出的部分 | 不适用（写操作不委派，§3.2 #7） | 未播出任何内容：`cancelled`；已播出部分：`interrupted` | BP A2 风险；ORA 缺这一层，过时答案会被念出 |

**规则**
- 打断后到达的任何结果、音频、流式文本，先查代际，过期即丢（KM 3.3）；已 `settled` 的调用拒绝迟到结果（§4.1 不变式 2）。
- 误打断（附和、咳嗽）由回合层过滤，不进入本矩阵；噪音规则只在模型尚未开始调工具或说话时生效（总结 §3.1）。

### 5.2 写操作的额外约束

1. **执行前再查一次代际和门槛**：门槛通过到真正调后端之间，若代际变化或门槛条件（后端状态）变化，放弃执行，`closed_by=cancelled`（总结 §5.3；KM 3.6）。
2. **同一用户回合只认第一个写操作调用**，后续回 `duplicate`（QW §2.1；BP A3）。
3. **执行完才被打断的，下一次空闲补一句告知**（总结 §5.3）。补的话由 `voice_template` 渲染，作 narration，被打断不重播；B 类同时在屏幕上留结果。
4. 不依赖上游的"禁止打断"开关：LK `ctx.disallow_interruptions()` 在 S2S 开着服务端判停时被忽略（LK Q5【仓库】）。
5. 后端按 `idempotency_key` 去重，作为最后一道保险；它不替代第 2 条。

---

## 6. 委派协议

### 6.1 前台 → 后台请求

| 字段 | 说明 | 依据 |
|---|---|---|
| `delegation_id`、`call_id`、`turn_id`、`generation` | 关联和代际校验 | KM 3.3 |
| `intent_hint` | 前台或规则层判出的意图类别 | — |
| `last_utterance_gist` | 前台复述的"最后一句用户话要点"。对冲转写竞态：前台可能在转写完成前就发出调用 | ORA Q1(b) |
| `transcript` | 我们自己的 ASR 定稿文本（有则优先于 gist） | BP A2 |
| `state_block` | 当前状态块（版本号随附） | 架构 §2.2；KM 3.4 |
| `recent_turns` | 最近 K 轮，assistant 部分用**用户实际听到的文本**；K 初值 4【建议值，需实测；取自总结 §3.1 策略层的"最近 4 轮"】 | KM 3.4 |
| `allowed_tools` | 后台可用的工具子集：**只读工具**；写操作不在内 | BP A2 约束 |
| `constraints` | 语音化约束：一两句、先结论、禁 markdown / 列表、数字逐位念（KM 2.7）；B 类另给"可上屏的结构化字段" | FE §4 |
| `deadline_ms` | 后台首句的截止时间；超时走 §6.4 | — |

后台不要求拿到全量历史（ORA 每次全量 JSON 重发、无摘要，是反例）；也不要求像 QW 那样只传自包含 objective——我们两份上下文要对得上指代（BP A2 风险），所以给状态块和最近几轮。

### 6.2 后台 → 前台的两个通道

| 通道 | 内容 | 去向 | 依据 |
|---|---|---|---|
| **speak**（要说的） | 流式正文，按句切出 | 仲裁器 speech 类 → 本地 TTS（B 类同时按句推转写）；带 `generation`，过期即丢 | PC Q6 commentary |
| **context**（只写上下文的） | 中间事实、工具细节、结构化字段 | 本地上下文和状态块；B 类结构化字段可上屏；不出声 | PC Q6 thinking |
| **terminal**（终止事件） | `done` / `failed` / `timeout` / `empty` | 工具层：据此收口前台调用 | PC Q6（委派失败、超时、无输出一定回一句） |

- 前台调用的 output 回传**实际播出的文本**（按播放位置截断），不发 `response.create`（BP A2）。
- 同一委派跨"承接句 → 工具 → 正文"两段保持同一条流式 TTS 连接，避免多一次建连和首包（BP A2 风险）。
- B 类高风险回合：speak 通道的每一句在进 TTS 前过出声前护栏（架构 §3.2）。

### 6.3 投机起跑与代际作废

1. ASR 定稿（约 0.37 s【实测，BP A.0】）即用"转写 + 状态块"启动后台，不等前台在约 0.8 s 发出委派调用（BP A2）。
2. 投机产出**先扣住不播**，直到前台真的发出委派调用、且 `turn_id` 一致才放行（PC `SpeculationGate` 同思路，PC Q6）。
3. 前台直答、或该回合 response 结束仍未委派，投机任务按代际作废；已产出的文本丢弃，不进上下文。
4. **投机任务不得产生副作用**：只能调 `write=false` 的工具；一旦需要写操作，立即撤销投机（PC 在投机中遇到工具调用就整体撤销，PC Q6；我们放宽到允许只读工具）。
5. 投机与前台直答抢话筒由仲裁器的代际和租约挡住（BP A2 风险）。
6. 投机命中率进 trace；A-E4 门槛 ≥ 70%（BP A.4），低于门槛关闭投机。

### 6.4 取消、失败与兜底

- **委派必须能取消**：打断、用户"算了"、代际变化都取消后台任务（§5.1 行 7）。
- **失败 / 超时 / 无输出，前台一定回一句**：由本地直念兜底话术（"这个我没查到，要不要换个问法？"一类），同时前台调用 `closed_by=cancelled, reason=timeout|failed|empty`。不把兜底交给前台模型再生成（再生成还要付约 1.2 s）。
- 同一用户回合只接受第一个委派，后续回 `duplicate`（QW §2.1）。
- 委派不可跨会话重建存活；重建时在途委派本地收口为 `superseded`，若 speak 通道已播出内容，已播文本进新会话历史（KM 3.4）。

---

## 7. 结果如何回到对话

### 7.1 回传 output 的内容

回传给模型的 output 是**收口条目**，不是给模型再念一遍的稿子。字段：

| 字段 | 说明 |
|---|---|
| `status` | `ok` / `failed` / `in_progress` / `interrupted` / `cancelled` / `superseded` / `duplicate` |
| `summary` | 一句话结果摘要，不含 ID、URL、路径（QW 结果指令同义，QW §2.3） |
| `spoken_to_user` | **实际播出的原文**，按播放回执（01 §3.1.6，经仲裁器 02 §4.2 转交）换算的位置截断，文本由上下文层按 05 §2.1 `interrupt_at` 算出，与 05 的 `text_presented` 是同一个量；未播出为空串；`delivery=in_progress`（立即回传、正文还在念）时暂填计划播出的文本，被打断后以打断标记更正（§7.1）（BP A1；架构 §1 原则 2"历史记用户实际听到的"） |
| `shown_to_user` | B 类：上屏内容的摘要或卡片 id |
| `alternatives` / `escalation` | 失败时随结果下发（§7.4） |
| `note` | 给模型的约束，如"已经告诉用户了，不要重复"；`in_progress` 时"不要编造结果、不要再次调用" |
| `delivery` | `in_progress`：立即回传时正文还在念（A1 / A2）。其余情况省略。与 `status=in_progress`（A3 进度收口）不是同一件事 |

- 长度上限 500 字符【建议值，需实测；参照 LK GPT-Live `append_commentary` ≤ 500 token】。
- 回传内容的三种写法（完整结果 / 实际播出原文 / 仅标记）哪种指代正确率最高，由 A-E1 定；本文默认"摘要 + 实际播出原文"。
- **回传时机：立即回传**，排在提示语之后播放，不等前面的音频播完（BP A.1 第 3 条；LK、PC 的串行化会把提示语时长串进延迟，A-E7 量化）。**已决定（review B-2）**：立即回传的 output 内容为 `summary` + 计划播出的文本（写在 `spoken_to_user`）+ `delivery: "in_progress"`；被打断时按 §5.1 行 4 补 `{"interrupted": true}`（这条补发是 05 §3.6 允许的第四种中途插入"已回传调用的打断标记"）；补的时候带不带 `played_text` 由 A-E1 定；补发通道（新建条目，还是同一 `call_id` 再回传）由 04 P0-16 探定。

### 7.2 不发再生成请求的条件

满足任一条即不发 `response.create`（级联即不再跑 LLM）：

1. 路线是 A1、A2、静默收口，或 A3 的承接句由本地直念；
2. `closed_by` 是 `interrupted` / `cancelled` / `superseded` / `duplicate(reject)`；
3. 调用所属代际已过期；
4. 用户正在说话，或回合尚未结束（PC：用户在说时不推理，PC Q2）；
5. 门槛拒绝或阶段不可见（拒绝话术本地直念）。

只有 A0 路线、`duplicate(confirm)`、A3 的模型承接句三种情况会触发再生成，且每个调用最多一轮。

### 7.3 上游会自动回复时的静默选项

- 上游在收到 output 后会自己开口的，必须用静默选项：Gemini `FunctionResponseScheduling.SILENT`（需声明 `NON_BLOCKING`，否则告警并照样作答）、Ultravox `agent_reaction="listens"`（LK Q2【仓库】）。
- 没有静默选项的上游（LK 插件里的 GPT-Live【仓库】；QW provider 读出的能力位相反，待核，BP A.5）：A1 / A2 不成立，按 §3.3 退到 A0 或半级联。
- 我们的上游是否支持，是 A-E0 探针的首项（BP §0；04 §7.3 P0-1）。探针结果写进能力声明（04 §2.2.5），工具执行器按能力声明选择行为（原语 `send_tool_result(reply_required=false)`，04 §3.7），不在代码里按厂商写分支。

### 7.4 替代方案随结果下发

业务失败（`ok=false`）时，结果必须带 `alternatives`（最多两项）和（B 类高风险阶段）`escalation`，话术模板固定为：

> **不能 + 最多两个可行选项 + 升级出口**
> 例（示意）："这个现在做不了。可以换成 X，或者先 Y，你想要哪个？要不要我帮你转人工？"

- 前半句诚实说明限制，后半句给行动选项（FE §5 模式 2）；不先说自己不行，只有工具明确返回不可用后才说明限制（FE §5 模式 3）。
- 替代项由工具从数据算出（FE §5 模式 1）；模型只负责说出来，不得自行补充替代项。
- A 类：最多两个选项、一次一个问题（架构 §2.4）；升级出口可省略。
- B 类：选项同时上屏可点，点选等同说出（架构 §3.2）；升级出口（转人工 / 紧急出口）由规则触发，不由模型决定（架构 §3.4 原则 6）。
- 评测：不支持的请求同时判 honesty（没做成不能说做成了）和 recovery（给了具体替代）（FE §5 模式 5；KM 2.12）。

---

## 8. 决策分层与门槛

### 8.1 顺序

```
ASR 定稿 / 触控事件
  → ① 规则短路 ──命中──▶ 直接执行动作（不进模型）
  → ② 阶段过滤：算出本回合可见动作集
  → ③ 模型提议：ToolSelector 输出 {动作 / 工具, 参数}
  → ④ 修正层：按状态纠正提议
  → ⑤ 实体解析：参数对到数据，原话优先
  → ⑥ 写操作门槛链（仅 write=true）
  → 执行 → §3 分流
```

① 与 ② 在模型之前（事前收窄）；④ 与 ⑥ 在模型之后（事后兜底）。事前收窄优于事后拦截，但 ④ 保留——调研里没有一个仓库有"模型之后按状态纠正"的层，它是我们独有的兜底（KM 2.7、3.6）。

### 8.2 各层接口

| 层 | 输入 | 输出 | 说明与出处 |
|---|---|---|---|
| ① 规则短路 | 识别文本、状态块 | `hit: 动作` 或 `pass` | 管确定和危险的：噪音静默、明确的控制指令、紧急出口、转人工。纯规则盲测 54%（训练句 100%）【实测，总结 §3.1】——只让它管确定的 |
| ② 阶段过滤 | 当前阶段、已完成调用 | 可见动作集 = 阶段动作 ∪ 全局动作 ∪ `visible_after` 已满足的动作 | FE §1.4；pipecat-flows 按节点换工具集。S2S 上不改上游工具表，在 ③ 的输入（级联）或 ③ 的输出校验（S2S）上生效 |
| ③ 模型提议 | 文本（或音频）、最近几轮、状态块、可见动作集 | 动作 + 参数 | `ToolSelector` 可替换接口：规则 / 小文本 LLM / S2S 原生 FC（总结 §4.1）。盲测命中：S2S FC 81%、小文本 LLM 84%【实测，总结 §3.1】 |
| ④ 修正层 | 提议、状态 | 修正后的动作 | 例：无进行中任务时把"确认完成"改成引导开始；刚问过用户问题时两字回答不算噪音（总结 §3.1） |
| ⑤ 实体解析 | 参数中 `entity: true` 的字段、原话、最近提到的实体 | 解析后的参数或 `unresolved` | 别名表、模糊匹配、指代；模型编的全名与原话冲突时以原话为准（总结 §4.1） |
| ⑥ 门槛链 | 动作、参数、原话、后端 | `pass` 或 `reject{gate, reason_code}` | 见 §8.4 |

**阶段外调用的处理**：S2S 模型调了当前阶段不可见的工具，工具层不执行，`closed_by=result`、`ok=false`、`reason_code=not_available_in_phase`，本地直念一句引导话术，不再生成。

### 8.3 阶段与全局动作

- 阶段决定模型能看到的动作集，把写操作门槛做成结构：例如"提交"只在"方案已展示"之后可见（FE §1.4；LK 确认工具只在前置步骤后注入）。
- **全局动作在任何阶段保留**，避免把用户锁死：闲聊、追问、纠正（改字段）、重复、取消 / "算了"、停止播报；B 类另加转人工、紧急出口、挂断（架构 §3.4 原则 4、6）。
- 严格节点图不适合用户随时换话题；图会退化成"全局工具 + 少数阶段工具"，按这个形态设计（FE §1.4）。
- 切阶段不额外跑一次推理（pipecat-flows 切节点多一次推理 0.5–0.8 s，FE §1.4），不用 RESET 丢历史。

### 8.4 门槛链

写操作执行前按顺序检查，任一不过即拒绝：

| 顺序 | `kind` | 检查 | 失败时 |
|---|---|---|---|
| 1 | `intent` | ③④ 的最终动作属于 `allowed_actions` | `reason_code=intent_mismatch` |
| 2 | `utterance` | 用户**原话**命中 `must_match`、不命中 `must_not_match`（例：有"开始 / 提交"类词，排除"怎么开始"） | `reason_code=utterance_not_explicit`；A 类念一句确认问题，B 类同时给确认按钮（架构 §3.2 两档确认） |
| 3 | `backend_state` | 重新读后端：前置状态成立、没有进行中的同类操作 | `reason_code=backend_state`，带 `alternatives` |

- 门槛拒绝回传 `ok=false` 收口（不悬挂），形状参照 SDK 的 `needsApproval` 拒绝时回传 "not approved"（ORA Q5）。
- 模型把"好的"判成"开始"的情况，就是被第 2 道拦下的（总结 §3.1）。
- 执行前再查一次代际和门槛（§5.2 第 1 条）。
- B 类的屏幕点选"确认"视为同时满足 1、2 两道（显式意图），第 3 道仍然要查。

---

## 9. profile 差异

| 维度 | A 无界面硬件 | B 有界面移动端 |
|---|---|---|
| 主要呈现方式 | `verbatim` 多；`screen` 不可用（注册时拒绝，需 `profile_overrides` 给直念版本） | `screen` 多；`verbatim` 用于要点和承接；`model` 的正文走文本先于语音 |
| 主要路线 | A1（状态类）、A2（讲解类）、A3（写操作与设备任务） | A1-屏 / A3-屏为主；A2 用于讲解；高风险回合禁用 A0（§3.3） |
| 结果体 | `voice_template` 必填；选项最多两个 | 结构化输出：`data` + `display`；产出物字段在会话结束时汇总（架构 §3.4 原则 5） |
| 阶段化 | 弱：自由对话，阶段只用于少数子流程 | 强：收集 → 澄清 → 分析 → 给方案 → 收尾；`phases` 普遍使用 |
| 特有工具 | **设备事件工具**：定时 / 到点、传感器、设备控制；事件先按 handle / context / respond / interrupt 定性再交仲裁器（KM 3.3）；设备 MCP 调用 30 s 超时（XZ §2.4） | **转人工工具**：`global`、由规则触发（模型可提议但执行由规则决定），任何阶段可用；带会话摘要（pipecat-flows 转人工时摘要"包括哪里出了错"，FE §1.2） |
| 等待期出声 | 沉默即故障：超过约 2 s 无声必须有声音填充（架构 §2.4）；提示语走旁路不进模型 | 屏幕状态 + 一句语音填充，两者都不进模型（架构 §3.2） |
| 进度节流 | 沿用 QW：首条 ≥ 60 s、间隔 ≥ 60 s | 屏幕进度可实时；语音进度沿用 QW 参数【建议值，需实测】 |
| 替代方案 | "不能 + 两个选项"，升级出口可省 | "不能 + 两个选项 + 升级出口"；选项上屏可点 |
| 出声前护栏 | 只查事实边界 | speak 通道逐句过完整规则集（架构 §3.2） |
| 确认 | 语音复述确认 | 语音复述 + 屏幕确认按钮 |

两个 profile 共享：注册表字段、分流决策表、状态机、六个终态、打断矩阵、委派协议、回传格式、门槛链接口。

---

## 10. 验收与开放问题

### 10.1 验收要点

所有指标用每轮 trace 和冻结盲测集统计；有竞态的项（打断、异步、委派）用 `--repeat` 出通过率，单次通过不算（KM 3.8）。

| 指标 | 门槛 | 依据 |
|---|---|---|
| 悬挂调用数（`received` 后超过 `timeout_ms` + 余量仍无 `closed_by`） | **0** | 总结 §5.2 |
| 后续 3–5 轮的重复调用率 | ≤ 现状，远低于 21%；目标 ≤ 2%【建议值，需实测】 | A-E1 |
| 写操作重复副作用 | **0** | A-E5 |
| 门槛拦截结果 | 与现状一致；阶段外工具被执行 **0** | A-E5；§8.2 |
| 打断收口：出声率 / 卡死 / 重复调用 / 误称已告知 | 20/20、0、0、0（每格 ≥ 20 次） | A-E3 |
| 迟到结果被播出或被回传 | **0**；`late_result_dropped` 计数可观测 | §4.1 |
| 双声（两段语音叠播） | **0** | A-E2、A-E6 |
| 有内容首音 A1 | P50 ≤ 1.1 s（本地 TTS） | A-E2 |
| 有内容首音 A2 | P50 ≤ 1.3 s、P90 ≤ 2.0 s；委派召回 ≥ 81%；闲聊误委派 ≤ 5%；投机命中 ≥ 70% | A-E4 |
| A3 | 首音下降；重复播报 < 5% | A-E5 |
| B 类工具回合 | ≤ 2 s 有内容（屏幕或语音） | 架构 §3.3 |
| 委派失败 / 超时 / 无输出后无声 | **0** | §6.4 |
| judge 分 | 不低于现状；不支持的请求 honesty 与 recovery 配对判，judge 看得到工具结果 | 06 §6.1 规则 7、8；FE §5 |
| trace 完整率 | 每个调用都有 `call_id, name, tool_route, closed_by, reply_required, generation, gate_result`（06 §3.5）：100% | 见下 |

**trace 字段**：以 06 为准，本文不另立字段表。原先提议的每调用字段已并入 06 §3.5：`call_id`、`tool_route`（A0 / A1 / A2 / A3 / silent；为避免与 06 §3.2 的选路 `route` 重名而改名）、`route_fallback_reason`、`gate_result`、`phase`、`generation`、`settled_at_ms`、`late_result_dropped`、`speculation_hit`。`speak_mode` 增加 `screen`、`none` 的提议已被 06 §3.3 采纳（`speak_mode` 描述"怎么说出来的"，与注册表的 `presentation` 不是同一件事）。

### 10.2 开放问题

1. **上游能否静默回传**（A-E0）：决定 A1 / A2 在 S2S 上能否成立；不成立时退 A0 还是改半级联，要结合 B 类的音色统一问题（架构 §6 问题 5）。
2. **S2S 上的在途占位**（P0）：假 output 是否被接受、是否引发自动回复、之后能否再回传真实结果。未通过前 S2S 不做占位。
3. ~~A1 的回传时机与截断的张力~~ **已决定**（review B-2）：保留立即回传，内容为 `summary` + 计划文本 + `delivery: in_progress`，打断后补 `{"interrupted": true}`（§7.1）。剩余待定：是否附 `played_text`（A-E1）；补发通道（04 P0-16）；串行化代价（A-E7）。
4. ~~"已完成未回传时被打断"的统一规则~~ **已决定**（review B-4）：按 `tools.silent_close` 分支（§5.1 行 3）。A-E3 加"追问召回"判据复核。
5. **`latency_class` 的阈值和 `timeout_ms` 初值**：200 ms / 15 s / 30 s 等都是建议值，需要按各工具实测 P90 定。
6. **B 类的高风险回合由谁判定**：建议先由规则（阶段 + 意图类别）定，模型只提议（架构 §6 问题 2）；这直接影响 §3.3 的 A0 禁用范围。
7. **A2 的两份上下文**：前台只有 output 字符串，后台有工具细节，指代要两边对上；`recent_turns` 的 K 和状态块是否够用，靠 A-E1 的指代追问验证。
8. **后台任务跨会话重建**：本文规定委派不跨重建存活；A3 的后台写操作结果在重建后怎么回到新会话（作 narration + 上下文条目）需要和 05 §4.6 对齐（05 已规定：旧会话在途调用由本地收口，结果走本地通道并写本地记录）。
9. **GPT-Live 等上游的能力位**：LK / PC 笔记与 QW provider 读出的相反，可能不是同一产品或版本（BP A.5）。
10. **契约落地形式**：注册表 schema 应作为 schema 文件与本文一同维护，两个团队的执行器都跑同一组 L0 用例（架构 §6 问题 3）；schema 文件的位置和版本规则待定。
