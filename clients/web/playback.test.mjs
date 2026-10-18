// Run: node --test clients/web/playback.test.mjs
// Drives the playback AudioWorkletProcessor under Node with stubbed worklet
// globals: receipts, preroll, play_gen gate, uninterruptible, buffer cap.
import { test } from "node:test";
import assert from "node:assert/strict";

const RATE = 16000;
const QUANTUM = 128;

let Processor;
globalThis.sampleRate = RATE;
globalThis.AudioWorkletProcessor = class {
  constructor() {
    this.port = { postMessage: (m) => this.port.out.push(m), out: [], onmessage: null };
  }
};
globalThis.registerProcessor = (_name, cls) => (Processor = cls);
await import("./worklets/playback.js");

function make() {
  const p = new Processor();
  p.onMessage({ type: "config", prerollSamples: 3200, maxSamples: 19200 }); // 200 ms / 1200 ms
  const out = [new Float32Array(QUANTUM)];
  const render = (quanta = 1) => {
    for (let i = 0; i < quanta; i++) p.process([], [out]);
    return out[0];
  };
  const push = (turnId, sentenceId, n, { playGen = 0, uninterruptible = false, value = 0.5 } = {}) => {
    const pcm = new Float32Array(n).fill(value);
    p.onMessage({ type: "push", pcm: pcm.buffer, turnId, sentenceId, playGen, uninterruptible });
  };
  const msgs = (type) => p.port.out.filter((m) => m.type === type);
  return { p, render, push, msgs };
}

test("preroll: silence until enough is queued, then started receipt", () => {
  const { render, push, msgs } = make();
  push("c1", 0, 1600); // 100 ms < 200 ms preroll
  assert.equal(render()[0], 0);
  assert.equal(msgs("started").length, 0);
  push("c1", 0, 1600); // now 200 ms
  assert.equal(render()[0], 0.5);
  assert.deepEqual(msgs("started")[0], { type: "started", turnId: "c1", sentenceId: 0, playedSamples: 0 });
});

test("ended on sentence change and on eos; played samples are DAC-consumed", () => {
  const { render, push, msgs } = make();
  push("c1", 0, 3200);
  push("c1", 1, 3200);
  render(25); // 3200 samples: sentence 0 fully consumed
  assert.equal(msgs("ended").length, 0); // boundary known only when the next chunk is taken
  render(1);
  assert.deepEqual(msgs("ended")[0], { type: "ended", turnId: "c1", sentenceId: 0, playedSamples: 3200 });
  assert.equal(msgs("started").length, 2);
  render(26); // sentence 1 drained; no eos yet -> no ended (R4.4-2), just a stall
  assert.equal(msgs("ended").length, 1);
});

test("eos after underrun still yields ended with exact count", () => {
  const { p, render, push, msgs } = make();
  push("c1", 0, 3200);
  render(26); // consumed all 3200 (25 quanta) + 1 stalled quantum
  assert.equal(msgs("ended").length, 0);
  p.onMessage({ type: "eos", turnId: "c1", sentenceId: 0 });
  render(1);
  assert.deepEqual(msgs("ended")[0], { type: "ended", turnId: "c1", sentenceId: 0, playedSamples: 3200 });
  assert.equal(p.underruns, 1);
});

test("stop mid-sentence: interrupted once with played samples, rest dropped, stopped reply", () => {
  const { p, render, push, msgs } = make();
  push("c1", 0, 3200);
  push("c1", 1, 3200);
  render(10); // 1280 samples of sentence 0
  p.onMessage({ type: "stop", playGen: 1, reqId: 7 });
  assert.deepEqual(msgs("interrupted"), [{ type: "interrupted", turnId: "c1", sentenceId: 0, playedSamples: 1280 }]);
  assert.deepEqual(msgs("stopped")[0], { type: "stopped", reqId: 7, interrupted: { turnId: "c1", sentenceId: 0, playedSamples: 1280 } });
  assert.equal(p.queuedSamples, 0);
  assert.equal(render()[0], 0);
  assert.equal(msgs("ended").length, 0); // no ended after interrupted
  assert.equal(msgs("started").length, 1); // sentence 1 never started
});

test("second gate: chunks tagged with an old play_gen are dropped on dequeue", () => {
  const { p, render, push, msgs } = make();
  p.onMessage({ type: "stop", playGen: 1, reqId: 1 }); // nothing playing
  assert.equal(msgs("stopped")[0].interrupted, null);
  push("c1", 0, 3200, { playGen: 0 }); // late residual, decoded before the stop
  push("c1", 0, 3200, { playGen: 1 });
  render(1);
  assert.deepEqual(msgs("dropped"), [{ type: "dropped", reason: "gen", turnId: "c1", sentenceId: 0 }]);
  assert.equal(render()[0], 0.5);
  assert.equal(msgs("started").length, 1);
});

test("uninterruptible sentence survives stop; later interruptible audio is dropped", () => {
  const { p, render, push, msgs } = make();
  push("c1", 0, 3200, { uninterruptible: true, value: 0.25 });
  push("c1", 1, 3200, { value: 0.5 });
  render(5);
  p.onMessage({ type: "stop", playGen: 1, reqId: 2 });
  assert.equal(msgs("interrupted").length, 0);
  assert.equal(msgs("stopped")[0].interrupted, null);
  render(20); // finish sentence 0 (3200 = 25 quanta)
  assert.equal(render()[0], 0); // sentence 1 gone
  p.onMessage({ type: "eos", turnId: "c1", sentenceId: 0 });
  render(1);
  assert.deepEqual(msgs("ended")[0], { type: "ended", turnId: "c1", sentenceId: 0, playedSamples: 3200 });
  assert.equal(msgs("started").length, 1);
});

test("buffer cap: pushes beyond maxSamples are dropped and counted, not blocking", () => {
  const { p, push, msgs } = make();
  for (let i = 0; i < 6; i++) push("c1", 0, 3200); // 6 x 200 ms = 1200 ms exactly
  push("c1", 0, 320);
  assert.deepEqual(msgs("dropped"), [{ type: "dropped", reason: "cap", turnId: "c1", sentenceId: 0 }]);
  assert.equal(p.queuedSamples, 19200);
});

test("stats message reports queue and consumed totals", () => {
  const { p, render, push, msgs } = make();
  push("c1", 0, 3200);
  render(2);
  p.onMessage({ type: "stats" });
  const s = msgs("stats")[0];
  assert.equal(s.consumedTotal, 256);
  assert.equal(s.queuedSamples, 3200 - 256);
  assert.equal(s.playing, true);
});
