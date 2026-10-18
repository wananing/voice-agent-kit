# Providers

真实 provider 的接入说明：类型名、配置键、环境变量、可选依赖、已知缺口，以及 `vak probe` 的用法。代码在 `vak/upstream/providers/`，能力声明在 `vak/upstream/capabilities/`，探针在 `vak/probes/`。

所有 provider 都遵守规范 04 §1.4：只翻译协议，不重试、不重连、不计回合。缺依赖或缺密钥时抛 `ProviderConfigError`，错误信息写明要装什么、设哪个变量。

## 1. 一览

| 类型名（`agent.yaml` 的 `type`） | 接口 | 类 | 用在链路 | 可选依赖（extra） | 能力声明 |
|---|---|---|---|---|---|
| `openai_realtime` | `RealtimeS2S` | `OpenAIRealtime` | `s2s` | 无（`websockets` 是基础依赖） | `openai_realtime.yaml` |
| `openai_realtime_text` | `AudioLLM` | `OpenAIRealtimeAudioLLM` | `half_cascade`（Realtime 文本模态 + 本地 TTS） | 无 | 同上 |
| `dashscope_realtime` | `RealtimeS2S` | `DashScopeRealtime` | `s2s` | 无 | `dashscope_realtime.yaml` |
| `funasr` | `STT` | `FunASRStreamingSTT` | `cascade` | 无 | 无（级联由 pipeline 声明） |
| `volcengine_stt` | `STT` | `VolcengineStreamingSTT` | `cascade` | 无 | 无 |
| `aliyun_stt` | `STT` | `AliyunStreamingSTT` | `cascade` | 无 | 无 |
| `openai_realtime_stt` | `STT` | `OpenAIRealtimeSTT` | `cascade` / `half_cascade` 旁路转写 | 无 | 无 |
| `deepgram_stt` | `STT` | `DeepgramStreamingSTT` | `cascade` | 无 | 无 |
| `assemblyai_stt` | `STT` | `AssemblyAIStreamingSTT` | `cascade` | 无 | 无 |
| `openai_stt` | `STT` | `OpenAITranscribeSTT` | `cascade`（非流式，不推荐） | 无（用 `httpx`） | 无 |
| `openai_compat` | `LLM` | `OpenAICompatLLM` | `cascade` | 无 | 无 |
| `ark` | `LLM` | `ArkLLM` | `cascade` | 无 | 无 |
| `bailian` | `LLM` | `BailianLLM` | `cascade` | 无 | 无 |
| `cosyvoice` | `TTS` | `CosyVoiceTTS` | `cascade` / `half_cascade` | 无 | 无 |
| `volcengine_tts` | `TTS` | `VolcengineStreamingTTS` | `cascade` / `half_cascade` | 无 | 无 |
| `aliyun_tts` | `TTS` | `AliyunStreamingTTS` | `cascade` / `half_cascade` | 无 | 无 |
| `openai_tts` | `TTS` | `OpenAITTS` | `cascade` / `half_cascade` | 无 | 无 |
| `moshi` | `FullDuplexModel` | `MoshiClient` | `full_duplex` | `opus`（`opuslib` + 系统 libopus） | `moshi.yaml` |

注册表：`vak.upstream.providers.PROVIDERS`；`build_provider(type_name, config, **kwargs)` 按 `type` 实例化（`kwargs` 透传 `clock` / `is_committed` / `client` / `codec`）；`capabilities_for(type_name)` 读静态能力声明，级联构件返回 `None`。

### 1.1 流式 STT 的共同约定

级联的 STT 一律用双向流式：按下即建连，说话期间边送音频边收 partial，松键发结束信号，只等最后一小段定稿。所有流式 STT 遵守同一条语义（`SttTurn` 在第一个 final 处停止读取）：

- 松键（`finish()`）之前只出 `final=False` 的 partial，文本是**整句累计**（已定稿的段 + 当前未定稿段），重复的不发。厂商在中途标的"定稿 / 句末 / end_of_turn"都并进累计文本，不当 final。
- `finish()` 之后**只出一个** `final=True`，文本是整次按键的全部内容。
- 结束信号之后连接断开、报错或超时，仍用已有文本补一个 final，并记 warning。
- 不重试、不重连；`cancel()` 幂等。

