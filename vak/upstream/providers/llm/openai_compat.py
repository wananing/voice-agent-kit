"""OpenAI-compatible streaming chat completions (`LLM`).

Works with any `/v1/chat/completions` server speaking the OpenAI SSE format
(OpenAI, vLLM, Ollama, DashScope compatible mode, ...). Tool calls arrive as
streamed argument fragments keyed by `index`; they are assembled here and
reported once, complete, only when the arguments parse as JSON (spec 04 §2.2.5
`tools.args_streaming`, rule 10). Usage is taken from the final chunk when
`stream_options.include_usage` is honoured.

Config keys: api_key (else $LLM_API_KEY, then $OPENAI_API_KEY; optional for
local servers), base_url (default https://api.openai.com/v1), model (required),
temperature, include_usage (True), extra_body (dict merged into the request),
timeout_s (60).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from vak.upstream.base import ToolDef
from vak.upstream.providers._util import cfg, http_client, require, warm_http
from vak.upstream.providers.base import ChatMessage, LLMDelta

log = logging.getLogger(__name__)


class ToolCallAssembler:
    """Accumulates streamed `tool_calls` fragments; `pop_complete()` returns
    calls whose arguments parse as a JSON object."""

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, Any]] = {}
        self._done: set[int] = set()

    def add(self, fragments: list[dict[str, Any]]) -> None:
        for frag in fragments:
            idx = int(frag.get("index", 0))
            call = self._calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
            if frag.get("id"):
                call["id"] = frag["id"]
            fn = frag.get("function") or {}
            if fn.get("name"):
                call["name"] = (call["name"] or "") + fn["name"] if call["name"] is None else call["name"]
            if fn.get("arguments"):
                call["arguments"] += fn["arguments"]

    def pop_complete(self, *, final: bool = False, except_index: int | None = None) -> list[dict[str, Any]]:
        """Return calls that are complete. Mid-stream, a call is complete when
        its JSON parses and a later index has started (`except_index` is the
        one still streaming); at `final` every remaining call is tried."""
        out: list[dict[str, Any]] = []
        for idx in sorted(self._calls):
            if idx in self._done or (not final and idx == except_index):
                continue
            call = self._calls[idx]
            args_text = call["arguments"] or "{}"
            try:
                args = json.loads(args_text)
            except ValueError:
                if final:
                    log.warning("openai_compat: dropping tool call %r with unparseable arguments", call["name"])
                    self._done.add(idx)
                continue
            if not isinstance(args, dict):
                if final:
                    self._done.add(idx)
                continue
            if not final and idx == max(self._calls):
                continue  # last index may still receive fragments
            self._done.add(idx)
            out.append({"id": call["id"] or f"call_{idx}", "name": call["name"] or "", "arguments": args})
        return out

    @property
    def current_index(self) -> int | None:
        return max(self._calls) if self._calls else None


def encode_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        d: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.role == "tool" and m.tool_call_id:
            d["tool_call_id"] = m.tool_call_id
        if m.role == "assistant" and m.tool_calls:
            d["tool_calls"] = m.tool_calls
            if not m.content:
                d["content"] = None
        if m.name:
            d["name"] = m.name
        out.append(d)
    return out


def encode_tools(tools: list[ToolDef]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.params}}
        for t in tools
    ]


class OpenAICompatLLM:
    def __init__(self, config: dict[str, Any], *, client: httpx.AsyncClient | None = None) -> None:
        self.config = dict(config)
        self.model: str = str(require(config, "model", "openai_compat"))
        self._base = str(cfg(config, "base_url", "https://api.openai.com/v1")).rstrip("/")
        self._api_key = config.get("api_key") or os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self._client = client or http_client(float(cfg(config, "timeout_s", 60)))
        self._response: httpx.Response | None = None
        self._cancelled = False

    def _body(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDef] | None,
        tool_choice: str | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": encode_messages(messages), "stream": True}
        if cfg(self.config, "include_usage", True):
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = encode_tools(tools)
            if tool_choice:
                body["tool_choice"] = (
                    tool_choice
                    if tool_choice in ("auto", "none", "required")
                    else {"type": "function", "function": {"name": tool_choice}}
                )
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if self.config.get("temperature") is not None:
            body["temperature"] = self.config["temperature"]
        body.update(cfg(self.config, "extra_body", {}))
        return body

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolDef] | None = None,
        tool_choice: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMDelta]:
        self._cancelled = False
        headers = {"Accept": "text/event-stream"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = self._client.build_request(
            "POST", f"{self._base}/chat/completions", json=self._body(messages, tools, tool_choice, max_tokens), headers=headers
        )
        self._response = await self._client.send(req, stream=True)
        try:
            if self._response.status_code >= 400:
                await self._response.aread()
                raise RuntimeError(f"openai_compat: HTTP {self._response.status_code}: {self._response.text[:300]}")
            async for delta in self._parse_sse(self._response.aiter_lines()):
                yield delta
        finally:
            await self._response.aclose()
            self._response = None

    async def _parse_sse(self, lines: AsyncIterator[str]) -> AsyncIterator[LLMDelta]:
        asm = ToolCallAssembler()
        usage: dict[str, int] | None = None
        finish: str | None = None
        async for line in lines:
            if self._cancelled:
                return
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                d = choice.get("delta") or {}
                if d.get("content"):
                    yield LLMDelta(text=d["content"])
                if d.get("tool_calls"):
                    asm.add(d["tool_calls"])
                    for call in asm.pop_complete(except_index=asm.current_index):
                        yield LLMDelta(tool_call=call)
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        for call in asm.pop_complete(final=True):
            yield LLMDelta(tool_call=call)
        yield LLMDelta(done=True, finish_reason=finish, usage=_usage(usage))

    async def warmup(self) -> None:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
        await warm_http(self._client, f"{self._base}/models", headers)

    async def cancel(self) -> None:
        self._cancelled = True
        if self._response is not None:
            await self._response.aclose()

    async def close(self) -> None:
        await self._client.aclose()


def _usage(u: dict[str, Any] | None) -> dict[str, int] | None:
    if not u:
        return None
    out: dict[str, int] = {}
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if isinstance(u.get(k), int):
            out[k] = u[k]
    details = u.get("prompt_tokens_details") or {}
    if isinstance(details.get("cached_tokens"), int):
        out["cached_tokens"] = details["cached_tokens"]
    return out or None
