# 00 术语与枚举总表

> **开源说明**：本文是 voice-agent-kit 的契约规范之一，正文保留设计期原貌。代码注释里的"spec 0X §y"指本目录的规范。文中的出处简写（架构 / 两类架构、KM、BP、总结、HC、PC-B、LK-B、UM、`notes/*.md` 等）和 `../../knowledge-map.md` 一类路径指向作者未公开的调研笔记，对应内容已整理进 [voice-agent-handbook](https://github.com/wananing/voice-agent-handbook)，按 [README 的对照表](README.md#出处简写对照) 查。

> 状态：草案 v0.1（2026-09-29）。由 01–06 六份规范的审校整合而来（审校记录见 `review-specs.md`）。
> 作用：同一概念只有一个名字、一套取值。六份规范里的术语表是本表的子集，出现分歧时以本表为准；本表的每一项都注明"定义出处"，改定义要同时改出处规范。
> 上位文档：`../voice-agent-two-architectures.md`（下称"架构"）§1、§4。规范简称：01 回合模型与客户端协议；02 输出仲裁器；03 工具注册表与调用收口；04 链路适配层；05 上下文与会话；06 trace 与评测。

---

## 1. 会话与回合

| 术语 | 字段 / 取值 | 定义 | 定义出处 |
|---|---|---|---|
| **连接会话** | `session_id` | 一次连接生命周期（含续接窗口内的重连）的上下文单位，网关在 `hello` 应答里分配。06 trace 的 `session_id` 即此 | 01 §1.3 |
| **续接窗口** | `resume_window`，默认 30 s | 断线后网关保留连接会话（含上游会话）、允许客户端带 `session_id` 续接的时长；过期回 `session_expired` | 01 §6.2 |
| **用户会话** | `user_session_id`（05 §2.1 条目、06 §3.2 trace） | 从用户开始对话到明确结束或会话恢复窗口过期为止；可跨多个连接会话、多个上游会话 | 05 §1.3 |
| **会话恢复窗口** | 默认 A 30 min | 续接窗口过期后，凭快照恢复同一用户会话的时长；上游会话按 05 §4 重建。**不要**再叫"续接窗口" | 05 §8.1 |
| **上游会话** | `session_seq`（第几个） | S2S 服务端持有的一次会话；"会话代数" = 同一用户会话下的第几个上游会话 | 05 §1.3；06 §3.6 |
| **回合** | `turn_id` | 一次用户输入（或一次事件播报）及其全部输出，从 `turn.start` 到 `turn.closed` | 01 §1.3 |
| **`turn_id` 格式** | `c<n>` / `s<n>`，帧头 `uint32` 最高位区分 | 检测到边界的一方生成：客户端 `c`、网关 `s`；先后以代际为准。文档示例一律用这个格式（不再用 `t41`、`t-0041`、`t_0042`） | 01 R2.2-1、R2.2-2 |
| **段** | `segment` | 同一回合的用户音频被上游或判停切出的子单元，不产生新 `turn_id` | 01 §1.3、§2.6 |
| **合并** | `turn.merge{from, into}` | `holding` 状态下的第二次按键并入原回合；合并后只有一个回合、一条 trace、一条本地 `user` 条目；不 +1 代际 | 01 R2.6-4 |
| **句** | `sentence_id`（int，回合内从 0 递增） | 下行分句单元，`(turn_id, sentence_id)` 唯一；回合内对 cue / speech / narration **统一编号**，网关据此查出 `request_id` | 01 §1.3、§3.2.1 |
| **边界来源** | `source` / trace `turn_source`：`button` / `wake` / `vad` / `text`（上行）+ `event`（仅下行，事件播报回合） | 回合开始 / 结束的依据；优先级 `button > text > wake > vad` | 01 §1.3、§2.3 |
| **输入方式** | `input` / trace `turn_input`：`typed` / `tap` / `test` | `source=text` 时的细分；B 类点选、改字段是 `text` + `tap`（不另设 `tap` 来源） | 01 §3.1.4；06 §3.2 |
| **上行窗口** | `listen.open / listen.close` | 麦克风上行窗口，会话级；开放麦下一个窗口跨多个回合。**不是** v1 / KM / 总结里的 `listen.start / stop`（那是回合边界，对应 `turn.start / turn.end`） | 01 R2.3-3、§3.4 |
| **松键** | `turn.end{source:"button"}`（v1：`listen.stop`） | 06 的锚点 T_rel | 01 §3.4；06 §2.2 |

## 2. 终态（四套，互不混用）

| 对象 | 字段 | 取值 | 定义出处 | trace |
|---|---|---|---|---|
| **回合终态** | `turn.closed.state` | `completed` / `interrupted` / `discarded` / `failed` / `cancelled`（五个） | 01 §2.4 | 06 §3.9 `turn_terminal`；原因记 `turn_closed_by`（`discarded` 的原因：`short_press` / `short_speech` / `false_wake` / `rule_silence`） |
| **调用终态** | `closed_by` | `result` / `interrupted` / `cancelled` / `progress` / `superseded` / `duplicate`（六个，不得增删） | 03 §4.2（语义权威） | 06 §3.5 |
| **上游回复终态** | `response.done.status` | `completed` / `cancelled` / `failed` / `incomplete` | 04 §4.3 | 06 §3.10 `response_status` |
| **输出请求终态** | 02 请求生命周期 | `done` / `interrupted` / `dropped`（原因 `stale` / `expired` / `duplicate` / `overflow` / `retry_exhausted`） | 02 §5.1 | 06 §3.8 `outputs[].status`、`drops_by_reason` |

另有 05 条目的 `playback_state`（`pending` / `playing` / `completed` / `interrupted` / `skipped`），描述单条 assistant 条目的播放结果，不是终态概念（05 §2.1）。

## 3. 仲裁

| 术语 | 字段 / 取值 | 定义 | 定义出处 |
|---|---|---|---|
| **代际** | `gen` / `generation`（整数） | 会话内单调递增；+1 的情况只有三种：新用户回合开始、用户打断、critical 抢占。打断与新回合同时发生只 +1 一次；被合并的 `turn.start` 不 +1。低代际输出一律丢弃 | 01 §1.3；02 §3.2 |
| **播放代际** | `play_gen` | 设备本地的二级代际，只在设备内部，不上协议 | 01 §5.4 |
| **回复 id** | `response_id`（上游）/ `response_ref`（调用方） | 一次上游回复的标识；**不是代际**。仲裁器把它绑定到发起时的 OutputRequest 及其代际 | 04 §1.5、§3.4 |
| **输出请求** | `request_id` | 进仲裁器的每一个"想出声"的东西；下游二次过滤的键 | 02 §2.1 |
| **来源** | `origin`：`model` / `tool_result` / `delegated_result` / `cue` / `task_progress` / `task_result` / `reminder` / `device_event` / `system_notice` / `system_exit` | 产生这条输出的组件。06 `outputs[].origin` 用同一取值（原草案的 `source: cue_lib / local_tts` 作废） | 02 §2.2 |
| **种类** | `kind`：`cue` / `speech` / `narration` | 决定是否需插话窗口、是否被冲刷 | 02 §1.3、§2.4 |
| **内容角色** | `content_role`：`filler` / `answer` / `progress` / `error` | 判定"有声音 / 有内容" | 06 §2.3 |
| **话筒租约** | `(lease_id, generation)` | "我现在可以出声"的凭证，授权时签发，绑定当前代际 | 02 §3.3 |
| **播报领取** | claim，TTL 60 s，TTL/3 续期 | 同一条 narration 在多连接 / 重连之间只能被一个连接领取（原文"领取租约"） | 02 §3.3；05 §8.3 |
| **前台租约** | — | 同一用户会话同一时刻只有一条连接能驱动上游和出声（原文"会话归属租约"） | 05 §8.3 |
| **插话窗口** | — | narration 可开始出声的时机：用户不在说 ∧ 回合已结束 ∧ 无未播完音频，再静默 350 ms | 02 §3.4 |
| **回执** | `playback.started / ended / interrupted {turn_id, sentence_id, played_ms, ts_ms, reason?}` | 客户端上报的播放事实；线上不带 `request_id`，网关按 `(turn_id, sentence_id)` 补上交给仲裁器 | 01 §3.1.6；02 §4.2 |
| **`played_ms`** | 回执字段 | **句内**实际播放时长 | 01 §3.1.6 |
| **截断位置** | `audio_end_ms` | 条目内播放位置 = `offset_ms(sentence_id) + played_ms`（换算到条目起点）；是 04 `truncate` 原语的参数。不要把单句 `played_ms` 直接当截断位置 | 01 §5.5；04 §3.6 |
| **冲刷** | 线上帧 `flush.start{flush_id, turn_ids}` / `flush.end`（网关发）；本地 TTS `tts_flush` / `flush_end`（TTS 回） | 丢弃某范围尚未播出的输出；`flush.end` 之后这些回合零音频。设备不回确认帧，只在仍在播时发 `playback.interrupted` | 01 §3.2.5；02 §3.5 |
| **回执超时** | 开始回执：下发完成 + 5 s；结束回执：剩余未播时长 + 2 s（= 01 `T_drain`） | 回执缺失的兜底 | 02 §3.7；01 §2.5 |

## 4. 工具

| 术语 | 字段 / 取值 | 定义 | 定义出处 |
|---|---|---|---|
| **呈现方式** | `presentation`：`verbatim` / `model` / `screen` / `silent` | 工具结果怎么到达用户；委派不是呈现方式 | 03 §1.3、§2.1 |
| **执行时长** | `latency_class`：`ms` / `seconds` / `background` | 注册表字段（原 06 草案的 `duration_class: instant / seconds / long` 作废） | 03 §2.1 |
| **提示语长度** | 注册表 `cue`：`long` / `short` / `none` | 工具层按路线选的 cue；默认 A1 为 `short`（≤ 300 ms），A2 / A3 为 `long`；工具在 cue 开播前已完成则不播。仲裁规则不变（cue 不冲刷、不被抢占） | 03 §2.1；02 §3.5 |
| **路线** | trace `tool_route`：`A0` / `A1` / `A2` / `A3` / `silent` | 工具回合的出声路线 | 03 §3 |
| **选路** | trace `route`：`s2s` / `cascade` / `half_cascade` | B 类按回合选链路；与 `tool_route` 不是一回事 | 架构 §3.2；06 §3.2 |
| **出声方式** | `speak_mode`：`verbatim` / `model` / `delegated` / `screen` / `none` | 本回合内容"怎么说出来的" | 06 §3.3（采纳 03 §10.1） |
| **收口 / 再生成** | `reply_required` | 收口 = 调用在上游和本地都有终态；再生成 = 回传后请模型再说。二者分开决定 | 03 §1.3；架构 §1 原则 1 |
| **静默收口** | `send_tool_result(reply_required=false)` | 回传但不触发再生成；能否做由能力声明 `tools.silent_close` 决定 | 04 §1.5、§3.7 |
| **`settled` 位** | — | 调用终态位，置位后拒绝一切迟到结果 | 03 §1.3 |
| **打断阶段** | trace `interrupt_phase`：`received` / `running` / `done_not_sent` / `sent_speaking` / `progress_closed` / `narration` / `delegating` | 对应 03 §5.1 打断矩阵 7 行 | 03 §5.1；06 §3.5 |

## 5. 上下文与会话

| 术语 | 字段 / 取值 | 定义 | 定义出处 |
|---|---|---|---|
| **已播放文本** | 05 `text_presented`；03 output 的 `spoken_to_user` / `played_text`；04 `truncate` 的 `played_text`；06 judge 输入 `heard_text` | 同一个量：用户实际听到（B 类另含看到）的文本。由**上下文层**按回执换算（05 §2.1 `interrupt_at`），仲裁器只提供回执 | 05 §1.3、§2.1；02 §1.2 |
| **镜像** | — | 上游会话里的内容；只写不读，不作为重灌来源 | 04 §1.5；05 §1.3 |
| **重灌** | — | 用本地上下文建新上游会话、在建会话参数里带入历史 | 04 §1.5；05 §1.3 |
| **灌入格式** | `history.format` / trace `history_format`：`pairs` / `packed` / `pairs_tools_as_text` | 历史带入上游的格式 | 04 §3.1；05 §3.2；06 §3.6 |
| **带入范围** | trace `history_scope`：`full` / `truncated` / `summary_recent` | 全量 / 截断到最近 K 轮 / 摘要 + 最近 K 轮 | 05 §4.1、§5.5；06 §3.6 |
| **重建原因** | `rebuild_reason`：`none` / `disconnect` / `upstream_error` / `context_overflow` / `state_change` / `turn_limit` / `scheduled` / `modality` / `app_resume`（B） | 会话重建的触发；原 05 的 `fatal` / `state` / `timer` / `overflow` 和原 06 的 `fatal_error` 作废 | 05 §4.2；06 §3.6 |
| **回复模态** | `reply_modality`：`audio` / `text_only` / `mixed` / `none` | 上游本轮回复的模态；音频会话出现 `text_only` 即告警 | 05 §3.5；06 §3.6 |
| **状态块** | `schema_version` / `state_rev` | 系统提示末尾的结构化短文本；分"立即生效 / 懒生效"两层 | 05 §2.2 |

## 6. 错误

| 术语 | 字段 / 取值 | 定义 | 定义出处 |
|---|---|---|---|
| **错误分类** | 04 事件 `error.class` = 06 trace `error_class` = 05 §7.1 四类：`fatal`（重建也没用）/ `transient`（会话失效可重建）/ `context_overflow`（上下文超限，先缩历史再重建）/ `benign`（非致命） | 一套枚举三处共用；未映射错误码默认 `transient`，受重建预算保护。KM 2.12 的三值写法作废 | BP B.5；KM 3.4；架构 §4"错误四分类"；06 §3.9 |
| **错误原因** | 04 `error.reason`：`auth` / `quota` / `billing` / `invalid_config` / `session_lost` / `timeout` / `throttled` / `cancel_race` / `empty_commit` / `duplicate` / `moderation` / `item_rejected` / `tool_parse` / `unknown` | 分类之下的细分，供策略层判断 | 04 §4.6 |
| **回合错误码** | `error.code` / trace `error_code`：`no_speech` / `asr_timeout` / `upstream_unavailable` / `upstream_timeout` / `upstream_rejected` / `tts_failed` / `audio_gap` / `link_lost` / `protocol_error` / `session_expired` / `internal` | 客户端协议里的回合级错误；与上游错误分类是两层 | 01 §3.2.4 |
| **重建预算** | 每回合 3 次；指数退避，首次 ≤ 0.5 s、封顶 10 s；连接稳定 10 s 清零 | 防毒回合无限重建 | 05 §7.2（依据 KM 3.4、BP B.5、LK-B、PC-B） |

## 7. 常用数值（出现在多份规范里的同一个量）

| 量 | 值 | 权威出处 | 引用处 |
|---|---|---|---|
| 下行超前量 | 200–500 ms，起点 300 ms | 01 R4.3-1（KM 2.1） | 架构 §2.2；06 `downlink_lead_ms` |
| 设备播放缓冲上限 | ≤ 1.2 s | 01 R4.3-4（KM 2.11） | 02 §3.4 |
| 沉默填充 | 提交后 2.0 s 无声插 cue（`T_filler`） | 01 §2.5（架构 §2.4 原则 1） | 02 §6；05 §7.2；06 `stuck`（阈值 = `T_filler` + 500 ms = 2.5 s，06 §3.9） |
| 结束回执兜底 | 剩余未播时长 + 2 s（`T_drain`） | 01 §2.5 | 02 §3.7 |
| 开始回执超时 | 下发完成 + 5 s（A、B） | 02 §3.7 | 05 §8.3 |
| 打断回执等待 | 300 ms（`T_intr_receipt`） | 01 §2.5 | — |
| 插话窗口静默 / 自唤醒 | 350 ms / 1 s | 02 §3.4、§3.7（qwen） | — |
| narration 重试上限 | 结果 / 提醒 8 次，进度 0 次 | 02 §3.6（qwen） | 05 §8.3 |
| 进度节流 | 首条 ≥ 60 s、全局间隔 ≥ 60 s、新覆盖旧 | 02 §4.3（qwen，KM 2.8） | 03 §2.1 `progress.throttle` |
| 播报领取 TTL | 60 s，每 TTL/3 续期 | 02 §3.3（qwen） | 05 §8.3 |
| 重建预算 | 3 次 / 封顶 10 s 退避 / 稳定 10 s 清零 | 05 §7.2 | 04 §4.6、§5；06 `restart_attempts_in_turn` |
| 续接窗口 / 会话恢复窗口 | 30 s / 30 min | 01 §6.2 / 05 §8.1 | — |
| 链路失效判定 | 客户端 15 s 无 `stt` 回显；网关写阻塞 10 s | 01 §2.5（总结 §2.1） | 06 `stt_echo_timeout` |
