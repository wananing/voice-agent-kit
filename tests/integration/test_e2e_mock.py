"""End-to-end over the loopback transport with mock providers: the five
core modules, the transport and a pipeline wired by `Session`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.integration.harness import Running, cleanup, new_project, start
from vak.transport.framing import unpack_downlink

PIPELINES = ["cascade", "half_cascade", "s2s", "full_duplex"]


@pytest.fixture
def tmp_project(tmp_path: Path):
    created: list[Path] = []

    def make(pipeline: str, **kw) -> Path:
        cfg = new_project(tmp_path, f"agent_{pipeline}", pipeline=pipeline, **kw)
        created.append(cfg.parent)
        return cfg

    yield make
    for d in created:
        cleanup(d)


async def _one_turn(r: Running, *, ms: int = 1000) -> dict:
    c = r.client
    t = c.next_turn()
    c.press(t)
    c.speak(t, ms)
    c.release(t)
    closed = await c.wait_for(lambda m: m.get("type") == "turn.closed" and m.get("turn_id") == str(t), 8)
    return closed


@pytest.mark.parametrize("pipeline", PIPELINES)
async def test_button_turn_completes_on_every_pipeline(tmp_project, pipeline):
    r = await start(tmp_project(pipeline))
    try:
        closed = await _one_turn(r)
        assert closed["state"] == "completed", closed
        assert closed["by"] == "playback_ended"
        assert closed["heard_ms"] > 0
        c = r.client
        # spec 01 §2.6: tts.start → sentence_start → audio → tts.stop{done} → turn.closed, in that order
        types = [m["type"] for m in c.received if m["type"] in ("tts.start", "tts.stop", "turn.closed")]
        assert types[0] == "tts.start" and types[-1] == "turn.closed"
        assert c.frames["c1"], "no downlink audio"
        # every downlink frame belongs to the turn and carries a sentence the client was told about
        sentences = {m["sentence_id"] for m in c.of_type("tts.sentence_start")}
        assert {s for s, _, _ in c.frames["c1"]} <= sentences
        if pipeline != "full_duplex":
            # the mock script calls get_time first: A1 verbatim result is spoken by local TTS
            texts = [m.get("text", "") for m in c.of_type("tts.sentence_start")]
            assert any("现在是" in t for t in texts), texts
        await asyncio.sleep(0.2)
    finally:
        await r.stop()
    traces = r.traces()
    assert len(traces) == 1
    tr = traces[0]
    assert tr["turn_terminal"] == "completed"
    assert tr["latency"]["sound_first_at_ms"] is not None
    assert tr["latency"]["content_first_at_ms"] >= tr["latency"]["sound_first_at_ms"]
    assert not tr.get("invariant_violations")
    if pipeline != "full_duplex":
        assert tr["turn_kind"] == "tool_verbatim"
        calls = tr["tool_calls"]
        assert calls and all(c["closed_by"] for c in calls)
        assert calls[0]["tool_route"] == "A1"


async def test_barge_in_flushes_and_closes_interrupted(tmp_project):
    r = await start(tmp_project("cascade"))
    try:
        c = r.client
        c.receipts = False  # we play the device by hand here
        t1 = c.next_turn()
        c.press(t1)
        c.speak(t1, 800)
        c.release(t1)
        first = await c.wait_for(lambda m: m.get("type") == "tts.sentence_start", 8)
        # let some audio flow, then press again mid-sentence (spec 01 §2.8 order)
        await asyncio.sleep(0.25)
        frames_before = len(c.frames.get("c1", []))
        assert frames_before > 0
        c.transport.client_send(
            {"type": "playback.started", "turn_id": "c1", "sentence_id": first["sentence_id"], "played_ms": 0, "ts_ms": 0}
        )
        c.interrupt(t1, first["sentence_id"], 120)
        t2 = c.next_turn()
        c.press(t2)
        closed1 = await c.wait_for(lambda m: m.get("type") == "turn.closed" and m.get("turn_id") == "c1", 5)
        assert closed1["state"] == "interrupted" and closed1["by"] == "button"
        # frame order: flush.start, flush.end, tts.stop{interrupted}, turn.closed
        seq = [m["type"] for m in c.received if m.get("turn_id") == "c1" or m["type"].startswith("flush")]
        i_fs, i_fe = seq.index("flush.start"), seq.index("flush.end")
        i_stop = next(i for i, m in enumerate(c.received) if m["type"] == "tts.stop" and m.get("reason") == "interrupted")
        i_closed = next(i for i, m in enumerate(c.received) if m["type"] == "turn.closed" and m["turn_id"] == "c1")
        assert i_fs < i_fe and i_stop < i_closed
        # zero frames for c1 after flush.end
        n_after = len(c.frames.get("c1", [])) - frames_before
        await asyncio.sleep(0.4)
        assert len(c.frames.get("c1", [])) - frames_before == n_after  # nothing new arrived after the flush
        # second turn continues normally
        c.receipts = True
        c.speak(t2, 600)
        c.release(t2)
        closed2 = await c.wait_for(lambda m: m.get("type") == "turn.closed" and m.get("turn_id") == "c2", 8)
        assert closed2["state"] == "completed"
        await asyncio.sleep(0.2)
    finally:
        await r.stop()
    tr = {t["turn_id"]: t for t in r.traces()}
    assert tr["c1"]["turn_terminal"] == "interrupted" and tr["c1"]["interrupt_source"] == "button"
    assert tr["c1"]["generation"] == 1 and tr["c2"]["generation"] == 2  # abort + turn.start = one +1 (spec 02 §2.5)
    assert tr["c2"]["turn_terminal"] == "completed"


async def test_text_turn(tmp_project):
    r = await start(tmp_project("cascade"))
    try:
        c = r.client
        t = c.next_turn()
        c.text(t, "现在几点")
        closed = await c.wait_for(lambda m: m.get("type") == "turn.closed" and m.get("turn_id") == str(t), 8)
        assert closed["state"] == "completed"
        await asyncio.sleep(0.2)
    finally:
        await r.stop()
    tr = r.traces()[0]
    assert tr["turn_source"] == "text" and tr["turn_input"] == "typed"
    assert tr["user_text"] == "现在几点"
    assert tr["anchors"]["primary"] == "tap"


async def test_open_mic_vad_turn(tmp_project):
    r = await start(tmp_project("cascade", mode="open_mic"), aec="os")
    try:
        c = r.client
        assert c.hello_reply["mode"] == "open_mic"
        c.transport.client_send({"type": "listen.open", "mode": "open_mic", "reason": "wake"})
        # speech then silence: the gateway's endpointer starts and ends the turn (gateway id s<n>)
        c.speak(None, 1200, key_down=False)
        from vak.upstream.providers.mock import silence_pcm

        frame = 16000 * 20 // 1000 * 2
        pcm = silence_pcm(1500, 16000)
        from vak.transport.framing import UplinkHeader, pack_uplink

        for off in range(0, len(pcm), frame):
            c.seq += 1
            c.transport.client_send(pack_uplink(UplinkHeader(seq=c.seq, ts_samples=c.ts_samples, turn_id=None), pcm[off : off + frame]))
            c.ts_samples += frame // 2
        started = await c.wait_for(lambda m: m.get("type") == "turn.start" and m.get("source") == "vad", 5)
        assert started["turn_id"].startswith("s")
        await c.wait_for(lambda m: m.get("type") == "turn.end" and m.get("source") == "vad", 5)
        closed = await c.wait_for(lambda m: m.get("type") == "turn.closed" and m.get("turn_id") == started["turn_id"], 10)
        assert closed["state"] == "completed", closed
        await asyncio.sleep(0.2)
    finally:
        await r.stop()
    tr = r.traces()[0]
    assert tr["turn_source"] == "vad" and tr["anchors"]["primary"] == "endpoint"


async def test_downlink_frames_carry_turn_and_sentence(tmp_project):
    r = await start(tmp_project("s2s"))
    try:
        await _one_turn(r)
        raw = r.transport.sent_frames()
        assert raw
        for b in raw:
            h, pcm = unpack_downlink(b)
            assert str(h.turn_id) == "c1"
            assert len(pcm) == 16000 * 20 // 1000 * 2  # 20 ms pcm16 at the negotiated 16 kHz
    finally:
        await r.stop()


@pytest.mark.parametrize("pipeline", ["cascade", "half_cascade", "s2s"])
async def test_trace_records_first_audio_segments(tmp_project, pipeline):
    """spec 06 §2.4: seg.llm_ms always; first_segment_ms and tts_start_ms on text-then-TTS routes only."""
    r = await start(tmp_project(pipeline))
    try:
        await _one_turn(r)
        await asyncio.sleep(0.2)
    finally:
        await r.stop()
    seg = r.traces()[0]["latency"]["seg"]
    assert seg["llm_ms"] >= 0
    if pipeline == "s2s":
        assert "tts_start_ms" not in seg and "first_segment_ms" not in seg
    else:
        assert 0 <= seg["first_segment_ms"] <= seg["tts_start_ms"]


async def test_ptt_traces_pass_validation_and_feed_the_summary(tmp_project):
    """A tool turn then a chat turn in ptt without a gateway VAD: T_ae falls back to the release,
    pre_tool_ms is rebased to T0, chat turns are kind `chat` (spec 06 §2.2, §2.4, §3.3)."""
    from vak.eval.trace_check import check_records, summarize

    r = await start(tmp_project("cascade"))
    try:
        await _one_turn(r)
        await _one_turn(r)
        await asyncio.sleep(0.2)
    finally:
        await r.stop()
    recs = r.traces()
    report = check_records(recs)
    assert not report.invalid, report.invalid
    assert [x["turn_kind"] for x in recs] == ["tool_verbatim", "chat"]
    assert 0 <= recs[0]["latency"]["pre_tool_ms"] <= recs[0]["latency"]["tool_done_at_ms"]
    assert {s["segment"] for s in summarize(report.valid).segments} >= {"llm_ms", "tts_start_ms"}
