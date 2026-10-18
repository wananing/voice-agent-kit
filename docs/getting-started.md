# 快速开始

目标：30 分钟内用 mock 上游跑通一个完整回合，再换成真实 provider。

## 1. 安装

```bash
cd voice-agent-kit
uv sync --extra dev          # 基础依赖
uv sync --extra opus         # 可选：Opus 编解码（需要系统 libopus）
uv sync --extra openai       # 可选：OpenAI 系 provider
```

## 2. 生成一个 agent

```bash
uv run vak new my_agent --pipeline cascade --profile A --mode ptt
```

生成的目录只有五样东西：`agent.yaml`、`persona.yaml`、`state_schema.yaml`、`tools.py`、`cues/`。默认 provider 是 mock，不需要任何 API key。

## 3. 跑起来

```bash
uv run vak run my_agent/agent.yaml
```

浏览器打开 http://localhost:8765/ ，按住按钮说话，松开。mock STT 会固定识别成一句话，mock LLM 按脚本回复（含一次工具调用），mock TTS 合成提示音。你应该看到：转写回显、`tts.sentence_start` 逐句上屏、`turn.closed{completed}`。

按住按钮打断正在播放的回复：客户端立即停播，网关依次发 `flush.start`、`flush.end`、`tts.stop{interrupted}`、`turn.closed{interrupted}`。

## 4. 看 trace

```bash
uv run vak trace summarize my_agent/traces/*.jsonl
```

输出按 `turn_kind × 锚点 × 时钟质量` 分组的有声音 / 有内容 P50 / P90。样本少于 20 条会标"样本不足"。

## 5. 换真实 provider

编辑 `agent.yaml` 的 `providers:`，或重新生成：

```bash
uv run vak new my_agent2 --pipeline cascade --providers cloud        # 云 STT/TTS + OpenAI 兼容 LLM
uv run vak new my_agent2v --pipeline cascade --providers volcengine  # 火山识别 + 方舟 + 火山合成（一套火山 key）
uv run vak new my_agent2a --pipeline cascade --providers aliyun      # 百炼识别 + 百炼 LLM + CosyVoice（一个 DashScope key）
uv run vak new my_agent3 --pipeline cascade --providers selfhost_zh  # FunASR + CosyVoice + 自托管 LLM
uv run vak new my_agent4 --pipeline s2s     --providers openai       # OpenAI Realtime
uv run vak new my_agent5 --pipeline s2s     --providers dashscope    # Qwen-Omni Realtime
uv run vak new my_agent6 --pipeline half_cascade --providers cloud   # Realtime 文本模态 + 本地 TTS
uv run vak new my_agent7 --pipeline full_duplex --providers moshi    # moshi
```

每个 provider 需要的环境变量和依赖见 `docs/providers.md`。不确定选哪家时，先用 `vak bench` 在自己的网络下测一遍（`docs/bench.md`）。

## 6. 接 S2S 之前：先跑探针

S2S 上游的两个能力决定整条路线成不成立：服务端判停能不能关（P0-2）、回传工具结果后能不能不续答（P0-1）。

```bash
uv run vak probe openai_realtime --items P0-1,P0-2 --config my_agent4/agent.yaml
```

结果写回 `vak/upstream/capabilities/openai_realtime.yaml`，运行时按它分支。没探过的字段按保守值运行。

## 7. 写工具

`tools.py` 里每个带 `@tool` 的 async 函数就是一个工具。装饰器参数就是规范 03 的注册表字段。启动时校验：写操作没有 `gates` 会直接拒绝启动。路线（直念 / 委派 / 进度收口 / 回传再生成）由 `presentation × latency_class × write` 静态算出，trace 里 `tool_calls[].tool_route` 能看到。

## 8. 接你自己的设备

读 `docs/protocol.md`。`clients/web/protocol.js` 的帧编解码可以直接移植到固件。
