"""Gateway over a real WebSocket: static client hosting, hello, a text turn,
resume after reconnect, and the `vak new` presets."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.client import connect

from tests.integration.harness import cleanup, new_project
from vak.agent import Agent
from vak.config import AgentConfig

HELLO = {
    "type": "hello",
    "proto": "va/1",
    "profile": "A",
    "client": {"model": "pytest", "fw": "0"},
    "caps": {
        "transport": "ws",
        "aec": "os",
        "modes": ["ptt"],
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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _recv_until(ws, pred, timeout=8.0):
    msgs = []
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=deadline - asyncio.get_running_loop().time())
        if isinstance(raw, bytes):
            continue
        m = json.loads(raw)
        msgs.append(m)
        if pred(m):
            return m, msgs
    raise TimeoutError([m.get("type") for m in msgs])


async def test_gateway_serves_client_and_runs_a_text_turn(tmp_path: Path):
    cfg = new_project(tmp_path, "gw", pipeline="cascade")
    agent = Agent.from_yaml(cfg, in_memory_traces=True)
    port = _free_port()
    gw = agent.gateway(host="127.0.0.1", port=port)
    task = asyncio.get_running_loop().create_task(gw.serve())
    try:
        await asyncio.sleep(0.3)
        async with httpx.AsyncClient(trust_env=False) as http:
            r = await http.get(f"http://127.0.0.1:{port}/")
            assert r.status_code == 200 and "<html" in r.text.lower()
            r = await http.get(f"http://127.0.0.1:{port}/protocol.js")
            assert r.status_code == 200 and "text/javascript" in r.headers["content-type"]
            r = await http.get(f"http://127.0.0.1:{port}/../pyproject.toml")
            assert r.status_code == 404
        async with connect(f"ws://127.0.0.1:{port}/ws", proxy=None) as ws:
            await ws.send(json.dumps(HELLO))
            hello, _ = await _recv_until(ws, lambda m: m["type"] == "hello")
            assert hello["mode"] == "ptt" and hello["audio_down"]["codec"] == "pcm16"
            await ws.send(json.dumps({"type": "turn.start", "turn_id": "c1", "source": "text", "text": "现在几点", "input": "typed"}))
            closed, msgs = await _recv_until(ws, lambda m: m["type"] == "turn.closed" and m["turn_id"] == "c1")
            # device receipts are needed for completion; without them T_drain closes it later.
            # Here we answer every sentence to finish fast.
            assert closed["state"] in ("completed", "interrupted")
            assert any(m["type"] == "tts.sentence_start" for m in msgs)
    finally:
        await gw.close()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        cleanup(cfg.parent)


async def test_resume_within_window(tmp_path: Path):
    cfg = new_project(tmp_path, "gw2", pipeline="cascade")
    agent = Agent.from_yaml(cfg, in_memory_traces=True)
    port = _free_port()
    gw = agent.gateway(host="127.0.0.1", port=port)
    task = asyncio.get_running_loop().create_task(gw.serve())
    try:
        await asyncio.sleep(0.3)
        async with connect(f"ws://127.0.0.1:{port}/ws", proxy=None) as ws:
            await ws.send(json.dumps(HELLO))
            hello, _ = await _recv_until(ws, lambda m: m["type"] == "hello")
            sid = hello["session_id"]
        await asyncio.sleep(0.2)
        async with connect(f"ws://127.0.0.1:{port}/ws", proxy=None) as ws:
            resume = {**HELLO, "resume": {"session_id": sid, "last_tx_seq": 0, "last_rx_seq": 0, "current_turn": None, "turn_state": None}}
            await ws.send(json.dumps(resume))
            hello2, _ = await _recv_until(ws, lambda m: m["type"] == "hello")
            assert hello2["session_id"] == sid and hello2["resume"]["ok"] is True
        async with connect(f"ws://127.0.0.1:{port}/ws", proxy=None) as ws:
            bad = {**HELLO, "resume": {"session_id": "S-nope", "last_tx_seq": 0, "last_rx_seq": 0}}
            await ws.send(json.dumps(bad))
            err, _ = await _recv_until(ws, lambda m: m["type"] == "error")
            assert err["code"] == "session_expired" and err["fatal"] is True
    finally:
        await gw.close()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        cleanup(cfg.parent)


@pytest.mark.parametrize(
    "pipeline,preset",
    [
        ("cascade", "mock"),
        ("cascade", "cloud"),
        ("cascade", "selfhost_zh"),
        ("cascade", "volcengine"),
        ("cascade", "aliyun"),
        ("half_cascade", "mock"),
        ("half_cascade", "cloud"),
        ("s2s", "mock"),
        ("s2s", "openai"),
        ("s2s", "dashscope"),
        ("full_duplex", "mock"),
        ("full_duplex", "moshi"),
    ],
)
def test_new_presets_produce_loadable_configs(tmp_path: Path, pipeline: str, preset: str, monkeypatch):
    import subprocess
    import sys

    from tests.integration.harness import REPO

    for var in ("OPENAI_API_KEY", "DASHSCOPE_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "VOLC_ASR_APP_KEY", "VOLC_ASR_ACCESS_KEY", "ARK_API_KEY"):
        monkeypatch.setenv(var, "x")
    dest = tmp_path / f"{pipeline}_{preset}"
    r = subprocess.run(
        [sys.executable, "-m", "vak.cli", "new", str(dest), "--pipeline", pipeline, "--providers", preset],
        cwd=REPO, capture_output=True, text=True, env={**os.environ},
    )
    assert r.returncode == 0, r.stderr
    cfg = AgentConfig.load(dest / "agent.yaml")
    assert cfg.pipeline.value == pipeline
    if preset == "mock":
        Agent(cfg, in_memory_traces=True)  # providers construct without any service
