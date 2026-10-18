// va/1 wire protocol: JSON message builders/parsers and the 16-byte audio
// frame header (spec 01 §3). Pure functions, no DOM or Web Audio, so this
// module runs under Node for tests and can be ported to firmware as-is.
//
// Frame header, 16 bytes, big-endian (docs/protocol.md):
//   0  ver        u8   = 1
//   1  kind       u8   0x01 uplink / 0x02 downlink
//   2  flags      u8
//   3  reserved   u8   = 0
//   4  seq        u32
//   uplink:   8 ts_samples u32, 12 turn_id u32
//   downlink: 8 turn_id u32, 12 sentence_id u16, 14 frame_idx u16
// turn_id u32: MSB set = gateway `s<n>`, clear = client `c<n>`, 0 = no turn.

export const PROTO = "va/1";
export const FRAME_HEADER_BYTES = 16;
export const FRAME_VERSION = 1;
export const KIND_UP = 0x01;
export const KIND_DOWN = 0x02;

// Uplink flags (spec 01 §3.1.2).
export const UP_FLAG_KEY_DOWN = 0x01;
export const UP_FLAG_ECHO_TAIL = 0x02;
export const UP_FLAG_RETRANSMIT = 0x04;
// Downlink flags (spec 01 §3.2.1).
export const DOWN_FLAG_UNINTERRUPTIBLE = 0x01;

const MSB = 0x80000000;
const LOW31 = 0x7fffffff;

// ---------------------------------------------------------------- turn ids

/** "c42" -> 42, "s17" -> 0x80000011, null/undefined/"" -> 0. */
export function encodeTurnId(text) {
  if (text == null || text === "") return 0;
  if (typeof text !== "string" || text.length < 2) throw new Error(`bad turn id: ${text}`);
  const side = text[0];
  const n = Number(text.slice(1));
  if ((side !== "c" && side !== "s") || !Number.isInteger(n) || n < 0 || n > LOW31) {
    throw new Error(`bad turn id: ${text}`);
  }
  return side === "s" ? (n | MSB) >>> 0 : n;
}

/** u32 -> "c42" / "s17"; 0 -> null (no turn). */
export function decodeTurnId(u32) {
  if (u32 === 0) return null;
  const side = (u32 & MSB) !== 0 ? "s" : "c";
  return side + (u32 & LOW31);
}

// ------------------------------------------------------------ frame codec

function writeHeader(view, kind, flags, seq) {
  view.setUint8(0, FRAME_VERSION);
  view.setUint8(1, kind);
  view.setUint8(2, flags & 0xff);
  view.setUint8(3, 0);
  view.setUint32(4, seq >>> 0, false);
}

/**
 * Build an uplink frame: header + payload.
 * @param {{seq:number, tsSamples:number, turnId:string|null, flags?:number}} h
 * @param {Uint8Array} payload encoded audio (Opus packet or pcm16le)
 * @returns {Uint8Array}
 */
export function encodeUplinkFrame(h, payload) {
  const out = new Uint8Array(FRAME_HEADER_BYTES + payload.byteLength);
  const view = new DataView(out.buffer);
  writeHeader(view, KIND_UP, h.flags ?? 0, h.seq);
  view.setUint32(8, h.tsSamples >>> 0, false);
  view.setUint32(12, encodeTurnId(h.turnId), false);
  out.set(payload, FRAME_HEADER_BYTES);
  return out;
}

/**
 * Build a downlink frame (used by tests and by anything that fakes a gateway).
 * @param {{seq:number, turnId:string|null, sentenceId:number, frameIdx:number, flags?:number}} h
 * @param {Uint8Array} payload
 */
export function encodeDownlinkFrame(h, payload) {
  const out = new Uint8Array(FRAME_HEADER_BYTES + payload.byteLength);
  const view = new DataView(out.buffer);
  writeHeader(view, KIND_DOWN, h.flags ?? 0, h.seq);
  view.setUint32(8, encodeTurnId(h.turnId), false);
  view.setUint16(12, h.sentenceId & 0xffff, false);
  view.setUint16(14, h.frameIdx & 0xffff, false);
  out.set(payload, FRAME_HEADER_BYTES);
  return out;
}

/**
 * Parse any va/1 frame. Returns null for a malformed frame.
 * @param {ArrayBuffer|Uint8Array} buf
 * @returns {null | {kind:number, flags:number, seq:number, payload:Uint8Array,
 *   tsSamples?:number, turnId:string|null, sentenceId?:number, frameIdx?:number,
 *   keyDown?:boolean, echoTail?:boolean, retransmit?:boolean, uninterruptible?:boolean}}
 */
