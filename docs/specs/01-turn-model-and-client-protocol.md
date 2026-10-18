# 规范 01：回合模型与客户端协议

> **开源说明**：本文是 voice-agent-kit 的契约规范之一，正文保留设计期原貌。代码注释里的"spec 0X §y"指本目录的规范。文中的出处简写（架构 / 两类架构、KM、BP、总结、HC、PC-B、LK-B、UM、`notes/*.md` 等）和 `../../knowledge-map.md` 一类路径指向作者未公开的调研笔记，对应内容已整理进 [voice-agent-handbook](https://github.com/wananing/voice-agent-handbook)，按 [README 的对照表](README.md#出处简写对照) 查。

> 状态：草案 v0.1（2026-09-29）。上位文档：`../voice-agent-two-architectures.md`（下称"两类架构"）§4 的"回合事件模型"契约。
> 依据：`../../knowledge-map.md`（下称 KM）、`../../notes/`、项目总结 `../../../realtime-voice-agent-lessons-2026-09.md`（下称"总结"）。出处写在结论后的括号里。
> 标注约定：**【建议值，需实测】** = 调研或推算得出、还没在我们的设备和网络上测过；**【实践值】** = 现有设备协议已在用；**【新增】** = 现有协议（总结 §2.1）里没有，本规范新加。
> 本规范只写 schema 示例和时序，不写实现代码。
> 相关规范：术语与枚举总表 `00-glossary.md`；输出仲裁 `02-output-arbiter.md`（下称 02）；工具收口 `03-tool-registry-and-call-closure.md`（03）；链路适配 `04-upstream-adapter-capabilities.md`（04）；上下文与会话 `05-context-and-session.md`（05）；trace 与评测 `06-trace-and-evaluation.md`（06）。**本规范提到的所有 trace 字段以 06 §3 为准**，本文不另列字段表。

---

## 1. 目的与范围

### 1.1 目的

两类客户端（A 无界面硬件、B 有界面移动 App）都以开放麦为目标，按键说话和开放麦是**同一个回合状态机的两种配置**（两类架构 前提 1、§2.2）。两类可能由不同团队实现，所以回合模型和客户端协议必须是**一份契约**：同一个问题（这一轮结束了没有、用户听到了哪里、打断后还会不会有旧音频）只能有一种答案（两类架构 §4 红线）。

本规范定义：

1. 回合状态机、`turn_id`、边界来源、终态、看门狗；
2. 客户端 ↔ 网关之间的消息（控制帧 JSON 和音频帧头）；
3. 音频参数和下行节奏；
4. 打断和断线重连的时序；
5. A / B 两个 profile 的字段差异；
6. 可测的验收断言。

### 1.2 范围

| 在范围内 | 不在范围内（另有规范或属实现） |
|---|---|
| 客户端 ↔ 网关的回合与音频协议 | 网关 ↔ 上游模型（S2S / STT / TTS）的适配（04），只写"网关必须对上游做什么"（cancel / truncate） |
| A：自有硬件，WebSocket | 输出仲裁器内部（租约、优先级，02），只写它在协议上暴露的部分（`kind`、代际、回执） |
| B：移动 App，WebRTC 或 WebSocket | B 的 UI 同步（卡片、表单、转写对齐），另立规范 07（owner 待指定）；在此之前以 02 §4.5 为准 |
| 回合终态、错误码 | 工具收口 `closed_by`（03 §4.2），本规范只引用 |
| 断线续接 | 会话重建器（05 §4），本规范只规定触发点 |

### 1.3 术语

全套术语与枚举以 `00-glossary.md` 为准；下表是本规范用到的子集，与总表一致。

| 术语 | 定义 |
|---|---|
| **会话（session）** | 一次连接生命周期内（含续接窗口内的重连）的上下文单位，由网关在 `hello` 应答里分配 `session_id`。 |
| **回合（turn）** | 一次用户输入及其对应的 agent 输出：从边界开始事件（`turn.start`）到网关发出终态（`turn.closed`）。回合从用户开口（按键）到 agent 播完（KM 3.2、总结 §2.1）。事件驱动的播报（narration）也用回合承载，见 §2.2。 |
| **`turn_id`** | 回合标识，会话内唯一。每一个与回合相关的帧都带它（KM 3.2、总结 §2.1）。 |
| **段（segment）** | 上游（或网关判停）把同一回合的用户音频切成的子单元。段不是回合，不产生新 `turn_id`（§2.6）。 |
| **句（sentence）** | 下行 TTS 的分句单元，`(turn_id, sentence_id)` 唯一；回执按句粒度（KM 2.1 待办 P1）。 |
| **代际（generation）** | 网关侧单调递增的整数 `gen`。每次打断或开始新回合 +1；所有待发输出都打上产生时的 `gen`，低于当前 `gen` 的一律丢弃，不论来源（总结 §2.2、KM 3.3）。另外两种 +1 的情况见 02 §3.2（critical 抢占）；"打断 + 开始新回合"同时发生（§5.1）只 +1 一次；被 §2.6 合并的 `turn.start` 不 +1。设备端另有本地"播放代际"`play_gen`，见 §5.4。 |
| **边界来源（source）** | 回合开始或结束的依据：`button`（按键）、`wake`（唤醒词）、`vad`（VAD + 语义判停）、`text`（文本 / 点选输入）。按键模式和开放麦模式的差别只在来源（两类架构 §2.2）。 |
| **终态（terminal state）** | 回合的最终结局，五选一：`completed` / `interrupted` / `discarded` / `failed` / `cancelled`（§2.4）。每个回合有且只有一个终态，由网关发出。 |
| **回执（receipt）** | 客户端上报的播放事实：`playback.started / ended / interrupted {turn_id, sentence_id, played_ms}`。送达以"开始播放"为准，不以服务端生成完为准；它是插话窗口和上下文截断的依据（KM 2.11）。线上不带 `request_id`：`sentence_id` 在回合内对 cue / speech / narration 统一编号，网关按 `(turn_id, sentence_id)` 查出 `request_id` 后交给仲裁器（02 §4.2）。`played_ms` 是**句内**时长，换成条目内位置见 §5.5。 |
| **冲刷（flush）** | 网关丢弃某些回合尚未播出的音频，并以 `flush.start` / `flush.end` 告知客户端；`flush.end` 之后这些回合**零音频**（KM 2.11、两类架构 §2.2）。 |
| **续接窗口（resume window）** | 连接断开后网关保留会话状态（含上游会话）、允许客户端带 `session_id` 续接的时长（§6.2，默认 30 s）。不同于 05 §8.1 的"会话恢复窗口"（默认 30 min，靠快照恢复用户会话、上游会话重建）：续接窗口过期只让本连接会话失效，用户会话仍可在恢复窗口内恢复。 |

---

## 2. 回合模型

### 2.1 状态机

网关持有**权威状态**；客户端维护一个镜像，只用于门控下行和呈现（灯效 / 通话态 UI）。

```
                 turn.start(source)
   ┌──────┐  ─────────────────────────►  ┌───────────┐
   │ idle │                              │ listening │◄───────────┐
   └──────┘                              └─────┬─────┘            │ 补充窗口内再次开始
      ▲                          turn.end      │                  │ （turn.merge，§2.6）
      │                   ┌────────────────────┤                  │
      │                   ▼                    │ 判定"说完"       │
      │             ┌──────────┐  窗口超时 /   │                  │
      │             │ holding  │──判定说完────►│                  │
      │             │（可选）  │───────────────┼──────────────────┘
      │             └──────────┘               ▼
      │                                  ┌───────────┐
      │                                  │ committed │  已向上游提交，等识别 / 首个输出
      │                                  └─────┬─────┘
      │                         首个出声输出   │
      │                     （tts.start）      ▼
      │                                  ┌────────────┐
      │                                  │ responding │  下行进行中
      │                                  └─────┬──────┘
      │                         tts.stop       ▼
      │                                  ┌──────────┐
      │                                  │ draining │  网关已发完，客户端仍在播
      │                                  └─────┬────┘
      │        turn.closed{state}             │ 最后一句 playback.ended / 看门狗
      └───────────────────────────────────────┘
   任一非终态 ──(打断 / 过滤 / 错误 / 取消 / 看门狗)──► turn.closed{interrupted|discarded|failed|cancelled}
```

| 状态 | 含义 | 进入条件 | 离开条件 |
|---|---|---|---|
| `idle` | 没有进行中的用户回合 | 会话建立；上一回合关闭 | 收到或产生 `turn.start` |
| `listening` | 正在收用户音频 | `turn.start` | `turn.end`；打断；`T_listen_max` |
| `holding` | 边界已结束但留补充窗口，用户可能接着说（仅按键模式、仅判"未说完"时，turn-detection-vad.md §3.1、§3.3） | `turn.end` 且语义判停 P(完成) < τ | 窗口内再次 `turn.start` → 合并回 `listening`；窗口到期 → `committed` |
| `committed` | 输入已向上游提交，等识别定稿和首个输出 | 提交 | 首个出声输出 → `responding`；过滤 → `discarded`；`T_asr` / `T_respond` 超时 → `failed` |
| `responding` | 该回合下行音频进行中（speech 或 cue） | 首个 `tts.start` | `tts.stop` → `draining`；打断 → `interrupted` |
| `draining` | 网关已发完，等客户端播完的回执 | `tts.stop{reason:"done"}` | 最后一句 `playback.ended` 或 `T_drain` → `completed` |
| 终态 | 见 §2.4 | — | — |

规则：

- **R2.1-1** 同一会话内同一时刻最多一个用户回合处于 `listening / holding / committed / responding`。新的 `turn.start` 到达时，若已有非终态回合（`holding` 除外，它走 §2.6 的合并），旧回合按 §5 走打断，终态 `interrupted`（`by` 标明原因）。`draining` 回合被新回合开始时同样按打断处理（仍在播）。
- **R2.1-2** 按键模式下网关**只认按键为边界和打断信号**，忽略上游 `speech_started` 之类的服务端判停事件，否则环境噪声会打断播报（KM 2.5）。
- **R2.1-3** 状态只能沿表中箭头前进，不回退；`holding → listening` 是同一回合的合并，不是回退到新回合。
- **R2.1-4** 客户端镜像的唯一硬约束：**下行音频只在镜像为 `responding / draining` 且 `turn_id` 等于当前回合时入播放队列**，其余一律丢弃（借小智"下行只在 Speaking 态收"，xiaozhi.md §2.1、§3.1）。

### 2.2 `turn_id` 生成与传播

- **R2.2-1 谁检测到边界开始，谁生成 `turn_id`。** 按键、唤醒词、文本输入由客户端生成；VAD 开始（开放麦，判停在网关，KM 2.11"判停不放端侧"）和事件播报由网关生成。这样按键回合不用等一个往返才能打 id。
- **R2.2-2 格式**：文本形式 `c<n>`（客户端生成）/ `s<n>`（网关生成），`n` 为会话内按来源单调递增的 31 位无符号整数，重连续接后接着编。二进制帧头里编码为 `uint32`：最高位 0 = 客户端、1 = 网关，低 31 位 = `n`。【新增】
- **R2.2-3 回合先后以网关 `gen` 为准**，不比较 `c` 与 `s` 的数值。
- **R2.2-4 传播**：所有回合相关帧（上行音频帧头、`turn.*`、`stt`、`tts.*`、`playback.*`、`error`、`flush.*`、`turn.closed`）都带 `turn_id`（总结 §2.1、KM 3.2）。迟到的识别结果、迟到的上游输出按 `turn_id` 归属到正确回合；已关闭回合的迟到输出由网关丢弃并记 trace，不下发。
- **R2.2-5 开放麦下音频归属以采样计数为准。** 开放麦时客户端持续上行、边界由网关判定，帧头 `turn_id` 只是客户端当时所知的提示值（可能为 0 = 无回合）；网关按 `turn.start / turn.end` 里的 `ts_samples` 区间把音频归到回合（transport-preprocessing.md §1.2"采样计数时间戳 … 和按键事件对齐"）。
- **R2.2-6 事件播报**（narration：到点、后台结果）由网关分配 `s<n>`，`turn.start{source:"event"}` 下发，走同一状态机（从 `committed` 起步），被用户打断时终态 `interrupted` 且**不重播**（KM 3.3）。`source:"event"` 只在下行出现，不属于用户边界来源。

### 2.3 边界来源事件与两种模式

边界来源抽象成 `turn.start / turn.end`，切换模式不动状态机（两类架构 §2.2、§2.4 原则 7）。

| 来源 | 谁发 `turn.start` | 谁发 `turn.end` | 方向 |
|---|---|---|---|
| `button` | 客户端（key-down） | 客户端（key-up） | 上行 |
| `wake` | 客户端（唤醒词命中） | 网关（VAD + 语义判停），`source:"vad"` | 上行 / 下行 |
| `vad` | 网关（开放麦检测到起说） | 网关（判停） | 下行（告知客户端，供灯效 / UI） |
| `text` | 客户端（文本、测试、点选） | 同一帧隐含结束（`turn.start` 带 `text` 即完整输入） | 上行 |

两种模式是配置，由 `hello` 协商、`listen.open{mode}` 声明（§3.2.3）：

| 配置项 | 按键模式 `ptt` | 开放麦模式 `open_mic` | 出处 |
|---|---|---|---|
| 边界来源 | `button`（`text` 恒可用） | `wake` / `vad`（`button` 仍可用作强制边界和打断） | 两类架构 §2.2 |
| 网关 VAD 的职责 | 不判停；做停顿压缩、短按过滤、补充窗口 | 起说 / 判停（VAD 静音 ~200 ms + 语义模型 + 自适应等待） | KM 2.3、2.4 |
| 打断信号 | 按键 | VAD（前提：客户端 AEC 在播放期间常开） | KM 2.2、2.5 |
| 上游服务端判停 | 能关就关；关不掉用"只切段不自动回复"或停顿压缩兜住 | 同左，网关自己判停 | livekit-agents-B.md §3.1–3.4 |
| `holding` 状态 | 启用（P(完成) < τ 时） | 不启用（自适应等待已含"未说完多等"） | turn-detection-vad.md §3.1 |

- **R2.3-1 来源优先级** `button > text > wake > vad`：开放麦下用户按了键，以按键为准，已在 `listening{vad}` 的回合改记 `source:"button"`，不新开回合。
- **R2.3-2 模式切换只在回合之间生效**（`idle` 时）。如果切换需要改变上游的服务端判停开关，走会话重建（上游多数不能运行中切换判停，livekit-agents-B.md §3.1）。
- **R2.3-3 `listen` 与 `turn` 的区别**：`listen.open / close` 是"麦克风上行窗口"（会话级，开放麦下一个窗口跨多个回合；按键模式下一次按键 = 一个窗口 = 一个回合），`turn.*` 是"这一次输入"。**注意与 v1 协议、KM 2.11、总结 §2.1 的 `listen.start / stop` 区分**：v1 的 `listen.start / stop / detect` 语义是回合边界，对应本规范的 `turn.start / turn.end / turn.start{source:"text"}`（§3.4），不对应 `listen.open / close`；新名字特意取得不同，避免把"开麦窗口"和"回合边界"混为一谈。其他规范引用"松键"时一律指 `turn.end{source:"button"}`。A 类开放麦的功耗和误唤醒由端侧 VAD 门控上行窗口解决，不进网关复杂度（两类架构 §2.4 原则 7）。
- **R2.3-4 判停进行中信号（网关内部事件，不上线）**：`turn.endpointing{turn_id, active}`。开放麦下 VAD 已判静音、语义判停尚未决定时发 `active:true`；判定说完（发 `turn.end`）或判定仍在说时发 `active:false`。按键模式不发。02 §3.4 据此把"判停进行中"视为用户在说（review B-5）。【新增】

### 2.4 终态

| 终态 | 含义 | 谁决定 / 谁发 | 典型触发 | 客户端动作 |
|---|---|---|---|---|
| `completed` | agent 输出播完 | 网关发 `turn.closed`，依据客户端最后一句 `playback.ended`；回执缺失时 `T_drain` 到期也发，带 `receipt_missing:true` | 正常结束 | 回 `idle` / 聆听态 |
| `interrupted` | 被用户或新回合打断 | 网关；触发来自客户端（`abort` / `playback.interrupted` / 新 `turn.start`）或网关 VAD | 按键、VAD barge-in、文本输入、新回合 | 已本地停播；按 §5 |
| `discarded` | 网关判定不是有效输入，本回合**未产生出声**（S2S 上游若已自行开始回复，由网关 `cancel_response` + 代际过滤保证零出声；review B-7-6） | 网关 | 短按过滤（按键 < 250 ms 或语音 < 150 ms，turn-detection-vad.md §3.2）、误唤醒 | A：可播一个极轻提示（turn-detection-vad.md §3.1"设备给个轻提示"）；B：UI 复位 |
| `failed` | 回合无法完成 | 网关，先发 `error{code}` 再发 `turn.closed`；**链路断开时客户端本地判定**（不发送，续接后以网关为准，§6.3） | 见 §3.3.4 错误码 | A：**必须发声**（本地预合成错误话术，两类架构 §2.4 原则 1、5）；B：UI + 语音 |
| `cancelled` | 客户端或会话主动结束 | 网关（响应客户端 `abort{reason:"user_cancel"}`、`listen.close`、挂断、会话关闭） | 挂断、App 切后台、会话结束 | 无声结束 |

- **R2.4-1 每个 `turn.start` 必须恰好对应一个 `turn.closed`**（KM 3.2"回合必须有终态"；总结 §2.1）。唯一例外是会话在续接窗口外失效，此时所有未关闭回合视为 `failed{link_lost}`，由会话级 `session.closed` 一并了结。
- **R2.4-2 网关是终态的唯一发出方**；客户端上报的是事实（回执、abort、边界），不宣布终态。
- **R2.4-3** `turn.closed` 之后该回合的任何下行帧都是协议违规（客户端计数，§8）。
- **R2.4-4** `turn.closed` 带 `by`（触发原因）和统计字段（`heard_ms`、`segments`），进 trace（06 §3.9 `turn_terminal` / `turn_closed_by`）；工具调用的 `closed_by` 六值（03 §4.2、06 §3.5）与回合终态分开记录，不混用。`discarded` 的 `by` 取 `short_press` / `short_speech` / `false_wake` / `rule_silence`（规则判定不回应，如 ≤ 2 字且不在白名单，总结 §3.1）。

### 2.5 看门狗

看门狗的作用：回合不因任何一方不回应而悬空（KM 3.2"识别开始后 N 秒无结果、回传后 N 秒模型不开口"）。网关把生效值在 `hello` 应答的 `timers` 里下发，客户端不自定。

| 计时器 | 起点 → 终点 | 默认 | 到期动作 | 依据 |
|---|---|---|---|---|
| `T_hold` | `turn.end` → 窗口结束 | 600 ms，上限 1000 ms | `holding → committed` | turn-detection-vad.md §3.2【建议值，需实测】 |
| `T_listen_max` | `turn.start` → 仍在 `listening` | A 30 s / B 60 s | 强制 `turn.end{reason:"max_duration"}` 并提交 | 【建议值，需实测】；smart-turn 只看最后 8 s（turn-detection-vad.md §3.2） |
| `T_asr` | 提交 → 识别定稿（`stt{final:true}`） | 3 s | `failed{asr_timeout}` | 实测提交→定稿约 0.37 s（KM 1.3），3 s 约 8 倍余量【建议值，需实测】 |
| `T_filler` | 提交 → 首个出声输出（含 cue） | A 2.0 s；B 由 UI 兜，语音填充 2.5 s | 仲裁器插一个 cue（"我在查"），**不关闭回合** | 两类架构 §2.4 原则 1"约 2 s 无声要填充"【建议值，需实测】 |
| `T_respond` | 定稿 → 首个 speech 输出（cue 不算） | 8 s | `failed{upstream_timeout}` | 【建议值，需实测】；工具回合在"进度收口"时视为已开始 speech（KM 1.3 秒级工具先回传进度） |
| `T_drain` | `tts.stop{done}` → 最后一句 `playback.ended` | 剩余未播时长 + 2 s | `completed{receipt_missing:true}` | 【建议值，需实测】 |
| `T_intr_receipt` | 网关发起冲刷 → 收到 `playback.interrupted` | 300 ms | 用网关估计位置截断上游（§5.3） | 【建议值，需实测】 |
| `T_link_client` | 客户端提交 → 收到本回合 `stt` 回显 | 15 s | 客户端判链路失效，走重连 | 总结 §2.1【实践值】 |
| `T_write` | 网关写 WebSocket 阻塞 | 10 s | 判对端失效 | Pipecat `audio_out_write_timeout_secs=10`（transport-preprocessing.md §1.3）；与客户端 15 s 对称（KM 2.1 P2） |
| `T_hello` | 连接建立 → `hello` 应答 | 10 s | 客户端判握手失败 | 小智 10 s（xiaozhi.md §1.1） |

### 2.6 一次输入被切成多段 / 多次输入并成一段

**(a) 一次按键被上游切成多段**（关不掉的服务端 VAD，总结 §5.6、KM 3.2）：

- **R2.6-1** 段不产生新 `turn_id`；网关在回合内记 `segment` 序号，`stt` 可按段回显（`final:false, segment:k`），回合定稿时再发一条 `final:true` 的合并文本。
- **R2.6-2** 回复只在回合提交时请求一次。上游支持"只切段不自动回复"（OpenAI 类 `create_response=false`）时用它，多段都在对话里，提交时统一 `response.create`（livekit-agents-B.md §3.2、KM 3.2）；不支持时，按键期间上游若自行开始回复，网关取消该回复、丢弃其输出（按 `gen` 过滤），并记入 trace（06 §3.7 `server_vad_segments_in_turn`），作为停顿压缩参数 K 是否合适的监控指标（turn-detection-vad.md §3.3）。
- **R2.6-3** 按键期间规则短路（短语音 silence 规则等）只在模型尚未开始调工具或说话时生效（总结 §5.6、KM 2.5）。

**(b) 一句话被两次按键分开**（松键太早）：

- **R2.6-4** 网关处于 `holding` 时收到新的 `turn.start{source:button, turn_id:c43}`，把它**合并**进 `c42`：下发 `turn.merge{from:"c43", into:"c42"}`，之后上下行都用 `c42`；客户端收到后把当前回合改为 `c42`，之前已用 `c43` 发出的音频帧由网关按 `ts_samples` 归并。【新增】
- **R2.6-5** 若新 `turn.start` 到达时旧回合已 `committed`，不合并，按新回合 + 打断旧回合处理（turn-detection-vad.md §3.3）。

---

## 3. 消息协议

### 3.0 通用约定

- WebSocket：文本帧 = JSON 控制消息；二进制帧 = 音频（§3.1.2、§3.2.1）。WebRTC（B）：音频走 RTP，控制消息走可靠有序 data channel，JSON 与 WS 相同（§7）。
- 所有 JSON 带 `type`，回合相关的带 `turn_id`；时间字段后缀 `_ms`（毫秒）或 `_samples`（采样点，按上行采样率计）。
- 客户端 → 网关需要确认的消息带 `id`（字符串，客户端内唯一），网关回 `*.ack`；网关 → 客户端的指令同样带 `id` 和 `timeout_ms`（JSON-RPC 风格，xiaozhi.md §3.2）。
- 未知 `type` 和未知字段必须忽略（前向兼容）；缺少必填字段回 `error{code:"protocol_error"}`，不断连。
- 与现有 A v1 协议（总结 §2.1）的对应关系见 §3.4。

### 3.1 上行（客户端 → 网关）

#### 3.1.1 `hello`：能力协商

连接建立后第一帧（借小智 `features{aec, mcp}` + 双向 audio_params，xiaozhi.md §1.1、§3.2；KM 2.11）。

```json
{
  "type": "hello",
  "proto": "va/1",
  "profile": "A",
  "client": { "model": "toy-s3", "fw": "1.4.2" },
  "caps": {
    "transport": "ws",
    "aec": "device",
    "modes": ["ptt", "open_mic"],
    "sources": ["button", "wake", "text"],
    "receipts": ["started", "ended", "interrupted"],
    "audio_up":   { "codec": "opus", "rate": 16000, "channels": 1, "frame_ms": 20 },
    "audio_down": { "codecs": ["opus"], "rates": [16000, 24000] },
    "play_buffer_max_ms": 1200,
    "preroll_ms": 250,
    "uninterruptible": true,
    "events": ["location", "nfc", "timer"],
    "commands": ["light", "volume"]
  },
  "resume": null
}
```

| 字段 | 取值 | 网关据此决定 |
|---|---|---|
| `aec` | `device`（硬件 / APM）\| `os`（iOS/Android/libwebrtc）\| `none` | `none` 时不允许 `open_mic`，按键打断后上行前 150–300 ms 标"疑似回声尾巴"（transport-preprocessing.md §7.2）；打断保护窗口长度（KM 2.2） |
| `modes` | `ptt` / `open_mic` | 可选模式；`open_mic` 要求 `aec != none` |
| `receipts` | 回执子集 | 缺 `interrupted` → 截断用网关估计；缺 `ended` → `completed` 靠 `T_drain`，narration 插话窗口按估计开启（KM 2.11 回执兜底） |
| `audio_up / audio_down` | §4 | 下行采样率 |
| `play_buffer_max_ms` | 设备播放缓冲上限 | 下行超前量上限（§4.3） |
| `preroll_ms` | 按下前预录长度 | 回合音频起点（KM 2.11 预录 200–300 ms） |
| `uninterruptible` | 是否支持不可打断片段 | 不支持则安全提示改为本地 cue 播放 |
| `resume` | 续接信息，见 §6.2 | — |

网关应答：

```json
{
  "type": "hello",
  "session_id": "S-8f2c",
  "mode": "ptt",
  "audio_down": { "codec": "opus", "rate": 16000, "frame_ms": 20 },
  "lead_ms": 300,
  "timers": { "T_hold": 600, "T_asr": 3000, "T_filler": 2000, "T_respond": 8000,
              "T_link_client": 15000, "ping_interval": 5000, "resume_window": 30000 },
  "next_server_turn": 1,
  "resume": null,
  "server_time_ms": 1790000000123
}
```

#### 3.1.2 上行音频帧头【新增，KM 2.1 待办 P1】

WS 二进制帧 = 16 字节头 + 一帧 Opus。字段语义是契约，字节布局是 A profile 的建议：

| 偏移 | 字段 | 类型 | 说明 |
|---|---|---|---|
| 0 | `ver` | u8 | 协议版本 = 1 |
| 1 | `kind` | u8 | `0x01` 上行音频 |
| 2 | `flags` | u8 | bit0 `key_down`（按键当前是否按住）；bit1 `echo_tail`（打断后疑似回声尾巴，只标不丢，transport-preprocessing.md §7.2）；bit2 `retransmit`（续接后补发） |
| 3 | 保留 | u8 | 0 |
| 4 | `seq` | u32 | 会话内递增，重连续接后接着编（transport-preprocessing.md §1.2） |
| 8 | `ts_samples` | u32 | 本帧首个采样点的采样计数（按 `audio_up.rate`），不用墙钟；16 kHz 下约 74 小时回绕，按 RFC 1982 式回绕比较 |
| 12 | `turn_id` | u32 | 编码见 R2.2-2；开放麦且无回合时为 0 |

用途：重连缺口检测（`seq`）、上行延迟与抖动监控（`ts_samples`，RFC 3550 抖动公式）、与按键事件对齐（`ts_samples` + `key_down`）（KM 2.1、transport-preprocessing.md §1.2、§7.1）。

#### 3.1.3 `listen`：上行窗口

```json
{ "type": "listen.open",  "mode": "open_mic", "reason": "wake" }
{ "type": "listen.close", "reason": "idle_timeout" }
```

按键模式下客户端可以不发 `listen.*`（一次按键即一个窗口）；开放麦下必须发，网关据此知道"没有上行不是断线，而是麦关了"。

#### 3.1.4 边界事件 `turn.start / turn.end`

```json
{ "type": "turn.start", "turn_id": "c42", "source": "button",
  "ts_samples": 1843200, "preroll_ms": 250 }

{ "type": "turn.end", "turn_id": "c42", "source": "button",
  "ts_samples": 1891200, "reason": "release" }

{ "type": "turn.start", "turn_id": "c43", "source": "text",
  "text": "明天几点出发", "input": "typed" }
```

- `ts_samples` = 边界时刻的采样计数（按下时刻，不含预录）；`turn.end.reason`：`release` / `max_duration` / `cancel`。
- `source:"text"` 时 `input` 取 `typed`（键入）/ `tap`（B 类点选、改字段）/ `test`（测试注入）；trace 记为 06 §3.2 的 `turn_input`。
- `source:"wake"` 时带 `wake_word` 和置信度，误唤醒由网关以 `discarded` 收场（两类架构 §2.2"误触发要有回退"）。

#### 3.1.5 `abort`

用户意图"停下"但不一定开始新输入（B 点"停止"、A 长按取消）；按键打断时与新 `turn.start` 一起发（§5.1）。

```json
{ "type": "abort", "turn_id": "c41", "reason": "button" }
```

`reason`：`button` / `wake` / `text` / `user_cancel`（借小智 `abort.reason`，xiaozhi.md §3.2；进 trace）。`user_cancel` 且无后续 `turn.start` → 终态 `cancelled`；其余 → `interrupted`。

#### 3.1.6 播放回执【KM 2.11 事件名为准】

```json
{ "type": "playback.started",     "turn_id": "c41", "sentence_id": 0, "played_ms": 0,    "ts_ms": 51234 }
{ "type": "playback.ended",       "turn_id": "c41", "sentence_id": 2, "played_ms": 1830, "ts_ms": 55012 }
{ "type": "playback.interrupted", "turn_id": "c41", "sentence_id": 1, "played_ms": 640,  "ts_ms": 52890,
  "reason": "button" }
```

- `played_ms`：**该句**内实际从扬声器送出的时长（DAC 已消费的采样数折算，不是解码或入队时长）。
- `ts_ms`：客户端单调时钟，靠 ping 锚点换算到网关时间（§3.1.8）。
- `started` 每句一次（首个采样送 DAC 时）；`ended` 每句一次；`interrupted` 在本地停播时对**正在播的那一句**发一次，之后同回合不再发 `ended`。
- 回执丢失的兜底由网关负责（`T_drain`、`T_intr_receipt`），客户端不重发回执。

#### 3.1.7 设备事件（带 id，需 ack）

```json
{ "type": "event", "id": "ev-1022", "name": "nfc", "ts_ms": 60012,
  "data": { "tag": "04A2..." }, "expires_ms": 30000 }
```

网关回：

```json
{ "type": "event.ack", "id": "ev-1022", "status": "accepted" }
```

- `status`：`accepted` / `duplicate`（同 id 已处理，仍须 ack）/ `rejected`（带 `code`）。
- 客户端未收到 ack 时重发，间隔 2 s 起指数退避，直到 `expires_ms`【建议值，需实测】；网关按 `id` 幂等（总结 §2.1"可靠事件路径要求 ack"；小智无 ack 是反例，xiaozhi.md §1.1）。
- 事件进网关后先按 handle / context / respond / interrupt 定性再交仲裁器（KM 3.3），协议层不关心。

#### 3.1.8 `ping`：心跳与时钟锚点【新增，KM 2.1 待办 P2】

```json
{ "type": "ping", "client_mono_ms": 61000, "ts_samples_up": 976000, "ts_samples_played": 412800 }
```

```json
{ "type": "pong", "client_mono_ms": 61000, "server_ms": 1790000061042 }
```

- 间隔 5 s【建议值，需实测】；连续 3 次无 `pong` 客户端视为链路可疑，开始重连（与 `T_link_client` 取先到者）。
- 用途：RTT、设备时钟漂移（单调时钟 ↔ 采样计数，类 RTCP SR，transport-preprocessing.md §1.2）、回执 `ts_ms` 换算；每轮写进 trace（06 §3.9 `rtt_ms`、§2.1 `clock_quality`）。

### 3.2 下行（网关 → 客户端）

#### 3.2.1 下行音频帧头【新增】

| 偏移 | 字段 | 类型 | 说明 |
|---|---|---|---|
| 0 | `ver` | u8 | 1 |
| 1 | `kind` | u8 | `0x02` 下行音频 |
| 2 | `flags` | u8 | bit0 `uninterruptible`（该帧属不可打断句） |
| 3 | 保留 | u8 | 0 |
| 4 | `seq` | u32 | 下行会话内递增 |
| 8 | `turn_id` | u32 | 编码见 R2.2-2 |
| 12 | `sentence_id` | u16 | 回合内从 0 递增 |
| 14 | `frame_idx` | u16 | 句内帧序号，客户端据此算 `played_ms` |

#### 3.2.2 `stt` 回显

```json
{ "type": "stt", "turn_id": "c42", "text": "明天几点", "final": false, "segment": 0 }
{ "type": "stt", "turn_id": "c42", "text": "明天几点出发", "final": true }
```

作用是软确认"我收到了这一句"，客户端据此解除 `T_link_client`（总结 §2.1）。小智的 stt 只做显示、不带回合，不可照搬（xiaozhi.md §1.1）。

#### 3.2.3 `tts.start / tts.sentence_start / tts.stop`

```json
{ "type": "tts.start", "turn_id": "c42", "kind": "speech", "codec": "opus", "rate": 16000 }

{ "type": "tts.sentence_start", "turn_id": "c42", "sentence_id": 0,
  "text": "明早八点出发。", "offset_ms": 0, "duration_ms": 1320, "interruptible": true }

{ "type": "tts.sentence_start", "turn_id": "c42", "sentence_id": 1,
  "text": "请牵好大人的手。", "offset_ms": 1320, "duration_ms": 1480, "interruptible": false }

{ "type": "tts.stop", "turn_id": "c42", "reason": "done" }
```

- `kind`：`speech` / `narration` / `cue`（仲裁器输出种类，总结 §2.2、KM 3.3）。
- `offset_ms`：该句在本回合下行音频时间轴上的起点；网关用它把"第几句 + 句内 `played_ms`"换成回合级播放位置，交给上游 truncate（§5.3）。
- `duration_ms`：已知时给出（预合成 cue、本地直念），流式 TTS 可省略。
- `interruptible:false`：不可打断片段，打断时保留（KM 2.1 P1；Pipecat `interruptible=False` + `FrameQueue.reset()`，transport-preprocessing.md §1.3）。**只用于白名单内的 `system_notice` / 安全话术模板**（如上例），单句限长 ≤ 3 s【建议值，需实测】，不用于保护写操作；其他来源一律 `true`（与 02 §3.2 一致，review B-7-2）。**按键打断时客户端仍立即停播其他句，但播完不可打断句**（§5.1）。
- `tts.stop.reason`：`done` / `interrupted` / `error`。

#### 3.2.4 回合级 `error` 与 `turn.closed`

```json
{ "type": "error", "turn_id": "c42", "code": "asr_timeout", "fatal": false,
  "message": "recognition timed out", "retry_after_ms": null }

{ "type": "turn.closed", "turn_id": "c42", "state": "failed", "by": "asr_timeout",
  "heard_ms": 0, "segments": 1 }
```

`failed` 时两帧连发；`error.turn_id = null` 表示会话级错误（如 `session_expired`）。

**错误码枚举**（前三个为现有协议，总结 §2.1【实践值】；其余【新增】）：

| code | 场景 | fatal（会话失效） | 客户端动作（A） |
|---|---|---|---|
| `no_speech` | 提交后识别为空 | 否 | 本地话术"没听清，再说一次" |
| `asr_timeout` | `T_asr` 到期 | 否 | 同上 |
| `upstream_unavailable` | 上游断开 / 致命错误，会话需重建 | 否（网关内部重建，客户端无感） | 本地话术"信号不好"（总结 §5.7） |
| `upstream_timeout` | `T_respond` 到期 | 否 | 本地话术 |
| `upstream_rejected` | 上游审核命中、空操作等非致命 | 否 | 本地兜底话术（总结 §5.7 非致命类） |
| `tts_failed` | 合成失败且无法降级 | 否 | 本地话术 |
| `audio_gap` | 重连缺口过长，本回合音频受损不可用（§4.5） | 否 | "刚才没听全，再说一次" |
| `link_lost` | 客户端本地判定；续接后网关以此补发终态 | 否 | 本地话术 |
| `protocol_error` | 帧格式 / 必填字段错 | 否 | 记日志 |
| `session_expired` | 续接窗口外，会话已失效 | 是 | 重新 `hello` |
| `internal` | 其他 | 视情况 | 本地话术 |

原则：A 类每个 `failed` 都必须发声，话术本地预合成，网关挂了也能出声（两类架构 §2.4 原则 1、5）。

#### 3.2.5 冲刷 `flush.start / flush.end`（含 abort 确认）

```json
{ "type": "flush.start", "flush_id": "f-77", "turn_ids": ["c41"], "reason": "abort", "ack_of": "abort" }
{ "type": "flush.end",   "flush_id": "f-77" }
{ "type": "tts.stop",    "turn_id": "c41", "reason": "interrupted" }
{ "type": "turn.closed", "turn_id": "c41", "state": "interrupted", "by": "button", "heard_ms": 640 }
```

契约（TEN flush start / end 思路，KM 2.11；小智 abort 后立即回 `tts.stop`，xiaozhi.md §2.3、§3.2）：

- **R3.2-1** 收到 `abort`（或网关自己决定打断）后，网关在处理该消息的同一轮事件循环内发 `flush.start`；它同时是 abort 的确认（`ack_of:"abort"`）。
- **R3.2-2** 客户端收到 `flush.start`：丢弃 `turn_ids` 中所有回合已缓冲未播的音频（除 `interruptible:false` 的句），若仍在播则立即停播并发 `playback.interrupted`。
- **R3.2-3** 网关清空自己的发送队列、把上游对这些回合的输出从发送路径上摘除（按 `gen` 过滤）后发 `flush.end`。**`flush.end` 之后，`turn_ids` 中的回合零音频帧**——这是可断言的硬契约。
- **R3.2-4** `flush.end`、`tts.stop{interrupted}`、`turn.closed` 连续发出。只懂 v1 协议的客户端把 `tts.stop{interrupted}` 当作 `flush.end`（§3.4）。
- **R3.2-5** 不带 `ack_of` 的 `flush.start` 是网关主动冲刷（开放麦 VAD 打断、网关侧取消），客户端处理相同。

#### 3.2.6 `turn.start / turn.end / turn.merge`（下行）

开放麦下网关检测到的边界、事件播报回合、补充窗口合并：

```json
{ "type": "turn.start", "turn_id": "s17", "source": "vad",   "ts_samples": 2211840 }
{ "type": "turn.end",   "turn_id": "s17", "source": "vad",   "ts_samples": 2260000, "reason": "endpoint" }
{ "type": "turn.start", "turn_id": "s18", "source": "event", "kind": "narration" }
{ "type": "turn.merge", "from": "c43", "into": "c42" }
```

#### 3.2.7 指令帧（带 id 与超时）

```json
{ "type": "cmd", "id": "cmd-310", "name": "light", "args": { "pattern": "thinking" }, "timeout_ms": 3000 }
```

```json
{ "type": "cmd.result", "id": "cmd-310", "ok": true }
```

- 网关在 `timeout_ms` 内没收到 `cmd.result` 视为失败，记 trace，不自动重发非幂等指令；默认 `timeout_ms` 3 s【建议值，需实测】（小智设备工具 30 s 超时，xiaozhi.md §1.1，面向的是慢工具）。
- 与上行事件 ack 对称（xiaozhi.md §3.2）。状态呈现类指令（灯效）走旁路，不进上下文（KM 2.11）。

### 3.3 典型时序

**按键普通回合（A，ptt）**

```
客户端                                 网关                             上游
key-down  turn.start{c42,button} ───►  idle→listening
音频帧(seq,ts,c42,key_down=1) ──────►  VAD 停顿压缩 → 转发 ──────────►
key-up    turn.end{c42,release} ────►  smart-turn P≥τ → committed ────► commit + response
                                ◄────  stt{c42,final}                ◄── 识别定稿
                                ◄────  tts.start{c42,speech}         ◄── 首音频
                                ◄────  tts.sentence_start{c42,0,...}
                                ◄────  音频帧(c42,s0) 按实时限速
playback.started{c42,0} ────────────►  （仲裁器：用户已开始听）
                                ◄────  tts.stop{c42,done}  responding→draining
playback.ended{c42,0} ──────────────►  draining→completed
                                ◄────  turn.closed{c42,completed}
```

**识别失败**：`committed` 中 `T_asr` 到期 → `error{c42,asr_timeout}` + `turn.closed{c42,failed}` → A 播本地话术。

**上游不回应**：`T_filler`（2 s）到期 → 下发 cue（`tts.start{kind:cue}`）；`T_respond`（8 s）到期 → `error{upstream_timeout}` + `turn.closed{failed}`。

### 3.4 与现有 A v1 协议的对应

| v1（总结 §2.1） | 本规范 | 兼容做法 |
|---|---|---|
| `listen.start` | `turn.start{source:"button"}` | 网关接受两者；v1 无 `turn_id` 时网关代生成并在 `stt` 中回显 |
| `listen.stop` | `turn.end{source:"button"}` | 同上 |
| `listen.detect{text}` | `turn.start{source:"text", text}` | 同上 |
| `stt`（带 turn_id） | `stt` | 不变，增 `final / segment` |
| `tts.start / sentence_start / stop` | 同名，`sentence_start` 增 `sentence_id / offset_ms / interruptible` | 旧客户端忽略新字段 |
| `error{no_speech/asr_timeout/upstream_unavailable}` | `error` + `turn.closed` | 旧客户端把 `error` 当终态 |
| `location / nfc` 带 id + ack | `event{name}` + `event.ack` | 名称映射 |
| 无 | `hello`、音频帧头、`playback.*`、`flush.*`、`turn.closed`、`turn.merge`、`cmd`、`ping` | v1 客户端 `hello` 缺失时网关按 v1 行为降级（截断用估计、无回执插话窗口） |

---

## 4. 音频

### 4.1 编码参数

| 项 | 建议 | 依据 |
|---|---|---|
| 上行编码 | Opus，单声道，16 kHz | KM 2.1；transport-preprocessing.md §7.2 |
| 帧长 | 20 ms（40 ms 可省包头；60 ms 不建议） | KM 2.1【建议值，需实测】（transport-preprocessing.md §7.3 第 6 项：20/40 ms 功耗和延迟对比） |
| DTX | **关**：和本地停顿压缩打架，且会造"静音"给上游 VAD | KM 2.1 |
| FEC | **关**（WS / TCP 上只增码率）；B 的 WebRTC 由 libwebrtc 决定，通常开 | KM 2.1；transport-preprocessing.md §1.1 |
| 码率 | 16–24 kbps | transport-preprocessing.md §7.3 第 6 项【建议值，需实测】 |
| 服务端解码 | 直接解到 16 kHz，不先解 48 k 再重采样（跑 RNNoise 时例外） | KM 2.1、2.2 |
| 下行 | Opus 16 kHz（或 24 kHz，按 `hello` 协商）/ 20 ms | 小智下行默认 24 kHz（xiaozhi.md §1.2） |

小智用 16 k / 60 ms / DTX 开，是为 ESP32 省 CPU，不作为本规范默认（KM 2.1）。

### 4.2 设备端采集约束（A）

- AEC 在设备端，**播放期间麦克风和 AEC 一直在跑**，实例跨轮保留；"按键才开采集"会让 AEC3 永远收敛不了（KM 2.2）。这是 `open_mic` 的硬前提，`aec:"none"` 的客户端不允许协商 `open_mic`。
- 按下前预录 200–300 ms，`turn.start.preroll_ms` 声明（KM 2.11【建议值，需实测】）。
- 高通 100–150 Hz、AGC，不设绝对音量门槛（KM 2.2、transport-preprocessing.md §7.2）。

### 4.3 下行限速与缓冲

- **R4.3-1 网关按实时节奏下发，超前 200–500 ms**，起点 300 ms（LiveKit 200 ms、unmute 320 ms；KM 2.1、transport-preprocessing.md §7.1 P0）【建议值，需实测】（实验：200 / 350 / 500 ms 对打断干脆度和卡顿率的影响，transport-preprocessing.md §7.3 第 8 项）。
- **R4.3-2 起播预缓冲**：每句开始时允许一次性直发不超过 `lead_ms` 的音频（小智直发 5 包 = 300 ms，xiaozhi.md §2.3）；设备端起播前攒 100–200 ms（transport-preprocessing.md §7.1）【建议值，需实测】。
- **R4.3-3 落后不追赶**：发送节拍落后时重新对齐，不突发补发（Pipecat `fastapi.py:598-607`，transport-preprocessing.md §1.3）。
- **R4.3-4 设备播放缓冲上限 ≤ 1.2 s**，`hello.play_buffer_max_ms` 声明；满了丢新到帧并计数，不阻塞网络接收（小智，xiaozhi.md §2.3；KM 2.11）。网关保证 `lead_ms ≤ play_buffer_max_ms − 200 ms`。
- **R4.3-5 禁止一次性推送整段音频**；限速是打断干脆、播放位置可估的前提（transport-preprocessing.md §1.3 对比表）。

### 4.4 TCP 卡顿不补静音

- **R4.4-1** TCP 卡顿后数据会一口气到。客户端**不得**因发送阻塞在上行插静音；网关**不得**因接收空档向上游插静音——关不掉的上游 VAD 会看到静音而切段（KM 2.1、transport-preprocessing.md §1.2）。卡顿后的突发原样按 `seq` 顺序转发。
- **R4.4-2** 下行卡顿导致设备欠载时，设备停在欠载处等待，不补静音也不发 `playback.ended`；恢复后接着播。
- 上游 S2S 对突发上行（快于实时）的反应【未确认，需实测】（transport-preprocessing.md §1.2、§7.3 第 2 项）。

### 4.5 重连缺口与 PLC

缺口 = 续接后首帧 `ts_samples` − 断前末帧 `ts_samples` − 帧长，扣除补发帧（§6.1）。

| 缺口 | 处理 | 依据 |
|---|---|---|
| ≤ 60–100 ms | 网关用 Opus PLC 补（libopus 解码传空包） | KM 2.1、transport-preprocessing.md §1.2【建议值，需实测】 |
| 100 ms – 1 s | 不补；回合标记 `audio_damaged`，照常继续，进 trace（06 §3.9） | transport-preprocessing.md §1.2"长缺口不补，只标记" |
| > 1 s 且回合在 `listening` | 本回合 `failed{audio_gap}` | 【建议值，需实测】 |

PLC 只用于**重连缺口**，不用于 TCP 卡顿（卡顿不丢数据）。

---

## 5. 打断

打断的动作序列固定为：**客户端立即停播 → 发 `interrupted` 回执 → 网关代际 +1 → 冲刷 → 上游 cancel / truncate**（KM 2.5"取消任务 → 清播放队列 → 修正上下文 → 代际 +1"；transport-preprocessing.md §7.1 P0）。三种触发只在"谁先知道"上不同。

### 5.1 按键触发（A；B 的按住说话）

```
客户端                                              网关                           上游
key-down（正在播 c41 第 1 句）
 ① 本地立即停播、清播放缓冲（不等网关）
    play_gen += 1；保留 interruptible:false 的句
 ② playback.interrupted{c41, s1, 640ms, button} ─►
 ③ abort{c41, button} ───────────────────────────►
 ④ turn.start{c42, button} ──────────────────────►  ⑤ gen += 1；c41 标记关闭中
                                             ◄──── ⑥ flush.start{f-77,[c41],ack_of:abort}
                                                    ⑦ 清发送队列；上游 c41 输出按 gen 丢弃
                                                    ⑧ cancel + truncate(offset(s1)+640ms) ─►
                                             ◄──── ⑨ flush.end{f-77}
                                             ◄──── ⑩ tts.stop{c41,interrupted}
                                             ◄──── ⑪ turn.closed{c41,interrupted,by:button}
 音频帧(c42) … 新回合照常                            listening(c42)
```

- ①→② 在同一次按键处理里完成；key-down 到扬声器静音 ≤ 一帧 + DAC 缓冲（验收见 §8）。
- ②③④ 按此顺序、同一 tick 发出；网关以 ② 的位置做截断。
- 播放中按键：上行前 150–300 ms 置 `echo_tail` 标志，只标记不丢弃（transport-preprocessing.md §7.2）。

### 5.2 VAD 触发（开放麦）

判停和打断在网关（KM 2.11），客户端不自行因本地 VAD 停播。

```
客户端（AEC 常开，持续上行）                    网关                                   上游
                                                VAD 在 c41 播放期间检测到起说
                                                过滤：保护窗口 / 最短时长 / 附和语
                                                确认打断 → gen += 1
                                         ◄──── flush.start{f-80,[c41],reason:vad}
                                         ◄──── turn.start{s19, vad}
 本地停播、清缓冲、play_gen += 1
 playback.interrupted{c41,s2,410ms,vad} ─────►  截断位置（等 ≤ T_intr_receipt）
                                                cancel + truncate ──────────────────►
                                         ◄──── flush.end / tts.stop / turn.closed{c41,interrupted,by:vad}
```

- 误打断过滤在发 `flush.start` **之前**完成：打断保护参数起点取 LiveKit `aec_warmup_duration=3.0 s`、`min_duration 0.5 s`、`false_interruption_timeout 2.0 s`（KM 2.2）；附和语（"嗯嗯"）与咳嗽过滤（KM 2.5）【建议值，需实测】。
- 代价：客户端停播要等一个 RTT 加过滤时长。是否允许客户端基于本地 VAD 先"压低音量、不停播"见开放问题 Q3。
- B 的 WebRTC 下客户端几乎无播放缓冲，网关清自己的队列即生效（transport-preprocessing.md §1.3），`playback.interrupted` 可选。

### 5.3 文本触发（文本输入、B 点选）

与按键相同：客户端本地停播 → `playback.interrupted{reason:"text"}` → `abort{reason:"text"}` → `turn.start{source:"text", text}`。B 的点选作为输入进同一状态机（两类架构 §3.2"用户点选和说话是等价的输入"）。

### 5.4 客户端二级代际（防残余包）

- **一级：`turn_id` 门控**。客户端只播当前回合的音频；已关闭或已打断的回合的帧一律丢（R2.1-4；总结 §2.1）。
- **二级：本地 `play_gen`**。客户端每次本地停播（按键、收到 `flush.start`）`play_gen += 1`；音频帧进入解码 / 播放流水线时打上当时的 `play_gen`，出队播放前比对，不等即丢。它防的是"同一回合 abort 后、已经在解码队列或等队列空位的残余包"被播出来——这类包的 `turn_id` 仍是合法的（借小智 `playback_generation_`，xiaozhi.md §2.1、§3.2）。小智设备端 abort 不立即清缓冲、要等状态切换，是反例（xiaozhi.md §2.3）。
- `play_gen` 只在设备内部，不上协议。

### 5.5 网关对上游的动作

- **截断位置** = `offset_ms(sentence_id) + played_ms`，来自客户端回执；`T_intr_receipt` 内没收到回执时用网关估计（已发出时长 − `lead_ms`），并在 trace 标 `truncate_estimated`（06 §3.8；KM 2.1"WebSocket + 设备缓冲下只能靠设备回执的 played_ms"；transport-preprocessing.md §1.3"服务端估计在设备深缓冲下不准"）。
- 上游支持 truncate 的：`cancel` + `truncate(audio_end_ms)`（LiveKit 做法，KM 2.5）；不支持的（Gemini、AWS 等）：本地上下文按"已完整播放的句 + 当前句按比例"记用户听到的文本，并保留显式打断标记（KM 2.5 待办、KM 3.4"历史记用户实际听到的内容"）。
- 打断发生在工具调用各阶段的收口规则见 03 §5.1（依据 KM 2.5 表和总结 §5.3），不在本规范重复。仲裁器侧的冲刷动作见 02 §3.5；上游原语 `cancel_response` / `truncate(item_id, audio_end_ms)` 见 04 §3.5、§3.6，`audio_end_ms` 即上面的截断位置（换算到该条目内）。

---

## 6. 断线重连

### 6.1 `seq` 缺口检测

- 上下行 `seq` 各自在会话内递增，重连续接后接着编，不归零（transport-preprocessing.md §1.2）。
- 网关按 `seq` 去重（续接补发可能重复）；`seq` 跳号 = 断线期间丢失，缺口时长用 `ts_samples` 算（§4.5）。单个 TCP 连接内不需要重排（transport-preprocessing.md §1.2）。
- 客户端保留最近 ≥ 2 s 的上行帧环形缓冲用于续接补发【建议值，需实测】。

### 6.2 会话续接

```json
{ "type": "hello", "proto": "va/1", "profile": "A", "caps": { "...": "..." },
  "resume": { "session_id": "S-8f2c", "last_tx_seq": 48210, "last_rx_seq": 9021,
              "current_turn": "c42", "turn_state": "listening" } }
```

```json
{ "type": "hello", "session_id": "S-8f2c",
  "resume": { "ok": true, "last_rx_seq": 48177, "turns": [ { "turn_id": "c42", "state": "listening" } ] } }
```

- **续接窗口**默认 30 s（A、B 相同；B 可因 App 切后台延长到 120 s）【建议值，需实测】。窗口内网关保留会话、上游会话和回合状态。
- 网关应答里的 `last_rx_seq` = 网关实际收到的最后上行 `seq`；客户端从其后补发（`flags.retransmit=1`），补发不到的部分即缺口。
- `resume.ok:false`（窗口外或网关重启）→ 网关回 `error{session_expired, fatal:true}`；客户端丢弃全部回合状态，走新会话；网关侧由会话重建器用本地上下文重建（KM 3.4"重建会话是常规路径"）。

### 6.3 重连后各状态回合的处理

| 断线时回合状态 | 续接成功 | 续接失败 |
|---|---|---|
| `listening`（按键仍按住） | 补发 + §4.5 缺口规则，继续 | 客户端本地 `failed{link_lost}`，A 播本地话术 |
| `listening`（断线期间已松键） | 客户端续接后补发 `turn.end`（`ts_samples` 为实际松键时刻），网关照常判停 | 同上 |
| `holding` / `committed` | 继续；若已超 `T_asr` / `T_respond`，网关补发 `error` + `turn.closed{failed}` | 同上 |
| `responding` / `draining` | 客户端对断线时正在播的句发 `playback.interrupted{reason:"link"}`；网关**不重发已发过的音频**，按回执截断上下文，回合 `interrupted{by:"link"}`；A 播本地"刚才信号不好"话术（总结 §5.7） | 同上；网关侧按网关估计截断 |
| `idle` | 无动作 | 新会话 |

- **R6.3-1** 断线期间网关为该回合生成的下行音频不补发（过时）；是否"从下一句续播"见开放问题 Q5。
- **R6.3-2** 客户端本地判定的 `failed{link_lost}` 以续接后网关补发的 `turn.closed` 为准；两者不一致时以网关为准，客户端只纠正状态、不再发声。
- **R6.3-3** 链路失效判定：客户端 `T_link_client` 15 s（总结 §2.1）或 ping 连续 3 次无应答；网关 `T_write` 10 s 或 ping 超时（KM 2.1 P2）。

---

## 7. Profile 差异

必填 = M，可选 = O，不适用 = —。

| 项 | A 自有硬件（WS） | B 移动 App（WS） | B 移动 App（WebRTC） |
|---|---|---|---|
| `hello` 能力协商 | M | M | M（走 data channel） |
| `caps.aec` | M（`device` / `none`） | M（`os`） | M（`os`，由 libwebrtc / OS 提供） |
| 模式 | `ptt` 现阶段，`open_mic` 演进 | `open_mic` 默认，`ptt` 降级 | 同左 |
| 边界来源 | `button`、`wake`（演进）、`text` | `vad`、`button`（降级）、`text` | 同左 |
| 上行帧头 `seq / ts_samples` | M | M | —（RTP 序号 / 时间戳承担） |
| 上行帧头 `turn_id` | M | M | —（按 RTP 时间戳映射到 `turn.start / end` 的采样区间，R2.2-5） |
| 上行帧头 `key_down` | M | O（仅 `ptt`） | —（按键状态走 data channel 的 `turn.*`） |
| `listen.open / close` | O（`ptt`）/ M（`open_mic`） | M | M |
| `turn.start / end` | M | M | M |
| `abort` | M | M | M |
| `playback.started / ended` | **M** | M | O（网关可用"已推入 − 仍排队"估计，transport-preprocessing.md §1.3） |
| `playback.interrupted` | **M**（`played_ms` 是唯一可靠的播放位置） | M | M（客户端本地停播时），网关主动冲刷时 O |
| `event` + `event.ack` | M（设备事件多） | O | O |
| `cmd` + `cmd.result` | O（灯效、音量） | O | O |
| `ping / pong` 时钟锚点 | M | M | O（RTCP SR/RR 承担 RTT 和时钟，transport-preprocessing.md §1.1） |
| 下行帧头 `turn_id / sentence_id` | M | M | —（下行音频无应用头，句边界靠 data channel 的 `tts.sentence_start` 与 RTP 时间戳对齐） |
| 下行限速 | 网关实现（§4.3） | 网关实现 | 媒体栈队列反压（LiveKit `queue_size_ms=200`，transport-preprocessing.md §1.3） |
| 抖动缓冲 / PLC / FEC | 设备浅缓冲；PLC 仅重连缺口 | 同 A | libwebrtc NetEQ（**仅当两端都是 libwebrtc**；aiortc 只有 16 包重排、无 NACK 无 PLC，KM 2.1） |
| 播放缓冲上限 | M（≤ 1.2 s） | M | —（NetEQ 自适应） |
| 二级代际 `play_gen` | M | M | O（客户端缓冲很浅，仍建议实现） |
| 续接窗口 | 30 s | 30–120 s | ICE 重启 / 重连房间；会话续接语义相同 |
| 失败必须发声 | M（本地话术） | O（UI 兜底 + 语音填充） | 同左 |

**B 的 WebRTC 下由 libwebrtc 承担的**：抖动缓冲、丢包隐藏、带内 FEC、RTP 序号与时间戳、RTCP 时钟同步与 RTT、AEC（客户端 OS / libwebrtc）、下行节奏的反压。**仍由本规范承担的**：`turn_id` 与回合终态、边界事件、回执（至少 `interrupted`）、冲刷契约、错误码、看门狗、会话续接。换言之，"换 WebRTC"只换掉 §3.1.2、§3.2.1、§4.3–4.5，不换 §2、§5（KM 2.1"换 WebRTC 就解决弱网不成立，除非两端都用 libwebrtc"）。

---

## 8. 验收要点

每条都是可在测试环境（弱网模拟 + 真机录音 + trace）里判真假的断言。数值阈值除注明外均为【建议值，需实测】。

**回合与终态**

1. 每个 `turn.start`（上下行）在会话内恰好对应一个 `turn.closed`；长稳压测（≥ 1000 回合、含随机断线、打断、上游故障注入）中悬空回合数 = 0。
2. `turn.closed` 之后该 `turn_id` 的下行帧数 = 0（客户端计数器）。
3. 上游不回应注入：`error{upstream_timeout}` 在 `T_respond ± 200 ms` 内到达；A 在 `T_filler ± 200 ms` 内有声（录音可验证），全程无 > 2.5 s 的静默（= `T_filler` + 500 ms，与 06 §3.9 `stuck` 阈值一致）。
4. ASR 空结果注入：100% 收到 `error{no_speech}` + `turn.closed{failed}`，A 100% 播出本地话术。
5. 按键模式下注入上游 `speech_started`：不触发打断（R2.1-2）。
6. 上游在按键期间切段：不产生新 `turn_id`；松键前产生的上游回复被丢弃；"松键前调工具"率 = 0（R2.6-2）。
7. 补充窗口内再按：两次按键得到一个回合（`turn.merge`），`stt` 合并文本完整。
8. 短按（< 250 ms）：终态 `discarded`，本回合零出声（`cancel_response` + 代际过滤；`server_only` 上游收不回已送的音频，只能保证不出声，04 §3.3）。

**打断与冲刷**

9. `flush.end` 之后，被冲刷回合的音频帧数 = 0（客户端和网关抓包双向核对）；1000 次随机时刻打断中违规 0 次。
10. 按键打断：key-down 到扬声器输出降到底噪以下 ≤ 60 ms（录音测）；不可打断句除外。
11. `playback.interrupted.played_ms` 与录音实测的播放位置误差 ≤ 40 ms。
12. 同回合 abort 后残余包：注入"abort 后 200 ms 内仍到达的同 `turn_id` 帧"，播出 = 0（二级代际生效）。
13. 打断后上游上下文只含 `heard_ms` 以内的内容（有 truncate 的上游核对 `audio_end_ms`；无 truncate 的核对本地历史）。
14. VAD 打断：保护窗口内（会话开头 3 s）的起说不触发 `flush.start`。

**音频与传输**

15. 网关下行任意时刻超前量 ≤ `lead_ms` + 一帧（发送时间 vs 音频时间轴）。
16. 注入 2 s TCP 上行卡顿：网关送上游的音频中新增静音帧 = 0，上游切段次数与无卡顿对照组无显著差异。
17. 重连缺口 80 ms：PLC 生效，回合无 `audio_damaged`；缺口 500 ms：有 `audio_damaged` 标记；缺口 2 s 且在 `listening`：`failed{audio_gap}`。
18. 续接补发：重复 `seq` 被去重，送上游的音频无重复片段。
19. 设备播放缓冲从不超过 `play_buffer_max_ms`；溢出计数在正常网络下 = 0。

**可靠消息**

20. 设备事件：断网期间产生的事件在续接后送达，网关处理恰好一次，重复发送均收到 `duplicate` ack。
21. `cmd` 无应答：`timeout_ms` 后记失败，不重复执行非幂等指令。
22. `hello` 降级：不支持回执的客户端（v1）能完成完整回合，截断标 `truncate_estimated`。

**两个 profile 互跑**

23. A、B 两个团队的实现能互相跑对方的协议用例集（两类架构 §4"两个团队各自实现也要能互相跑对方的用例"）。

---

## 9. 开放问题

1. ~~Q1 `turn_id` 双源生成是否值得~~ **已决定**（review B-5）：维持双源生成（按键回合省一个往返，对 A 的首音有意义）。配套约束：适配层只接受网关归一后的 `turn_id`（04 §3.2）。
2. **Q2 看门狗默认值**：`T_asr 3 s`、`T_respond 8 s`、`T_filler 2 s`、`T_drain +2 s` 全部是推算，需要用每轮 trace 的分布（P95/P99）校准；工具回合是否需要单独的 `T_respond`。
3. **Q3 开放麦下客户端能否基于本地 VAD 先"压低音量"**：能缩短打断体感延迟，但误触发会让播报忽大忽小；是否引入 `tts.duck` 原语待 A 开放麦原型验证。与之相关：是否需要"暂停 / 恢复"原语来实现 LiveKit 式 `resume_false_interruption`（KM 2.5）。
4. **Q4 下行超前量**：200 / 350 / 500 ms 的取舍依赖园区蜂窝网络卡顿分布（transport-preprocessing.md §7.3 第 1、8 项）；是否按 RTT 自适应。
5. **Q5 重连后是否从下一句续播**：当前规则是不续播、打断收场、提示重问；如果断线频繁，续播体验更好，但要处理"用户在断线期间说了话"的冲突。
6. **Q6 续接窗口长度**：30 s 是否覆盖 A 的弱网场景和 B 的切后台场景；上游会话能否在窗口内保活（取决于上游超时）。
7. **Q7 上游对突发上行和上行空洞的反应**：决定 §4.4 是否需要网关侧"按实时重新整形"上行（transport-preprocessing.md §7.3 第 2 项）。
8. **Q8 事件播报是否一律走回合**：narration 用 `s<n>` 回合承载让终态和冲刷统一，但会让"回合数"指标混入非用户回合；trace 里需要区分 `source:event`。
9. **Q9 B 的 WebRTC 下 `playback.started / ended` 是否真的可省**：网关估计在 libwebrtc 客户端缓冲很浅时误差小（transport-preprocessing.md §1.3），但 B 的 UI 同步（字幕对齐）可能仍需要客户端回执，待 UI 同步规范确定。
10. **Q10 A 类开放麦硬件前提**：设备 AEC 常开、端侧 VAD 门控、唤醒功耗与误唤醒率（两类架构 §6 Q1）；不满足时 A 的 `open_mic` 协商必须被拒。
