// Capture worklet: microphone -> 16 kHz mono -> 20 ms (320-sample) Int16
// frames, each stamped with ts_samples = running 16 kHz sample count of its
// first sample (spec 01 §3.1.2). Runs continuously once started so the
// browser AEC keeps converging across turns (handbook audio-preprocessing).
//
// Messages out: { type: "frame", pcm: ArrayBuffer(Int16 x 320), tsSamples }

const TARGET_RATE = 16000;
const FRAME_SAMPLES = 320; // 20 ms at 16 kHz

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE; // input samples per output sample
    this.pending = new Float32Array(0); // unconsumed input samples
    this.frame = new Int16Array(FRAME_SAMPLES);
    this.frameFill = 0;
    this.tsSamples = 0; // 16 kHz counter, stamped on the frame's first sample
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;

    if (this.ratio === 1) {
      this.pushSamples(ch);
      return true;
    }

    // Box-filter decimation (or linear stretch when upsampling): for each
    // output sample average the input span [pos, pos+ratio). Cheap and good
    // enough for speech; the browser normally honours a 16 kHz context so
    // this path is only a fallback.
    const merged = new Float32Array(this.pending.length + ch.length);
    merged.set(this.pending, 0);
    merged.set(ch, this.pending.length);
    const ratio = this.ratio;
    const outCount = Math.floor(merged.length / ratio);
    const out = new Float32Array(outCount);
    let pos = 0;
    for (let i = 0; i < outCount; i++) {
      const start = Math.floor(pos);
      const end = Math.max(start + 1, Math.floor(pos + ratio));
      let acc = 0;
      for (let j = start; j < end && j < merged.length; j++) acc += merged[j];
      out[i] = acc / (end - start);
      pos += ratio;
    }
    const consumed = Math.floor(pos);
    this.pending = merged.subarray(consumed).slice();
    this.pushSamples(out);
    return true;
  }

  pushSamples(samples) {
    for (let i = 0; i < samples.length; i++) {
      let v = samples[i];
      if (v > 1) v = 1;
      else if (v < -1) v = -1;
      this.frame[this.frameFill++] = v < 0 ? v * 32768 : v * 32767;
      if (this.frameFill === FRAME_SAMPLES) {
        const buf = this.frame.buffer.slice(0);
        this.port.postMessage({ type: "frame", pcm: buf, tsSamples: this.tsSamples }, [buf]);
        this.tsSamples = (this.tsSamples + FRAME_SAMPLES) >>> 0;
        this.frameFill = 0;
      }
    }
  }
}

registerProcessor("va-capture", CaptureProcessor);
