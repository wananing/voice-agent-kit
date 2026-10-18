"""OpenAI Realtime API provider (spec 04 §2.3 table row "OpenAI Realtime").

`OpenAIRealtime` implements `RealtimeS2S` over WebSocket. `OpenAIRealtimeAudioLLM`
wraps the same connection in text-only output mode for the half-cascade
(`modalities=["text"]`, spec 04 §6 / LK-B §4).

Config keys (all optional unless noted):
    api_key            else $OPENAI_API_KEY (required)
    base_url           default wss://api.openai.com/v1/realtime (any compatible server)
    model              default gpt-realtime
    dialect            "ga" (default) | "beta" (older *-realtime-preview models / compat servers)
    voice, instructions, tools, modalities, turn_detection (None disables server VAD),
    input_audio_transcription, history (list[HistoryItem]), connect_timeout_ms
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from vak.core.clock import Clock
from vak.core.types import ErrorClass, ErrorReason, ErrorScope, HistoryItem
from vak.upstream.base import EventType, ToolDef
from vak.upstream.providers.base import AudioTurn, ChatMessage, LLMDelta
from vak.upstream.providers.realtime_common import CodeEntry, RealtimeSessionBase, rules

_F, _T, _B, _C = ErrorClass.FATAL, ErrorClass.TRANSIENT, ErrorClass.BENIGN, ErrorClass.CONTEXT_OVERFLOW
_S, _R, _I = ErrorScope.SESSION, ErrorScope.RESPONSE, ErrorScope.ITEM

# Stable codes seen in open-source OpenAI Realtime clients (spec 04 §4.6 rules
# 1/4/5; PC-B §2.2; LK-B §2.2, §3.2). Kept in sync with capabilities/openai_realtime.yaml.
OPENAI_CODE_MAP: dict[str, CodeEntry] = {
    "invalid_api_key": (_F, ErrorReason.AUTH, _S),
    "insufficient_quota": (_F, ErrorReason.QUOTA, _S),
    "session_expired": (_T, ErrorReason.SESSION_LOST, _S),
    "rate_limit_exceeded": (_T, ErrorReason.THROTTLED, _S),
    "server_error": (_T, ErrorReason.UNKNOWN, _S),
    "response_cancel_not_active": (_B, ErrorReason.CANCEL_RACE, _R),
    "conversation_already_has_active_response": (_B, ErrorReason.CANCEL_RACE, _R),
    "input_audio_buffer_commit_empty": (_B, ErrorReason.EMPTY_COMMIT, _I),
    "item_create_duplicate_item_id": (_B, ErrorReason.DUPLICATE, _I),
    "item_truncate_invalid_audio_end_ms": (_B, ErrorReason.ITEM_REJECTED, _I),
}

OPENAI_MESSAGE_RULES = rules(
    (r"invalid[_ -]?api[_ -]?key|authentication|unauthorized|forbidden|401|403", _F, ErrorReason.AUTH, _S),
    (r"quota", _F, ErrorReason.QUOTA, _S),
    (r"billing", _F, ErrorReason.BILLING, _S),
    (r"model[_ -]?not[_ -]?found|does not exist", _F, ErrorReason.INVALID_CONFIG, _S),
    (r"context.*(length|too long|exceed)|maximum context", _C, ErrorReason.UNKNOWN, _S),
    (r"another response is in progress|already has an active response", _B, ErrorReason.CANCEL_RACE, _R),
    (r"no (active|ongoing) response|cancellation failed", _B, ErrorReason.CANCEL_RACE, _R),
    (r"content[_ -]?(filter|moderation|safety|policy)|policy violation", _B, ErrorReason.MODERATION, _R),
    (r"rate limit", _T, ErrorReason.THROTTLED, _S),
    (r"timed? ?out", _T, ErrorReason.TIMEOUT, _S),
)


class OpenAIRealtime(RealtimeSessionBase):
    provider_name = "openai_realtime"
    default_base_url = "wss://api.openai.com/v1/realtime"
    default_model = "gpt-realtime"
    api_key_env = "OPENAI_API_KEY"
    dialect = "ga"
    input_sample_rate = 24000  # pcm16 24 kHz on the wire; append_audio resamples 16k -> 24k
    output_sample_rate = 24000
    default_turn_detection = {"type": "server_vad", "threshold": 0.9, "silence_duration_ms": 500}
    default_transcription = {"model": "gpt-4o-mini-transcribe"}
    code_map = OPENAI_CODE_MAP
    message_rules = OPENAI_MESSAGE_RULES

    def __init__(
        self,
        config: dict[str, Any],
        *,
        clock: Clock | None = None,
        is_committed: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(config, clock=clock, is_committed=is_committed)
        d = self.config.get("dialect")
        if d in ("beta", "ga"):
            self.dialect = d


class OpenAIRealtimeAudioLLM:
    """`AudioLLM` over an OpenAI Realtime session with text-only output.

    One `stream()` call = one response. The first call connects (system
    message -> instructions, earlier messages -> history seeded at connect),
    later calls reuse the session; the server keeps the conversation so only
    the new audio turn is sent.
    """

    streaming_input = True

    def __init__(self, config: dict[str, Any], *, clock: Clock | None = None) -> None:
        self.model: str = str(config.get("model") or OpenAIRealtime.default_model)
        self._rt = OpenAIRealtime({**config, "modalities": ["text"]}, clock=clock)
        self._connected = False
        self._lock = asyncio.Lock()

    @property
    def session(self) -> OpenAIRealtime:
        return self._rt

    async def _ensure_connected(self, messages: list[ChatMessage], tools: list[ToolDef] | None) -> None:
        if self._connected:
            return
        system = "\n".join(m.content for m in messages if m.role == "system")
        history: list[HistoryItem] = []
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "tool":
                history.append(HistoryItem(role="tool_output", text=m.content, tool_call_id=m.tool_call_id))
            elif m.role == "assistant" and m.tool_calls:
                for tc in m.tool_calls:
                    fn = tc.get("function", tc)
                    history.append(
                        HistoryItem(
                            role="tool_call",
                            text="",
                            tool_call_id=tc.get("id"),
                            tool_name=fn.get("name"),
                            tool_args=_args_dict(fn.get("arguments")),
                        )
                    )
                if m.content:
                    history.append(HistoryItem(role="assistant", text=m.content))
            else:
                history.append(HistoryItem(role=m.role, text=m.content))  # type: ignore[arg-type]
        await self._rt.connect(
            {"instructions": system, "tools": tools or [], "history": history, "modalities": ["text"]}
        )
        self._connected = True

    async def feed(self, pcm16: bytes) -> None:
        await self._rt.append_audio(pcm16, int(self._rt.config.get("input_sample_rate", 16000)))

    async def stream(
        self,
        messages: list[ChatMessage],
        audio: AudioTurn | None,
        tools: list[ToolDef] | None = None,
    ) -> AsyncIterator[LLMDelta]:
        async with self._lock:
            await self._ensure_connected(messages, tools)
            if audio is not None and audio.pcm16:
                await self._rt.append_audio(audio.pcm16, audio.sample_rate)
            await self._rt.commit_input()
            ref = await self._rt.create_response({"modalities": ["text"], "tools": tools} if tools else {"modalities": ["text"]})
            response_id: str | None = None
            async for ev in self._rt.events():
                if ev.type is EventType.RESPONSE_STARTED and ev.response_ref == ref:
                    response_id = ev.response_id
                elif response_id is None:
                    continue
                elif ev.response_id != response_id:
                    continue
                elif ev.type is EventType.RESPONSE_TEXT_DELTA and ev.text:
                    yield LLMDelta(text=ev.text)
                elif ev.type is EventType.TOOL_CALL:
                    yield LLMDelta(tool_call={"id": ev.call_id, "name": ev.name, "arguments": ev.args or {}})
                elif ev.type is EventType.RESPONSE_DONE:
                    usage = None
                    if ev.usage is not None:
                        usage = {
                            k: v
                            for k, v in {
                                "input_text_tokens": ev.usage.input_text_tokens,
                                "input_audio_tokens": ev.usage.input_audio_tokens,
                                "cached_input_tokens": ev.usage.cached_input_tokens,
                                "output_text_tokens": ev.usage.output_text_tokens,
                            }.items()
                            if v is not None
                        }
                    yield LLMDelta(done=True, finish_reason=ev.status.value if ev.status else None, usage=usage)
                    return
                elif ev.type is EventType.ERROR and ev.error is not None and ev.error.scope is not ErrorScope.ITEM:
                    yield LLMDelta(done=True, finish_reason=f"error:{ev.error.reason.value}")
                    return

    async def cancel(self) -> None:
        if self._connected:
            await self._rt.cancel_response(None)

    async def close(self) -> None:
        if self._connected:
            await self._rt.disconnect()
            self._connected = False


def _args_dict(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments:
        import json

        try:
            v = json.loads(arguments)
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}
    return {}