`vak new --pipeline cascade --providers ...` 的预设：`cloud`（`openai_realtime_stt` + OpenAI LLM / TTS）、`volcengine`（火山识别 + 方舟 + 火山合成，一套火山 key）、`aliyun`（百炼识别 + 百炼 LLM + CosyVoice 云合成，一个 `DASHSCOPE_API_KEY`）、`selfhost_zh`（FunASR + 自托管 CosyVoice）。Deepgram / AssemblyAI 把 `stt` 块换成下面的配置即可。

## 2. 配置键与环境变量

### `openai_realtime` / `openai_realtime_text`

| 键 | 默认 | 说明 |
|---|---|---|
| `api_key` | `$OPENAI_API_KEY` | 必填其一 |
| `base_url` | `wss://api.openai.com/v1/realtime` | 兼容服务（如 huggingface/speech-to-speech）改这里 |
| `model` | `gpt-realtime` | |
| `dialect` | `ga` | `ga`（`output_modalities`、嵌套 `audio.*`）或 `beta`（`modalities`、`OpenAI-Beta: realtime=v1`，旧 `*-realtime-preview` 模型 / 兼容服务） |
| `turn_detection` | `{type: server_vad, threshold: 0.9, silence_duration_ms: 500}` | `null` 关闭服务端判停；`{type: semantic_vad, create_response: false, interrupt_response: false}` 只切段不回复 |
| `voice`, `instructions`, `tools`, `modalities` | — | 建会话时的 `session.update` |
| `input_audio_transcription` | `{model: gpt-4o-mini-transcribe}` | `null` 关闭用户转写 |
| `history` | `[]` | `HistoryItem` 列表，**只在 connect 时**逐条 `conversation.item.create`（规范 04 §3.1） |
| `connect_timeout_ms` / `ready_timeout_ms` | 10000 / 5000 | 等 `session.created` / `session.updated` |
| `input_sample_rate`（仅 `openai_realtime_text.feed`） | 16000 | 线上 24 kHz，`append_audio` 内部重采样 |

事件映射见 `realtime_common.py` 顶部注释与规范 04 §2.4：`response.audio.delta` / `response.output_audio.delta` 都解成 PCM16 24 kHz，带 `item_id` 和 `duration_ms`；`response.function_call_arguments.done` → `tool.call`（参数完整；`before_commit` 由构造时传入的 `is_committed()` 回调决定）；`response.done` → `response.done` + `usage`（含按模态的缓存 token）。`add_tool_output` **不**发 `response.create`：静默收口靠省略（`tools.silent_close=by_omission`）。

### `dashscope_realtime`

| 键 | 默认 | 说明 |
|---|---|---|
| `api_key` | `$DASHSCOPE_API_KEY` | |
| `base_url` | `wss://dashscope.aliyuncs.com/api-ws/v1/realtime` | Qwen3.8-Omni 要业务空间专属地址 `wss://<WorkspaceId>.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime` |
| `model` | `qwen-omni-turbo-realtime` | |
| `turn_detection` | `{type: semantic_vad}` | Audio 系列可用 `{type: smart_turn}`；`null` 能否关闭待 P0-2 / P0-11 |
| `nested_audio_config` | `false` | Qwen3.8-Omni 用嵌套 `session.audio`，设 `true` |
| 其余 | 同 `openai_realtime`（beta 方言） | |

差异：输入 16 kHz、输出 24 kHz；`input_audio_format: pcm`；用户转写另有 `conversation.item.input_audio_transcription.text` 事件；Omni 不回显客户端 item id；错误按消息子串分类（`errors.code_stability=message_match`）。

### `funasr`

| 键 | 默认 | 说明 |
|---|---|---|
| `url` | `ws://localhost:10095` | `wss://` 自签证书时 `ssl_verify: false` |
| `sample_rate` | 16000 | 发给服务端的 `audio_fs`；8k/16k |
| `chunk_size` / `chunk_interval` | `[5,10,5]` / 10 | 600 ms 块、回看 / 前瞻 300 ms |
| `hotwords` | `""` | 字符串或 `{词: 权重}`；只对离线那一遍生效 |
| `itn` | `true` | |

协议：首包 JSON（`mode: "2pass"`）→ 二进制 PCM → `{"is_speaking": false}`。`2pass-online` 片段拼成 partial，`2pass-offline` 定稿一段（`Transcript.segment` 递增）；一次按键可能出多段，松键前的段并进 partial，松键后的那段 offline 结果带上全部段作为 final。

