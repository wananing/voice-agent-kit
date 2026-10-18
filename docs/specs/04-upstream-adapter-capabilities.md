# 链路适配层：能力声明与 I/O 原语

> **开源说明**：本文是 voice-agent-kit 的契约规范之一，正文保留设计期原貌。代码注释里的"spec 0X §y"指本目录的规范。文中的出处简写（架构 / 两类架构、KM、BP、总结、HC、PC-B、LK-B、UM、`notes/*.md` 等）和 `../../knowledge-map.md` 一类路径指向作者未公开的调研笔记，对应内容已整理进 [voice-agent-handbook](https://github.com/wananing/voice-agent-handbook)，按 [README 的对照表](README.md#出处简写对照) 查。

> 规范编号 04。状态：草案，除注明"实测"的数字外都没有在我们的上游上验证过。
> 上位文档：`../voice-agent-two-architectures.md`（下称"架构"）§3.2、§4、§5。本规范展开架构 §4 契约表里"链路适配层：能力声明 + I/O 原语"这一行。
>
> **出处简写**（路径相对 `docs/`）：
>
> | 简写 | 文件 |
> |---|---|
> | 架构 | `design/voice-agent-two-architectures.md` |
> | KM | `knowledge-map.md` |
> | BP | `bottleneck-proposals.md` |
> | PC-B | `notes/pipecat-B.md` |
> | LK-B | `notes/livekit-agents-B.md` |
> | LK | `notes/livekit-agents.md`（第一批，只在 say 一处引用） |
> | E2E | `notes/e2e-models.md` |
> | QW | `notes/qwen-audio-agent.md` |
> | HC | `notes/half-cascade-open.md` |
>
> 相关规范（同目录）：术语与枚举总表 `00-glossary.md`；01 回合与客户端协议；02 输出仲裁；03 工具收口；05 上下文与会话；06 trace 与评测（trace 字段权威表，本规范的事件字段按它命名）。
>
> **标注**：【实测】= 我们自己测的；【仓库自述】= 仓库或文档自己说的；【未确认】= 笔记里没有依据，或是闭源服务端行为，只能靠 P0 探针测；【推断】= 从代码行为推出来，没有跑过；【建议】= 本规范给的默认值，没有来源，可以改。
> 笔记都是读开源框架代码得来的，所以下文说"OpenAI 支持 X"时，严格的意思是"开源框架的 OpenAI 插件这么用了"，服务端的实际行为仍以 P0 探针为准。

---

## 1. 目的、范围与术语

### 1.1 目的

用同一层接入四类上游，让对话策略只写一份：

1. 闭源 S2S（OpenAI Realtime、Gemini Live、AWS Nova Sonic、GPT-Live、DashScope、Doubao、StepFun 等）；
2. 自托管 S2S（例如 MiniCPM-o 4.5）；
3. 级联：STT → 文本 LLM → TTS；
4. 半级联：音频 LLM 只出文本（Step-Audio 2 mini、Qwen3-Omni Thinker）→ TTS。

两条依据：

- 架构 §3.2 要求 B 类**按回合类型和风险等级**在 S2S、级联、半级联之间选路，A 类也按回合分流（KM 1.1）。如果每条链路各有一套策略，同一个问题会出现几种答案。
- Pipecat 把"何时 `response.create`、灌入格式、重连次数、换会话时机"写在各个服务里，结果同一个问题各服务处理不一致，例如重连后"已回传"登记表只有部分服务会重建（PC-B §6、§7.2；BP B.1-5）。LiveKit 用 `RealtimeCapabilities` 声明差异、框架按声明分支（LK-B §1.1）；qwen-audio-agent 用能力位，"通用代码不按厂商名分支"（QW §2.6）。本规范取这两者的做法：**差异写成数据（能力声明），行为收成原语，策略全部上移。**

### 1.2 范围

| 在范围内 | 不在范围内（只定义与本层的接口） |
|---|---|
| 能力声明 schema 与已知上游对照表（§2） | 设备协议层（01）、UI 同步层（架构 §4 允许各自实现） |
| I/O 原语的语义、降级、幂等、超时（§3） | 输出仲裁器（02）、回合状态机、看门狗（01 §2；架构 §1 原则 5） |
| 标准化事件流与错误默认归类（§4） | 会话重建器、摘要、状态块内容（05；KM 3.4） |
| 策略上移清单：适配层不得做的决定（§5） | 工具注册表与 `closed_by` 终态（03） |
| 级联 / 半级联如何映射成同一组原语（§6） | 出声前护栏的规则本身（架构 §3.2），本层只提供钩子位置 |
| P0 探针流程（§7） | 选路规则本身（架构 §3.2、§6 开放问题 2） |

### 1.3 位置

```
链路无关层（策略）：回合状态机 · 仲裁器 · 会话重建器 · 工具层 · 护栏 · 分流 · trace
        │  原语调用（§3）                         ▲ 标准化事件（§4）
        ▼                                         │
┌──────────────────────── 链路适配层 ──────────────────────────┐
│  能力声明（§2，静态数据 + 探针证据）                           │
│  适配器：OpenAI 系 │ Gemini │ Nova │ … │ 级联组合 │ 半级联     │
│  只做：协议编解码、字段映射、镜像登记、事件归一、错误归类        │
└──────────────────────────────────────────────────────────────┘
        │ 各厂商原生协议 / 自托管推理服务
        ▼
       上游
```

### 1.4 设计原则

1. **只翻译，不决策。** 适配层能做的是"把原语翻成上游协议、把上游事件翻成标准事件"。凡是有两种以上合理做法的地方，都是策略，归链路无关层（§5）。
2. **做不到就明确拒绝，不偷偷替代。** 原语在当前上游上做不到时返回 `unsupported`（带能力字段名），不自行换一种做法。反例：StepFun 不支持按回复传指令，qwen 的协议层会把指令写成一条普通 user item，**进入历史**（E2E Q4；QW §2.6）。这种替代要由策略层显式选择。
3. **能力未知按最保守值运行。** 字段取 `unknown` 时，上层按"不支持"处理，直到 P0 探针给出结论（§7）。
4. **上层不出现厂商名。** 链路无关层只读能力声明，不写 `if vendor == …`（QW §2.6）。
5. **本地是真相，上游是镜像。** 本层维护的只是"上游会话里现在有什么"的登记；对话的真相在链路无关层（架构 §1 原则 3；KM 3.4）。

### 1.5 术语

| 术语 | 定义 |
|---|---|
| **能力声明**（capabilities） | 描述某个上游（某厂商某模型某版本）能做什么、怎么做的静态数据。每个字段带取值和证据等级。由 P0 探针产出（§7），随适配器一起版本化。对应 LiveKit `RealtimeCapabilities`（LK-B §1.1）、qwen 能力位（E2E Q4）。 |
| **原语**（primitive） | 适配层对上暴露的 I/O 操作，共九个（§3）。原语不含策略：何时调、带什么参数由上层决定。 |
| **镜像**（mirror） | 适配层对"上游会话里当前有哪些条目、各条目的上游 id、哪些工具结果已回传"的登记。只在收到上游确认时更新（LiveKit `RemoteChatContext` 的做法，LK-B §1.2）。镜像**不是**历史的来源：重灌用的是链路无关层的本地上下文，而不是镜像（反例：LiveKit OpenAI 插件重连时重放镜像，导致被截断的回复以完整文本重放，LK-B §2.1、§6.2）。 |
| **重灌**（re-seed） | 建新会话时，把本地上下文（人设、状态块、历史、工具表）按上游允许的方式一次性带入。"历史只在建会话时带入"是业界共识（BP B.1-1；KM 3.4），所以重灌是常规路径，不是故障路径。 |
| **静默收口**（silent close） | 把工具结果回传给上游，让这个调用在上游那边有终态，但**不触发**上游再生成一次回复。结果由我们直接念或上屏（架构 §1 原则 1；KM 1.1 的 A1；03 §3）。对应原语 `send_tool_result(reply_required=false)`。 |
| **按回复覆盖**（per-response override） | 只对某一次回复生效的 instructions / 输出模态 / 工具，不改会话配置。例如 OpenAI `response.create{instructions, output_modalities, tools, tool_choice}`（PC-B §1.3；LK-B §1.6）。价值是绕开"改配置就重新预填"（KM 3.4），但它本身是否破坏缓存【未确认】（PC-B §1.3）。 |
| **代际**（generation） | 仲裁器的整数计数器 `gen`，每次打断或新回合 +1（定义见 01 §1.3、02 §3.2），不是本层的概念。本层对应的是**回复 id**：每次回复一个 `response_id`，适配层给每个输出事件打上它；仲裁器把 `response_id` 绑定到发起时的 OutputRequest（及其代际），打断或取消后按代际丢弃（02 §3.2；架构 §1 原则 5；KM 3.3）。 |

---

## 2. 能力声明

### 2.1 总体形态

每个适配器附一份能力声明。每个字段是"值 + 证据"：

```yaml
# 能力声明示例（格式示意，值取自 OpenAI Realtime 的笔记，未经我们探测）
adapter: openai_realtime
upstream: { vendor: openai, model: "<model-id>", api_version: "<ver>" }
probed_at: null                    # P0 探针完成后填日期
fields:
  turn.server_detection_disable:
    value: yes
    evidence: code_read            # probed | doc | code_read | unknown
    refs: ["PC-B §3.1", "LK-B §3.1"]
  turn.segment_only:
    value: partial                 # semantic_vad 有 create_response=false；server_vad 未确认
    evidence: code_read
    refs: ["PC-B §3.3", "LK-B §3.2"]
  ...
```

- `evidence` 四档：`probed`（P0 探针测过，附 trace 编号）> `doc`（厂商文档）> `code_read`（开源框架代码里读到）> `unknown`。上线前，§7.4 列出的"关键字段"必须达到 `probed`。
- 取值 `unknown` 时上层按最保守值处理（§1.4-3）。每个字段下面写明"保守值"是什么。

### 2.2 字段定义

每个字段给出：类型与取值、含义、探测方法、对上层策略的影响。探测方法里的 "P0-n" 指 §7.3 表中的行号。

#### 2.2.1 回合与判停

| 字段 | 类型 / 取值 | 含义 | 探测方法 | 对上层策略的影响 |
|---|---|---|---|---|
| `turn.server_detection_disable` | `yes \| no \| unknown`；保守值 `no` | 能否完全关掉服务端判停（VAD / 语义 VAD） | 建会话时传关闭参数（OpenAI `turn_detection: null/False`，Gemini `disabled=True`，DashScope 试 `turn_detection: null`），送含 700 ms 静音的音频，看是否仍自动切段、自动回复（P0-2、P0-11） | `yes`：按键 / 本地判停直接驱动 `commit_turn`，走 BP B.3 第 1 档。`no`：看下一个字段 |
| `turn.segment_only` | `yes \| partial \| no \| unknown`；保守值 `no` | 能否保留服务端切段，但不自动发起回复（OpenAI `create_response=false`） | 打开该开关，送一段 2 s 语音 + 1 s 静音，确认有切段事件、无回复事件（P0-2） | `yes`：BP B.3 第 2 档，松键时 `commit_turn` + `request_response`，一句被切成几段也无妨，松键前不会调工具（LK-B §3.2 推断）。`no`：只能走第 3 档，自己在上游前处理音频并扣住松键前的 function call（BP B.3、C1） |
| `turn.server_interrupt_optional` | `yes \| no \| unknown`；保守值 `no` | 服务端检测到用户开口时，能否**不**自动取消正在进行的回复（OpenAI `interrupt_response`） | 播放回复中送一段噪声 / 附和语，看回复是否被服务端取消（P0-2） | `no` 时，服务端 `speech_started` 会直接打掉回复；上层"打断只认按键"的规则（BP B.3）只能管到我们这侧的播放，管不到上游生成 |
| `turn.server_silence_ms` | `int \| range \| unknown` | 服务端判停实际需要的静音时长；可调时给范围 | 合成音频插入 200 / 300 / 400 / 500 / 700 ms 静音，看从多长开始切段（P0-3） | 决定 C1 停顿压缩的阈值 K（BP §0、C1） |
| `turn.sensitivity_levels` | `list \| none` | 不能关时可调的灵敏度档位（Nova 2 的 `endpointing_sensitivity` LOW / MEDIUM / HIGH，PC-B §3.1） | 读文档，各档跑 P0-3 | 关不掉时至少调到最不敏感一档 |
| `turn.commit_mode` | `commit_create \| activity_window \| whole_utterance \| server_only \| none` | 客户端怎样宣告"这一轮输入结束"。`commit_create`：OpenAI 系 `input_audio_buffer.commit` + `response.create`；`activity_window`：Gemini `activity_start` / `activity_end`，窗口外的音频不发（PC-B §3.1）；`whole_utterance`：整段音频一次提交（半级联，HC §0）；`server_only`：只有服务端判停；`none`：无回合概念（真全双工） | 读协议；P0-4 测空 commit、过短 commit、清空缓冲 | 决定 `commit_turn` 的翻译方式（§3.3）；`server_only` / `none` 时按键说话只能靠音频门控 |
| `turn.commit_min_ms` | `int \| unknown` | 提交所需的最短音频。OpenAI 插件要求 > 100 ms，否则不提交（LK-B §3.3） | P0-4：分别提交 50 / 100 / 150 ms 音频 | 短按过滤阈值不能低于它；低于它的按键由上层直接丢弃（C1） |
| `turn.empty_commit_behavior` | `error(code) \| ignored \| unknown` | 空提交的反应。服务端判停已经自己提交时，客户端的 commit 会落空（OpenAI `input_audio_buffer_commit_empty`，LK-B §2.2、§3.2） | P0-4 | 决定这个错误码是否进 benign 映射（§4.6） |
| `turn.input_mute` | `yes \| no \| unknown` | 能否让上游在一段时间内"不听"（Doubao `input_audio_mute/unmute.commit`，E2E Q4） | P0-11：按键期间 unmute、其余时间 mute，看松键前是否还会切段 / 回复 | 【未确认】可能是关不掉判停时"只在按键期间才听"的杠杆 |

#### 2.2.2 上下文与历史

| 字段 | 类型 / 取值 | 含义 | 探测方法 | 对上层策略的影响 |
|---|---|---|---|---|
| `history.injection` | 集合，元素取 `init_only \| packed_item \| per_item \| client_content`；保守值 `{init_only}` | 上游接受历史的方式。`init_only`：只能放在建会话参数里（Nova 非 interactive 文本事件、GPT-Live `session.start.input`）；`packed_item`：建会话后插入**一条**打包的消息（Pipecat 对 OpenAI 的补丁；qwen 的 `<restored_context>`）；`per_item`：逐条插入对话条目；`client_content`：Gemini `send_client_content(turns)`（PC-B §1.2；LK-B §2.3；QW §2.6） | 读协议确定集合；再用 P0-9 对每种方式测"回忆正确率 + 后续回复模态" | 重灌时可选的格式（§3.1）。**集合里有 `per_item` 不代表它可用**：我们实测建会话后逐条插入历史 0/6、建会话时带入成对问答 6/6（LK-B 表 L2【实测】）；OpenAI 逐条插入后"模型会忘了自己能出音频"（PC-B §1.2）。选哪种由策略层按 B-X2 结果定（BP B.2） |
| `history.mid_session_effective` | `yes \| no \| unknown`；保守值 `no` | 会话中途插入的 system / user 条目，模型是否真的看（我们的上游"基本不看"，LK-B L2【实测】） | P0-9 扩展：中途插入一条事实，下一轮追问 | `no` 时，状态只能走配置更新、按回复覆盖或重建（BP B.4） |
| `history.limits` | `{max_items?, max_tokens?, max_item_bytes?}` | 建会话时历史的上限：AWS ≤ 40 条（LK-B §2.3）；GPT-Live ≤ 128 条且服务端另有 8192 token 上限（LK-B §2.3）；Nova 续接参考实现单条 50 KB、总量 200 KB（PC-B §2.3） | 读文档；超限时看报错还是静默截断 | 历史裁剪在策略层做（§5），适配层只校验并拒绝超限 |
| `history.constraints` | 规则列表 | 历史的结构要求。例：Nova 要求历史以 user 开头、在音频开始前发完（LK-B §2.3）；Gemini 2.5 要求最后一条是 user，且用户第一句是语音时会无视灌入的历史（PC-B §1.2） | 读协议；P0-9 | 由策略层在渲染历史时满足；适配层只校验，**不自行补内容**（例如不自己插 `[Resuming conversation]`） |
| `history.text_flips_modality` | `yes \| no \| unknown`；保守值 `yes` | 往音频会话里灌文本历史后，模型是否改用文本回复。LiveKit 专门有一条报错（LK-B §4.2）；KM 3.4 列为风险 | P0-9：灌文本历史后统计后续 N 轮 `reply_modality` | `yes` 时灌入格式要避开它，trace 必须记 `reply_modality`（06 §3.6） |
| `history.tool_calls` | `keep \| as_text \| drop \| unknown` | 工具调用和结果在历史里怎么保留。Gemini 灌带 tool call 的历史报 1007，只能转文字；Nova 直接丢掉（PC-B §1.2） | P0-9：灌入含工具调用的历史，看报错与回忆 | `as_text` 时策略层可选"工具调用转文字"格式（风险：模型模仿 `[Called function …]` 说话，BP B.2）；`drop` 时关键工具结果要进状态块 |
| `history.item_id_echo` | `server_id \| client_id \| none` | 上游是否回显条目 id，本地 id 能否直接当上游 id（LiveKit 两边共用一个 id，LK-B §1.2；Qwen Omni 不回显 item id，E2E Q4） | 插入一条带 id 的条目，看回执 | `none` 时镜像只能按顺序对齐；条目级错误无法定位到具体条目 |
| `history.item_ack` | `yes \| no \| unknown` | 插入条目后有无确认（Doubao `acknowledgesConversationItems:F`，E2E Q4） | 同上 | `no` 时 `open` 的"就绪"只能靠超时判断 |

#### 2.2.3 配置更新

| 字段 | 类型 / 取值 | 含义 | 探测方法 | 对上层策略的影响 |
|---|---|---|---|---|
| `config.update.<target>`，target ∈ `instructions / tools / voice` | `live_full \| live_sparse \| append_only \| next_session` | 会话中途更新该项的方式。`live_full`：立即生效，但每次要发整份配置（OpenAI `session.update`，PC-B §1.3）；`live_sparse`：立即生效，可以只发差量（GPT-Live 只对委派后端的 tools 可以，PC-B §1.3）；`append_only`：只能追加，不能替换（GPT-Live 的 append 事件）；`next_session`：会话内改不了或改了不生效，要等下一个会话，包括"改了就重连"的情况（Gemini 改 tools、Nova、GPT-Live 的 instructions，PC-B §1.3；LK-B §1.6） | 读协议；P0-6：更新后下一轮是否体现新指令、`cached_tokens` 是否掉 | 决定状态块"立即生效"那一层能走哪条通道（BP B.4）。`next_session` 的项只能攒到换会话时一起生效（PC-B M3） |
| `config.update_invalidates_cache` | `yes \| no \| unknown`；保守值 `yes` | 配置更新后是否整段重新预填。我们的上游：未命中缓存的输入 ×3.2，粗粒度更新首音多 80–200 ms，每轮更新多 150–270 ms（KM 3.4【实测】） | P0-6：更新前后各一轮，比较 `cached_tokens` 和首音 | `yes` 时策略层要少改、晚改（BP B.1-4） |
| `config.append_channels` | 列表：`instructions \| thinking \| commentary`，带 `max_tokens` | 不重写前缀的追加通道。只有 GPT-Live 有，每条 ≤ 500 token（PC-B §1.3） | 读协议；P0-5 | 可作为状态注入的旁路，是否真不破坏缓存要测 |
| `config.update_ack` | `yes \| no` | 配置更新有无确认（qwen `acknowledgesSessionUpdate`，E2E Q4） | 读协议 | 决定 `update_config` 的完成判定（§3.8） |

#### 2.2.4 回复控制

| 字段 | 类型 / 取值 | 含义 | 探测方法 | 对上层策略的影响 |
|---|---|---|---|---|
| `response.client_initiated` | `yes \| no` | 客户端能否主动发起回复（qwen `clientResponses`；MiniCPM-o 为 F，E2E Q4） | 读协议 | `no` 时委派结果注入、"结果由我们直接念"之外的收口路径都不可用（E2E Q4） |
| `response.overrides` | `{instructions: replace \| none, modalities: bool, tools: bool, tool_choice: bool, instructions_fallback: history_item \| none}` | 按回复覆盖（§1.5）。OpenAI 的 `response.create{instructions}` 是用"会话指令 + 追加"**替换**会话指令（LK-B §1.6）；StepFun `response.create` 只带 modalities，单次指令改为插入 user item，会进历史（E2E Q4） | P0-5：按回复传一条会话里没有的指令，看是否生效、下一轮是否还生效（即是否进了历史）、`cached_tokens` 是否变化 | 可用时，状态块优先随回复下发而不是改会话配置（BP B.4）；`instructions_fallback: history_item` 只能由策略层显式选用（§1.4-2） |
| `response.single_slot` | `yes \| no \| unknown`；保守值 `yes` | 同一时刻是否只允许一个进行中的回复，并发的 `response.create` 会被拒（qwen `singleResponseSlot`，E2E Q4；OpenAI 有 `conversation_already_has_active_response` 错误码，PC-B §2.2） | P0：回复进行中再发一次请求 | `yes` 时上层（仲裁器）必须串行化回复请求；适配层遇并发请求返回 `slot_busy`，不排队（§3.4） |
| `response.say` | `native \| via_local_tts \| none` | 能否让上游按给定文本念出来（用上游原生音色）。LiveKit 里声明了 `supports_say` 的是 Phonic、Ultravox，这时 `say()` 交给模型自己念，`add_to_chat_ctx=False` 会被忽略；OpenAI 没声明，要配 TTS（LK Q1(b)）。qwen 的 provider 接口有 `buildSpeakResponse`（QW §2.6），各家实现没读【未确认】 | P0：让上游念一段含数字、专有名词的文本，比对转写是否逐字 | `native` 且逐字时，"直念"可以不换音色；否则直念走本地 TTS，接受音色差异（架构 §6-5） |

#### 2.2.5 工具

| 字段 | 类型 / 取值 | 含义 | 探测方法 | 对上层策略的影响 |
|---|---|---|---|---|
| `tools.calling` | `native \| prompted \| none` | 原生 function call；或者只能靠系统提示手写格式（Qwen3-Omni 手写 hermes `<tools>`，HC Q2）；或者没有（MiniCPM-o，E2E Q4） | 读协议；跑一次调用 → 回注 → 续答 | `prompted` 时解析与格式校验在适配层做，失败按 benign 报；`none` 的上游不能承接工具回合 |
| `tools.auto_reply_after_result` | `yes \| no \| unknown`；保守值 `yes` | 回传工具结果后，上游会不会不等请求就自己续答。自动：Gemini、Nova、Phonic、Ultravox、Doubao；不自动：OpenAI、DashScope、StepFun、Step-Audio 2；GPT-Live 两份代码说法相反（KM 2.10；E2E Q4；HC Q2） | P0-1：回传 function output 后不发 `response.create`，观察 5 s 内是否开口，≥ 20 次 | `yes` 时"静默收口"默认不可用，A1 / A2 要换路（BP §0 第 1 行）。自动续答的好处是省一次往返，代价是失去"结果由我们直接念、模型不再生成"这条路（E2E Q4） |
| `tools.silent_close` | `by_omission \| flag \| none \| unknown` | 静默收口怎么做：不自动续答的上游"只回传不请求"即可（`by_omission`）；自动续答的上游是否有显式的静默选项（`flag`） | P0-1 | 决定 `send_tool_result(reply_required=false)` 能否执行（§3.7） |
| `tools.placeholder_accepted` | `yes \| no \| unknown` | 在途工具先回一个"仍在进行中"的假 output，上游是否接受、是否引发自动回复 | P0-12 | 决定 A2 和 KM 3.5 的去重手段能否用 |
| `tools.parallel_calls` | `yes \| no \| unknown` | 一次回复里能否有多个调用。Step-Audio 示例默认 `parallel_tool_calls: False`，并行能否用【未确认】（HC Q2） | 构造需要两个工具的请求 | `no` 时工具层不必处理同回复多调用的收口顺序 |
| `tools.args_streaming` | `complete \| incremental` | 调用参数是一次到齐，还是分片到达（Step-Audio 流式下分片，用最新值覆盖，HC Q2） | 读协议 | 适配层负责拼完整，只对上发完整的 `tool.call`（§4.4） |

#### 2.2.6 截断

| 字段 | 类型 / 取值 | 含义 | 探测方法 | 对上层策略的影响 |
|---|---|---|---|---|
| `truncate.mode` | `audio_ms \| text_rewrite \| local_only \| none` | 打断后怎样让上游知道"用户只听到了前 N 毫秒"。`audio_ms`：OpenAI `conversation.item.truncate(audio_end_ms)`；`text_rewrite`：文本模态下删掉原条目、用已播放文本重建（LK-B §4.2）；`local_only`：上游无状态，本地上下文就是下一次请求的输入（级联、半级联）；`none`：上游保留完整回复（Gemini、AWS，LK-B §6.3） | 截断后追问"你刚才最后说了什么"（P0 扩展） | `none` 时，上游会以为自己说完了，只能在下次重灌时用"已播放文本"纠正（KM 3.4）。截断支持不影响本地上下文必须记"听到的"这一点 |

#### 2.2.7 会话生命周期

| 字段 | 类型 / 取值 | 含义 | 探测方法 | 对上层策略的影响 |
|---|---|---|---|---|
| `session.resumption` | `none \| handle` | 断线后能否凭 handle 恢复服务端状态（Gemini session resumption；handle 只在内存，不跨进程，PC-B §2.3） | P0-6：断线后带 handle 重连，追问断线前的信息 | `handle` 时重建器可以先试续接，失败再重灌 |
| `session.max_secs` | `int \| unknown` | **上游**硬上限。AWS 约 8 min（PC-B §2.3）。注意 LiveKit OpenAI 插件 20 min、AWS 插件 6 min 是客户端策略，不是上游上限（KM 2.10） | 读文档；长会话实测到被断 | 换会话时机要早于它（BP B.2） |
| `session.max_turns_retained` | `int \| unknown` | 上游实际保留的轮数。我们的上游约 20 轮（BP B.0-4【实测】） | P0：40 轮脚本，第 3 轮说名字，之后追问 | 主动换会话周期 N < 它（BP B-X4） |
| `session.expiry_notice` | `yes \| no` | 到时限前是否预告（Gemini `go_away`：LiveKit 收到就重连，LK-B §2.2；Pipecat 没处理，PC-B §2.3） | 读协议 | `yes` 时发 `session.expiring` 事件，给重建器留时间 |
| `session.idle_timeout_secs` | `int \| unknown` | 空闲会话被回收的时间（Nova 预建会话空闲 30 s 就重建，PC-B §2.3） | 建会话后不送音频，看多久被关 | 决定预建会话要不要保活、何时重建（BP B.2） |
| `session.server_compaction` | `none \| sliding_window` | 服务端自行压缩上下文（Gemini `SlidingWindow`，本地不知道丢了什么，PC-B §2.3） | 读协议 | 打开它等于让上游替我们做摘要，和"摘要在我们这侧"（§5）冲突，默认不开【建议】 |
| `session.init_ready_signal` | `ack_event \| timeout` | `open` 何时算就绪：有确认事件（`session.updated`）还是只能等（Nova 首次建会话最多等 500 ms，LK-B §2.3） | 读协议 | 决定 `open` 的完成判定（§3.1） |

#### 2.2.8 用量与缓存

| 字段 | 类型 / 取值 | 含义 | 探测方法 | 对上层策略的影响 |
|---|---|---|---|---|
| `usage.cache_reported` | `none \| cached_tokens \| cached_tokens_by_modality` | 是否上报缓存命中。OpenAI `response.done` 有 `cached_tokens` 和文本 / 音频明细（PC-B §1.3；LK-B §1.6）；其他厂商【未确认】 | P0-7 | B 节所有实验都用这个指标（BP §0）；`none` 时只能用首音差间接估算 |
| `usage.semantics` | 描述 | 缓存 token 是否已包含在 prompt_tokens 里，各厂商不同（06 §3.10 `usage_semantics`） | 读文档，对账单 | trace 按它归一，避免成本算重 |

#### 2.2.9 错误

| 字段 | 类型 / 取值 | 含义 | 探测方法 | 对上层策略的影响 |
|---|---|---|---|---|
| `errors.code_map` | 表：原始错误码（或消息子串）→ `{class, reason}` | 该上游错误码到标准四类（§4.6）的映射。它是数据，随探针更新 | P0-8：收集错误码清单；回放历史错误日志重新标注（BP B-X3） | 事件流按它给 `class`；策略层按 `class + reason` 决定重建、缩历史还是忽略 |
| `errors.code_stability` | `stable_codes \| message_match` | 错误码稳定可用，还是要按消息子串匹配（xAI 的错误码和 OpenAI 不一样，Pipecat 按子串兜底，PC-B §2.2） | 读文档；看实际错误 | `message_match` 时映射表写子串规则 |
| `errors.event_correlation` | `yes \| no` | 上行事件能否带 id、错误能否回指到具体事件（LiveKit 条目类错误带 `event_id`，只让对应 future 失败，LK-B §2.2） | 发一个非法条目，看错误里有无 event_id | `yes` 时条目级错误定位到条目（BP B.5），只让这一条失败 |

#### 2.2.10 输出

| 字段 | 类型 / 取值 | 含义 | 探测方法 | 对上层策略的影响 |
|---|---|---|---|---|
| `output.modalities` | 子集：`audio \| text` | 能输出的模态 | 读协议 | 只有 `text` 的上游（级联、半级联理解端）必须接本地 TTS（§6） |
| `output.modality_switch` | `per_response \| per_session_live \| session_fixed` | 输出模态能在什么粒度上切。OpenAI `response.create` 每次可带 `output_modalities`，但一次只能一种（PC-B §4）；Ultravox 会话中可切 `output_medium`（PC-B §4）；Step-Audio 2 按回合用 `<tts_start>` 选出语音还是只出文本（HC Q1b）；Gemini 设 TEXT 可能不被新模型支持（PC-B §4） | P0-5：同一会话里一轮音频、一轮文本 | 决定"按回复选模态"（BP B.6-2）能否做；`session_fixed` 时 S2S 回合和文本回合要两个会话或两个上游 |
| `output.transcript` | `yes \| no` | 音频输出是否带文本转写 | 读协议 | 没有转写就拿不到"已播放文本"，本地历史只能记"说了什么" |
| `output.text_lead_ms` | `int \| unknown` | 同一回复里文本比对应音频早多少。S2S 只早几十毫秒（架构 §3.2；KM 2.10：moshi 的文本 token 只早 80 ms）；级联是整句 | 同时记录 text delta 和 audio delta 的时间戳 | 决定出声前护栏钩子能否插入（§6.4）。S2S 上视为不可用 |
| `output.audio_format` | `{encoding, sample_rate}` | 输出音频格式（Doubao 输出 float32 PCM，E2E Q4） | 读协议 | 适配层统一转码，上层只看到一种格式 |
| `output.voice_prompt` | `yes \| no \| unknown` | 能否指定参考音色（Step-Audio 2 token2wav 的 `prompt_wav`，音质和稳定性【未确认】，HC Q1b） | 读协议；主观评测 | 影响音色统一（架构 §6-5） |

#### 2.2.11 上行

| 字段 | 类型 / 取值 | 含义 | 探测方法 | 对上层策略的影响 |
|---|---|---|---|---|
| `uplink.faster_than_realtime` | `tolerated \| degrades \| unknown`；保守值 `degrades` | TCP 卡顿后音频突发、比实时快时，识别和 VAD 是否正常 | P0-10：把 3 s 音频在 0.5 s 内送完 | `degrades` 时适配层前要有限速；"卡顿不补静音"（KM 2.1）是否成立取决于它 |
| `uplink.gaps` | `tolerated \| degrades \| unknown`；保守值 `degrades` | 上行有空洞（本地删掉静音帧后时间轴不连续）时的反应 | P0-10：删掉句中 400 ms 静音后直接拼接送出 | 决定 C1 的"删帧"能否做，还是只能"压缩"（BP §0） |
| `uplink.streaming_input` | `streaming \| whole_utterance` | 能否边说边送。两个开源半级联候选都没有流式输入（HC §0） | 读接口 | `whole_utterance` 时，松键后才开始预填，首音要加上整段预填时间 |
| `uplink.audio_format` | `{encoding, sample_rate, chunk_ms?}` | 输入格式（MiniCPM-o 每块 1 s float32，E2E Q4） | 读协议 | 适配层转码与分块 |

#### 2.2.12 级联专用（其他上游填 `n/a`）

| 字段 | 类型 / 取值 | 含义 | 影响 |
|---|---|---|---|
| `cascade.user_transcript` | `native \| side_request \| none` | 用户转写从哪来。级联天然有；半级联理解端不产出转写，可以在回复后并行发一次低优先级转写请求（KM 2.10；HC Q3） | 没有转写就没法把历史存成文本，前缀缓存难命中（HC Q3） |
| `cascade.pre_speech_hook` | `sentence \| none` | 出声前护栏钩子的粒度（§6.4） | 选路时"文本先于语音"的回合只能走有钩子的适配器（架构 §3.2） |
| `cascade.prefix_cache` | `yes \| no \| unknown` | 自托管推理服务的前缀缓存能否命中、能否读到命中量。vLLM 对多模态输入的前缀匹配【未确认】（HC Q3） | 历史存文本、易变状态放消息尾部（KM 3.4；BP B.6-4） |

### 2.3 已知上游对照表

以下全部来自笔记（多数是读开源框架代码），**不是我们的探针结果**。"—" 表示笔记里没有涉及。标【未确认】的格子在 P0 里必须测。

**表 A：回合、历史、配置**

| 上游 | 判停能否关 | 只切段不回复 | 提交方式 | 历史注入 | 工具调用在历史里 | 配置更新（instructions / tools） |
|---|---|---|---|---|---|---|
| **OpenAI Realtime** | 能（`turn_detection` 设 False / null，PC-B §3.1；LK-B §3.1） | semantic_vad 有 `create_response=false`；server_vad 是否支持【未确认】（PC-B §3.3；LK-B §3.2） | `commit_create`，commit 需 > 100 ms（LK-B §3.3） | `per_item` 可用但有坑；Pipecat 改用 `packed_item`（PC-B §1.2） | `keep`（上游）；LiveKit 插件重连时自己丢掉（LK-B §2.1） | `live_full` / `live_full`（PC-B §1.3） |
| **Gemini Live** | 能（`disabled=True`，PC-B §3.1）；LiveKit 插件仍声明 False，因为框架还不能门控（LK-B §3.3） | — | `activity_window`（PC-B §3.1） | `client_content`（Vertex 不支持该配置）；2.5 版首句语音时无视历史（PC-B §1.2）；无 handle 时打包成一条（LK-B §2.3） | `as_text`（带 tool call 报 1007，PC-B §1.2） | instructions：Pipecat 只存不用 = `next_session`，LiveKit 发一条 role=model 的内容 turn（会进历史）；tools：`next_session`（改就重连）（PC-B §1.3；LK-B §1.6） |
| **AWS Nova Sonic** | 不能，只有灵敏度（Nova 2）（PC-B §3.1） | — | `server_only` | `init_only`，≤ 40 条，以 user 开头，音频前发完（LK-B §2.3） | `drop`（PC-B §1.2） | `next_session` / `next_session`（改工具 0.15 s 后回收，LK-B §1.6） |
| **GPT-Live（OpenAI Live）** | 服务端判停，Pipecat 无关闭入口；上游能否关【未确认】（PC-B §3.1） | — | `server_only`【未确认】 | `init_only`，≤ 128 条 / 8192 token（LK-B §2.3） | 纯文本历史，工具调用怎么带【未确认】 | instructions `next_session`（建会话后不可变）+ `append_only`（三种 append，≤ 500 token）；委派工具 `live_sparse`（PC-B §1.3）。qwen provider 读出 `mutableSession:T`，与此相反，待核（KM 2.10） |
| **DashScope**（Qwen Audio / Omni Realtime） | `smart_turn` / `semantic_vad`；文档称不暴露手动 VAD 调参；能否关【未确认】，P0 试 `turn_detection: null`（E2E Q4） | 【未确认】 | OpenAI 兼容，`commit_create`【推断】 | `per_item` 可用（`conversationItems:T`）；qwen 用 `packed_item`（E2E Q4；QW §2.6）；Omni 不回显 item id | 【未确认】 | `mutableSession:T`，按 OpenAI 兼容推断为 `live_full`【推断】 |
| **Doubao Seeduplex** | 档案写 `server_vad`，但建会话时没发 `turn_detection`；能否关【未确认】；`input_audio_mute/unmute` 可能是杠杆【未确认】（E2E Q4） | 【未确认】 | 自有方言，`response.create` 翻成 `speech_text_buffer.commit`（E2E Q4） | 有条目接口，但条目无确认（E2E Q4） | 【未确认】 | `mutableSession:T`，方式【未确认】 |
| **StepFun**（StepAudio 3 Realtime） | `server_vad`；能否关【未确认】；外放易误打断（E2E Q4） | 【未确认】 | OpenAI 兼容方言【推断】 | 同 DashScope（E2E Q4） | 【未确认】 | `mutableSession:T`，方式【未确认】 |
| **MiniCPM-o 4.5**（自托管） | 没有判停事件，模型每步自己决定听还是说；`force_listen` 写死 false，语义【未确认】（E2E Q4；KM 2.10） | n/a | `none` | 只有 `session.init{system_prompt}`，无条目接口（`conversationItems:F`）；历史只能写进 system_prompt【推断】 | n/a（无工具） | `next_session`（`mutableSession:F`） |
| **级联组合**（STT + LLM + TTS） | 自控 | 自控 | 本地判停 → STT 定稿 | 每次请求自带全部消息，格式任意 | `keep` | 每次请求重组 prompt，立即生效；代价只在前缀缓存（KM 3.4） |
| **Step-Audio 2 mini**（半级联，自托管） | 自控 | 自控 | `whole_utterance`（按 25 s 切块只是编码器窗口，HC Q1b） | 每次请求自带；demo 保留历史原始音频，建议存文本（HC Q3） | `keep`（assistant `tool_calls` + `role:"input"` 回注，HC Q2） | 同级联 |

**表 B：回复、工具、截断、会话、观测、输出**

| 上游 | 按回复覆盖 | 工具结果后自动回复 / 静默 | 截断 | 续接 / 时长 | 缓存上报 | 输出模态 | say / 直接念 | 单响应槽位 |
|---|---|---|---|---|---|---|---|---|
| **OpenAI Realtime** | instructions（替换）、modalities、tools、tool_choice（LK-B §1.6；PC-B §1.3） | 不自动；不发 `response.create` 即静默，服务端是否真不开口靠 P0-1 | `audio_ms`；文本模态 `text_rewrite`（LK-B §4.2） | 无 handle；上游时长上限【未确认】（20 min 是 LiveKit 插件策略） | `cached_tokens`（含音频明细） | audio 或 text 二选一，可按回复选（PC-B §4） | 无，需本地 TTS（LK Q1(b)） | 有 `conversation_already_has_active_response`，说明会拒并发（PC-B §2.2）；qwen 默认值写 F，待核 |
| **Gemini Live** | 无；LiveKit 靠改会话级工具再改回，一次带工具的回复可能触发两次重连【推断】（LK-B §1.6） | 自动；静默选项【未确认】 | `none`（LK-B §6.3） | resumption handle + `go_away`；服务端可滑窗压缩（PC-B §2.3；LK-B §2.2） | 【未确认】 | audio；TEXT 在新模型上可能不支持（PC-B §4） | 【未确认】 | qwen 的 Google Live 为 T（E2E Q4） |
| **AWS Nova Sonic** | 无 | 自动（KM 2.10）；静默【未确认】 | `none`；本地历史来自模型输出文本，是"说了什么"（LK-B §6.3） | 无 handle；约 8 min 硬上限；预建会话空闲 30 s（PC-B §2.3） | 【未确认】 | audio（+ 文本事件） | 【未确认】 | 【未确认】 |
| **GPT-Live** | qwen provider 读出 `perResponseInstructions:T`【未确认】（E2E Q4） | 两份代码说法相反，待核（KM 2.10） | 【未确认】 | 【未确认】 | 【未确认】 | audio | 【未确认】 | qwen 为 F（E2E Q4） |
| **DashScope** | instructions（`perResponseInstructions:T`）（E2E Q4） | 不自动（适合"结果由我们直接念"）（KM 2.10） | 【未确认】 | 【未确认】 | 【未确认】 | 【未确认】 | 【未确认】 | T（E2E Q4） |
| **Doubao** | 无（E2E Q4） | **自动**（E2E Q4） | 【未确认】 | 【未确认】 | 【未确认】 | audio，float32 PCM | 【未确认】 | T |
| **StepFun** | 只有 modalities；指令会写成 user item 进历史（E2E Q4） | 不自动 | 【未确认】 | 【未确认】 | 【未确认】 | 可按回复带 modalities【推断】 | 【未确认】 | T |
| **MiniCPM-o 4.5** | 无（`clientResponses:F`） | n/a（无工具） | `none` | 【未确认】 | 【未确认】 | listen / text / audio 交错 delta（E2E Q4） | 无 | F（无客户端回复） |
| **级联组合** | 全部（每次请求自带） | 不自动；静默 = 不发起下一次 LLM 调用 | `local_only`，按 TTS 播放位置精确截断（KM 1.1；PC-B §4） | 无会话概念 | 自托管 vLLM 可取命中量【推断】；云 LLM 看厂商 | text → 本地 TTS | `native`（直接进 TTS） | 自控 |
| **Step-Audio 2 mini** | 全部；模态按回合选（`<tts_start>`，HC Q1b） | 不自动，续答由客户端发起（HC Q2） | 只出文本时 `local_only`；原生语音模式能否按文本截停【未确认】（KM 2.10） | 无会话概念 | stepfun vLLM 分支的前缀缓存【未确认】（HC Q3） | text；或 text + 音频 token（token2wav，可设 `prompt_wav`） | 只出文本时走本地 TTS = `native`；原生语音模式【未确认】 | 自控 |

所有 provider 代码里都**没有看到关闭判停的路径**（MiniCPM-o 本来就没有判停），国产云三家的判停能否关只能靠 P0（E2E Q4；KM 2.10）。上行速率容忍（比实时快、有空洞）对所有 S2S 上游都【未确认】；级联取决于所选 STT；半级联是整段提交，不受影响【推断】。

---

## 3. I/O 原语

### 3.0 通用约定

- **返回值**：每个原语返回 `Ack{ok, code?, class?, detail?}`。`ok=false` 时 `code` 取 `unsupported(<能力字段名>) | invalid_state | slot_busy | timeout | upstream_error`，`class` 同 §4.6 四类。
- **关联 id**：调用方传入 `turn_id`（回合）、`response_ref`（调用方给这次回复起的 id）等；适配层把上游 id（item_id、response_id、call_id）和它们对应起来，所有事件都带双方的 id。每个上行事件带适配层生成的 `event_id`，以便把上游错误回指到具体调用（BP B.5；LK-B §2.2）。
- **不重试**：原语失败就上报，不自己重试、不自己重连。重连预算在链路无关层（§5；05 §7.2）。唯一的例外是协议层面的"同一请求重发"（例如 WebSocket 帧发送失败前的一次重写），不改变语义。
- **超时**：下文的超时值都是【建议】默认值，可按上游配置。超时一律以事件报出（`error{class: transient, reason: timeout}`），不让调用方干等（01 §2）。
- **不可并发的原语**（`open`、`close`、`update_config`）在同一会话内串行执行；其余可以并发调用，适配层保证上行顺序与调用顺序一致。

### 3.1 `open(SessionInit)`

```yaml
SessionInit:
  instructions: "<人设 + 规则>"          # 不含易变状态
  state_block: { version: 12, text: "<状态块>" }   # 放在 instructions 末尾还是别处，见下
  tools: [ <工具 schema，建会话时给全> ]
  history:
    format: pairs | packed | pairs_tools_as_text   # 由策略层选（BP B.2），适配层不选
    items:
      - { role: user, text: "…", turn_id: c41 }
      - { role: assistant, text: "<已播放文本>", interrupted: true, turn_id: c41 }
      - { role: tool, call_id: c7, name: get_route, result_text: "…" }
  resume_handle: null                      # 仅 session.resumption = handle 时有效
  output_default: audio | text
```

**语义**：建一个新的上游会话，把 `SessionInit` 按上游允许的方式一次性带入（重灌）。完成后返回 `SessionHandle{session_id, effective: {history_items_sent, tools_sent}}`，并发 `session.opened`。

- `state_block` 与 `instructions` 分开传，是为了让适配器决定它在上游 prompt 里的**物理位置**（例如级联把易变的状态块放消息尾部以保前缀缓存，KM 3.4、BP B.6-4；S2S 通常拼在系统提示末尾，架构 §2.2）。位置由能力决定，内容由策略层决定。
- 历史格式由 `history.format` 指定，适配器只负责翻成上游机制（例如 `packed` 在 OpenAI 上是一条 `conversation.item.create`，在 Nova 上是建会话参数里的一段文本）。
- 镜像在此时初始化：登记已带入的条目、已完成的工具调用（对应 Pipecat `send_new_results=False` 的做法，重建后登记表同步重建，PC-B §2.2 M6）。

**按能力降级**：

| 条件 | 行为 |
|---|---|
| `history.format` 不在 `history.injection` 能表达的范围内（例如对 `tool_calls: drop` 的上游要求 `pairs` 且历史含工具条目） | 返回 `unsupported(history.tool_calls)`，不自行丢弃或转文字 |
| 历史超过 `history.limits` | 返回 `invalid_state(history_over_limit, limit=…)`，不自行截断 |
| 违反 `history.constraints`（如 Nova 要以 user 开头） | 返回 `invalid_state(history_constraint, rule=…)`，不自行补条目 |
| `resume_handle` 给了但上游不支持 / handle 失效 | 返回 `unsupported(session.resumption)` 或 `upstream_error`；由重建器决定改走重灌 |
| 上游无会话概念（级联、半级联） | 只建本地请求上下文，立即就绪 |

**幂等**：不幂等。每次调用建一个新会话；同一个 `SessionHandle` 只能 open 一次。

**超时**：连接 10 s（LiveKit 默认连接超时 10 s，LK-B §2.1）；就绪判定按 `session.init_ready_signal`：有确认事件的等确认，每条历史条目的确认统一等 5 s（LiveKit 做法，LK-B §1.4）；没有确认的等固定时间（Nova 500 ms，LK-B §2.3）后视为就绪。超时发 `error{transient, reason: open_timeout}` 并关闭半建的连接。

### 3.2 `send_audio(frame, meta)`

```yaml
meta: { turn_id: c42, seq: 1031, sample_count: 16000, captured_at_ms: 1727…, source: button | vad | wake }
```

**语义**：把一帧用户音频送上游。第一次出现新的 `turn_id` 时，按 `turn.commit_mode` 隐式开启输入窗口（Gemini 发 `activity_start`，PC-B §3.1）。适配层只做转码、分块（例如 MiniCPM-o 1 s 一块）、按 seq 去重。

**`turn_id` 只接受网关归一后的值**（review B-5）：被 `turn.merge` 合并的回合用合并后的值（`into`，01 R2.6-4），不会因合并前的 `c43` 多开一个输入窗口；开放麦下按 01 R2.2-5 的 `ts_samples` 区间归属后的值；**不接受 0**（帧头"无回合"）。归一由网关在调用本原语之前完成；收到未归一的值（含 0）视为调用方错误，返回 `invalid_state(turn_id)`。

**不做**：不判断这帧该不该送（按键门控、停顿压缩、删帧都在上层，BP B.3、C1），不补静音，不限速（限速是上层按 `uplink.*` 能力决定的策略）。

**按能力降级**：

| 条件 | 行为 |
|---|---|
| `uplink.streaming_input = whole_utterance` | 在本地累积，`commit_turn` 时一次提交 |
| 级联 | 送流式 STT |
| 上游背压 / 发送缓冲超过阈值 | 发 `uplink.backpressure{buffered_ms}` 事件，不丢帧；丢不丢由上层定 |

**幂等**：按 `(turn_id, seq)` 去重，重复帧丢弃。

**超时**：不阻塞，无超时；发送失败报 `error{transient, reason: send_failed}`。

### 3.3 `commit_turn(turn_id, mode)`

`mode ∈ commit | discard`。

**语义**：宣告这一轮用户输入结束（`commit`），或者丢掉这一轮已送的音频（`discard`，用于短按过滤和打断时清缓冲，对应 LiveKit `clear_user_turn`，LK-B §3.3）。`commit` 本身**不**请求回复；需要回复时上层接着调 `request_response`。这样"提交"和"回复"分开，只切段不回复、按键扣住工具（BP B.3）都能组合出来。

**翻译**：

| `turn.commit_mode` | `commit` | `discard` |
|---|---|---|
| `commit_create` | `input_audio_buffer.commit` | `input_audio_buffer.clear` |
| `activity_window` | `activity_end` | 不发 `activity_end`，关闭窗口【推断：Gemini 无显式丢弃】 |
| `whole_utterance` | 封存本地缓冲 | 清空本地缓冲 |
| 级联 | STT flush，等 final | 丢弃 STT 结果 |
| `server_only` / `none` | 返回 `ok` + `code: committed_by_server`，不发任何东西 | 返回 `unsupported(turn.commit_mode)` |

**按能力降级**：音频短于 `turn.commit_min_ms` 时返回 `invalid_state(too_short)`，不发上游；服务端判停已自行提交导致的空提交错误，按 `turn.empty_commit_behavior` 归为 benign（LK-B §3.2）。

**幂等**：同一 `turn_id` 重复 `commit` 返回首次结果；已 `commit` 的回合再 `discard` 返回 `invalid_state`。

**超时**：等上游提交确认 2 s【建议】；级联等 STT final 1.5 s【建议】，超时发 `error{transient, reason: commit_timeout}`，同时把已有的 partial 作为最后结果上报（标 `final_by_timeout: true`）。

### 3.4 `request_response(turn_id, response_ref, overrides)`

```yaml
overrides:
  instructions: "<仅本次回复的补充指令，例如状态块>"   # 可选
  modalities: [text]                                 # 可选
  tools: [...] / tool_choice: none | auto | <name>  # 可选
```

**语义**：请求上游生成一次回复，返回上游 `response_id`（与 `response_ref` 绑定）。之后该回复的所有事件都带这两个 id（§4.3）。

**按能力降级**：

| 条件 | 行为 |
|---|---|
| `response.client_initiated = no` | 返回 `unsupported(response.client_initiated)` |
| 某个 override 不支持（按 `response.overrides`） | 整个请求返回 `unsupported(response.overrides.<字段>)`，**不**去掉该字段后照发，**不**把指令写成历史条目 |
| `response.single_slot = yes` 且已有进行中的回复 | 返回 `slot_busy`，不排队；是否先取消旧回复由编排层（回合状态机）决定，不归仲裁器（02 §3.1：仲裁器授权的是出声，不是生成；review B-6） |
| 服务端已经自动发起了回复（判停关不掉、或工具结果后自动续答） | 返回 `ok` 并绑定到那个已存在的回复，`origin: server`；上层可据此判断"这不是我要的那次回复" |
| 级联 / 半级联 | 见 §6.2 |

**幂等**：按 `response_ref` 幂等，重复调用返回同一个 `response_id`。

**超时**：`response.started` 2 s、首个 delta 3 s【建议】；超时发 `error{transient, reason: response_timeout}`，回合终态由上层看门狗给（01 §2）。

### 3.5 `cancel_response(response_id)`

**语义**：停止该回复的生成。适配层保证：发出 cancel 之后，该 `response_id` 的 delta **不再向上转发**（即使上游迟到），最后发一个 `response.done{status: cancelled}`。仲裁器仍按代际再过滤一次（架构 §1 原则 5，双保险；参照 TEN 消费端按 request_id 二次过滤，KM 1.2）。

**按能力降级**：级联 = 取消 LLM 流 + TTS flush，并遵守 flush_end 之后零音频（架构 §2.2）；无取消接口的上游（MiniCPM-o）只做本地丢弃，返回 `ok` + `code: local_only`。

**幂等**：对已结束或已取消的回复再次取消，返回 `ok`；上游的"取消竞态"错误（`response_cancel_not_active`、"Cancellation failed"）归 benign（PC-B §2.2；LK-B §2.2）。

**超时**：等上游确认 1 s【建议】；超时也在本地视为已取消，并发 `response.done{status: cancelled, confirmed: false}`。

### 3.6 `truncate(item_id, audio_end_ms, played_text?)`

**语义**：告诉上游，这条助手输出用户只听到了前 `audio_end_ms`。`audio_end_ms` 是**条目内**的播放位置，由网关按 01 §5.5 从播放回执换算（`offset_ms(sentence_id) + played_ms`，再换到该条目的起点）；它不是单句回执里的 `played_ms`（01 §3.1.6 的 `played_ms` 是句内时长）。`played_text` 是上下文层按 05 §2.1 `interrupt_at` 算出的"已播放文本"（仲裁器只提供回执，不管文本，02 §1.2），用于文本方式截断。无论上游是否支持，**本地上下文都记已播放文本**（架构 §1 原则 2；05 §3.3），这不是本原语的职责，是链路无关层的。

**按 `truncate.mode` 降级**：

| 取值 | 行为 |
|---|---|
| `audio_ms` | 发 `conversation.item.truncate(audio_end_ms)`；`audio_end_ms = 0` 时改为删除该条目（LK-B §6 提到的第一批做法） |
| `text_rewrite` | 需要 `played_text`，缺则返回 `invalid_state`；删掉原条目、以已播放文本重建（LK-B §4.2） |
| `local_only` | 更新本地请求上下文里的该条目，立即返回 |
| `none` | 返回 `unsupported(truncate.mode)`；上层在下次重灌时纠正 |

**幂等**：同一条目只接受单调变小的 `audio_end_ms`；更大或相等的值返回 `ok` 不发。镜像只在收到上游确认（如 `conversation.item.truncated`）后更新——LiveKit 插件没处理这个确认，镜像一直是完整文本，重连时就暴露了（LK-B §6.2）。

**超时**：确认 2 s【建议】，超时报 benign（`scope: item`），不影响会话。

### 3.7 `send_tool_result(call_id, result, reply_required)`

**语义**：把工具结果回传，让上游那边的调用有终态。`reply_required=false` = 静默收口（§1.5）；`true` = 回传后让上游据此生成回复，返回 `response_id`。是否需要再生成由工具的"呈现方式 + 执行时长 + 是否写操作"决定，是上层的决定（架构 §1 原则 1；KM 1.1）。

**按能力降级**：

| 条件 | `reply_required=false` | `reply_required=true` |
|---|---|---|
| `tools.auto_reply_after_result = no` | 只回传（`by_omission`） | 回传 + 发起回复（等价于再调一次 `request_response`） |
| `= yes` 且 `tools.silent_close = flag` | 回传并带静默标志 | 回传 |
| `= yes` 且无静默选项 | 返回 `unsupported(tools.silent_close)`，**不发送**。由上层选：不回传、本地收口；或者照发并取消随后的自动回复；或者接受再生成 | 回传，绑定服务端自动发起的回复 |
| `= unknown` | 按 `yes` 处理（§1.4-3） | 同左 |
| 级联 / 半级联 | 结果追加到本地消息，不发起 LLM 调用 | 追加后发起一次 LLM 调用（Step-Audio 用 `role:"input"` 回注，HC Q2） |

**幂等**：按 `call_id` 幂等。重复回传返回 `ok` + `code: duplicate`（LiveKit 把 `item_create_duplicate_item_id` 视为成功，LK-B §1.4）。`call_id` 不在镜像里（例如重建会话后在途调用丢失，PC-B §2.2）返回 `invalid_state(unknown_call)`，由工具层本地收口（BP B.2）。

**超时**：条目确认 5 s【建议，取 LiveKit 条目确认时长】；超时报 benign（`scope: call`），工具层按 `closed_by` 规则处理。

### 3.8 `update_config(patch, mode)`

```yaml
patch: { instructions?: "…", state_block?: {version, text}, tools?: [...], voice?: "…" }
mode: live | append | next_session
```

**语义**：会话中途修改配置。`mode` 由策略层按状态块分层选择（BP B.4）：必须立即生效的用 `live` 或 `append`，可以晚的用 `next_session`（只登记，下次 `open` 时由重建器带入，适配层不保存）。

**按能力降级**：

| 条件 | 行为 |
|---|---|
| `mode=live`，patch 里某项的 `config.update.<target>` 是 `live_full` | 发整份配置 |
| `… live_sparse` | 只发差量 |
| `… next_session` | 返回 `unsupported(config.update.<target>)`。**不得**自己重连让它生效（Gemini 改工具即重连、Nova 改工具即回收都是插件的隐式行为，LK-B §1.6；PC-B §1.3）；要不要换会话由重建器决定 |
| `mode=append`，无 `config.append_channels` 或超过 `max_tokens` | 返回 `unsupported` / `invalid_state(too_long)` |
| 级联 / 半级联 | 立即生效于下一次请求 |

**幂等**：按 `state_block.version`（或 patch 的哈希）幂等，同版本重复调用不发。

**超时**：有确认的等 `session.updated` 3 s【建议】；更新完成后下一次 `usage` 事件会反映缓存变化，适配层不做判断，只上报。

### 3.9 `close(reason)`

**语义**：关闭会话。立即关闭，不等待进行中的回复——"等助手说完再换"是重建器的交接时机策略（BP B.2），不是 `close` 的语义。关闭前把进行中的回复以 `response.done{status: cancelled}` 收尾，发 `session.closed{reason, final_usage}`。

**幂等**：重复关闭返回 `ok`。

**超时**：优雅关闭 2 s【建议】，超时强制断开。

---

## 4. 标准化事件流

### 4.1 公共信封

```yaml
event:
  type: "<见下>"
  session_id: s-19
  turn_id: c42            # 能确定时必填（格式见 01 R2.2-2）
  response_id: r-88       # 回复相关事件必填
  response_ref: rr-42-1   # 调用方给的 id，能对上时必填
  item_id: i-301          # 上游条目 id（或本地分配的句 id）
  seq: 5520               # 本会话单调递增
  t_upstream_ms: …        # 上游时间戳（有的话）
  t_local_ms: …           # 适配层收到的时间
  raw_ref: "<原始事件类型/id，便于排查>"
```

事件字段命名对齐 trace 权威表（06 §3）：`reply_modality`、`server_vad_segments_in_turn`、`cached_input_tokens` 等从这些事件直接取。

### 4.2 回合与识别

| 事件 | 字段 | 说明 |
|---|---|---|
| `input.speech_started` / `input.speech_stopped` | `source: server_vad` | 服务端判停信号，只作信息。上层是否据此打断，按策略（BP B.3："打断只认按键"） |
| `input.segmented` | `turn_id, segment_index` | 服务端在同一个 `turn_id` 内切了一段。上层据此统计"一次按键被切成几段"（BP B.3；06 §3.7 `server_vad_segments_in_turn`） |
| `input.committed` | `by: client \| server` | 输入已提交 |
| `transcript.user.partial` / `.final` | `text, turn_id, final_by_timeout?` | 用户识别。迟到的 final 仍带原 `turn_id`，由上层归到正确回合（KM 2.11） |
| `transcript.assistant` | `response_id, item_id, text, is_final` | **回显之一**：上游对自己输出音频的转写。它是"说了什么"，不是"听到了什么"；已播放文本由仲裁器按播放回执截取 |
| `item.echo` | `local_id?, item_id, kind` | **回显之二**：上游确认收到了我们注入的条目（`conversation.item.added` 一类，LK-B §1.2），用于更新镜像。`history.item_id_echo = none` 的上游不发 |

> 说明："回显"在需求里没有定义，本规范把它拆成上面两种。如果团队另有所指，在开放问题里改。

### 4.3 回复与音频

| 事件 | 字段 | 说明 |
|---|---|---|
| `response.started` | `response_id, response_ref?, origin: client \| server_vad \| auto_tool` | `origin` 让上层分辨"我请求的回复"和"上游自己发起的回复"（关不掉判停、自动续答时会出现） |
| `response.text.delta` | `response_id, item_id, text` | 文本输出 |
| `response.audio.delta` | `response_id, item_id, seq, audio, duration_ms` | 音频输出，统一格式（§2.2.10）。每个 delta 必带 `response_id` 和 `item_id`，仲裁器按代际丢旧输出（架构 §1 原则 5） |
| `response.sentence` | `response_id, item_id(=sentence_id), text, audio_ms, submitted_at_ms?` | 级联 / 半级联在句边界发，供 UI 按句对齐、设备分句下发（架构 §2.2、§3.2）。`submitted_at_ms` 是该句文本交给 TTS 的本地时刻（拿不到时省略），供 trace 计算 `seg.first_segment_ms`（06 §2.4） |
| `response.done` | `response_id, status: completed \| cancelled \| failed \| incomplete, reply_modality, confirmed?` | 每个 `response.started` 必有且只有一个 `response.done` |

### 4.4 工具调用

| 事件 | 字段 | 说明 |
|---|---|---|
| `tool.call` | `call_id, name, args(完整), response_id, turn_id, before_commit: bool` | 只在参数完整后发一次（`tools.args_streaming = incremental` 时由适配层拼好）。`before_commit=true` 表示调用出现在该回合 `commit_turn` 之前（关不掉判停时的"松键前调工具"），上层据此扣住（BP B.3 第 3 档） |
| `tool.call_cancelled` | `call_id, reason` | 上游侧取消（例如回复被打断） |

### 4.5 会话

| 事件 | 字段 |
|---|---|
| `session.opened` | `session_id, effective` |
| `session.resumable` | `handle`（Gemini `session_resumption_update`，PC-B §2.3） |
| `session.expiring` | `remaining_s, source: go_away \| max_secs` |
| `session.closed` | `reason, by: client \| server, final_usage` |
| `uplink.backpressure` | `buffered_ms` |

### 4.6 错误

```yaml
error:
  class: fatal | transient | context_overflow | benign     # 与 06 §3.9 error_class 同一枚举
  reason: auth | quota | billing | invalid_config | session_lost |
          timeout | throttled | cancel_race | empty_commit | duplicate | moderation |
          item_rejected | tool_parse | unknown
  scope: session | response | item | call
  ref: { event_id?, response_id?, item_id?, call_id? }
  raw: { code?, message? }
```

四类的含义（与 KM 3.4 / BP B.5 的四类一一对应；trace 记法见 06 §3.9 `error_class`，05 §7.1 引用同一套）：

| class | 含义 | 对应四类 | 上层默认动作（策略层决定，此处只是参考） |
|---|---|---|---|
| `fatal` | 重建也没用 | 重建也没用（鉴权、配额、计费） | 停止，兜底话术，告警 |
| `transient` | 会话或回复失效，但换个会话可能恢复 | 会话失效可重建 | 走重建器，受重建预算约束（05 §7.2） |
| `context_overflow` | 上下文超限，原样重建必然再失败 | 上下文超限 | 必须先缩历史再重建，否则原样失败（LK-B §2.2 Gemini 1007；05 §4.2 `rebuild_reason=context_overflow`） |
| `benign` | 只影响这一回复 / 条目 / 调用 | 非致命 | 记日志，只让这一条失败 |

**默认归类规则**（上游的 `errors.code_map` 没覆盖时按顺序套用；`code_map` 优先）：

1. 鉴权、授权、配额、计费、账号停用 → `fatal`。依据：LiveKit OpenAI 只把这四类当致命，否则"重连能成功但每次生成都失败，会无限循环"（LK-B §2.2）；Pipecat `ErrorCategory` 把 AUTHENTICATION / AUTHORIZATION / INVALID_REQUEST 视为永久（PC-B §2.1）。
2. 会话还没建成就失败（参数错误） → `fatal, reason: invalid_config`（LiveKit Gemini 连上之前失败不重试，LK-B §2.2；Pipecat OpenAI Live 同，PC-B §2.2）。
3. 上下文超限（Gemini 1007 一类） → `context_overflow`。适配层不重试。
4. 连接断开、服务端结束会话、流超时、限流、模型未就绪 → `transient, reason: session_lost | throttled | timeout`（Nova 的 Throttling / ModelNotReady / ModelTimeout 会重启会话，LK-B §2.2）。
5. 取消竞态（`response_cancel_not_active`、`conversation_already_has_active_response`、"Cancellation failed"）、服务端已提交导致的空 commit、重复条目 id、带 event_id 的条目级错误、审核命中、工具结果解析错误（Nova "Tool Response parsing error"） → `benign`（PC-B §2.2 M8；LK-B §2.2）。
6. `response.done` 状态为 failed 且错误码不在致命表 → `benign, scope: response`（LiveKit "乐观地视为可恢复"，LK-B §2.2）。
7. 其余 → `transient, reason: unknown`，同时记一条"未映射错误码"日志，供 P0 更新 `code_map`。选 `transient` 而不是 `fatal` 的理由：重建器有重连预算兜底（每回合最多 3 次，§5），不会无限循环；两家框架的做法相反（Pipecat OpenAI 其余一律致命、LiveKit 其余一律可恢复，PC-B §2.2；LK-B §2.2），这里取可恢复加预算【建议】。
8. 错误码不稳定的上游（`errors.code_stability = message_match`）按消息子串匹配（PC-B §2.2 xAI）。

审核命中归 `benign` 是我们的约定（KM 3.4）；qwen-audio-agent 选择隔离出错回合后重建会话（QW §2.6），代价是缓存全冷。两者选哪个是策略，适配层只报 `reason: moderation`。

### 4.7 用量

| 事件 | 字段 |
|---|---|
| `usage` | `response_id, input_text_tokens, input_audio_tokens, cached_input_tokens, cached_text_tokens, cached_audio_tokens, output_text_tokens, output_audio_tokens, generated_audio_ms, usage_semantics` |

每个 `response.done` 之后发一次（上游有上报时）。字段名对齐 06 §3.10，接 OTel `gen_ai.usage.*`（06 §4.3）。`usage.cache_reported = none` 时 `cached_input_tokens` 为空，不填 0。

---

## 5. 策略上移清单

以下决定**必须**在链路无关层做。适配层提供能力声明和原语，不自作主张。

| 决定 | 归属（链路无关层组件） | 适配层提供 | 适配层不得 |
|---|---|---|---|
| **灌入格式**：成对问答 / 打包成一条 / 工具转文字（BP B.2、B-X2） | 会话重建器 | `history.injection`、`history.tool_calls`、`history.text_flips_modality`、`history.constraints` | 自选格式；自行丢弃工具条目或转文字；补"[Resuming conversation]"一类内容 |
| **历史裁剪**：保留几条、哪些 | 会话重建器 | `history.limits` | 静默截断 |
| **换会话时机**：主动换会话周期、交接空档、预建与否（BP B.2、B-X4） | 会话重建器 | `session.max_secs`、`max_turns_retained`、`expiry_notice`、`idle_timeout_secs`；`session.expiring` 事件 | 自己按时长回收会话（LiveKit AWS 插件 6 min、OpenAI 插件 20 min 都是插件策略，KM 2.10）；配置变更触发的隐式重连 |
| **摘要**：何时做、输入是什么（已播放文本）（KM 3.4） | 会话重建器 + 后台文本 LLM | — | 打开服务端上下文压缩（`session.server_compaction`）而不告知 |
| **重连预算**：每回合最多 3 次、指数退避（封顶 10 s）、稳定 10 s 清零（BP B.5；PC-B M7；参数以 05 §7.2 为准） | 会话重建器 | 错误四分类 + reason | 自动重连；自动重试失败的原语 |
| **非致命白名单** | 会话重建器（策略配置） | `errors.code_map` 作为默认映射；原始码透传 | 吞掉错误；对 benign 错误自行重建会话 |
| **按键门控**：只在按键期间送音频、停顿压缩、删帧、短按丢弃、松键前 function call 扣住（BP B.3 V1–V3、C1） | 音频门控 + 回合状态机 | `turn.*`、`uplink.*`；`tool.call.before_commit` | 自行过滤音频；自行扣住或丢弃 function call |
| **打断判定**：是否响应服务端 `speech_started` | 回合状态机 + 仲裁器 | `input.speech_started` 事件、`turn.server_interrupt_optional` | 收到 `speech_started` 自行取消回复（上游自己取消的除外，那是上游行为，要如实上报） |
| **分流**：本回合走 S2S / 级联 / 半级联（架构 §3.2） | 分流规则（对话编排） | 能力声明（尤其 `cascade.pre_speech_hook`、`output.text_lead_ms`、`output.modality_switch`） | 在适配器内部按内容选路 |
| **状态注入通道**：配置更新 / 按回复覆盖 / 追加通道 / 懒生效（BP B.4、B-X5） | 会话重建器 + 状态块管理 | `config.*`、`response.overrides` | 把按回复指令改写成历史条目 |
| **收口与再生成**：`reply_required` 取值、自动续答上游上的替代方案（架构 §1 原则 1） | 工具层 | `tools.*`；`send_tool_result` 的明确拒绝 | 自行决定是否让模型再说一遍 |
| **输出模态与音色**：本回合要音频还是文本、直念用上游 say 还是本地 TTS（BP B.6） | 分流规则 + 仲裁器 | `output.*`、`response.say` | 上游只出文本时自行接 TTS 播放（级联适配器内置 TTS 是例外，见 §6.1） |
| **出声前护栏**（架构 §3.2） | 护栏 | 钩子位置与等待机制（§6.4） | 自己判定内容 |
| **已播放文本**进历史（架构 §1 原则 2） | 上下文层（05 §3.3），依据仲裁器转交的播放回执（02 §4.2） | `truncate` 原语、`transcript.assistant` | 用生成全文代替已播放文本更新镜像以外的任何东西 |

检查办法：适配器代码里不应出现回合计数、定时换会话、重试循环、按内容判断的分支；链路无关层代码里不应出现厂商名（§8 验收）。

---

## 6. 级联与半级联作为"上游"

### 6.1 组成

级联适配器内部由三段组成：流式 STT、文本 LLM（流式、支持工具）、流式 TTS。半级联适配器是两段：音频 LLM（只出文本）+ TTS。对上它们和 S2S 适配器暴露同一组原语、同一种事件流。

TTS 放在适配器内部，是因为对级联来说"出声"就是回复的一部分；但它必须和 A 类直念、B 类系统话术用的是**同一个 TTS 配置（同一个声音）**，否则三条路线无法混用（架构 §3.2 推论一）。TTS 服务本身可以共享，适配器只是它的一个调用方。

### 6.2 原语映射

| 原语 | 级联（STT → LLM → TTS） | 半级联（Step-Audio 2 mini / Qwen3-Omni Thinker → TTS） |
|---|---|---|
| `open` | 不建远端会话；构造本地请求上下文：`system(人设 + 工具说明)` 作稳定前缀，历史存文本，**状态块放消息尾部**以保前缀缓存（KM 3.4；BP B.6-4） | 同左；历史一律存文本，只有本轮是音频（HC Q3 建议）。Qwen3-Omni 的工具只能写进 system（`tools.calling = prompted`，HC Q2） |
| `send_audio` | 送流式 STT | 本地累积（`whole_utterance`） |
| `commit_turn(commit)` | STT flush，等 final；final 作为本轮 user 文本 | 封存音频，作为本轮 user 的音频 content part |
| `request_response` | LLM 流式生成 → 按句切分 → 过护栏钩子（§6.4）→ 句级 TTS；识别定稿即可投机起跑（架构 §2.2）。TTS 接受增量文本时（双向流式，如火山 v3），一轮回复开一个 TTS 流：无护栏则 LLM 增量逐个送入、由 TTS 切句；有护栏则送入放行后的整句 | 音频 LLM 流式出文本 → 同左。Step-Audio 的"只出文本"靠助手消息 `content: None`，出原生语音靠 `<tts_start>`（HC Q1b） |
| `cancel_response` | 取消 LLM 流；TTS flush，flush_end 之后零音频（架构 §2.2） | 同左 |
| `truncate` | `local_only`：按 TTS 播放位置把本地 assistant 文本截成已播放部分，下次请求就是截断后的上下文（PC-B §4：文本模式下天然按实际播放位置截断） | 同左 |
| `send_tool_result` | 追加 tool 消息；`reply_required=true` 时再发一次 LLM 调用，只付文本 LLM 的 TTFT（KM 1.1） | Step-Audio：`role:"input"` + `tool_call_id` 回注，再插一条空助手消息续答（HC Q2）；Qwen3-Omni 回注格式【未确认】 |
| `update_config` | 立即生效于下一次请求 | 同左 |
| `close` | 释放 STT / TTS 流 | 释放 |

### 6.3 事件映射

- 级联：STT 的 partial / final → `transcript.user.*`；LLM 文本 → `response.text.delta`；TTS 每句 → `response.sentence` + `response.audio.delta`（`item_id` 用句 id），句级边界同时供 UI 同步（架构 §3.2）。TTS 自己切句时，句界取 TTS 报告的句子（如火山的 TTSSentenceStart）；TTS 不报句界时整轮回复算一句；LLM 的工具调用 → `tool.call`，并且**在该回复的任何音频之前**上报。
- 半级联：理解端不产出用户转写（HC Q3）。`cascade.user_transcript = side_request` 时，适配器在回复结束后发一次低优先级转写请求，结果以 `transcript.user.final{late: true}` 上报；是否开启由策略层在 `SessionInit` 里指定（KM 2.10）。
- 两者都没有 `input.speech_started` / `input.segmented`（判停在上层）。

### 6.4 文本先于语音的护栏钩子

位置：**LLM（或理解端）产出一个完整句子之后、提交给 TTS 之前**。

```yaml
pre_speech_hook:
  input:  { response_id, turn_id, sentence_index, text, is_last }
  output: allow | replace{ text } | block{ escalate: bool }
  budget_ms: 50            # 【建议】超时按 block 处理还是 allow，由护栏配置决定
```

- 钩子由护栏（链路无关层）注册，适配器只负责在这个位置**同步等待**结论，不看内容（§5）。
- 注册了钩子时，即使 TTS 接受增量文本，适配器也只把**放行后的整句**送进 TTS，不送 token 增量；没有钩子时才逐 token 送入（§6.2）。
- `block` 之后，适配器取消该回复（相当于 `cancel_response`），发 `response.done{status: cancelled, reason: guardrail}`；后续由策略层决定说什么（"不能 + 具体替代 + 升级出口"，架构 §3.2）。
- 工具调用在出声之前上报（§6.3），护栏可以在工具层拦截写操作，不必等到文本层。
- 首句的钩子耗时直接叠加在"有内容"首音上（B 类预算 ≤ 1.5 s，架构 §3.3），所以钩子要能在句级上快速返回。
- **S2S 上游没有这个钩子**：文本只比声音早几十毫秒（`output.text_lead_ms`，架构 §3.2；KM 2.10），声明为 `cascade.pre_speech_hook = none`。Step-Audio 2 的原生语音模式（`<tts_start>`）同样没有，能否按文本截停【未确认】（KM 2.10）。这就是"高风险回合走文本"的结构原因：分流规则读这个字段，而不是读厂商名。
- 风险：Step-Audio 的示例里首轮就加了 `<tts_start>`，模型可能先说一句再发出工具调用（HC Q2【未确认】）；只出文本 + 工具的组合没有示例。接入时要在 P0 里测"工具调用前是否已有文本 / 音频输出"。

### 6.5 级联的能力声明要点

| 字段 | 级联 | 半级联 |
|---|---|---|
| `turn.*` | 自控；`commit_mode` = 本地判停 → STT final | 自控；`whole_utterance` |
| `history.injection` | 每次请求自带，任意格式 | 同左 |
| `config.update.*` | 立即生效，代价只在前缀缓存 | 同左 |
| `response.overrides` | 全部支持 | 全部；模态按回合 |
| `tools.auto_reply_after_result` | `no` | `no`（Step-Audio，HC Q2） |
| `truncate.mode` | `local_only` | `local_only`（只出文本时） |
| `session.*` | 无会话上限 | 同左；输入 25 s 切块（HC Q1b） |
| `uplink.streaming_input` | `streaming`（取决于 STT） | `whole_utterance` |
| `cascade.pre_speech_hook` | `sentence` | `sentence`（只出文本时） |
| `usage.cache_reported` | 自托管时看推理服务指标【推断】 | 同左【未确认】 |

---

## 7. P0 探针流程

依据 BP §0："A、B、C 三节的大部分方案都取决于我们的上游 S2S 能做什么"。接入任何新上游（包括同一厂商换模型版本）都走这个流程，产出该上游的能力声明，并把汇总写进 `docs/upstream-capabilities.md`（BP §0 指定的位置）。

### 7.1 准备

1. **探针音频集（冻结）**：
   - 合成语音，句中插入 200 / 300 / 400 / 500 / 700 ms 静音（BP §0 第 3 行）；
   - 儿童语音带句中停顿（和 C-X3 共用样本，BP B-X1）；
   - 50 / 100 / 150 ms 的短片段；
   - 外放回声条件下的播放中人声 / 附和语（StepFun 误打断，BP §0 第 11 行）；
   - 含数字、专有名词的念读文本（测 say 逐字性）。
2. **探针 harness**：直接调用适配器原语，记录原始上游事件和标准化事件；每次运行带 `upstream.model`、`api_version`、日期，进 trace。
3. **重复次数**：有竞态或概率性的项每组 ≥ 20 次（BP B.8："6 次太少"）。

### 7.2 步骤

1. **静态填表**：读厂商文档和开源适配代码，填出所有字段，`evidence` 标 `doc` / `code_read`。已有笔记的上游直接用 §2.3 的对照表起步。
2. **逐项探测**：按 §7.3 执行，结果写回字段，`evidence: probed`，附 trace 编号。
3. **错误码收集**：探测过程中出现的所有错误码进 `errors.code_map`；再回放历史错误日志，按 §4.6 重新标注（BP B-X3）。
4. **判定与分支**：按 §7.4 的判定标准给出取值，据此确定上层走哪条路（例如 BP B.3 的三档）。
5. **评审**：能力声明和适配器代码一起评审、一起版本化。
6. **复测触发**：上游模型版本或 API 版本变化；厂商发布变更说明；`unmapped error` 日志连续出现；每季度一次【建议】。

### 7.3 探测项

| # | 探测项（BP §0） | 步骤 | 写入字段 | 影响的方案 |
|---|---|---|---|---|
| P0-1 | 回传 function output 不请求回复，上游会不会自己开口；有无静默选项 | 触发工具调用 → 回传结果 → 5 s 内不发任何请求，观察是否出现 `response.started`；有静默参数的试参数 | `tools.auto_reply_after_result`、`tools.silent_close` | A1、A2 |
| P0-2 | 服务端判停能否关、能否调大静音时长、能否只切段不回复、能否不自动打断 | 依次试关闭参数、最大静音时长、`create_response=false`、`interrupt_response=false`；送 2 s 语音 + 1 s 静音、播放中送噪声 | `turn.server_detection_disable`、`turn.segment_only`、`turn.server_interrupt_optional`、`turn.sensitivity_levels` | B.3、C1 |
| P0-3 | 服务端判停实际静音阈值 | 用 §7.1 的静音梯度样本，统计各档切段率 | `turn.server_silence_ms` | C1 停顿压缩参数 K |
| P0-4 | 空 commit、过短 commit、清空缓冲 | 分别提交 0 / 50 / 100 / 150 ms；提交前 clear | `turn.commit_min_ms`、`turn.empty_commit_behavior`、`turn.commit_mode` | C1 短按丢弃 |
| P0-5 | 按回复传 instructions / 选输出模态；追加通道 | 按回复传一条会话里没有的指令，看本轮是否遵循、下一轮是否仍遵循（进没进历史）、`cached_tokens` 变化；同会话一轮音频一轮文本 | `response.overrides`、`output.modality_switch`、`config.append_channels` | A2、B.4、B.6 |
| P0-6 | 会话续接、时长上限、会话内配置更新是否重新预填 | 带 handle 断线重连后追问；长会话跑到被断；更新配置前后比较 `cached_tokens` 和首音 | `session.resumption`、`session.max_secs`、`config.update.*`、`config.update_invalidates_cache` | B.2、B.4 |
| P0-7 | 是否返回缓存命中 token | 固定前缀连续两轮，看 usage | `usage.cache_reported`、`usage.semantics` | B 节所有实验 |
| P0-8 | 错误码清单：上下文超限类、取消竞态类 | 构造超长历史、重复取消、并发回复、非法条目 | `errors.*`、`response.single_slot` | B.5 |
| P0-9 | 建会话时带入文本历史后，回复模态会不会变成纯文本；各灌入方式的回忆效果 | 对 `history.injection` 里的每种方式，灌入含名字、陪同人、工具结果的历史，统计回忆正确率和后续 N 轮 `reply_modality`；中途插入一条事实后追问 | `history.text_flips_modality`、`history.mid_session_effective`、`history.tool_calls`、`history.constraints` | B.2、B-X2 |
| P0-10 | 上行比实时快、上行有空洞 | 3 s 音频 0.5 s 内送完；删掉句中 400 ms 静音后拼接送出；比较识别和切段 | `uplink.faster_than_realtime`、`uplink.gaps` | C1 删帧；"卡顿不补静音" |
| P0-11 | 厂商特有的判停杠杆 | DashScope 试 `turn_detection: null`；Doubao 按键期间 unmute、其余 mute；StepFun 外放条件下统计误打断率（E2E-E5） | `turn.*`、`turn.input_mute` | B.3；换上游时的选择 |
| P0-12 | 在途工具占位是否被接受、是否引发自动回复 | 回传"仍在进行中"的假 output，观察报错与回复 | `tools.placeholder_accepted` | A2、KM 3.5 去重 |
| P0-13 | say / 直接念是否逐字（本规范补充） | 让上游念含数字、专有名词的文本 20 次，比对输出转写 | `response.say` | 直念是否换音色 |
| P0-14 | 截断是否生效（本规范补充） | 截断后追问"你刚才最后一句说了什么" | `truncate.mode` | 重灌纠正的必要性 |
| P0-15 | 工具调用前是否已有输出（本规范补充，半级联必测） | 触发工具的请求，记录 `tool.call` 之前的 text / audio delta | 影响 `cascade.pre_speech_hook` 的有效性 | 护栏 |
| P0-16 | 已回传调用被打断后，打断标记的补发通道（review B-2） | 回传结果后在正文播放中打断，分两组补 `{"interrupted": true}`：新建一条对话条目 / 同一 `call_id` 再回传一次 output；各 ≥ 20 次，看上游是否接受、是否报错或触发自动回复，追问"刚才说到哪了"看模型是否知道被打断 | 结论写进 `docs/upstream-capabilities.md`；是否新增能力字段待探针后定 | 03 §5.1 行 4、§7.1；§3.7 的 `call_id` 幂等规则是否要为打断标记开例外；05 §3.6 第四种中途插入 |

### 7.4 判定标准

- **行为类字段**（例如"会不会自己开口"）：20 次中 20 次符合 → `yes`；0 次 → `no`；其间 → 记 `flaky(k/20)`，上层按保守值处理【建议】。概率性的"不支持"比"不支持"更危险，因为它会在线上偶发。
- **阈值类字段**（静音阈值、最短提交）：取切段率首次超过 50% 的那一档作为阈值，同时记录各档切段率【建议】。
- **效果类字段**（灌入方式的回忆效果）：按 B-X2 的通过标准：回忆正确率不低于现状、纯文本回复轮数为 0、首轮不把历史当问题回答、不模仿 `[Called function …]` 格式（BP B.8）。
- **关键字段**（上线前必须 `probed`）：`turn.server_detection_disable`、`turn.segment_only`、`history.injection`（含效果）、`history.text_flips_modality`、`tools.auto_reply_after_result`、`tools.silent_close`、`config.update_invalidates_cache`、`usage.cache_reported`、`errors.code_map`、`response.single_slot`。它们直接决定 A1–A3、B.2–B.5 走哪条路（BP §0）。

---

## 8. 验收要点与开放问题

### 8.1 验收要点

1. **能力声明完整**：每个接入的上游有一份能力声明，所有字段有值和证据等级；§7.4 的关键字段为 `probed`，附 trace 编号。
2. **上层无厂商分支**：链路无关层代码里搜不到厂商名或模型名（配置和能力声明文件除外）。
3. **适配层无策略**：适配器代码里没有重试循环、定时回收、回合计数、按内容分支；逐条对照 §5 的"适配层不得"列。
4. **明确拒绝**：对每个上游，把所有 `unsupported` 组合跑一遍（例如在自动续答上游上 `send_tool_result(reply_required=false)`、在 `next_session` 上游上 `update_config(live)`），确认返回 `unsupported(<字段>)` 而不是悄悄换一种做法。
5. **共用行为测试**：用本地 mock 模拟各厂商原生协议，覆盖上下文投递、工具续答、取消、重连、异步播报排队（qwen 的 `realtime-provider-behavior.test.mjs` 做法，QW §2.6）。同一套用例对所有适配器跑，也能被 A、B 两个团队互相跑（架构 §4）。
6. **事件流不变式**：
   - 每个 `response.audio.delta` 带 `response_id` 和 `item_id`；
   - `cancel_response` 确认后该 `response_id` 零 delta；
   - 每个 `response.started` 恰好一个 `response.done`；
   - 每个 `tool.call` 在工具层最终有 `closed_by` 终态（KM 2.8、2.12）；
   - 每个原语超时都有事件，无悬挂。
7. **故障注入**：通过 BP B-X3（识别中、工具执行中、已回传未出声、出声中断开，各 20 次）：回合都有终态，写操作重复执行为 0，重连后重复调用为 0。
8. **错误归类**：历史错误日志回放后，未映射错误码占比可统计，并有降低的趋势；`fatal` 误判导致的放弃会话为 0。
9. **trace**：06 §3.6、§3.7、§3.10 的会话、判停、usage 字段能从标准事件直接得到（`reply_modality`、`server_vad_segments_in_turn`、`cached_input_tokens`、`rebuild_reason` 等）。
10. **级联 / 半级联**：护栏钩子在每句 TTS 之前被调用，`block` 后零音频；工具调用先于该回复的任何音频上报。

### 8.2 开放问题

1. **"回显"的含义**：本规范拆成助手输出转写和条目确认两种（§4.2）。如果指的是别的（例如 AEC 残留的回声识别），需要补字段。
2. **OpenAI server_vad 是否支持 `create_response=false`**：Pipecat 的 server_vad 模型里没有该字段（PC-B §3.3），LiveKit 按通用字段处理（LK-B §3.2）。【未确认】，影响我们是否能走 BP B.3 第 2 档。
3. **GPT-Live 的可变性和自动续答**：LiveKit、Pipecat 说不可变，qwen 读出 `mutableSession:T`；自动续答两份代码相反（KM 2.10）。可能不是同一产品或版本，要按我们实际接的版本测。
4. **按回复覆盖会不会同样让缓存失效**（PC-B §1.3；BP B.4）：决定状态块能否放在这里。
5. **单响应槽位**：OpenAI 的错误码说明会拒并发，qwen 的默认值却是 F，要测。
6. **未映射错误的默认类别**：本规范取 `transient` + 重连预算（§4.6 规则 7）。如果线上发现预算内的无效重建代价太大，要改成 `fatal`。
7. **say 的逐字性**：上游原生念读是否逐字（数字、专名），目前没有任何证据。不逐字的话，直念必须走本地 TTS，音色统一问题回到架构 §6-5。
8. **级联适配器是否内置 TTS**：本规范让级联适配器内置 TTS（§6.1），但 A 类的本地直念 TTS 在适配层之外（架构 §2.1）。两处共用同一个 TTS 服务和声音配置；如果将来要把 TTS 统一挪到仲裁器之后，`request_response` 在级联上就只出文本，要改 §6.2。
9. **半级联没有流式输入**：`whole_utterance` 意味着松键后才开始预填，首音代价未测（HC §0；KM 2.10 实验 E1）。
10. **Step-Audio 原生语音模式能否按文本截停**：决定它能否在同一上下文里承担 S2S 回合而不失去护栏（KM 2.10；架构 §3.2 推论二）。
11. **Doubao `input_audio_mute` 的语义**：是否等于"不听"，还是只是不送识别，【未确认】。
12. ~~`say` 是否需要成为独立原语~~ **已决定**（review B-6）：不设独立原语。契约里只有本地 TTS 一条念读通道；上游逐字念是实验，待 P0-13 通过（且 `response.say = native`）后在 `request_response.overrides` 里加 `verbatim_text` 字段承载，02 用 `upstream_stream` 承载其音频。
