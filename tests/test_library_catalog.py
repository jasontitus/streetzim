"""The library page's data (web/library/): cloud/region-places.tsv has a row
for every map on the site, with known continents and regions, and
web/generate.py's write_library_catalog turns the live maps and their places
into web/library/catalog.json."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONTINENTS = {"Africa", "Asia", "Europe", "North America", "Oceania", "South America"}
REGIONS_M49 = {
    "Northern Africa", "Western Africa", "Middle Africa", "Eastern Africa", "Southern Africa",
    "Central Asia", "Eastern Asia", "South-eastern Asia", "Southern Asia", "Western Asia",
    "Northern Europe", "Western Europe", "Southern Europe", "Eastern Europe",
    "Northern America", "Central America", "Caribbean", "South America",
    "Australia and New Zealand", "Melanesia", "Micronesia", "Polynesia",
}


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("site_generate", ROOT / "web" / "generate.py")
    mod = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, ["generate.py"]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


def test_every_site_map_has_its_places(gen):
    places = gen.load_places()
    site = {r["id"] for r in gen.REGIONS}
    assert sorted(site - set(places)) == [] and sorted(set(places) - site) == []
    m49 = gen.load_m49()
    assert {r for r, _ in m49.values()} == REGIONS_M49 and {k for _, k in m49.values()} == CONTINENTS
    subregions = {p["subregion"] for p in places.values() if p["subregion"]}
    for rid, p in places.items():
        assert p["countries"] and set(p["countries"]) <= set(m49), rid
        assert p["continents"] and set(p["continents"]) <= CONTINENTS, rid
        # the regions are exactly those of its countries (plus "also")
        assert {m49[c][0] for c in p["countries"]} <= set(p["regions"]) <= REGIONS_M49, rid
        # an area, or a map that holds areas, is inside one country; the areas it holds are other maps'
        assert not (p["subregion"] or p["areas"]) or len(p["countries"]) == 1, rid
        assert set(p["areas"]) <= subregions - {p["subregion"]}, rid


def test_countries_sit_in_their_un_m49_regions(gen):
    """Spot checks of the table against UN M49, and of what follows from it."""
    m49 = gen.load_m49()
    for country, region in [("China", "Eastern Asia"), ("Iran", "Southern Asia"), ("Afghanistan", "Southern Asia"),
                            ("Zambia", "Eastern Africa"), ("Zimbabwe", "Eastern Africa"), ("Mauritania", "Western Africa"),
                            ("Sudan", "Northern Africa"), ("Cuba", "Caribbean"), ("Mexico", "Central America"),
                            ("Czechia", "Eastern Europe"), ("Cyprus", "Western Asia"), ("Georgia", "Western Asia"),
                            ("Tuvalu", "Polynesia"), ("Kiribati", "Micronesia"), ("Palau", "Micronesia"),
                            ("Taiwan", "Eastern Asia"), ("Kosovo", "Southern Europe"), ("Greenland", "Northern America")]:
        assert m49[country][0] == region, country
    places = gen.load_places()
    assert "Eastern Africa" in places["southern-africa"]["regions"]
    assert "Eastern Asia" in places["himalayas"]["regions"]
    assert {"Madagascar", "Morocco", "South Africa"} <= set(places["africa"]["countries"])
    assert {"Belarus", "Cyprus", "Georgia", "Iceland"} <= set(places["europe"]["countries"])
    assert "Asia" in places["russia"]["continents"]
    us = places["united-states"]["areas"]
    assert "Florida" in us and "Hawaii" not in us and "Alaska" not in us


def test_catalog_lists_live_maps_with_their_places(gen, tmp_path, capsys):
    places = {"x": {"continents": ["Asia"], "regions": ["Eastern Asia"], "countries": ["Taiwan"], "subregion": "Taipei"},
              "y": {"continents": ["Europe"], "regions": ["Northern Europe"], "countries": ["Iceland"], "subregion": ""}}
    library = {"y": {"id": "y", "title": "Iceland", "description": "d", "tier": "country", "language": "en",
                     "file": "osm-y-2026-10-26.zim", "size": 5, "date": "2026-10-26", "download": "u",
                     "details": "v", "torrent": None, "preview": None},
               "x": {"id": "x", "title": "Taipei", "description": "d", "tier": "local", "language": "en",
                     "file": "osm-x-2026-10-29.zim", "size": 7, "date": "2026-10-29", "download": "u",
                     "details": "v", "torrent": None, "preview": "/drive/?zim=u"}}
    versions = {"x": [{"lang": "zh", "label": "中文", "size": 9, "webseed": "https://w.example/merged/x/zh/u/f.zim"}]}
    ids = gen.write_library_catalog(library, versions, places=places, out_dir=str(tmp_path))
    assert ids == ["y", "x"]                                     # by title
    doc = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))
    assert doc["format"] == "streetzim-library/1"
    assert [m["builder"] for m in doc["maps"]] == [False, True]
    assert doc["maps"][0]["versions"] == [] and doc["maps"][1]["versions"] == [{**versions["x"][0], "size_label": "<1 MB"}]
    assert doc["maps"][1]["places"]["subregion"] == "Taipei"
    assert not list(tmp_path.glob("catalog.json.*"))            # no temporary left
    # a live map without a places row: listed without places, and a warning (the main page does not wait)
    gen.write_library_catalog({**library, "z": {**library["x"], "id": "z", "title": "Z"}}, {},
                              places=places, out_dir=str(tmp_path))
    assert "region-places.tsv" in capsys.readouterr().out
    z = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))["maps"][-1]
    assert z["id"] == "z" and z["places"]["countries"] == [] and z["places"]["areas"] == []


def test_descriptions_are_plain_text(gen):
    assert gen.plain_text("Spain, Portugal &amp; Andorra &mdash; Madrid") == "Spain, Portugal & Andorra — Madrid"
    assert gen.plain_text("<b>Hawai&lsquo;i</b>  big   island") == "Hawai‘i big island"


def test_the_page_reads_its_data_and_writes_text_not_html():
    page = (ROOT / "web" / "library" / "index.html").read_text(encoding="utf-8")
    assert "fetch('catalog.json')" in page and "recipes/index.json" not in page    # one small file
    # Data reaches the page as text; the one innerHTML is a constant icon.
    assert page.count(".innerHTML") == 1 and "dl.innerHTML = DL;" in page


def test_the_catalog_goes_where_script_dir_points(gen, tmp_path, monkeypatch):
    """A site build in a test (SCRIPT_DIR pointed at tmp) must not write the
    repository's web/library/catalog.json."""
    real = ROOT / "web" / "library" / "catalog.json"
    before = real.stat().st_mtime_ns if real.exists() else None
    monkeypatch.setattr(gen, "SCRIPT_DIR", str(tmp_path))
    (tmp_path / "library").mkdir()
    places = {"y": {"continents": ["Europe"], "regions": ["Northern Europe"], "countries": ["Iceland"], "subregion": ""}}
    lib = {"y": {"id": "y", "title": "Iceland", "description": "", "tier": "country", "language": "en", "file": "f",
                 "size": 1, "date": None, "download": "u", "details": "v", "torrent": None, "preview": None}}
    gen.write_library_catalog(lib, {}, places=places)
    assert (tmp_path / "library" / "catalog.json").exists()
    assert gen.library_dir() == str(tmp_path / "library")
    assert (real.stat().st_mtime_ns if real.exists() else None) == before


