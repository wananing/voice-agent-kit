# 贡献指南

动手之前，先读 `docs/architecture.md`，再读你要改的模块对应的规范（`docs/specs/0X-*.md`）。规范是契约，代码按规范写；规范没写的约定见 architecture §11。

开发环境：

```bash
uv sync --extra dev
uv run pytest -q                      # 全量测试，不需要网络和任何 key
uv run ruff check vak tests           # 零告警
node --test clients/web/              # 浏览器客户端的测试
```

## 1. 契约文件

这些文件定义了模块之间的接口，字段名逐字来自规范。改动它们要走 §6 的流程，不要在实现某个模块时顺手改掉：

| 文件 | 内容 |
|---|---|
| `vak/core/types.py` | 全部枚举和数据类，字段名来自规范 00 术语表 |
| `vak/core/ports.py` | 模块之间的接口（Protocol）。每个模块实现自己的端口，调用别人的端口 |
| `vak/core/bus.py` | 进程内事件总线，事件名用规范里的名字 |
| `vak/core/clock.py` | `Clock` 协议、`MonotonicClock`、`FakeClock`。**所有定时器都通过注入的 clock**，不直接 `asyncio.sleep` |
| `vak/core/defaults.py` | 全部可调参数，带规范出处。模块构造函数接收对应的 `*Params` 对象 |
| `vak/core/ids.py` | `IdGen.new("req")` 生成会话内唯一 id |
| `vak/upstream/base.py` | `Upstream` 的九个原语、`Ack`、`UpstreamEvent`、`UpstreamError` |
| `vak/upstream/capabilities.py` | 能力声明；`caps.get(name)` 已把 `unknown` 换成保守值 |
| `vak/upstream/providers/base.py` | STT / LLM / TTS（含 `TTSTextStream`）/ AudioLLM / RealtimeS2S / FullDuplexModel 接口 |

如果发现契约有缺口或错误，请开 issue，或者在 PR 描述里单独写一节"需要的契约改动"，写明文件、字段和原因。

## 2. 模块地图

一个 PR 尽量只动一个模块和它的测试目录：

| 模块 | 代码 | 测试 |
|---|---|---|
| 回合状态机（规范 01） | `vak/core/turn/` | `tests/unit/turn/` |
| 传输、音频、cue（规范 01 §2.5–2.9、§4） | `vak/transport/`、`vak/audio/`、`vak/cue/` | `tests/unit/transport/`、`tests/unit/audio/` |
| 输出仲裁（规范 02） | `vak/core/arbiter/` | `tests/unit/arbiter/` |
| 工具层（规范 03） | `vak/core/tools/` | `tests/unit/tools/` |
| 上下文与会话（规范 05） | `vak/core/context/` | `tests/unit/context/` |
| trace 与评测（规范 06） | `vak/core/trace/`、`vak/eval/`、`schemas/trace.schema.json` | `tests/unit/trace/`、`tests/unit/eval/` |
| 链路与 mock（规范 04） | `vak/upstream/pipelines/`、`vak/upstream/mirror.py`、`vak/upstream/providers/mock.py`、`vak/upstream/capabilities/mock*.yaml` | `tests/unit/upstream/`、`tests/conformance/` |
| 真实 provider | `vak/upstream/providers/`（mock 除外）、`vak/upstream/capabilities/*.yaml`、`vak/probes/`、`docs/providers.md` | `tests/unit/providers/` |
| 集成（session、gateway、agent、cli、模板、示例、客户端） | `vak/core/session.py`、`vak/core/gateway.py`、`vak/core/proxy.py`、`vak/agent.py`、`vak/config.py`、`vak/cli.py`、`templates/`、`examples/`、`clients/` | `tests/integration/`（`harness.py` 提供 loopback 客户端） |

## 3. 代码约定

