"""Transport core shared by the WebSocket and loopback transports (spec 01 §3, §4.3, §6).

`BaseTransport` implements `TransportPort` plus the connection-level duties
that do not need a socket: hello / resume handshake, uplink seq dedupe and
gap counting, ping -> pong clock anchors, event ack with idempotency by id,
`cmd` with timeout, write timeout (`T_write`), pacing and the generation
filter. Subclasses provide three raw I/O methods. `LoopbackTransport` is
the in-memory variant used by tests and by the integration layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from vak.audio.codec import FRAME_MS, AudioCodec, make_codec
from vak.core.clock import Clock
from vak.core.defaults import TurnTimers
from vak.core.ids import IdGen
from vak.core.types import ErrorCode, Mode, TurnId
from vak.transport import protocol as P
from vak.transport.framing import U32, DownlinkHeader, FramingError, pack_downlink, serial_gt, unpack_uplink
from vak.transport.pacer import DownlinkPacer, PacedFrame
from vak.transport.protocol import AudioDownChoice, ClientHello, ProtocolError, ResumeRequest

log = logging.getLogger(__name__)

OnMessage = Callable[[dict[str, Any]], Awaitable[None]]
OnAudio = Callable[[int, int, int, int, bytes], Awaitable[None]]
"""`on_audio(seq, ts_samples, turn_id_u32, flags, pcm16)` — one decoded uplink frame."""

SEEN_EVENT_IDS_MAX = 1024  # idempotency window for `event.id` (spec 01 §3.1.7); not a spec value


class TransportClosed(RuntimeError):
    """The connection is gone (closed, or the peer was declared dead by `T_write`)."""


class HandshakeFailed(RuntimeError):
    """No valid hello within `T_hello`, or the connection closed first."""


@dataclass(slots=True)
class ResumeState:
    """What a session keeps per connection so a reconnect can resume (spec 01 §6.2)."""

    session_id: str
    last_rx_seq: int  # last uplink seq the gateway received (client retransmits after it)
    last_tx_seq: int  # last downlink seq sent; numbering continues, never restarts
    turns: list[tuple[TurnId, str]] = field(default_factory=list)
    event_ids: set[str] = field(default_factory=set)  # acceptance item 20: exactly-once across reconnect


ResumeLookup = Callable[[ResumeRequest], ResumeState | None]


class BaseTransport:
    def __init__(
        self,
        *,
        clock: Clock,
        timers: TurnTimers,
        ids: IdGen,
        session_id: str,
        mode: Mode = Mode.PTT,
        next_server_turn: int = 1,
        resume_lookup: ResumeLookup | None = None,
    ) -> None:
        self.clock = clock
        self.timers = timers
        self.ids = ids
        self.session_id = session_id
        self._wanted_mode = mode
        self._next_server_turn = next_server_turn
        self._resume_lookup = resume_lookup

        self.hello: ClientHello | None = None
        self.mode: Mode = mode
        self.audio_down: AudioDownChoice | None = None
        self.resumed = False
        self._codec_down: AudioCodec | None = None
        self._codec_up: AudioCodec | None = None

        self._generation = 0
        self._tx_seq = 0
        self._last_rx_seq: int | None = None
        self.uplink_dup_frames = 0
        self.uplink_gaps = 0
        self.uplink_lost_frames = 0
        self.uplink_decode_errors = 0

        # (client_mono_ms, server_ms) pairs for the trace clock_quality (spec 01 §3.1.8)
        self.clock_anchors: deque[tuple[int, int]] = deque(maxlen=16)
        self._seen_events: OrderedDict[str, None] = OrderedDict()
        self._pending_cmds: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.cmd_timeouts = 0

        self.peer_dead = False
        self.closed = False
        self._pacer = DownlinkPacer(clock, self._send_frame, timers.lead_ms)

    # -- raw I/O (subclasses) -------------------------------------------------

    async def _raw_send(self, data: str | bytes) -> None:
        raise NotImplementedError

    async def _raw_recv(self) -> str | bytes | None:
        """Next frame, or None when the connection is closed."""
        raise NotImplementedError

    async def _raw_close(self) -> None:
        raise NotImplementedError

    # -- helpers --------------------------------------------------------------

    async def _race(self, aw: Awaitable[Any], timeout_ms: int) -> tuple[bool, Any]:
        """Run `aw` against `clock.sleep(timeout_ms)`. Returns (completed, result)."""
        task = asyncio.ensure_future(aw)
        sleeper = asyncio.ensure_future(self.clock.sleep(timeout_ms))
        done, _ = await asyncio.wait({task, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            sleeper.cancel()
            return True, task.result()
        task.cancel()
        return False, None

    async def _send_raw(self, data: str | bytes) -> None:
        if self.closed:
            raise TransportClosed("transport closed")
        ok, _ = await self._race(self._raw_send(data), self.timers.T_write)
        if not ok:  # spec 01 §2.5 T_write: a blocked write means the peer is dead
            self.peer_dead = True
            log.warning("session %s: write blocked for T_write=%d ms, peer dead", self.session_id, self.timers.T_write)
            await self.close("write_timeout")
            raise TransportClosed("write timeout")

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        try:
            msg = json.loads(raw)
        except ValueError as e:
            raise ProtocolError(f"invalid JSON: {e}") from e
        if not isinstance(msg, dict):
            raise ProtocolError("message must be a JSON object")
        return msg

    async def _reject(self, exc: ProtocolError) -> None:
        """Rule 17: answer error{protocol_error}, keep the connection."""
        await self.send_json(P.protocol_error(exc))

    def _remember_event(self, event_id: str) -> None:
        self._seen_events[event_id] = None
        while len(self._seen_events) > SEEN_EVENT_IDS_MAX:
            self._seen_events.popitem(last=False)

    # -- handshake (spec 01 §3.1.1, §6.2) --------------------------------------

    async def handshake(self) -> ClientHello:
        ok, hello = await self._race(self._handshake_loop(), self.timers.T_hello)
        if not ok:
            raise HandshakeFailed("no valid hello within T_hello")
        return hello

    async def _handshake_loop(self) -> ClientHello:
        while True:
            raw = await self._raw_recv()
            if raw is None:
                raise HandshakeFailed("connection closed before hello")
            if isinstance(raw, bytes):
                continue  # audio before hello: ignore
            try:
                msg = self._parse_json(raw)
                t = P.validate_uplink(msg)
            except ProtocolError as e:
                await self._reject(e)
                continue
            if t is None:
                continue
            if t != "hello":
                await self._reject(ProtocolError(f"expected hello before {t}"))
                continue
            try:
                hello = P.parse_hello(msg)
            except ProtocolError as e:
                await self._reject(e)
                continue
            down = P.negotiate_audio_down(hello.caps)
            up = P.negotiate_audio_up(hello.caps)
            if down is None or up is None:
                await self._reject(ProtocolError("no common audio codec / sample rate"))
                continue
            resume_reply: dict[str, Any] | None = None
            if hello.resume is not None:
                state = self._resume_lookup(hello.resume) if self._resume_lookup else None
                if state is None or state.session_id != hello.resume.session_id:
                    await self.send_json(
                        P.error(None, ErrorCode.SESSION_EXPIRED, "resume window expired or session unknown", fatal=True)
                    )
                    continue  # client drops its turn state and sends a fresh hello
                self.session_id = state.session_id
                self._last_rx_seq = state.last_rx_seq
                self._tx_seq = state.last_tx_seq
                for eid in state.event_ids:
                    self._remember_event(eid)
                self.resumed = True
                resume_reply = P.resume_ok(state.last_rx_seq, state.turns)
            self.hello = hello
            self.mode = P.negotiate_mode(hello.caps, self._wanted_mode)
            self.audio_down = down
            self._codec_down = make_codec(down.codec, down.rate, down.frame_ms)
            self._codec_up = make_codec(up.codec, up.rate, up.frame_ms)
            self._pacer.lead_ms = self.lead_ms
            await self.send_json(
                P.hello_reply(
                    session_id=self.session_id,
                    mode=self.mode,
                    audio_down=down,
                    lead_ms=self.lead_ms,
                    timers=self.timers,
                    profile=hello.profile,
                    next_server_turn=self._next_server_turn,
                    server_time_ms=self.clock.epoch_ms(),
                    resume=resume_reply,
                )
            )
            self._pacer.start()
            return hello

    # -- read loop (spec 01 §3.1) ------------------------------------------------

    async def run(self, on_message: OnMessage, on_audio: OnAudio) -> None:
        """Read until the connection closes, dispatching control messages to
        `on_message` and decoded audio to `on_audio`. Handles ping, event ack
        and cmd.result itself."""
        try:
            while True:
                raw = await self._raw_recv()
                if raw is None:
                    break
                if isinstance(raw, bytes):
                    await self._on_binary(raw, on_audio)
                    continue
                try:
                    msg = self._parse_json(raw)
                    t = P.validate_uplink(msg)
                except ProtocolError as e:
                    await self._reject(e)
                    continue
                if t is None or t == "hello":
                    continue  # unknown type (rule 17) or a late hello: ignore
                if t == "ping":
                    server_ms = self.clock.epoch_ms()
                    self.clock_anchors.append((int(msg["client_mono_ms"]), server_ms))
                    await self.send_json(P.pong(int(msg["client_mono_ms"]), server_ms))
                    continue
                if t == "cmd.result":
                    fut = self._pending_cmds.pop(str(msg["id"]), None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                    continue
                if t == "event":
                    await self._on_event(msg, on_message)
                    continue
                await on_message(msg)
        finally:
            await self._pacer.stop()

    async def _on_event(self, msg: dict[str, Any], on_message: OnMessage) -> None:
        eid = str(msg["id"])
        if eid in self._seen_events:  # spec 01 §3.1.7: idempotent by id, still ack
            await self.send_json(P.event_ack(eid, "duplicate"))
            return
        try:
            await on_message(msg)
        except Exception:  # noqa: BLE001 - handler failure must not kill the read loop
            log.exception("event %s handler failed", eid)
            await self.send_json(P.event_ack(eid, "rejected", code=ErrorCode.INTERNAL.value))
            return
        self._remember_event(eid)
        await self.send_json(P.event_ack(eid, "accepted"))

    async def _on_binary(self, raw: bytes, on_audio: OnAudio) -> None:
        try:
            h, payload = unpack_uplink(raw)
        except FramingError as e:
            await self._reject(ProtocolError(f"bad audio frame: {e}"))
            return
        if self._last_rx_seq is not None:
            if not serial_gt(h.seq, self._last_rx_seq):  # spec 01 §6.1: dedupe retransmits by seq
                self.uplink_dup_frames += 1
                return
            expected = (self._last_rx_seq + 1) % U32
            if h.seq != expected:
                self.uplink_gaps += 1
                self.uplink_lost_frames += (h.seq - expected) % U32
        self._last_rx_seq = h.seq
        assert self._codec_up is not None, "handshake first"
        try:
            pcm = self._codec_up.decode(payload)
        except Exception:  # noqa: BLE001 - a corrupt frame is data, not a bug in the loop
            self.uplink_decode_errors += 1
            log.warning("session %s: uplink frame seq=%d failed to decode", self.session_id, h.seq)
            return
        tid = h.turn_id.to_u32() if h.turn_id is not None else 0
        await on_audio(h.seq, h.ts_samples, tid, h.flags, pcm)

    # -- TransportPort (vak/core/ports.py) ----------------------------------------

    async def send_json(self, msg: dict[str, Any]) -> None:
        await self._send_raw(json.dumps(msg, ensure_ascii=False, separators=(",", ":")))

    async def send_audio(
        self,
        pcm16: bytes,
        turn_id: TurnId,
        sentence_id: int,
        frame_idx: int,
        generation: int,
        uninterruptible: bool = False,
    ) -> bool:
        if generation < self._generation:  # spec 02 rule 11: consumer-side filter
            self._pacer.consumer_dropped_frames += 1
            return False
        assert self._codec_down is not None and self.audio_down is not None, "handshake first"
        frame = PacedFrame(
            payload=self._codec_down.encode(pcm16),
            duration_ms=(len(pcm16) // 2) * 1000 // self.audio_down.rate,
            generation=generation,
            turn_id=turn_id,
            sentence_id=sentence_id,
            frame_idx=frame_idx,
            uninterruptible=uninterruptible,
        )
        return self._pacer.push(frame)

    async def flush(self, turn_ids: list[TurnId], reason: str, ack_of: str | None) -> str:
        """spec 01 R3.2-1..3: flush.start, clear the queue, flush.end — same tick."""
        flush_id = self.ids.new("f")
        await self.send_json(P.flush_start(flush_id, list(turn_ids), reason, ack_of))
        self._pacer.flush()
        await self.send_json(P.flush_end(flush_id))
        return flush_id

    async def wait_sent(self) -> None:
        """Wait until every queued downlink frame has left the pacer."""
        await self._pacer.wait_drained()

    def set_generation(self, generation: int) -> None:
        self._generation = generation
        self._pacer.set_generation(generation)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def lead_ms(self) -> int:
        """spec 01 R4.3-4: lead <= play_buffer_max_ms - 200, never below one frame."""
        lead = self.timers.lead_ms
        if self.hello is not None:
            lead = min(lead, self.hello.caps.play_buffer_max_ms - 200)
        return max(FRAME_MS, lead)

    @property
    def sample_rate(self) -> int:
        """Negotiated downlink rate; callers hand `send_audio` PCM16 at this rate."""
        assert self.audio_down is not None, "handshake first"
        return self.audio_down.rate

    @property
    def consumer_dropped_frames(self) -> int:
        return self._pacer.consumer_dropped_frames

    @property
    def pacer(self) -> DownlinkPacer:
        return self._pacer

    # -- commands (spec 01 §3.2.7) --------------------------------------------------

    async def cmd(self, name: str, args: dict[str, Any], timeout_ms: int | None = None) -> dict[str, Any] | None:
        """Send a `cmd`; return the `cmd.result` or None on timeout (counted, never resent)."""
        cmd_id = self.ids.new("cmd")
        t = timeout_ms if timeout_ms is not None else self.timers.cmd_timeout_ms
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_cmds[cmd_id] = fut
        await self.send_json(P.cmd(cmd_id, name, args, t))
        ok, result = await self._race(fut, t)
        self._pending_cmds.pop(cmd_id, None)
        if not ok:
            self.cmd_timeouts += 1
            return None
        return result

    # -- lifecycle ----------------------------------------------------------------------

    def resume_state(self) -> ResumeState:
        """Snapshot for the session registry so a reconnect can resume (turns filled by the session)."""
        return ResumeState(
            session_id=self.session_id,
            last_rx_seq=self._last_rx_seq if self._last_rx_seq is not None else 0,
            last_tx_seq=self._tx_seq,
            event_ids=set(self._seen_events),
        )

    @property
    def last_rx_seq(self) -> int | None:
        return self._last_rx_seq

    @property
    def tx_seq(self) -> int:
        return self._tx_seq

    async def close(self, reason: str = "close") -> None:
        if self.closed:
            return
        self.closed = True
        log.debug("session %s: transport closing (%s)", self.session_id, reason)
        for fut in self._pending_cmds.values():
            if not fut.done():
                fut.cancel()
        self._pending_cmds.clear()
        await self._pacer.stop()
        await self._raw_close()

    # -- pacer sink -------------------------------------------------------------------------

    async def _send_frame(self, f: PacedFrame) -> None:
        self._tx_seq = (self._tx_seq + 1) % U32
        hdr = DownlinkHeader(
            seq=self._tx_seq,
            turn_id=f.turn_id,
            sentence_id=f.sentence_id,
            frame_idx=f.frame_idx,
            uninterruptible=f.uninterruptible,
        )
        await self._send_raw(pack_downlink(hdr, f.payload))


class LoopbackTransport(BaseTransport):
    """In-memory transport: the "client" is the test (or the integration layer).

    `client_send` feeds uplink frames; `client_recv` / `sent` observe downlink.
    """

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._inbox: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.outbox: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.sent: list[str | bytes] = []

    async def _raw_send(self, data: str | bytes) -> None:
        self.sent.append(data)
        self.outbox.put_nowait(data)

    async def _raw_recv(self) -> str | bytes | None:
        return await self._inbox.get()

    async def _raw_close(self) -> None:
        self._inbox.put_nowait(None)

    # client side

    def client_send(self, msg: dict[str, Any] | bytes) -> None:
        self._inbox.put_nowait(json.dumps(msg) if isinstance(msg, dict) else msg)

    def client_close(self) -> None:
        self._inbox.put_nowait(None)

    async def client_recv(self) -> dict[str, Any] | bytes:
        data = await self.outbox.get()
        return json.loads(data) if isinstance(data, str) else data

    def sent_json(self) -> list[dict[str, Any]]:
        return [json.loads(s) for s in self.sent if isinstance(s, str)]

    def sent_frames(self) -> list[bytes]:
        return [s for s in self.sent if isinstance(s, bytes)]
