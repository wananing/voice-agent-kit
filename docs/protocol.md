# 设备协议 va/1

设备或客户端与网关之间的协议以规范 01（[`docs/specs/01-turn-model-and-client-protocol.md`](specs/01-turn-model-and-client-protocol.md)）为准。本文只记脚手架实现与规范的差异，以及接一个新客户端时的最短路径。

## 与规范 01 的差异

| 项 | 规范 | 脚手架 |
|---|---|---|
| 音频编码 | Opus | `hello.caps.audio_up.codec` 和 `audio_down.codecs` 可写 `opus` 或 `pcm16`；网关取客户端列表里第一个它支持的。`pcm16` 用于没有 libopus 的开发环境，帧头布局不变 |
| 帧头字节序 | 未规定 | 大端（网络序） |
| WebRTC | §7 预留 | v1 不实现 |
| `cmd` | 可选 | 实现，但参考客户端不声明任何 `commands` |

## 接一个新客户端的最短路径

1. 发 `hello`，声明 `caps`（最少：`aec`、`modes`、`receipts`、两个 audio 对象）。没有回执能力也能跑，但截断按估计、trace 标 `receipt_mode=estimated`。
2. 按键模式：key-down 发 `turn.start{source:"button"}`，之后每 20 ms 一帧音频（16 字节头 + 帧），key-up 发 `turn.end{reason:"release"}`。
3. 下行只在收到 `tts.start` 后、`turn_id` 等于当前回合时把音频入播放队列（第一道闸）；本地 `play_gen` 在 abort 时 +1，解码队列里旧代际的帧出队时丢弃（第二道闸）。
4. 每句播放开始发 `playback.started`，结束发 `playback.ended`，`played_ms` 按 DAC 实际消费的采样数算。
5. 播放中按键：本地立即停播、`play_gen += 1`，同一 tick 依次发 `playback.interrupted`、`abort`、`turn.start`。
6. 收到 `turn.closed` 后该回合结束；无屏设备在 `failed` 时必须用本地预合成话术发声。
7. 每 5 s 发 `ping`；断线后 30 s 内带 `resume` 重连。

参考实现：`clients/web/`（浏览器，JavaScript，无依赖），`protocol.js` 里的帧编解码可以直接移植。

## 验收

规范 01 §8 的清单里，脚手架用 `tests/unit/turn`、`tests/unit/transport` 和 `tests/integration` 覆盖不依赖真实设备的项；依赖真实设备的项（key-down 到扬声器降到底噪 ≤ 60 ms、`played_ms` 与录音实测误差 ≤ 40 ms）要在目标硬件上测。
