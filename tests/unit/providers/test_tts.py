"""CosyVoice / OpenAI TTS chunk parsing against a fake httpx transport."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from vak.upstream.providers._util import ProviderConfigError
from vak.upstream.providers.tts.cosyvoice import CosyVoiceTTS
from vak.upstream.providers.tts.openai_tts import OpenAITTS

from .fakes import streaming_transport

PCM = bytes(range(256)) * 40  # 10240 bytes of "audio"


def cosy(body: bytes = PCM, *, chunk_bytes: int = 4096, status: int = 200, **cfg):
    transport = streaming_transport(body, status=status, chunk=1000)
    client = httpx.AsyncClient(transport=transport)
    return CosyVoiceTTS({"url": "http://tts:50000", "voice": "中文女", "chunk_bytes": chunk_bytes, **cfg}, client=client), transport


async def test_cosyvoice_sft_request_and_chunks():
    tts, transport = cosy()
    chunks = [c async for c in tts.synthesize("你好", "req-1")]
    req = transport.handler.requests[0]
    form = parse_qs(req.content.decode())
    assert req.url.path == "/inference_sft" and form == {"tts_text": ["你好"], "spk_id": ["中文女"]}
    assert b"".join(c.pcm16 for c in chunks) == PCM
    assert all(c.sample_rate == 24000 for c in chunks)
    assert [c.is_last for c in chunks] == [False] * (len(chunks) - 1) + [True]
    assert tts.supports_text_stream is False and tts.sample_rate == 24000


async def test_cosyvoice_declared_rate_for_cv1():
    tts, _ = cosy(sample_rate=22050)
    chunks = [c async for c in tts.synthesize("x", "r")]
    assert chunks[0].sample_rate == 22050


async def test_cosyvoice_flush_drops_remaining_chunks():
    tts, _ = cosy(chunk_bytes=1024)
    got = []
    async for c in tts.synthesize("长句子", "req-2"):
        got.append(c)
        if len(got) == 2:
            await tts.flush("req-2")
    assert len(got) == 2 and not any(c.is_last for c in got)
    # a new request with another id is unaffected
    again = [c async for c in tts.synthesize("再来", "req-3")]
    assert again[-1].is_last


async def test_cosyvoice_zero_shot_sends_prompt_wav(tmp_path):
    wav = tmp_path / "p.wav"
    wav.write_bytes(b"RIFFfake")
    transport = streaming_transport(PCM, chunk=5000)
    tts = CosyVoiceTTS(
        {"url": "http://tts:50000", "mode": "zero_shot", "prompt_wav": str(wav), "prompt_text": "参考文本"},
        client=httpx.AsyncClient(transport=transport),
    )
    [c async for c in tts.synthesize("x", "r")]
    req = transport.handler.requests[0]
    assert req.url.path == "/inference_zero_shot" and b"RIFFfake" in req.content and "参考文本".encode() in req.content
    assert b'name="prompt_wav"' in req.content  # multipart, like CosyVoice client.py


def test_cosyvoice_config_errors():
    with pytest.raises(ProviderConfigError, match="prompt_wav"):
        CosyVoiceTTS({"mode": "zero_shot"}, client=httpx.AsyncClient())
    with pytest.raises(ProviderConfigError, match="mode"):
        CosyVoiceTTS({"mode": "bistream"}, client=httpx.AsyncClient())


async def test_cosyvoice_http_error():
    tts, _ = cosy(b"nope", status=500)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        [c async for c in tts.synthesize("x", "r")]


async def test_openai_tts_request_pcm_and_chunks():
    transport = streaming_transport(PCM, chunk=3000)
    tts = OpenAITTS({"api_key": "k", "voice": "marin", "chunk_bytes": 4096}, client=httpx.AsyncClient(transport=transport))
    chunks = [c async for c in tts.synthesize("Hello", "r1")]
    req = transport.handler.requests[0]
    import json

    body = json.loads(req.content)
    assert req.url.path == "/v1/audio/speech" and body == {"model": "gpt-4o-mini-tts", "input": "Hello", "voice": "marin", "response_format": "pcm"}
    assert req.headers["authorization"] == "Bearer k"
    assert b"".join(c.pcm16 for c in chunks) == PCM and chunks[-1].is_last and chunks[0].sample_rate == 24000


async def test_openai_tts_flush_closes_stream():
    transport = streaming_transport(PCM, chunk=500)
    tts = OpenAITTS({"api_key": "k", "chunk_bytes": 512}, client=httpx.AsyncClient(transport=transport))
    got = []
    async for c in tts.synthesize("Hello", "r1"):
        got.append(c)
        await tts.flush("r1")
    assert len(got) == 1


def test_openai_tts_requires_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderConfigError, match="OPENAI_API_KEY"):
        OpenAITTS({}, client=httpx.AsyncClient())


async def test_openai_tts_warmup_preconnects_and_swallows_errors():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(401)

    tts = OpenAITTS({"api_key": "k"}, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await tts.warmup()
    assert seen[0].method == "GET" and seen[0].url.path == "/v1/models"
    assert seen[0].headers["authorization"] == "Bearer k"

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    tts = OpenAITTS({"api_key": "k"}, client=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    await tts.warmup()  # a failed warmup never surfaces


def test_default_clients_keep_idle_connections_for_the_reuse_window():
    from vak.upstream.providers._util import KEEPALIVE_S

    tts = OpenAITTS({"api_key": "k"})
    assert tts._client._transport._pool._keepalive_expiry == KEEPALIVE_S == 60.0
