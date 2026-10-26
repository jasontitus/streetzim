"""Live cards link to the language builder page (/build/) only for a region
whose published recipes were made from a map file archive.org serves: the
one the card offers, or the one before it, still in the item until the
keep-2 cleanup deletes it (web/build/recipes/index.json, written by
ops/langpack/publish_site.sh): the builder downloads that base."""
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


MD5 = "0123456789abcdef0123456789abcdef"


def _region(root, rid, file, md5=MD5, menu=True, title=None):
    d = root / rid
    d.mkdir(parents=True)
    (d / "region.json").write_text(json.dumps({"id": rid, "title": title or rid.title(),
                                               "base": {"file": file, "md5": md5},
                                               "menu": [{"lang": "de"}] if menu else []}))


def test_regions_with_a_menu_and_their_base(tmp_path):
    _region(tmp_path, "greece", "osm-greece-2026-10-15.zim")
    _region(tmp_path, "iceland", "osm-iceland-2026-10-14.zim", menu=False)       # nothing to build
    (tmp_path / "broken").mkdir()
    assert gen.build_page_regions(str(tmp_path)) == {"greece": {"file": "osm-greece-2026-10-15.zim", "md5": MD5}}


def test_no_recipes_means_no_links(tmp_path):
    assert gen.build_page_regions(str(tmp_path / "missing")) == {}


def test_card_link():
    html = gen.render_live_card(REGION, "1 GB", build_page=True)
    assert 'href="/build/?region=greece"' in html and 'data-track="build"' in html
    assert ">Build in another language</a>" in html
    assert html.index('data-track="download"') < html.index('data-track="build"') < html.index('data-track="details"')
    assert 'data-track="build"' not in gen.render_live_card(REGION, "1 GB")
    de = gen.render_live_card(REGION, "1 GB", build_page=True, lang="de")
    assert ">In einer anderen Sprache</a>" in de


def test_link_only_for_the_recipes_base():
    b = {"greece": {"file": "osm-greece-2026-10-15.zim", "md5": MD5}}
    assert gen.builder_link(b, "greece", "osm-greece-2026-10-15.zim", MD5.upper())
    assert not gen.builder_link(b, "greece", "osm-greece-2026-11-02.zim", MD5)    # a newer map: recipes are stale
    assert not gen.builder_link(b, "greece", "osm-greece-2026-10-15.zim", "f" * 32)  # re-uploaded in place
    assert not gen.builder_link(b, "greece", "osm-greece-2026-10-15.zim", None)
    assert not gen.builder_link(b, "iceland", "osm-iceland-2026-10-14.zim", MD5)
    # a newer map is live, the recipes' base is still in the item: kept until new recipes
    files = [{"name": "osm-greece-2026-11-02.zim", "md5": "e" * 32}, {"name": "osm-greece-2026-10-15.zim", "md5": MD5}]
    assert gen.builder_link(b, "greece", "osm-greece-2026-11-02.zim", "e" * 32, files)
    assert not gen.builder_link(b, "greece", "osm-greece-2026-11-02.zim", "e" * 32, files[:1])           # deleted
    assert not gen.builder_link(b, "greece", "osm-greece-2026-11-02.zim", "e" * 32,
                                [files[0], {**files[1], "md5": "f" * 32}])                              # replaced
    assert not gen.builder_link({"greece": {**b["greece"], "md5": ""}}, "greece", "x.zim", "e" * 32,
                                [{"name": "osm-greece-2026-10-15.zim", "md5": ""}])                     # no MD5 to match
    # a third upload: keep-2 is deleting the recipes' base, though the listing still names it
    third = files + [{"name": "osm-greece-2026-11-09.zim", "md5": "d" * 32}]
    assert not gen.builder_link(b, "greece", "osm-greece-2026-11-09.zim", "d" * 32, third)
    assert gen.newest_zims(third) == {"osm-greece-2026-11-02.zim", "osm-greece-2026-11-09.zim"}


def test_the_page_lists_only_live_bases(tmp_path, monkeypatch):
    """Review r3 M-4 / M-5: the site build links a card and lists its region on
    /build/ only while archive.org's live ZIM is the recipes' base (name and
    MD5); a pruned or re-uploaded base drops off with the next deploy."""
    ids = [r["id"] for r in gen.REGIONS]
    monkeypatch.setattr(gen, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(gen, "BUILDER_LIVE", {})
    monkeypatch.setattr(gen, "BUILDER_NEWER", {})
    monkeypatch.setattr(gen, "fetch_archive_items", lambda: {i: {"item_size": 1} for i in ids})
    monkeypatch.setattr(gen, "fetch_item_details", lambda item_id, **kw: {
        "files": [{"name": f"osm-{item_id[len('streetzim-'):]}-2026-10-06.zim", "size": "5000000000",
                   "md5": MD5 if item_id != "streetzim-iceland" else "f" * 32}]
        + ([{"name": "osm-benelux-2026-10-26.zim", "size": "5000000000", "md5": "e" * 32}]
           if item_id == "streetzim-benelux" else []),
        "metadata": {}})
    rec = tmp_path / "build" / "recipes"
    _region(rec, "greece", "osm-greece-2026-10-06.zim", title="Greece")
    _region(rec, "iceland", "osm-iceland-2026-10-06.zim", title="Iceland")         # live file re-uploaded
    _region(rec, "baltics", "osm-baltics-2026-09-01.zim", title="Baltics")         # pruned: another live file
    _region(rec, "benelux", "osm-benelux-2026-10-06.zim", title="Benelux")         # a newer one live, this one kept
    gen.build_page()
    en = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert 'href="/build/?region=greece"' in en and 'href="/build/?region=benelux"' in en
    assert 'href="/build/?region=iceland"' not in en and 'href="/build/?region=baltics"' not in en
    idx = json.loads((rec / "index.json").read_text())
    assert [r["id"] for r in idx["regions"]] == ["benelux", "greece"]
    assert idx["regions"][0]["newer"] == "osm-benelux-2026-10-26.zim" and "newer" not in idx["regions"][1]
