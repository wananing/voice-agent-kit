"""Minimal fakes for the arbiter's ports (contributing.md §4)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from vak.core.arbiter import Arbiter
from vak.core.bus import Bus
from vak.core.clock import FakeClock
from vak.core.defaults import ArbiterParams
from vak.core.ids import IdGen
from vak.core.types import ContextEntry, PlaybackReceipt, Priority, Profile, TurnId
from vak.upstream.base import Ack
from vak.upstream.providers.base import TTSChunk

RATE = 16000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2


def tone(ms: int, rate: int = RATE) -> bytes:
    """Non-silent PCM16 (constant 4000 ≈ -18 dBFS)."""
    return b"\xa0\x0f" * (rate * ms // 1000)


def silence(ms: int, rate: int = RATE) -> bytes:
    return b"\x00\x00" * (rate * ms // 1000)


@dataclass(slots=True)
class Frame:
    pcm: bytes
    turn_id: TurnId
    sentence_id: int
    frame_idx: int
    generation: int
    uninterruptible: bool
    t_ms: int


class FakeTransport:
    """Records json and frames in order; applies its own generation filter."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.json: list[dict[str, Any]] = []
        self.frames: list[Frame] = []
        self.events: list[tuple[str, Any]] = []  # ("json", msg) | ("frame", Frame) | ("flush", (id, turns))
        self.flushes: list[tuple[str, list[TurnId], str, str | None]] = []
        self.generation = 0
        self.consumer_dropped = 0
        self._n = 0
        self.lead_ms = 0

    async def send_json(self, msg: dict[str, Any]) -> None:
        self.json.append(msg)
        self.events.append(("json", msg))

    async def send_audio(
        self,
        pcm16: bytes,
        turn_id: TurnId,
        sentence_id: int,
        frame_idx: int,
        generation: int,
        uninterruptible: bool = False,
    ) -> bool:
        if generation < self.generation:
            self.consumer_dropped += 1
            return False
        f = Frame(pcm16, turn_id, sentence_id, frame_idx, generation, uninterruptible, self.clock.now_ms())
        self.frames.append(f)
        self.events.append(("frame", f))
        return True

    async def flush(self, turn_ids: list[TurnId], reason: str, ack_of: str | None) -> str:
        self._n += 1
        fid = f"f-{self._n}"
        self.flushes.append((fid, list(turn_ids), reason, ack_of))
        await self.send_json(
            {
                "type": "flush.start",
                "flush_id": fid,
                "turn_ids": [str(t) for t in turn_ids],
                "reason": reason,
                "ack_of": ack_of,
            }
        )
        await self.send_json({"type": "flush.end", "flush_id": fid})
        self.events.append(("flush", (fid, list(turn_ids), self.clock.now_ms())))
        return fid

    def set_generation(self, generation: int) -> None:
        self.generation = generation

    def frames_for_turn(self, turn: TurnId) -> list[Frame]:
        return [f for f in self.frames if f.turn_id == turn]

    def frames_after_flush(self, arb: Arbiter | None = None) -> list[Frame]:
        """Frames of a flushed turn sent after that turn's flush.end, excluding
        uninterruptible frames and (with `arb`) requests granted after the flush
        or the critical request that triggered it."""
        flushed: dict[TurnId, int] = {}
        bad: list[Frame] = []
        for kind, item in self.events:
            if kind == "flush":
                for t in item[1]:
                    flushed[t] = item[2]
            elif kind == "frame" and item.turn_id in flushed and not item.uninterruptible:
                if arb is not None:
                    rid = arb.sentence_owner(item.turn_id, item.sentence_id)
                    rec = arb.record(rid) if rid else None
                    if rec is not None and (
                        rec.req.priority is Priority.CRITICAL
                        or (rec.granted_at_ms is not None and rec.granted_at_ms >= flushed[item.turn_id])
                    ):
                        continue
                bad.append(item)
        return bad


class FakeContext:
    def __init__(self) -> None:
        self.pending: list[tuple[TurnId, str, int, str, str]] = []
        self.playback: list[tuple[str, int, str, int, bool]] = []

    def record_assistant_pending(self, turn_id, request_id, sentence_id, text_generated, source):
        self.pending.append((turn_id, request_id, sentence_id, text_generated, source))
        return None  # type: ignore[return-value]

    def record_assistant_playback(self, request_id, sentence_id, state, played_ms, estimated=False):
        self.playback.append((request_id, sentence_id, state, played_ms, estimated))
        return None

    # unused by the arbiter
    def record_user(self, *a, **k) -> ContextEntry: ...
    def record_tool_call(self, call) -> ContextEntry: ...
    def record_tool_output(self, call) -> ContextEntry: ...
    def record_system_event(self, turn_id, text) -> ContextEntry: ...
    def presented_text(self, turn_id) -> str:
        return ""

    def project_history(self, caps):
        return ([], "pairs", "full", 0)

    def state_block(self): ...
    def render_instructions(self) -> str:
        return ""

    def update_state(self, name, value):
        return "lazy"


