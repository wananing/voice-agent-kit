# 输出仲裁器规范

> **开源说明**：本文是 voice-agent-kit 的契约规范之一，正文保留设计期原貌。代码注释里的"spec 0X §y"指本目录的规范。文中的出处简写（架构 / 两类架构、KM、BP、总结、HC、PC-B、LK-B、UM、`notes/*.md` 等）和 `../../knowledge-map.md` 一类路径指向作者未公开的调研笔记，对应内容已整理进 [voice-agent-handbook](https://github.com/wananing/voice-agent-handbook)，按 [README 的对照表](README.md#出处简写对照) 查。

> 状态：草案 v0.1（2026-09-29）。适用：A 类（无界面硬件）与 B 类（有界面移动端）两个 profile。
> 定位：`../voice-agent-two-architectures.md`（下称"两类架构"）§4 规定"仲裁器语义不允许分叉"，本文就是这份唯一的语义。实现可以有两份，本文的规则只有一份。
>
> 出处写法：KM x.y = `../../knowledge-map.md`；总结 §x = `../../../realtime-voice-agent-lessons-2026-09.md`；笔记 = `../../notes/*.md`。
> 标注：**【实测】** 我们自己测过；**【仓库】** 来自读代码或仓库默认值，没有跑过；**【建议值，需实测】** 本文给的起始值，上线前要用 trace 校准；**【本规范约定】** 调研里没有直接对应，是本文为补齐语义做的规定。
>
> 本文不写代码。schema 用 YAML 示意，字段名是契约，编码格式（JSON / protobuf）由各实现决定。
>
> 相关规范：术语与枚举总表 `00-glossary.md`；回合与客户端协议 `01-turn-model-and-client-protocol.md`（下称 01）；工具收口 `03-tool-registry-and-call-closure.md`（03）；链路适配 `04-upstream-adapter-capabilities.md`（04）；上下文与会话 `05-context-and-session.md`（05）；trace `06-trace-and-evaluation.md`（06，trace 字段权威表）。

---

## 1. 目的、范围与术语

### 1.1 目的

回答一个问题：**任一时刻，谁可以向用户出声，出多少，被打断后怎么办。**

要保证的四件事：

1. 同一时刻只有一个输出在出声（双声 = 0）。依据：总结 §3.3 实测双声 0；LiveKit 同一时刻只授权一个 SpeechHandle（`notes/livekit-agents.md` §1）。
2. 用户打断或回合切换后，旧输出一帧都不再出声，包括上游迟到的帧。依据：KM 3.3"保留代际（Pipecat 的一次性清空不防迟到帧）"。
3. 非本回合的输出（事件播报、后台结果、进度）只在不打扰用户的时候插话，且"不打扰"以客户端真实播放为准。依据：KM 3.3、2.11；`notes/qwen-audio-agent.md` §2.3。
4. 任何一条信号丢失（回执丢失、上游不回应），仲裁器都不会卡死，也不会无限重播。依据：KM 2.11 回执兜底。

### 1.2 范围

**在范围内**：输出请求的接收、排队、授权、冲刷、丢弃、重试；插话窗口的判定；对下游（设备 / 客户端 / 本地 TTS / 上游适配）发出的控制指令；写进 trace 的仲裁事件。

**不在范围内**：

- 判停和打断**检测**（谁算"用户开口"）——归回合状态机（KM 2.4、2.5）。仲裁器只消费它的事件。
- 说什么——归模型、工具层、事实通道。仲裁器不改内容。
- 上下文截断的**执行**——归上下文 / 会话层（KM 3.4、2.5）。仲裁器只提供"用户实际听到了什么"（回执中的 `played_ms`）。
- 设备事件的去抖、幂等、定性——归事件线（两类架构 §2.4 第 6 条）。进入仲裁器的已经是定性后的请求（§4.3）。

### 1.3 术语

| 术语 | 定义 | 出处 |
|---|---|---|
| **来源（origin）** | 产生这条输出的组件。取值见 §2.2。用于按来源冲刷、按来源统计。 | qwen 每条 response 带 `origin`（`notes/qwen-audio-agent.md` §2.4 第 3 点） |
| **种类（kind）** | `cue` / `speech` / `narration` 三选一，决定是否需要插话窗口、是否被冲刷、被打断后的默认策略。 | 总结 §2.2；两类架构 §2.2 |
| **代际（generation）** | 会话内单调递增的整数（01 §1.3 的 `gen`）。每条输出和每个下行帧都带一个代际；**低于当前代际的一律丢弃，不看来源**。何时 +1 见 §3.2。 | 总结 §2.2；KM 3.3 |
| **话筒租约（lease）** | "我现在可以出声"的凭证，由仲裁器发放，绑定一个代际。持有者出声前必须持有**当前代际**的租约；长生命周期任务每次出声前重新领取。与"播报领取"（§3.3，跨连接防重复播报）、"前台租约"（05 §8.3，会话归属）是三件事，见 00 术语表。 | 总结 §2.2（话筒租约）；qwen 跨连接领取（`notes/qwen-audio-agent.md` §2.3） |
| **插话窗口（barge-in window）** | 非本回合输出（narration）可以开始出声的时机。定义见 §3.4。 | qwen `AnnouncementWindow`（`notes/qwen-audio-agent.md` §2.3） |
| **回执（playback receipt）** | 客户端上报的播放事实：`playback.started / ended / interrupted {turn_id, sentence_id, played_ms}`（线上格式见 01 §3.1.6）。**送达以"开始播放"为准，不以服务端生成完为准。** | KM 2.11（事件名以此为准）；两类架构 §2.2 |
| **冲刷（flush）** | 让某个范围内的输出立刻停止并清空其排队部分，保证"之后零音频"。对设备 / 客户端的线上帧是 `flush.start / flush.end`（01 §3.2.5，`flush.end` 由网关发出）；对本地 TTS 是 `tts_flush` / `flush_end`（TTS 回的确认）。 | TEN TTS2 flush 契约（`notes/ten-and-deployment.md` Q4） |
| **授权（grant）** | 仲裁器把话筒交给某条请求的动作。同一时刻至多一个有效授权。 | LiveKit 串行调度（`notes/livekit-agents.md` §1） |

---

## 2. 输入模型：输出请求

### 2.1 schema

每一个"想出声"的东西都以一条 **输出请求（OutputRequest）** 进入仲裁器。模型回复、本地直念、提示语、事件播报、委派结果都一样（两类架构 §1 原则 5）。

```yaml
OutputRequest:
  request_id: "req-7f3a"          # 必填，会话内唯一。下游二次过滤的键（§3.9）
  origin: tool_result             # 必填，见 §2.2
  kind: speech                    # 必填，cue | speech | narration
  priority: normal                # 必填，critical | high | normal | low（§2.3）
  generation: 12                  # cue / speech 必填：发起时的当前代际
                                  # narration 省略：授权时由租约盖章（§3.3）
  turn_id: "c41"                  # cue / speech 必填（格式见 01 R2.2-2）；narration 可空（授权时绑定当时的回合）
  interruptible: true             # 默认 true；false 仅用于白名单内的 system_notice / 安全话术模板，限长 ≤ 3 s（§3.2）
  on_interrupt: drop              # drop | retry。用户打断一律按 drop 处理（§3.6）
  max_retries: 0                  # on_interrupt=retry 时生效
  expires_at: "2026-09-29T10:15:00+08:00"   # 可空。过期未开始播即丢弃
  dedupe_key: "task-88:result"    # 可空。同键去重规则见 §3.8
  supersedes: "task-88:progress"  # 可空。入队时替换同键的未开始请求（新覆盖旧）
  context_policy: after_played    # none | before_speak | after_played（§2.5）
  content:                        # 三选一
    type: text_tts                # prerecorded | text_tts | upstream_stream
    text: "到了，下一站是……"
    voice: default                # 统一音色（两类架构 §3.2 推论一）
  ui:                             # 仅 B 类，可空（§4.5）
    transcript: sentence_aligned  # sentence_aligned | none
    card_ref: "card-203"          # 卡片可早于语音上屏，不受音频对齐约束
  trace:                          # 由调用方填，仲裁器透传
    tool_call_id: "call_91"
    task_id: "task-88"
```

三种内容形态的差异：

| `content.type` | 典型来源 | 时长是否可知 | 冲刷时要做什么 | 出处 |
|---|---|---|---|---|
| `prerecorded`（预合成音频，本地库或缓存） | cue、固定问候、错误话术 | 入队时已知 | 下游停播、丢缓冲 | 本地提示语库（总结 §3.3；KM 2.11） |
| `text_tts`（文本，由本地 TTS 合成） | 逐字念的工具结果、到点播报、委派结果 | 合成后按 chunk 累计可知 | TTS flush + 下游停播 | 总结 §5.5；KM 2.9 时间戳做法 |
| `upstream_stream`（上游音频流，S2S 直出） | 模型回复 | 流结束才知道 | 上游 cancel + truncate + 下游停播 + 迟到帧过滤 | KM 2.5；`notes/qwen-audio-agent.md` §2.5 |

`upstream_stream` 额外携带上游的 `response_id` 和 `item_id`（字段名同 04 §4.1、§4.3 事件信封），用于 cancel、truncate 和迟到帧识别（§3.9、§4.4）。

### 2.2 来源（origin）取值

契约只规定取值集合，不规定每个 profile 必须全部出现。

| origin | 默认 kind | 默认 priority | 说明 |
|---|---|---|---|
| `model` | speech | normal | S2S / 文本 LLM 的回复 |
| `tool_result` | speech | normal | 本回合内由我们直念的结果（A1，KM 2.8） |
| `delegated_result` | speech 或 narration | normal | 委派结果：本回合内返回算 speech；回合已结束才到算 narration（A2，KM 2.8） |
| `cue` | cue | high | 旁路提示语，不进模型（总结 §3.3） |
| `task_progress` | narration | low | 长任务进度（§4.3 节流） |
| `task_result` | narration | normal | 后台任务结果、秒级写操作的结果（A3，KM 2.8） |
| `reminder` | narration | normal | 定时器 / 到点（qwen `reminder.due`，`notes/qwen-audio-agent.md` §2.4 第 6 点） |
| `device_event` | narration | normal | 传感器、到达、NFC 等（A 类，两类架构 §2.4） |
| `system_notice` | speech | high | 回合级错误话术、沉默填充（两类架构 §2.4 第 1 条） |
| `system_exit` | speech | critical | 转人工、挂断、紧急出口（B 类，两类架构 §3.4 第 6 条） |

### 2.3 优先级语义

- **优先级只决定排队顺序，不决定抢占。** 除 `critical` 之外，任何请求都不能打断正在出声的授权；它们只能排队（LiveKit LOW/NORMAL/HIGH 也只影响调度顺序，`notes/livekit-agents.md` §1）。
- **同优先级按入队顺序（FIFO）。**
- **`critical` 等同一次"系统发起的打断"**：代际 +1，按 §3.5 冲刷，然后立即授权自己（对应 qwen `AgentDelivery.interrupt` 的"先 cancel 再注入"，`notes/qwen-audio-agent.md` §2.4 第 7 点；qwen 仓库里该模式没有使用方，所以这里的行为是【本规范约定】）。
- 种类的约束优先于优先级：一条 `high` 的 narration 仍然要等插话窗口；一条 `low` 的 speech 不需要窗口。

### 2.4 种类的默认属性

| 属性 | cue | speech | narration |
|---|---|---|---|
| 绑定回合 | 是（当前回合） | 是（当前回合） | 否（授权时绑定） |
| 需要插话窗口 | 否 | 否 | **是** |
| 打断时被冲刷 | **否**（已在播的放完；未开始的仍受代际过滤，见 §3.5） | 是 | 是 |
| 被用户打断后 | drop | drop | drop |
| 非用户原因中断后 | drop | drop | 按 `on_interrupt`：进度 drop，结果 / 提醒 retry |
| 进上下文 | 否（`context_policy=none`，总结 §3.3） | 按来源 | 按来源 |

依据：总结 §2.2（cue 不冲刷，speech / narration 冲刷）；`notes/qwen-audio-agent.md` §2.4 第 5 点、§2.5（被打断不重播）、§3.2（进度丢弃、结果有上限重试）。

### 2.5 context_policy

"写进上下文"和"出声"分开记账（`notes/qwen-audio-agent.md` §3.2）。仲裁器不写上下文，只在对应时刻通知上下文层：

- `none`：永远不通知。cue 固定为此值。
- `before_speak`：入队即通知（事实先可知、话后说；用户在播报前追问时模型能答）。适用于结果类 narration。
- `after_played`：收到 `playback.ended` 或 `playback.interrupted` 时通知，并附 `played_ms`，上下文层据此只记"用户实际听到的"（两类架构 §1 原则 2；总结 §5.5 "播完后以成对问答形式补记"）。上下文层是否再把它补记进**上游**会话，见 05 §3.6（已决定分通道：与工具调用相关的"自己的话"经 function output 的 `spoken_to_user`；与调用无关的 narration 只写本地，要紧事实进状态块懒生效层；上线前先跑 B-X2，跑完前线上保持总结 §5.5 的成对问答补记），不归仲裁器。

重试不重复通知（qwen `batch.contextInjected`，`notes/qwen-audio-agent.md` §2.3）。

---

## 3. 仲裁规则

### 3.1 串行授权

- 仲裁器维护**一个话筒**和**一个有界等待队列**。话筒要么空闲，要么被恰好一个授权持有。
- 话筒空闲时，从队列里按 §2.3 的顺序取第一个**可授权**的请求。可授权 = 代际不低于当前代际 ∧ 未过期 ∧（kind≠narration ∨ 插话窗口打开）。
- 被跳过的 narration 留在队列里，不阻塞后面的 cue / speech。
- 授权在以下任一情况结束：收到该请求最后一句的 `playback.ended`；收到 `playback.interrupted`；被冲刷；回执超时（§3.7）。
- **结束授权 ≠ 窗口打开**：speech 结束后回合可能仍未终结（例如还在等工具续答），narration 依然要等（§3.4）。
- 队列有界。超过上限时丢弃优先级最低、入队最早的请求并记 trace（总结 §2.2"队列有界"）。上限：每会话 32 条【建议值，需实测】。

**授权的是"出声"，不是"生成"。** 上游可以提前生成、本地 TTS 可以提前合成；但只有持有授权的请求能向下游发音频帧。这让"提示语和正文排队、不串行等待"成立（总结 §3.3：结果排在提示语后几乎不等待，P50 0，P90 0.2 s【实测】）。

### 3.2 代际语义

**当前代际 `G` 何时 +1**（只有这三种）：

1. **新用户回合开始**：回合状态机发出 `turn.start`（任一来源：button / wake / vad / text，01 §2.3）。被 01 §2.6 合并进 `holding` 回合的 `turn.start`（下发 `turn.merge`）不算新回合，不 +1。
2. **用户打断**：回合状态机判定 barge-in（按键模式只认按键，KM 2.5）。
3. **critical 抢占**（§2.3）。

打断与新回合同时发生（01 §5.1：`abort` + `turn.start` 同一 tick；01 §5.2：VAD 打断后下发 `turn.start`）时只 +1 一次，状态机只发一个事件（`turn.start` 或 `user.barge_in` 其一）。

以下情况**不** +1：回合正常结束、工具调用返回、会话重建（发生在回合之间，KM 3.2）、narration 开始或结束。

**过滤规则**：代际比较发生在三处，任一处 `request.generation < G` 就丢弃：

1. **入队时**：迟到的请求直接丢弃，记 `dropped_stale`。
2. **授权时**：排队期间代际变了的，丢弃。
3. **每个下行帧发出时**：已授权但生成中的输出（尤其 `upstream_stream`），每一帧都带代际，发出前比较。

**上游迟到帧**：上游 cancel 之后仍可能推来的音频 delta（Pipecat 不过滤、TEN S2S 版不过滤，`notes/pipecat.md` Q4、`notes/ten-and-deployment.md` Q2；是否真会发生待 A-E6 验证），因为它所属的请求代际已低于 `G`，由第 3 处过滤丢弃。适配层同时对已 cancel 的 `response_id` 打 `suppressed` 标记（qwen，`notes/qwen-audio-agent.md` §2.5），两者并存：代际是主机制，`suppressed` 用于 trace 计数。

**narration 的代际**：narration 在入队时没有代际；授权时由租约盖上**当时的** `G`（§3.3）。所以一条两分钟前产生的后台结果不会因为"出生代际旧"而永远播不出去，但它一旦开始播放，就和 speech 一样受代际约束。

**不可打断（`interruptible=false`）**：只允许**白名单内的 `system_notice` / 安全话术模板**使用（例如"请牵好大人的手"；critical 的收尾话术"正在为您转人工"也在白名单内），单条限长 ≤ 3 s【建议值，需实测】，与 01 §3.2.3 一致（review B-7-2）。它不阻止代际 +1，但冲刷时跳过它（已在播的放完），它的帧按"盖章代际 = 冲刷后的新代际"发出。白名单外的来源、或超过限长的请求传 `false` 时仲裁器按 `true` 处理并记 warning【本规范约定】。理由：LiveKit 的 `disallow_interruptions()` 在 S2S 服务端判停下会被忽略（KM 2.5），我们不依赖"不可打断"保护写操作，写操作靠执行前查代际（总结 §5.3）。

### 3.3 话筒租约

- **租约 = (lease_id, generation)**，由仲裁器在授权时签发。持有者向下游发帧时必须带租约；仲裁器和下游都按租约里的代际过滤。
- **长生命周期任务出声前必须重新领取。** 事件分发循环、后台任务的回帖协程、定时器，这些对象活得比一个回合长。它们不得缓存租约；每次要出声都以一条新的 OutputRequest 入队，授权时拿到当前代际的新租约。反例是总结 §2.2 的 bug："每个连接只有第一轮有声音"——任务拿着第一轮的过期租约出声，被过滤。
- **跨连接的播报领取**（claim，防重复播报；05 §8.3 第 2 条是同一机制）：同一条 narration（按 `dedupe_key`）在多个前台连接或断线重连之间只能被一个连接领取。领取 TTL 60 s，每 TTL/3 续期；连接断开只释放领取，不算送达，重连后可重新领取（qwen，`notes/qwen-audio-agent.md` §2.3【仓库】）。A 类单设备单连接时退化为"重连后重新领取"。

### 3.4 插话窗口

**窗口关闭 ⇔ 以下任一成立：**

1. **用户在说**（回合状态机的 `user_speaking`；按键模式下 = 按键按住）。
2. **回合未结束**（`turn_pending`）：用户开口时置位；只有当该回合的回复已结束，**且**（没有音频 ∨ 音频已收到 `playback.ended`），**且**不在等工具续答时才清除。所以"模型在说话"和"工具回合没完"都挡住 narration。
3. **有未播完的音频**：存在已下发、但还没收到 `playback.ended` / `playback.interrupted`（或已按 §3.7 判超时）的输出。

三者都不成立，窗口才打开。依据：qwen `windowBlocked`（`notes/qwen-audio-agent.md` §2.3）；KM 3.3。

**关键约定**：

- **以客户端回执为准。** "服务端生成完"或"服务端发完"不算播完。A 类设备有 ≤1.2 s 的浅缓冲、服务端超前 200–500 ms（两类架构 §2.2），不以回执为准就会把 narration 插进一句话的尾巴。
- **窗口打开后再静默一小段才授权**：350 ms（qwen `announcementQuietMs` 默认值【仓库】；【建议值，需实测】，儿童场景可能要更长）。
- **被挡住的 narration 每 1 s 自唤醒检查一次**（qwen【仓库】），而不是只靠事件驱动——事件本身可能丢。
- **按种类收紧**：进度（`task_progress`）比结果更严，窗口打开之外还要求"会话 ready 且没有任何在途输出"（qwen 进度的 `busy` 条件，`notes/qwen-audio-agent.md` §2.4 第 4 点）。
- 判停进行中（开放麦下，VAD 已停但语义判停未决）视为"用户在说"（LiveKit `wait_for_idle` 含"判停不在进行"，`notes/livekit-agents.md` Q3）。信号取 01 R2.3-4 的网关内部事件 `turn.endpointing{turn_id, active}`：`active=true` 期间窗口条件 1 成立。

### 3.5 冲刷规则

**触发**：代际 +1 的三种情况（§3.2），以及按来源的定向冲刷（例如任务结束时清掉它排队中的进度，qwen `cancelResponses(predicate)`，`notes/qwen-audio-agent.md` §2.4 第 2 点）。

**范围**：

| | 正在播（持有授权） | 排队中、未授权 |
|---|---|---|
| cue | **不冲刷**，放完 | 低代际的被 §3.2 过滤丢弃 |
| speech | 冲刷 | 丢弃 |
| narration | 冲刷，按 §3.6 判定是否重试 | 保留在队列里（它们不属于任何回合），等下一个窗口 |

cue 不冲刷的理由：很短、本地播放（总结 §2.2）。**cue 的长短由工具层选择（03 §2.1 `cue`：`long / short / none`），仲裁器不抢占**：同回合的结果 speech 不打断正在播的 cue，只排在它后面；"工具在 cue 开播前已完成就不播 cue"由工具层在 cue 授权前按定向冲刷撤回实现，不是仲裁规则（review B-1）。"低代际未开始的 cue 仍丢"是【本规范约定】：一个旧回合的"我查一下路线"在新回合开头冒出来没有意义。A 类按键时设备本地先停播、不等服务端（KM 2.11），这是设备协议行为，发生在仲裁器之前；它和"服务端不为 cue 发冲刷"不矛盾。

**动作序列（对 speech / narration 的持有者）**：

1. 仲裁器撤销授权，代际已 +1（若是回合级冲刷）。
2. `upstream_stream`：通知上游适配 cancel，并按回执换算的播放位置发 truncate（§4.4）。
3. `text_tts`：向本地 TTS 发 `tts_flush(flush_id)`。
4. 向下游（设备 / 客户端）发冲刷指令：线上帧是 `flush.start{flush_id, turn_ids}`，随后 `flush.end`、`tts.stop{interrupted}`、`turn.closed`（01 §3.2.5；v1 客户端把 `tts.stop{interrupted}` 当冲刷完成，两类架构 §2.2）。线上帧按 `turn_id` 冲刷，网关内部按 `request_id` 记账。
5. 等下游 `flush_end`（本地 TTS 回的确认；设备侧没有确认帧，以网关清空自己的发送队列、发出 `flush.end` 为准，设备的 `playback.interrupted` 只提供播放位置，见 01 R3.2-3）。**契约：`flush_end` 之后，该 `flush_id` 覆盖的 request_id 零音频**（TEN TTS2，`notes/ten-and-deployment.md` Q4）。
6. 等待期间话筒不授权给普通请求；critical 除外（它就是触发者）。等待上限 1 s，超时视为已停止、记 `flush_timeout`，仍放行【建议值，需实测】（参照 qwen 单槽位取消 `responseCancelGraceMs` 1 s 后重连，`notes/qwen-audio-agent.md` §2.5）。

### 3.6 被打断后：不重播 vs 有上限重试

判定表（第一个命中的行生效）：

| 条件 | 结果 | 出处 |
|---|---|---|
| 中断原因是**用户打断**（`reason=user_interruption`） | **视为已送达，不重播**，无论 kind 和 `on_interrupt` | qwen：结果和进度被用户打断都视为已送达（`notes/qwen-audio-agent.md` §2.5）；进度注释 "Replaying the same monologue would be more disruptive than omitting it"（§2.4） |
| kind = cue 或 speech | drop | speech 属于已结束的回合；上下文由截断处理 |
| `on_interrupt = drop`（进度默认） | drop | qwen §3.2 |
| 已过 `expires_at` | drop，记 `expired` | KM 2.11（到点播报要有过期时间） |
| 已被 `supersedes` 覆盖或同键已送达 | drop，记 `duplicate` | §3.8 |
| 重试次数已达 `max_retries` | drop，释放领取，记 `retry_exhausted` | qwen 最多 8 次（`notes/qwen-audio-agent.md` §2.3【仓库】） |
| 其余（非用户原因：critical 抢占、连接断开、回执超时、系统冲刷） | 重新入队，重试计数 +1 | 同上 |

默认 `max_retries`：结果 / 提醒 = 8（沿用 qwen【仓库】；A 类是否太多【建议值，需实测】），进度 = 0。

**已知取舍**：用户打断发生在"上下文已注入（`before_speak`）但还没开始播"时，也按已送达处理——用户可能一句没听到，但模型上下文里有，可以追问（qwen 同样如此，`notes/qwen-audio-agent.md` §2.5）。本规范接受这个取舍；需要"一定要让用户听到"的内容不应走 narration，而应上屏（B）或在下一回合由模型带出。

### 3.7 回执丢失的兜底

回执丢失或客户端节流会让窗口永远关着（KM 2.11）。三道兜底：

| 机制 | 规则 | 数值 |
|---|---|---|
| **自唤醒** | 队列里有被挡住的 narration 时，定时重检窗口，不依赖事件到达 | 1 s（qwen【仓库】） |
| **开始回执超时** | 下发完成后在期限内没收到 `playback.started`：视为未送达；narration 按 §3.6 重试（若仍被挡则继续推迟，**绝不叠出第二段**）；speech 不重试，只记 trace | qwen：`response.done` 后 120 s（【仓库】）。A 类 120 s 过长，建议 `下发完成 + 5 s`【建议值，需实测】 |
| **结束回执超时** | 下发完成后迟迟没有 `ended`：与 01 §2.5 `T_drain` 同一口径，`下发完成 + 剩余未播时长 + 2 s` 后视为播完，释放"未播完音频"这一项窗口条件，记 `receipt_missing` | A、B 相同：剩余未播时长 + 2 s（= 01 `T_drain`）【建议值，需实测】 |
| **重试上限** | 见 §3.6 | 8 |

`upstream_stream` 在流结束前时长未知，结束回执超时从流结束（下发完成）时刻开始算。

客户端不支持回执时（`hello` 能力协商里声明，两类架构 §2.2），仲裁器退化为"按下发时长 + 缓冲上限估算播放结束"，并在 trace 里标 `receipt_mode=estimated`。这是降级，不是等价方案。

### 3.8 同回合去重

- **同一回合、同一 `dedupe_key` 只授权一次**，后到的直接丢弃，记 `duplicate`。典型：同一用户回合里模型发起了两次同一个后台任务，结果回来两份（qwen"同一用户回合只认第一个发起 spawn 的 response"，`notes/qwen-audio-agent.md` §3.2；工具层对应 `closed_by=duplicate`，KM 2.12）。
- **跨回合的同键**：已送达的 `dedupe_key` 在会话内记住，再来的丢弃（防重连后重复播到点提醒，qwen 跨连接领取，§3.3）。
- **`supersedes`**：入队时如果队列里有同键且未开始的请求，替换它（进度"新覆盖旧"，§4.3）。已开始播放的不替换。
- 模型层的"说过就输出空"（LiveKit 回帖指令，`notes/livekit-agents.md` Q3）是另一层去重，属于内容生成，不归仲裁器；仲裁器只保证不重复授权。

### 3.9 消费端按 request_id 的二次过滤

中心仲裁器之外，每个消费端（设备下行出口、本地 TTS、B 类客户端播放器）独立做一道过滤，作为第二道防线（TEN 在 TTS 端按 `request_id` 丢迟到输入，`notes/ten-and-deployment.md` Q4；小智设备端二级代际防"同回合 abort 后残余包"，KM 2.11）：

- 消费端维护 `current_generation` 和 `closed_request_ids`（已冲刷或已结束的 request_id 集合，按会话保留最近 N 个，N=256【建议值，需实测】）。
- 收到帧时：`generation < current_generation` 丢；`request_id ∈ closed_request_ids` 丢。
- 收到冲刷时：先把覆盖的 request_id 加入集合、清缓冲，再回 `flush_end`（本地 TTS 等网关内消费端；设备 / 客户端不回确认帧，只在仍在播时发 `playback.interrupted`，01 R3.2-2）。
- 丢弃计数上报（进 trace 的"迟到帧"统计，§7）。**消费端丢了帧，说明中心过滤漏了，这是缺陷信号，不是正常路径。**

---

## 4. 与其他组件的接口

### 4.1 回合状态机

| 方向 | 事件 | 仲裁器动作 |
|---|---|---|
| 状态机 → 仲裁器 | `turn.start {turn_id, source}` | 代际 +1；冲刷；`turn_pending=true`；`user_speaking=true` |
| 状态机 → 仲裁器 | `user.barge_in {turn_id, source}` | 代际 +1；冲刷（中断原因 `user_interruption`） |
| 状态机 → 仲裁器 | `user_speaking` 变化、判停进行中（`turn.endpointing{turn_id, active}`，01 R2.3-4） | 更新窗口条件 1 |
| 状态机 → 仲裁器 | `turn.awaiting_tool {turn_id, bool}` | 更新窗口条件 2 的"等工具续答" |
| 状态机 → 仲裁器 | `turn.reply_done {turn_id, outcome}`（回复已结束，不再有新输出；含失败 / 取消） | 清 `turn_pending`（仍需音频播完） |
| 仲裁器 → 状态机 | `turn.audio_drained {turn_id}` | 该回合所有 speech 已收到结束回执或已判超时；状态机据此判定"agent 播完"，回合才能到终态（KM 3.2：回合从开口到 agent 播完） |
| 仲裁器 → 状态机 | `turn.no_output {turn_id}` | 回合已结束但没有任何输出被授权；交给看门狗决定是否发 `system_notice`（两类架构 §2.4"沉默即故障"） |

仲裁器**不**自己决定回合终态，也不自己发看门狗话术；话术作为 `system_notice` 请求回到仲裁器。

上表是网关内部事件，不是线上帧。与 01 回合状态机的对应：`turn.start` = 01 `turn.start`（上下行）；`user.barge_in` = 01 §5 的打断（进入 `interrupted` 终态前）；`turn.reply_done` = 01 `responding → draining`（`tts.stop{done}`）或进入 `failed` / `cancelled`；`turn.audio_drained` = 01 `draining → completed` 的依据，之后网关发 `turn.closed`（01 §2.4）。

### 4.2 播放回执

- 事件：`playback.started / ended / interrupted {turn_id, sentence_id, request_id, played_ms, reason}`。`request_id` 是本规范要求在 KM 2.11 字段之上加带的（没有它，同回合的 cue 和 speech 无法区分）【本规范约定】。线上回执（01 §3.1.6）不带 `request_id`，由网关按 `(turn_id, sentence_id)` 查出后补上再交给仲裁器（`sentence_id` 在回合内对所有 kind 统一编号，01 §1.3）；trace 里的 `playback_receipts[]` 带补上的 `request_id`（06 §3.12）。
- `played_ms`：`interrupted` 时必填，是截断的依据（KM 2.5）；`ended` 时填整句时长。
- 回执按句上报。仲裁器以"该请求最后一句的 `ended`"作为请求结束。
- B 类走 WebRTC 时回执经数据通道上报；A 类走设备协议。字段一致。

### 4.3 工具层

**narration 的来源**：工具层（含后台任务、委派）是 narration 的主要来源。入仲裁器之前，工具层 / 事件线按 `handle / context / respond / interrupt` 四类定性（qwen `AgentDelivery`，KM 3.3）：

| 定性 | 进仲裁器吗 | 形式 |
|---|---|---|
| handle | 否 | 静默处理 |
| context | 否（只写上下文） | — |
| respond | 是 | narration，等窗口 |
| interrupt | 是 | `priority=critical` |

**本回合内的工具结果**（A1 逐字念）：以 speech 入队，排在同回合的 cue 后面（总结 §3.3）。工具层回传前查代际，过期的不入队，工具调用按 03 §4.2 / §5.1 的规则收口（`notes/qwen-audio-agent.md` §2.5；KM 2.8"回传前查代际"）。打断（含新回合打断旧回合）一律记 `interrupted` / `cancelled`，按 03 §5.1 取值；`superseded` 只用于会话重建和 `on_duplicate=replace`（03 §4.2）。

**进度（`task_progress`）节流参数**（qwen `progress-announcement-manager`，`notes/qwen-audio-agent.md` §2.4 第 5 点【仓库】；KM 2.8"A3 和长任务播报直接沿用"）：

| 参数 | 值 | 说明 |
|---|---|---|
| 首条延迟 | ≥ 60 s | 任务开始 60 s 内不播进度 |
| 全局最小间隔 | ≥ 60 s | 跨任务共用一个 `last_announced_at` |
| 覆盖 | 新覆盖旧 | 同任务用 `supersedes="{task_id}:progress"` |
| 静默防抖 | 等进度文本静默 `quiet_ms` 再播，防抖有上限 | 具体值 qwen 未在笔记中给出【建议值，需实测】：quiet 2 s，上限 10 s |
| 来源 | 只看后台协议消息，不从工具活动推断 | 工具活动只驱动 UI / 灯效 |
| 清除 | 任务完成、失败、取消，或出现权限 / 输入请求时，移除该任务的排队进度 | 工具层发定向冲刷 |
| 被打断 | 不重播 | §3.6 |

节流由工具层做，仲裁器只执行 `supersedes` 和定向冲刷；仲裁器不理解"进度"的业务含义。60 s 在 A 类（沉默 > 2 s 即故障）下对"第一句"来说太长——前 60 s 的"有声音"靠 cue 和收口时的承接句，不靠进度【本规范约定】。

### 4.4 上游适配

仲裁器对上游只发两个原语，即 04 §3.5 `cancel_response` 与 §3.6 `truncate`，由适配层按能力声明翻译（KM 2.10 能力位；`notes/qwen-audio-agent.md` §3.2）：

| 原语 | 参数 | 适配层行为 |
|---|---|---|
| `cancel_response(response_id)` | — | 发 `response.cancel`；单槽位厂商超过 1 s 未释放则断开重连（qwen【仓库】；重连与否由重建器决定，04 §3.0 "不重试"） |
| `truncate(item_id, audio_end_ms, played_text?)` | `audio_end_ms` = 01 §5.5 的截断位置（`offset_ms(sentence_id) + played_ms`，换算到该条目内），不是单句的 `played_ms`；`played_text` 由上下文层按 05 §2.1 `interrupt_at` 算出 | 按 04 §3.6 的 `truncate.mode` 降级；`audio_end_ms=0` 时删除该条；不支持截断的厂商（Gemini、AWS，KM 2.5）返回 `unsupported`，只记 trace |

- 适配层给每个上游音频帧打 `request_id` 和代际，交给仲裁器过滤；取消后到达的事件标 `suppressed`。
- `audio_end_ms` 以客户端回执为准，不用墙钟估算（墙钟估算偏大，KM 2.5）；回执缺失时按 01 §5.5 用网关估计并标 `truncate_estimated`。
- 适配层不得绕过仲裁器直接向下游写音频。

### 4.5 UI 同步（B 类）

B 类多一个输出面：屏幕。B 类 UI 同步另立规范 07（owner 待指定），在此之前以本节为准（01 §1.2 同此）。约定：

- **转写上屏与音频按句对齐**：每句转写带 `request_id`、`sentence_id`、代际，**在该句 `playback.started` 时才上屏**，不在生成时上屏（unmute 按播放时刻释放文本，`notes/unmute.md` Q3；LiveKit `synchronized_transcript`，`notes/livekit-agents-B.md`；两类架构 §3.2）。
- **冲刷时**：已上屏的句子按 `played_ms` 标记为"被打断"（截断显示或加标记由 UI 决定）；未上屏的句子丢弃。上屏内容和上下文层记的内容一致（两类架构 §1 原则 2）。
- **卡片 / 结构化字段不受音频对齐约束**，可以早于语音上屏（两类架构 §3.3"有内容（屏幕）可早于语音"）。它们不经过话筒授权，但同样带代际，低代际的卡片更新由客户端丢弃。
- UI 事件（上屏时刻）回报给 trace（06 §3.11 `ui_events[]`、`ui_audio_skew_ms`；两类架构 §4）。
- `system_exit`：先上屏出口状态（转人工中 / 已挂断），再出声；挂断时语音可以只保留一句，屏幕是主通道。

---

## 5. 状态机与时序

### 5.1 仲裁器状态机

```
             ┌──────────── grant(req) ────────────┐
             │                                    ▼
          [IDLE] ◄── ended/超时/无可授权 ── [GRANTED(req, lease)]
             ▲                                    │
             │                         flush 触发 │ (代际+1 / 定向 / critical)
             │                                    ▼
             └──── flush_end / 1 s 超时 ──── [FLUSHING(flush_id)]
                                                  │
                                    critical 请求 ─┘→ 直接 GRANTED(critical)
```

请求生命周期：

```
queued ──grant──► granted ──首帧下发──► sent ──started──► playing ──ended──► done
  │                 │                    │                   │
  │                 └── 代际过期 ─────────┴──── flush ────────┴──► interrupted
  │                                                                  │
  ├── 过期 / 代际过期 / 重复 / 队列溢出 ──► dropped                     ├─ 用户原因 ─► done(视为已送达)
  └── (narration) 窗口关 ──► 留在 queued，1 s 自唤醒                     └─ 非用户原因 ─► retry_wait ─► queued | dropped(retry_exhausted)
```

### 5.2 正常回合（S2S 直出）

```
t     回合状态机          仲裁器                         上游/TTS               客户端
0.00  turn.start(c41)     G=12→13, turn_pending
      turn.end(c41)
0.9x                      入队 speech(model, G13)         response 开始出音频
                          话筒空闲 → grant(lease G13)     帧(req-a, G13) ──────► 播放
                                                                              playback.started(req-a)
                                                                              …
                                                          流结束
                          等结束回执
                                                                              playback.ended(req-a)
                          授权结束；turn.audio_drained ──► 状态机
      turn.reply_done(c41) turn_pending 清除 → 窗口打开（350 ms 静默后可插 narration）
```

### 5.3 工具回合：提示语 + 结果排队

```
t     事件                                   仲裁器
0.80  模型给出 function call（总结 §3.4）       入队 cue(req-c, G13) → grant，本地播短 cue"嗯，"（A1 默认 `cue: short` ≤ 300 ms，03 §2.1）
                                             turn.awaiting_tool = true（窗口条件 2 保持关闭）
0.81  工具执行完成（1–50 ms）                   工具层查代际 = 13，通过
      A1：结果本地直念                          入队 speech(tool_result, req-r, G13) —— 话筒被 cue 占着，排队
      （同时向上游收口，不再生成，KM 2.8）
1.1x  playback.ended(req-c)                  cue 授权结束 → grant(req-r)，结果紧接提示语播出（固定前缀预合成，本地 TTS 首包不进等待）
      playback.ended(req-r)                  turn.audio_drained
      turn.awaiting_tool = false; turn.reply_done 窗口打开
```

变体：**长 cue**（A2 / A3 默认，或 A-E2 的长 cue 对照组）。cue 为"我查一下路线。"（约 0.76 s），`playback.ended(req-c)` 在 1.6x，结果在约 1.6 s 才出声（P50 等待 0【实测】是在"结果晚于 cue 播完"时测的，06 §2.4）。

变体：**工具在 cue 开播前已完成**。工具层在 cue 授权前按定向冲刷撤回 cue，结果直接授权，有声音 = 有内容。

变体：**工具在跑时用户打断**。`user.barge_in` → G=14；cue 若在播则放完；工具返回后查代际 13 < 14，不入队，调用按收口规则关闭（KM 2.5 权威表"我们"一列：未回传则不回传；已回传则补 `{"interrupted": true}`）。

变体：**A3 秒级写工具**。收口时模型说承接句（speech）；结果后到，以 `task_result` narration 入队，等窗口。

### 5.4 事件播报插话

```
t      事件                                    仲裁器
0      定时器到点 → reminder，定性 respond       入队 narration(req-n, dedupe_key=rem-5, expires_at=+10 min)
       此时模型正在长回答（speech req-a 在播）     窗口关闭（条件 3：有未播完音频）→ 留在队列
+1 s   自唤醒                                   仍关闭
…      playback.ended(req-a); turn.reply_done   窗口条件全不成立 → 静默 350 ms
+350ms                                          领取话筒租约：盖章 G=13 → grant(req-n)
       playback.started(req-n)                  送达（以 started 为准）；按 context_policy 通知上下文层
       playback.ended(req-n)                    done；rem-5 记入已送达键
```

变体：**播报中用户按键**。`turn.start` → G=14，冲刷 req-n，原因 `user_interruption` → 视为已送达，不重播。
变体：**播报下发后 5 s 没有 `started`**。按 §3.7 视为未送达；若窗口仍开 → 重试（计数 1/8）；若已关 → 推迟，绝不叠声。

### 5.5 打断的各阶段

| 打断时刻 | 代际 | 正在播的 | 排队的 | 上游 | 工具 / 上下文 |
|---|---|---|---|---|---|
| 用户说完、还没有任何输出 | +1 | — | 本回合的 speech 丢 | cancel 在途 response | 未开始的工具调用按收口规则关闭 |
| cue 在播，工具在跑 | +1 | cue 放完 | 结果 speech 被过滤 | cancel | 工具返回查代际不通过 → 不回传（总结 §5.3） |
| 结果 speech 在播 | +1 | 冲刷 | 丢 | cancel + truncate(`audio_end_ms`) | 已回传则补 `{"interrupted": true}`（总结 §5.3，20/20 vs 15/20【实测】）；上下文只记已播部分 |
| narration 在播 | +1 | 冲刷，视为已送达 | 其他 narration 保留 | —（本地 TTS flush） | `after_played` 按 `played_ms` 通知 |
| 冲刷进行中又被打断 | 再 +1 | — | — | — | 合并为一次冲刷，等最后一个 `flush_end` |
| 上游在 cancel 后又推来音频 | 不变 | — | — | 标 `suppressed` | 帧被代际过滤；消费端二次过滤计数应为 0 |

---

## 6. Profile 差异

同一套规则，差异只在配置（两类架构 §4 表"输出仲裁器"一行）。

| 维度 | A 无界面硬件 | B 有界面移动端 |
|---|---|---|
| 来源 | 多：model、tool_result、cue、device_event、reminder、task_progress、task_result、system_notice | 少：model、tool_result、delegated_result、system_notice、system_exit；cue 可选（屏幕可承担"正在查"） |
| 最高优先级 | 一般没有 critical 来源 | **`system_exit`（转人工、挂断、紧急出口）为 critical**，由规则触发，不由模型触发（两类架构 §3.4 第 6 条） |
| 额外输出面 | 灯效（不经仲裁器，走旁路，KM 2.11） | **UI 同步**：转写按句对齐上屏，卡片可先于语音（§4.5） |
| 回合边界来源 | button（现阶段）/ wake / vad | vad + 语义判停；按住说话为降级 |
| 打断来源 | 按键为主；忽略服务端 `speech_started`（KM 2.5） | VAD + 语义；附和语过滤在状态机里做 |
| 回执通道 | 自有 WS 设备协议；`hello` 协商是否支持回执 | WebRTC 数据通道 |
| 结束回执超时 | 剩余未播时长 + 2 s（= 01 `T_drain`） | 同左【建议值，需实测】 |
| 开始回执超时 | 下发完成 + 5 s【建议值，需实测】 | 同左 |
| 沉默填充 | 必须：回合 > 2 s 无声由看门狗发 `system_notice`（两类架构 §2.4） | 可由 UI 状态兜住，语音填充仍建议有（两类架构 §3.2） |
| narration 量 | 多：到点、到达、后台结果、进度 | 少：主要是工具结果；进度多数只上屏 |
| 队列上限 | 32【建议值，需实测】 | 16【建议值，需实测】 |
| 被打断后重试 | 结果 / 提醒 8 次（qwen 默认） | 结果改为上屏兜底后可设 2 次【建议值，需实测】 |

---

## 7. 可观测

trace 字段以 06 §3 为准，本规范不另造字段、不另列字段表。仲裁器负责写入的字段及写入时刻：

| 06 字段（所在节） | 写入时刻 / 规则 |
|---|---|
| `outputs[]`：`request_id, origin, kind, content_role, generation, sentence_id, sent_at_ms, played_ms, status`（06 §3.8） | 每条请求结束（done / dropped / interrupted）时写一项 |
| `latency.sound_first_at_ms` / `content_first_at_ms` / `content_first_ready_at_ms` / `queue_wait_ms` / `cue_to_content_gap_ms`（06 §2.3、§2.4、§3.4） | 按 06 §2.3 判定表和**送出口径**（首帧送出网关 + 首部静音偏移）；回执口径另记 `*_played_at_ms`，不用 `playback.started` 代替送出口径 |
| `latency.local_speech_first_audio_at_ms` / `model_speech_first_audio_at_ms`（06 §2.4） | 按 `content.type` 是本地（`prerecorded` / `text_tts`）还是上游（`upstream_stream`）区分 |
| `latency.playback_started_at_ms` / `playback_ended_at_ms`、`playback_receipts[]`（06 §3.4、§3.12） | 收到回执 |
| `speak_mode`（06 §3.3，工具层写） | 仲裁器提供 origin，映射：`tool_result`→`verbatim`，`model`→`model`，`delegated_result`→`delegated` |
| `closed_by`（06 §3.5，工具层写） | 仲裁器因代际或去重丢弃了某工具的输出时，通知工具层按 03 §4.2 / §5.1 收口 |
| `interrupted`、`interrupt_at_ms`、`interrupt_source`（06 §3.7） | 冲刷时 |
| `usage.responses[].interrupted`、`played_audio_ms`（06 §3.10） | 请求结束时 |
| 仲裁组：`double_voice_count`、`stale_frames_dropped`、`lease_violations`、`queue_truncated`、`drops_by_reason`、`flush_count` / `flush_ms_max` / `flush_timeouts`、`receipt_timeouts`、`consumer_dropped_frames`、`suppressed_frames`、`receipt_mode`、`truncate_estimated`（06 §3.8） | 回合收尾时汇总 |
| `generation`（06 §3.2） | 回合收尾时的当前代际 |
| `narration_attempts` / `narration_delivered`（06 §3.12） | 事件回合收尾时 |

`reply_modality` 由适配层写（06 §3.6），仲裁器不写。

原 §8.2 开放问题 1（"KM 2.12 缺仲裁器相关字段"）已由 06 §3.8 "仲裁与播放"字段组覆盖：丢弃按原因计数、冲刷耗时、回执超时、消费端二次过滤丢帧、`suppressed` 帧、`receipt_mode`、代际值都已有字段名。

---

## 8. 验收要点与开放问题

### 8.1 断言清单

有竞态的断言用重复率判（两类架构 §1 原则 11；Pipecat `--repeat` 出通过率，KM 2.12），每条至少 20 次。

| # | 断言 | 怎么测 | 通过标准 |
|---|---|---|---|
| 1 | **双声 0**：任意时刻至多一个请求处于 playing | 客户端回执时间线：任意两条请求的 [started, ended] 不重叠（cue 与同回合 speech 也不重叠） | 0 次重叠（总结 §3.3 基线【实测】双声 0） |
| 2 | **迟到帧过滤**：代际 +1 后，旧 request_id 零音频到达扬声器 | 注入：打断后 0–2 s 内由 mock 上游继续推旧 response 的 delta | 中心过滤丢弃；消费端二次过滤丢帧数 = 0；扬声器侧 0 帧 |
| 3 | **flush_end 之后零音频** | 冲刷后记录下游收到的帧 | 0 帧（TEN 同名测试断言，`notes/ten-and-deployment.md` Q4） |
| 4 | **回执缺失不卡死** | 丢弃全部 `playback.ended` | narration 在"结束回执超时"后仍能播出；回合能到终态 |
| 5 | **开始回执缺失有上限重试** | 丢弃全部 `playback.started` | 同一 narration 重试 ≤ `max_retries` 次后丢弃；任何时刻不叠声 |
| 6 | **被用户打断不重播** | narration 播放中按键 / 开口 | 该 narration 之后不再出现 |
| 7 | **非用户原因中断会重试** | narration 播放中断开连接再重连 | 重连后重播一次，且不重复（跨连接领取） |
| 8 | **窗口以回执为准** | 模型长回答期间到点 | narration 的 started 晚于模型回答最后一句的 ended + 350 ms |
| 9 | **工具回合中 narration 不插队** | 工具在跑（`awaiting_tool`）时后台结果到达 | 结果在回合终态之后才播 |
| 10 | **同回合去重** | 同一回合两次相同 `dedupe_key` | 只授权一次，另一条记 duplicate |
| 11 | **过期丢弃** | narration 在窗口关闭期间过 `expires_at` | 不播，记 expired |
| 12 | **租约不陈旧** | 长会话中后台任务跨 ≥ 10 个回合多次出声 | 每次都能出声（反例总结 §2.2"只有第一轮有声"） |
| 13 | **cue 不冲刷、旧 cue 不复活** | cue 在播时打断；以及打断后旧回合的 cue 才到 | 在播的放完；迟到的旧 cue 不播 |
| 14 | **critical 抢占**（B） | 模型回答中触发转人工 | 当前输出在冲刷上限内停止，出口话术紧接着播出；屏幕先于语音显示出口状态 |
| 15 | **上屏与音频对齐**（B） | 回放 UI 事件和回执 | 每句转写上屏时刻与其 `playback.started` 的偏差 ≤ 200 ms（06 §3.11 `ui_audio_skew_ms`）【建议值，需实测】；被打断句的显示与 `played_ms` 一致 |
| 16 | **队列有界** | 慢设备 + 长回答 | 队列长度不超上限，溢出有记录 |
| 17 | **进度节流** | 3 分钟长任务、每 5 s 一条进度 | 最多 2 条进度出声（60 s、120 s 附近），内容为最新版本；任务完成后无残留进度 |

### 8.2 开放问题

1. ~~KM 2.12 缺仲裁器相关字段~~：已由 06 §3.8 覆盖（见 §7 末尾），本条关闭。
2. **上游 cancel 之后是否真的还会推音频**（A-E6，`notes/pipecat.md` Q4 未确认）。不影响规则（代际本来就覆盖），影响断言 2 的注入方式是否贴近真实。
3. **cue 的边界**：旧回合未开始的 cue 丢弃（§3.5）是本规范的解释；总结 §2.2 只说"cue 不冲刷"。若实现团队发现有"跨回合仍需要的 cue"，需要回来改。
4. **回执超时数值**：qwen 的 120 s 开始回执超时对 A 类太长；本文的开始回执 5 s、结束回执"剩余未播 + 2 s"（与 01 `T_drain` 一致）未实测。需要按真实设备的缓冲和网络分布校准。
5. **重试上限 8 是否适合 A 类**：没有屏幕兜底时，一条结果重试 8 次可能跨越多个回合、内容已不合时宜。可能要与 `expires_at` 联动（例如结果默认 10 分钟过期）。
6. **用户打断即"已送达"的代价**：`before_speak` 的结果在用户一句都没听到时被视为已送达（§3.6）。A 类没有屏幕，是否需要"下一个空闲窗口提一句'刚才的结果要不要再听'"——这是产品决策，不是仲裁规则。
7. **B 类 critical 是否需要"不可打断"的收尾话术**：用户在转人工话术中再开口怎么办。本文允许 `interruptible=false` 仅用于此；是否真要这样需产品确认。
8. ~~开放麦下的"判停进行中"如何对外暴露~~ **已决定**（review B-5）：01 R2.3-4 补网关内部事件 `turn.endpointing{turn_id, active}`，§3.4、§4.1 引用。
9. **是否做成共享包**（两类架构 §6 第 3 条）：本文只定语义；若两队各自实现，§8.1 的断言清单应作为共享评测用例互相跑。
