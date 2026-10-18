// UI glue for the va/1 reference client. All protocol behaviour is in
// session.js; this file only wires DOM events and renders what the session emits.

import { Session } from "./session.js";

const $ = (id) => document.getElementById(id);
const el = {
  url: $("url"), profile: $("profile"), mode: $("mode"), pcm16: $("pcm16"),
  connect: $("connect"), disconnect: $("disconnect"), lamp: $("lamp"), state: $("state"), turn: $("turn"), conn: $("conn"),
  talk: $("talk"), text: $("text"), send: $("send"), listenOpen: $("listen-open"), listenClose: $("listen-close"),
  transcript: $("transcript"), sentences: $("sentences"), stats: $("stats"), log: $("log"),
};

// Default gateway URL: same host when served by the gateway, else localhost:8765.
el.url.value = location.protocol.startsWith("http")
  ? `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`
  : "ws://localhost:8765/ws";

let session = null;
const transcripts = new Map(); // turn_id -> element
const sentenceEls = new Map(); // "turn/sentence" -> element
const LOG_MAX = 300;

function log(cls, text) {
  const line = document.createElement("div");
  line.className = cls;
  line.textContent = `${new Date().toISOString().slice(11, 23)} ${text}`;
  el.log.appendChild(line);
  while (el.log.childElementCount > LOG_MAX) el.log.removeChild(el.log.firstChild);
  el.log.scrollTop = el.log.scrollHeight;
}

function short(m) {
  const { type, ...rest } = m;
  return `${type} ${JSON.stringify(rest)}`;
}

function setControls(connected) {
  el.connect.disabled = connected;
  el.disconnect.disabled = !connected;
  el.talk.disabled = !connected;
  el.text.disabled = !connected;
  el.send.disabled = !connected;
  const openMic = el.mode.value === "open_mic";
  el.listenOpen.disabled = !connected || !openMic;
  el.listenClose.disabled = !connected || !openMic;
  for (const c of [el.url, el.profile, el.mode, el.pcm16]) c.disabled = connected;
}

function onEvent(name, d) {
  switch (name) {
    case "log":
      log(d.level, d.text);
      break;
    case "tx":
      log("tx", `→ ${short(d)}`);
      break;
    case "rx":
      log("rx", `← ${short(d)}`);
      break;
    case "state":
      el.lamp.className = `lamp ${d.state}`;
      el.state.textContent = d.state;
      el.turn.textContent = d.turn_id ? `turn ${d.turn_id}` : "";
      break;
    case "connection":
      el.conn.textContent = d.connected
        ? `session ${d.sessionId} · gateway mode ${d.mode} · up ${d.up} · down ${d.down ? `${d.down.codec}@${d.down.rate}` : '-'} · lead ${d.leadMs} ms`
        : "disconnected";
      if (d.connected && d.mode && d.mode !== el.mode.value) log("warn", `gateway mode is ${d.mode}, UI selected ${el.mode.value}`);
      break;
    case "turn.local":
      if (d.source === "text") transcriptLine(d.turn_id, d.text, true);
      break;
    case "stt":
      transcriptLine(d.turn_id, d.text, d.final);
      break;
    case "sentence.started": {
      // Spec 02 §4.5 / §5: text goes on screen when its audio starts, not when generated.
      const key = `${d.turn_id}/${d.sentence_id}`;
      let s = sentenceEls.get(key);
      if (!s) {
        s = document.createElement("div");
        s.className = "sentence playing";
        s.innerHTML = `<span class="turn"></span><span class="t"></span><small></small>`;
        s.querySelector(".turn").textContent = `${d.turn_id}#${d.sentence_id}`;
        s.querySelector(".t").textContent = d.text;
        el.sentences.appendChild(s);
        sentenceEls.set(key, s);
      }
      break;
    }
    case "sentence.ended":
    case "sentence.interrupted": {
      const s = sentenceEls.get(`${d.turn_id}/${d.sentence_id}`);
      if (!s) break;
      s.className = `sentence ${name === "sentence.ended" ? "" : "interrupted"}`;
      s.querySelector("small").textContent = `${name.slice(9)} ${d.played_ms} ms`;
      break;
    }
    case "turn.closed":
      log(d.local ? "warn" : "rx", `turn ${d.turn_id} closed: ${d.state}${d.by ? ` by ${d.by}` : ""}${d.local ? " (local)" : ""}`);
      break;
  }
}

