"""End-to-end harness: an Agent built from a generated project, one Session
over a LoopbackTransport, a scripted client that speaks va/1.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vak.agent import Agent
from vak.core.ids import IdGen
from vak.core.session import Session
from vak.core.types import TurnId
from vak.transport.base import LoopbackTransport
from vak.transport.framing import UplinkHeader, pack_uplink, unpack_downlink
from vak.upstream.providers.mock import tone_pcm

REPO = Path(__file__).resolve().parents[2]


def new_project(tmp: Path, name: str, *, pipeline: str, profile: str = "A", mode: str = "ptt") -> Path:
    """`vak new` into tmp, mock providers."""
    dest = tmp / name
    r = subprocess.run(
        [sys.executable, "-m", "vak.cli", "new", str(dest), "--pipeline", pipeline, "--profile", profile, "--mode", mode, "--providers", "mock"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    return dest / "agent.yaml"


@dataclass
class Client:
    """Scripted va/1 client on the loopback transport."""

    transport: LoopbackTransport
    seq: int = 0
    ts_samples: int = 0
    turn_n: int = 0
    received: list[Any] = field(default_factory=list)
    frames: dict[str, list[tuple[int, int, bytes]]] = field(default_factory=dict)  # turn_id -> (sentence, idx, pcm)
    hello_reply: dict[str, Any] | None = None
    ended: dict[str, set[int]] = field(default_factory=dict)
    _reader: asyncio.Task[None] | None = None
    receipts: bool = True

    def start_reader(self) -> None:
        self._reader = asyncio.get_running_loop().create_task(self._read())

    async def _read(self) -> None:
        while True:
            msg = await self.transport.client_recv()
            if isinstance(msg, bytes):
                h, pcm = unpack_downlink(msg)
                tid = str(h.turn_id) if h.turn_id else "0"
                self.frames.setdefault(tid, []).append((h.sentence_id, h.frame_idx, pcm))
                continue
            self.received.append(msg)
            if msg.get("type") == "hello":
                self.hello_reply = msg
            if self.receipts:
                await self._auto_receipts(msg)

    async def _auto_receipts(self, msg: dict[str, Any]) -> None:
        """A well-behaved device: started when the sentence starts, ended when tts.stop arrives."""
        t = msg.get("type")
        if t == "tts.sentence_start":
            self.transport.client_send(
                {"type": "playback.started", "turn_id": msg["turn_id"], "sentence_id": msg["sentence_id"], "played_ms": 0, "ts_ms": 0}
            )
        if t == "tts.stop" and msg.get("reason") == "done":
            tid = msg["turn_id"]
            sentences = sorted({s for s, _, _ in self.frames.get(tid, [])} - self.ended.get(tid, set()))
            for s in sentences:
                self.ended.setdefault(tid, set()).add(s)
                pcm_len = sum(len(p) for ss, _, p in self.frames.get(tid, []) if ss == s)
                rate = (self.hello_reply or {}).get("audio_down", {}).get("rate", 16000)
                played = pcm_len // 2 * 1000 // rate
                self.transport.client_send(
                    {"type": "playback.ended", "turn_id": tid, "sentence_id": s, "played_ms": played, "ts_ms": 0}
                )

    def hello(self, *, profile: str = "A", aec: str = "os", modes: list[str] | None = None) -> None:
        self.transport.client_send(
            {
                "type": "hello",
                "proto": "va/1",
                "profile": profile,
                "client": {"model": "test", "fw": "0"},
                "caps": {
                    "transport": "ws",
                    "aec": aec,
                    "modes": modes or ["ptt", "open_mic"],
                    "sources": ["button", "text"],
                    "receipts": ["started", "ended", "interrupted"],
                    "audio_up": {"codec": "pcm16", "rate": 16000, "channels": 1, "frame_ms": 20},
                    "audio_down": {"codecs": ["pcm16"], "rates": [16000]},
                    "play_buffer_max_ms": 1200,
                    "preroll_ms": 0,
                    "uninterruptible": True,
                    "events": [],
                    "commands": [],
                },
                "resume": None,
            }
        )

    def next_turn(self) -> TurnId:
        self.turn_n += 1
        return TurnId("c", self.turn_n)

    def press(self, turn: TurnId) -> None:
        self.transport.client_send({"type": "turn.start", "turn_id": str(turn), "source": "button", "ts_samples": self.ts_samples, "preroll_ms": 0})

    def release(self, turn: TurnId) -> None:
        self.transport.client_send({"type": "turn.end", "turn_id": str(turn), "source": "button", "ts_samples": self.ts_samples, "reason": "release"})

    def text(self, turn: TurnId, text: str) -> None:
        self.transport.client_send({"type": "turn.start", "turn_id": str(turn), "source": "text", "text": text, "input": "typed"})

    def speak(self, turn: TurnId | None, ms: int, *, key_down: bool = True) -> None:
        """Send `ms` of tone as 20 ms pcm16 frames."""
        pcm = tone_pcm(ms, 16000)
        frame = 16000 * 20 // 1000 * 2
        for off in range(0, len(pcm), frame):
            self.seq += 1
            h = UplinkHeader(seq=self.seq, ts_samples=self.ts_samples, turn_id=turn, key_down=key_down)
            self.transport.client_send(pack_uplink(h, pcm[off : off + frame]))
            self.ts_samples += frame // 2

    def interrupt(self, turn: TurnId, sentence_id: int, played_ms: int) -> None:
        self.transport.client_send({"type": "playback.interrupted", "turn_id": str(turn), "sentence_id": sentence_id, "played_ms": played_ms, "ts_ms": 0, "reason": "button"})
        self.transport.client_send({"type": "abort", "turn_id": str(turn), "reason": "button"})

    async def wait_for(self, pred: Any, timeout_s: float = 5.0) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            for m in self.received:
                if pred(m):
                    return m
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"no message matching; got {[m.get('type') for m in self.received]}")
            await asyncio.sleep(0.01)

    def of_type(self, t: str) -> list[dict[str, Any]]:
        return [m for m in self.received if m.get("type") == t]

    def closed(self, turn: TurnId | str) -> dict[str, Any] | None:
        for m in self.received:
            if m.get("type") == "turn.closed" and m.get("turn_id") == str(turn):
                return m
        return None


@dataclass
class Running:
    agent: Agent
    session: Session
    transport: LoopbackTransport
    client: Client
    task: asyncio.Task[None]

    async def stop(self) -> None:
        self.transport.client_close()
        try:
            await asyncio.wait_for(self.task, 5)
        except (TimeoutError, asyncio.CancelledError):
            self.task.cancel()
        await self.session.close("test_done")
        if self.client._reader:
            self.client._reader.cancel()

    def traces(self) -> list[dict[str, Any]]:
        exp = self.agent.memory_exporter
        return list(exp.records) if exp is not None else []


async def start(config_path: Path, **client_kw: Any) -> Running:
    agent = Agent.from_yaml(config_path, in_memory_traces=True)
    parts = agent.parts
    transport = LoopbackTransport(
        clock=agent.clock, timers=parts.defaults.turn, ids=IdGen(), session_id="S-test", mode=parts.mode
    )
    client = Client(transport)
    client.start_reader()
    client.hello(**client_kw)
    await transport.handshake()
    session = Session(parts=parts, clock=agent.clock, session_id="S-test")
    await session.attach(transport)
    task = asyncio.get_running_loop().create_task(session.run(transport))
    return Running(agent, session, transport, client, task)


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
