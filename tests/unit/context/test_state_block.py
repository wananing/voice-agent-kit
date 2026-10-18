"""Spec 05 §2.2 state block and §6.2 injection channel table."""

from __future__ import annotations

import pytest

from vak.core.context.state_block import StateBlockStore, StateSchema, choose_injection_channel
from vak.core.defaults import ContextParams
from vak.core.types import StateLayer

from .fakes import SCHEMA, make_caps


def test_render_fixed_order_and_empty_as_wu():
    st = StateBlockStore(SCHEMA, ContextParams())
    assert st.render() == "当前任务：无。\n称呼：无。\n进度：无。\n刚说到：无。"
    st.update("progress", "第二站")
    st.update("task", "看日落")
    st.apply_pending()
    assert st.render() == "当前任务：看日落。\n称呼：无。\n进度：第二站。\n刚说到：无。"


def test_pending_applied_only_at_checkpoint_and_state_rev_increments():
    st = StateBlockStore(SCHEMA, ContextParams())
    assert st.update("task", "看日落") is StateLayer.IMMEDIATE
    assert st.update("progress", "1") is StateLayer.LAZY
    assert st.has_pending and st.state_rev == 0 and st.render().startswith("当前任务：无。")
    changed = st.apply_pending()
    assert changed == {StateLayer.IMMEDIATE, StateLayer.LAZY}
    assert st.state_rev == 1 and not st.has_pending
    st.update("task", "看日落")  # same value → no rev bump
    assert st.apply_pending() == set() and st.state_rev == 1
    assert st.version == "2.1"


def test_unknown_field_rejected_and_schema_fixed():
    st = StateBlockStore(SCHEMA, ContextParams())
    with pytest.raises(KeyError):
        st.update("price", "100")  # business facts do not belong here anyway


def test_length_check():
    st = StateBlockStore(SCHEMA, ContextParams(state_block_max_chars=30))
    assert st.check_length() == []
    st.update("task", "很" * 40)
    st.apply_pending()
    assert st.check_length() and "too long" in st.check_length()[0]


def test_schema_from_dict_rejects_duplicates():
    with pytest.raises(ValueError):
        StateSchema.from_dict({"fields": [{"name": "a", "layer": "lazy"}, {"name": "a", "layer": "lazy"}]})


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, "rebuild"),  # all unknown → conservative values → immutable upstream
        ({"response__overrides": {"instructions": "replace"}}, "per_response_override"),
        ({"config__append_channels": [{"name": "instructions", "max_tokens": 500}]}, "append"),
        ({"config__append_channels": ["instructions"]}, "append"),
        ({"config__update__instructions": "live_full"}, "config_update"),
        ({"config__update__instructions": "live_sparse"}, "config_update"),
        ({"config__update__instructions": "next_session"}, "rebuild"),
        ({"config__update__instructions": "append_only"}, "rebuild"),
        (
            {"response__overrides": {"instructions": "none"}, "config__update__instructions": "live_full"},
            "config_update",
        ),
        (
            {"response__overrides": {"instructions": "replace"}, "config__update__instructions": "live_full"},
            "per_response_override",
        ),
    ],
)
def test_choose_injection_channel_table(fields, expected):
    assert choose_injection_channel(make_caps(**fields)) == expected


def test_lazy_layer_never_touches_upstream():
    caps = make_caps(response__overrides={"instructions": "replace"})
    assert choose_injection_channel(caps, StateLayer.LAZY) == "lazy"