### `volcengine_stt`（火山引擎豆包大模型流式识别）

| 键 | 默认 | 说明 |
|---|---|---|
| `app_key` / `access_key` | `$VOLC_ASR_APP_KEY` / `$VOLC_ASR_ACCESS_KEY` | 放在握手头 `X-Api-App-Key` / `X-Api-Access-Key` |
| `resource_id` | `volc.bigasr.sauc.duration` | `X-Api-Resource-Id` |
| `mode` | `async` | `async` → `bigmodel_async`（双向流式优化版，默认）；`stream` → `bigmodel`；`nostream` → `bigmodel_nostream`（流式进、结束才出结果） |
| `url` | 由 `mode` 决定 | 显式指定时覆盖 `mode` |
| `result_type` | `full` | 每包带完整 utterance 列表，漏包不丢字 |
| `enable_itn` / `enable_punc` / `enable_ddc` | `true` / `true` / `false` | |
| `enable_nonstream` | `false` | 二遍识别（仅 async） |
| `hotwords`, `boosting_table_name`, `end_window_size`, `language`（仅 nostream）, `sample_rate`(16000) | | |

协议：自定义二进制帧（4 字节头 + 序号 + 长度 + gzip 负载）。`start()` 发 full client request（seq 1），`feed()` 攒约 100 ms 一包发 audio-only，`finish()` 把剩余音频作为最后一包（负序号、last 标志）。服务端 `result.utterances[].definite` 标记已定稿的段。参考：`xiaozhi-esp32-server/.../asr/doubao_stream.py`、`ten-framework/.../bytedance_llm_based_asr/`。

### `aliyun_stt`（阿里云百炼 / DashScope 实时识别）

| 键 | 默认 | 说明 |
|---|---|---|
| `api_key` | `$DASHSCOPE_API_KEY` | |
| `model` | `paraformer-realtime-v2` | `fun-asr-realtime` 可填，走同一协议，但参考实现里没有，未核实 |
| `url` | `wss://dashscope.aliyuncs.com/api-ws/v1/inference` | 国际站 `wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference` |
| `language_hints`, `vocabulary_id`（v2）, `disfluency_removal_enabled`, `semantic_punctuation_enabled`, `max_sentence_silence`, `sample_rate`(16000) | | 设了才发 |

协议：`run-task`（`streaming: duplex`）→ 等 `task-started`（之前到的音频先缓存，到了按序补发）→ 二进制 PCM → `finish-task` → `task-finished`。`result-generated` 每次带当前句全文，`sentence_end` 为句末。参考：`xiaozhi-esp32-server/.../asr/aliyunbl_stream.py`、`ten-framework/.../aliyun_asr_bigmodel_python/`。

### `openai_realtime_stt`（OpenAI Realtime 转写会话）

| 键 | 默认 | 说明 |
|---|---|---|
| `api_key` | `$OPENAI_API_KEY` | |
| `base_url` | `wss://api.openai.com/v1/realtime` | 连接时加 `?intent=transcription` |
| `model` | `gpt-4o-transcribe` | 或 `gpt-4o-mini-transcribe` |
| `language`, `prompt`, `noise_reduction`（`near_field` / `far_field`）, `sample_rate`(16000) | | |

协议：GA 版 `session.update`（`session.type: transcription`，`turn_detection: null`）→ `input_audio_buffer.append`（重采样到 24 kHz、base64）→ 松键 `input_audio_buffer.commit` → `...input_audio_transcription.delta` / `.completed`。**注意**：服务端判停关掉后，只有 commit 之后才开始转写，按住期间没有 partial；省掉的是上传时间，识别本身仍在松键之后。中文场景首选火山或阿里。

### `deepgram_stt`

| 键 | 默认 | 说明 |
|---|---|---|
| `api_key` | `$DEEPGRAM_API_KEY` | `Authorization: Token <key>` |
| `model` / `language` | `nova-3` / `en` | |
| `punctuate` / `smart_format` | `true` / `false` | |
| `endpointing` | 不发（服务端默认） | 毫秒或 `false` |
| `keyterms`（Nova-3）, `keywords`（Nova-2 及更早）, `url`, `sample_rate`(16000) | | |