function transcriptLine(turnId, text, final) {
  let line = transcripts.get(turnId);
  if (!line) {
    line = document.createElement("div");
    line.innerHTML = `<span class="turn"></span><span class="t"></span>`;
    line.querySelector(".turn").textContent = turnId;
    el.transcript.appendChild(line);
    transcripts.set(turnId, line);
  }
  line.querySelector(".t").textContent = text;
  line.querySelector(".t").className = final ? "t" : "t partial";
}

function renderStats() {
  if (!session) return;
  const s = session.snapshot();
  const rows = [
    ["state", `${s.state}${s.turn ? ` (${s.turn})` : ""}`],
    ["session", s.sessionId ?? "-"],
    ["codec up / down", `${s.up} / ${s.down ? `${s.down.codec}@${s.down.rate}` : "-"} (ctx ${s.ctxRate} Hz)`],
    ["rtt (pong)", s.rtt == null ? "-" : `${s.rtt} ms`],
    ["play buffer", `${s.bufferMs} ms (lead ${s.leadMs ?? "-"} ms, cap 1200)`],
    ["play_gen", s.playGen],
    ["frames tx / rx", `${s.txFrames} / ${s.rxFrames}  (seq ${s.txSeq} / ${s.rxSeq})`],
    ["dropped: cap", s.droppedCap],
    ["dropped: play_gen (consumer)", s.droppedGen],
    ["dropped: gate (turn/state)", s.gated],
    ["violations: after turn.closed", s.afterClosed],
    ["violations: after flush.end", s.afterFlushEnd],
    ["underruns", s.underruns],
    ["protocol errors", s.protocolErrors],
    ["reconnects", s.reconnects],
    ["last turn.closed", s.lastClosed ? `${s.lastClosed.turn_id} ${s.lastClosed.state} by=${s.lastClosed.by} heard_ms=${s.lastClosed.heard_ms}` : "-"],
  ];
  el.stats.innerHTML = "";
  for (const [k, v] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = String(v);
    el.stats.append(dt, dd);
  }
}
setInterval(renderStats, 250);

// ---------------------------------------------------------------- controls

el.connect.onclick = async () => {
  session = new Session({ profile: el.profile.value, mode: el.mode.value, forcePcm16: el.pcm16.checked }, onEvent);
  setControls(true);
  try {
    await session.connect(el.url.value.trim());
  } catch (e) {
    log("error", `start failed: ${e.message ?? e}`);
    setControls(false);
    session = null;
  }
};

el.disconnect.onclick = async () => {
  if (!session) return;
  await session.disconnect();
  session = null;
  setControls(false);
  el.conn.textContent = "disconnected";
};

// Hold-to-talk: pointer (mouse + touch) and spacebar.
const press = (e) => {
  if (e) e.preventDefault();
  if (!session) return;
  el.talk.classList.add("down");
  session.pressStart();
};
const release = () => {
  if (!session) return;
  el.talk.classList.remove("down");
  session.pressEnd();
};
el.talk.addEventListener("pointerdown", press);
el.talk.addEventListener("pointerup", release);
el.talk.addEventListener("pointercancel", release);
el.talk.addEventListener("pointerleave", (e) => {
  if (e.buttons) release();
});
el.talk.addEventListener("contextmenu", (e) => e.preventDefault());
window.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || e.repeat || e.target === el.text || e.target.tagName === "INPUT") return;
  press(e);
});
window.addEventListener("keyup", (e) => {
  if (e.code !== "Space" || e.target === el.text) return;
  release();
});
window.addEventListener("blur", release);

const sendText = () => {
  const text = el.text.value.trim();
  if (!text || !session) return;
  el.text.value = "";
  session.sendText(text);
};
el.send.onclick = sendText;
el.text.addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendText();
});

el.listenOpen.onclick = () => session && session.listenOpen();
el.listenClose.onclick = () => session && session.listenClose();
el.mode.onchange = () => setControls(false);
