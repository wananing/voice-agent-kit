# 示例

每个目录都是 `vak new` 生成的完整 agent 项目（`agent.yaml`、`persona.yaml`、`state_schema.yaml`、`tools.py`、`cues/`），只改了链路、profile、模式和 provider 预设。跑法都一样：

```bash
uv run vak run examples/<name>/agent.yaml
# 浏览器打开 http://localhost:8765/
```

| 目录 | 链路 | profile / 模式 | provider | 需要 |
|---|---|---|---|---|
| `playground` | 级联 | A · 按键 | 离线演示配置，不连接外部服务 | 无。建议先运行此示例 |
| `ptt_cascade_zh` | 级联 | A · 按键 | FunASR + OpenAI 兼容 LLM + CosyVoice | `FUNASR_URL`、`LLM_BASE_URL`、`LLM_MODEL`、`COSYVOICE_URL` |
| `cloud_cascade` | 级联 | B · 开放麦 | OpenAI Realtime 流式转写 + OpenAI TTS + OpenAI 兼容 LLM | `OPENAI_API_KEY` |
| `half_cascade` | 半级联 | B · 开放麦 | OpenAI Realtime 文本模态 + OpenAI TTS + 侧路 STT | `OPENAI_API_KEY` |
| `open_mic_s2s` | 端到端 S2S | B · 开放麦 | OpenAI Realtime；换 DashScope 只改 `providers.s2s` | `OPENAI_API_KEY`（或 `DASHSCOPE_API_KEY`） |
| `full_duplex_moshi` | 原生全双工 | B · 开放麦 | moshi 服务 | `MOSHI_URL`，`uv sync --extra opus` |

开放麦示例依赖客户端 AEC：浏览器参考客户端声明 `aec: os`，三项约束全开。没有 AEC 的客户端会被网关降级为按键模式。

接 S2S 之前先跑探针：

```bash
uv run vak probe config --config examples/open_mic_s2s/agent.yaml --items P0-1,P0-2
```

环境变量、provider 参数和已知坑见 `docs/providers.md`。
