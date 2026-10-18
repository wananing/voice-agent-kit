# 级联测速：`vak bench` 和参考数据

`vak bench` 用来回答"哪套级联组合在我的网络下最快"。它对 STT、LLM、TTS 的每个候选分别计时，最后把各环节 P50 相加，给组合排序。

本文所有数字都标了来源。分三种：

- **【本机实测】**：`vak bench` 在你自己的环境里跑出来的。
- **【外部报告】**：别人的测试，我们没复现。
- **【作者实测】**：作者自己测过，但没有留下数字和测试条件。

只有【本机实测】可以直接写进延迟预算，另外两种只能用来缩小候选范围。

## 1. 用法

```bash
# 1. 录一段 2–4 s 的真人中文，16 kHz 单声道 PCM16 WAV，放在 bench.yaml 旁边
# 2. 设好各家的 key（没设的候选会被跳过，不影响别的）
vak bench examples/bench/bench.yaml --site "上海 电信家宽" --out result.json
vak bench examples/bench/bench.yaml --only llm --runs 10      # 只测一个环节
```

`bench.yaml` 的格式见 `examples/bench/bench.yaml`：`stt` / `llm` / `tts` 三个列表，每项就是 agent.yaml 里的 provider 块。可以给候选加 `name:` 起别名，同一 provider 换不同参数时用得上（比如方舟开、关思考各测一次）。可选字段有 `system_prompt`、`prompts`、`sentences`、`tools`，不写就用默认的系统提示词、3 句测试句和 2 个工具。

输出分两部分：

1. **终端表格**：第一行是来源行，写明 vak 版本、UTC 时间、`site`、音频文件、`runs` 和 `warmup`。
2. **`--out` JSON**：包含 `source`（同上，另加 Python 版本、平台、音频时长）、`metrics`（每个指标的定义）、每个候选的原始样本和 P50/P90/min/max、`combinations`（组合排序）。

贴数字时请连同 `source` 一起贴。

## 2. 测试口径

| 环节 | 主指标 | 计时 | 和小智 `performance_tester` 的区别 |
|---|---|---|---|
| STT | `final_ms` | 调 `finish()`（用户说完）到拿到 final 文本。另报 `first_partial_ms`（第一帧音频到第一条非空中间结果）和 `connect_ms`（`start()` 耗时） | 小智从建连前开始计时，并且把整段音频一次灌完。这里按 20 ms 一帧实时推流；连接在说话前就建好，因为级联在回合开始就启动 STT，建连时间单独报告 |
| LLM | `first_segment_ms` | 发请求到切出第一段可以送 TTS 的文本。切句规则就是级联用的 `vak.audio.text.split_sentences`：首段至少 6 字，碰到逗号就切。工具调用也算"等待结束"。另报 `first_token_ms` 和 `total_ms` | 小智不带 tools，同一模型的几句并发发出。这里带上系统提示词和工具表，请求串行发；测之前先调 `warmup()`，和级联回合开始时的预连一致 |
| TTS | `first_audio_ms` | 发请求到第一个**非空 PCM** 块。另报 `total_ms`，以及 `rtf`（合成耗时 ÷ 音频时长） | 小智的火山双流式测试把第一条下行消息当首音，没校验消息类型。我们的 provider 只产出音频块，控制帧不会被计入 |
| 组合 | `estimate_ms` | 三个主指标 P50 之和 | 小智也是分模块各测各的，再把每项第一名拼成组合。**两边都不是端到端实测**。真实首音还要加上判停静音、网络到端侧、播放缓冲，见 `docs/architecture.md` §11 |

统计口径：每个候选先丢掉 `warmup` 次请求（默认 1 次），剩下的算 P50、P90、min、max。出错的请求单独计数，不进入统计。

STT 每次运行都会新建一个实例，和级联每回合一个实例的做法一致。

**已知局限：**

- 没有统计字错率，只把识别文本打出来，供人工看一眼识别是否正常。
- 一次只测一路请求，不反映并发或限流下的表现。
- TTS 是整句送入（`synthesize`）。`volcengine_tts` 在级联里是 token 级送入（一轮回复一个会话），这条路径的首音要在完整回合的 trace 里看（`vak trace summarize`），`vak bench` 测不出来。

## 3. 参考数据

