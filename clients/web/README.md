# Browser reference client (va/1, profile B over WebSocket)

Dependency-free static client for the `vak` gateway. Plain ES modules, no bundler, no build step. It is the reference for the client half of spec 01 ([`docs/specs/01-turn-model-and-client-protocol.md`](../../docs/specs/01-turn-model-and-client-protocol.md)) and the differences listed in [`docs/protocol.md`](../../docs/protocol.md).

## Run

Served by the gateway (default):

```bash
uv run vak run my_agent/agent.yaml      # serve_web_client: true
# open http://localhost:8765/
```

Standalone:

```bash
python -m http.server 8080 --directory clients/web
# open http://localhost:8080/ and set the gateway URL (default ws://localhost:8765/ws)
```

Requirements: a secure context (`https://` or `localhost`) for `getUserMedia` and `AudioWorklet`; a microphone. Opus needs WebCodecs (`AudioEncoder` / `AudioDecoder`, Chrome/Edge 94+); otherwise, or with "force pcm16" ticked, the client offers only `pcm16`.

Tests (pure code only, no browser):

```bash
node --test clients/web/protocol.test.mjs clients/web/playback.test.mjs
```

## Files

| File | What |
|---|---|
| `protocol.js` | JSON message builders / parser and the 16-byte frame codec (spec 01 §3.1.2, §3.2.1). Pure functions; runs under Node and ports to firmware as-is. |
| `audio.js` | `AudioEngine` (capture + playback worklets, one `AudioContext` asked for 16 kHz), `UplinkEncoder` / `DownlinkDecoder` (pcm16, Opus via WebCodecs). |
| `worklets/capture.js` | Mic → 16 kHz mono → 20 ms Int16 frames stamped with `ts_samples`. Runs for the whole session so AEC keeps converging. |
| `worklets/playback.js` | Ring queue → speaker. Counts DAC-consumed samples per sentence, emits started / ended / interrupted, second-gate `play_gen` drop, buffer cap, preroll, underrun stall. |
| `session.js` | WebSocket, turn-machine mirror, two-level gating, receipts, interruption order, ping, T_link_client, reconnect / resume with retransmit. |
| `app.js`, `index.html` | UI: profile / mode / URL / force-pcm16, hold-to-talk (mouse, touch, Space), text input, `listen.open/close`, transcript, assistant sentences, status lamp, stats, event log. |
| `protocol.test.mjs`, `playback.test.mjs` | `node --test` suites for the codec and the playback worklet logic. |

## Implemented

- `hello` with `proto: "va/1"`, `profile`, `client{model:"web", fw:"0.1"}`, the caps block from spec 01 §3.1.1 (`aec:"os"`, both modes, `button`/`text` sources, all three receipts, `audio_up` 16 kHz / 20 ms, `audio_down` codecs `["opus","pcm16"]` (or `["pcm16"]`), rates `[16000, 24000]`, `play_buffer_max_ms: 1200`, `preroll_ms: 250`, `uninterruptible: true`, no events, no commands). Reply handling: `session_id`, `timers` (ping interval, `T_link_client`, `resume_window`), `lead_ms`, negotiated `audio_down.codec/rate` (decoder created from it, re-created if a `tts.start` names a different codec/rate), `mode`, `next_server_turn` (logged).
- Uplink codec: Opus when WebCodecs supports it, else pcm16. If the hello reply carries `audio_up.codec` (not in the spec; an optional gateway hint) the encoder is switched to it.
- Audio capture: `getUserMedia` with `echoCancellation`, `noiseSuppression`, `autoGainControl` on, mono; AudioWorklet at 16 kHz, 320-sample frames; 250 ms preroll ring sent ahead of each button turn with earlier `ts_samples`; `key_down` flag while pressed; `echo_tail` flag for the 200 ms after a barge-in press; `retransmit` flag on resume. Opus: 20 kbps, DTX off, FEC off.
- Playback: AudioWorklet ring queue; 240 ms preroll before the first sample of a run; cap 1200 ms, overflow drops the new frame and counts it; underrun stalls without inserting silence or sending `ended` (R4.4-2); `played_ms` from samples actually rendered in `process()` (minus `AudioContext.outputLatency` for `interrupted`).
- Turn-machine mirror (`idle / listening / committed / responding / draining` + terminal; `holding` is gateway-internal and is only seen through `turn.merge`). Downlink frames enter the decode queue only when the mirror is `responding`/`draining`, the frame's `turn_id` equals the current turn and no local stop happened for it (gate 1, R2.1-4). Each frame is tagged with `play_gen` before decoding and dropped on dequeue if a stop happened since (gate 2, §5.4). Both drop counts are on the stats panel; counters for frames after `turn.closed` and after `flush.end` (acceptance items 2 and 9).
- Receipts per sentence: `playback.started` at the first rendered sample, `ended` when the sentence's last frame is consumed (known from the next sentence's first frame or from `tts.stop{done}` → decoder flush → end-of-sentence marker), `interrupted` once for the sentence mid-play, never followed by `ended`. `interruptible:false` sentences (frame flag or `tts.sentence_start`) are kept through a stop and played to the end.
- Button interruption (§5.1): on press during `responding`/`draining` the client stops locally, `play_gen += 1`, then sends `playback.interrupted{reason:"button"}`, `abort{reason:"button"}`, `turn.start{source:"button"}` in that order in one task. Text input during playback does the same with `reason:"text"` then `turn.start{source:"text", input:"typed"}`. In `open_mic` the client never stops on its own: it waits for `flush.start` (VAD barge-in) and sends `playback.interrupted{reason: flush.reason}`.
- `flush.start` → drop buffered audio of the listed turns (except uninterruptible), stop if playing; `flush.end` → later frames of those turns are counted as violations.
- `turn.start`/`turn.end` from the gateway (`vad`, `event`), `turn.merge` (current turn id switches to `into`), `stt` partial/final, `error` (session-level `session_expired` → full reset and a fresh `hello`), `turn.closed` (state/by/heard_ms on the stats panel), `cmd` → `cmd.result{ok:false}`, unknown types ignored.
- `ping` every `timers.ping_interval` (default 5 s) with `client_mono_ms`, `ts_samples_up`, `ts_samples_played`; RTT from `pong`; 3 unanswered → reconnect. `T_link_client` (commit → first `stt`) → reconnect.
- Reconnect / resume (§6): exponential backoff (0.5 s → 5 s); `hello.resume{session_id, last_tx_seq, last_rx_seq, current_turn, turn_state}`; on `resume.ok` the uplink ring (3 s of frames, spec asks ≥ 2 s) is retransmitted from `last_rx_seq + 1` with the retransmit flag, then control messages produced during the outage (`turn.end`, receipts, `abort`) are sent; gateway turn states from `resume.turns` overwrite the mirror; a sentence playing at link loss gets `playback.interrupted{reason:"link"}`. Outside `resume_window` or on `session_expired` all open turns are locally `failed{link_lost}` and a new session starts.
- UI sync: an assistant sentence appears only when its `playback.started` fires (spec 02 §4.5); interrupted sentences show `played_ms`.

