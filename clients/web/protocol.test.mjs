// Run: node --test clients/web/protocol.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import * as P from "./protocol.js";

test("turn id u32 encoding: client, gateway (MSB), none", () => {
  assert.equal(P.encodeTurnId("c42"), 42);
  assert.equal(P.encodeTurnId("s17"), 0x80000011);
  assert.equal(P.encodeTurnId("s0"), 0x80000000);
  assert.equal(P.encodeTurnId(null), 0);
  assert.equal(P.encodeTurnId(""), 0);
  assert.equal(P.decodeTurnId(42), "c42");
  assert.equal(P.decodeTurnId(0x80000011), "s17");
  assert.equal(P.decodeTurnId(0), null);
  // max 31-bit n round-trips on both sides
  assert.equal(P.decodeTurnId(P.encodeTurnId("c2147483647")), "c2147483647");
  assert.equal(P.decodeTurnId(P.encodeTurnId("s2147483647")), "s2147483647");
  assert.throws(() => P.encodeTurnId("x1"));
  assert.throws(() => P.encodeTurnId("c-1"));
  assert.throws(() => P.encodeTurnId("c2147483648"));
});

test("uplink frame round-trip, big-endian layout", () => {
  const payload = new Uint8Array([0xaa, 0xbb, 0xcc]);
  const f = P.encodeUplinkFrame(
    { seq: 0x01020304, tsSamples: 1843200, turnId: "c42", flags: P.UP_FLAG_KEY_DOWN | P.UP_FLAG_RETRANSMIT },
    payload,
  );
  assert.equal(f.byteLength, P.FRAME_HEADER_BYTES + 3);
  // byte layout
  assert.deepEqual(Array.from(f.subarray(0, 8)), [1, 0x01, 0x05, 0, 0x01, 0x02, 0x03, 0x04]);
  assert.deepEqual(Array.from(f.subarray(8, 12)), [0x00, 0x1c, 0x20, 0x00]); // 1843200
  assert.deepEqual(Array.from(f.subarray(12, 16)), [0, 0, 0, 42]);
  const d = P.decodeFrame(f);
  assert.equal(d.kind, P.KIND_UP);
  assert.equal(d.seq, 0x01020304);
  assert.equal(d.tsSamples, 1843200);
  assert.equal(d.turnId, "c42");
  assert.equal(d.keyDown, true);
  assert.equal(d.echoTail, false);
  assert.equal(d.retransmit, true);
  assert.deepEqual(Array.from(d.payload), [0xaa, 0xbb, 0xcc]);
});

test("uplink frame with no turn (open mic outside a turn) encodes turn_id 0", () => {
  const f = P.encodeUplinkFrame({ seq: 1, tsSamples: 320, turnId: null }, new Uint8Array(0));
  assert.deepEqual(Array.from(f.subarray(12, 16)), [0, 0, 0, 0]);
  assert.equal(P.decodeFrame(f).turnId, null);
});

test("downlink frame round-trip incl. gateway turn id and uninterruptible flag", () => {
  const payload = new Uint8Array(640);
  payload[0] = 7;
  const f = P.encodeDownlinkFrame(
    { seq: 9021, turnId: "s17", sentenceId: 3, frameIdx: 65535, flags: P.DOWN_FLAG_UNINTERRUPTIBLE },
    payload,
  );
  assert.deepEqual(Array.from(f.subarray(0, 4)), [1, 0x02, 0x01, 0]);
  assert.deepEqual(Array.from(f.subarray(8, 12)), [0x80, 0, 0, 0x11]);
  assert.deepEqual(Array.from(f.subarray(12, 16)), [0, 3, 0xff, 0xff]);
  const d = P.decodeFrame(f.buffer); // ArrayBuffer input, as from WebSocket
  assert.equal(d.kind, P.KIND_DOWN);
  assert.equal(d.seq, 9021);
  assert.equal(d.turnId, "s17");
  assert.equal(d.sentenceId, 3);
  assert.equal(d.frameIdx, 65535);
  assert.equal(d.uninterruptible, true);
  assert.equal(d.payload.byteLength, 640);
  assert.equal(d.payload[0], 7);
});

test("decodeFrame rejects short, wrong-version and unknown-kind frames", () => {
  assert.equal(P.decodeFrame(new Uint8Array(15)), null);
  const bad = P.encodeUplinkFrame({ seq: 1, tsSamples: 0, turnId: "c1" }, new Uint8Array(0));
  bad[0] = 2;
  assert.equal(P.decodeFrame(bad), null);
  const unk = P.encodeUplinkFrame({ seq: 1, tsSamples: 0, turnId: "c1" }, new Uint8Array(0));
  unk[1] = 0x03;
  assert.equal(P.decodeFrame(unk), null);
});