### 3.1 小智外部报告【外部报告】

- **来源**：`xinnan-tech/xiaozhi-performance-research`（https://github.com/xinnan-tech/xiaozhi-performance-research ），在 `xiaozhi-esp32-server` 的 `README.md:207` 和 `docs/FAQ.md:103` 里被引用。读的是 HEAD `90c6601`（2026-08-07）。
- **测试条件**：工具是 `xiaozhi-esp32-server/main/xiaozhi-server/performance_tester/`；地点是广州海珠区，中国联通宽带；各表的测试时间写在表头。
- **报告自己的声明**：属于内部技术评估，"不具备权威评判性"。
- **口径问题**：以下是读测试工具代码时发现的，写在各表的"注意"列里。小智项目的整体分析见手册的 [xiaozhi-esp32-server](https://github.com/wananing/voice-agent-handbook/blob/master/04-projects/devices/xiaozhi-esp32-server.md) 页。

**全 API 最快组合（报告结论）**：

| 环节 | 选型 | 报告数字 | 注意 |
|---|---|---|---|
| ASR | 讯飞流式 ASR | 首词 0.795 s | 测试端点是经典听写 `iat-api.xfyun.cn/v2/iat`，生产 provider 用的是大模型听写，两者不一致；而且含建连、整段灌入 |
| LLM | 百炼 qwen-flash-2025-07-28 | 首 token 0.434 s | 明细表里是 0.670 s，报告前后矛盾 |
| TTS | 火山双流式 TTS | 首音 0.317 s | 测试没发 StartSession，很可能量到的是第一条控制帧 |

**LLM 首 token 明细**：2026-08-07 10:41，"小智超长角色提示词"约 6.1k 字，不带 tools，并发发出。

| 模型 | 首 token |
|---|---|
| qwen3-8b | 0.587 s |
| qwen3.6-flash | 0.600 s |
| qwen3.6-35b-a3b | 0.601 s |
| qwen-flash-2025-07-28 | 0.670 s |
| doubao-seed-2-0-mini-260215 | 0.700 s |
| kimi-k2 | 0.774 s |
| qwen-plus-2025-12-01 | 0.821 s |
| deepseek-v4-flash | 0.901 s |

**流式 ASR 首词**：2026-04-22 14:01。口径是从建连到第一条结果，整段音频一次灌完；测试音频是 24k/44.1k 的提示音，却按 16k 送进去。

| 模型 | 首词 |
|---|---|
| XunfeiStreamASR | 0.795 s |
| DoubaoStreamASRV1 | 0.852 s |
| DoubaoStreamASRV2 | 0.864 s |
| AliyunStreamASR | 0.984 s |
| Qwen3ASRFlash | 1.023 s |

**流式 TTS 首音**：2025-09-30 10:33，口径是从建连到第一个下行块，数据已有一年。

| 模型 | 首音 | 注意 |
|---|---|---|
| PaddleSpeechTTS（本地） | 0.103 s | 生产路径会攒齐整句才下发，这个数字用不上 |
| XunFeiTTS | 0.253 s | |
| IndexStream | 0.312 s | 服务端是整段返回，实际是整句合成耗时 |
| HuoshanDoubleStreamTTS | 0.317 s | 可能是控制帧，见上 |
| AliyunStreamTTS | 0.712 s | |

百炼 CosyVoice 双流式在测试工具里有，但报告里没有它的数字。

### 3.2 方舟 doubao-seed-2-0-mini-260428【作者实测】

作者在 2026-10 自测：doubao-seed-2-0-mini-260428 在**开快速推理、关思考**的设置下最快。没有留下数字和测试条件。

据此，`ark` provider 的默认模型定为这个，并默认发 `thinking: {type: disabled}` 和 `service_tier: fast`（快速推理，即"在线推理（低延迟）"模式），见 `vak/upstream/providers/llm/vendors.py`。要对比快速推理的效果，可以在 bench.yaml 里给方舟写两条候选，一条加 `name: ark-default` 和 `extra_body: {service_tier: default}`。外部报告测的是旧版 260215，0.700 s，不能拿来和这里比。

### 3.3 本机实测

还没有。跑完 `vak bench` 后，把 `--out` 的 JSON 和来源行一起补在这里，不同 `site` 的结果分开列。
