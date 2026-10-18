"""OpenAI `/audio/speech` streaming TTS (`TTS`).

One request per sentence with `response_format=pcm` (PCM16 mono 24 kHz).
`flush()` closes the HTTP stream for that request; the rest is dropped.
`supports_text_stream=False` (no incremental text input).

Config keys: api_key (else $OPENAI_API_KEY), base_url
(default https://api.openai.com/v1), model (default gpt-4o-mini-tts),
voice (default alloy), instructions (voice style, gpt-4o-mini-tts only),
speed, chunk_bytes (9600 = 200 ms), timeout_s (60).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from vak.upstream.providers._util import cfg, http_client, secret, warm_http
from vak.upstream.providers.base import TTSChunk


class OpenAITTS:
    supports_text_stream = False
    sample_rate = 24000

    def __init__(self, config: dict[str, Any], *, client: httpx.AsyncClient | None = None) -> None:
        self.config = dict(config)
        self.voice: str = str(cfg(config, "voice", "alloy"))
        self.model: str = str(cfg(config, "model", "gpt-4o-mini-tts"))
        self._api_key = secret(config, "api_key", "OPENAI_API_KEY", "openai_tts")
        self._base = str(cfg(config, "base_url", "https://api.openai.com/v1")).rstrip("/")
        self._chunk = int(cfg(config, "chunk_bytes", 9600))
        self._client = client or http_client(float(cfg(config, "timeout_s", 60)))
        self._stopped: set[str] = set()

    def _body(self, text: str) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "input": text, "voice": self.voice, "response_format": "pcm"}
        for k in ("instructions", "speed"):
            if self.config.get(k) is not None:
                body[k] = self.config[k]
        return body

    async def synthesize(self, text: str, request_id: str) -> AsyncIterator[TTSChunk]:
        self._stopped.discard(request_id)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with self._client.stream("POST", f"{self._base}/audio/speech", json=self._body(text), headers=headers) as r:
            if r.status_code >= 400:
                await r.aread()
                raise RuntimeError(f"openai_tts: HTTP {r.status_code}: {r.text[:200]}")
            pending = b""
            async for raw in r.aiter_bytes(self._chunk):
                if request_id in self._stopped:
                    break
                if pending:
                    yield TTSChunk(pcm16=pending, sample_rate=self.sample_rate)
                pending = raw
            if request_id in self._stopped:
                return
            if len(pending) % 2:
                pending = pending[:-1]
            yield TTSChunk(pcm16=pending, sample_rate=self.sample_rate, is_last=True)

    async def warmup(self) -> None:
        await warm_http(self._client, f"{self._base}/models", {"Authorization": f"Bearer {self._api_key}"})

    async def flush(self, request_id: str) -> None:
        self._stopped.add(request_id)

    async def close(self) -> None:
        await self._client.aclose()