test("hello carries the spec field names", () => {
  const m = P.hello({
    profile: "B",
    client: { model: "web", fw: "0.1" },
    uplinkCodec: "opus",
    downlinkCodecs: ["opus", "pcm16"],
  });
  assert.equal(m.type, "hello");
  assert.equal(m.proto, "va/1");
  assert.equal(m.profile, "B");
  assert.deepEqual(m.client, { model: "web", fw: "0.1" });
  assert.equal(m.caps.transport, "ws");
  assert.equal(m.caps.aec, "os");
  assert.deepEqual(m.caps.modes, ["ptt", "open_mic"]);
  assert.deepEqual(m.caps.sources, ["button", "text"]);
  assert.deepEqual(m.caps.receipts, ["started", "ended", "interrupted"]);
  assert.deepEqual(m.caps.audio_up, { codec: "opus", rate: 16000, channels: 1, frame_ms: 20 });
  assert.deepEqual(m.caps.audio_down, { codecs: ["opus", "pcm16"], rates: [16000, 24000] });
  assert.equal(m.caps.play_buffer_max_ms, 1200);
  assert.equal(m.caps.preroll_ms, 250);
  assert.equal(m.caps.uninterruptible, true);
  assert.deepEqual(m.caps.events, []);
  assert.deepEqual(m.caps.commands, []);
  assert.equal(m.resume, null);

  const r = P.hello({
    profile: "A",
    client: { model: "web", fw: "0.1" },
    uplinkCodec: "pcm16",
    downlinkCodecs: ["pcm16"],
    resume: { session_id: "S-1", last_tx_seq: 10, last_rx_seq: 20, current_turn: "c3", turn_state: "listening" },
  });
  assert.deepEqual(r.resume, { session_id: "S-1", last_tx_seq: 10, last_rx_seq: 20, current_turn: "c3", turn_state: "listening" });
});

test("turn / listen / abort builders", () => {
  assert.deepEqual(P.turnStartButton("c42", 1843200, 250), {
    type: "turn.start", turn_id: "c42", source: "button", ts_samples: 1843200, preroll_ms: 250,
  });
  assert.deepEqual(P.turnEnd("c42", 1891200), {
    type: "turn.end", turn_id: "c42", source: "button", ts_samples: 1891200, reason: "release",
  });
  assert.deepEqual(P.turnStartText("c43", "明天几点出发"), {
    type: "turn.start", turn_id: "c43", source: "text", text: "明天几点出发", input: "typed",
  });
  assert.deepEqual(P.abort("c41", "button"), { type: "abort", turn_id: "c41", reason: "button" });
  assert.deepEqual(P.listenOpen("open_mic", "wake"), { type: "listen.open", mode: "open_mic", reason: "wake" });
  assert.deepEqual(P.listenClose("idle_timeout"), { type: "listen.close", reason: "idle_timeout" });
});

test("receipts, ping, cmd.result", () => {
  assert.deepEqual(P.playbackReceipt("started", "c41", 0, 0, 51234), {
    type: "playback.started", turn_id: "c41", sentence_id: 0, played_ms: 0, ts_ms: 51234,
  });
  assert.deepEqual(P.playbackReceipt("ended", "c41", 2, 1830, 55012), {
    type: "playback.ended", turn_id: "c41", sentence_id: 2, played_ms: 1830, ts_ms: 55012,
  });
  assert.deepEqual(P.playbackReceipt("interrupted", "c41", 1, 640, 52890, "button"), {
    type: "playback.interrupted", turn_id: "c41", sentence_id: 1, played_ms: 640, ts_ms: 52890, reason: "button",
  });
  assert.equal("reason" in P.playbackReceipt("ended", "c41", 2, 1830, 55012), false);
  assert.deepEqual(P.ping(61000, 976000, 412800), {
    type: "ping", client_mono_ms: 61000, ts_samples_up: 976000, ts_samples_played: 412800,
  });
  assert.deepEqual(P.cmdResult("cmd-310", true), { type: "cmd.result", id: "cmd-310", ok: true });
  assert.deepEqual(P.cmdResult("cmd-310", false, "unsupported"), { type: "cmd.result", id: "cmd-310", ok: false, error: "unsupported" });
});

test("parseMessage ignores garbage and typeless objects", () => {
  assert.equal(P.parseMessage("not json"), null);
  assert.equal(P.parseMessage("[1,2]"), null);
  assert.equal(P.parseMessage('{"foo":1}'), null);
  assert.deepEqual(P.parseMessage('{"type":"pong","client_mono_ms":1}'), { type: "pong", client_mono_ms: 1 });
});