协议：`wss://api.deepgram.com/v1/listen?encoding=linear16&interim_results=true...` → 二进制 PCM → 松键发 `{"type":"Finalize"}`，带 `from_finalize: true` 的结果即本次 final，随后发 `CloseStream`。

### `assemblyai_stt`（Universal-Streaming v3）

| 键 | 默认 | 说明 |
|---|---|---|
| `api_key` | `$ASSEMBLYAI_API_KEY` | `Authorization: <key>` |
| `speech_model` | `universal-streaming-english` | 其他语言换多语种模型 |
| `format_turns` | `true` | |
| `keyterms`, `url`, `sample_rate`(16000) | | |

协议：`wss://streaming.assemblyai.com/v3/ws?encoding=pcm_s16le...` → 二进制 PCM → 松键发 `ForceEndpoint` 和 `Terminate`；`Turn` 消息的 `end_of_turn` + `turn_is_formatted` 为一段结束，松键时打开的那一段结束即 final，兜底是 `Termination`。

### `openai_stt`

| 键 | 默认 |
|---|---|
| `api_key` | `$OPENAI_API_KEY` |
| `base_url` | `https://api.openai.com/v1` |
| `model` | `gpt-4o-mini-transcribe` |
| `language`, `prompt`, `sample_rate`(16000), `timeout_s`(30) | |

### `openai_compat`

| 键 | 默认 | 说明 |
|---|---|---|
| `model` | 必填 | |
| `base_url` | `https://api.openai.com/v1` | vLLM / Ollama / DashScope 兼容模式都行 |
| `api_key` | `$LLM_API_KEY`，再 `$OPENAI_API_KEY` | 本地服务可不填 |
| `include_usage` | `true` | 发 `stream_options.include_usage`；服务端不支持时设 `false` |
| `temperature`, `extra_body`, `timeout_s` | | |

工具调用分片按 `index` 拼装，**只在参数解析成 JSON 对象后**发一条完整的 `LLMDelta(tool_call=...)`（规范 04 规则 10）；解析失败的调用丢弃并记日志。`cancel()` 关闭 SSE 流。

### `ark`（火山方舟）/ `bailian`（阿里百炼）

两家都是 OpenAI 兼容接口，类继承 `OpenAICompatLLM`，`openai_compat` 的配置键都能用。区别只在默认值，以及**默认关闭思考**：思考 token 排在可朗读文本之前，会把首音推后数秒。开关取自 xiaozhi-esp32-server 按域名下发的参数。

| | `ark` | `bailian` |
|---|---|---|
| `base_url` | `https://ark.cn-beijing.volces.com/api/v3` | `https://dashscope.aliyuncs.com/compatible-mode/v1`（国际站 `dashscope-intl`） |
| `api_key` | `$ARK_API_KEY` | `$DASHSCOPE_API_KEY` |
| `model` | `doubao-seed-2-0-mini-260428`（也可填 `ep-...` 接入点） | `qwen-flash` |
| 关思考 | `thinking: {type: disabled}` | `enable_thinking: false` |
| 推理模式 | `service_tier: fast`（快速推理） | — |

`thinking: true` 打开思考；`extra_body` 里的同名键优先。

方舟的 `service_tier: fast` 与思考开关无关，总是发送。接入点有低延迟限流配额时，请求优先走"在线推理（低延迟）"；没有配额或配额用满时，自动降级到常规模式，不会报错。可选值还有 `auto`（方舟默认，优先 TPM 保障包）、`default`（只用常规）、`flex`（低优），用 `extra_body: {service_tier: auto}` 覆盖。低延迟配额需要在方舟控制台给接入点单独开通，没开通时 `fast` 等同常规。

方舟默认模型 `doubao-seed-2-0-mini-260428`：2026-10 作者实测，豆包系列里它在开快速推理（`service_tier: fast`）、关思考时首字最快。百炼的 `qwen-flash` 取自 xiaozhi 的 `config.yaml`，未实测。上线时在控制台选定模型后显式写 `model`。各家首字数据和来源见 `docs/bench.md` §3，自己的网络下用 `vak bench` 实测。

### `cosyvoice`

