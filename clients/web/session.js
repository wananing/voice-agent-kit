// va/1 client session: WebSocket + mirror of the gateway's turn machine
// (spec 01 §2.1), two-level downlink gating (§2.1 R2.1-4, §5.4), playback
// receipts (§3.1.6), button / text interruption order (§5.1, §5.3), ping
// clock anchor (§3.1.8) and reconnect / resume with uplink retransmit (§6).
//
// The gateway holds the authoritative state. Everything here is either a
// fact we report (edges, receipts, abort) or a mirror used only to gate
// audio and drive the UI.

import * as P from "./protocol.js";
import { AudioEngine, DownlinkDecoder, UplinkEncoder, FRAME_SAMPLES, UP_RATE, opusSupported } from "./audio.js";

const OUTPUT_STATES = new Set(["responding", "draining"]);
const TERMINAL = new Set(["completed", "interrupted", "discarded", "failed", "cancelled"]);
const PREROLL_MS = 250;
const PREROLL_FRAMES = Math.ceil(PREROLL_MS / 20);
const ECHO_TAIL_SAMPLES = Math.round(0.2 * UP_RATE); // 150–300 ms after a barge-in press
const RETRANSMIT_RING = 150; // 3 s of 20 ms frames (spec asks ≥ 2 s)
const PING_MISS_LIMIT = 3;
const DEFAULT_TIMERS = { T_link_client: 15000, ping_interval: 5000, resume_window: 30000 };

export class Session {
  /**
   * @param {{profile:string, mode:string, forcePcm16:boolean}} opts
   * @param {(name:string, data:any)=>void} emit UI callback
   */
  constructor(opts, emit) {
    this.opts = opts;
    this.emit = emit;
    this.ws = null;
    this.url = null;
    this.userClosed = false;

    this.audio = new AudioEngine({
      onFrame: (pcm, ts) => this.onCaptureFrame(pcm, ts),
      onReceipt: (r) => this.onReceipt(r),
      onDropped: (reason) => {
        if (reason === "cap") this.stats.droppedCap++;
        else this.stats.droppedGen++;
      },
    });
    this.encoder = null;
    this.decoder = null;
    this.prerollRing = []; // last 250 ms of raw capture, sent ahead of a button turn
    this.upCodec = "pcm16";
    this.downCodecs = ["pcm16"];
    this.down = null; // { codec, rate }

    this.resetSessionState();

    this.stats = {
      rtt: null, txFrames: 0, rxFrames: 0, droppedCap: 0, droppedGen: 0, gated: 0,
      afterClosed: 0, afterFlushEnd: 0, protocolErrors: 0, reconnects: 0, lastClosed: null,
    };
  }

  resetSessionState() {
    this.sessionId = null;
    this.timers = { ...DEFAULT_TIMERS };
    this.leadMs = null;
    this.gatewayMode = null;
    this.turns = new Map(); // id -> { state, source, sentences: Map, lastSentenceId, closed, flushed, flushEnded, localStopped }
    this.currentTurn = null;
    this.clientTurnN = 0;
    this.txSeq = 0;
    this.rxSeq = 0;
    this.lastTxTs = 0;
    this.retransmitRing = [];
    this.pendingControl = [];
    this.flushes = new Map(); // flush_id -> turn_ids
    this.keyDown = false;
    this.listenOpened = false;
    this.echoTailUntil = 0;
    this.stopReason = null;
    this.pongMissed = 0;
    this.linkTimers = new Map();
    this.disconnectedAt = null;
    this.reconnectAttempt = 0;
  }

  // ------------------------------------------------------------ lifecycle

  async connect(url) {
    this.url = url;
    this.userClosed = false;
    await this.audio.start();
    if (!this.opts.forcePcm16 && (await opusSupported())) {
      this.upCodec = "opus";
      this.downCodecs = ["opus", "pcm16"];
    } else {
      this.upCodec = "pcm16";
      this.downCodecs = ["pcm16"];
    }
    this.openSocket();
  }

