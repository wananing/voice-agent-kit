"""Tools for open_mic_s2s.

Each function is a tool. Metadata on the decorator is the registry entry
(spec 03 §2.1); the kit computes the speaking route (A0–A3) from
presentation × latency_class × write and enforces gates and closure.
"""

from __future__ import annotations

from datetime import datetime

from vak import tool


@tool(
    presentation="verbatim",
    latency_class="ms",
    write=False,
    voice_template="现在是{hour}点{minute}分。",
    phases="global",
    interrupt_policy="cancel",
    on_duplicate="replace",
    timeout_ms=1000,
)
async def get_time() -> dict:
    """查询当前时间。"""
    now = datetime.now()
    return {"ok": True, "summary": "报了当前时间", "data": {"hour": now.hour, "minute": now.minute}}


@tool(
    presentation="verbatim",
    latency_class="seconds",
    write=True,
    voice_template="好，{item} 已经记下了。",
    idempotency_key=["item"],
    gates=[
        {"kind": "intent", "allowed_actions": ["add_note"]},
        {"kind": "utterance", "must_match": ["记", "加", "添"], "must_not_match": ["怎么记", "能不能记"]},
    ],
    phases="global",
    interrupt_policy="finish_then_notify",
    on_duplicate="reject",
    timeout_ms=15000,
    progress={"ack_template": "好，我记一下。"},
)
async def add_note(item: str) -> dict:
    """把用户说的一件事记下来。参数 item 是要记的内容。"""
    # 这里调用你的后端；用 idempotency_key 去重。
    return {"ok": True, "summary": f"记下了：{item}", "data": {"item": item}}