| 键 | 默认 | 说明 |
|---|---|---|
| `url` | `http://localhost:50000` | `CosyVoice/runtime/python/fastapi/server.py` |
| `mode` | `sft` | `sft` / `zero_shot` / `cross_lingual` / `instruct2` |
| `voice`（= `spk_id`） | `中文女` | `sft` 模式 |
| `prompt_wav`, `prompt_text`, `instruct_text` | — | 非 `sft` 模式必填 `prompt_wav` |
| `sample_rate` | 24000 | CosyVoice1 是 22050，自己声明 |
| `chunk_bytes` | 9600 | 200 ms |

### 1.2 流式 TTS 的共同约定（`volcengine_tts` / `aliyun_tts`）

- **会话粒度。** `aliyun_tts` 一句一个会话；`volcengine_tts` 在级联里一轮回复一个会话（token 级，见下），直念和 cue 仍是一句一个会话。连接长驻：`warmup()`（级联在回合开始时调用）或第一个会话时建连，之后跨句、跨回合复用，省掉每次的握手。
- **连接池。** pipeline 预取一句时会有两句同时合成；一条连接同时只跑一个会话，所以池里按需开第二条，空闲最多留 2 条。复用前检查连接仍然打开；发送失败直接丢弃连接并抛错，不重试。
- **打断。** `flush()` 立刻停止产出；后台发取消 / 结束信号并排空剩余音频，确认会话结束后才放回池子，超时（5 s）就关掉连接，绝不把还在跑的会话交还池子。
- **文本怎么送。** `volcengine_tts` 声明 `supports_text_stream = True`：级联把 LLM 的每个 token 增量作为一个 TaskRequest 推给同一个会话，由服务端切句，句界从 TTSSentenceStart 回来，再按句发事件（`docs/architecture.md` §11 第 12 条）。配了护栏时改为推送放行后的整句。`aliyun_tts` 仍是按句送全文（`supports_text_stream = False`），百炼的 `continue-task` 也能增量送，需要时照火山的接法补 `open_text_stream`。

### `volcengine_tts`（火山双向流式合成 v3）

| 键 | 默认 | 说明 |
|---|---|---|
| `app_key` / `access_key` | `$VOLC_TTS_APP_KEY` / `$VOLC_TTS_ACCESS_KEY`，没有则用 `$VOLC_ASR_*` | 握手头 |
| `resource_id` | `volc.service_type.10029` | 2.0 模型见 docstring（`seed-tts-2.0`） |
| `voice` | `zh_female_shuangkuaisisi_moon_bigtts` | |
| `sample_rate` | 24000 | |
| `speech_rate` / `loudness_rate` | 不发 | -50..100 |
| `emotion`, `url` | | |

协议：`wss://openspeech.bytedance.com/api/v3/tts/bidirection`，二进制事件帧。新连接 StartConnection → ConnectionStarted；每个会话 StartSession → SessionStarted → TaskRequest × N → FinishSession → TTSSentenceStart{text} / 音频帧 / TTSSentenceEnd …… → SessionFinished。级联里一轮回复一个会话，回复开始即开会话，每个 LLM token 一个 TaskRequest；`synthesize()`（直念、cue）是一个会话只发一个 TaskRequest。打断发 CancelSession（SessionStarted 之后随时可发，之后的 push 被丢弃）。`seed-tts-2.0` 不发 TTSSentenceStart，整轮回复算一句，截断只能整句保留或删除。参考：`xiaozhi-esp32-server/.../tts/huoshan_double_stream.py`、`ten-framework/.../bytedance_tts_duplex/`。

### `aliyun_tts`（百炼 CosyVoice 流式合成）

| 键 | 默认 | 说明 |
|---|---|---|
| `api_key` | `$DASHSCOPE_API_KEY` | |
| `model` / `voice` | `cosyvoice-v2` / `longxiaochun_v2` | 音色要和模型代际匹配（v3 用 v3 音色） |
| `sample_rate` | 24000 | 只接受 8000 / 16000 / 22050 / 24000 / 44100 / 48000 |
| `volume`(0–100), `rate` / `pitch`(0.5–2.0) | 不发 | |
| `url` | `wss://dashscope.aliyuncs.com/api-ws/v1/inference` | 国际站 `dashscope-intl`，key 分区域 |

协议：与 `aliyun_stt` 同一套任务协议。`run-task`（`SpeechSynthesizer`，duplex）→ `task-started` → `continue-task`（整句）→ `finish-task` → 二进制音频 → `task-finished`。空闲超过 50 s 的连接不复用（xiaozhi 记录服务端约 1 分钟断开空闲连接）。参考：`xiaozhi-esp32-server/.../tts/alibl_stream.py`、`ten-framework/.../cosy_tts_python/`。

