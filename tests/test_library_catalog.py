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
    for rid, p in places.items():
        assert p["continents"] and set(p["continents"]) <= CONTINENTS, rid
        assert p["regions"] and set(p["regions"]) <= REGIONS_M49, rid
        # An area inside a country names that one country.
        assert not p["subregion"] or len(p["countries"]) == 1, rid
        # Only a continent map covers every country of its regions.
        if not p["countries"]:
            assert next(r for r in gen.REGIONS if r["id"] == rid)["tier"] == "continent", rid


def test_catalog_lists_live_maps_with_their_places(gen, tmp_path):
    places = {"x": {"continents": ["Asia"], "regions": ["Eastern Asia"], "countries": ["Taiwan"], "subregion": "Taipei"},
              "y": {"continents": ["Europe"], "regions": ["Northern Europe"], "countries": ["Iceland"], "subregion": ""}}
    library = {"y": {"id": "y", "title": "Iceland", "description": "d", "tier": "country", "language": "en",
                     "file": "osm-y-2026-10-26.zim", "size": 5, "date": "2026-10-26", "download": "u",
                     "details": "v", "torrent": None, "preview": None},
               "x": {"id": "x", "title": "Taipei", "description": "d", "tier": "local", "language": "en",
                     "file": "osm-x-2026-10-29.zim", "size": 7, "date": "2026-10-29", "download": "u",
                     "details": "v", "torrent": None, "preview": "/drive/?zim=u"}}
    ids = gen.write_library_catalog(library, {"x"}, places=places, out_dir=str(tmp_path))
    assert ids == ["y", "x"]                                     # by title
    doc = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))
    assert doc["format"] == "streetzim-library/1"
    assert [m["builder"] for m in doc["maps"]] == [False, True]
    assert doc["maps"][1]["places"]["subregion"] == "Taipei"
    assert not list(tmp_path.glob("catalog.json.*"))            # no temporary left
    with pytest.raises(ValueError, match="region-places"):
        gen.write_library_catalog({**library, "z": {**library["x"], "id": "z", "title": "Z"}}, set(),
                                  places=places, out_dir=str(tmp_path))


def test_descriptions_are_plain_text(gen):
    assert gen.plain_text("Spain, Portugal &amp; Andorra &mdash; Madrid") == "Spain, Portugal & Andorra — Madrid"
    assert gen.plain_text("<b>Hawai&lsquo;i</b>  big   island") == "Hawai‘i big island"


def test_the_page_reads_its_data_and_writes_text_not_html():
    page = (ROOT / "web" / "library" / "index.html").read_text(encoding="utf-8")
    assert "fetch('catalog.json')" in page and "fetch('../build/recipes/index.json')" in page
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
    gen.write_library_catalog(lib, set(), places=places)
    assert (tmp_path / "library" / "catalog.json").exists()
    assert gen.library_dir() == str(tmp_path / "library")
    assert (real.stat().st_mtime_ns if real.exists() else None) == before