class FakeTrace:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.fields: dict[TurnId, dict[str, Any]] = {}
        self.lists: dict[TurnId, dict[str, list[dict[str, Any]]]] = {}
        self.counters: dict[TurnId, dict[str, int]] = {}

    def write(self, turn_id: TurnId, fragment: dict[str, Any]) -> None:
        d = self.fields.setdefault(turn_id, {})
        for k, v in fragment.items():
            if isinstance(v, dict) and isinstance(d.get(k), dict):
                d[k].update(v)
            else:
                d[k] = v

    def append(self, turn_id: TurnId, list_field: str, item: dict[str, Any]) -> None:
        self.lists.setdefault(turn_id, {}).setdefault(list_field, []).append(item)

    def incr(self, turn_id: TurnId, counter: str, by: int = 1) -> None:
        c = self.counters.setdefault(turn_id, {})
        c[counter] = c.get(counter, 0) + by

    def now_ms(self) -> int:
        return self.clock.now_ms()

    def counter(self, name: str) -> int:
        return sum(c.get(name, 0) for c in self.counters.values())

    def outputs(self, turn_id: TurnId | None = None) -> list[dict[str, Any]]:
        if turn_id is not None:
            return self.lists.get(turn_id, {}).get("outputs", [])
        return [o for lists in self.lists.values() for o in lists.get("outputs", [])]


class FakeUpstream:
    capabilities = None

    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.truncated: list[tuple[str, int, str | None]] = []

    async def cancel_response(self, response_id: str) -> Ack:
        self.cancelled.append(response_id)
        return Ack.success()

    async def truncate(self, item_id: str, audio_end_ms: int, played_text: str | None = None) -> Ack:
        self.truncated.append((item_id, audio_end_ms, played_text))
        return Ack.success()

    async def open(self, init):
        return Ack.success()

    async def send_audio(self, frame, meta):
        return Ack.success()

    async def commit_turn(self, turn_id, mode):
        return Ack.success()

    async def request_response(self, turn_id, response_ref, overrides=None):
        return Ack.success()

    async def send_tool_result(self, call_id, result, reply_required):
        return Ack.success()

    async def update_config(self, patch, mode):
        return Ack.success()

    async def close(self, reason):
        return Ack.success()

    def events(self):
        raise NotImplementedError


class FakeTTS:
    """Synthesises `ms_per_char` of tone per character, in `chunk_ms` chunks,
    sleeping `latency_ms` on the clock before the first chunk."""

    sample_rate = RATE
    supports_text_stream = False
    voice = "default"

    def __init__(
        self,
        clock: FakeClock,
        *,
        ms_per_char: int = 100,
        chunk_ms: int = 200,
        latency_ms: int = 0,
        flush_latency_ms: int = 0,
    ) -> None:
        self.clock = clock
        self.ms_per_char = ms_per_char
        self.chunk_ms = chunk_ms
        self.latency_ms = latency_ms
        self.flush_latency_ms = flush_latency_ms
        self.flushed: list[str] = []
        self.calls: list[tuple[str, str]] = []

    async def synthesize(self, text: str, request_id: str) -> AsyncIterator[TTSChunk]:
        self.calls.append((text, request_id))
        if self.latency_ms:
            await self.clock.sleep(self.latency_ms)
        total = max(self.chunk_ms, len(text) * self.ms_per_char)
        sent = 0
        while sent < total:
            ms = min(self.chunk_ms, total - sent)
            sent += ms
            yield TTSChunk(tone(ms), RATE, is_last=sent >= total)
            await asyncio.sleep(0)

    async def flush(self, request_id: str) -> None:
        self.flushed.append(request_id)
        if self.flush_latency_ms:
            await self.clock.sleep(self.flush_latency_ms)


class FakeCues:
    def __init__(self, cues: dict[str, int]) -> None:
        self.cues = {k: (tone(ms), RATE, ms) for k, ms in cues.items()}

    def get(self, cue_id: str):
        return self.cues.get(cue_id)


@dataclass(slots=True)
class _PlayingSentence:
    turn_id: TurnId
    sentence_id: int
    request_id: str | None
    duration_ms: int
    started_at: int


@dataclass(slots=True)
class Receipt:
    kind: str
    request_id: str | None
    turn_id: TurnId
    sentence_id: int
    t_ms: int
    played_ms: int


