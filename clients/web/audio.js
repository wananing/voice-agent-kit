// Browser audio for the va/1 reference client (spec 01 §4, handbook
// audio-preprocessing): capture via AudioWorklet at 16 kHz mono / 20 ms with
// echoCancellation + noiseSuppression + autoGainControl on, playback via an
// AudioWorklet ring queue that reports DAC-consumed samples, and the two
// payload codecs (pcm16 little-endian, Opus through WebCodecs when present).
//
// This module knows nothing about turns or the WebSocket; session.js wires
// its callbacks to the protocol.

export const UP_RATE = 16000;
export const FRAME_MS = 20;
export const FRAME_SAMPLES = 320;

// ------------------------------------------------------------------ codecs

/** True when WebCodecs can encode and decode Opus at the rates we use. */
export async function opusSupported() {
  if (typeof AudioEncoder === "undefined" || typeof AudioDecoder === "undefined") return false;
  try {
    const enc = await AudioEncoder.isConfigSupported(opusEncoderConfig());
    const dec16 = await AudioDecoder.isConfigSupported({ codec: "opus", sampleRate: 16000, numberOfChannels: 1 });
    return !!(enc.supported && dec16.supported);
  } catch {
    return false;
  }
}

function opusEncoderConfig() {
  return {
    codec: "opus",
    sampleRate: UP_RATE,
    numberOfChannels: 1,
    bitrate: 20000, // 16–24 kbps (spec 01 §4.1)
    opus: { frameDuration: FRAME_MS * 1000, usedtx: false, useinbandfec: false }, // DTX off, FEC off (spec 01 §4.1)
  };
}

/** Int16 PCM -> little-endian bytes (pcm16 payload). */
export function pcm16Bytes(int16) {
  const out = new Uint8Array(int16.length * 2);
  const view = new DataView(out.buffer);
  for (let i = 0; i < int16.length; i++) view.setInt16(i * 2, int16[i], true);
  return out;
}

/** pcm16 little-endian payload -> Float32 [-1, 1]. */
export function pcm16ToFloat(bytes) {
  const n = bytes.byteLength >> 1;
  const view = new DataView(bytes.buffer, bytes.byteOffset, n * 2);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = view.getInt16(i * 2, true) / 32768;
  return out;
}

/** Linear resample Float32 PCM. Exact passthrough when rates match. */
export function resampleLinear(src, srcRate, dstRate) {
  if (srcRate === dstRate) return src;
  const ratio = srcRate / dstRate;
  const n = Math.floor(src.length / ratio);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const pos = i * ratio;
    const j = Math.floor(pos);
    const frac = pos - j;
    const a = src[j];
    const b = j + 1 < src.length ? src[j + 1] : a;
    out[i] = a + (b - a) * frac;
  }
  return out;
}

/**
 * Uplink encoder. `onPacket(bytes, tsSamples)` is called once per 20 ms frame,
 * in order. For pcm16 it is synchronous; for Opus the WebCodecs encoder is
 * asynchronous and ts_samples is recovered from the chunk timestamp.
 */
export class UplinkEncoder {
  constructor(codec, onPacket) {
    this.codec = codec;
    this.onPacket = onPacket;
    this.enc = null;
    if (codec === "opus") {
      this.enc = new AudioEncoder({
        output: (chunk) => {
          const bytes = new Uint8Array(chunk.byteLength);
          chunk.copyTo(bytes);
          const tsSamples = Math.round((chunk.timestamp * UP_RATE) / 1e6) >>> 0;
          this.onPacket(bytes, tsSamples);
        },
        error: (e) => console.error("opus encoder", e),
      });
      this.enc.configure(opusEncoderConfig());
    }
  }

  /** @param {Int16Array} pcm 320 samples @16 kHz  @param {number} tsSamples */
  encode(pcm, tsSamples) {
    if (this.codec === "pcm16") {
      this.onPacket(pcm16Bytes(pcm), tsSamples);
      return;
    }
    const data = new AudioData({
      format: "s16",
      sampleRate: UP_RATE,
      numberOfFrames: pcm.length,
      numberOfChannels: 1,
      timestamp: Math.round((tsSamples * 1e6) / UP_RATE),
      data: pcm.buffer.slice(pcm.byteOffset, pcm.byteOffset + pcm.byteLength),
    });
    this.enc.encode(data);
    data.close();
  }

