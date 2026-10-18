"""Regenerate the README diagrams (light and dark): python docs/assets/make_diagrams.py docs/assets

Style follows voice-agent-handbook assets/STYLE.md: hand-written SVG, no external fonts.
"""
import sys
from pathlib import Path

OUT = Path(sys.argv[1])
FONT = "-apple-system, 'Segoe UI', 'PingFang SC', 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif"
PAL = {
    "light": dict(bg="#ffffff", fg="#1f2328", mute="#656d76", line="#d0d7de", soft="#f6f8fa",
                  blue="#0969da", green="#1a7f37", orange="#bc4c00", blue_bg="#ddf4ff", green_bg="#dafbe1", orange_bg="#fff1e5"),
    "dark": dict(bg="#0d1117", fg="#e6edf3", mute="#8b949e", line="#30363d", soft="#161b22",
                 blue="#58a6ff", green="#3fb950", orange="#d29922", blue_bg="#122d4d", green_bg="#12361f", orange_bg="#3d2306"),
}


class Svg:
    def __init__(self, h, c):
        self.h, self.c, self.parts = h, c, []

    def rect(self, x, y, w, h, fill="soft", stroke="line", dash=False):
        d = ' stroke-dasharray="5 4"' if dash else ""
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{self.c[fill]}" stroke="{self.c[stroke]}" stroke-width="1.5"{d}/>')

    def text(self, x, y, s, size=14, color="fg", anchor="start", bold=False):
        b = ' font-weight="600"' if bold else ""
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{self.c[color]}" text-anchor="{anchor}"{b}>{s}</text>')

    def line(self, x1, y1, x2, y2, color="mute", arrow=True, dash=False):
        m = f' marker-end="url(#a-{color})"' if arrow else ""
        d = ' stroke-dasharray="5 4"' if dash else ""
        self.parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{self.c[color]}" stroke-width="1.5"{m}{d}/>')

    def render(self):
        marks = "".join(
            f'<marker id="a-{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{self.c[k]}"/></marker>'
            for k in ("mute", "blue", "green", "orange")
        )
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 {self.h}" width="100%" font-family="{FONT}">\n'
                f"<defs>{marks}</defs>\n"
                f'<rect x="0" y="0" width="1200" height="{self.h}" fill="{self.c["bg"]}"/>\n' + "\n".join(self.parts) + "\n</svg>\n")


def architecture(c):
    s = Svg(660, c)
    s.text(32, 52, "How vak is layered", 20, bold=True)
    s.text(32, 80, "You write four files. The runtime in the middle is the kit; models and vendors plug in underneath.", 14, "mute")

    # client
    s.rect(32, 120, 200, 380)
    s.text(132, 150, "Client", 14, anchor="middle", bold=True)
    for i, t in enumerate(["browser reference", "client, firmware", "or your app"]):
        s.text(132, 180 + i * 20, t, 14, "mute", "middle")
    s.text(132, 280, "protocol va/1", 14, anchor="middle")
    for i, t in enumerate(["WebSocket", "JSON control frames", "+ audio frames with", "a 16-byte header"]):
        s.text(132, 304 + i * 18, t, 12, "mute", "middle")
    s.line(232, 200, 286, 200, "mute")
    s.line(286, 230, 232, 230, "mute")

    # runtime
    s.rect(288, 120, 640, 380, fill="bg", stroke="blue")
    s.text(304, 148, "vak runtime", 14, "blue", bold=True)
    s.rect(304, 164, 608, 52)
    s.text(320, 186, "transport", 14, bold=True)
    s.text(320, 205, "protocol codec · downlink pacing · seq and resume · clock anchors", 12, "mute")

    s.text(304, 244, "core: policy lives here, no vendor names allowed", 12, "mute")
    core = [("Turn", "state machine"), ("Output", "arbiter"), ("Tool layer", "A0–A3 routes"), ("Context", "and session"), ("Trace", "and eval")]
    w, gap = 112, 12
    for i, (a, b) in enumerate(core):
        x = 304 + i * (w + gap)
        s.rect(x, 256, w, 60, fill="blue_bg", stroke="blue")
        s.text(x + w / 2, 281, a, 14, anchor="middle", bold=True)
        s.text(x + w / 2, 301, b, 12, "mute", "middle")

    s.line(560, 326, 560, 360, "mute")
    s.line(656, 360, 656, 326, "mute")
    s.text(552, 348, "9 primitives", 12, "mute", "end")
    s.text(664, 348, "standard events", 12, "mute")

    s.text(304, 384, "upstream adapters: translate only, degrade by declared capability", 12, "mute")
    pipes = [("cascade", "STT → LLM → TTS"), ("half_cascade", "audio LLM → TTS"), ("s2s", "realtime model"), ("full_duplex", "moshi-style")]
    w, gap = 143, 12
    for i, (a, b) in enumerate(pipes):
        x = 304 + i * (w + gap)
        s.rect(x, 396, w, 60)
        s.text(x + w / 2, 421, a, 14, anchor="middle", bold=True)
        s.text(x + w / 2, 441, b, 12, "mute", "middle")
    s.text(304, 484, "also in the box: vak new · run · probe · trace · eval · bench", 12, "mute")

    # you write
    s.rect(968, 120, 200, 380, fill="green_bg", stroke="green")
    s.text(1068, 150, "You write", 14, "green", "middle", bold=True)
    files = [("agent.yaml", "pipeline, providers"), ("persona.yaml", "who the agent is"), ("tools.py", "@tool functions"), ("state_schema.yaml", "what it must track")]
    for i, (a, b) in enumerate(files):
        y = 172 + i * 78
        s.rect(984, y, 168, 58, fill="bg", stroke="green")
        s.text(1068, y + 25, a, 14, anchor="middle", bold=True)
        s.text(1068, y + 44, b, 12, "mute", "middle")
    s.line(968, 310, 930, 310, "green")

    # providers
    s.line(608, 500, 608, 534, "mute")
    s.rect(32, 536, 1136, 92)
    s.text(48, 562, "Providers", 14, bold=True)
    rows = [
        ("STT", "Volcengine · Aliyun · OpenAI Realtime · Deepgram · AssemblyAI · FunASR"),
        ("LLM", "any OpenAI-compatible · Volcengine Ark · Aliyun Bailian"),
        ("TTS", "Volcengine (token-level) · Aliyun CosyVoice · OpenAI · self-hosted CosyVoice"),
    ]
    for i, (k, v) in enumerate(rows):
        s.text(140, 562 + i * 22, k, 12, "blue", bold=True)
        s.text(180, 562 + i * 22, v, 12, "mute")
    s.text(780, 562, "S2S", 12, "blue", bold=True)
    s.text(820, 562, "OpenAI Realtime · Qwen-Omni Realtime", 12, "mute")
    s.text(780, 584, "Duplex", 12, "blue", bold=True)
    s.text(830, 584, "moshi", 12, "mute")
    return s.render()


