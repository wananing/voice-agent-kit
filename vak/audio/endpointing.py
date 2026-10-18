"""Gateway-side endpointing for open mic (spec 01 §2.3; handbook turn-detection).

Chain: VAD says silence for `vad_silence_ms` -> emit `speech_end` and ask
the semantic detector for P(done) over the last seconds of audio -> P >= tau:
`endpoint` once the silence reaches `min_endpoint_ms`; otherwise keep
waiting up to `max_endpoint_ms` then `endpoint` anyway. If the user speaks
again while waiting the pending endpoint is cancelled and `speech_start`
is emitted again. All durations are measured in samples (`ts_samples`),
never wall clock; `now_ms` is accepted so callers can log it but never
drives a decision.
"""

from __future__ import annotations

import enum
from collections import deque
from dataclasses import dataclass
from typing import Literal, Protocol

from vak.core.defaults import EndpointingParams
from vak.core.types import IllegalTransition

from .vad import VadPort


class SemanticTurnDetector(Protocol):
    def predict(self, pcm16_last_n_seconds: bytes, sample_rate: int) -> float:
        """P(user is done) in [0, 1]."""
        ...


class NullSemantic:
    """No semantic model: every pause is a finished turn (pure VAD endpointing)."""

    def predict(self, pcm16_last_n_seconds: bytes, sample_rate: int) -> float:
        return 1.0


@dataclass(frozen=True, slots=True)
class EndpointEvent:
    kind: Literal["speech_start", "speech_end", "endpoint"]
    ts_samples: int
    prob: float | None = None


class EpState(enum.StrEnum):
    IDLE = "idle"  # no speech seen
    SPEAKING = "speaking"  # VAD says speech
    SILENCE = "silence"  # VAD silence shorter than vad_silence_ms
    WAITING = "waiting"  # speech_end emitted, waiting for min/max endpoint


class EpEvent(enum.StrEnum):
    SPEECH = "speech"
    SILENCE = "silence"
    VAD_SILENCE_ELAPSED = "vad_silence_elapsed"
    DEADLINE = "deadline"


_TRANSITIONS: dict[tuple[EpState, EpEvent], EpState] = {
    (EpState.IDLE, EpEvent.SILENCE): EpState.IDLE,
    (EpState.IDLE, EpEvent.SPEECH): EpState.SPEAKING,
    (EpState.SPEAKING, EpEvent.SPEECH): EpState.SPEAKING,
    (EpState.SPEAKING, EpEvent.SILENCE): EpState.SILENCE,
    (EpState.SILENCE, EpEvent.SPEECH): EpState.SPEAKING,
    (EpState.SILENCE, EpEvent.SILENCE): EpState.SILENCE,
    (EpState.SILENCE, EpEvent.VAD_SILENCE_ELAPSED): EpState.WAITING,
    (EpState.WAITING, EpEvent.SPEECH): EpState.SPEAKING,
    (EpState.WAITING, EpEvent.SILENCE): EpState.WAITING,
    (EpState.WAITING, EpEvent.DEADLINE): EpState.IDLE,
}


class Endpointer:
    def __init__(
        self,
        params: EndpointingParams,
        vad: VadPort,
        semantic: SemanticTurnDetector | None,
        sample_rate: int,
        context_seconds: int = 8,  # smart-turn looks at the last 8 s (handbook)
    ) -> None:
        self.params = params
        self.vad = vad
        self.semantic = semantic
        self.sample_rate = sample_rate
        self._ctx_max = sample_rate * context_seconds * 2
        self._ctx: deque[bytes] = deque()
        self._ctx_len = 0
        self.state = EpState.IDLE
        self._silence_start: int = 0  # ts_samples where the current silence began
        self._deadline_samples: int = 0  # silence length (samples) that ends WAITING
        self._prob: float | None = None

    # -- helpers -----------------------------------------------------------

    def _ms(self, ms: int) -> int:
        return self.sample_rate * ms // 1000

    def _fire(self, ev: EpEvent) -> None:
        key = (self.state, ev)
        if key not in _TRANSITIONS:
            raise IllegalTransition(f"endpointer: {self.state.value} + {ev.value}")
        self.state = _TRANSITIONS[key]

    def _remember(self, pcm16: bytes) -> None:
        self._ctx.append(pcm16)
        self._ctx_len += len(pcm16)
        while self._ctx_len > self._ctx_max and self._ctx:
            self._ctx_len -= len(self._ctx.popleft())

    def _context(self) -> bytes:
        return b"".join(self._ctx)

    @property
    def speaking(self) -> bool:
        return self.state is EpState.SPEAKING

    @property
    def endpointing(self) -> bool:
        """True while a `speech_end` is pending a decision (turn.endpointing active)."""
        return self.state is EpState.WAITING

    def reset(self) -> None:
        """Forget the current utterance (turn closed by button, abort, ...)."""
        self.state = EpState.IDLE
        self._ctx.clear()
        self._ctx_len = 0
        self._prob = None

    # -- main entry --------------------------------------------------------

    def feed(self, pcm16: bytes, ts_samples: int, now_ms: int) -> list[EndpointEvent]:
        """Feed one uplink frame starting at `ts_samples`. Returns 0..2 events."""
        del now_ms  # decisions are sample-based (handbook: silence by samples, not wall clock)
        out: list[EndpointEvent] = []
        n_samples = len(pcm16) // 2
        frame_end = ts_samples + n_samples
        self._remember(pcm16)
        speech = self.vad.process(pcm16)

        if speech:
            was = self.state
            self._fire(EpEvent.SPEECH)
            if was in (EpState.IDLE, EpState.WAITING):
                out.append(EndpointEvent("speech_start", ts_samples))
            return out

        if self.state is EpState.SPEAKING:
            self._silence_start = ts_samples
        self._fire(EpEvent.SILENCE)
        if self.state is EpState.IDLE:
            return out

        silence = frame_end - self._silence_start
        if self.state is EpState.SILENCE and silence >= self._ms(self.params.vad_silence_ms):
            self._fire(EpEvent.VAD_SILENCE_ELAPSED)
            sem = self.semantic or NullSemantic()
            self._prob = float(sem.predict(self._context(), self.sample_rate))
            done = self._prob >= self.params.semantic_threshold
            wait_ms = self.params.min_endpoint_ms if done else self.params.max_endpoint_ms
            self._deadline_samples = self._ms(wait_ms)
            out.append(EndpointEvent("speech_end", self._silence_start, self._prob))

        if self.state is EpState.WAITING and silence >= self._deadline_samples:
            self._fire(EpEvent.DEADLINE)
            out.append(EndpointEvent("endpoint", self._silence_start, self._prob))
            self._ctx.clear()
            self._ctx_len = 0
        return out