  /** Resolve after all queued input has been emitted (call before turn.end so no frame trails it). */
  async flush() {
    if (this.enc && this.enc.state === "configured") {
      try {
        await this.enc.flush();
      } catch {
        /* closed concurrently */
      }
    }
  }

  close() {
    if (this.enc && this.enc.state !== "closed") this.enc.close();
  }
}

/**
 * Downlink decoder. `onPcm(float32, sampleRate, meta)` receives decoded audio
 * with the `meta` object given to decode(), in order. The caller captures
 * play_gen into `meta` *before* calling decode(): that is the "tag at decode
 * time" of spec 01 §5.4, and it is what makes late Opus output droppable.
 */
export class DownlinkDecoder {
  constructor(codec, rate, onPcm) {
    this.codec = codec;
    this.rate = rate;
    this.onPcm = onPcm;
    this.dec = null;
    this.metas = []; // FIFO: Opus produces one output per input, in order
    this.n = 0;
    if (codec === "opus") {
      this.dec = new AudioDecoder({
        output: (audio) => {
          const meta = this.metas.shift();
          const pcm = new Float32Array(audio.numberOfFrames);
          try {
            audio.copyTo(pcm, { planeIndex: 0, format: "f32-planar" });
          } catch {
            audio.copyTo(pcm, { planeIndex: 0 });
          }
          const sr = audio.sampleRate;
          audio.close();
          if (meta) this.onPcm(pcm, sr, meta);
        },
        error: (e) => console.error("opus decoder", e),
      });
      this.dec.configure({ codec: "opus", sampleRate: rate, numberOfChannels: 1 });
    }
  }

  decode(payload, meta) {
    if (this.codec === "pcm16") {
      this.onPcm(pcm16ToFloat(payload), this.rate, meta);
      return;
    }
    this.metas.push(meta);
    this.dec.decode(new EncodedAudioChunk({ type: "key", timestamp: this.n++ * FRAME_MS * 1000, data: payload }));
  }

  /** Resolve after every pending output has been delivered (used before an eos marker). */
  async flush() {
    if (this.dec && this.dec.state === "configured") {
      try {
        await this.dec.flush();
      } catch {
        /* reset by a concurrent close */
      }
    }
  }

  close() {
    if (this.dec && this.dec.state !== "closed") this.dec.close();
  }
}

// ---------------------------------------------------------------- engine

/**
 * One AudioContext (asked for 16 kHz so capture needs no resampling; if the
 * browser refuses, the capture worklet resamples), one microphone stream that
 * stays open for the whole session so AEC can converge, one playback worklet.
 *
 * Callbacks:
 *   onFrame(int16, tsSamples)                      every 20 ms while capturing
 *   onReceipt({type, turnId, sentenceId, playedMs}) started / ended / interrupted
 *   onDropped(reason)                              "cap" | "gen"
 */
export class AudioEngine {
  constructor({ onFrame, onReceipt, onDropped }) {
    this.onFrame = onFrame;
    this.onReceipt = onReceipt;
    this.onDropped = onDropped;
    this.ctx = null;
    this.mic = null;
    this.capture = null;
    this.player = null;
    this.playGen = 0;
    this.lastTsSamples = 0; // 16 kHz counter of the latest captured frame
    this.stats = { queuedSamples: 0, consumedTotal: 0, underruns: 0, playing: false };
    this.stopWaiters = new Map();
    this.stopReq = 0;
    this.prerollMs = 240;
    this.maxBufferMs = 1200;
  }

  get rate() {
    return this.ctx ? this.ctx.sampleRate : UP_RATE;
  }

