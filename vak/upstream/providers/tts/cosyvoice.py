"""CosyVoice runtime FastAPI server client (`TTS`).

Server: CosyVoice/runtime/python/fastapi/server.py exposes
`/inference_sft` (tts_text, spk_id), `/inference_zero_shot` (tts_text,
prompt_text, prompt_wav file), `/inference_cross_lingual` (tts_text,
prompt_wav) and `/inference_instruct2` (tts_text, instruct_text, prompt_wav),
all as form-data GET/POST returning a chunked `StreamingResponse` of raw
PCM16 (`generate_data`: `tts_speech * 2**15 -> int16`). The native rate is
24 kHz for CosyVoice2/3 and 22.05 kHz for CosyVoice1 (handbook
04-projects/tts/cosyvoice.md); declare it with `sample_rate`.

Limits (documented, not worked around here):
- `supports_text_stream=False`: the HTTP server takes one complete string
  per request; bistream text input exists only in the Python API
  (`Qwen2LM.inference_bistream`) and cannot be used with vLLM. One call per
  sentence.
- The server's `inference_*` calls are not opened with `stream=True`, so the
  first chunk arrives after the whole segment is synthesised; chunks still
  stream over HTTP. For real first-chunk latency wrap the Python API or use
  Triton.
- No cancel API: `flush()` sets a stop flag; the generator stops yielding
  and closes the response, remaining chunks are dropped locally. The
  server's `llm_job` thread keeps generating to the end.

Config keys: url (default http://localhost:50000), mode (sft | zero_shot |
cross_lingual | instruct2; default sft), voice/spk_id (default 中文女),
prompt_text, prompt_wav (path), instruct_text, sample_rate (24000),
chunk_bytes (9600 = 200 ms at 24 kHz), timeout_s (60).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from vak.upstream.providers._util import ProviderConfigError, cfg, http_client
from vak.upstream.providers.base import TTSChunk

log = logging.getLogger(__name__)


class CosyVoiceTTS:
    supports_text_stream = False

    def __init__(self, config: dict[str, Any], *, client: httpx.AsyncClient | None = None) -> None:
        self.config = dict(config)
        self.sample_rate: int = int(cfg(config, "sample_rate", 24000))
        self.voice: str = str(config.get("voice") or config.get("spk_id") or "中文女")
        self.mode: str = str(cfg(config, "mode", "sft"))
        if self.mode not in ("sft", "zero_shot", "cross_lingual", "instruct2"):
            raise ProviderConfigError(f"cosyvoice: unknown mode {self.mode!r}")
        if self.mode != "sft" and not config.get("prompt_wav"):
            raise ProviderConfigError(f"cosyvoice: mode {self.mode!r} requires config 'prompt_wav'")
        self._url = str(cfg(config, "url", "http://localhost:50000")).rstrip("/")
        self._chunk = int(cfg(config, "chunk_bytes", 9600))
        self._client = client or http_client(float(cfg(config, "timeout_s", 60)))
        self._stopped: set[str] = set()
        self._prompt_wav: bytes | None = None

    def _request(self, text: str) -> tuple[str, dict[str, str], dict[str, Any] | None]:
        data: dict[str, str] = {"tts_text": text}
        files: dict[str, Any] | None = None
        if self.mode == "sft":
            data["spk_id"] = self.voice
        else:
            if self._prompt_wav is None:
                self._prompt_wav = Path(str(self.config["prompt_wav"])).read_bytes()
            files = {"prompt_wav": ("prompt_wav", self._prompt_wav, "application/octet-stream")}
            if self.mode == "zero_shot":
                data["prompt_text"] = str(cfg(self.config, "prompt_text", ""))
            elif self.mode == "instruct2":
                data["instruct_text"] = str(cfg(self.config, "instruct_text", ""))
        return f"{self._url}/inference_{self.mode}", data, files

    async def synthesize(self, text: str, request_id: str) -> AsyncIterator[TTSChunk]:
        self._stopped.discard(request_id)
        url, data, files = self._request(text)
        async with self._client.stream("POST", url, data=data, files=files) as r:
            if r.status_code >= 400:
                await r.aread()
                raise RuntimeError(f"cosyvoice: HTTP {r.status_code}: {r.text[:200]}")
            pending = b""
            async for raw in r.aiter_bytes(self._chunk):
                if request_id in self._stopped:
                    break  # tts_flush: drop the rest locally
                if pending:
                    yield TTSChunk(pcm16=pending, sample_rate=self.sample_rate)
                pending = raw
            if request_id in self._stopped:
                return
            # trailing odd byte would corrupt PCM16 alignment
            if len(pending) % 2:
                pending = pending[:-1]
            yield TTSChunk(pcm16=pending, sample_rate=self.sample_rate, is_last=True)

    async def flush(self, request_id: str) -> None:
        self._stopped.add(request_id)

    async def close(self) -> None:
        await self._client.aclose()
