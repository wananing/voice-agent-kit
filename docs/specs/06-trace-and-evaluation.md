# 06 trace 字段规范与评测规范

> **开源说明**：本文是 voice-agent-kit 的契约规范之一，正文保留设计期原貌。代码注释里的"spec 0X §y"指本目录的规范。文中的出处简写（架构 / 两类架构、KM、BP、总结、HC、PC-B、LK-B、UM、`notes/*.md` 等）和 `../../knowledge-map.md` 一类路径指向作者未公开的调研笔记，对应内容已整理进 [voice-agent-handbook](https://github.com/wananing/voice-agent-handbook)，按 [README 的对照表](README.md#出处简写对照) 查。

> 状态：v0.1 草案（2026-09-29）。A、B 两个团队共用的契约，对应 `../voice-agent-two-architectures.md`（下称"两类架构"）§1 原则 11、§4 的"trace schema"与"评测 harness"两项，以及 KM 1.3（总结 §3.3）的首音两指标。
>
> 依据：`../../knowledge-map.md`（下称 KM）1.3、2.12、3.8；`../../bottleneck-proposals.md`（下称 BP）；`../../notes/` 下各笔记；项目总结 `../../../realtime-voice-agent-lessons-2026-09.md`（下称"总结"）§7。
>
> 标注约定：**【建议值】** 表示本文给出、还没有实测验证的数值或取值；**【待定】** 表示本文没法定、要列入 §10 开放问题的事项。没有标注的条款是契约，两个团队都必须遵守。
>
> 权威性：本文生效后，**本文 §3 取代 KM 2.12 成为 trace 字段的权威表**（KM 2.12 改为引用本文）。BP 各节的实验只引用本文字段，不另外定义。之所以要这么做，是因为字段分散在多处时已经出过不一致，比如 `closed_by` 在三处分别列了 4、5、6 个值（`notes/review-knowledge-map.md` C3）。
>
> 与其他规范的分工（同目录）：01–05 不再各列 trace 字段表，只引用本文节号；**字段的语义取值**以定义该概念的规范为准——回合终态与边界来源以 01 §2.3–2.4 为准，仲裁以 02 为准，`closed_by`、呈现方式、路线以 03 §1.3、§3、§4.2 为准，错误分类与重建原因以本文 §3.6、§3.9 与 04 §4.6、05 §4.2、§7.1 共同的一套为准。术语与枚举总表见 `00-glossary.md`。

---

## 1. 目的、范围与术语

### 1.1 目的

1. **同一个问题只有一个答案。** A、B 两类可以各自实现，但"首音多少""这版比上版好不好""这轮算不算通过"必须用同一套口径回答（两类架构 §0、§4 红线）。
2. **评测先于方案。** 所有方案实验（BP A-E\*、B-X\*、C-X\*）都用本文的 trace 和用例格式出数；换人设、换提示语只需要看版本前缀分批统计，不需要改门禁（总结 §7）。
3. **两个团队能互跑对方的用例。** 用例格式、rubric、trace schema 只有一份（两类架构 §4）。

### 1.2 范围

| 包括 | 不包括 |
|---|---|
| 每轮 trace 的字段、类型、单位、锚点、写入方 | 设备协议、UI 同步协议本身（各自实现，两类架构 §4 红线） |
| 延迟口径（起点、终点、分段） | 存储选型、看板实现 |
| 汇总维度、OTel 导出命名、统计方法 | 价格表与计费结算（只规定 trace 里要有什么，§3.10） |
| L0 / L1 / L2 / 音频层评测的用例格式与流程；judge rubric；样本集管理；门禁 | 具体用例内容（由各业务的用例仓库维护） |

### 1.3 术语

| 术语 | 定义 | 出处 |
|---|---|---|
| **回合（turn）** | 从一次用户输入的边界开始（按键、唤醒、VAD 起说、B 类的一次点选），到该输入引起的所有输出都进入终态为止。回合有 `turn_id`，每一帧都带它；回合必须有终态（§3.9 `turn_terminal`）。一次补充窗口内合并的两次按键算同一回合。设备事件引起的播报（到点、传感器）另起"事件回合"，`turn_source=event` | KM 3.2；01 §2；两类架构 §2.2 |
| **会话（session）** | 网关上一次设备连接 / 一次通话的生命周期，`session_id`。一个会话里可以包含多个**上游会话**（重建、主动换会话），用 `session_seq` 区分。跨连接会话的用户会话（05 §1.3）另记 `user_session_id`（§3.2） | KM 3.4 |
| **代际（generation）** | 仲裁器的计数器，每次打断或新回合 +1（critical 抢占也 +1，02 §3.2）；低代际的输出一律丢弃 | 总结 §2.2；01 §1.3 |
| **有声音** | 本回合第一次有**非静音的音频**开始送给用户，来源不限（提示语、模型前导语、错误话术都算）。只算音频，屏幕变化不算（B 类屏幕另计，§2.4） | KM 1.3、3.1；总结 §3.3 |
| **有内容** | 本回合第一次有**回应用户请求的音频**开始送给用户。预合成提示语、"我查一下"类前导语、承接句都**不算**；本地直念的工具结果、上游逐字念通道念的结果（实验通道，§2.3）、模型的正式回复**算**。判定规则见 §2.3 | KM 1.3；总结 §3.3 |
| **首音** | "有声音"和"有内容"的统称。任何报表都不许只写"首音"而不说是哪一个 | KM 1.3（总结 §3.3） |
| **冻结盲测集** | 一组从未用于调参的样本（文本句或录音），带版本号，冻结后不改内容；每个方案在同一版本上比较。它能暴露"训练句 100%、盲测 54%"这类差距 | 总结 §3.1、§7；KM 3.8 |
| **judge** | 按 rubric 对回复做逐轮二元判定的评审模型，也可以是规则（如按字数判 `brevity`）。judge 只给结论；只有结论是 no 时才写理由 | `notes/flows-and-evals.md` §3.2 |
| **通过率** | 同一条用例（或脚本）在同一版本上重复跑 N 次，通过次数 / N。有竞态的用例只报通过率，不报单次结果 | `notes/flows-and-evals.md` §2.2（Pipecat `--repeat`）；KM 3.8 |
| **竞态用例** | 结果依赖时序的用例：打断、异步工具、判停、重连、事件插话、模拟用户 | 同上 |
| **profile** | A（无界面硬件）或 B（有界面移动端）。字段和评测项的差异都写成 profile 差异，不写成两套 schema | 两类架构 §0 |

---

## 2. 延迟口径（最重要）

### 2.1 时钟与时间戳的写法

1. **规范时钟是网关的单调时钟。** 所有 `*_at_ms` 字段都是相对本回合主锚点 T0 的毫秒偏移（整数，可以为负）；另记一个 `t0_epoch_ms`（网关墙钟，Unix 毫秒），用来跨系统对齐。
2. **客户端（设备 / App）产生的时间点**（松键、播放回执、UI 上屏、点选）按这个顺序换算到网关时钟：
   - 有时钟锚点时（KM 2.1 待办 P2"时钟锚点与 RTT 心跳"），用客户端时间戳加上估计的偏移，`clock_quality=synced`；
   - 没有时钟锚点时，用网关收到该帧的时刻减去 RTT/2，`clock_quality=rx_minus_half_rtt`【建议值】；
   - 连 RTT 都没有时，直接用网关收到的时刻，`clock_quality=gateway_rx`。
   `clock_quality` 必须写进 trace，汇总时按它分组，不同质量的数据不混算。
3. **音频内部的时间点用采样计数。** 上行帧头带 seq 和采样计数（KM 2.1 P1）。比如"用户最后一个有声帧"在网关时钟上的位置，是从松键时刻按采样计数倒推出来的（§2.2），不用网关收到那一帧的时刻：TCP 卡顿后数据会突发到达，收帧时刻不代表说话时刻（KM 2.1"卡顿不补静音"）。
4. **单位一律是毫秒整数**：时长字段以 `_ms` 结尾，时间点字段以 `_at_ms` 结尾。token 数、字符数、次数是整数，比例是 0–1 的浮点数。
5. **缺失和不适用要区分**：字段不适用时**省略**（比如普通回合没有 `pre_tool_ms`）；应该测到但没测到时写 `null`（比如播放回执丢了）。汇总时 `null` 计入"数据缺失率"，不当作 0。

### 2.2 三个锚点

| 锚点 | 字段 | 定义 | 怎么测 | 写入方 |
|---|---|---|---|---|
| **T_ae：用户音频流结束** | `anchors.audio_end_at_ms` | 用户**最后一个有声帧**结束的时刻，**不含尾部静音** | 网关本地 VAD（C1 的 TEN VAD，每跳 16 ms）在上行音频上找最后一段语音的结束采样点；按"该采样点距松键 / 判停时刻的采样数"换算到网关时钟。离线评测和测试脚本里，由脚本直接给出（合成音频里的语音结束位置，不是文件结束位置） | 网关 |
| **T_rel：松键** | `anchors.release_at_ms` | 用户松开按键的时刻（`turn.end{source:"button"}`，即 v1 协议的 `listen.stop`，01 §3.4）。补充窗口里合并了多次按键时，取**最后一次**松键 | 设备帧，按 §2.1 第 2 条换算 | 设备 → 网关 |
| **T_ep：判停** | `anchors.endpoint_at_ms` | 系统判定"用户说完了"并开始请求回复的时刻：VAD 静音到阈值后，语义判停（smart-turn、自适应等待）给出"说完"的那一刻 | 网关判停模块打点。B 类如果判停放在客户端，客户端打点后按 §2.1 换算 | 网关（或客户端） |

**主锚点 T0 怎么选**（`anchors.primary` 字段写明选了哪个）：

| 场景 | T0 | 理由 | 出处 |
|---|---|---|---|
| A 类按键模式 | **T_rel** | 松键之前系统不能也不该出声；两类架构 §2.3 的预算按松键算 | 两类架构 §2.3；KM 1.3 |
| A 类开放麦（唤醒 / 常听）、B 类开放麦 | **T_ep** | 两类架构 §2.3、§3.3 的预算从判停算；判停段（T_ep − T_ae）单独计、单独有预算（0.3–0.6 s） | 两类架构 §2.3、§3.3 |
| B 类按住说话（降级模式） | **T_rel** | 同 A 类按键 | 两类架构 §3.2 |
| 点选、改字段等触控输入（B） | 客户端点击时刻 `anchors.tap_at_ms` | 没有音频，不存在 T_ae | 两类架构 §3.2"点选和说话是等价输入" |
| 事件回合（A 的到点、传感器） | 事件到达网关的时刻 `anchors.event_at_ms` | 同上 | 两类架构 §2.4 原则 6 |
| **离线基准、测试脚本、跨 profile 对比** | **T_ae** | 只有 T_ae 在所有模式下都有定义；也是 qwen bench 的口径（"用户音频流结束，不含尾部静音"） | `notes/flows-and-evals.md` §2.3 |

规则：

- **三个锚点凡是能测到的都要写进 trace**，不管 T0 选的是哪个。用户感知延迟一律可以用"`*_at_ms` − `anchors.audio_end_at_ms`"换算出来，所以不需要为每个锚点各存一套字段。
- **两个派生量必须报**：`ptt_tail_silence_ms` = T_rel − T_ae（按键模式下，用户说完到松键之间的静音。这段时间是用户自己的，不算系统延迟，但要监控，因为它会影响用户感知）；`endpoint_ms` = T_ep − T_ae（开放麦的判停段）。
- **KM 2.12 原文"起点统一为松键（用户音频流结束，不含尾部静音）"把 T_rel 和 T_ae 写成了一个点**。按键模式下两者之差就是 `ptt_tail_silence_ms`，通常不为零，所以本文把它们拆开。以后报数必须注明锚点，写成"有内容 P50 1.05 s（T_rel）"这样。
- 总结 §3.2"逐字念 0.6–0.7 s"的起点没有核实（`notes/review-knowledge-map.md` F6），在核实之前不要拿这个数和按本文口径测出的数直接比。

### 2.3 "有声音"与"有内容"的判定

**计时点**：本回合某段输出的第一个音频帧**送出网关**的时刻，加上这段音频开头的静音时长（裁掉首部静音后计时）。

- 预合成的提示语在合成时已经裁掉首尾静音（总结 §3.3），偏移量为 0；
- 流式 TTS 和 S2S 的输出，由网关在编码前的 PCM 上检测首个非静音样本：10 ms 窗 RMS 高于 −45 dBFS 算非静音【建议值，需在样本集上校准】；
- 设备播放回执到了以后，另记 `*_played_at_ms`（§3.4）。门禁用送出口径（任何环境都能测）；回执口径用来校验"设备缓冲 + 下行"这一段。

**判定表**（仲裁器在每段输出入队时根据 `kind` 和 `content_role` 判定；两者由产生这段输出的组件在入队时填写）：

| 输出 | `kind` | `content_role` | 算有声音 | 算有内容 | 依据 |
|---|---|---|---|---|---|
| 预合成提示语（"我查一下路线。"） | `cue` | `filler` | 是 | **否** | 总结 §3.3；KM 3.1"提示语只解决前者，不能冒充后者" |
| 模型在同一个 response 里、function call 之前说的前导语 | `speech` | `filler` | 是 | **否** | KM 2.8 原则 3（前导语是"执行期间出声"的一种） |
| B 类"正在查"类语音填充、承接句 | `speech` | `filler` | 是 | **否** | 两类架构 §3.3 工具回合一行 |
| 模型的普通回复（本回合没有工具调用） | `speech` | `answer` | 是 | 是 | — |
| 工具结果回传后模型再生成的回复 | `speech` | `answer` | 是 | 是 | KM 1.3 |
| **本地 TTS 直念工具结果**（A1） | `speech` | `answer` | 是 | **是** | BP A1；KM 2.8 |
| 上游逐字念通道念我们写的结果（**实验，待 P0-13 通过后按 04 Q12 以 `request_response.overrides.verbatim_text` 承载，不设独立原语**；契约内只有本地 TTS 一条念读通道） | `speech` | `answer` | 是 | 是 | 总结 §3.2；review B-6 |
| 委派后台流式文本 → 本地 TTS（A2） | `speech` | 首段是承接句时为 `filler`，否则为 `answer` | 是 | 看 `content_role` | BP A2 |
| 秒级写操作的"已受理 / 已开始"进度收口（A3） | `speech` | `progress` | 是 | 是，但单独归类为 `content_kind=progress` 统计【建议值】 | BP A3 |
| 回合级错误话术（"没听清，再说一次"） | `speech` | `error` | 是 | **否**；本回合 `turn_terminal=failed` | 两类架构 §2.4 原则 1 |
| 事件播报 | `narration` | `answer` | 对事件回合计 | 对事件回合计 | 两类架构 §2.2 |

补充规则：

1. **有内容的计时点是这段音频开始送给用户的时刻，不是它准备好的时刻。** 正文排在提示语后面时，要等提示语播完才轮到它。所以另记 `content_first_ready_at_ms`（正文第一帧到达仲裁器）和 `queue_wait_ms`（= 送出 − 就绪），和总结 §3.3"结果排在提示语后的等待 P50 0、P90 0.2 s"是同一个口径。
2. **`content_role` 判不准时以 L1 judge 为准**：judge 对某段回复判 `continue`（"还没答完"，§6.2），就说明它不是内容。L1 报表里要统计"`content_role=answer` 但 judge 判 `continue`"的比例，用来发现漏标。
3. **B 类屏幕另计**：`ui_feedback_first_at_ms`（通话态第一次变化，比如聆听 → 思考）和 `ui_content_first_at_ms`（第一张结构化卡片 / 第一个字段上屏）是独立指标，**不并入**有声音 / 有内容（两类架构 §3.3："有内容（屏幕）可早于语音"，但"有声音"仍然不能松）。

### 2.4 分段名称、定义与单位

所有分段都是毫秒整数。"累计"类字段是 `*_at_ms`，相对 T0；"段"类字段是 `*_ms`，表示两点之差。

**通用（每个回合）**

| 名称 | 类型 | 定义 | 对应预算 | 出处 |
|---|---|---|---|---|
| `sound_first_at_ms` | 累计 | T0 → 有声音 | A ≤ 900（T_rel）；B ≤ 1000（T_ep） | 两类架构 §2.3、§3.3 |
| `content_first_at_ms` | 累计 | T0 → 有内容 | A：普通 ≤ 1000、逐字念工具 ≤ 1100、组织语言工具 ≤ 1300；B：语音 ≤ 1500、工具回合 ≤ 2000 | 同上 |
| `content_first_ready_at_ms` | 累计 | T0 → 正文第一帧到达仲裁器 | — | 总结 §3.3 |
| `queue_wait_ms` | 段 | 正文就绪 → 正文送出 | — | 总结 §3.3 |
| `cue_to_content_gap_ms` | 段 | 本回合第一段 `filler` 播完 → 有内容 | A ≤ 1500 | 两类架构 §2.3 |
| `endpoint_ms` | 段 | T_ae → T_ep（开放麦） | 300–600，不超过 2500 | 两类架构 §3.3 |
| `ptt_tail_silence_ms` | 段 | T_ae → T_rel（按键） | 只监控，不设预算 | 本文 §2.2 |
| `stt_final_at_ms` | 累计 | T0 → 识别定稿 | 现状约 370（T_rel） | KM 1.3 |
| `seg.stt_first_word_ms` | 段 | 首个语音帧送出 → 首个转写词（流式 ASR） | — | `notes/unmute.md` Q2e（`stt_latency`） |
| `seg.vad_ms` | 段 | T_ae → 发起回复请求（判停 + 冲刷） | — | 同上（`vad_latency`） |
| `seg.llm_ms` | 段 | 发起回复请求 → LLM / S2S 首个 token（或首个音频 delta） | — | 同上（`llm_latency`） |
| `seg.tts_start_ms` | 段 | LLM 首词 → 首个音频包（级联路线；S2S 直出时省略） | — | 同上（`tts_start_latency`） |
| `seg.first_segment_ms` | 段 | LLM 首词 → 首段文本提交给 TTS（含切句等待和出声前护栏；TTS 按 token 送入时接近 0）。`tts_start_ms − first_segment_ms` 即 TTS 首包。级联、半级联；S2S 省略 | — | 【本规范约定】（vak 实现时补，用来区分切句等待和 TTS 首包） |

unmute 的恒等式"端到端首音 ≈ vad + llm + tts_start"（`notes/unmute.md` Q2e）只在"普通回合 + T_ae 口径"下成立，报表可以用它做一致性检查。

**工具回合另外分"工具前 / 工具后"两段**（qwen bench 口径，`notes/flows-and-evals.md` §2.3）：

| 名称 | 类型 | 定义 | 出处 |
|---|---|---|---|
| `fc_received_at_ms` | 累计 | T0 → 网关收到第一个 function call（完整参数） | KM 2.12；`notes/livekit-agents.md` |
| `pre_tool_ms` | 段 | **工具前** = T0 → 第一个工具开始执行（≈ `fc_received_at_ms` + 调度开销）；提示语在这一刻播 | qwen bench"工具开始前" |
| `tool_exec_ms` | 段 | 每个调用各自的执行时长；回合级取产出内容的那个调用 | KM 1.3 |
| `tool_done_at_ms` | 累计 | T0 → 产出本回合内容的那个调用完成。有多个调用时，取有内容之前最后一个完成的调用【建议值】 | KM 2.12 |
| `output_sent_at_ms` | 累计 | T0 → function output 回传上游（没有回传时省略） | KM 2.12 |
| `post_tool_ms` | 段 | **工具后** = `tool_done_at_ms` → 有内容 | qwen bench"工具返回后" |
| `regen_ms` | 段 | `output_sent_at_ms` → 模型再生成的首个音频（只在 `speak_mode=model` 时有），用来单独观察"回传再生成"的约 1.2 s | KM 1.3；总结 §3.4 |
| `local_speech_first_audio_at_ms` / `model_speech_first_audio_at_ms` | 累计 | 本地直念 / 模型出声的第一帧，二者分开记，这样"有内容"能按 `speak_mode` 分类统计 | KM 2.12；BP A.4 |

**示例（A 类按键、逐字念工具回合，数字为示意；这是长 cue 的写法，即 A-E2 的长 cue 对照组，§3.13 的 trace 示例与它一致）**：

```
T_ae        -180   用户说完（松键前还有 180 ms 静音 → ptt_tail_silence_ms=180）
T_rel = T0     0
stt_final    370
fc_received  800   ← pre_tool_ms ≈ 805
cue 有声音    830   sound_first_at_ms=830
tool_done    815   tool_exec_ms=10
本地直念就绪 1060   content_first_ready_at_ms=1060
cue 播完     1590   queue_wait_ms=540
有内容       1600   content_first_at_ms=1600，post_tool_ms=785
```

这个例子暴露了一个预算上的矛盾：提示语约 0.83 s 开播、长约 0.76 s，所以按本文口径，用长 cue 时逐字念工具回合的"有内容"不会早于约 1.6 s，达不到两类架构 §2.3 的 ≈ 1.1 s。**已决定（review B-1）**：**本文不改口径**（有内容就是用户能听到的时刻），改由工具层按路线选 cue（03 §2.1 `cue`）：A1 用短 cue（≤ 300 ms），并把 `voice_template` 的固定前缀预合成；工具在 cue 开播前已完成则不播 cue。仲裁规则不变（02 §3.5 cue 不冲刷）。决定后的时间线（数字为示意）：

```
T_rel = T0     0
fc_received  800   短 cue 授权
cue 有声音    830   sound_first_at_ms=830（≤ 900）
tool_done    815
直念前缀就绪  820   固定前缀预合成，本地 TTS 首包（约 245 ms）不进等待
短 cue 播完  1110   cue 长 280 ms【建议值，需实测】
有内容       1120   content_first_at_ms≈1120（≈ 1.1 s）
```

若工具在 cue 授权前已完成，不播 cue，有声音 = 有内容 ≈ 直念前缀送出时刻。A-E2 用长 cue / 短 cue / 无 cue 三组对照验证。

---

## 3. trace 字段权威表

### 3.1 记录粒度与写入

- **每个回合一条 trace 记录**，按 `(session_id, turn_id)` 唯一。各组件只写自己负责的字段，网关的 trace 收集器负责合并（总结 §7："每轮一条 trace，字段固定，三条链路共用同一个 schema"）。
- **收尾时机**：回合进入终态后，再等"最后一个播放回执"或"上游 usage 到达"，取二者中较晚的一个；最多等 5 s【建议值】，超时就写出，没到的字段记 `null`。
- **写入方缩写**：客户端 = A 类的设备 / B 类的 App；网关；仲裁器；适配层 = 链路适配层（S2S / 级联 / 半级联的上游适配）；工具层 = 链路无关层里的工具执行器和决策分层。
- **必填**：M = 两类都必填；M-A / M-B = 只在该 profile 必填；O = 可选；C = 满足条件时必填（条件写在说明里）。
- schema 文件（JSON Schema）与本文同时维护，放在 `docs/design/specs/schemas/turn-trace.schema.json`【待定：文件由谁建、什么时候建，见 §10】。两个团队的 CI 都用它校验自己产出的样例 trace（§8）。

### 3.2 标识与版本

| 字段 | 类型 | 单位 | 锚点 / 来源 | 写入方 | 必填 | A/B 差异 | 说明与出处 |
|---|---|---|---|---|---|---|---|
| `trace_schema_version` | string | — | — | 网关 | M | — | 本文的版本，比如 `"06-v0.1"` |
| `profile` | enum `A` / `B` | — | — | 网关 | M | — | |
| `session_id` | string | — | — | 网关 | M | — | 连接会话（01 §1.3）；续接窗口过期后换新 |
| `user_session_id` | string | — | 05 §2.1 条目的 `user_session_id` | 网关 | M | — | 用户会话（05 §1.3），可跨多个连接会话；续接窗口过期、在 05 §8.1 恢复窗口内重连时不变，trace 按它串起同一用户会话（review B-7-4） |
| `turn_id` | string | — | 与设备协议里的 `turn_id` 相同，格式 `c<n>` / `s<n>`（01 R2.2-2）；被 `turn.merge` 合并的回合只出一条 trace，用合并后的 `turn_id` | 网关 | M | — | 总结 §2.1；01 §2.6 |
| `generation` | int | — | 仲裁器代际 | 仲裁器 | M | — | 用来区分被打断的回复（`notes/ten-and-deployment.md` Q8-2） |
| `turn_source` | enum `button` / `wake` / `vad` / `text` / `event` | — | `turn.start` 的 `source`（01 §2.3、R2.2-6），多次来源以 01 R2.3-1 的最终值为准 | 网关 | M | A：`button`、`wake`、`text`、`event`；B：`vad`、`button`、`text` | 两类架构 §2.2；01 §1.3 |
| `turn_input` | enum `typed` / `tap` / `test` | — | `turn.start.input`（01 §3.1.4） | 网关 | C（`turn_source=text`） | B 的点选、改字段为 `tap` | 总结 §2.1 `listen.detect` 对应 `text` + `typed` |
| `t0_epoch_ms` | int | ms（Unix） | T0 | 网关 | M | — | §2.1 |
| `device_id` / `user_id_hash` | string | — | — | 网关 | M-A / M-B | A 按设备，B 按用户 | 汇总维度（§4）；用户标识只存哈希 |
| `pipeline` | string | — | 配置 | 适配层 | M | B 按回合选路，一个会话内可能变化 | KM 2.12；例：`s2s:<vendor>`、`cascade:<asr>+<llm>+<tts>`、`half_cascade:<model>` |
| `provider` / `model` | string | — | 配置 | 适配层 | M | — | `notes/ten-and-deployment.md` Q8-2 |
| `prompt_version` | string | — | 配置 | 网关 | M | — | 格式为 `<pipeline 前缀>/<persona>@<v>+tools@<v>+state@<v>`。前缀区分链路，后缀依次是人设、工具表、状态块的版本（总结 §7） |
| `route` | enum `s2s` / `cascade` / `half_cascade` | — | 本回合的选路结果 | 网关 | M-B | A 省略（固定是 S2S + 本地直念） | 两类架构 §3.2 选路规则 |
| `risk_level` | enum `low` / `high` | — | 分流规则 | 工具层 | M-B | A 省略 | 同上；用来核查"高风险回合有没有走文本先于语音" |
| `gateway_build` | string | — | 构建 | 网关 | M | — | 代码版本，和 `prompt_version` 分开 |

### 3.3 决策

| 字段 | 类型 | 单位 | 锚点 / 来源 | 写入方 | 必填 | A/B 差异 | 说明与出处 |
|---|---|---|---|---|---|---|---|
| `turn_kind` | enum `chat` / `tool_verbatim` / `tool_compose` / `tool_write` / `event` / `ui_input` / `error` | — | 回合结束时判定 | 工具层 | M | A 没有 `ui_input` | 延迟预算按回合类型分（两类架构 §2.3），汇总时必须按它分组 |
| `user_text` | string | — | ASR 定稿 | 适配层 | M | — | 用于归因和 L2；按隐私策略脱敏 |
| `action` | string | — | 决策结果 | 工具层 | M | — | 动作名；没有动作时写 `none` |
| `decision_layer` | enum `rule` / `model` / `correction` / `gate` | — | 最后生效的那一层 | 工具层 | M | B 多一层 `stage` | 两类架构 §4 决策分层 |
| `tool_calls[]` | array | — | 见 §3.5 | 工具层 | C（有调用时） | — | |
| `slots` | object | — | 规则归一化之后的值 | 工具层 | O | — | KM 2.12 |
| `entity_resolution` | object | — | 实体解析结果（原文 → 规范 id） | 工具层 | O | — | KM 2.12 |
| `speak_mode` | enum `verbatim` / `model` / `delegated` / `screen` / `none` | — | 本回合内容的出声方式：`verbatim` 本地直念（A1）；`model` 模型再生成（A0）；`delegated` 委派后台（A2）；`screen` 内容上屏、语音只念要点（B，03 §3.2 #8、#9）；`none` 不出声（静默收口，03 §3.2 #10） | 工具层 | C（工具回合） | `screen` 只在 B | KM 2.12；BP A1–A3；采纳 03 §10.1 的提议 |
| `write_gate` | enum `passed` / `blocked` / `n_a` | — | 写操作门槛 | 工具层 | C（有写操作） | — | 总结 §3.1 的三道门槛 |
| `stage` / `visible_actions_version` | string | — | 阶段状态机 | 工具层 | M-B | A 省略 | 两类架构 §3.2"阶段决定动作集" |
| `form_version_before` / `form_version_after` | int | — | 结构化状态 | 工具层 | M-B | A 省略 | 两类架构 §3.1 |

### 3.4 延迟分段

§2.4 列出的所有字段都放在 `latency` 对象下，锚点放在 `anchors` 对象下。这里只补充写入方、必填性和 A/B 差异。

| 字段 | 写入方 | 必填 | A/B 差异 |
|---|---|---|---|
| `anchors.primary` | 网关 | M | A 按键为 `release`；开放麦为 `endpoint` |
| `anchors.audio_end_at_ms` | 网关 | M（有音频输入的回合） | — |
| `anchors.release_at_ms` | 客户端 → 网关 | C（按键） | — |
| `anchors.endpoint_at_ms` | 网关 / 客户端 | C（开放麦） | B 必有 |
| `anchors.clock_quality` | 网关 | M | — |
| `latency.sound_first_at_ms` / `content_first_at_ms` | 仲裁器 | M | — |
| `latency.content_first_ready_at_ms` / `queue_wait_ms` / `cue_to_content_gap_ms` | 仲裁器 | C（有 `filler` 时） | — |
| `latency.content_kind` | 仲裁器 | M | enum `answer` / `progress` / `none`（本回合没有内容，如错误回合或静默回合） |
| `latency.stt_final_at_ms`、`seg.*` | 适配层 | M（能拿到的分段都要写） | S2S 直出时没有 `seg.tts_start_ms` |
| `latency.fc_received_at_ms` … `regen_ms` | 工具层 / 适配层 | C（工具回合） | — |
| `latency.sound_first_played_at_ms` / `content_first_played_at_ms` | 客户端（播放回执） | O；设备协议 P0 播放回执上线后改为 M-A | B 用客户端的播放事件 |
| `latency.playback_started_at_ms` / `playback_ended_at_ms` | 客户端 | 同上 | — |
| `latency.ui_feedback_first_at_ms` / `ui_content_first_at_ms` | 客户端 | M-B | A 省略 |

### 3.5 收口（每个工具调用一项，`tool_calls[]`）

| 字段 | 类型 | 单位 | 写入方 | 必填 | 说明与出处 |
|---|---|---|---|---|---|
| `call_id` | string | — | 适配层 | M | 上游给的调用 id |
| `name` / `args` | string / object | — | 适配层 | M | `args` 存模型给的原始参数，归一化后的值放在 `slots` |
| `presentation` | enum `verbatim` / `model` / `screen` / `silent` | — | 工具层 | M | 工具注册表的"呈现方式"（03 §1.3、§2.1）；委派是路线不是呈现方式，记在 `tool_route` |
| `is_write` | bool | — | 工具层 | M | |
| `latency_class` | enum `ms` / `seconds` / `background` | — | 工具层 | M | 注册表"执行时长"（03 §2.1） |
| `tool_route` | enum `A0` / `A1` / `A2` / `A3` / `silent` | — | 工具层 | M | 本调用走的出声路线（03 §3.2 决策表；带屏的 A1-屏 / A3-屏记 `A1` / `A3`，由 `presentation=screen` 区分）。不叫 `route`，避免与 §3.2 选路字段重名 |
| `route_fallback_reason` | string | — | 工具层 | C（运行时改了路线） | 03 §3.3 |
| `phase` | string | — | 工具层 | C（有阶段的 profile） | 调用发生时的阶段（03 §8.3） |
| `generation` | int | — | 工具层 | M | 调用登记时的代际（03 §4.1） |
| `gate_result` | enum `pass` / `intent_mismatch` / `utterance_not_explicit` / `backend_state` / `not_available_in_phase` / `n_a` | — | 工具层 | C（写操作或阶段外调用） | 03 §8.2、§8.4 的 `reason_code`；回合级汇总仍用 §3.3 `write_gate` |
| `late_result_dropped` | int | 次 | 工具层 | M | `settled` 之后到达被拒的结果数（03 §4.1 不变式 2） |
| `speculation_hit` | bool | — | 工具层 | C（A2 且开了投机） | 03 §6.3 |
| `started_at_ms` / `done_at_ms` / `tool_exec_ms` | int | ms（相对 T0） | 工具层 | M | |
| `reply_required` | bool | — | 工具层 | M | 回传后要不要触发模型再生成；收口和再生成是两件事（KM 2.8 原则 2） |
| `closed_by` | enum，见下表 | — | 工具层 | M | **每个调用必须有一个值**；没有值的调用就是"悬着"，计入 §8 的"悬挂调用数" |
| `settled_at_ms` | int | ms | 工具层 | M | 进入终态的时刻；之后到达的结果一律拒绝（KM 3.5 `settled`） |
| `interrupt_phase` | enum `received` / `running` / `done_not_sent` / `sent_speaking` / `progress_closed` / `narration` / `delegating` / `n_a` | — | 工具层 | C（被打断的调用） | 按 03 §5.1 打断矩阵的 7 行（依据 KM 2.5 打断收口权威表） |

**`closed_by` 六个值**（KM 2.12、3.5，全文统一；BP A.4 只列了 4 个值，以本表为准。**语义以 03 §4.2 为准**，下表是摘要）：

| 值 | 含义 | 典型场景 | 出处 |
|---|---|---|---|
| `result` | 工具正常完成并回传结果，含业务失败（`ok=false`）、门槛拒绝、阶段不可见 | 正常路径；本地直念时也要回传收口 | 03 §4.2；总结 §5.2 |
| `interrupted` | 用户打断（含新回合打断旧回合）时调用已完成：还没回传的按 03 §5.1 行 3（按 `tools.silent_close` 分支：有静默收口则回传真实结果 + `not_spoken: true`，否则不回传）；已经回传的补一个 `{"interrupted": true}`（是否附 `played_text` 由 A-E1 定） | 按键 / VAD 打断 | 03 §4.2、§5.1；总结 §5.3 |
| `cancelled` | 调用在跑时被取消：打断且 `interrupt_policy=cancel`、用户说"算了"、超时、委派被取消、执行前再查代际 / 门槛不通过（门槛链本身拒绝记 `result` + `ok=false`，不记 `cancelled`） | 异步任务取消 | 03 §4.2、§5.2；KM 2.5 |
| `progress` | 先回传进度，用进度收口；最终结果换新调用、以 narration 送出 | 秒级写工具（A3） | KM 2.8；BP A3 |
| `superseded` | 会话被重建，或被 `on_duplicate=replace` 的新调用替换；不触发生成。**不用于打断**（打断记 `interrupted` / `cancelled`） | 重建前在途的调用；被替换的调用 | 03 §4.2；KM 3.3（qwen） |
| `duplicate` | `on_duplicate=reject/confirm` 命中；同一用户回合的第二个写操作或第二个委派 | 重复调用 | 03 §4.2；BP A3；KM 2.8 |

### 3.6 会话与缓存

| 字段 | 类型 | 单位 | 写入方 | 必填 | A/B 差异 | 说明与出处 |
|---|---|---|---|---|---|---|
| `session_seq` | int | — | 网关 | M | — | 本回合所在的上游会话序号，从 1 开始 |
| `rebuild_reason` | enum `none` / `disconnect` / `upstream_error` / `context_overflow` / `state_change` / `turn_limit` / `scheduled` / `modality` / `app_resume` | — | 网关 | M | `app_resume` 只在 B 类出现（切后台后恢复） | 与 05 §4.2 的触发表一一对应。`upstream_error` = `error_class=transient` 的上游错误（不叫 `fatal_error`：`fatal` 类按定义不重建） |
| `history_format` | enum `pairs` / `packed` / `pairs_tools_as_text` | — | 网关 | C（本回合发生了重建） | — | 灌入格式，取值同 04 §3.1 `history.format`、05 §3.2；BP B-X2 |
| `history_scope` | enum `full` / `truncated` / `summary_recent` | — | 网关 | C（本回合发生了重建） | — | 带入范围：全量 / 截断到最近 K 轮 / 摘要 + 最近 K 轮（05 §4.1、§5.5）；BP B-X4。原 KM 2.12 的 `history_mode` 拆成这两个字段 |
| `history_items` | int | 条 | 网关 | C（本回合发生了重建） | — | 带入的历史条数（05 §4.1） |
| `handoff_start_at_ms` / `handoff_done_at_ms` | int | ms | 网关 | C（主动换会话） | — | KM 2.12 |
| `state_block_version` | string | — | 网关 | M | B 为表单版本 | KM 3.4 |
| `state_update` | enum `none` / `immediate` / `lazy` | — | 网关 | M | — | 状态块分两层生效（两类架构 §2.2） |
| `cached_input_tokens` / `uncached_input_tokens` | int | token | 适配层 | M（上游返回缓存信息时） | — | 用来验证"会话配置更新会让未命中缓存的输入 ×3.2"（总结 §5.4） |
| `reply_modality` | enum `audio` / `text_only` / `mixed` / `none` | — | 适配层 | M | — | 灌入文本历史后模型可能改用文本回复（KM 3.4）；音频会话出现 `text_only` 即告警（05 §3.5） |
| `turns_in_upstream_session` | int | — | 网关 | M | — | 上游通常只保留约 20 轮（总结 §5.4） |

### 3.7 判停、打断与音频门控

| 字段 | 类型 | 单位 | 写入方 | 必填 | A/B 差异 | 说明与出处 |
|---|---|---|---|---|---|---|
| `press_ms` | int | ms | 客户端 | C（按键） | — | 按键时长，也就是 usage 里的 `input_audio_ms` 来源 |
| `speech_ms` | int | ms | 网关 | M | — | 本地 VAD 判出的语音总时长 |
| `server_vad_segments_in_turn` | int | — | 适配层 | M-A | B 走自控判停时省略 | 关不掉的服务端 VAD 在一个回合里切了几段（KM 3.2） |
| `compressed_silence_ms` | int | ms | 网关 | C（开了停顿压缩） | — | C1 停顿压缩压掉的静音总长 |
| `dropped_short_press` | bool | — | 网关 | M-A | — | 本回合是否被当作短按丢弃；会话级再汇总成次数（C1） |
| `smart_turn_prob` | float | 0–1 | 网关 | C（跑了判停模型） | — | |
| `endpoint_wait_ms` | int | ms | 网关 | C | A：补充窗口实际等待；B：自适应等待的实际值 | KM 2.4 |
| `supplement_window_hit` | bool | — | 网关 | C（开了补充窗口） | A | 判"没说完"后用户是否真的又按了；配合 C-X4"多等轮次 ≤ 15%" |
| `interrupted` | bool | — | 仲裁器 | M | — | 本回合的输出是否被用户打断 |
| `interrupt_at_ms` / `interrupt_source` | int / enum `button` / `vad` / `wake` / `text` / `link` / `critical` / `guardrail` | ms | 仲裁器 | C（被打断） | A 以 `button` 为主；B 以 `vad`、`text`（点选）为主 | 与 01 `playback.interrupted.reason` / `abort.reason` 同名取值；`critical` = 02 §2.3 critical 抢占；KM 2.5 |
| `false_interrupt_filtered` | int | 次 | 网关 | O | B 更常见 | 被当作附和、咳嗽过滤掉的次数（KM 2.5） |

### 3.8 仲裁与播放

KM 2.12 没有这一组，本文新增。原因是 BP A.4 要统一报"双声次数"，A-E6 要统计"打断后仍到达的上游帧"，这两个数都得有字段承载；02 §7 列出的仲裁器验收所需量（按原因的丢弃计数、冲刷耗时、回执超时、消费端二次过滤、`suppressed`、`receipt_mode`、代际）也在本组落地，02 §7 引用本节。

| 字段 | 类型 | 单位 | 写入方 | 必填 | 说明与出处 |
|---|---|---|---|---|---|
| `outputs[]` | array | — | 仲裁器 | M | 每段输出（每条 OutputRequest）一项：`{request_id, origin, kind, content_role, generation, sentence_id, sent_at_ms, played_ms, status}`。`origin` 取 02 §2.2 的来源值（`model` / `tool_result` / `cue` / …）；`sentence_id` 是 01 的回合内整数句号（多句请求记首句）；`played_ms` 为该请求累计播放时长；`status` 取 `played` / `truncated`（播了一部分被打断）/ `flushed`（未播即被冲刷）/ `dropped_stale` / `dropped_expired` / `dropped_duplicate` / `dropped_overflow` / `dropped_retry_exhausted` |
| `double_voice_count` | int | 次 | 仲裁器 | M | 两个来源的音频同时在播的次数；门禁要求为 0（BP A.4） |
| `stale_frames_dropped` | int | 帧 | 仲裁器 | M | 被代际过滤掉的迟到帧（A-E6） |
| `lease_violations` | int | 次 | 仲裁器 | M | 拿着过期租约想出声的次数（总结 §2.2） |
| `queue_truncated` | bool | — | 仲裁器 | M | 有界队列是否发生了截断（总结 §2.2） |
| `drops_by_reason` | object | 次 | 仲裁器 | M | `{stale, expired, duplicate, overflow, retry_exhausted}` 各自的丢弃计数（02 §3.2、§3.6、§3.8、§3.1） |
| `flush_count` / `flush_ms_max` / `flush_timeouts` | int / int / int | 次 / ms / 次 | 仲裁器 | M | 冲刷次数、`flush.start → flush.end`（本地 TTS 为 `tts_flush → flush_end`）最长耗时、超过 1 s 上限的次数（02 §3.5 第 6 步） |
| `receipt_timeouts` | object | 次 | 仲裁器 | M | `{started, ended}`：开始回执超时、结束回执超时次数（02 §3.7；结束回执超时与 01 `T_drain` 同口径） |
| `consumer_dropped_frames` | int | 帧 | 消费端 → 仲裁器 | M | 消费端按 `request_id` / 代际二次过滤丢掉的帧数；应为 0，非 0 是缺陷信号（02 §3.9） |
| `suppressed_frames` | int | 帧 | 适配层 | M | 已 cancel 的 `response_id` 迟到被标 `suppressed` 的帧数（02 §3.2；04 §3.5） |
| `receipt_mode` | enum `receipts` / `estimated` | — | 仲裁器 | M | 客户端不支持回执、仲裁器按下发时长估算时为 `estimated`（02 §3.7；01 §3.1.1 `caps.receipts`） |
| `truncate_estimated` | bool | — | 网关 | C（本回合发生了截断） | 截断位置用网关估计而不是回执（01 §5.5） |

### 3.9 传输与错误

| 字段 | 类型 | 单位 | 写入方 | 必填 | A/B 差异 | 说明与出处 |
|---|---|---|---|---|---|---|
| `uplink_seq_gaps` | int | 个 | 网关 | M-A | B 走 WebRTC 时用 RTCStats 的丢包数代替 | KM 2.1 |
| `uplink_jitter_ms` | int | ms | 网关 | M | B 从 RTCStats 取 | RFC 3550 口径（KM 2.12） |
| `rtt_ms` | int | ms | 网关 | M | — | 同时用于 §2.1 的时钟换算 |
| `downlink_lead_ms` | int | ms | 网关 | M-A | — | 下行限速的超前量，目标 200–500（KM 2.1） |
| `plc_ms` | int | ms | 网关 | O | — | 重连缺口里用 PLC 补的时长 |
| `turn_terminal` | enum `completed` / `interrupted` / `discarded` / `failed` / `cancelled` | — | 网关 | M | — | **回合终态**，取值与 01 §2.4 完全一致。原草案的 `silence`（按规则不回应）和 `dropped`（短按丢弃）都归 `discarded`，用 `turn_closed_by` 区分；原 `error` 即 `failed` |
| `turn_closed_by` | string | — | 网关 | M | — | `turn.closed.by`（01 R2.4-4）：`completed` 时取 `playback_ended` 或 `T_drain`（回执缺失，01 §2.4 `receipt_missing`）；`discarded` 时取 `short_press` / `short_speech` / `false_wake` / `rule_silence`；`interrupted` 时取打断来源；`failed` 时取错误码 |
| `error_code` | enum，取值即 01 §3.2.4 错误码表（`no_speech` / `asr_timeout` / `upstream_unavailable` / `upstream_timeout` / `upstream_rejected` / `tts_failed` / `audio_gap` / `link_lost` / `protocol_error` / `session_expired` / `internal`） | — | 网关 | C（`turn_terminal=failed`） | — | 回合级错误码（总结 §2.1），和设备协议里的错误帧一致 |
| `audio_damaged` | bool | — | 网关 | C（有重连缺口） | — | 缺口 100 ms – 1 s 不补只标记（01 §4.5） |
| `error_class` | enum `fatal` / `transient` / `context_overflow` / `benign` | — | 适配层 | C（有上游错误） | — | 见下方说明；与 04 §4.6 事件的 `class`、05 §7.1 的四类是同一套 |
| `restart_attempts_in_turn` | int | 次 | 网关 | M | — | 每个回合最多 3 次（KM 3.4；05 §7.2） |
| `watchdog_fired` | enum `none` / `T_asr` / `T_filler` / `T_respond` / `T_drain` / `T_intr_receipt` / `T_listen_max` / `tool_timeout` | — | 网关 | M | — | 看门狗（KM 3.2）；计时器名同 01 §2.5，`tool_timeout` 为 03 §4.1 的调用超时 |
| `stuck` | bool | — | 网关 | M | — | 看门狗兜底之后仍没有终态，或者用户听到的无声时间（播放口径）超过 `T_filler` + 500 ms = 2.5 s【建议值，需实测】而且没有任何填充；与 01 §8 验收第 3 条"无 > 2.5 s 静默"一致（review B-7-3）。这是 BP 各节"卡死次数"的来源；A 类另外按"沉默即故障"（两类架构 §2.4 原则 1）统计 |

**关于 `error_class`**：KM 2.12 给了三个值（fatal / transient / benign），KM 3.4 和 BP B.5 分了四类（重建也没用 / 可以重建 / 上下文超限 / 非致命）。"上下文超限"的处理方式是先缩历史再重建，和另外三类都不同，所以本文采用四个值：`fatal` 对应"重建也没用"，`transient` 对应"可以重建"，另加 `context_overflow`，`benign` 对应"非致命"。这四个值同时是 04 §4.6 适配层错误事件的 `class` 取值和 05 §7.1 的分类，三处只有这一套；未映射的错误码默认 `transient`（04 §4.6 规则 7、05 §7.1）。KM 2.12 的三值写法已作废。

### 3.10 usage 与成本

放在 `usage` 对象下。**上游 usage 按 response 逐条记**（一个工具回合可能有两个 response：带 function call 的那个，和回传后再生成的那个），回合级的值由收集器求和。字段集合取 LiveKit `RealtimeModelMetrics` 和 Pipecat `LLMTokenUsage` 的并集（`notes/ten-and-deployment.md` Q7、Q8-2）。

| 字段 | 类型 | 单位 | 写入方 | 必填 | 说明与出处 |
|---|---|---|---|---|---|
| `usage_semantics` | enum `net` / `gross` | — | 适配层（来自配置） | M | `net` 表示 `input_tokens` 不含缓存（Anthropic / Bedrock），`gross` 表示含缓存（OpenAI 兼容）。**汇总时不许把缓存数加回 `gross` 的输入**，否则会重复计算（Q7 第 1 点） |
| `responses[].response_id` | string | — | 适配层 | M | |
| `responses[].response_status` | enum `completed` / `cancelled` / `failed` / `incomplete` | — | 适配层 | M | 同 04 §4.3 `response.done.status` |
| `responses[].interrupted` | bool | — | 仲裁器 | M | **被打断的回复照样计费**，照样要记 usage（Q7 第 2 点） |
| `responses[].input_text_tokens` / `input_audio_tokens` | int | token | 适配层 | M | 音频 token 单价通常远高于文本 |
| `responses[].cached_input_tokens`、`cached_text_tokens`、`cached_audio_tokens` | int | token | 适配层 | M（上游提供时） | 同 04 §4.7 `usage` 事件字段名；KM 2.12 原写 `input_cached_tokens`，统一为 `cached_input_tokens`（与 §3.6 回合级字段同名） |
| `responses[].output_text_tokens` / `output_audio_tokens` | int | token | 适配层 | M | |
| `generated_audio_ms` | int | ms | 适配层 / 本地 TTS | M | 生成的音频总长 |
| `played_audio_ms` | int | ms | 客户端（播放回执的 `played_ms`） | M-A；B 取客户端播放事件 | 和 `generated_audio_ms` 对比，就知道"生成了但没播出去"的比例 |
| `input_audio_ms` | int | ms | 客户端 / 网关 | M | 按键时长或送上游的音频时长。按键说话时输入音频 token 应当和它成正比，偏离说明上游在计静音或有重发（Q8-2） |
| `upstream_session_duration_ms` | int | ms | 网关 | C（按会话时长计费的上游） | |
| `local_tts_chars` | int | 字 | 本地 TTS | M | 本地播报通道的成本 |
| `asr_audio_ms` | int | ms | 适配层 | C（级联 / 自控 ASR） | 注意 Pipecat 的 `STTUsage` 含静音（Q7） |

- **trace 里只记量，不记金额。** 金额在汇总时用带版本号的价格表换算（`price_table_version`）。调研里没有一个仓库把 usage 换算成钱（Q7）。
- **被打断回复的 token 要单独汇总**（`interrupted_output_tokens`、占总输出的比例）。儿童场景插嘴频繁，这一项可能很大（Q8-3 第 4 点）。

### 3.11 UI 事件（B）

| 字段 | 类型 | 单位 | 写入方 | 必填 | 说明与出处 |
|---|---|---|---|---|---|
| `ui_events[]` | array | — | 客户端 | M-B | `{type, at_ms, target, sentence_id?}`，其中 `type` 取 `state_change` / `transcript_shown` / `card_shown` / `field_shown` / `option_tap` / `field_edit` / `confirm_tap` / `hangup` / `handoff_tap` |
| `ui_audio_skew_ms` | int | ms | 网关（由 `ui_events` 和 `outputs` 算出） | M-B | 同一句的卡片 / 转写上屏时刻减去该句音频开始时刻，取本回合绝对值最大的一项；目标 ≤ 200【建议值】（两类架构 §3.2"看到的和听到的同步"） |
| `handoff_to_human` | bool | — | 工具层 | M-B | 有没有走转人工出口；必须由规则触发（两类架构 §3.4 原则 6） |
| `guardrail_result` | enum `pass` / `rewrite` / `block` / `n_a` | — | 工具层 | M-B | 出声前护栏的结果（两类架构 §3.2） |

### 3.12 设备事件（A）

| 字段 | 类型 | 单位 | 写入方 | 必填 | 说明与出处 |
|---|---|---|---|---|---|
| `device_events[]` | array | — | 客户端 → 网关 | M-A | `{event_id, type, at_ms, disposition, acked, deduped}`，其中 `type` 取 `location` / `nfc` / `timer` / `sensor` / `task_done`，`disposition` 取 `handle` / `context` / `respond` / `interrupt`（KM 3.3，qwen `AgentDelivery`） |
| `playback_receipts[]` | array | — | 客户端（网关补 `request_id`） | M-A（设备协议 P0 上线后） | `{sentence_id, request_id, started_at_ms, ended_at_ms, interrupted, played_ms, reason?}`。线上回执格式见 01 §3.1.6（不带 `request_id`，由网关按 `(turn_id, sentence_id)` 补，02 §4.2）；`sentence_id` 为回合内整数；`played_ms` 为句内时长 |
| `receipt_missing` | int | 个 | 网关 | M-A | 超时没收到的回执数 |
| `narration_attempts` / `narration_delivered` | int / bool | — | 仲裁器 | C（事件回合） | 回执缺失时有上限地重试；被打断的不重播（KM 3.3） |
| `stt_echo_timeout` | bool | — | 客户端 | M-A | 设备 15 s 没收到匹配的回显就判链路失效（总结 §2.1） |
| `feedback_signal` | enum `none` / `led` / `tone` | — | 客户端 | O | "所有状态必须可听"（两类架构 §2.4 原则 2） |

### 3.13 完整回合示例（A 类、按键、逐字念工具回合）

数字和 §2.4 的时间线一致，都是示意。

```json
{
  "trace_schema_version": "06-v0.1",
  "profile": "A",
  "session_id": "s_7f3a",
  "user_session_id": "u_2c91",
  "turn_id": "c42",
  "generation": 57,
  "turn_source": "button",
  "t0_epoch_ms": 1790000000000,
  "device_id": "dev_0193",
  "pipeline": "s2s:vendorX",
  "provider": "vendorX",
  "model": "realtime-2026-06",
  "prompt_version": "s2s/guide@3.4+tools@12+state@7",
  "gateway_build": "gw-1.18.2",

  "turn_kind": "tool_verbatim",
  "user_text": "下一站怎么走",
  "action": "next_stop_route",
  "decision_layer": "model",
  "speak_mode": "verbatim",
  "slots": {"target": "next_stop"},
  "tool_calls": [
    {
      "call_id": "call_9k2",
      "name": "get_route",
      "args": {"to": "next"},
      "presentation": "verbatim",
      "is_write": false,
      "latency_class": "ms",
      "tool_route": "A1",
      "generation": 57,
      "late_result_dropped": 0,
      "started_at_ms": 805,
      "done_at_ms": 815,
      "tool_exec_ms": 10,
      "reply_required": false,
      "closed_by": "result",
      "settled_at_ms": 816
    }
  ],

  "anchors": {
    "primary": "release",
    "audio_end_at_ms": -180,
    "release_at_ms": 0,
    "clock_quality": "rx_minus_half_rtt"
  },
  "latency": {
    "ptt_tail_silence_ms": 180,
    "stt_final_at_ms": 370,
    "seg": {"vad_ms": 190, "llm_ms": 610},
    "fc_received_at_ms": 800,
    "pre_tool_ms": 805,
    "tool_done_at_ms": 815,
    "output_sent_at_ms": 816,
    "sound_first_at_ms": 830,
    "local_speech_first_audio_at_ms": 1600,
    "content_first_ready_at_ms": 1060,
    "queue_wait_ms": 540,
    "content_first_at_ms": 1600,
    "content_kind": "answer",
    "post_tool_ms": 785,
    "cue_to_content_gap_ms": 10,
    "sound_first_played_at_ms": 905,
    "content_first_played_at_ms": null
  },

  "session_seq": 3,
  "rebuild_reason": "none",
  "state_block_version": "7",
  "state_update": "none",
  "reply_modality": "audio",
  "turns_in_upstream_session": 9,
  "cached_input_tokens": 3840,
  "uncached_input_tokens": 212,

  "press_ms": 1650,
  "speech_ms": 1290,
  "server_vad_segments_in_turn": 1,
  "compressed_silence_ms": 0,
  "dropped_short_press": false,
  "smart_turn_prob": 0.91,
  "supplement_window_hit": false,
  "interrupted": false,

  "outputs": [
    {"request_id": "req-c", "origin": "cue", "kind": "cue", "content_role": "filler", "generation": 57, "sentence_id": 0,
     "sent_at_ms": 830, "played_ms": 760, "status": "played"},
    {"request_id": "req-r", "origin": "tool_result", "kind": "speech", "content_role": "answer", "generation": 57, "sentence_id": 1,
     "sent_at_ms": 1600, "played_ms": null, "status": "played"}
  ],
  "double_voice_count": 0,
  "stale_frames_dropped": 0,
  "lease_violations": 0,
  "queue_truncated": false,
  "drops_by_reason": {"stale": 0, "expired": 0, "duplicate": 0, "overflow": 0, "retry_exhausted": 0},
  "receipt_timeouts": {"started": 0, "ended": 1},
  "consumer_dropped_frames": 0,
  "suppressed_frames": 0,
  "receipt_mode": "receipts",

  "uplink_seq_gaps": 0,
  "uplink_jitter_ms": 14,
  "rtt_ms": 88,
  "downlink_lead_ms": 320,
  "turn_terminal": "completed",
  "turn_closed_by": "T_drain",
  "restart_attempts_in_turn": 0,
  "watchdog_fired": "none",
  "stuck": false,

  "usage": {
    "usage_semantics": "gross",
    "responses": [
      {"response_id": "r_551", "response_status": "completed", "interrupted": false,
       "input_text_tokens": 3120, "input_audio_tokens": 932,
       "cached_input_tokens": 3840, "cached_text_tokens": 3050, "cached_audio_tokens": 790,
       "output_text_tokens": 18, "output_audio_tokens": 0}
    ],
    "generated_audio_ms": 2900,
    "played_audio_ms": null,
    "input_audio_ms": 1650,
    "local_tts_chars": 22
  },

  "device_events": [],
  "playback_receipts": [
    {"sentence_id": 0, "request_id": "req-c", "started_at_ms": 905, "ended_at_ms": 1665, "interrupted": false, "played_ms": 760}
  ],
  "receipt_missing": 1,
  "stt_echo_timeout": false
}
```

示例里 `sentence_id=1`（`req-r`）的回执没有收到，所以 `content_first_played_at_ms` 和 `played_audio_ms` 都记为 `null`，同时 `receipt_missing=1`。这就是 §2.1 第 5 条"缺失和不适用要区分"的写法。

---

## 4. 汇总维度与导出

### 4.1 汇总层级

**轮 → 会话 → 设备（A）/ 用户（B）→ 天。** 在每一层上都可以按下面这些维度切分：

| 维度 | 为什么必须切 | 出处 |
|---|---|---|
| `prompt_version`（可以只取前缀或后缀） | 换一版人设或提示语，不改门禁，按版本分批比较 | 总结 §7；KM 3.8 |
| `pipeline` / `route` | 三条链路的延迟结构不一样，混在一起没有意义 | 两类架构 §3.2 |
| `turn_kind` + `speak_mode` | 预算是按回合类型给的；"有内容"要能按本地直念 / 模型再生成 / 委派分开统计 | 两类架构 §2.3；BP A.4 |
| `anchors.primary` + `clock_quality` | 锚点和时钟质量不同的数据不能混算 | 本文 §2.1、§2.2 |
| `profile` | — | — |
| 人群分层（年龄段等，来自样本集或用户画像） | 儿童语音是已知的瓶颈 | BP C-X0 |

### 4.2 统计方法

1. **延迟报 P50 和 P90，不报平均值。** 平均值容易被一次冷连接拉偏，小智测试工具就是反例：只算平均值，样本约 5 次（`notes/xiaozhi-model-choices.md` §1.2 第 8 条）。做容量测试时可以另报 P95（`notes/ten-and-deployment.md` Q8-3）。
2. **每个分位数都要附带有效样本数 n 和失败率。** 失败（超时、没有终态、基础设施错误）不计入延迟分布，但要单独报比例（qwen bench"有效样本数和均值一起报"、"基础设施错误与行为失败分开记"，`notes/flows-and-evals.md` §2.3）。
3. **样本量下限**：
   - 报 P50：每组 n ≥ 20（BP B.8："每组 ≥ 20 次，总结 §5.4 的 6 次太少"）；
   - 报 P90：每组 n ≥ 50【建议值】。20 个样本的 P90 基本就是第 18、19 个值，太不稳定；n 不够时照样给出数值，但要标"样本不足"，而且不能用于门禁；
   - 通过率：每条脚本 N ≥ 10（BP C-X9 的"每条跑 10 次"）。
4. **token 和成本报总和与每轮均值**，这是唯一允许用平均值的地方，因为它们是可加的量。
5. **比例类指标**（重复调用率、写操作误触率、被打断 token 占比）要写清分子分母，比如"重复调用率 = 后续 3–5 轮内重复调同一工具的回合数 / 工具回合数"（BP A.4）。

### 4.3 OTel 导出

- **span 结构**：每个回合一个 span，名字是 `voice.turn`；子 span 分别是 `voice.stt`、`voice.llm`（或 `voice.s2s.response`）、`voice.tool.<name>`、`voice.tts`、`voice.playback`。span 的起点是 T0，子 span 的时间取 trace 里对应的点。
- **usage 沿用 `gen_ai.usage.*` 命名**，这样和 LiveKit / Pipecat 一致，可以直接接现成看板（`notes/ten-and-deployment.md` Q7、Q8-2）：

| trace 字段 | OTel span 属性 |
|---|---|
| 输入 token 合计（按 `usage_semantics` 还原成总额） | `gen_ai.usage.input_tokens` |
| 输出 token 合计 | `gen_ai.usage.output_tokens` |
| `input_audio_tokens` | `gen_ai.usage.audio.input_tokens`（Pipecat 在用的名字） |
| `output_audio_tokens` | `gen_ai.usage.audio.output_tokens` |
| `cached_audio_tokens` | `gen_ai.usage.audio.cache_read.input_tokens`（Pipecat 在用的名字） |
| `cached_input_tokens` | `gen_ai.usage.cache_read.input_tokens`【待定：要和 OTel GenAI 语义约定的当前版本逐项核对】 |
| `input_text_tokens` / `output_text_tokens` / `cached_text_tokens` | `gen_ai.usage.text.input_tokens` 等【建议值，仿照 audio 的命名空间自拟】 |
| `provider` / `model` | `gen_ai.provider.name` / `gen_ai.request.model`【待定：同上，核对语义约定版本】 |

- **其余字段用 `voice.*` 命名空间**，名字就是 trace 字段名，比如 `voice.latency.content_first_at_ms`、`voice.turn.kind`、`voice.tool.closed_by`、`voice.prompt_version`。
- **指标（metrics）**：延迟类用 histogram，单位 ms，比如 `voice.latency.sound_first`、`voice.latency.content_first`；计数类用 counter，比如 `voice.turn.stuck`、`voice.arbiter.double_voice`、`voice.usage.interrupted_output_tokens`。标签只用低基数的维度：`profile`、`pipeline`、`route`、`turn_kind`、`speak_mode`、`prompt_version` 前缀。`session_id`、`turn_id` 这类高基数字段只放在 span 上，不放进指标标签。

---

## 5. 评测分层

| 层 | 测什么 | 怎么跑 | 频率 / 门禁 | 出处 |
|---|---|---|---|---|
| **L0 决策** | 给定文本（和状态），动作 / 工具 / 参数 / 终态对不对 | 进程内，毫秒级，mock 工具，几百条 | 每次提交，进 CI | 总结 §7；KM 2.12 |
| **L1 对话** | 多轮脚本：状态变化、指代、打断、重连、不支持的请求；延迟预算 | 模拟客户端 + 模拟后端 + 真上游；文本模式和音频模式都要跑 | 发布前 | 同上；`notes/flows-and-evals.md` §6.2 |
| **L2 线上只读** | 真实会话上跑同一套逐轮标准 | 会话结束时离线跑，禁止写操作 | 持续，看板 + 告警 | 同上 |
| **音频层** | ASR、判停、TTS 在真实声学条件下的表现 | 合成 / 录音 + 噪声注入 + 真链路 | 发布前（换 ASR、TTS、VAD 时必跑） | BP C3；KM 2.12 |

### 5.1 L0 决策用例

**格式**：JSONL，每行一个用例（仿照 qwen smart-cockpit bench，`notes/flows-and-evals.md` §2.3）。

```json
{"id": "route_next_003",
 "set": "blind-zh-v1",
 "profile": ["A", "B"],
 "tags": {"age_group": "7-10", "category": "route"},
 "state": {"stage": "touring", "itinerary_version": 4, "current_stop": "熊猫馆", "task_active": false},
 "history": [{"user": "我们接下来去哪", "assistant": "下一站是海洋馆。"}],
 "turns": [
   {"user": "那个怎么走",
    "asr_variants": [
      {"kind": "homophone", "text": "那个怎么奏"},
      {"kind": "truncation", "text": "那个怎"},
      {"kind": "reduplication", "text": "那个那个怎么走走"}
    ],
    "expected_calls": [{"name": "get_route", "args_subset": {"to": "海洋馆"}}],
    "forbidden_calls": ["start_task", "adjust_itinerary"]}
 ],
 "expected_final_state": {"itinerary_version": 4, "task_active": false},
 "response_quality": {"rubric": ["understanding"], "evaluated": false}}
```

```json
{"id": "neg_write_ok_011",
 "set": "blind-zh-v1",
 "profile": ["A"],
 "state": {"stage": "plan_confirm", "route_read_to_user": false},
 "turns": [{"user": "好的", "expect_no_tool": true, "forbidden_calls": ["start_task"]}],
 "expected_final_state": {"task_active": false}}
```

**字段约定**：

| 字段 | 必填 | 含义 | 出处 |
|---|---|---|---|
| `id` / `set` | M | `set` 是样本集的版本名（§7），冻结盲测集和训练集要分开 | 总结 §7 |
| `profile` | M | 这条用例适用于哪个 profile；两个 profile 都适用的用例由两个团队共跑 | 两类架构 §4 |
| `state` | M | **状态块快照**作为输入，相当于 Pipecat turn-completion 的 `context:` | `notes/flows-and-evals.md` §6.2 |
| `history` | O | 预置的历史，可以含错误转写（LiveKit 做法） | 同上 §2.1 |
| `turns[].expected_calls[]` | C | `name` + `args_subset`（只比对列出的参数）；参数用规则归一化（别名表）比对，**不用 LLM 判** | qwen README；KM 2.12 |
| `turns[].expect_no_tool` | C | 这一轮不应调用任何工具 | qwen |
| `turns[].forbidden_calls` | O | 这一轮禁止出现的调用，主要是写操作的负例（比如"好的"不能触发 `start_task`） | BP C3 |
| `turns[].asr_variants[]` | O（盲测集要求每句 2–3 个） | 文本层 ASR 噪声变体，`kind` 取 `homophone` / `truncation` / `reduplication` / `accent`（B 类可以加 `term_error`，即专有名词错误） | BP C3、C-X8 |
| `expected_final_state` | C（有状态变化的用例） | **期望终态**，比如行程对象或表单的字段 | qwen `expected_final_state` |
| `response_quality` | O | 挂上 rubric id，默认不评；L0 不跑 judge | qwen `response_quality` 默认不评 |

**指标分开命名，不合成一个"准确率"**（BP C3；qwen README）：`action_hit`（动作命中率）、`slot_hit`（槽位命中率）、`no_tool_correct`（无工具轮正确率）、`write_false_trigger`（写操作误触率，分母是所有不该触发写操作的轮）、`final_state_match`（终态一致率）。**原句和各类 ASR 变体分开报**（C-X8"报出各自掉多少点"）。

**重试策略**：只有"超时，而且既没有工具调用也没有文字"才重试一次；基础设施错误和行为失败分开记（qwen README，`notes/flows-and-evals.md` §2.3）。

### 5.2 L1 对话脚本

**格式**：YAML，一个文件一个脚本。结构参照 Pipecat eval，在它的基础上加了我们需要的断言（`notes/flows-and-evals.md` §2.2、§6.2）。

```yaml
id: interrupt_during_tool_reply_02
profile: [A]
modality: [text, audio]          # 两种都跑；音频模式的用户话由 TTS 合成（§5.4）
race: true                       # 竞态脚本：只按通过率进门禁（§8.2）
repeat: 10                       # 默认重复次数，可以用 --repeat N 覆盖
mock_backend: fixtures/route_zoo_v3.json
setup:
  state: {stage: touring, current_stop: 熊猫馆}
turns:
  - user: "下一站怎么走"
    expect:
      - event: function_call
        name: get_route
        args_subset: {to: 海洋馆}
      - event: sound_first
        within_ms: 900             # 预算按 anchors.primary 计，A 按键即 T_rel
      - event: content_first
        within_ms: 1100
      - event: tool_closed
        closed_by: result
  - user: "等等，厕所在哪"
    send_after: {event: content_first, delay_ms: 1500}   # 在正文播放中插话
    expect:
      - event: bot_interrupted
      - event: function_call
        name: practical_info
        args_subset: {kind: toilet}
      - event: response
        eval: [understanding, honesty]
  - user: "嗯"                     # 附和语
    expect:
      - event: response
        absent: true
        within_ms: 3000            # 3 s 内不应该回应
      - event: turn_terminal
        value: discarded           # turn_closed_by: rule_silence
assert_session:
  stuck: 0
  double_voice_count: 0
  hanging_calls: 0                 # 没有 closed_by 的调用数
```

```yaml
id: sim_child_remove_stop_01
profile: [A]
mode: simulated                  # LLM 模拟用户
race: true
repeat: 10
persona:
  id: child_7y
  description: "7 岁孩子，短句，会跑题、改主意，常用'它''那个'指代，偶尔一两个字就不说了"
  goal: "把'爬行馆'从今天的路线里去掉；做不到就接受一个替代方案"
  end_when: goal_met_or_clearly_failed
judge:
  per_turn: [understanding, honesty, recovery, efficiency]
  session: [task_completion]
report: regression_only          # 只拿来找回归，不报绝对分（§6.4）
```

**断言与字段约定**：

| 构件 | 含义 | 出处 |
|---|---|---|
| `send_after: {event, delay_ms}` | 锚定在某个事件之后再发这句用户话，用来构造插话；可以锚定的事件包括 `llm_started`、`sound_first`、`content_first`、`fc_received`、`playback_started` | Pipecat `send_after` |
| `absent: true` + `within_ms` | 在时限内**不应**出现某个事件，用于噪音不回应、2 字白名单、被打断的播报不重播 | Pipecat `empty_user_turn.yaml` |
| `sound_first` / `content_first` 的 `within_ms` | 首音预算，口径严格按 §2 执行：从 T0 起算，锚点由 `anchors.primary` 决定 | 本文 §2；两类架构 §2.3、§3.3 |
| `function_call` 的 `name` + `args_subset` | 只比对列出的参数，归一化规则和 L0 相同 | Pipecat；qwen |
| `tool_closed.closed_by` | 断言收口的终态 | 本文 §3.5 |
| `eval: [rubric id…]` | 这一轮要跑的逐轮 judge 标准（§6） | — |
| `race` / `repeat` | 竞态标记；用 `--repeat N` 得出通过率，失败按类型聚合（`timeout` / `judge_no` / `missing_function_call` / `latency_budget` / `unexpected_event` / `infra`） | Pipecat `--repeat`、results.py |
| `persona` / `goal` | LLM 模拟用户。**至少要有两个 persona**：A 类是"7 岁孩子"和"家长"（BP C3）；B 类按业务人群定。目标要写成可以判定的形式 | `notes/flows-and-evals.md` §6.2 |
| `assert_session` | 会话级的硬指标，直接从 trace 里取 | 本文 §3 |

**必备脚本组**（每个 profile 都要有）：插话打断；工具各阶段的打断（对应 KM 2.5 权威表的四个阶段，A-E3）；空转写 / 噪音不回应；不支持的请求（honesty 和 recovery 配对判，`notes/flows-and-evals.md` §6.2）；断线重连 / 故障注入（B-X3 的四个注入点）；指代与早期信息召回（B-X4）。

### 5.3 L2 线上只读

- **触发**：会话结束时（或者每天批量）对真实会话**离线**跑 L1 的同一套逐轮 rubric。参照 LiveKit `JudgeGroup` 挂在会话结束时的做法（`notes/flows-and-evals.md` §2.1、§6.2）。
- **只读**：L2 不重放写操作，也不调后端的写接口；judge 的输入只来自 trace 和已经记录下来的工具结果。
- **judge 看到的文本**用"用户实际听到的"（按 `played_ms` 截断后的文本），被打断的地方标 `[interrupted]`（LiveKit 转录格式；KM 3.4"历史记用户听到的"）。
- **输出**：每轮一条评分记录，通过 `(session_id, turn_id)` 和 trace 关联；按 `prompt_version` 分批统计。
- **待归因队列**：任一标准判 no，或者任一硬指标越界（`stuck`、`double_voice_count > 0`、有内容超过预算的 2 倍【建议值】），这一轮就进入队列。归因之后给它打上根因标签（`asr` / `endpoint` / `decision` / `tool_data` / `presentation` / `latency` / `judge_error`），其中有代表性的补进 L1 脚本或 L0 用例（`notes/flows-and-evals.md` §6.2）。
- **抽样**：全量跑的成本太高时按天抽样，每天每个 `prompt_version` 不少于 200 轮【建议值】；低分的轮次全量进队列。

### 5.4 音频层评测

四个参考仓库都没有音频层的噪声注入、TTS 发音评测，也没有儿童语音样本，这些要我们自己建（KM 2.12；`notes/flows-and-evals.md` §3.3）。

| 集合 | 构成 | 跑法 | 指标 | 出处 |
|---|---|---|---|---|
| **合成 + 噪声小集合** | 20–30 条，用中文 TTS 合成目标人群的音色（A 类是儿童），叠加场景噪声，SNR 分 20 / 10 / 5 dB 三档 | 真链路：客户端模拟器 → 网关 → 上游 → 转写 → judge | CER、有声音 / 有内容、judge 分随 SNR 的变化 | BP C3；`notes/flows-and-evals.md` §6.4 E6 |
| **真人录音冻结集** | A：C-X0 儿童样本集（≥ 200 条、分年龄段、1/3 是句中停顿 ≥ 400 ms 的样本，带按键起止、语音段、是否一轮、参考文本的标注）；B：真人通话录音（分层方式见 §7.3） | 离线回放给 VAD / 判停 / ASR；也可以走真链路 | VAD 帧级 PR；切段率；单字识别率；CER（统一用 `normalize_zh`）；空结果率；松键 → 定稿 P50 / P90 | BP C-X0、C-X2–C-X4；`notes/asr-zh.md` §3.2 |
| **TTS 集** | 短句（6–15 字）+ TN 测试集（数字、时间、单位、字母、emoji） | TTS 离线合成，再用 ASR 转写回来做比对 | 首包 P50 / P90（裁掉首部静音后计时）、读错率、音色盲听 | `notes/tts-zh.md` §3.3 E2-a、E2-g、E2-e |

音频层的回放和测试脚本都必须遵守 §7.4 的口径要求。

---

## 6. judge rubric 规范

### 6.1 规则

| # | 规则 | 出处 |
|---|---|---|
| 1 | **逐轮二元判定**。分数 = 通过轮数占比，不给部分分 | Pipecat simulation.py；`notes/flows-and-evals.md` §3.2 第 1 条 |
| 2 | **"条件 + 条件外算通过"的写法**：每条标准写成"当……时，回复要……；不满足该条件的回复算通过"。否则 judge 会把"never"读成"always" | 同上第 2 条 |
| 3 | **`not_applicable` 做成一个结论选项**，而不是写成"情境不出现时算通过"的规则。后者会连带放过"应该做却没做"的回复 | 同上第 3 条 |
| 4 | **`continue` 选项**：回复只是提示语、前导语、承接句（"我查一下"）时判 `continue`，等后续内容出来再判，不判 no | 同上第 4 条；BP C3 |
| 5 | **对转写错误宽容**：同音字、近音字、数字写法不同，都按口语意思判；不能只因为转写错误就判 no | 同上第 5 条 |
| 6 | **被评对象和上下文分开传**：`latest_bot_reply` 单独给，前文放在 `context` 里 | 同上第 6 条 |
| 7 | **judge 必须看得到工具调用和工具结果**。看不到结果就判不出"诚实但显得不会"（Pipecat 的模拟评测恰恰看不到结果，这一点不照搬） | KM 2.12；总结 §7 |
| 8 | **honesty 和 recovery 配对判**：同一轮两个都判，报表里要报两者都通过的比例。改进 recovery 的时候不许让 honesty 下降（C-X6"honesty 不降"） | KM 2.12；BP C-X6 |
| 9 | **只有判 no 才写理由**；定期抽查判 yes 的结果做人工复核，抽样比例 ≥ 5%，每周一次【建议值】 | `notes/flows-and-evals.md` §3.2 第 7 条、§6.3 |
| 10 | **能量化的不用 judge**：`brevity` 按字数 / 句数用规则判；参数匹配用别名表判 | BP C3；KM 2.12 |
| 11 | **judge 本身要校准**：先用人工标注校准（低分轮 + 随机 50 轮），一致率 ≥ 85% 才可以用于门禁（C-X1）；judge 模型、prompt、temperature（固定为 0）都要写进结果记录 | BP C-X1；LiveKit judge temperature 0 |
| 12 | **模拟用户只拿来找回归，不报绝对分**。LLM 扮的孩子说话太完整、太礼貌；而且同一个任务每次的对话都不一样（模拟器漂移），分数的小幅变化不能归因到 prompt | KM 3.8；`notes/flows-and-evals.md` §6.3 |
| 13 | 判断回复和后端数据是否一致时，要把期望值写进标准，并且固定 mock | `notes/flows-and-evals.md` §3.2 第 9 条 |

### 6.2 输出格式

```json
{"session_id": "s_7f3a", "turn_id": "c43", "rubric_id": "recovery", "rubric_version": 2,
 "verdict": "no",
 "reason": "用户要求去掉爬行馆，回复只说'没有不经过那里的路线'，没有给任何替代",
 "judge": {"model": "judge-model-x", "prompt_version": "judge@5", "temperature": 0}}
```

`verdict` 只能取 `yes` / `no` / `not_applicable` / `continue` 四个值之一；`reason` 只在 `no` 时填。

### 6.3 rubric 模板

```yaml
rubric_id: recovery
version: 2
applies_to: [A, B]
scope: per_turn
inputs: [context, latest_bot_reply, tool_calls, tool_results, heard_text]
criterion: >
  当回复拒绝了或做不到用户这一轮提出的某个请求时，回复要给出至少一个具体、可行、
  来自工具结果或已知数据的替代（一次最多两个）；
  没有拒绝任何请求的回复算通过。
options:
  yes: 满足上述要求，或回复没有拒绝任何请求
  no: 拒绝或做不到时，没有给出具体替代，或者给出的替代不在工具结果 / 已知数据里
  not_applicable: 这一轮用户没有提出请求（比如纯寒暄、附和）
  continue: 回复只是提示语、前导语或承接句，内容还在后面
leniency:
  - 按口语意思判，不能只因为转写错误（同音字、数字写法）判 no
reason_required_for: [no]
pair_with: honesty
```

**基础标准集**（两个 profile 共用，业务可以在此基础上增加。出处：BP C3；`notes/flows-and-evals.md` §6.1）：

| rubric_id | 条件式写法（摘要） | 判定方式 |
|---|---|---|
| `understanding` | 回复回应的是用户这一轮的意图 | judge |
| `honesty` | 当回复声称某件事已经完成（已开启、已打卡、已改好）时，本回合要有支持这一点的工具结果；没有这类声称的回复算通过 | judge（看工具结果） |
| `recovery` | 见上方模板 | judge，和 `honesty` 配对 |
| `efficiency` | 当所需信息用户已经给过时，回复不要求用户重复（复述确认可以）；不涉及索取信息的回复算通过 | judge |
| `brevity` | A：不超过 2 句【建议值：面向儿童】；B：语音部分不超过 3 句【建议值】 | 规则 |
| `task_completion`（会话级，B 必跑） | 完成任务、恰当转接、正确拒绝，都算 pass | judge（LiveKit 口径） |
| `tone_consistency`（换声音、委派时） | 音色和语气没有割裂 | judge 或盲听（BP A-E4、B-X6） |

---

## 7. 冻结盲测集与样本集管理

### 7.1 冻结规则

1. **盲测集从未用于调参**：规则、prompt、阈值都不许针对盲测集里的样本修改（总结 §7）。
2. **冻结后只读**。增删改样本都要发一个新版本，旧版本保留、继续可跑；不同版本之间的分数不能直接比较。
3. **泄漏处理**：盲测集里的样本一旦被拿来做针对性调试（比如为了修它改了规则），就移到训练集，并且发新版本的盲测集。样本上看失败原因是允许的，但看过之后的修复不能只在它上面验证。
4. **每个版本都有清单文件**：版本名、冻结日期、样本数、分层统计、覆盖清单（§7.3）、来源、标注规范版本、校验和。
5. **两个团队的用例仓库分开，但格式统一**；标了 `profile: [A, B]` 的用例由两边共跑（两类架构 §4）。
6. **现有基线**：A 的 L0 盲测集是 63 句（总结 §3.1），命名为 `blind-zh-v1`；C-X0 儿童录音集冻结以后命名为 `audio-child-v1`。

### 7.2 版本命名

`<集合类型>-<语言/人群>-v<主版本>[.<次版本>]`，例如 `blind-zh-v1`、`asrvar-zh-v1`（盲测集的 ASR 变体）、`audio-child-v1`、`l1-scripts-A-v3`。主版本变化表示样本变了；次版本只用于修正标注，而且要在清单里写明修改了哪些条目。

### 7.3 分层与覆盖清单模板

按人群分层（A：年龄段 4–6 岁 / 7–10 岁，BP C-X0；B：按业务定，比如年龄段、方言区、首次 / 回访用户），报表要按层分开报。

```yaml
set: audio-child-v1
frozen_at: 2026-10-15
profile: A
size: 212
strata:
  age_group: {"4-6": 104, "7-10": 108}
  environment: {indoor_quiet: 90, outdoor: 72, doll_playing: 50}
coverage:                          # 每项写目标数和实际数
  mid_utterance_pause_ge_400ms: {target: 70, actual: 74}   # ≥ 1/3
  drawl: {target: 20, actual: 21}
  self_repetition: {target: 20, actual: 18}
  filler_word_ending: {target: 15, actual: 16}
  one_or_two_char_answer: {target: 20, actual: 22}
  accidental_short_press: {target: 15, actual: 15}
  proper_nouns: {target: 30, actual: 31}
annotation:
  spec_version: ann@2
  fields: [press_start, press_end, speech_segments, is_single_turn, reference_text]
source: 真设备按键录制
checksum: sha256:…
```

B 类的覆盖清单在上面的基础上增加：多约束的一句话、改字段、中途点选、转人工请求、高风险意图、背景人声。

### 7.4 测试脚本的口径要求

照搬现成测试工具之前必须改的地方（`notes/xiaozhi-model-choices.md` §1.2、§6.3；`notes/asr-zh.md` §3.2；`notes/tts-zh.md` §3.3）：

| # | 要求 | 反例 |
|---|---|---|
| 1 | **按实时节奏发音频**：按设备的实际帧长（A 类 Opus 帧长 20 ms，KM 2.1）逐帧发送，不许一次性灌入，也不许不限速连发 | 小智测试工具把整段音频压成最后一包，或者不限速连发 |
| 2 | **音频用样本集里的 16 kHz 单声道 PCM**，采样率要核对 | 把 44.1 kHz 的提示音当 16 kHz 送 |
| 3 | **计时分段**：建连、首个 partial、松键 → 定稿分开计；冷连接和长连接复用分开测；按键即建连和松键才建连分开测 | 把建连时间算进首词 |
| 4 | **只在确认收到音频帧时停表**：校验消息类型，控制帧、错误帧都不算 | 把第一条下行消息（可能是 ConnectionStarted）当首音 |
| 5 | **首音裁掉首部静音后再计时**（和 §2.3 一致） | — |
| 6 | **锚点用 T_ae**：合成音频后面拼接的尾部静音不算进用户说话时间；脚本要输出三个锚点，报数时注明用的是哪个 | — |
| 7 | **单并发、带真实工具表和真实 prompt** 测 LLM 首 token；并发测试另外单独做 | 不带工具、同一模型并发请求 |
| 8 | **输出 P50 / P90 和失败率**，不输出平均值；n 要满足 §4.2 | 只算平均值，而且分母算错 |
| 9 | ASR 测试同时输出识别文本，用来算 CER | 只测耗时 |

---

## 8. 门禁

### 8.1 三级门禁

| 级别 | 内容 | 通过条件 | 出处 |
|---|---|---|---|
| **CI（每次提交）** | L0 全量（训练集 + 冻结盲测集 + ASR 变体） | 每个命名指标相对主干基线下降不超过 2 个百分点【建议值】；`write_false_trigger` 不许上升；盲测集结果单独列出 | 总结 §7；BP C3 |
| | trace schema 校验 | 两个团队都用同一份 JSON Schema 校验自己产出的样例 trace（覆盖普通、工具、打断、错误四类回合），任何一个字段不合规都算失败 | 两类架构 §4 |
| | 确定性的 L1 文本脚本（`race: false`，mock 上游） | 全部通过 | — |
| | 跨团队用例 | 标了 `profile: [A, B]` 的 L0 用例双方都要跑 | 两类架构 §4 |
| **发布前** | L1 全量（真上游，文本模式 + 音频模式） | 非竞态脚本全部通过；竞态脚本按 §8.2 执行 | 总结 §7 |
| | 延迟预算 | 按 `turn_kind` 分组，P50 ≤ 两类架构 §2.3 / §3.3 的目标；P90 按各实验表里给的值执行（例如 A-E4 的 P90 ≤ 2.0 s），没给值的按"P50 目标 × 1.5"【建议值】；n 满足 §4.2 | 两类架构 §2.3、§3.3；BP A.4 |
| | 硬指标 | `stuck` = 0，`double_voice_count` = 0，悬挂调用 = 0，写操作重复执行 = 0 | BP A.4、A-E3、B-X3 |
| | 音频层 | 换 ASR / TTS / VAD / 降噪时必跑；指标不劣于上一版【阈值待定】 | BP C-X5 |
| | judge 标准 | 逐轮通过率不低于上一版减 3 个百分点【建议值】；honesty 不许下降 | BP C-X6 |
| **线上监控** | L2 逐轮评分、延迟 P50 / P90、`stuck`、双声、错误率（按 `error_class`）、重建次数、缓存命中、被打断 token 占比、`played/generated` 比例、数据缺失率（`null` 比例） | 看板按 `prompt_version` 分批显示；告警阈值由各团队定，但**告警的指标名必须和本文字段一致** | KM 2.12；`notes/ten-and-deployment.md` Q8-3 |

### 8.2 竞态用例的规则

1. **单次通过不说明问题**。有竞态的东西（打断、异步工具、判停、重连、事件插话、模拟用户）单次运行只能说明过没过，重复跑才能说明多常过（Pipecat README；KM 3.8）。
2. **标了 `race: true` 的脚本只按通过率进门禁**，N ≥ 10，门槛是通过率 ≥ 90%【建议值】。单次失败不阻断发布，但要进失败类型聚合。
3. **没有通过率的竞态脚本不作门禁**（BP C3"有竞态的脚本没有通过率就不作门禁"）。新加的竞态脚本先进入"观察"状态；在连续两个基线版本上测出的通过率相差不超过 10 个百分点【建议值】，才转为门禁。方差大的脚本先不作门禁（C-X9）。
4. **模拟用户脚本永远不作绝对门槛**，只做回归比较：同一个 persona 在新旧版本上各跑 N 次，出现显著下降才报警（§6.1 第 12 条）。
5. **有竞态的硬指标例外**：`stuck`、双声、写操作重复执行这几项即使出现在竞态脚本里，也是**出现一次就失败**（BP A-E3"卡死 0"、B-X3"写操作重复执行为 0"）。这几项是正确性要求，不是概率问题。

---

## 9. profile 差异表

| 项 | A 无界面硬件 | B 有界面移动端 |
|---|---|---|
| 主锚点 | 按键时是 T_rel；开放麦时是 T_ep | T_ep；按住说话（降级）时是 T_rel；点选时是点击时刻 |
| 有声音 / 有内容 | 只有音频 | 音频口径同 A；另记 `ui_feedback_first_at_ms`、`ui_content_first_at_ms`，不合并进首音 |
| 延迟预算（两类架构） | §2.3：有声音 ≤ 0.9 s，有内容 1.0 / 1.1 / 1.3 s，首句到正文的空档 ≤ 1.5 s | §3.3：判停 0.3–0.6 s，有声音 ≤ 1.0 s，有内容（语音）≤ 1.5 s，工具回合 ≤ 2 s |
| 独有的 trace 字段 | 设备事件（`device_events`、`disposition`）、播放回执、`receipt_missing`、`narration_*`、`server_vad_segments_in_turn`、短按丢弃、补充窗口、`downlink_lead_ms`、`stt_echo_timeout` | UI 事件（上屏时刻、点选、改字段）、`ui_audio_skew_ms`、`route` / `risk_level`、`stage`、表单版本、`guardrail_result`、`handoff_to_human` |
| 独有的评测项 | **音频层样本集**（儿童录音 `audio-child-v1`、合成 + 噪声、停顿压缩回放）；设备事件插话（A-E6）；沉默即故障（无声 > 2.5 s 就算 `stuck`，§3.9）；`brevity` ≤ 2 句 | **任务完成率**（会话级 `task_completion`）；**结构化抽取准确率**（字段级，对照标注的金标准表单，按规则归一化后算 precision / recall）；**产出物质量**（摘要卡片的 rubric：覆盖所有关键字段、没有编造事实、下一步可执行）；**UI 同步**（`ui_audio_skew_ms` 的 P90、转写和卡片与音频按句对齐）；分流合规（高风险回合走了文本先于语音的比例应为 100%）；出声前护栏的拦截率和误拦率 |
| persona（L1 模拟用户） | 7 岁孩子、家长 | 按业务人群定（至少两个） |
| 共跑用例 | 所有标了 `profile: [A, B]` 的 L0 / L1 用例，比如收口、打断各阶段、不支持的请求、指代 | 同左 |

B 类指标的目标值目前都还没有定，只规定口径。数值等业务方确定之后再写进本文的次版本。

---

## 10. 验收要点与开放问题

### 10.1 验收要点

两个团队各自对照下面这张清单自查，全部满足才算"遵守本契约"：

1. 每个回合恰好一条 trace，能通过 `turn-trace.schema.json` 的校验；`trace_schema_version` 写的是本文版本。
2. 三个锚点能测到的都写了；`anchors.primary` 和 `clock_quality` 必填；所有时间字段都是相对 T0 的毫秒整数。
3. 有声音 / 有内容按 §2.3 的判定表判定，`outputs[]` 里每段输出都带 `kind` 和 `content_role`；首部静音裁掉后才计时。
4. 工具回合有 `pre_tool_ms` / `post_tool_ms`，每个调用都有 `closed_by`（六个值之一）和 `settled_at_ms`。
5. usage 按 response 逐条记，带 `usage_semantics`，被打断的回复也有 usage；`played_audio_ms` 和 `generated_audio_ms` 都有。
6. 缺失字段写 `null`，不适用的字段省略，二者没有混用。
7. OTel 导出的 usage 属性用 `gen_ai.usage.*`，其他属性用 `voice.*`；指标标签里没有高基数字段。
8. 报表只报 P50 / P90，并附带 n 和失败率；n 不够的标了"样本不足"。
9. L0 用例、L1 脚本、rubric 和本文的格式一致；对方团队的共跑用例能直接跑起来。
10. 冻结盲测集有版本清单；CI 里盲测集的结果单独列出。
11. 竞态脚本都标了 `race: true`，只按通过率进门禁。
12. judge 能看到工具结果；判 yes 的结果有定期抽查复核的记录。

### 10.2 开放问题

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| 1 | ~~逐字念工具回合的预算与提示语冲突~~ **已决定**（review B-1） | A1 的验收标准（A-E2） | 保持用户听到的口径；A1 用短 cue（≤ 300 ms）+ 固定前缀预合成，工具在 cue 开播前已完成则不播 cue；架构 §2.3 改为"有声音 ≤ 0.9 s、有内容 ≈ 1.1 s"；仲裁规则不变。A-E2 用长 cue / 短 cue / 无 cue 三组对照验证（§2.4） |
| 2 | 总结 §3.2"逐字念 0.6–0.7 s"的起点没有核实 | A1 上游通道的估算，以及 A-E2 的门槛 | 用本文口径重新测一次（`notes/review-knowledge-map.md` F6） |
| 3 | 设备时钟同步（KM 2.1 P2）还没做 | 回执口径的精度，以及 `clock_quality` 的分布 | P2 之前门禁只用送出口径 |
| 4 | 首部静音的判定阈值（−45 dBFS / 10 ms）是拍的 | 流式输出的有声音计时 | 在 `audio-child-v1` 和 TTS 集上校准 |
| 5 | B 类的"有声音"要不要把 UI 变化也算进去（**开放，产品定**；review B-7-7） | 两类架构 §3.3 的 B 类有声音 ≤ 1.0 s 写的手段是"UI 立即变化 + 语音填充"；影响 B 的门禁 | 本文把两者分开记；B 的门禁要不要把 UI 纳入，由产品决定 |
| 6 | OTel GenAI 语义约定里缓存 token、provider 的属性名，以及 `gen_ai.usage.text.*` 这个命名空间 | 看板兼容 | 对照 OTel semconv 的当前版本和 Pipecat 实际用的名字逐项核对，之后发次版本 |
| 7 | 一个回合里有多个工具调用时，`tool_done_at_ms` 该取哪个调用 | `post_tool_ms` 的口径 | 先用"有内容之前最后一个完成的调用"，按实际数据再评估 |
| 8 | A3 的"进度收口"算不算有内容 | A3 回合的首音统计 | 暂时算有内容，并单独归类为 `content_kind=progress` |
| 9 | ~~`error_class` 从三个值改成四个值~~ | 已处理：KM 2.12 开头已注明权威表移至本文；04 §4.6、05 §7.1 已改用同一套四值 | 关闭 |
| 10 | P90 的样本量下限（50）、竞态门槛（90%）、L0 允许的降幅（2 个百分点）、judge 抽查比例（5%） | 门禁松紧 | 跑完 C-X9（L1 重复度）和 C-X1（judge 校准）后回填 |
| 11 | schema 文件、共享的 trace 收集器由谁维护 | 契约怎么落地（两类架构 §6 开放问题 3） | 最低要求是 schema 文件和共跑用例放在同一个仓库；是否做成共享实现另议 |
| 12 | B 类指标的目标值（任务完成率、抽取准确率、UI 同步）都还没有 | B 的发布门禁 | 先只报数不设门槛，积累两个版本的基线之后再定 |
