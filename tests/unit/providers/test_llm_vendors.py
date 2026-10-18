"""Ark / Bailian: OpenAI-compatible endpoint defaults and thinking disabled by default."""

from __future__ import annotations

import json

import httpx
import pytest

from vak.upstream.providers import PROVIDERS
from vak.upstream.providers._util import ProviderConfigError
from vak.upstream.providers.base import ChatMessage
from vak.upstream.providers.llm.vendors import ArkLLM, BailianLLM

from .fakes import sse, streaming_transport

BODY = sse([{"id": "x", "choices": [{"index": 0, "delta": {"content": "好"}, "finish_reason": "stop"}]}])


async def _request(llm) -> httpx.Request:  # type: ignore[no-untyped-def]
    _ = [d async for d in llm.stream([ChatMessage(role="user", content="hi")])]
    return llm._client._transport.handler.requests[0]


@pytest.mark.parametrize(
    "cls,env,url,model,off",
    [
        (ArkLLM, "ARK_API_KEY", "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
         "doubao-seed-2-0-mini-260428", {"thinking": {"type": "disabled"}}),
        (BailianLLM, "DASHSCOPE_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
         "qwen-flash", {"enable_thinking": False}),
    ],
)
async def test_defaults_endpoint_key_model_and_thinking_off(cls, env, url, model, off, monkeypatch):
    monkeypatch.setenv(env, "secret")
    llm = cls({}, client=httpx.AsyncClient(transport=streaming_transport(BODY)))
    req = await _request(llm)
    body = json.loads(req.content)
    assert str(req.url) == url and req.headers["authorization"] == "Bearer secret"
    assert body["model"] == model and body["stream"] is True
    assert {k: body[k] for k in off} == off


async def test_thinking_opt_in_and_extra_body_wins(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "k")
    llm = ArkLLM({"thinking": True, "model": "ep-1"}, client=httpx.AsyncClient(transport=streaming_transport(BODY)))
    body = json.loads((await _request(llm)).content)
    assert "thinking" not in body and body["model"] == "ep-1"
    llm = BailianLLM({"api_key": "k", "extra_body": {"enable_thinking": True}},
                     client=httpx.AsyncClient(transport=streaming_transport(BODY)))
    assert json.loads((await _request(llm)).content)["enable_thinking"] is True


async def test_ark_fast_service_tier_by_default_and_overridable(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "k")
    llm = ArkLLM({"thinking": True}, client=httpx.AsyncClient(transport=streaming_transport(BODY)))
    assert json.loads((await _request(llm)).content)["service_tier"] == "fast"  # independent of thinking
    llm = ArkLLM({"extra_body": {"service_tier": "default"}}, client=httpx.AsyncClient(transport=streaming_transport(BODY)))
    assert json.loads((await _request(llm)).content)["service_tier"] == "default"
    llm = BailianLLM({"api_key": "k"}, client=httpx.AsyncClient(transport=streaming_transport(BODY)))
    assert "service_tier" not in json.loads((await _request(llm)).content)


@pytest.mark.parametrize("cls,env", [(ArkLLM, "ARK_API_KEY"), (BailianLLM, "DASHSCOPE_API_KEY")])
def test_missing_key(cls, env, monkeypatch):
    monkeypatch.delenv(env, raising=False)
    with pytest.raises(ProviderConfigError, match=env):
        cls({})


def test_registered_as_llm():
    assert PROVIDERS["ark"].kind == "llm" and PROVIDERS["bailian"].kind == "llm"
