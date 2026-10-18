"""One connection session: wires the five core modules, the transport and
the upstream for one client (architecture §4.4).

The session owns no policy. It builds the objects, routes upstream events
to the modules that own them, and tears everything down. Reconnects swap
the transport under a proxy; rebuilds swap the upstream under a proxy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from vak.core.arbiter import Arbiter
from vak.core.bus import Bus
from vak.core.clock import Clock
from vak.core.context import (
    ContextStore,
    ModalityMonitor,
    SessionRebuilder,
    SnapshotStore,
    StateBlockStore,
    Summarizer,
)
from vak.core.defaults import Defaults
from vak.core.ids import IdGen
from vak.core.ports import CuePort
from vak.core.proxy import TransportProxy, UpstreamProxy
from vak.core.tools import Registry, TextLLMDelegate, ToolLayer
from vak.core.trace import TraceCollector
from vak.core.trace.exporter import Exporter
from vak.core.turn import ClientCaps, ResumeInfo, TurnMachine
from vak.core.types import Mode, Persona, Pipeline, Profile, RebuildReason, TurnId
from vak.transport.base import BaseTransport, ResumeState
from vak.upstream.base import EventType, SessionInit, Upstream, UpstreamEvent
from vak.upstream.capabilities import Capabilities
from vak.upstream.providers.base import LLM, TTS

log = logging.getLogger(__name__)

GATEWAY_BUILD = "vak-0.1.0"


@dataclass(slots=True)
class SessionParts:
    """Everything an agent provides to every session (shared, read-only)."""

    profile: Profile
    mode: Mode
    pipeline: Pipeline
    defaults: Defaults
    registry: Registry
    persona: Persona
    rules: str
    state_store_factory: Callable[[], StateBlockStore]
    cues: CuePort | None
    caps: Capabilities
    upstream_factory: Callable[[Callable[[], bool]], Upstream]  # takes is_committed()
    local_tts_factory: Callable[[], TTS | None]
    delegate_llm_factory: Callable[[], LLM | None]
    endpointer_factory: Callable[[int], Any]  # sample_rate -> Endpointer | None
    exporter_factory: Callable[[str], Exporter]  # session_id -> exporter
    provider_name: str
    model_name: str
    snapshot_dir: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Session:
    def __init__(
        self,
        *,
        parts: SessionParts,
        clock: Clock,
        session_id: str,
        user_session_id: str | None = None,
    ) -> None:
        self.parts = parts
        self.clock = clock
        self.session_id = session_id
        self.user_session_id = user_session_id or session_id
        self.ids = IdGen()
        self.bus = Bus()
        self.transport = TransportProxy(None, on_audio_sent=self._on_audio_sent)
        self.upstream = UpstreamProxy()
        self.built = False
        self.closed = False
        self.disconnected_at_ms: int | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._resume_turns: list[tuple[TurnId, str]] = []
        self._turns_in_upstream_session = 0
        self._consumer_dropped_seen = 0
        # filled by build()
        self.trace: TraceCollector
        self.context: ContextStore
        self.arbiter: Arbiter
        self.tools: ToolLayer
        self.machine: TurnMachine
        self.rebuilder: SessionRebuilder
        self.modality: ModalityMonitor
        self.snapshots: SnapshotStore | None = None

    # ------------------------------------------------------------------ build

    async def attach(self, transport: BaseTransport) -> None:
        """Handshake done on `transport`; build on first attach, resume on later ones."""
        self.transport.set_target(transport)
        if not self.built:
            await self._build(transport)
            return
        info = ResumeInfo(ok=True, disconnected_at_ms=self.disconnected_at_ms or self.clock.now_ms())
        self.disconnected_at_ms = None
        self._resume_turns = []
        await self.machine.on_resume(info)

    async def _build(self, transport: BaseTransport) -> None:
        p = self.parts
        d = p.defaults
        hello = transport.hello
        assert hello is not None, "attach() needs a transport that completed its handshake"
        caps_dict = _caps_to_dict(hello.caps)
        client_caps = ClientCaps.from_hello(caps_dict)
        mode = transport.mode  # negotiated (open_mic needs aec != none)

        self.trace = TraceCollector(
            session_id=self.session_id,
            user_session_id=self.user_session_id,
            profile=p.profile,
            pipeline=p.pipeline.value,
            provider=p.provider_name,
            model=p.model_name,
            prompt_version_fn=lambda: self.context.prompt_version(p.pipeline.value),
            gateway_build=GATEWAY_BUILD,
            clock=self.clock,
            params=d.trace,
            exporter=p.exporter_factory(self.session_id),
            bus=self.bus,
        )
        self.context = ContextStore(
            user_session_id=self.user_session_id,
            profile=p.profile,
            params=d.context,
            clock=self.clock,
            ids=self.ids,
            persona=p.persona,
            state=p.state_store_factory(),
            trace=self.trace,
            tools_version=p.registry.registry_version,
            rules=p.rules,
        )
        local_tts = p.local_tts_factory()
        self.arbiter = Arbiter(
            profile=p.profile,
            params=d.arbiter,
            clock=self.clock,
            bus=self.bus,
            transport=self.transport,
            context=self.context,
            trace=self.trace,
            ids=self.ids,
            upstream=self.upstream,
            tts=local_tts,
            cues=p.cues,
            client_receipts=set(client_caps.receipts),
        )
        delegate_llm = p.delegate_llm_factory()
        delegate = TextLLMDelegate(delegate_llm, p.registry, ids=self.ids) if delegate_llm is not None else None
        self.tools = ToolLayer(
            registry=p.registry,
            profile=p.profile,
            params=d.tools,
            clock=self.clock,
            bus=self.bus,
            arbiter=self.arbiter,
            upstream=self.upstream,
            context=self.context,
            trace=self.trace,
            ids=self.ids,
            cues=p.cues,
            caps=p.caps,
            delegate=delegate,
            phase_provider=self._phase,
        )
        endpointer = p.endpointer_factory(client_caps.uplink_rate) if mode is Mode.OPEN_MIC else None
        self.machine = TurnMachine(
            profile=p.profile,
            mode=mode,
            timers=d.turn,
            endpointing=d.endpointing,
            clock=self.clock,
            bus=self.bus,
            transport=self.transport,
            arbiter=self.arbiter,
            upstream=self.upstream,
            context=self.context,
            tools=self.tools,
            trace=self.trace,
            ids=self.ids,
            endpointer=endpointer,
            client_caps=client_caps,
            cues=p.cues,
        )
        # upstream: first real instance now, later ones via the rebuilder
        real = p.upstream_factory(self._is_committed)
        self.upstream.set_target(real)
        summarizer = Summarizer(delegate_llm, d.context, self.clock) if delegate_llm is not None else None
        self.rebuilder = SessionRebuilder(
            context=self.context,
            upstream_factory=lambda: p.upstream_factory(self._is_committed),
            upstream=real,
            caps=p.caps,
            clock=self.clock,
            bus=self.bus,
            params=d.context,
            trace=self.trace,
            tools=self.tools,
            registry_tools=(p.registry.tool_table if p.caps.get("tools.calling") != "none" else list),
            summarizer=summarizer,
        )
        self.rebuilder.start()
        self.modality = ModalityMonitor(d.context, self.rebuilder.rebuild, self.bus)
        self.modality.start()
        if p.snapshot_dir is not None:
            self.snapshots = SnapshotStore(d.context, self.clock, directory=p.snapshot_dir)
            self.snapshots.bind(self.bus, self.context)

        self.bus.on("session.rebuilt", self._on_rebuilt)
        self.bus.on("user.barge_in", self._on_barge_in)
        self.bus.on("turn.start", self._on_turn_start)
        self.bus.on("turn.closed", self._on_turn_closed)

        init = SessionInit(
            user_session_id=self.user_session_id,
            session_seq=self.context.session_seq,
            instructions=self.context.render_instructions(),
            state_block_version=self.context.state.version,
            tools=p.registry.tool_table() if p.caps.get("tools.calling") != "none" else [],
            history=[],
            history_format=self.context.history_format(p.caps),
            turn_detection=None if mode is Mode.PTT else "default",
            voice=p.extra.get("voice"),
            language=p.extra.get("language"),
        )
        ack = await real.open(init)
        if not ack.ok:
            log.error("session %s: upstream open failed: %s", self.session_id, ack)
        self._spawn(self._pump_upstream())
        self.built = True

    # ------------------------------------------------------------------ run

    async def run(self, transport: BaseTransport) -> None:
        """Drive one connection until it closes. Call `attach()` first."""
        try:
            await transport.run(self.machine.handle_message, self._on_audio)
        finally:
            self.disconnected_at_ms = self.clock.now_ms()
            self._resume_turns = [
                (t.turn_id, t.state.value) for t in self.machine._turns.values() if not t.terminal  # noqa: SLF001
            ]

    def resume_state(self) -> ResumeState | None:
        """What a reconnecting client resumes against (spec 01 §6.2)."""
        t = self.transport.target
        if t is None:
            return None
        state = t.resume_state()
        state.turns = list(self._resume_turns)
        return state

    async def close(self, reason: str = "close") -> None:
        if self.closed:
            return
        self.closed = True
        if self.built:
            try:
                await self.machine.close_session(reason)
            except Exception:  # noqa: BLE001
                log.exception("close_session failed")
            self.rebuilder.stop()
            self.modality.stop()
            if self.snapshots is not None:
                self.snapshots.unbind()
            try:
                await self.upstream.target.close(reason)
            except Exception:  # noqa: BLE001
                log.debug("upstream close failed", exc_info=True)
            await self.upstream.shutdown()
            await self.arbiter.close()
            await self.trace.close()
        for task in list(self._tasks):
            task.cancel()
        t = self.transport.target
        if t is not None and not t.closed:
            await t.close(reason)

    # ------------------------------------------------------------------ routing

    async def _on_audio(self, seq: int, ts_samples: int, turn_id_u32: int, flags: int, pcm16: bytes) -> None:
        if self.built and self.rebuilder.handoff_active and self.rebuilder.buffer_uplink(pcm16):
            return  # spec 05 §4.7: buffered, replayed after the new upstream opens
        await self.machine.handle_audio(turn_id_u32, seq, ts_samples, flags, pcm16)

    async def _pump_upstream(self) -> None:
        async for ev in self.upstream.events():
            try:
                await self._dispatch(ev)
            except Exception:  # noqa: BLE001
                log.exception("dispatching upstream event %s failed", ev.type.value)

    async def _dispatch(self, ev: UpstreamEvent) -> None:
        t = ev.type
        if t is EventType.TOOL_CALL:
            await self.tools.on_tool_call(ev)
        if t is EventType.TRANSCRIPT_USER_FINAL and ev.turn_id is not None and ev.text:
            self.tools.on_user_final(ev.turn_id, ev.text)
        if t is EventType.RESPONSE_DONE and ev.reply_modality is not None:
            self.modality.on_response_done(ev.reply_modality)
        if t is EventType.USAGE and ev.usage is not None:
            tid = ev.turn_id or self._turn_for_response(ev.response_id)
            if tid is not None:
                turn = self.machine.turn(tid)
                interrupted = bool(turn is not None and turn.interrupt_source is not None)
                self.trace.on_usage(tid, ev.usage, interrupted)
        if t is EventType.ERROR and ev.error is not None:
            self._spawn(self.rebuilder.on_upstream_error(ev.error))
        if t is EventType.SESSION_CLOSED and not self.closed and ev.reason not in ("close", "rebuild"):
            if not (ev.reason or "").startswith("rebuild"):
                self._spawn(self.rebuilder.rebuild(RebuildReason.DISCONNECT))
        await self.machine.on_upstream_event(ev)

    def _turn_for_response(self, response_id: str | None) -> TurnId | None:
        if response_id is None:
            return None
        rid = self.arbiter.request_for_response(response_id)
        rec = self.arbiter.record(rid) if rid else None
        if rec is not None and rec.req.turn_id is not None:
            return rec.req.turn_id
        cur = self.machine.current_turn
        return cur.turn_id if cur is not None else None

    async def _on_rebuilt(self, payload: dict[str, Any]) -> None:
        new = self.rebuilder.upstream
        if new is not None and new is not self.upstream.target:
            self.upstream.set_target(new)

    async def _on_barge_in(self, payload: dict[str, Any]) -> None:
        await self.tools.on_barge_in(int(payload.get("generation", self.arbiter.generation)))
        self.rebuilder.set_user_speaking(True)

    async def _on_turn_start(self, payload: dict[str, Any]) -> None:
        self.rebuilder.set_user_speaking(True)
        self.context.state.apply_pending()  # checkpoint: before the next turn (spec 05 §2.3)
        self._turns_in_upstream_session += 1
        tid = payload.get("turn_id")
        if tid is not None:
            # spec 06 §3.3 turn_kind: the tool layer overwrites it for tool turns
            src = str(payload.get("source") or "")
            kind = {"text": "ui_input", "event": "event"}.get(src, "chat")
            self.trace.write(tid, {**self._baseline_fragment(), "turn_kind": kind})

    async def _on_turn_closed(self, payload: dict[str, Any]) -> None:
        self.rebuilder.set_user_speaking(False)
        self.context.state.apply_pending()  # checkpoint: turn closed
        tid = payload.get("turn_id")
        if tid is None:
            return
        t = self.transport.target
        frag: dict[str, Any] = {
            "user_text": self.context.last_user_text(TurnId.parse(str(tid))) or "",
            "state_block_version": self.context.state.version,
            "consumer_dropped_frames": (t.consumer_dropped_frames if t is not None else 0) - self._consumer_dropped_seen,
        }
        if t is not None:
            self._consumer_dropped_seen = t.consumer_dropped_frames
            frag["uplink_seq_gaps"] = t.uplink_gaps
        turn = self.machine.turn(tid)
        if turn is not None:
            frag["usage"] = {"played_audio_ms": turn.heard_ms}
        self.trace.write(tid, frag)

    def _baseline_fragment(self) -> dict[str, Any]:
        """Required trace fields whose value is known at turn start (spec 06 §2.5).
        Components overwrite what they measure; counters start at zero."""
        p = self.parts
        t = self.transport.target
        hello = t.hello if t is not None else None
        frag: dict[str, Any] = {
            "session_seq": self.context.session_seq,
            "rebuild_reason": "none",
            "state_block_version": self.context.state.version,
            "state_update": "none",
            "reply_modality": "none",
            "turns_in_upstream_session": self._turns_in_upstream_session,
            "interrupted": False,
            "restart_attempts_in_turn": self.rebuilder.attempts_in_turn,
            "double_voice_count": 0,
            "stale_frames_dropped": 0,
            "lease_violations": 0,
            "queue_truncated": False,
            "drops_by_reason": {"stale": 0, "expired": 0, "duplicate": 0, "overflow": 0, "retry_exhausted": 0},
            "flush_count": 0,
            "flush_ms_max": 0,
            "flush_timeouts": 0,
            "receipt_timeouts": {"started": 0, "ended": 0},
            "consumer_dropped_frames": 0,
            "suppressed_frames": 0,
            "receipt_mode": self.arbiter.receipt_mode.value,
            "anchors": {"clock_quality": "gateway_rx"},  # no RTT anchor yet → spec 06 §2.1 rule 2
            "usage": {"usage_semantics": "net", "responses": [], "local_tts_chars": 0},
            "action": "none",
            "decision_layer": "model",
        }
        if p.profile is Profile.A:
            frag.update(
                {
                    "device_id": str((hello.client or {}).get("model", "unknown")) if hello is not None else "unknown",
                    "server_vad_segments_in_turn": 0,
                    "dropped_short_press": False,
                    "uplink_seq_gaps": 0,
                    "downlink_lead_ms": t.lead_ms if t is not None else 0,
                    "device_events": [],
                    "playback_receipts": [],
                    "receipt_missing": 0,
                    "stt_echo_timeout": False,
                }
            )
        return frag

    def _on_audio_sent(self) -> None:
        if self.built:
            self.rebuilder.note_downlink_audio()

    def _is_committed(self) -> bool:
        cur = self.machine.current_turn if self.built else None
        return cur is None or cur.committed_at_ms is not None

    def _phase(self) -> str:
        if not self.built:
            return "global"
        return self.context.state.values().get("stage") or "global"

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _caps_to_dict(caps: Any) -> dict[str, Any]:
    """Transport `ClientCaps` dataclass → the raw hello dict shape the turn machine parses."""
    d = asdict(caps)
    out: dict[str, Any] = {}
    for k, v in d.items():
        if hasattr(v, "value"):
            out[k] = v.value
        elif isinstance(v, list):
            out[k] = [x.value if hasattr(x, "value") else x for x in v]
        else:
            out[k] = v
    return out
