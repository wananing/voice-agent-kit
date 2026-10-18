"""Minimal fakes for provider tests: a WebSocket stand-in, an httpx transport
that streams canned bytes/SSE, identity Opus codecs. No network anywhere."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx


class FakeWS:
    """Records what the provider sends; `feed()` queues frames for the reader."""

    def __init__(self, incoming: Iterable[Any] = ()) -> None:
        self.sent: list[Any] = []
        self.closed = False
        self._in: asyncio.Queue[Any] = asyncio.Queue()
        for m in incoming:
            self._in.put_nowait(m)

    async def send(self, data: Any) -> None:
        self.sent.append(data)

    def feed(self, *frames: Any) -> None:
        for f in frames:
            self._in.put_nowait(f)

    def end(self) -> None:
        self._in.put_nowait(None)

    async def recv(self) -> Any:
        m = await self._in.get()
        if m is None:
            raise ConnectionError("closed")
        return m

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        m = await self._in.get()
        if m is None:
            raise StopAsyncIteration
        return m

    async def close(self) -> None:
        self.closed = True

    @property
    def sent_json(self) -> list[dict[str, Any]]:
        return [json.loads(s) for s in self.sent if isinstance(s, str)]


def sse(chunks: Iterable[dict[str, Any] | str]) -> bytes:
    """Encode chat-completion chunks as an SSE body ending with [DONE]."""
    lines = []
    for c in chunks:
        lines.append("data: " + (c if isinstance(c, str) else json.dumps(c)))
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def streaming_transport(body: bytes, *, status: int = 200, chunk: int = 7, headers: dict[str, str] | None = None) -> httpx.MockTransport:
    """An httpx transport whose response body arrives in small pieces."""

    async def gen() -> AsyncIterator[bytes]:
        for i in range(0, len(body), chunk):
            yield body[i : i + chunk]

    def handler(request: httpx.Request) -> httpx.Response:
        handler.requests.append(request)  # type: ignore[attr-defined]
        return httpx.Response(status, stream=_AsyncStream(gen()), headers=headers or {})

    handler.requests = []  # type: ignore[attr-defined]
    return httpx.MockTransport(handler)


class _AsyncStream(httpx.AsyncByteStream):
    def __init__(self, it: AsyncIterator[bytes]) -> None:
        self._it = it

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for b in self._it:
            yield b


class IdentityCodec:
    """`encode`/`decode` pass PCM through, so Ogg framing can be tested
    without libopus."""

    def encode(self, pcm_data: bytes, frame_size: int) -> bytes:
        return pcm_data

    def decode(self, opus_data: bytes, frame_size: int, decode_fec: bool = False) -> bytes:
        return opus_data
