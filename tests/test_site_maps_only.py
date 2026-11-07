"""Maps-only site languages (web/generate.py MAPS_ONLY): a language whose
site.<lang>.json says "_maps_only": true has a home page listing only the
maps with a version in it and pages for those maps alone; nothing links a
page it does not have, and while no map has a version it has no pages."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen_maps_only", ROOT / "web" / "generate.py")
gen = importlib.util.module_from_spec(spec)
sys.modules["gen_maps_only"] = gen
spec.loader.exec_module(gen)
sys.path.insert(0, str(ROOT / "tests"))
import test_site_languages as tsl  # noqa: E402

ML = "ta"       # the maps-only language of these tests (any code no real site language takes)
WITH, WITHOUT = tsl.WITH, tsl.WITHOUT


def _build(tmp_path, monkeypatch, versions_in_ml=True):
    """The site with a maps-only Tamil: Kyiv has a Tamil version (or none)."""
    monkeypatch.setattr(gen, "MAPS_ONLY", {ML})
    monkeypatch.setattr(gen, "_MAPS_ONLY_PAGES", {})
    monkeypatch.setattr(gen, "ALL_LANGUAGES", list(gen.ALL_LANGUAGES) + [(ML, "தமிழ்", f"{ML}/")])
    monkeypatch.setattr(gen, "LANGUAGES", list(gen.ALL_LANGUAGES))
    en = gen._i18n_file("site", "en")
    monkeypatch.setitem(gen._I18N, ("site", ML), {**en, "lang_name": "தமிழ்"})
    monkeypatch.setitem(gen._I18N, ("regions", ML), {})
    monkeypatch.setattr(gen, "_VERSIONS", {})
    tsl._recipes(tmp_path / "build" / "recipes", langs=tsl.LANGS | ({ML} if versions_in_ml else set()))
    # the helper module's mock, pointed at this module
    monkeypatch.setattr(tsl, "gen", gen)
    tsl._mock(monkeypatch, tmp_path, [r["id"] for r in gen.REGIONS])
    gen.build_page()
    return tmp_path


def _read(site, rel):
    p = site / rel / "index.html"
    return p.read_text(encoding="utf-8") if p.exists() else None


def test_a_maps_only_language_lists_its_maps_alone(tmp_path, monkeypatch):
    site = _build(tmp_path, monkeypatch)
    home = _read(site, ML)
    assert home and f'href="/{ML}/maps/{WITH}/"' in home
    assert f"/maps/{WITHOUT}/" not in home
    assert _read(site, f"{ML}/maps/{WITH}") is not None
    assert _read(site, f"{ML}/maps/{WITHOUT}") is None
    # a full language still has every map's page
    assert _read(site, f"de/maps/{WITHOUT}") is not None


def test_nothing_links_a_page_a_maps_only_language_does_not_have(tmp_path, monkeypatch):
    site = _build(tmp_path, monkeypatch)
    for rel in (f"maps/{WITHOUT}", f"de/maps/{WITHOUT}"):
        page = _read(site, rel)
        assert f'/{ML}/maps/{WITHOUT}/' not in page, rel              # picker, other languages
        assert f'hreflang="{ML}"' not in page, rel                    # alternates
    page = _read(site, f"maps/{WITH}")
    assert f'href="/{ML}/maps/{WITH}/"' in page and f'hreflang="{ML}"' in page
    assert f'hreflang="{ML}"' in _read(site, "")                      # its home page is linked
    sm = (site / "sitemap.xml").read_text(encoding="utf-8")
    assert f"{gen.SITE_URL}{ML}/maps/{WITH}/" in sm and f"{gen.SITE_URL}{ML}/" in sm
    assert f"{ML}/maps/{WITHOUT}/" not in sm


def test_with_no_map_in_it_a_maps_only_language_has_no_pages(tmp_path, monkeypatch):
    site = _build(tmp_path, monkeypatch, versions_in_ml=False)
    assert _read(site, ML) is None
    assert f'hreflang="{ML}"' not in _read(site, "")
    assert f"{ML}/" not in (site / "sitemap.xml").read_text(encoding="utf-8")
    assert ML not in {c for c, _n, _s in gen.LANGUAGES}
    assert ML in {c for c, _n, _s in gen.ALL_LANGUAGES}


def test_a_map_that_loses_its_version_loses_its_page(tmp_path, monkeypatch):
    site = _build(tmp_path, monkeypatch)
    stale = site / ML / "maps" / WITHOUT
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "index.html").write_text("old")
    gen.write_map_pages(gen.collect_maps(gen.fetch_archive_items(), gen.build_page_regions(), True), True, True)
    assert not stale.exists()
    assert (site / ML / "maps" / WITH / "index.html").exists()


def test_the_flag_is_read_from_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "I18N_DIR", str(tmp_path))
    (tmp_path / "site.xx.json").write_text(json.dumps({"_maps_only": True, "lang_name": "X"}))
    (tmp_path / "site.yy.json").write_text(json.dumps({"lang_name": "Y"}))
    (tmp_path / "site.zz.json").write_text("not json")
    assert gen._maps_only_flag("xx") is True
    assert gen._maps_only_flag("yy") is False
    assert gen._maps_only_flag("zz") is False
    assert gen._maps_only_flag("missing") is False


@pytest.mark.parametrize("lang,path,want", [("de", f"maps/{WITHOUT}/", True), (ML, "", True)])
def test_has_page_for_full_languages_and_home_pages(monkeypatch, lang, path, want):
    monkeypatch.setattr(gen, "MAPS_ONLY", {ML})
    monkeypatch.setattr(gen, "_MAPS_ONLY_PAGES", {ML: set()})
    assert gen.has_page(lang, path) is want


def test_a_language_comes_back_once_a_map_has_a_version(tmp_path, monkeypatch):
    """The language list of one build does not stick to the next."""
    _build(tmp_path / "a", monkeypatch, versions_in_ml=False)
    assert ML not in {c for c, _n, _s in gen.LANGUAGES}
    # the next build in the same process, now with a Tamil version (LANGUAGES as the first left it)
    monkeypatch.setattr(gen, "_VERSIONS", {})
    site = tmp_path / "b"
    tsl._recipes(site / "build" / "recipes", langs=tsl.LANGS | {ML})
    tsl._mock(monkeypatch, site, [r["id"] for r in gen.REGIONS])
    gen.build_page()
    assert ML in {c for c, _n, _s in gen.LANGUAGES}
    assert (site / ML / "index.html").exists()


def test_the_checker_lets_a_maps_only_language_name_only_its_maps(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import check_site_i18n as chk
    en_site = json.loads((ROOT / "web" / "i18n" / "site.en.json").read_text(encoding="utf-8"))
    for name in ("site.en.json", "regions.en.json"):
        (tmp_path / name).write_text((ROOT / "web" / "i18n" / name).read_text(encoding="utf-8"), encoding="utf-8")
    one = {"kyiv": {"title": "Київ", "description": "Місто"}}
    (tmp_path / "regions.xx.json").write_text(json.dumps(one), encoding="utf-8")
    (tmp_path / "site.xx.json").write_text(json.dumps({**en_site, "_maps_only": True}), encoding="utf-8")
    assert chk.main(["--dir", str(tmp_path), "xx"]) == 0
    (tmp_path / "site.xx.json").write_text(json.dumps(en_site), encoding="utf-8")   # a full language
    assert chk.main(["--dir", str(tmp_path), "xx"]) == 1


def test_an_outage_keeps_a_maps_only_languages_pages_but_unlinks_them(tmp_path, monkeypatch):
    site = _build(tmp_path, monkeypatch)
    assert (site / ML / "index.html").exists()
    # the next build sees no Tamil version (archive.org or the worker down a moment)
    monkeypatch.setattr(gen, "_VERSIONS", {})
    monkeypatch.setattr(gen, "lang_versions", lambda rid, *a, **k: {})
    gen.build_page()
    assert (site / ML / "index.html").exists() and (site / ML / "maps" / WITH / "index.html").exists()
    assert f'hreflang="{ML}"' not in _read(site, "")
    assert f"{ML}/" not in (site / "sitemap.xml").read_text(encoding="utf-8")


def _entry(rid, live=True, language="en"):
    return {"region": {"id": rid, "language": language}, "live": live, "build_ok": True}


def test_a_version_counts_only_with_its_size(monkeypatch):
    monkeypatch.setattr(gen, "MAPS_ONLY", {ML})
    monkeypatch.setattr(gen, "_MAPS_ONLY_PAGES", {})
    monkeypatch.setattr(gen, "ALL_LANGUAGES", [("en", "English", ""), (ML, "தமிழ்", f"{ML}/")])
    sizes = {"a": 1000, "b": 0}
    monkeypatch.setattr(gen, "lang_versions",
                        lambda rid, *a, **k: {ML: {"size": sizes[rid], "url": "", "builder": "/build/"}})
    assert gen.maps_only_pages([_entry("a"), _entry("b"), _entry("c", live=False)]) == []
    assert gen._MAPS_ONLY_PAGES[ML] == {"a"}
    sizes["a"] = 0
    assert gen.maps_only_pages([_entry("a"), _entry("b")]) == [ML]


def test_a_map_made_in_the_language_keeps_it(monkeypatch):
    monkeypatch.setattr(gen, "MAPS_ONLY", {ML})
    monkeypatch.setattr(gen, "_MAPS_ONLY_PAGES", {})
    monkeypatch.setattr(gen, "ALL_LANGUAGES", [("en", "English", ""), (ML, "தமிழ்", f"{ML}/")])
    monkeypatch.setattr(gen, "lang_versions", lambda rid, *a, **k: {})
    assert gen.maps_only_pages([_entry("a"), _entry("chennai", language=ML)]) == []
    assert gen.maps_only_pages([_entry("a"), _entry("chennai", live=False, language=ML)]) == [ML]


def test_a_listed_map_without_a_name_in_the_language_is_warned_of(monkeypatch, capsys):
    monkeypatch.setattr(gen, "MAPS_ONLY", {ML})
    monkeypatch.setattr(gen, "_MAPS_ONLY_PAGES", {})
    monkeypatch.setattr(gen, "ALL_LANGUAGES", [("en", "English", ""), (ML, "தமிழ்", f"{ML}/")])
    monkeypatch.setitem(gen._I18N, ("regions", ML), {"a": {"title": "அ", "description": "அ"}})
    monkeypatch.setattr(gen, "lang_versions", lambda rid, *a, **k: {ML: {"size": 1, "url": "", "builder": "/b"}})
    gen.maps_only_pages([_entry("a"), _entry("b")])
    out = capsys.readouterr().out
    assert "WARNING" in out and "['b']" in out
