"""Volcengine Ark (火山方舟) and Alibaba Model Studio (阿里百炼) chat models.

Both speak the OpenAI chat-completions SSE format, so these are
`OpenAICompatLLM` with the vendor's endpoint, key variable and model
default filled in, plus **thinking disabled by default**: reasoning
tokens arrive before any speakable text and push the first audio back by
seconds. The switches are the ones xiaozhi-esp32-server sends per host
(`core/providers/llm/openai/openai.py`): Ark `thinking: {type: disabled}`,
DashScope `enable_thinking: false`.

Ark also sends `service_tier: fast` by default: requests use the endpoint's
low-latency quota when it has one and silently fall back to the regular tier
otherwise (Ark API reference, `service_tier`: fast | auto | default | flex).

Config keys: everything `openai_compat` takes, plus `thinking` (False).
`extra_body` entries win over the defaults set here.

- `ark`: base_url https://ark.cn-beijing.volces.com/api/v3, api_key else
  $ARK_API_KEY, model default doubao-seed-2-0-mini-260428 (an endpoint id
  `ep-...` also works).
- `bailian`: base_url https://dashscope.aliyuncs.com/compatible-mode/v1
  (international: https://dashscope-intl.aliyuncs.com/compatible-mode/v1),
  api_key else $DASHSCOPE_API_KEY, model default qwen-flash.

The Ark default was measured by the author (2026-10) as the fastest Doubao
model with fast inference on and thinking off; the Bailian default comes
from xiaozhi-esp32-server's config.yaml. Set `model` explicitly in production.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from vak.upstream.providers._util import ProviderConfigError
from vak.upstream.providers.llm.openai_compat import OpenAICompatLLM


def _vendor_config(
    config: dict[str, Any],
    *,
    name: str,
    base_url: str,
    key_env: str,
    model: str,
    thinking_off: dict[str, Any],
    always: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = dict(config)
    out.setdefault("base_url", base_url)
    out.setdefault("model", model)
    key = out.get("api_key") or os.environ.get(key_env)
    if not key:
        raise ProviderConfigError(f"{name}: set config 'api_key' or environment variable {key_env}")
    out["api_key"] = key
    extra = dict(always or {})
    if not out.get("thinking"):
        extra.update(thinking_off)
    extra.update(out.get("extra_body") or {})
    out["extra_body"] = extra
    return out


class ArkLLM(OpenAICompatLLM):
    def __init__(self, config: dict[str, Any], *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(
            _vendor_config(
                config,
                name="ark",
                base_url="https://ark.cn-beijing.volces.com/api/v3",
                key_env="ARK_API_KEY",
                model="doubao-seed-2-0-mini-260428",
                thinking_off={"thinking": {"type": "disabled"}},
                always={"service_tier": "fast"},
            ),
            client=client,
        )


class BailianLLM(OpenAICompatLLM):
    def __init__(self, config: dict[str, Any], *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(
            _vendor_config(
                config,
                name="bailian",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                key_env="DASHSCOPE_API_KEY",
                model="qwen-flash",
                thinking_off={"enable_thinking": False},
            ),
            client=client,
        )