## Not implemented

- WebRTC transport (profile B column 3 in spec 01 §7); only WebSocket.
- Wake word (`source:"wake"`), device events (`event` / `event.ack`), commands (`cmd` is answered `ok:false`), local pre-synthesised failure prompts (profile A "must make a sound on `failed`").
- Client-side VAD of any kind; in `open_mic` the gateway owns boundaries and barge-in.
- `holding` state display (the client cannot observe it; it only reacts to `turn.merge`).
- Opus PLC for reconnect gaps is a gateway concern (§4.5); the client only retransmits.
- RFC 1982 wrap-around comparison of `ts_samples` (wrap is ~74 h at 16 kHz).

## Assumptions to confirm against the gateway

- WebSocket path: the UI defaults to `/ws` on the page's host (or `ws://localhost:8765/ws` when opened from a file / other server); edit the URL field if the gateway uses another path.
- `pcm16` payloads are little-endian signed 16-bit mono (frame headers are big-endian).
- `tts.stop{reason:"done"}` arrives after the last downlink frame of the turn (it is what triggers the last `playback.ended`).

## Spec 01 §8 acceptance items the client participates in

| # | Item | Client side |
|---|---|---|
| 1 | every `turn.start` ↔ one `turn.closed` | client-generated turns are tracked in `turns`; local `failed{link_lost}` only outside the resume window (R2.4-1 exception) |
| 2 | no downlink frames after `turn.closed` | counted: `violations: after turn.closed` |
| 7 | re-press inside the supplement window → `turn.merge` | handled: current turn id becomes `into`, later frames use it |
| 8 | short press → `discarded`, zero sound | mirror never enters `responding` for that turn, so any stray frame is gated |
| 9 | zero frames after `flush.end` | counted: `violations: after flush.end` |
| 10 | key-down → speaker below noise floor ≤ 60 ms | local stop happens in the worklet on its next 128-sample quantum (8 ms at 16 kHz) plus device output latency; needs measurement on the target machine |
| 11 | `playback.interrupted.played_ms` within 40 ms of the recording | samples rendered in `process()` minus `AudioContext.outputLatency`; needs measurement |
| 12 | residual same-`turn_id` frames after abort are not played | gate 1 (`localStopped`) drops frames that arrive after the stop; gate 2 (`play_gen`) drops frames already in the decode queue; `dropped: play_gen` counts gate-2 hits (non-zero means the gateway's flush let something through) |
| 15 | downlink lead ≤ `lead_ms` + one frame | observable as `play buffer` on the stats panel |
| 17 / 18 | reconnect gap handling, retransmit de-dup | client retransmits with the retransmit flag from `last_rx_seq + 1`; de-dup is the gateway's |
| 19 | play buffer never exceeds `play_buffer_max_ms`, overflow count 0 on a normal network | cap enforced in the worklet; `dropped: cap` on the stats panel |
| 22 | `hello` downgrade for receipt-less clients | not applicable (this client sends all three receipts) |
| 23 | A / B implementations run each other's cases | this client can be switched to profile A in the UI (same wire behaviour; A-only duties such as local failure prompts are not implemented) |
