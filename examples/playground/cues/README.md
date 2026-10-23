# 预合成提示语

放 16 kHz 单声道 16-bit wav。文件名即 cue id：

| 文件 | 用途 | 长度 |
|---|---|---|
| thinking_short.wav | 工具回合短提示（A1） | ≤ 300 ms |
| thinking_long.wav | 需要组织语言 / 秒级工具（A2 / A3） | 约 0.7–0.8 s |
| error_generic.wav | 回合失败话术（无屏设备必须发声） | 1–2 s |
| error_no_speech.wav | 没听清 | 1–2 s |
| reconnecting.wav | 会话重建期间的填充 | 1–2 s |

没有文件时运行时用合成提示音代替，能跑但不好听。用你的 TTS 同一音色预合成，裁掉首部静音。