1. **显式状态机。** 状态用枚举，转移用表（`dict[(state, event)] -> state`），非法转移抛 `IllegalTransition`。不要用一组布尔标志拼出状态。
2. **定时器用 clock。** 写成 `await self.clock.sleep(ms)`；看门狗是一个可取消的任务，覆盖每个非终态。测试里用 `FakeClock.advance()` 推进时间。
3. **代际比较三处：** 入队、授权、每发一帧。凡是发音频的函数，签名里都带 `generation`。
4. **每个回调入口的第一行先查 `settled` 和代际**，再做别的。
5. **禁词。** `vak/core/` 和 `vak/transport/` 下不能出现厂商或模型名（openai、dashscope、qwen、moshi、funasr、cosyvoice、gemini、doubao …），链路差异一律用能力声明表达。`vak/upstream/` 下不写重试循环、重连和定时回收。
6. **trace 只写自己的字段。** 通过 `TracePort.write(turn_id, {...})` 写，key 必须是规范 06 §2.5 里的名字，并在注释里标出规范节号。
7. **建议值不硬编码**，一律从 `defaults.py` 的参数对象里取。
8. **不做规范以外的事。** 没要求的配置项、抽象、"以后可能用到"的钩子都不加。
9. **风格：** Python 3.11+，`from __future__ import annotations`，dataclass + slots，类型标注齐全，`ruff check` 零告警。日志用 `logging.getLogger(__name__)`，不用 print。
10. **文档字符串用英文**，保持简短；注释里引用规范节号，比如 `# spec 02 §2.8 row 1`。出处简写的含义见 `docs/specs/README.md`。

## 4. 测试约定

- 用 `pytest-asyncio` 的自动模式，直接写 `async def test_...`。
- 模块测试只依赖契约文件和本模块；别的模块的端口用最小 fake 代替，放在 `tests/unit/<module>/fakes.py`。
- 规范"验收清单"里凡是不依赖真实上游和真实设备的项，每项至少一个测试，测试名里带上清单编号或关键词。
- 有竞态的项（打断、迟到帧、回执丢失）用 `FakeClock` 写成确定性测试，另外加一个 `repeat` 参数化（≥ 20 次）的随机时序测试。
- provider 测试不连网络：WebSocket 用 `tests/unit/providers/fakes.py` 里的 `FakeWS`，HTTP 用 `streaming_transport`。

## 5. 加一个 provider

1. 在 `vak/upstream/providers/<kind>/` 下实现 `providers/base.py` 里对应的接口。所有音频都是单声道 PCM16，采样率要声明，不要假设。可选实现 `warmup()`，级联会在回合开始时调用它预连。
2. 在 `vak/upstream/providers/__init__.py` 的 `PROVIDERS` 里注册。可选依赖放在 `lazy_import` 里，缺失时报清楚的错误。
3. S2S 和全双工 provider 还要在 `vak/upstream/capabilities/` 下写能力声明，每个字段标 `evidence`（`code_read` / `doc` / `probed` / `unknown`），没把握的写 `unknown`，运行时会按保守值处理。
4. 协议级测试放在 `tests/unit/providers/`，构造测试放在 `test_capabilities_and_registry.py`。
5. 在 `docs/providers.md` 里写配置项、环境变量、协议要点、参考实现，并在已知问题表里列出待实测项。
6. 如果是级联用的 STT / LLM / TTS，可以顺手在 `examples/bench/bench.yaml` 里加一条候选。

## 6. 改契约的流程

契约改动要按这个顺序同步：规范条文（`docs/specs/`）→ 契约文件 → 所有实现它的模块 → 各模块的 fake（`tests/unit/*/fakes.py`）→ `docs/architecture.md` §4 / §11。已经按这个流程改过的例子有：`send_text`、`TracePort.mark_complete` / `on_usage`、`TransportPort.sample_rate`、`SessionInit.turn_detection`、`RealtimeS2S.add_user_text`、`TTSTextStream`，可以照着这几处改。

## 7. PR 描述里写什么

1. 实现或修改了规范的哪些节。
2. 验收清单里哪些项有测试，哪些项做不到，以及原因。
3. 需要的契约改动（如果有）。
4. 对其他模块的假设，例如"假设 session 在 `response.done` 之后调用 `arbiter.on_reply_done`"。
5. 涉及真实上游时，说明是否实测过；实测过的话附上数字和测试条件。
