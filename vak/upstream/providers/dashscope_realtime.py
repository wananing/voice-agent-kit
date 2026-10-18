"""DashScope (Qwen-Omni / Qwen-Audio Realtime) provider.

OpenAI-Realtime *beta* dialect with documented differences, read from
qwen-audio-agent (`server/src/voice/providers/dashscope.mjs`,
`openai-compatible-protocol.mjs`, `realtime-input-runtime.mjs`) and
docs/notes/e2e-models.md Q4:

- endpoint `wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=...`
  (Qwen3.8-Omni needs a workspace endpoint, config `base_url`);
- input pcm16 16 kHz, output pcm16 24 kHz;
- `turn_detection` default `semantic_vad` (Omni) / `smart_turn` (Audio family);
  whether `null` disables it is P0-2/P0-11 material (capabilities: unknown);
- user transcript may also stream as `conversation.item.input_audio_transcription.text`;
- one response slot (`singleResponseSlot: true`); no auto reply after a tool
  result (`automaticToolResponses: false`) -> silent close by omission;
- Omni does not echo client item ids; errors are matched by message substring.

Config keys: api_key (else $DASHSCOPE_API_KEY), base_url, model
(default qwen-omni-turbo-realtime), voice, nested_audio_config (Qwen3.8),
plus the common keys of `RealtimeSessionBase`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vak.core.clock import Clock
from vak.core.types import ErrorClass, ErrorReason, ErrorScope
from vak.upstream.providers.realtime_common import CodeEntry, RealtimeSessionBase, rules

_F, _T, _B = ErrorClass.FATAL, ErrorClass.TRANSIENT, ErrorClass.BENIGN
_S, _R, _I = ErrorScope.SESSION, ErrorScope.RESPONSE, ErrorScope.ITEM

# DashScope codes are not stable across models; qwen-audio-agent classifies
# by message substring (dashscope.mjs classifyError). Kept in capabilities yaml.
DASHSCOPE_CODE_MAP: dict[str, CodeEntry] = {
    "response_cancel_not_active": (_B, ErrorReason.CANCEL_RACE, _R),
    "conversation_already_has_active_response": (_B, ErrorReason.CANCEL_RACE, _R),
    "input_audio_buffer_commit_empty": (_B, ErrorReason.EMPTY_COMMIT, _I),
}

DASHSCOPE_MESSAGE_RULES = rules(
    (
        r"invalid[_ -]?api[_ -]?key|incorrect api key|authentication failed|unauthorized|unexpected server response: (401|403)",
        _F,
        ErrorReason.AUTH,
        _S,
    ),
    (r"\barrearage\b|account is not in good standing", _F, ErrorReason.BILLING, _S),
    (r"allocationquota\.freetieronly|free allocated quota exceeded|free tier .* exhausted", _F, ErrorReason.QUOTA, _S),
    (r"model(\.|_)?accessdenied|model[_ -]?not[_ -]?found", _F, ErrorReason.INVALID_CONFIG, _S),
    (r"another response is in progress|cannot create response while .*response.*in progress", _B, ErrorReason.CANCEL_RACE, _R),
    (r"already has (a pending response request|an active response)", _B, ErrorReason.CANCEL_RACE, _R),
    (r"no active response", _B, ErrorReason.CANCEL_RACE, _R),
    (r"user is speaking", _B, ErrorReason.CANCEL_RACE, _R),
    (
        r"data[_ -]?inspection[_ -]?failed|ip[_ -]?infringement[_ -]?suspect|inappropriate content"
        r"|content[_ -]?(filter|moderation|safety|policy)|(input|output) data may contain",
        _B,
        ErrorReason.MODERATION,
        _R,
    ),
    (r"throttl|rate limit|too many requests", _T, ErrorReason.THROTTLED, _S),
    (r"inactiv|idle", _T, ErrorReason.SESSION_LOST, _S),
)


class DashScopeRealtime(RealtimeSessionBase):
    provider_name = "dashscope_realtime"
    default_base_url = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    default_model = "qwen-omni-turbo-realtime"
    api_key_env = "DASHSCOPE_API_KEY"
    dialect = "beta"
    input_sample_rate = 16000  # dashscope.mjs inputSampleRate
    output_sample_rate = 24000  # dashscope.mjs outputSampleRate
    default_turn_detection = {"type": "semantic_vad"}
    default_transcription = None
    code_map = DASHSCOPE_CODE_MAP
    message_rules = DASHSCOPE_MESSAGE_RULES

    def __init__(
        self,
        config: dict[str, Any],
        *,
        clock: Clock | None = None,
        is_committed: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(config, clock=clock, is_committed=is_committed)
        self._nested_audio = bool(self.config.get("nested_audio_config", False))

    def _headers(self, merged: dict[str, Any]) -> dict[str, str]:
        headers = super()._headers(merged)
        headers.pop("OpenAI-Beta", None)  # not part of the DashScope dialect
        return headers

    def _encode_session(self, patch: dict[str, Any]) -> dict[str, Any]:
        s = super()._encode_session(patch)
        # DashScope names the raw format "pcm" (dashscope.mjs buildSession)
        if "input_audio_format" in s:
            s["input_audio_format"] = "pcm"
        if "output_audio_format" in s:
            s["output_audio_format"] = "pcm"
        if self._nested_audio and not self._configured:
            # Qwen3.8-Omni: nested audio config on first setup only
            s.pop("input_audio_format", None)
            s.pop("output_audio_format", None)
            voice = s.pop("voice", None)
            audio: dict[str, Any] = {
                "input": {"format": {"type": "pcm", "sample_rate": self.input_sample_rate}},
                "output": {"format": {"type": "pcm", "sample_rate": self.output_sample_rate}},
            }
            if voice is not None:
                audio["output"]["voice"] = voice
            s["audio"] = audio
        return s
