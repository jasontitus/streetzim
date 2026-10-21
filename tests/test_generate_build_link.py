"""Live cards link to the language builder page (/build/) only for a region
whose published recipes were made from the very map file the card offers
(web/build/recipes/index.json, written by ops/langpack/publish_site.sh): the
builder downloads that base, and archive.org's keep-2 cleanup deletes older
ones."""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen_build", ROOT / "web" / "generate.py")
gen = importlib.util.module_from_spec(spec)
sys.modules["gen_build"] = gen
spec.loader.exec_module(gen)

REGION = {"id": "greece", "title": "Greece", "zim_file": "osm-greece-2026-10-15.zim",
          "description": "Greece.", "tier": "country"}


def _index(tmp_path, regions):
    p = tmp_path / "index.json"
    p.write_text(json.dumps({"format": "streetzim-build-index/1", "regions": regions}))
    return p


def test_regions_with_a_menu_and_their_base(tmp_path):
    p = _index(tmp_path, [
        {"id": "greece", "base": {"file": "osm-greece-2026-10-15.zim"}, "menu": [{"lang": "de"}]},
        {"id": "iceland", "base": {"file": "osm-iceland-2026-10-14.zim"}, "menu": []},      # nothing to build
        {"id": "broken"},
    ])
    assert gen.build_page_regions(p) == {"greece": "osm-greece-2026-10-15.zim"}


def test_no_index_or_a_bad_one_means_no_links(tmp_path):
    assert gen.build_page_regions(tmp_path / "missing.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]")
    assert gen.build_page_regions(bad) == {}
    bad.write_text("{not json")
    assert gen.build_page_regions(bad) == {}


def test_card_link():
    html = gen.render_live_card(REGION, "1 GB", build_page=True)
    assert 'href="/build/?region=greece"' in html and 'data-track="build"' in html
    assert ">Build in another language</a>" in html
    assert html.index('data-track="download"') < html.index('data-track="build"') < html.index('data-track="details"')
    assert 'data-track="build"' not in gen.render_live_card(REGION, "1 GB")
    de = gen.render_live_card(REGION, "1 GB", build_page=True, lang="de")
    assert ">In einer anderen Sprache</a>" in de


def test_link_only_for_the_recipes_base():
    b = {"greece": "osm-greece-2026-10-15.zim"}
    assert gen.builder_link(b, "greece", "osm-greece-2026-10-15.zim")
    assert not gen.builder_link(b, "greece", "osm-greece-2026-11-02.zim")     # a newer map: recipes are stale
    assert not gen.builder_link(b, "iceland", "osm-iceland-2026-10-14.zim")
    assert not gen.builder_link({"x": ""}, "x", "")
