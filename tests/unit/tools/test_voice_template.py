"""voice_template rendering and Chinese number reading (spec 03 §2.1, §3.1; KM 2.7)."""

from __future__ import annotations

import pytest

from vak.core.tools import render
from vak.core.tools.voice_template import read_decimal, read_digits, read_integer, read_time, spoken


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "零"), (3, "三"), (10, "十"), (15, "十五"), (20, "二十"), (23, "二十三"), (100, "一百"),
        (105, "一百零五"), (110, "一百一十"), (115, "一百一十五"), (1000, "一千"), (1005, "一千零五"),
        (1010, "一千零一十"), (1200, "一千二百"), (10000, "一万"), (10005, "一万零五"), (12345, "一万二千三百四十五"),
        (100000, "十万"), (1000001, "一百万零一"), (100000001, "一亿零一"), (-23, "负二十三"),
    ],
)
def test_read_integer(n, expected):
    assert read_integer(n) == expected


@pytest.mark.parametrize("s,expected", [("3.5", "三点五"), ("0.25", "零点二五"), ("-1.05", "负一点零五"), ("12", "十二")])
def test_read_decimal(s, expected):
    assert read_decimal(s) == expected


def test_read_digits_and_time():
    assert read_digits("13800138000") == "幺三八零零幺三八零零零"
    assert read_time("14", "30") == "十四点三十"
    assert read_time("14", "05") == "十四点零五"
    assert read_time("9", "00") == "九点"


def test_spoken_values():
    assert spoken(23) == "二十三"
    assert spoken(3.5) == "三点五"
    assert spoken(True) == "是"
    assert spoken(None) == ""
    assert spoken([1, 2]) == "一、二"
    assert spoken("14:30 出发") == "十四点三十 出发"
    assert spoken("电话 13800138000") == "电话 幺三八零零幺三八零零零"
    assert spoken("2024年") == "二零二四年"
    assert spoken("房间 007") == "房间 零零七"
    assert spoken("还剩 12 个，温度 36.6") == "还剩 十二 个，温度 三十六点六"


def test_render_template_fields_and_missing():
    assert render("已经完成 {done} 个，还剩 {left} 个。", {"done": 3, "left": 12}) == "已经完成 三 个，还剩 十二 个。"
    assert render("现在是 {hour} 点 {minute} 分。", {"hour": 14, "minute": 5}) == "现在是 十四 点 五 分。"
    assert render("去{place}，{missing}好吗？", {"place": "3号楼"}) == "去三号楼，好吗？"
    assert render("没有占位符 123。", {}) == "没有占位符 123。"  # literal text untouched
