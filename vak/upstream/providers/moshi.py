"""moshi full-duplex client (`FullDuplexModel`).

Protocol (moshi/rust/protocol.md; moshi/moshi/moshi/client.py, server.py):
WebSocket at `<url>/api/chat`, binary frames with a one-byte type prefix:
0 handshake (server -> client, first frame), 1 audio (Ogg/Opus 24 kHz mono,
both directions), 2 text (UTF-8, moshi's inner monologue), 3 control (unused
in full streaming mode), 4 metadata JSON, 5 error, 6 ping. The server drops
every non-audio uplink frame, so there is no system prompt, no tools, no
history and no truncation (handbook 04-projects/full-duplex/moshi.md).

Opus goes through `opuslib` (lazy import; extra `opus`, needs libopus) plus
the tiny Ogg muxer in `_ogg_opus.py`. moshi is English-only; one Python
server instance serves one connection at a time; sessions end after ~5 min.

Config keys: url (default ws://localhost:8998), path (default /api/chat),
headers, frame_ms (default 20), connect_timeout_ms (default 10000).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from vak.core.clock import Clock, MonotonicClock
from vak.upstream.providers._ogg_opus import OggOpusReader, OggOpusWriter, OpusDecoderLike, OpusEncoderLike
from vak.upstream.providers._util import ProviderConfigError, cfg, lazy_import
from vak.upstream.providers.base import DuplexFrame

log = logging.getLogger(__name__)

MT_HANDSHAKE, MT_AUDIO, MT_TEXT, MT_CONTROL, MT_METADATA, MT_ERROR, MT_PING = range(7)


class MoshiError(RuntimeError):
    """Server-side error frame (MT=5)."""


class MoshiClient:
    sample_rate_in = 24000
    sample_rate_out = 24000

    def __init__(
        self,
        config: dict[str, Any],
        *,
        clock: Clock | None = None,
        codec: tuple[OpusEncoderLike, OpusDecoderLike] | None = None,
    ) -> None:
        self.config = dict(config)
        self.clock = clock or MonotonicClock()
        self._codec = codec
        self._ws: Any = None
        self._writer: OggOpusWriter | None = None
        self._reader: OggOpusReader | None = None
        self._handshake: asyncio.Future[bytes] | None = None

    # ------------------------------------------------------------------

    def _make_codec(self) -> tuple[OpusEncoderLike, OpusDecoderLike]:
        if self._codec is not None:
            return self._codec
        opuslib = lazy_import("opuslib", provider="moshi", extra="voice-agent-kit[opus]")
        try:
            enc = opuslib.Encoder(self.sample_rate_in, 1, opuslib.APPLICATION_VOIP)
            dec = opuslib.Decoder(self.sample_rate_out, 1)
        except Exception as e:  # libopus missing or unusable
            raise ProviderConfigError(f"moshi: opuslib could not initialise libopus: {e}") from e
        return enc, dec

    async def _ws_connect(self, url: str, headers: dict[str, str]) -> Any:
        from websockets.asyncio.client import connect

        return await connect(url, additional_headers=headers, max_size=None)

    async def connect(self, config: dict[str, Any]) -> None:
        merged = {**self.config, **(config or {})}
        enc, dec = self._make_codec()
        self._writer = OggOpusWriter(enc, self.sample_rate_in, frame_ms=int(cfg(merged, "frame_ms", 20)))
        self._reader = OggOpusReader(dec, self.sample_rate_out)
        url = cfg(merged, "url", "ws://localhost:8998").rstrip("/") + cfg(merged, "path", "/api/chat")
        self._ws = await self._ws_connect(url, dict(cfg(merged, "headers", {})))
        # The server speaks first (MT=0). Wait for it with the injected clock.
        timeout = int(cfg(merged, "connect_timeout_ms", 10_000))
        first = asyncio.ensure_future(self._ws.recv())
        sleeper = asyncio.ensure_future(self.clock.sleep(timeout))
        done, _ = await asyncio.wait({first, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        if first not in done:
            first.cancel()
            raise TimeoutError("moshi: no handshake from server")
        sleeper.cancel()
        msg = first.result()
        if not (isinstance(msg, bytes) and msg and msg[0] == MT_HANDSHAKE):
            raise MoshiError(f"moshi: expected handshake, got {msg[:1]!r}")

    async def send(self, pcm16: bytes) -> None:
        if self._ws is None or self._writer is None:
            raise RuntimeError("moshi: not connected")
        data = self._writer.append_pcm(pcm16)
        if data:
            await self._ws.send(bytes([MT_AUDIO]) + data)

    def receive(self) -> AsyncIterator[DuplexFrame]:
        return self._receive()

    async def _receive(self) -> AsyncIterator[DuplexFrame]:
        if self._ws is None:
            raise RuntimeError("moshi: not connected")
        async for msg in self._ws:
            if not isinstance(msg, bytes):
                continue
            frame = self.parse_frame(msg)
            if frame is not None:
                yield frame

    def parse_frame(self, msg: bytes) -> DuplexFrame | None:
        """Decode one downlink frame. Returns None for frames without
        audio/text (handshake, metadata, ping, unknown)."""
        if not msg:
            return None
        kind, payload = msg[0], msg[1:]
        if kind == MT_AUDIO:
            if self._reader is None:
                raise RuntimeError("moshi: not connected")
            pcm = self._reader.append_bytes(payload)
            return DuplexFrame(pcm16=pcm, sample_rate=self.sample_rate_out) if pcm else None
        if kind == MT_TEXT:
            return DuplexFrame(text=payload.decode("utf-8", "replace"))
        if kind == MT_ERROR:
            raise MoshiError(payload.decode("utf-8", "replace"))
        if kind not in (MT_HANDSHAKE, MT_CONTROL, MT_METADATA, MT_PING):
            log.warning("moshi: unknown message kind %d", kind)
        return None

    async def disconnect(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None