### `openai_tts`

| 键 | 默认 |
|---|---|
| `api_key` | `$OPENAI_API_KEY` |
| `base_url` | `https://api.openai.com/v1` |
| `model` / `voice` | `gpt-4o-mini-tts` / `alloy` |
| `instructions`, `speed`, `chunk_bytes`(9600) | |

输出固定 PCM16 24 kHz（`response_format=pcm`），每句一次请求。

### `moshi`

| 键 | 默认 | 说明 |
|---|---|---|
| `url` | `ws://localhost:8998` | 拼上 `path`（默认 `/api/chat`） |
| `frame_ms` | 20 | Opus 帧长 |
| `connect_timeout_ms` | 10000 | 等服务端握手帧（MT=0） |

安装：`uv sync --extra opus`（`opuslib` 需要系统 libopus）。也可以在构造时注入 `codec=(encoder, decoder)`。上行 / 下行都是 24 kHz PCM16，Ogg/Opus 封装由 `_ogg_opus.py` 做（moshi 自己的客户端用 `sphn`，协议是 Ogg 容器，裸 Opus 包不行）。

## 3. 已知缺口

| provider | 缺口 | 出处 |
|---|---|---|
| `openai_stt` | **非流式**：整段缓冲，松键后上传再转写，首音多 0.5–1 s（推算）。保留作兜底，预设已改用流式 STT | 接口是文件级 `/audio/transcriptions` |
| `volcengine_tts` / `aliyun_tts` | 只有协议级单测，没连过真实服务。待实测：火山 FinishSession 之后能否再 CancelSession、SessionFailed 后连接能否复用、是否必须等 SessionStarted（不等能省一个往返）、会话开着但迟迟没有文本时服务端等多久、逐 token 的 TaskRequest 是否影响韵律和首包、TTSSentenceStart 的 `text` 是否与送入文本一致；阿里最简 `continue-task`（不带 model）是否被接受、空闲连接保活时长；两家的并发配额与首包时间 | 各模块 docstring |
| 五个云流式 STT | 只有协议级单测（假 WebSocket），**没连过真实服务**。各自待实测的点：火山最后一包用负序号是否被接受（不行就改成 xiaozhi 的无序号 `0b0010`，`finish()` 里一行）；阿里 `fun-asr-realtime` 是否同协议；OpenAI 关判停后 commit 到 `.completed` 的耗时、是否有 delta；Deepgram 空尾音时 `Finalize` 是否仍回 `from_finalize`；AssemblyAI 紧跟 `Terminate` 是否影响强制断句的格式化结果。都要对照 `stt_final_ms`（1.5 s）看定稿是否来得及 | 各模块 docstring |
| `cosyvoice` | **没有取消接口**：`flush()` 只是本地停止读流，服务端 `llm_job` 线程会跑完；`supports_text_stream=False`，HTTP 服务不支持文本流入（bistream 只在 Python API，且不能和 vLLM 同用）；官方 server 的 `inference_*` 没开 `stream=True`，首块要等整段合成 | 手册 [`04-projects/tts/cosyvoice.md`](https://github.com/wananing/voice-agent-handbook/blob/master/04-projects/tts/cosyvoice.md) |
| `funasr` | partial 无标点、无热词；一次回合可能被 VAD 切成多段 `2pass-offline`；不收 Opus | 手册 [`04-projects/asr/funasr.md`](https://github.com/wananing/voice-agent-handbook/blob/master/04-projects/asr/funasr.md) |
| `moshi` | **只会英语**；无系统提示、无工具、无历史、无截断；Python 服务端一次只服务一个连接；一场会话约 5–6 分钟（`max_steps=4096`、360 s 超时）；真全双工与按键说话冲突，网关用 `open_mic` 持续送帧（含静音） | `docs/notes/e2e-models.md` Q2；`moshi/FAQ.md` |
| `openai_realtime` | `voice` 首次出声后不能改（`config.update.voice=next_session`）；`response.single_slot`、`history.mid_session_effective`、`config.update_invalidates_cache` 都是 `unknown`，上线前要跑 P0 | `capabilities/openai_realtime.yaml` |
| `dashscope_realtime` | 判停能否关（`turn.server_detection_disable`）`unknown`，决定按键设备能否用 `s2s`；截断是否生效 `unknown` | `capabilities/dashscope_realtime.yaml`；`docs/notes/e2e-models.md` Q4 |

许可（取自 手册 [`05-comparison/model-matrix.md`](https://github.com/wananing/voice-agent-handbook/blob/master/05-comparison/model-matrix.md)，权重许可需单独核对）：

| 项目 | 代码 | 权重 |
|---|---|---|
| FunASR | MIT | ModelScope 另有许可，待确认 |
| CosyVoice | Apache-2.0 | 待确认 |
| moshi | Python MIT、Rust Apache-2.0 | CC-BY 4.0 |
| openai-realtime-agents（参考实现） | MIT | — |
| OpenAI / DashScope 云 API | 各自服务条款 | — |

## 4. 跑探针：`vak probe`

探针 harness 在 `vak/probes/p0.py`，按规范 04 §7.3 的 P0-1 … P0-16 编号；规范 §7.4 的判定规则：行为类字段 20/20 → `yes`、0/20 → `no`、其间 → `flaky(k/20)`，写回时取保守值；阈值类字段取切段率首次超过 50% 的档位。

```bash
uv run vak probe openai_realtime --items P0-1,P0-2 --config my_agent/agent.yaml      # 两个关键项
uv run vak probe dashscope_realtime --items all --n 20 --speech ./probe_speech/     # 全部，带真人语音
```

（`vak probe` 子命令由 `vak/cli.py` 接到 `vak.probes.probe_and_dump`，参数：`--items`、`--n`、`--speech <dir>`、`--out <yaml>`。）

在代码里直接用：

```python
from vak.probes import ProbeAssets, ProbeContext, probe_and_dump
from vak.upstream.providers import capabilities_for

caps = capabilities_for("openai_realtime")

async def factory(**kw):             # kw: turn_detection / history / tools
    return build_s2s_upstream(provider_config | kw, caps)   # 由 pipelines.s2s 提供

ctx = ProbeContext(upstream_factory=factory, assets=ProbeAssets(speech_dir="probe_speech"), caps=caps, n=20)
report = await probe_and_dump(["P0-1", "P0-2", "P0-3", "P0-4", "P0-8"], ctx, "vak/upstream/capabilities/openai_realtime.yaml")
print(report.summary())
```

| 项 | 状态 | 写入字段 | 需要 |
|---|---|---|---|
| P0-1 | 实现 | `tools.auto_reply_after_result`、`tools.silent_close` | 语音样本 `ask_tool.wav`（没有时用音调代替，靠 `tool_choice` 强制调用） |
| P0-2 | 实现 | `turn.server_detection_disable`、`turn.segment_only`、`turn.server_interrupt_optional` | factory 接受 `turn_detection` |
| P0-3 | 实现 | `turn.server_silence_ms` | — |
| P0-4 | 实现 | `turn.commit_min_ms`、`turn.empty_commit_behavior` | — |
| P0-8 | 实现（单槽位部分） | `response.single_slot`；原始错误码进 `raw.error_codes` | — |
| P0-9 | 实现 | `history.text_flips_modality`、`history.mid_session_effective` | `ProbeContext.check_text`、`inject_fact` 回调 + `ask_fact.wav`、`chat.wav` |
| P0-14 | 实现 | `truncate.mode` | `check_text` 回调 + `ask_long.wav`、`ask_last.wav` |
| P0-5/6/7/10/11/12/13/15/16 | 桩 | — | 报告里列为 `not implemented` |

探针音频集（`vak/probes/assets.py`）全部用 numpy 合成：2 s 音调 + 1 s 静音、插入 200/300/400/500/700 ms 静音的音调对、50/100/150 ms 短片段、噪声；真人语音放在 `--speech` 目录下按名字加载（`ask_tool`、`ask_fact`、`ask_long`、`ask_last`、`chat`，16 kHz 单声道 PCM16 wav）。

**前提**：factory 构造的 upstream 要带静态能力声明。探针走的是九个原语，降级表读声明：例如 `send_tool_result(reply_required=false)` 只有在 `tools.auto_reply_after_result` 声明为 `no` 时才会真正发送；被拒的调用记在 `raw` 里，字段写 `unknown`。