export function decodeFrame(buf) {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  if (bytes.byteLength < FRAME_HEADER_BYTES) return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (view.getUint8(0) !== FRAME_VERSION) return null;
  const kind = view.getUint8(1);
  const flags = view.getUint8(2);
  const seq = view.getUint32(4, false);
  const payload = bytes.subarray(FRAME_HEADER_BYTES);
  if (kind === KIND_UP) {
    return {
      kind, flags, seq, payload,
      tsSamples: view.getUint32(8, false),
      turnId: decodeTurnId(view.getUint32(12, false)),
      keyDown: (flags & UP_FLAG_KEY_DOWN) !== 0,
      echoTail: (flags & UP_FLAG_ECHO_TAIL) !== 0,
      retransmit: (flags & UP_FLAG_RETRANSMIT) !== 0,
    };
  }
  if (kind === KIND_DOWN) {
    return {
      kind, flags, seq, payload,
      turnId: decodeTurnId(view.getUint32(8, false)),
      sentenceId: view.getUint16(12, false),
      frameIdx: view.getUint16(14, false),
      uninterruptible: (flags & DOWN_FLAG_UNINTERRUPTIBLE) !== 0,
    };
  }
  return null;
}

// ------------------------------------------------------- control messages

/** Parse a JSON control message; null if not an object with a string `type`. */
export function parseMessage(text) {
  let m;
  try {
    m = JSON.parse(text);
  } catch {
    return null;
  }
  if (!m || typeof m !== "object" || typeof m.type !== "string") return null;
  return m;
}

/**
 * `hello` (spec 01 §3.1.1). `resume` is null for a fresh session or
 * {session_id, last_tx_seq, last_rx_seq, current_turn, turn_state}.
 */
export function hello({ profile, client, uplinkCodec, downlinkCodecs, resume = null }) {
  return {
    type: "hello",
    proto: PROTO,
    profile,
    client,
    caps: {
      transport: "ws",
      aec: "os",
      modes: ["ptt", "open_mic"],
      sources: ["button", "text"],
      receipts: ["started", "ended", "interrupted"],
      audio_up: { codec: uplinkCodec, rate: 16000, channels: 1, frame_ms: 20 },
      audio_down: { codecs: downlinkCodecs, rates: [16000, 24000] },
      play_buffer_max_ms: 1200,
      preroll_ms: 250,
      uninterruptible: true,
      events: [],
      commands: [],
    },
    resume,
  };
}

export function listenOpen(mode, reason = "user") {
  return { type: "listen.open", mode, reason };
}

export function listenClose(reason = "user") {
  return { type: "listen.close", reason };
}

/** key-down (spec 01 §3.1.4). ts_samples = press moment, preroll excluded. */
export function turnStartButton(turnId, tsSamples, prerollMs) {
  return { type: "turn.start", turn_id: turnId, source: "button", ts_samples: tsSamples, preroll_ms: prerollMs };
}

/** Text input; the same frame implies turn.end. */
export function turnStartText(turnId, text, input = "typed") {
  return { type: "turn.start", turn_id: turnId, source: "text", text, input };
}

/** key-up. reason: release / max_duration / cancel. */
export function turnEnd(turnId, tsSamples, reason = "release") {
  return { type: "turn.end", turn_id: turnId, source: "button", ts_samples: tsSamples, reason };
}

/** reason: button / wake / text / user_cancel (spec 01 §3.1.5). */
export function abort(turnId, reason) {
  return { type: "abort", turn_id: turnId, reason };
}

/**
 * Playback receipt (spec 01 §3.1.6). kind: started / ended / interrupted.
 * `reason` only for interrupted (button / text / vad / link / flush).
 */
export function playbackReceipt(kind, turnId, sentenceId, playedMs, tsMs, reason) {
  const m = { type: `playback.${kind}`, turn_id: turnId, sentence_id: sentenceId, played_ms: playedMs, ts_ms: tsMs };
  if (kind === "interrupted") m.reason = reason;
  return m;
}

/** Heartbeat and clock anchor (spec 01 §3.1.8). */
export function ping(clientMonoMs, tsSamplesUp, tsSamplesPlayed) {
  return { type: "ping", client_mono_ms: clientMonoMs, ts_samples_up: tsSamplesUp, ts_samples_played: tsSamplesPlayed };
}

/** Reply to a gateway `cmd` (spec 01 §3.2.7). */
export function cmdResult(id, ok, error) {
  const m = { type: "cmd.result", id, ok };
  if (!ok && error) m.error = error;
  return m;
}
