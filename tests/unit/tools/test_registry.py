"""Registry validation and the §3.2 route table (spec 03 §2.1, §3.2, §8.2)."""

from __future__ import annotations

import pytest

from tests.unit.tools import sample_tools
from vak.core.tools import ROUTE_TABLE, Registry, RegistryError, static_route, tool
from vak.core.tools.registry import DEFAULT_CUE
from vak.core.types import CueLength, LatencyClass, Presentation, ToolRoute

P, L = Presentation, LatencyClass


def mk(**kw):
    base = dict(presentation="verbatim", latency_class="ms", write=False, voice_template="t", timeout_ms=1000)
    base.update(kw)

    @tool(**base)
    async def f(x: str = "") -> dict:
        """f"""
        return {}

    return f


def load(fn, profile="A"):
    return Registry([fn], profile)


# --- §2.1 rejection rules -------------------------------------------------

def test_reject_write_without_gates():
    with pytest.raises(RegistryError, match="gates"):
        load(mk(write=True, idempotency_key=["x"], interrupt_policy="continue"))


def test_reject_write_without_idempotency_key():
    with pytest.raises(RegistryError, match="idempotency_key"):
        load(mk(write=True, gates=[{"kind": "intent"}], interrupt_policy="continue"))


def test_reject_screen_without_voice_template():
    with pytest.raises(RegistryError, match="voice_template"):
        load(mk(presentation="screen", voice_template=None), profile="B")


def test_reject_verbatim_without_voice_template():
    with pytest.raises(RegistryError, match="voice_template"):
        load(mk(voice_template=None))


def test_reject_write_with_interrupt_cancel():
    with pytest.raises(RegistryError, match="cancel"):
        load(mk(write=True, gates=[{"kind": "intent"}], idempotency_key=["x"], interrupt_policy="cancel"))


def test_reject_non_ms_without_progress():
    with pytest.raises(RegistryError, match="progress"):
        load(mk(latency_class="seconds"))


def test_reject_screen_in_profile_a_unless_override():
    f = mk(presentation="screen")
    with pytest.raises(RegistryError, match="profile A"):
        load(f, "A")
    assert load(f, "B").get("f").presentation is P.SCREEN
    g = mk(presentation="screen", profile_overrides={"A": {"presentation": "verbatim", "voice_template": "v"}})
    assert load(g, "A").get("f").presentation is P.VERBATIM
    assert load(g, "A").get("f").voice_template == "v"


def test_reject_silent_write():
    with pytest.raises(RegistryError, match="row 11"):
        load(mk(presentation="silent", voice_template=None, write=True, gates=[{"kind": "intent"}],
                idempotency_key=["x"], interrupt_policy="continue"))


def test_reject_override_of_disallowed_field_and_bad_name():
    with pytest.raises(RegistryError, match="override"):
        load(mk(profile_overrides={"A": {"write": True}}))
    with pytest.raises(RegistryError, match="snake_case"):
        load(mk(name="GetStatus"))
    with pytest.raises(RegistryError, match="not decorated"):
        Registry([lambda: None], "A")  # type: ignore[list-item]


def test_duplicate_name_rejected():
    with pytest.raises(RegistryError, match="duplicate"):
        Registry([mk(), mk()], "A")


# --- §3.2 route table: all 11 rows ----------------------------------------

@pytest.mark.parametrize(
    "row,pres,lc,write,route",
    [
        (1, P.VERBATIM, L.MS, False, ToolRoute.A1),
        (2, P.VERBATIM, L.MS, True, ToolRoute.A1),
        (3, P.VERBATIM, L.SECONDS, False, ToolRoute.A3),
        (4, P.VERBATIM, L.SECONDS, True, ToolRoute.A3),
        (5, P.VERBATIM, L.BACKGROUND, True, ToolRoute.A3),
        (5, P.MODEL, L.BACKGROUND, False, ToolRoute.A3),
        (5, P.SCREEN, L.BACKGROUND, True, ToolRoute.A3),
        (6, P.MODEL, L.MS, False, ToolRoute.A2),
        (6, P.MODEL, L.SECONDS, False, ToolRoute.A2),
        (7, P.MODEL, L.MS, True, ToolRoute.A1),
        (7, P.MODEL, L.SECONDS, True, ToolRoute.A3),
        (8, P.SCREEN, L.MS, False, ToolRoute.A1),
        (9, P.SCREEN, L.SECONDS, False, ToolRoute.A3),
        (9, P.SCREEN, L.SECONDS, True, ToolRoute.A3),
        (10, P.SILENT, L.MS, False, ToolRoute.SILENT),
        (10, P.SILENT, L.BACKGROUND, False, ToolRoute.SILENT),
    ],
)
def test_route_table_rows_1_to_10(row, pres, lc, write, route):
    assert static_route(pres, lc, write) is route
    assert ROUTE_TABLE[(pres, lc, write)] is route


@pytest.mark.parametrize("lc", list(L))
def test_route_table_row_11_forbidden(lc):
    with pytest.raises(RegistryError):
        static_route(P.SILENT, lc, True)
    assert (P.SILENT, lc, True) not in ROUTE_TABLE


def test_route_and_cue_default_stored_on_spec():
    r = Registry(sample_tools.ALL, "A")
    assert r.get("get_status").route is ToolRoute.A1 and r.get("get_status").cue is CueLength.SHORT
    assert r.get("explain_topic").route is ToolRoute.A2 and r.get("explain_topic").cue is CueLength.LONG
    assert r.get("submit_request").route is ToolRoute.A3 and r.get("submit_request").cue is CueLength.LONG
    assert r.get("remember_preference").route is ToolRoute.SILENT and r.get("remember_preference").cue is CueLength.NONE
    assert r.get("long_job").cancellable_by_user is True and r.get("get_status").cancellable_by_user is False
    assert DEFAULT_CUE[ToolRoute.A0] is CueLength.SHORT
    assert r.registry_version == ""


def test_timeout_default_from_params():
    f = mk(timeout_ms=0, latency_class="seconds", progress={"ack_template": "a"})
    assert load(f).get("f").timeout_ms == 15_000


# --- visibility, tool table, loading --------------------------------------

def test_visible_tools_phases_and_visible_after():
    r = Registry(sample_tools.ALL, "B")
    vis = r.visible_tools("confirm", set())
    assert "get_status" in vis and "submit_request" not in vis and "show_options" not in vis
    assert "submit_request" in r.visible_tools("confirm", {"show_options"})
    assert "show_options" in r.visible_tools("propose", set())
    assert not r.is_visible("nope", "global", set())


def test_tool_table_is_whole_table_and_profile_resolved():
    r = Registry(sample_tools.ALL, "A")
    names = [t.name for t in r.tool_table()]
    assert names == [f.__vak_tool__.name for f in sample_tools.ALL]
    assert len(names) == len(sample_tools.ALL)  # never phase-filtered
    assert r.readonly_names() == [n for n in names if n not in ("set_flag", "submit_request")]
    assert r.get("submit_request").params["properties"]["option_id"]["entity"] is True


def test_load_from_dotted_module_path():
    r = Registry("tests.unit.tools.sample_tools", "B", registry_version="2026-09-29.1")
    assert set(r.names) == {s.name for s in Registry(sample_tools.ALL, "B")}
    assert r.registry_version == "2026-09-29.1"
    assert r.wants_progress("long_job") and not r.wants_progress("get_status")