  openSocket() {
    this.log("info", `connecting ${this.url}`);
    const ws = new WebSocket(this.url);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => this.onOpen(ws);
    ws.onmessage = (e) => (typeof e.data === "string" ? this.onControl(e.data) : this.onAudioFrame(e.data));
    ws.onerror = () => this.log("warn", "websocket error");
    ws.onclose = (e) => this.onClose(ws, e);
    this.ws = ws;
  }

  async disconnect() {
    this.userClosed = true;
    this.stopTimers();
    if (this.ws) this.ws.close(1000, "user");
    this.ws = null;
    if (this.encoder) this.encoder.close();
    if (this.decoder) this.decoder.close();
    this.encoder = this.decoder = null;
    await this.audio.close();
    this.resetSessionState();
    this.refreshMirror();
    this.emit("connection", { connected: false });
  }

  onOpen(ws) {
    if (ws !== this.ws) return;
    const resume = this.sessionId
      ? {
          session_id: this.sessionId,
          last_tx_seq: this.txSeq - 1,
          last_rx_seq: this.rxSeq,
          current_turn: this.currentTurn,
          turn_state: this.currentTurn ? this.turns.get(this.currentTurn).state : "idle",
        }
      : null;
    this.helloTimer = setTimeout(() => {
      this.log("warn", "no hello reply within T_hello, reconnecting");
      ws.close();
    }, 10000);
    if (!this.encoder) this.encoder = new UplinkEncoder(this.upCodec, (bytes, ts) => this.onUplinkPacket(bytes, ts));
    this.sendNow(P.hello({ profile: this.opts.profile, client: { model: "web", fw: "0.1" }, uplinkCodec: this.upCodec, downlinkCodecs: this.downCodecs, resume }));
    this.log("info", resume ? `hello (resume ${resume.session_id}, tx ${resume.last_tx_seq}, rx ${resume.last_rx_seq})` : "hello");
  }

  onClose(ws, e) {
    if (ws !== this.ws) return;
    clearTimeout(this.helloTimer);
    clearInterval(this.pingTimer);
    this.log("warn", `socket closed (${e.code})`);
    this.emit("connection", { connected: false });
    if (this.userClosed) return;

    if (this.expired) {
      // session_expired: start a fresh session on a new socket (spec 01 §6.2)
      this.expired = false;
      this.openSocket();
      return;
    }
    if (!this.sessionId) {
      this.scheduleReconnect();
      return;
    }
    if (this.disconnectedAt == null) {
      this.disconnectedAt = performance.now();
      // Sentence mid-play at link loss: interrupted{reason:"link"}; the gateway
      // will not resend that audio (spec 01 §6.3). Receipt is queued and sent after resume.
      const t = this.turns.get(this.currentTurn);
      if (t && OUTPUT_STATES.has(t.state)) {
        t.localStopped = true;
        this.stopReason = "link";
        this.audio.stopPlayback();
      }
    }
    if (performance.now() - this.disconnectedAt > this.timers.resume_window) {
      this.log("warn", "resume window passed, starting a new session");
      this.failOpenTurnsLocally();
      this.resetSessionState();
      this.refreshMirror();
    }
    this.scheduleReconnect();
  }

  scheduleReconnect() {
    const delay = Math.min(5000, 500 * 2 ** this.reconnectAttempt);
    this.reconnectAttempt++;
    this.stats.reconnects++;
    this.log("info", `reconnect in ${delay} ms`);
    this.reconnectTimer = setTimeout(() => this.openSocket(), delay);
  }

  /** R6.3: outside the resume window every open turn is locally failed{link_lost}. */
  failOpenTurnsLocally() {
    for (const [id, t] of this.turns) {
      if (!TERMINAL.has(t.state)) {
        t.state = "failed";
        t.closed = true;
        this.emit("turn.closed", { turn_id: id, state: "failed", by: "link_lost", local: true });
      }
    }
  }

  stopTimers() {
    clearTimeout(this.helloTimer);
    clearTimeout(this.reconnectTimer);
    clearInterval(this.pingTimer);
    for (const t of this.linkTimers.values()) clearTimeout(t);
    this.linkTimers.clear();
  }

  // ------------------------------------------------------------- sending

  sendNow(obj) {
    this.ws.send(JSON.stringify(obj));
    this.emit("tx", obj);
  }