def test_builder_versions_come_from_the_build_index(gen, tmp_path):
    (tmp_path / "index.json").write_text(json.dumps({"regions": [
        {"id": "x", "menu": [{"lang": "de", "label": "Deutsch", "size": 3, "webseed": "https://w.example/merged/x/de/u/f.zim",
                              "recipe": "big", "packs": [1, 2]}, {"lang": "th", "size": 4}]},
        {"id": "y", "menu": []}]}), encoding="utf-8")
    v = gen.builder_versions(str(tmp_path))
    assert v == {"x": [{"lang": "de", "label": "Deutsch", "size": 3, "webseed": "https://w.example/merged/x/de/u/f.zim"},
                       {"lang": "th", "label": "th", "size": 4, "webseed": None}], "y": []}
    assert gen.builder_versions(str(tmp_path / "none")) == {}


def test_the_site_build_writes_the_catalog_the_main_page_shows(gen, tmp_path, monkeypatch):
    """Through build_page: each live map's size, size label, date and
    download link in the catalog are what its card on the main page shows,
    and the builder's versions come from build/recipes/index.json."""
    ids = [r["id"] for r in gen.REGIONS]
    monkeypatch.setattr(gen, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(gen, "BUILDER_LIVE", {})
    monkeypatch.setattr(gen, "BUILDER_NEWER", {})
    monkeypatch.setattr(gen, "fetch_archive_items", lambda: {i: {"item_size": 1} for i in ids})
    sizes = {i: 1_000_000_000 + 7_777_777 * n for n, i in enumerate(ids)}
    monkeypatch.setattr(gen, "fetch_item_details", lambda item_id, **kw: {
        "files": [{"name": f"osm-{item_id[len('streetzim-'):]}-2026-10-{6 + len(item_id) % 20:02d}.zim",
                   "size": str(sizes[item_id[len('streetzim-'):]]), "md5": "0" * 32}], "metadata": {}})
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    rec = tmp_path / "build" / "recipes" / "greece"
    rec.mkdir(parents=True)
    greece_file = f"osm-greece-2026-10-{6 + len('streetzim-greece') % 20:02d}.zim"
    (rec / "region.json").write_text(json.dumps({"id": "greece", "title": "Greece", "base": {"file": greece_file, "md5": "0" * 32},
                                                 "menu": [{"lang": "de", "label": "Deutsch", "size": 123456789,
                                                           "webseed": "https://w.example/merged/greece/de/u/f.zim"}]}))
    gen.build_page()
    doc = json.loads((tmp_path / "library" / "catalog.json").read_text(encoding="utf-8"))
    en = (tmp_path / "index.html").read_text(encoding="utf-8")
    by = {m["id"]: m for m in doc["maps"]}
    assert set(by) == set(ids)
    for rid in ("greece", "taipei", "africa"):
        m = by[rid]
        assert m["size"] == sizes[rid] and m["size_label"] == gen.human_size(sizes[rid]), m
        assert m["date"] and m["file"] in m["download"] and m["download"].startswith("https://archive.org/download/streetzim-" + rid + "/")
        assert m["download"] in en and m["size_label"] in en
    assert by["greece"]["versions"] == [{"lang": "de", "label": "Deutsch", "size": 123456789,
                                        "webseed": "https://w.example/merged/greece/de/u/f.zim",
                                        "size_label": gen.human_size(123456789)}]
    assert by["greece"]["builder"] and not by["taipei"]["builder"]
    assert doc["countries"]["China"] == "Eastern Asia" and doc["regions"]["Eastern Asia"] == "Asia"
