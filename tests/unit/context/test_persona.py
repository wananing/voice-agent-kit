"""Spec 05 §2.3 persona checks."""

from __future__ import annotations

from vak.core.context.persona import check_persona, load_persona, render_persona

from .fakes import PERSONA

GOOD = {
    "version": "3",
    "role": "我是小驼，景区的语音向导。",
    "background": "我从小在山里长大，喜欢讲故事。",
    "personality": ["热情", "耐心", "直接"],
    "register": "对孩子用短句。",
    "hard_rules": ["每轮一两句", "数字口语化"],
    "banned_words": ["亲", "为您服务"],
    "boundaries": "只说被告知的事实。",
}


def test_load_and_render_fixed_order():
    p = load_persona(GOOD)
    assert p.version == "3" and p.hard_rules == ["每轮一两句", "数字口语化"]
    text = render_persona(p)
    order = [text.index(k) for k in ("角色：", "背景：", "性格：", "语域：", "硬规则：", "不要用这些词：", "边界：")]
    assert order == sorted(order)
    assert check_persona(p) == []


def test_background_with_digits_date_place_flagged():
    p = load_persona({**GOOD, "background": "我 2019 年来到杭州市，带过 300 个团。"})
    problems = check_persona(p)
    assert any("digits" in x for x in problems)
    assert any("place name" in x for x in problems)
    p2 = load_persona({**GOOD, "background": "我十年前来到这里。"})
    assert any("date" in x for x in check_persona(p2))


def test_hard_rules_required_and_banned_words_not_used_in_persona():
    p = load_persona({**GOOD, "hard_rules": [], "boundaries": "很高兴为您服务。"})
    problems = check_persona(p)
    assert "hard_rules: missing" in problems
    assert any("为您服务" in x for x in problems)


def test_fixture_persona_passes():
    assert check_persona(PERSONA) == []
