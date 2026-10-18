# open_mic_s2s

由 `vak new` 生成。目录：

- `agent.yaml` 链路、provider、profile、模式
- `persona.yaml` 人设
- `state_schema.yaml` 状态块
- `tools.py` 工具函数
- `cues/` 预合成提示语

运行：

```bash
vak run agent.yaml
```

然后用浏览器打开 http://localhost:8765/ 用参考客户端对话，或按 `docs/protocol.md` 接你自己的设备。
