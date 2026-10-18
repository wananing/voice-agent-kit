"""Decorated tools used by the executor tests. `KNOBS` lets a test control
delays (via the injected FakeClock), blocking and results."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from vak.core.tools import Entity, tool

KNOBS: dict[str, Any] = {}


def reset_knobs(clock: Any) -> None:
    KNOBS.clear()
    KNOBS.update({"clock": clock, "delay_ms": 0, "block": None, "ok": True, "backend_calls": 0, "progress": []})


async def _wait() -> None:
    if KNOBS.get("delay_ms"):
        await KNOBS["clock"].sleep(KNOBS["delay_ms"])
    block: asyncio.Event | None = KNOBS.get("block")
    if block is not None:
        await block.wait()


@tool(presentation="verbatim", latency_class="ms", write=False, voice_template="已经完成 {done} 个，还剩 {left} 个。",
      on_duplicate="replace", timeout_ms=1000)
async def get_status() -> dict:
    """查询当前任务的进度和剩余项。"""
    await _wait()
    if not KNOBS.get("ok", True):
        return {"ok": False, "reason_code": "not_supported", "summary": "现在查不了",
                "alternatives": [{"label": "稍后再试", "action": "retry"}, {"label": "换个问法", "action": "rephrase"},
                                 {"label": "第三个", "action": "x"}]}
    return {"ok": True, "summary": "报了进度", "data": {"done": 3, "left": 12}}


@tool(presentation="verbatim", latency_class="ms", write=True, voice_template="已经把{key}设为{value}。",
      idempotency_key=["key"], gates=[{"kind": "intent", "allowed_actions": ["set_flag"]},
                                       {"kind": "utterance", "must_match": ["设", "打开"], "must_not_match": ["怎么设"]},
                                       {"kind": "backend_state", "check": "flag_ok"}],
      interrupt_policy="finish_then_notify", on_duplicate="reject", timeout_ms=1000)
async def set_flag(key: str, value: str) -> dict:
    """设置一个开关。"""
    KNOBS["backend_calls"] += 1
    await _wait()
    return {"ok": True, "summary": f"设了{key}", "data": {"key": key, "value": value}}


@tool(presentation="verbatim", latency_class="seconds", write=True, voice_template="提交完成，{summary}。",
      idempotency_key=["option_id"], gates=[{"kind": "intent", "allowed_actions": ["confirm_submit", "submit_request"]},
                                             {"kind": "utterance", "must_match": ["提交", "确认"], "must_not_match": ["怎么提交"]},
                                             {"kind": "backend_state", "check": "no_active_request"}],
      phases=["confirm"], visible_after=["show_options"], interrupt_policy="finish_then_notify", on_duplicate="reject",
      duplicate_scope="turn", timeout_ms=15000,
      progress={"ack_template": "好，已经提交{option_id}，正在处理。", "throttle": {"first_after_s": 60, "min_interval_s": 60}})
async def submit_request(option_id: Annotated[str, Entity()]) -> dict:
    """提交用户已确认的请求。"""
    KNOBS["backend_calls"] += 1
    await _wait()
    return {"ok": True, "summary": f"提交了{option_id}", "data": {"option_id": option_id}}


@tool(presentation="model", latency_class="seconds", write=False, on_duplicate="replace", timeout_ms=8000,
      progress={"ack_template": "我查一下。"})
async def explain_topic(target: Annotated[str, Entity()], depth: str = "brief") -> dict:
    """讲解用户问到的对象。"""
    await _wait()
    return {"ok": True, "summary": f"关于{target}的要点", "data": {"target": target, "facts": ["a", "b"]}}


@tool(presentation="screen", latency_class="ms", write=False, voice_template="给你列了{count}个方案，看屏幕。",
      phases=["analyze", "propose"], on_duplicate="replace", timeout_ms=1000,
      profile_overrides={"A": {"presentation": "verbatim", "voice_template": "有{count}个方案，前两个是{first}和{second}。"}})
async def show_options(query: str | None = None) -> dict:
    """在屏幕上列出可选方案。"""
    await _wait()
    return {"ok": True, "summary": "列了方案", "data": {"count": 3, "first": "甲", "second": "乙"},
            "display": {"card_id": "c-17"}}


@tool(presentation="silent", latency_class="ms", write=False, on_duplicate="replace", timeout_ms=1000)
async def remember_preference(key: str, value: str) -> dict:
    """记下用户表达的偏好。"""
    await _wait()
    return {"ok": True, "summary": f"记住了{key}"}


@tool(presentation="verbatim", latency_class="background", write=False, voice_template="任务做完了，{summary}。",
      on_duplicate="reject", duplicate_scope="in_flight", timeout_ms=600000, interrupt_policy="continue",
      progress={"ack_template": "好，开始做了。", "throttle": {"first_after_s": 60, "min_interval_s": 60}})
async def long_job(progress, label: str = "x") -> dict:
    """跑一个长任务。"""
    for text in KNOBS.get("progress", []):
        await KNOBS["clock"].sleep(KNOBS.get("progress_gap_ms", 0))
        await progress(text)
    await _wait()
    return {"ok": True, "summary": f"{label}完成"}


@tool(presentation="verbatim", latency_class="ms", write=False, voice_template="现在是{hour}点{minute}分。",
      on_duplicate="confirm", timeout_ms=1000)
async def get_time() -> dict:
    """报时。"""
    return {"ok": True, "summary": "报了时间", "data": {"hour": 14, "minute": 5}}


ALL = [get_status, set_flag, submit_request, explain_topic, show_options, remember_preference, long_job, get_time]
