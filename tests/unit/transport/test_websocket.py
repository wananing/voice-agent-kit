"""One real server/client pair over the `websockets` library (spec 01 §3.0)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from vak.core.clock import MonotonicClock
from vak.core.defaults import TurnTimers
from vak.core.ids import IdGen
from vak.core.types import TurnId
from vak.transport import protocol as P
from vak.transport.framing import UplinkHeader, pack_uplink, unpack_downlink
from vak.transport.websocket import WebSocketTransport

HELLO: dict[str, Any] = {
    "type": "hello",
    "proto": "va/1",
    "profile": "A",
    "client": {"model": "test", "fw": "0"},
    "caps": {
        "aec": "device",
        "modes": ["ptt"],
        "audio_up": {"codec": "pcm16", "rate": 16000, "channels": 1, "frame_ms": 20},
        "audio_down": {"codecs": ["pcm16"], "rates": [16000]},
    },
    "resume": None,
}
PCM = b"\x02\x00" * 320


async def test_websocket_server_client_roundtrip() -> None:
    got: dict[str, Any] = {"msgs": [], "audio": []}
    event_seen = asyncio.Event()
    server_done = asyncio.Event()

    async def handler(ws: Any) -> None:
        t = WebSocketTransport(ws, clock=MonotonicClock(), timers=TurnTimers(), ids=IdGen(), session_id="S-ws")
        got["hello"] = await t.handshake()

        async def on_message(m: dict[str, Any]) -> None:
            got["msgs"].append(m)
            if m["type"] == "event":
                event_seen.set()

        async def on_audio(seq: int, ts: int, tid: int, flags: int, pcm: bytes) -> None:
            got["audio"].append((seq, ts, tid, flags, pcm))

        run = asyncio.get_running_loop().create_task(t.run(on_message, on_audio))
        await event_seen.wait()
        t.set_generation(1)
        assert await t.send_audio(PCM, TurnId("c", 1), 0, 0, 1)
        await t.send_json(P.stt(TurnId("c", 1), "hi", True))
        await t.flush([TurnId("c", 1)], "abort", "abort")
        await run
        got["transport"] = t
        server_done.set()

    async with asyncio.timeout(10), serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}", proxy=None) as ws:  # ignore http_proxy env
            await ws.send(json.dumps(HELLO))
            reply = json.loads(await ws.recv())
            assert reply["type"] == "hello" and reply["session_id"] == "S-ws"
            assert reply["audio_down"] == {"codec": "pcm16", "rate": 16000, "frame_ms": 20}
            await ws.send(json.dumps({"type": "ping", "client_mono_ms": 5}))
            pong = json.loads(await ws.recv())
            assert pong["type"] == "pong" and pong["client_mono_ms"] == 5
            await ws.send(pack_uplink(UplinkHeader(seq=1, ts_samples=0, turn_id=TurnId("c", 1), key_down=True), PCM))
            await ws.send(json.dumps({"type": "event", "id": "ev-1", "name": "nfc", "ts_ms": 1, "data": {}}))
            frames: list[Any] = []
            while len(frames) < 4:  # ack, audio frame, stt, flush.start, flush.end (audio/stt order not fixed)
                frames.append(await ws.recv())
                if isinstance(frames[-1], str) and json.loads(frames[-1])["type"] == "flush.end":
                    break
            texts = [json.loads(f) for f in frames if isinstance(f, str)]
            binaries = [f for f in frames if isinstance(f, bytes)]
            assert texts[0] == {"type": "event.ack", "id": "ev-1", "status": "accepted"}
            assert any(m["type"] == "stt" and m["text"] == "hi" for m in texts)
            assert len(binaries) == 1
            hdr, payload = unpack_downlink(binaries[0])
            assert hdr.turn_id == TurnId("c", 1) and hdr.seq == 1 and payload == PCM
        await server_done.wait()

    assert got["hello"].profile.value == "A"
    assert got["audio"] == [(1, 0, 1, 0x01, PCM)]
    assert [m["type"] for m in got["msgs"]] == ["event"]