  /** Control message; facts are queued while the link is down and sent after resume. */
  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN && this.sessionId) {
      this.sendNow(obj);
    } else if (obj.type !== "ping") {
      this.pendingControl.push(obj);
    }
  }

  // ------------------------------------------------------------- uplink

  get uplinkActive() {
    return this.keyDown || (this.opts.mode === "open_mic" && this.listenOpened);
  }

  onCaptureFrame(pcm, ts) {
    this.prerollRing.push({ pcm, ts });
    if (this.prerollRing.length > PREROLL_FRAMES) this.prerollRing.shift();
    if (!this.uplinkActive || !this.encoder) return;
    this.encoder.encode(pcm, ts);
  }

  /** Called per encoded 20 ms packet (sync for pcm16, async for Opus). */
  onUplinkPacket(bytes, tsSamples) {
    const t = this.turns.get(this.currentTurn);
    const turnId = t && t.state === "listening" ? this.currentTurn : null; // R2.2-5: hint only
    let flags = 0;
    if (this.keyDown) flags |= P.UP_FLAG_KEY_DOWN;
    if (this.echoTailUntil && tsSamples < this.echoTailUntil) flags |= P.UP_FLAG_ECHO_TAIL;
    const frame = P.encodeUplinkFrame({ seq: this.txSeq, tsSamples, turnId, flags }, bytes);
    this.txSeq++;
    this.lastTxTs = tsSamples;
    this.retransmitRing.push({ seq: this.txSeq - 1, frame });
    if (this.retransmitRing.length > RETRANSMIT_RING) this.retransmitRing.shift();
    if (this.ws && this.ws.readyState === WebSocket.OPEN && this.sessionId) {
      this.ws.send(frame);
      this.stats.txFrames++;
    }
  }

  retransmitAfter(lastRxSeq) {
    let n = 0;
    for (const { seq, frame } of this.retransmitRing) {
      if (seq <= lastRxSeq) continue;
      const copy = frame.slice();
      copy[2] |= P.UP_FLAG_RETRANSMIT;
      this.ws.send(copy);
      n++;
    }
    this.log("info", `retransmitted ${n} frames after seq ${lastRxSeq}`);
  }

  // ------------------------------------------------------- user actions

  nowSamples() {
    return (this.audio.lastTsSamples + FRAME_SAMPLES) >>> 0;
  }

  newClientTurn() {
    this.clientTurnN++;
    const id = `c${this.clientTurnN}`;
    this.turns.set(id, this.newTurn("button"));
    return id;
  }

  newTurn(source, state = "listening") {
    return { state, source, sentences: new Map(), lastSentenceId: null, closed: false, flushed: false, flushEnded: false, localStopped: false };
  }

  /** Stop local playback for a barge-in; resolves after abort was sent (if anything was playing). */
  async bargeIn(reason) {
    const cur = this.currentTurn;
    const t = this.turns.get(cur);
    if (!t || !OUTPUT_STATES.has(t.state)) return false;
    t.localStopped = true; // first gate: no more frames of this turn into the queue
    this.stopReason = reason;
    // ① stop + play_gen += 1; the worklet's `interrupted` arrives (and is sent as ②)
    // before this resolves, so ②③④ go out in order and in one task.
    await this.audio.stopPlayback();
    this.send(P.abort(cur, reason)); // ③
    return true;
  }

  async pressStart() {
    if (this.keyDown || !this.audio.ctx) return;
    this.keyDown = true;
    const ts = this.nowSamples();
    const interrupted = await this.bargeIn("button");
    if (interrupted) this.echoTailUntil = (ts + ECHO_TAIL_SAMPLES) >>> 0;
    if (!this.keyDown) return; // released during the await
    const id = this.newClientTurn();
    this.currentTurn = id;
    this.refreshMirror();
    this.send(P.turnStartButton(id, ts, PREROLL_MS)); // ④
    // Preroll: the 250 ms captured before the press, stamped before `ts`.
    for (const f of this.prerollRing) this.encoder.encode(f.pcm, f.ts);
    this.emit("turn.local", { turn_id: id, source: "button" });
  }

  async pressEnd() {
    if (!this.keyDown) return;
    this.keyDown = false;
    const ts = this.nowSamples();
    const id = this.currentTurn;
    const t = this.turns.get(id);
    await this.encoder.flush(); // no audio frame trails turn.end
    if (t && t.state === "listening" && t.source === "button") {
      this.send(P.turnEnd(id, ts, "release"));
      t.state = "committed";
      this.refreshMirror();
      this.armLinkTimer(id);
    }
  }

  async sendText(text) {
    if (!text || !this.sessionId) return;
    await this.bargeIn("text");
    const id = this.newClientTurn();
    const t = this.turns.get(id);
    t.source = "text";
    t.state = "committed"; // text implies turn.end
    this.currentTurn = id;
    this.refreshMirror();
    this.send(P.turnStartText(id, text, "typed"));
    this.armLinkTimer(id);
    this.emit("turn.local", { turn_id: id, source: "text", text });
  }

  listenOpen() {
    this.listenOpened = true;
    this.send(P.listenOpen(this.opts.mode, "user"));
  }

  listenClose() {
    this.listenOpened = false;
    this.send(P.listenClose("user"));
  }

  // ------------------------------------------------------------ receipts

  onReceipt(r) {
    const tsMs = Math.round(performance.now());
    if (r.type === "interrupted") {
      this.send(P.playbackReceipt("interrupted", r.turnId, r.sentenceId, r.playedMs, tsMs, this.stopReason || "flush")); // ②
    } else {
      this.send(P.playbackReceipt(r.type, r.turnId, r.sentenceId, r.playedMs, tsMs));
    }
    const t = this.turns.get(r.turnId);
    const s = t && t.sentences.get(r.sentenceId);
    this.emit(`sentence.${r.type}`, { turn_id: r.turnId, sentence_id: r.sentenceId, played_ms: r.playedMs, text: s ? s.text : "" });
  }

  // ------------------------------------------------------- downlink audio

  onAudioFrame(buf) {
    const f = P.decodeFrame(buf);
    if (!f || f.kind !== P.KIND_DOWN) {
      this.stats.protocolErrors++;
      return;
    }
    this.rxSeq = f.seq;
    this.stats.rxFrames++;
    const t = this.turns.get(f.turnId);
    if (t && t.closed) {
      this.stats.afterClosed++; // spec 01 §8 item 2
      return;
    }
    if (t && t.flushEnded) {
      this.stats.afterFlushEnd++; // spec 01 §8 item 9
      return;
    }
    // First gate (R2.1-4): current turn, mirror in responding/draining, not locally stopped.
    if (!t || f.turnId !== this.currentTurn || !OUTPUT_STATES.has(t.state) || t.localStopped) {
      this.stats.gated++;
      return;
    }
    if (!this.decoder) return;
    t.lastSentenceId = f.sentenceId;
    const s = t.sentences.get(f.sentenceId);
    const meta = {
      turnId: f.turnId,
      sentenceId: f.sentenceId,
      playGen: this.audio.playGen, // tagged at decode time (second gate, §5.4)
      uninterruptible: f.uninterruptible || (s ? s.interruptible === false : false),
    };
    this.decoder.decode(f.payload, meta);
  }

  // ----------------------------------------------------- downlink control

  onControl(text) {
    const m = P.parseMessage(text);
    if (!m) {
      this.stats.protocolErrors++;
      return;
    }
    this.emit("rx", m);
    const h = this.handlers[m.type];
    if (h) h.call(this, m);
    // unknown types are ignored (spec 01 §3.0)
  }

  handlers = {
    hello(m) {
      clearTimeout(this.helloTimer);
      this.reconnectAttempt = 0;
      this.disconnectedAt = null;
      const resumed = !!this.sessionId;
      this.sessionId = m.session_id;
      this.timers = { ...DEFAULT_TIMERS, ...(m.timers || {}) };
      this.leadMs = m.lead_ms ?? null;
      this.gatewayMode = m.mode ?? null;
      if (m.audio_down) this.setDownlink(m.audio_down.codec, m.audio_down.rate);
      if (m.audio_up && m.audio_up.codec && m.audio_up.codec !== this.upCodec) {
        // Non-spec extension: gateway tells us which uplink codec it will accept.
        this.upCodec = m.audio_up.codec;
        this.encoder.close();
        this.encoder = new UplinkEncoder(this.upCodec, (b, ts) => this.onUplinkPacket(b, ts));
      }
      this.log("info", `session ${this.sessionId} mode=${m.mode} down=${this.down ? `${this.down.codec}@${this.down.rate}` : '-'} lead=${this.leadMs}ms next_server_turn=${m.next_server_turn}`);
      if (resumed && m.resume) {
        if (m.resume.ok) {
          for (const t of m.resume.turns || []) {
            const local = this.turns.get(t.turn_id);
            if (local && !TERMINAL.has(t.state)) local.state = t.state; // gateway is authoritative (R6.3-2)
          }
          this.retransmitAfter(m.resume.last_rx_seq ?? -1);
        } else {
          this.log("warn", "resume rejected");
        }
      }
      const pending = this.pendingControl;
      this.pendingControl = [];
      for (const obj of pending) this.sendNow(obj);
      this.startPing();
      this.refreshMirror();
      this.emit("connection", { connected: true, sessionId: this.sessionId, mode: this.gatewayMode, down: this.down, up: this.upCodec, leadMs: this.leadMs, timers: this.timers, nextServerTurn: m.next_server_turn });
    },

    pong(m) {
      this.pongMissed = 0;
      this.stats.rtt = Math.round(performance.now() - m.client_mono_ms);
    },

    stt(m) {
      this.disarmLinkTimer(m.turn_id);
      this.emit("stt", m);
    },

    "turn.start"(m) {
      // Gateway-detected boundaries (vad) and event narration turns (§3.2.6).
      const state = m.source === "event" ? "committed" : "listening";
      this.turns.set(m.turn_id, this.newTurn(m.source, state));
      this.currentTurn = m.turn_id;
      this.refreshMirror();
    },

    "turn.end"(m) {
      const t = this.turns.get(m.turn_id);
      if (t && t.state === "listening") {
        t.state = "committed";
        this.refreshMirror();
      }
    },

    "turn.merge"(m) {
      const into = this.turns.get(m.into) || this.newTurn("button");
      this.turns.set(m.into, into);
      this.turns.delete(m.from);
      into.state = this.keyDown ? "listening" : "committed";
      if (this.currentTurn === m.from) this.currentTurn = m.into;
      this.refreshMirror();
    },

    "tts.start"(m) {
      let t = this.turns.get(m.turn_id);
      if (!t) {
        t = this.newTurn("event", "committed");
        this.turns.set(m.turn_id, t);
      }
      if (this.currentTurn == null || TERMINAL.has(this.turns.get(this.currentTurn)?.state ?? "completed")) this.currentTurn = m.turn_id;
      if (m.turn_id === this.currentTurn && !TERMINAL.has(t.state)) t.state = "responding";
      if (m.codec && m.rate && (!this.down || m.codec !== this.down.codec || m.rate !== this.down.rate)) this.setDownlink(m.codec, m.rate);
      this.refreshMirror();
    },

    "tts.sentence_start"(m) {
      const t = this.turns.get(m.turn_id);
      if (!t) return;
      t.sentences.set(m.sentence_id, { text: m.text ?? "", interruptible: m.interruptible !== false, offset_ms: m.offset_ms ?? null });
    },

    "tts.stop"(m) {
      const t = this.turns.get(m.turn_id);
      if (!t) return;
      if (m.reason === "done" && t.state === "responding") {
        t.state = "draining";
        this.refreshMirror();
        // Let the last sentence's `ended` fire once every pending frame has been decoded.
        const last = t.lastSentenceId;
        if (last != null) this.decoder.flush().then(() => this.audio.endOfSentence(m.turn_id, last));
      }
    },

    "flush.start"(m) {
      const ids = m.turn_ids || [];
      this.flushes.set(m.flush_id, ids);
      let stop = false;
      for (const id of ids) {
        const t = this.turns.get(id);
        if (!t) continue;
        t.flushed = true;
        if (OUTPUT_STATES.has(t.state) && !t.localStopped) {
          t.localStopped = true;
          stop = true;
        }
      }
      if (stop) {
        // Gateway-initiated (vad / cancel) or our own abort being acked: drop
        // buffered audio for these turns, keep uninterruptible sentences.
        this.stopReason = m.ack_of === "abort" ? this.stopReason : m.reason || "flush";
        this.audio.stopPlayback();
      }
    },

    "flush.end"(m) {
      for (const id of this.flushes.get(m.flush_id) || []) {
        const t = this.turns.get(id);
        if (t) t.flushEnded = true;
      }
      this.flushes.delete(m.flush_id);
    },

    error(m) {
      this.log("error", `error ${m.code}${m.turn_id ? ` (${m.turn_id})` : ""}: ${m.message ?? ""}`);
      if (m.code === "session_expired" || (m.fatal && m.turn_id == null)) {
        this.log("warn", "session expired: dropping all turn state, new session");
        this.failOpenTurnsLocally();
        this.stopTimers();
        this.resetSessionState();
        this.refreshMirror();
        this.expired = true;
        this.ws.close();
      }
    },

    "turn.closed"(m) {
      const t = this.turns.get(m.turn_id);
      if (t) {
        t.state = m.state;
        t.closed = true;
      }
      this.disarmLinkTimer(m.turn_id);
      this.stats.lastClosed = { turn_id: m.turn_id, state: m.state, by: m.by ?? null, heard_ms: m.heard_ms ?? null };
      if (this.currentTurn === m.turn_id) this.currentTurn = null;
      this.refreshMirror();
      this.emit("turn.closed", m);
    },

    cmd(m) {
      this.send(P.cmdResult(m.id, false, "unsupported")); // caps.commands is empty
    },
  };

  setDownlink(codec, rate) {
    if (this.decoder) this.decoder.close();
    this.down = { codec, rate };
    this.decoder = new DownlinkDecoder(codec, rate, (pcm, sr, meta) => this.audio.enqueue(pcm, sr, meta));
  }

  // ---------------------------------------------------- timers / mirror

  startPing() {
    clearInterval(this.pingTimer);
    this.pongMissed = 0;
    this.pingTimer = setInterval(() => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      if (this.pongMissed >= PING_MISS_LIMIT) {
        this.log("warn", `${this.pongMissed} pings unanswered, reconnecting`);
        this.ws.close();
        return;
      }
      this.pongMissed++;
      this.sendNow(P.ping(Math.round(performance.now()), this.lastTxTs, this.audio.playedSamplesUp()));
    }, this.timers.ping_interval);
  }

  /** T_link_client: commit → first stt echo for this turn (spec 01 §2.5). */
  armLinkTimer(turnId) {
    this.disarmLinkTimer(turnId);
    this.linkTimers.set(
      turnId,
      setTimeout(() => {
        this.linkTimers.delete(turnId);
        const t = this.turns.get(turnId);
        if (t && !t.closed && this.ws) {
          this.log("warn", `no stt for ${turnId} within T_link_client, reconnecting`);
          this.ws.close();
        }
      }, this.timers.T_link_client),
    );
  }

  disarmLinkTimer(turnId) {
    const h = this.linkTimers.get(turnId);
    if (h) {
      clearTimeout(h);
      this.linkTimers.delete(turnId);
    }
  }

  get mirrorState() {
    const t = this.turns.get(this.currentTurn);
    return t && !TERMINAL.has(t.state) ? t.state : "idle";
  }

  refreshMirror() {
    this.emit("state", { state: this.mirrorState, turn_id: this.currentTurn });
  }

  log(level, text) {
    this.emit("log", { level, text });
  }

  /** Snapshot for the stats panel. */
  snapshot() {
    return {
      ...this.stats,
      state: this.mirrorState,
      turn: this.currentTurn,
      sessionId: this.sessionId,
      up: this.upCodec,
      down: this.down,
      leadMs: this.leadMs,
      gatewayMode: this.gatewayMode,
      bufferMs: this.audio.bufferedMs(),
      underruns: this.audio.stats.underruns,
      playGen: this.audio.playGen,
      txSeq: this.txSeq,
      rxSeq: this.rxSeq,
      ctxRate: this.audio.rate,
    };
  }
}
