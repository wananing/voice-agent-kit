"""WebSocket gateway: one `Session` per client, resume within the resume
window (spec 01 §6.2), static hosting of the reference web client.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from http import HTTPStatus
from pathlib import Path
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.http11 import Response

from vak.core.clock import Clock, MonotonicClock
from vak.core.ids import IdGen
from vak.core.session import Session, SessionParts
from vak.transport.base import HandshakeFailed, ResumeState
from vak.transport.protocol import ResumeRequest
from vak.transport.websocket import WebSocketTransport

log = logging.getLogger(__name__)

WEB_CLIENT_DIR = Path(__file__).resolve().parents[2] / "clients" / "web"


class Gateway:
    def __init__(
        self,
        parts: SessionParts,
        *,
        host: str = "0.0.0.0",
        port: int = 8765,
        clock: Clock | None = None,
        serve_web_client: bool = True,
        ws_path: str = "/ws",
    ) -> None:
        self.parts = parts
        self.host = host
        self.port = port
        self.clock = clock or MonotonicClock()
        self.serve_web_client = serve_web_client
        self.ws_path = ws_path
        self._ids = IdGen()
        self._sessions: dict[str, Session] = {}
        self._detached: dict[str, tuple[Session, ResumeState, int]] = {}  # session_id -> (session, state, expires_at)
        self._server: Server | None = None

    # ------------------------------------------------------------------ serve

    async def serve(self) -> None:
        async with serve(
            self._handle,
            self.host,
            self.port,
            process_request=self._process_request,
            max_size=2**20,
        ) as server:
            self._server = server
            log.info("vak gateway listening on ws://%s:%d%s", self.host, self.port, self.ws_path)
            await asyncio.Future()  # run until cancelled

    def _process_request(self, connection: ServerConnection, request: Any) -> Response | None:
        path = request.path.split("?", 1)[0]
        if path == self.ws_path:
            return None  # upgrade to WebSocket
        if not self.serve_web_client:
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        file = (WEB_CLIENT_DIR / rel).resolve()
        if not str(file).startswith(str(WEB_CLIENT_DIR)) or not file.is_file():
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if file.suffix in (".js", ".mjs"):
            ctype = "text/javascript"
        resp = Response(HTTPStatus.OK, "OK", _headers({"Content-Type": ctype}), file.read_bytes())
        return resp

    # ------------------------------------------------------------------ connections

    async def _handle(self, ws: ServerConnection) -> None:
        session_id = self._ids.new("S")
        transport = WebSocketTransport(
            ws,
            clock=self.clock,
            timers=self.parts.defaults.turn,
            ids=IdGen(),
            session_id=session_id,
            mode=self.parts.mode,
            resume_lookup=self._resume_lookup,
        )
        try:
            await transport.handshake()
        except HandshakeFailed as e:
            log.info("handshake failed: %s", e)
            await transport.close("handshake_failed")
            return
        session: Session
        if transport.resumed and transport.session_id in self._detached:
            session, _, _ = self._detached.pop(transport.session_id)
            log.info("session %s resumed", session.session_id)
        else:
            session = Session(parts=self.parts, clock=self.clock, session_id=transport.session_id)
        self._sessions[session.session_id] = session
        try:
            await session.attach(transport)
            await session.run(transport)
        except Exception:  # noqa: BLE001
            log.exception("session %s failed", session.session_id)
        finally:
            self._sessions.pop(session.session_id, None)
            await self._detach(session)

    async def _detach(self, session: Session) -> None:
        """Keep the session for the resume window, then close it."""
        state = session.resume_state()
        window = self.parts.defaults.turn.resume_window
        if state is None or session.closed:
            await session.close("disconnect")
            return
        expires = self.clock.now_ms() + window
        self._detached[session.session_id] = (session, state, expires)
        asyncio.get_running_loop().create_task(self._expire(session.session_id, window))

    async def _expire(self, session_id: str, window_ms: int) -> None:
        await self.clock.sleep(window_ms)
        entry = self._detached.get(session_id)
        if entry is None:
            return
        session, _, expires = entry
        if self.clock.now_ms() < expires:
            return
        self._detached.pop(session_id, None)
        await session.close("resume_window_expired")

    def _resume_lookup(self, req: ResumeRequest) -> ResumeState | None:
        entry = self._detached.get(req.session_id)
        if entry is None:
            return None
        _, state, expires = entry
        if self.clock.now_ms() >= expires:
            return None
        return state

    async def close(self) -> None:
        for s in list(self._sessions.values()):
            await s.close("gateway_shutdown")
        for s, _, _ in list(self._detached.values()):
            await s.close("gateway_shutdown")
        self._detached.clear()
        if self._server is not None:
            self._server.close()


def _headers(d: dict[str, str]) -> Any:
    from websockets.datastructures import Headers

    h = Headers()
    for k, v in d.items():
        h[k] = v
    return h
