"""The names the tile profile writes (resources/tilemaker/process-openmaptiles.lua,
SetNameAttributes), run in Lua 5.1, tilemaker's Lua, through lupa:

- an OSM name tag can hold several names separated by ";" ("洛杉矶;洛杉磯",
  Simplified;Traditional), and a label drew them all: every name written
  (name:latin, name_int, name:xx) keeps the first non-empty one, trimmed;
- name:zh comes from name:zh-Hans (Simplified) when OSM has it, else from
  name:zh;
- name:xx is still written only where it differs from the place's own name,
  and only for the languages STREETZIM_TILE_LANGUAGES asks for.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "resources" / "tilemaker" / "process-openmaptiles.lua"
lua51 = pytest.importorskip(
    "lupa.lua51", reason="lupa is not installed (requirements-dev.txt), so the tile "
    "profile's Lua cannot run here: pip install 'lupa>=2.0'")


@pytest.fixture
def profile(monkeypatch):
    """The profile loaded the way tilemaker loads it, for the languages given."""
    def load(langs: str | None = "de,fr,zh"):
        if langs is None:
            monkeypatch.delenv("STREETZIM_TILE_LANGUAGES", raising=False)
        else:
            monkeypatch.setenv("STREETZIM_TILE_LANGUAGES", langs)
        rt = lua51.LuaRuntime(unpack_returned_tuples=True)
        rt.execute(PROFILE.read_text(encoding="utf-8"))
        return rt
    return load


def names(rt, tags: dict[str, str]) -> dict[str, str]:
    """What SetNameAttributes writes for an object with these tags."""
    written: dict[str, str] = {}
    g = rt.globals()
    g.Find = lambda k: tags.get(k, "")
    g.Holds = lambda k: k in tags
    g.Attribute = written.__setitem__
    g.SetNameAttributes()
    return written


def test_several_names_keep_the_first(profile):
    rt = profile()
    w = names(rt, {"name": "Los Angeles", "name:zh": "洛杉矶;洛杉磯"})
    assert w == {"name:latin": "Los Angeles", "name:zh": "洛杉矶"}
    w = names(rt, {"name": "Biel/Bienne;Biel", "name:de": " Biel ; Bienne", "name:fr": ";Bienne"})
    assert w == {"name:latin": "Biel/Bienne", "name:de": "Biel", "name:fr": "Bienne"}
    # A blank first value is skipped too.
    assert names(rt, {"name": "Biel/Bienne", "name:fr": "  ; Bienne"})["name:fr"] == "Bienne"


def test_the_own_name_and_english_keep_the_first_too(profile):
    rt = profile()
    assert names(rt, {"name": "東京;とうきょう", "name:en": "Tokyo;Tôkyô"}) == {
        "name:latin": "Tokyo", "name_int": "東京"}
    # name:xx missing or equal to the own name: not written.
    assert names(rt, {"name": "東京;とうきょう", "name:en": "Tokyo", "name:zh": "東京;东京",
                      "name:de": "Tokio"}) == {
        "name:latin": "Tokyo", "name_int": "東京", "name:de": "Tokio"}
    # The same first name: no name_int, as for a plain "Basel".
    assert names(rt, {"name": "Basel;Bâle", "name:en": "Basel"}) == {"name:latin": "Basel"}
    # A translation equal to the first own name is not written.
    assert names(rt, {"name": "Basel;Bâle", "name:de": "Basel", "name:fr": "Bâle"}) == {
        "name:latin": "Basel", "name:fr": "Bâle"}


def test_zh_prefers_simplified(profile):
    rt = profile()
    assert names(rt, {"name": "Taipei", "name:zh": "臺北", "name:zh-Hans": "台北"})["name:zh"] == "台北"
    assert names(rt, {"name": "Taipei", "name:zh": "臺北;台北", "name:zh-Hans": " ; "})["name:zh"] == "臺北"
    assert names(rt, {"name": "Taipei", "name:zh-Hans": "台北;臺北"})["name:zh"] == "台北"
    # name:zh-Hans is read for zh only.
    assert "name:de" not in names(rt, {"name": "Taipei", "name:zh-Hans": "台北"})


def test_an_object_named_only_in_simplified_has_names(profile):
    rt = profile()
    g = rt.globals()
    g.Holds = lambda k: k == "name:zh-Hans"
    assert g.HasNames() is True
    g.Holds = lambda k: False
    assert g.HasNames() is False


def test_single_names_and_english_builds_are_as_before(profile):
    rt = profile(None)
    assert names(rt, {"name": "Genève", "name:en": "Geneva", "name:de": "Genf"}) == {
        "name:latin": "Geneva", "name_int": "Genève"}
    assert names(rt, {"name": "Monaco"}) == {"name:latin": "Monaco"}
    assert names(rt, {}) == {"name:latin": ""}
    rt = profile("de")
    assert names(rt, {"name": "Genève", "name:en": "Geneva", "name:de": "Genf"}) == {
        "name:latin": "Geneva", "name_int": "Genève", "name:de": "Genf"}
