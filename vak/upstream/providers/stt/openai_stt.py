"""OpenAI transcription endpoint as a non-streaming `STT`.

The utterance is buffered and sent as one WAV to `POST /audio/transcriptions`
when `finish()` is called. **No partials are available**: `results()` yields
exactly one final Transcript (or nothing after `cancel()`). Use it when a
cloud cascade is acceptable and partial-driven speculation is not needed.

Config keys: api_key (else $OPENAI_API_KEY), base_url
(default https://api.openai.com/v1), model (default gpt-4o-mini-transcribe),
language, prompt, sample_rate (16000), timeout_s (30).
"""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from collections.abc import AsyncIterator
from typing import Any

import httpx

from vak.upstream.providers._util import cfg, http_client, secret
from vak.upstream.providers.base import Transcript

log = logging.getLogger(__name__)


def pcm16_to_wav(pcm16: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)
    return buf.getvalue()


class OpenAITranscribeSTT:
    def __init__(self, config: dict[str, Any], *, client: httpx.AsyncClient | None = None) -> None:
        self.config = dict(config)
        self.sample_rate: int = int(cfg(config, "sample_rate", 16000))
        self.language: str | None = config.get("language")
        self.model: str = str(cfg(config, "model", "gpt-4o-mini-transcribe"))
        self._api_key = secret(config, "api_key", "OPENAI_API_KEY", "openai_stt")
        self._base = str(cfg(config, "base_url", "https://api.openai.com/v1")).rstrip("/")
        self._client = client
        self._own_client = client is None
        self._buf = bytearray()
        self._queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._buf.clear()
        if self._client is None:
            self._client = http_client(float(cfg(self.config, "timeout_s", 30)))

    async def feed(self, pcm16: bytes) -> None:
        self._buf.extend(pcm16)

    async def finish(self) -> None:
        self._task = asyncio.create_task(self._transcribe(bytes(self._buf)))

    async def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
        await self._queue.put(None)
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def results(self) -> AsyncIterator[Transcript]:
        return self._results()

    async def _results(self) -> AsyncIterator[Transcript]:
        while True:
            t = await self._queue.get()
            if t is None:
                return
            yield t

    async def _transcribe(self, pcm16: bytes) -> None:
        assert self._client is not None
        data: dict[str, Any] = {"model": self.model, "response_format": "json"}
        if self.language:
            data["language"] = self.language
        if self.config.get("prompt"):
            data["prompt"] = self.config["prompt"]
        try:
            r = await self._client.post(
                f"{self._base}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                data=data,
                files={"file": ("utterance.wav", pcm16_to_wav(pcm16, self.sample_rate), "audio/wav")},
            )
            r.raise_for_status()
            text = str(r.json().get("text", ""))
        except (httpx.HTTPError, ValueError) as e:
            # finish() must still produce a final (spec 04 §3.3); the pipeline
            # reports the empty final as no_speech / asr failure.
            log.warning("openai_stt: transcription failed: %s", e)
            text = ""
        await self._queue.put(Transcript(text=text, final=True, segment=0))
        await self._queue.put(None)