  async start() {
    if (this.ctx) return;
    this.ctx = new AudioContext({ sampleRate: UP_RATE, latencyHint: "interactive" });
    await this.ctx.audioWorklet.addModule(new URL("./worklets/capture.js", import.meta.url));
    await this.ctx.audioWorklet.addModule(new URL("./worklets/playback.js", import.meta.url));

    // All three constraints on; AEC must run on the machine that owns the speaker.
    this.mic = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      video: false,
    });
    const src = this.ctx.createMediaStreamSource(this.mic);
    this.capture = new AudioWorkletNode(this.ctx, "va-capture", { numberOfOutputs: 0 });
    this.capture.port.onmessage = (e) => {
      const m = e.data;
      if (m.type !== "frame") return;
      this.lastTsSamples = m.tsSamples;
      this.onFrame(new Int16Array(m.pcm), m.tsSamples);
    };
    src.connect(this.capture);

    this.player = new AudioWorkletNode(this.ctx, "va-playback", { outputChannelCount: [1] });
    this.player.port.onmessage = (e) => this.onPlayerMessage(e.data);
    this.player.connect(this.ctx.destination);
    this.configurePlayer();
    this.statsTimer = setInterval(() => this.player.port.postMessage({ type: "stats" }), 100);
    if (this.ctx.state !== "running") await this.ctx.resume();
  }

  configurePlayer() {
    this.player.port.postMessage({
      type: "config",
      prerollSamples: Math.round((this.rate * this.prerollMs) / 1000),
      maxSamples: Math.round((this.rate * this.maxBufferMs) / 1000),
    });
  }

  onPlayerMessage(m) {
    switch (m.type) {
      case "started":
      case "ended":
        this.onReceipt({ type: m.type, turnId: m.turnId, sentenceId: m.sentenceId, playedMs: this.samplesToMs(m.playedSamples) });
        break;
      case "interrupted":
        // Subtract the output latency: those samples left the worklet but not yet the speaker.
        this.onReceipt({
          type: "interrupted",
          turnId: m.turnId,
          sentenceId: m.sentenceId,
          playedMs: Math.max(0, this.samplesToMs(m.playedSamples) - this.outputLatencyMs()),
        });
        break;
      case "stopped": {
        const w = this.stopWaiters.get(m.reqId);
        if (w) {
          this.stopWaiters.delete(m.reqId);
          w(m.interrupted);
        }
        break;
      }
      case "dropped":
        this.onDropped(m.reason);
        break;
      case "stats":
        this.stats = m;
        break;
    }
  }

  samplesToMs(samples) {
    return Math.round((samples * 1000) / this.rate);
  }

  outputLatencyMs() {
    const s = this.ctx && typeof this.ctx.outputLatency === "number" ? this.ctx.outputLatency : 0;
    return Math.round(s * 1000);
  }

  /** Buffered-but-unplayed audio in ms (from the last worklet stats, ≤ 100 ms old). */
  bufferedMs() {
    return this.samplesToMs(this.stats.queuedSamples);
  }

  /** DAC-consumed samples converted to 16 kHz units for ping.ts_samples_played. */
  playedSamplesUp() {
    return Math.round((this.stats.consumedTotal * UP_RATE) / this.rate);
  }

  /**
   * Enqueue decoded audio. `playGen` was captured when the frame entered the
   * decode pipeline; the worklet drops it on dequeue if a stop happened since.
   */
  enqueue(pcm, srcRate, { turnId, sentenceId, playGen, uninterruptible }) {
    const out = resampleLinear(pcm, srcRate, this.rate);
    const buf = out.buffer.slice(out.byteOffset, out.byteOffset + out.byteLength);
    this.player.port.postMessage({ type: "push", pcm: buf, turnId, sentenceId, playGen, uninterruptible }, [buf]);
  }

  /** Tell the worklet a sentence has no more frames so it can emit `ended`. */
  endOfSentence(turnId, sentenceId) {
    this.player.port.postMessage({ type: "eos", turnId, sentenceId });
  }

  /**
   * Local stop: play_gen += 1, drop queued interruptible audio, keep
   * uninterruptible sentences. Resolves with the interrupted sentence
   * ({turnId, sentenceId, playedMs}) or null if nothing was mid-play.
   * The interrupted receipt is *also* delivered through onReceipt.
   */
  stopPlayback() {
    this.playGen += 1;
    if (!this.player) return Promise.resolve(null);
    const reqId = ++this.stopReq;
    return new Promise((resolve) => {
      this.stopWaiters.set(reqId, (intr) =>
        resolve(
          intr
            ? { turnId: intr.turnId, sentenceId: intr.sentenceId, playedMs: Math.max(0, this.samplesToMs(intr.playedSamples) - this.outputLatencyMs()) }
            : null,
        ),
      );
      this.player.port.postMessage({ type: "stop", playGen: this.playGen, reqId });
    });
  }

  async close() {
    clearInterval(this.statsTimer);
    if (this.mic) for (const t of this.mic.getTracks()) t.stop();
    if (this.ctx) await this.ctx.close();
    this.ctx = this.mic = this.capture = this.player = null;
  }
}