class ClientSim:
    """A one-speaker device: plays sentences in arrival order, honours
    flush.start, and sends receipts. `drop` lists receipt kinds to lose."""

    def __init__(
        self, arb: Arbiter, transport: FakeTransport, clock: FakeClock, *, drop: set[str] | None = None
    ) -> None:
        self.arb = arb
        self.transport = transport
        self.clock = clock
        self.drop = drop or set()
        self.receipts: list[Receipt] = []
        self.overlaps = 0  # frames of another request arriving while one is still playing
        self._seen_events = 0
        self._queue: list[tuple[TurnId, int, int]] = []  # (turn, sid, duration)
        self._durations: dict[tuple[TurnId, int], int] = {}
        self._current: _PlayingSentence | None = None
        self._ended_at: int | None = None
        self._flushed_turns: set[TurnId] = set()

    @dataclass(slots=True)
    class Interval:
        request_id: str | None
        start: int
        end: int

    def _ingest(self) -> None:
        for kind, item in self.transport.events[self._seen_events :]:
            if kind == "frame":
                key = (item.turn_id, item.sentence_id)
                dur = (len(item.pcm) // 2) * 1000 // RATE
                if key not in self._durations:
                    self._durations[key] = 0
                    self._queue.append((item.turn_id, item.sentence_id, 0))
                    owner = self.arb.sentence_owner(*key)
                    cur = self._current
                    if cur is not None and cur.request_id != owner and not self._request_done(cur.request_id):
                        self.overlaps += 1
                self._durations[key] += dur
            elif kind == "flush":
                _fid, turns, _t = item
                self._flushed_turns.update(turns)
                self._queue = [q for q in self._queue if q[0] not in turns]
                cur = self._current
                if cur is not None and cur.turn_id in turns:
                    played = self.clock.now_ms() - cur.started_at
                    self._current = None
                    self._emit("interrupted", cur, played)
        self._seen_events = len(self.transport.events)

    def _request_done(self, rid: str | None) -> bool:
        rec = self.arb.record(rid) if rid else None
        return rec is None or rec.terminal

    def _emit(self, kind: str, s: _PlayingSentence, played_ms: int) -> None:
        self.receipts.append(
            Receipt(kind, s.request_id, s.turn_id, s.sentence_id, self.clock.now_ms(), played_ms)
        )
        if kind in self.drop:
            return
        asyncio.get_running_loop().create_task(
            self.arb.on_receipt(
                PlaybackReceipt(
                    kind,
                    s.turn_id,
                    s.sentence_id,
                    played_ms,
                    self.clock.now_ms(),
                    reason="button" if kind == "interrupted" else None,
                )  # type: ignore[arg-type]
            )
        )

    async def step(self) -> None:
        """Advance playback bookkeeping to the current clock time."""
        self._ingest()
        now = self.clock.now_ms()
        cur = self._current
        if cur is not None:
            dur = self._durations[(cur.turn_id, cur.sentence_id)]
            if now - cur.started_at >= dur:
                self._current = None
                self._emit("ended", cur, dur)
        if self._current is None and self._queue:
            turn, sid, _ = self._queue.pop(0)
            self._current = _PlayingSentence(turn, sid, self.arb.sentence_owner(turn, sid), 0, now)
            self._emit("started", self._current, 0)
        await settle()

    async def run(self, ms: int, step_ms: int = FRAME_MS) -> None:
        for _ in range(max(1, ms // step_ms)):
            await self.step()
            await self.clock.advance(step_ms)
            await self.step()

    def intervals(self) -> list[Interval]:
        """[started, ended/interrupted] per sentence from the receipt log."""
        out: list[ClientSim.Interval] = []
        open_: dict[tuple[TurnId, int], Receipt] = {}
        for r in self.receipts:
            key = (r.turn_id, r.sentence_id)
            if r.kind == "started":
                open_[key] = r
            elif key in open_:
                out.append(ClientSim.Interval(r.request_id, open_.pop(key).t_ms, r.t_ms))
        return out

    def overlapping_pairs(self) -> int:
        iv = self.intervals()
        n = 0
        for i, a in enumerate(iv):
            for b in iv[i + 1 :]:
                if a.request_id != b.request_id and a.start < b.end and b.start < a.end:
                    n += 1
        return n


async def settle(n: int = 6) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


@dataclass
class World:
    clock: FakeClock
    bus: Bus
    transport: FakeTransport
    context: FakeContext
    trace: FakeTrace
    upstream: FakeUpstream
    tts: FakeTTS
    cues: FakeCues
    ids: IdGen
    arb: Arbiter
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


def make_world(
    *,
    profile: Profile = Profile.A,
    params: ArbiterParams | None = None,
    receipts: set[str] | None = None,
    tts_latency_ms: int = 0,
) -> World:
    clock = FakeClock()
    bus = Bus()
    bus.record = True
    transport = FakeTransport(clock)
    context = FakeContext()
    trace = FakeTrace(clock)
    upstream = FakeUpstream()
    tts = FakeTTS(clock, latency_ms=tts_latency_ms)
    cues = FakeCues({"short": 300, "long": 760})
    ids = IdGen()
    arb = Arbiter(
        profile=profile,
        params=params or ArbiterParams(),
        clock=clock,
        bus=bus,
        transport=transport,
        context=context,
        trace=trace,
        ids=ids,
        upstream=upstream,
        tts=tts,
        cues=cues,
        frame_ms=FRAME_MS,
        client_receipts=receipts,
    )
    return World(clock, bus, transport, context, trace, upstream, tts, cues, ids, arb)