def barge_in(c):
    s = Svg(700, c)
    s.text(32, 52, "What happens on a barge-in", 20, bold=True)
    s.text(32, 80, "The user presses the button while the agent is talking. Nothing in this picture is code you write.", 14, "mute")
    D, V, U = 200, 600, 1000
    for x, name in ((D, "Device"), (V, "vak"), (U, "Upstream")):
        s.rect(x - 80, 104, 160, 36, fill="soft")
        s.text(x, 127, name, 14, anchor="middle", bold=True)
        s.line(x, 140, x, 672, "line", arrow=False)

    def msg(y, a, b, label, color="mute", dash=False):
        s.line(a, y, b - 4 if b > a else b + 4, y, color, dash=dash)
        mid = (a + b) / 2
        s.text(mid, y - 8, label, 12, color if color != "mute" else "fg", "middle")

    y = 176
    msg(y, D, V, "turn.start {button} · audio frames · turn.end"); y += 38
    msg(y, V, U, "commit_turn · request_response"); y += 38
    msg(y, U, V, "sentence · audio (generation 1)", "blue"); y += 38
    msg(y, V, D, "tts.start · tts.sentence_start · audio, paced", "blue"); y += 30

    s.line(32, y, 1168, y, "orange", arrow=False, dash=True)
    s.text(40, y + 20, "user presses the button mid-sentence", 12, "orange")
    y += 40

    s.rect(D - 150, y - 4, 300, 44, fill="orange_bg", stroke="orange")
    s.text(D, y + 15, "stop playback within one tick", 12, anchor="middle")
    s.text(D, y + 32, "local play generation + 1", 12, "mute", "middle")
    y += 64
    msg(y, D, V, "playback.interrupted {played_ms} · abort · turn.start", "orange"); y += 22

    s.rect(V - 150, y - 4, 300, 44, fill="orange_bg", stroke="orange")
    s.text(V, y + 15, "generation + 1, flush queued audio", 12, anchor="middle")
    s.text(V, y + 32, "context cut at what was actually heard", 12, "mute", "middle")
    y += 66
    msg(y, V, U, "cancel_response · truncate(audio_end_ms)", "orange"); y += 38
    msg(y, U, V, "late audio (generation 1)", "mute", dash=True)
    s.text(V + 14, y + 20, "dropped: stale generation", 12, "orange")
    y += 50
    msg(y, V, D, "flush.start · flush.end · tts.stop {interrupted}", "orange"); y += 38
    msg(y, V, D, "turn.closed {interrupted}, then the new turn runs", "fg" if False else "mute")
    return s.render()


for name, fn in (("architecture", architecture), ("barge-in", barge_in)):
    for theme in ("light", "dark"):
        (OUT / f"{name}-{theme}.svg").write_text(fn(PAL[theme]), encoding="utf-8")
print("ok")
