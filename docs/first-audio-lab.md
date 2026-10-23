# 首音实验指南

本文说明如何用 vak 测量级联链路的首音、定位耗时所在的分段，以及如何逐项验证各项优化的实际收益。各项优化的原理见 voice-agent-handbook 的 [first-audio](https://github.com/wananing/voice-agent-handbook/blob/master/03-mechanisms/first-audio.md) 页，本文只讨论在 vak 中如何测量与对照。

本文中的数字均标注来源，标注方式与 [bench.md](bench.md) 相同。

## 1. 首音的构成

规范 06 定义了两个首音指标：**有声音**（`latency.sound_first_at_ms`，本回合第一次播出非静音音频）和**有内容**（`latency.content_first_at_ms`，第一次播出回答本身）。两者都相对于回合的时间零点 T0 计算，T0 的取法见规范 06 §2.2。

级联链路上，"有内容"的首音可以近似分解为以下几段：

```
有内容 ≈ STT 定稿 + 判停 + LLM 首个输出 + 首段切出 + TTS 首包 + 下行与播放缓冲
```

每一段在 trace 中都有对应字段，由 vak 自动写入：

| 分段 | trace 字段 | 起止 | 说明 |
|---|---|---|---|
| STT 首字 | `latency.seg.stt_first_word_ms` | 首个语音帧送入 STT → 首个非空转写 | 反映流式 STT 的响应速度 |
| STT 定稿 | `latency.stt_final_at_ms` | T0 → 识别定稿 | 时间点，不是时长 |
| 判停 | `latency.seg.vad_ms` | 用户说完 → 发起回复请求 | 仅开放麦；按键模式由松键直接触发，没有这一段 |
| LLM 首个输出 | `latency.seg.llm_ms` | 发起回复请求 → 第一个文本增量（S2S 为第一个音频增量） | 受模型、思考模式、网络与连接复用影响 |
| 首段切出 | `latency.seg.first_segment_ms` | LLM 首词 → 首段文本提交给 TTS | 包含切句等待和出声前护栏；TTS 按 token 送入时接近 0 |
| LLM 首词到首包 | `latency.seg.tts_start_ms` | LLM 首词 → 首个音频包 | 减去 `first_segment_ms` 即 TTS 首包耗时 |

分段只统计本回合第一次请求的回复。工具回合在此基础上还有工具执行与再生成两段，见规范 06 §2.4。

## 2. 查看分段

```bash
uv run vak trace summarize my_agent/traces/*.jsonl
```

输出的第一张表是按回合类型、锚点和时钟质量分组的首音 P50 / P90；第二张表是普通回合的分段 P50 / P90。单个回合的完整记录在 `traces/*.jsonl` 中，`latency` 对象包含上表所有字段。

阅读数字时需注意：

- **样本量。** P50 至少需要 20 个回合，P90 至少需要 50 个（`vak/core/defaults.py` 的 `min_n_p50`、`min_n_p90`），不足时表中会注明"样本不足"。
- **冷启动。** 每个会话的第一个回合通常包含建连时间，与后续回合的分布不同，对照实验时应分开统计或剔除。
- **按键模式的 T_ae。** 按键模式下网关不运行本地 VAD，"用户说完"的时刻（`anchors.audio_end_at_ms`）按松键时刻记录，因此松键前的尾部静音会计入说话时长。
- **时钟质量。** "有声音""有内容"按设备与网关的时钟对齐方式分组，不同组之间不能直接比较；分段字段全部取自网关一侧的单一时钟，不受此影响。

## 3. 级联已有的优化

下表列出 vak 级联链路中与首音相关的优化、影响的分段，以及做对照实验时需要修改的位置。只有第 5 项提供了配置开关；其余各项需要修改代码或配置才能关闭，修改位置已在表中注明。

| # | 优化 | 影响的分段 | 位置 | 对照方法 |
|---|---|---|---|---|
| 1 | 双向流式 STT：边说边识别，连接在回合开始时建立 | STT 定稿 | `vak/upstream/providers/stt/` 各 provider | 将 `stt` 换成非流式的 `openai_stt` |
| 2 | 回合开始时预连 LLM 与 TTS | LLM 首个输出、TTS 首包 | `vak/upstream/pipelines/cascade.py` 的 `_warm()`；间隔由 `upstream.warmup_interval_ms` 控制 | 注释掉 `_warm()` 中的调用 |
| 3 | HTTP 连接保活 60 s | LLM 首个输出 | `vak/upstream/providers/_util.py` 的 `KEEPALIVE_S` | 改为 5（httpx 默认值） |
| 4 | 首段短切：首段至少 6 字、遇逗号即切，14 字强制切 | 首段切出 | `vak/audio/text.py` 的 `SplitParams` | 修改 `first_min_chars`、`first_force_chars` |
| 5 | TTS 预取：当前句播放时合成下一句 | 句间衔接（不影响首音） | `vak/upstream/pipelines/textout.py` 的 `_speak()` | `agent.yaml` 中设置 `tuning: {upstream.tts_lookahead: 0}` |
| 6 | TTS 按 token 送入：一轮回复一个会话，由引擎切句 | 首段切出、TTS 首包 | `textout.py` 的 `_open_text_stream()`；provider 声明 `supports_text_stream` | 将 `vak/upstream/providers/tts/volcengine.py` 中的 `supports_text_stream` 改为 `False` |
| 7 | LLM 关闭思考；方舟启用 `service_tier: fast` | LLM 首个输出 | `vak/upstream/providers/llm/vendors.py` | 在 `llm` 配置中设置 `thinking: true`，或 `extra_body: {service_tier: default}` |

第 2、3 项的收益来自建连开销，只能在真实网络上测量。

## 4. 在真实上游上做实验

1. **选型。** 先用 `vak bench` 在自己的网络下比较候选厂商的各段耗时（[bench.md](bench.md)）。`vak bench` 测量的是单个环节，组合排序是估算值。
2. **端到端测量。** 用选定的组合运行 agent，完成至少 20 个普通回合，再用 `vak trace summarize` 查看首音与分段。
3. **逐项对照。** 每次只改动第 3 节表中的一项，其余条件保持不变，各收集同等数量的回合后比较分段的 P50 / P90。
4. **记录条件。** 记录测试地点与网络、时间、厂商与模型、样本量，以及是否剔除了冷启动回合。数字对外引用时请附上这些条件，并按 [bench.md](bench.md) 的方式标注来源。

## 5. 局限

- 分段只覆盖网关一侧。下行网络、设备缓冲和播放的耗时需要依靠客户端的播放回执（`latency.*_played_at_ms`），参考客户端已经支持，自研设备需要按 [protocol.md](protocol.md) 实现。
- 只调用工具、没有文本的回复，不会记录 `first_segment_ms` 和 `tts_start_ms`。
- S2S 链路只记录 `llm_ms`（请求到第一个音频增量），没有切句和 TTS 两段。
