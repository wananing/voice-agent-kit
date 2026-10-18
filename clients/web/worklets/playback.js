// Playback worklet: queue of decoded PCM chunks -> speaker, with the
// accounting the protocol needs (spec 01 §3.1.6, §4.3, §5.4):
//
// - every chunk is tagged (turnId, sentenceId, playGen, uninterruptible);
// - chunks whose playGen != current are dropped on dequeue (second gate),
//   unless uninterruptible;
// - buffer cap: a push that would exceed `maxSamples` is dropped and counted;
// - preroll: output starts when >= prerollSamples are queued (or an eos
//   marker is queued); on underrun we stall silently and keep the position
//   (R4.4-2: no silence insertion, no `ended`);
// - played samples are counted here, in process(), i.e. what actually left
//   for the DAC, so `played_ms` is not "decoded" or "enqueued" length;
// - receipts: started (first sample of a sentence rendered), ended (last
//   chunk of a sentence consumed, known from the next sentence's first chunk
//   or an eos marker), interrupted (stop while a sentence was mid-play).
//
// Messages in:
//   { type:"config", prerollSamples, maxSamples }
//   { type:"push", pcm: ArrayBuffer(Float32, already at context rate),
//     turnId, sentenceId, playGen, uninterruptible }
//   { type:"eos", turnId, sentenceId }            // sentence has no more frames
//   { type:"stop", playGen, reqId }               // local stop; drop interruptible audio
//   { type:"stats" }
// Messages out:
//   { type:"started"|"ended", turnId, sentenceId, playedSamples }
//   { type:"interrupted", turnId, sentenceId, playedSamples }
//   { type:"stopped", reqId, interrupted: {turnId, sentenceId, playedSamples}|null }
//   { type:"dropped", reason:"cap"|"gen", turnId, sentenceId }
//   { type:"stats", queuedSamples, consumedTotal, underruns, playing }

class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = []; // chunks and eos markers, in arrival order
    this.queuedSamples = 0; // unplayed pcm samples: queue + remainder of head
    this.head = null; // chunk currently being consumed
    this.headPos = 0;
    this.current = null; // { turnId, sentenceId, played, uninterruptible } sentence in progress
    this.playGen = 0;
    this.prerollSamples = Math.round(sampleRate * 0.24);
    this.maxSamples = Math.round(sampleRate * 1.2);
    this.playing = false;
    this.consumedTotal = 0;
    this.underruns = 0;
    this.port.onmessage = (e) => this.onMessage(e.data);
  }

  onMessage(m) {
    switch (m.type) {
      case "config":
        if (m.prerollSamples) this.prerollSamples = m.prerollSamples;
        if (m.maxSamples) this.maxSamples = m.maxSamples;
        break;
      case "push": {
        const pcm = new Float32Array(m.pcm);
        if (this.queuedSamples + pcm.length > this.maxSamples) {
          this.port.postMessage({ type: "dropped", reason: "cap", turnId: m.turnId, sentenceId: m.sentenceId });
          break;
        }
        this.queue.push({ kind: "pcm", pcm, turnId: m.turnId, sentenceId: m.sentenceId, playGen: m.playGen, uninterruptible: !!m.uninterruptible });
        this.queuedSamples += pcm.length;
        break;
      }
      case "eos":
        this.queue.push({ kind: "eos", turnId: m.turnId, sentenceId: m.sentenceId });
        break;
      case "stop":
        this.stop(m.playGen, m.reqId);
        break;
      case "stats":
        this.postStats();
        break;
    }
  }

  stop(newGen, reqId) {
    this.playGen = newGen;
    let interrupted = null;
    // Sentence mid-play (rendering or stalled on underrun): interrupt it unless uninterruptible.
    if (this.current && !this.current.uninterruptible) {
      interrupted = { turnId: this.current.turnId, sentenceId: this.current.sentenceId, playedSamples: this.current.played };
      this.port.postMessage({ type: "interrupted", ...interrupted });
      if (this.head) this.queuedSamples -= this.head.pcm.length - this.headPos;
      this.head = null;
      this.headPos = 0;
      this.current = null;
    }
    // Keep uninterruptible chunks (and eos markers for the sentences they belong to).
    const keepKeys = new Set();
    if (this.current) keepKeys.add(this.current.turnId + "/" + this.current.sentenceId);
    for (const c of this.queue) if (c.kind === "pcm" && c.uninterruptible) keepKeys.add(c.turnId + "/" + c.sentenceId);
    const kept = [];
    let queued = this.head ? this.head.pcm.length - this.headPos : 0;
    for (const c of this.queue) {
      if (keepKeys.has(c.turnId + "/" + c.sentenceId)) {
        kept.push(c);
        if (c.kind === "pcm") queued += c.pcm.length;
      }
    }
    this.queue = kept;
    this.queuedSamples = queued;
    if (!this.head && this.queue.length === 0) this.playing = false;
    this.port.postMessage({ type: "stopped", reqId, interrupted });
  }

  // Take the next renderable chunk into `head`, emitting receipts. Returns false if none.
  advance() {
    while (this.queue.length > 0) {
      const c = this.queue.shift();
      if (c.kind === "eos") {
        if (this.current && this.current.turnId === c.turnId && this.current.sentenceId === c.sentenceId) {
          this.emitEnded();
        }
        continue;
      }
      if (c.playGen !== this.playGen && !c.uninterruptible) {
        this.queuedSamples -= c.pcm.length;
        this.port.postMessage({ type: "dropped", reason: "gen", turnId: c.turnId, sentenceId: c.sentenceId });
        continue;
      }
      if (this.current && (this.current.turnId !== c.turnId || this.current.sentenceId !== c.sentenceId)) {
        this.emitEnded();
      }
      if (!this.current) {
        this.current = { turnId: c.turnId, sentenceId: c.sentenceId, played: 0, uninterruptible: c.uninterruptible };
        this.port.postMessage({ type: "started", turnId: c.turnId, sentenceId: c.sentenceId, playedSamples: 0 });
      }
      this.head = c;
      this.headPos = 0;
      return true;
    }
    return false;
  }

  emitEnded() {
    const s = this.current;
    this.port.postMessage({ type: "ended", turnId: s.turnId, sentenceId: s.sentenceId, playedSamples: s.played });
    this.current = null;
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    const n = out.length;

    if (!this.playing) {
      const eosQueued = this.queue.some((c) => c.kind === "eos");
      if (this.head || this.queuedSamples >= this.prerollSamples || eosQueued) {
        this.playing = true;
      } else {
        out.fill(0);
        return true;
      }
    }

    let i = 0;
    while (i < n) {
      if (!this.head && !this.advance()) break;
      const pcm = this.head.pcm;
      const take = Math.min(n - i, pcm.length - this.headPos);
      out.set(pcm.subarray(this.headPos, this.headPos + take), i);
      i += take;
      this.headPos += take;
      this.queuedSamples -= take;
      this.current.played += take;
      this.consumedTotal += take;
      if (this.headPos >= pcm.length) {
        this.head = null;
        this.headPos = 0;
      }
    }
    if (i < n) {
      out.fill(0, i);
      // Drained. A sentence still open means its eos has not arrived: underrun,
      // stall and keep the position. Otherwise we are simply idle.
      if (this.current) this.underruns++;
      this.playing = false;
    }
    for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);
    return true;
  }

  postStats() {
    this.port.postMessage({
      type: "stats",
      queuedSamples: this.queuedSamples,
      consumedTotal: this.consumedTotal,
      underruns: this.underruns,
      playing: this.playing,
    });
  }
}

registerProcessor("va-playback", PlaybackProcessor);
