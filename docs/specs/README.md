# 规范 00–06

voice-agent-kit 的契约。`vak/core/types.py`、`vak/core/ports.py`、`vak/upstream/base.py` 等契约文件就是按这七份规范写的，代码注释里的"spec 02 §2.8"指的就是这里的章节。规范写于实现之前，正文保留了设计期原貌，因此有两点需要先说明：

- **"A / B 两个团队""两个 profile"**：设计时假设有两类设备，A 类是无界面硬件，B 类是带屏移动端，分别由两个团队实现。规范的要求是两边的语义只能有一份。到了 vak 里，这体现为 `profile: A | B` 配置，不同 profile 的默认参数不同。
- **"我们""实测"**：指作者在设计期做的调研和测量。标【建议值，需实测】的数字还没有在真实上游上校准过。

| 编号 | 文件 | 内容 | 主要实现位置 |
|---|---|---|---|
| 00 | [00-glossary.md](00-glossary.md) | 术语与枚举总表，名字和取值以它为准 | `vak/core/types.py` |
| 01 | [01-turn-model-and-client-protocol.md](01-turn-model-and-client-protocol.md) | 回合模型、设备协议、判停、打断、下行节流 | `vak/core/turn/`、`vak/transport/`、`docs/protocol.md` |
| 02 | [02-output-arbiter.md](02-output-arbiter.md) | 输出仲裁：谁能出声、代际、冲刷、回执 | `vak/core/arbiter/` |
| 03 | [03-tool-registry-and-call-closure.md](03-tool-registry-and-call-closure.md) | 工具注册表、调用收口的六个终态 | `vak/core/tools/` |
| 04 | [04-upstream-adapter-capabilities.md](04-upstream-adapter-capabilities.md) | 九个上游原语、能力声明、四种链路的映射 | `vak/upstream/` |
| 05 | [05-context-and-session.md](05-context-and-session.md) | 上下文账本、状态块、会话重建 | `vak/core/context/` |
| 06 | [06-trace-and-evaluation.md](06-trace-and-evaluation.md) | trace 字段权威表、L0 / L1 评测 | `vak/core/trace/`、`vak/eval/`、`schemas/trace.schema.json` |

修改规范的流程见 [contributing §6](../contributing.md#6-改契约的流程)。

## 出处简写对照

规范和代码注释在结论后面用括号标出处。其中的简写最初指向作者的调研笔记，这些笔记没有公开，内容已经整理进 [voice-agent-handbook](https://github.com/wananing/voice-agent-handbook)（下称"手册"）。查找时用下表：

| 简写 | 原指 | 手册内对应 |
|---|---|---|
| 架构、两类架构 | 两类架构的上位文档 | [cascade](https://github.com/wananing/voice-agent-handbook/blob/master/02-architectures/cascade.md)、[half-cascade](https://github.com/wananing/voice-agent-handbook/blob/master/02-architectures/half-cascade.md)、[s2s](https://github.com/wananing/voice-agent-handbook/blob/master/02-architectures/s2s.md)、[floor-control](https://github.com/wananing/voice-agent-handbook/blob/master/02-architectures/floor-control.md) |
| 总结 | 一份实战设计笔记 | [latency-budget](https://github.com/wananing/voice-agent-handbook/blob/master/01-foundations/latency-budget.md)，以及各机制页里标"实测"的数字 |
| KM | 调研知识地图 | [03-mechanisms](https://github.com/wananing/voice-agent-handbook/tree/master/03-mechanisms) 各页 |
| BP | 瓶颈方案 | [tool-calls](https://github.com/wananing/voice-agent-handbook/blob/master/03-mechanisms/tool-calls.md)、[session-recovery](https://github.com/wananing/voice-agent-handbook/blob/master/03-mechanisms/session-recovery.md)、[turn-detection](https://github.com/wananing/voice-agent-handbook/blob/master/03-mechanisms/turn-detection.md) |
| HC、`notes/half-cascade-open.md` | 半级联调研笔记 | [half-cascade](https://github.com/wananing/voice-agent-handbook/blob/master/02-architectures/half-cascade.md) |
| E2E、`notes/e2e-models.md` | 端到端模型笔记 | [04-projects/e2e-models](https://github.com/wananing/voice-agent-handbook/tree/master/04-projects/e2e-models) |
| LK、LK-B、`notes/livekit-agents*.md` | livekit-agents 笔记 | [livekit-agents](https://github.com/wananing/voice-agent-handbook/blob/master/04-projects/frameworks/livekit-agents.md) |
| PC、PC-B、`notes/pipecat*.md` | pipecat 笔记 | [pipecat](https://github.com/wananing/voice-agent-handbook/blob/master/04-projects/frameworks/pipecat.md) |
| QW、`notes/qwen-audio-agent.md` | qwen-audio-agent 笔记 | [qwen-audio-agent](https://github.com/wananing/voice-agent-handbook/blob/master/04-projects/frameworks/qwen-audio-agent.md) |
| ORA、`notes/openai-realtime-agents.md` | openai-realtime-agents 笔记 | [openai-realtime-agents](https://github.com/wananing/voice-agent-handbook/blob/master/04-projects/frameworks/openai-realtime-agents.md) |
| FE、`notes/flows-and-evals.md` | flows 与评测笔记 | [pipecat-flows](https://github.com/wananing/voice-agent-handbook/blob/master/04-projects/frameworks/pipecat-flows.md)、[evaluation](https://github.com/wananing/voice-agent-handbook/blob/master/03-mechanisms/evaluation.md) |
| UM、`notes/unmute.md` | unmute 笔记 | [unmute](https://github.com/wananing/voice-agent-handbook/blob/master/04-projects/full-duplex/unmute.md) |
| `notes/ten-and-deployment.md` | TEN 与部署笔记 | [ten-framework](https://github.com/wananing/voice-agent-handbook/blob/master/04-projects/frameworks/ten-framework.md) |
| TTS、TTS-alt、`notes/tts-zh.md` | 中文 TTS 笔记 | [04-projects/tts](https://github.com/wananing/voice-agent-handbook/tree/master/04-projects/tts) |
| `notes/asr-zh.md` | 中文 ASR 笔记 | [04-projects/asr](https://github.com/wananing/voice-agent-handbook/tree/master/04-projects/asr) |
| `notes/xiaozhi*.md` | 小智项目笔记 | [xiaozhi-esp32-server](https://github.com/wananing/voice-agent-handbook/blob/master/04-projects/devices/xiaozhi-esp32-server.md)、[xiaozhi-esp32](https://github.com/wananing/voice-agent-handbook/blob/master/04-projects/devices/xiaozhi-esp32.md) |
| `notes/review-*.md`、`review-specs.md` | 调研笔记和规范的审校记录 | 无对应页，结论已改进正文 |
| 其他 `notes/x.md` | 其他项目笔记 | [04-projects](https://github.com/wananing/voice-agent-handbook/tree/master/04-projects) 下的对应项目页 |
| `handbook <主题>`（代码注释） | 手册页面 | 按主题名找：`first-audio`、`turn-detection`、`tool-calls`、`audio-preprocessing` 在 [03-mechanisms](https://github.com/wananing/voice-agent-handbook/tree/master/03-mechanisms)；`full-duplex` 在 [02-architectures](https://github.com/wananing/voice-agent-handbook/tree/master/02-architectures)；`turn-protocol` 是规范 01 的蒸馏版，在 [skills/voice-agent-handbook/references/specs](https://github.com/wananing/voice-agent-handbook/tree/master/skills/voice-agent-handbook/references/specs) |
