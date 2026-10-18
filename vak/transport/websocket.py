"""va/1 over one server-side WebSocket connection (spec 01 §3.0).

Text frames carry JSON control messages, binary frames carry the 16-byte
header plus one encoded audio frame. All protocol logic lives in
`BaseTransport`; this class only adapts the `websockets` connection.
"""

from __future__ import annotations

from typing import Any

from websockets.exceptions import ConnectionClosed

from vak.transport.base import BaseTransport


class WebSocketTransport(BaseTransport):
    def __init__(self, ws: Any, **kw: Any) -> None:
        """`ws` is a `websockets` server connection (asyncio implementation)."""
        super().__init__(**kw)
        self._ws = ws

    async def _raw_send(self, data: str | bytes) -> None:
        await self._ws.send(data)

    async def _raw_recv(self) -> str | bytes | None:
        try:
            return await self._ws.recv()
        except ConnectionClosed:
            return None

    async def _raw_close(self) -> None:
        await self._ws.close()
