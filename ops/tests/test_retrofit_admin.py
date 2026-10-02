"""cloud/swap_viewer_rust.py --rebuild-search --add-admin-areas: a ZIM built
before administrative-area search gets the areas a build would have given it
(their search records, keyed as the writer keys them, their Kiwix pages and
redirects), and the search records are recovered and counted before anything
is written (docs/search-prefix-locality.md, "Retrofit").

The end-to-end tests write a small source ZIM with libzim, a tiny OSM extract
with tests/test_admin_areas.Osm, and run the retrofit with the Python packer.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud.search_shards import LEAF_SEP, leaf_for, prefixes_for, tier_for  # noqa: E402


def _svr():
    pytest.importorskip("libzim")
    pytest.importorskip("streetzim.zim_writer")
    os.environ.setdefault("STREETZIM_ROOT", str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "swap_viewer_rust_admin_under_test", ROOT / "ops" / "cloud" / "swap_viewer_rust.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- a small source ZIM -------------------------------------------------------

def _rule1_layout(records):
    """search-data as a rule-1 writer laid it out (every record in each of
    its rule-1 prefixes)."""
    chunks: dict = {}
    for r in records:
        for k in prefixes_for(r["n"], 1):
            chunks.setdefault(k, []).append(r)
    return chunks


SRC_RECORDS = [
    {"n": "Kathmandu", "t": "place", "s": "city", "a": 2.0, "o": 2.0, "l": "Testland"},
    {"n": "बागमती चोक", "t": "street", "s": "", "a": 2.1, "o": 2.1, "l": "Testland"},
    {"n": "Cafe Bagmati", "t": "poi", "s": "cafe", "a": 2.2, "o": 2.2, "l": "Testland"},
    {"n": "Lake Rara", "t": "water", "s": "lake", "a": 1.0, "o": 1.0, "l": "Testland"},
]
CAT_MANIFEST = {"chips": {"food": {"label": "Food", "count": 1}}, "categories": {}}


def _write_source(path, records, *, extra_pages=(), total=None,
                  bounds=(-1.0, -1.0, 4.5, 4.5)):
    from libzim.writer import Creator, Hint, Item, StringProvider

    class It(Item):
        def __init__(self, p, title, mime, data, front=False):
            super().__init__()
            self.p, self.t, self.m, self.d, self.f = p, title, mime, data, front

        def get_path(self):
            return self.p

        def get_title(self):
            return self.t

        def get_mimetype(self):
            return self.m

        def get_contentprovider(self):
            return StringProvider(self.d)

        def get_hints(self):
            return {Hint.FRONT_ARTICLE: self.f}

    chunks = _rule1_layout(records)
    manifest = {"total": len(records) if total is None else total, "keep_me": "yes",
                "chunks": {k: len(v) for k, v in chunks.items()}}
    with Creator(str(path)).config_indexing(False, "eng") as c:
        c.set_mainpath("index.html")
        c.add_metadata("Title", "Test")
        c.add_item(It("index.html", "Map", "text/html", "<html>old viewer</html>", True))
        c.add_item(It("map-config.json", "", "application/json",
                      json.dumps({"name": "T", "bounds": list(bounds)})))
        c.add_item(It("category-index/manifest.json", "", "application/json",
                      json.dumps(CAT_MANIFEST)))
        c.add_item(It("category-index/chip-food.json", "", "application/json",
                      json.dumps([records[2]])))
        c.add_item(It("search-data/manifest.json", "Search Manifest", "application/json",
                      json.dumps(manifest)))
        for k, v in chunks.items():
            c.add_item(It(f"search-data/{k}.json", f"Search chunk {k}", "application/json",
                          json.dumps(v)))
        for p, title in extra_pages:
            c.add_item(It(p, title, "text/html", f"<html>{title}</html>", True))
    return path


@pytest.fixture
def osm(tmp_path):
    pytest.importorskip("osmium")
    if not shutil.which("osmium"):
        pytest.skip("osmium CLI not installed")
    from tests.test_admin_areas import Osm, square
    o = Osm()
    o.way(1, square(0, 0, 4, 4))
    o.rel(1, [("w", 1, "outer")], {"name": "Testland", "admin_level": "2",
                                   "ISO3166-1": "TL"})
    o.way(2, square(0, 2, 4, 4))
    o.rel(2, [("w", 2, "outer")], {"name": "बागमती प्रदेश", "name:en": "Bagmati Province",
                                   "admin_level": "4", "wikidata": "Q2",
                                   "wikipedia": "ne:बागमती प्रदेश"})
    o.way(3, square(1, 2.5, 3, 3.5))
    o.rel(3, [("w", 3, "outer")], {"name": "Kathmandu", "admin_level": "6",
                                   "border_type": "district", "alt_name": "Kantipur"})
    # Outside the ZIM's box: not added.
    o.way(4, square(10, 10, 11, 11))
    o.rel(4, [("w", 4, "outer")], {"name": "Faraway", "admin_level": "6"})
    return o.write(tmp_path / "x.osm")


def _run(svr, src, dst, tmp, monkeypatch, **kw):
    monkeypatch.setattr(svr, "_on_root_fs", lambda p: False)   # Docker's /tmp
    monkeypatch.setattr(svr, "_fs_type", lambda p: "ext4")
    tmp.mkdir(exist_ok=True)
    return svr.swap_viewer_rust(str(src), str(dst), rebuild_search=True,
                                tmp_dir=str(tmp), **kw)


@pytest.fixture
def no_rg(monkeypatch):
    """No reverse_geocoder lookups (regions come from the polygons here)."""
    from streetzim import admin_areas as A
    monkeypatch.setattr(A, "_rg_lookup", lambda pts: [{} for _ in pts])
    monkeypatch.setattr(A.GeoNamesPlaces, "load", classmethod(lambda cls: None))


def _read(a, path):
    return bytes(a.get_entry_by_path(path).get_item().content)


def _leaves(manifest, rec):
    """The leaves a reader fetches to find ``rec`` by each of its keys: the
    prefix's own chunk, or for a character-split prefix the leaf leaf_for
    picks among the planned paths of the record's tier."""
    out: dict[str, set] = {}
    keys = prefixes_for(rec["n"])
    for alt in rec.get("alt") or ():
        keys |= prefixes_for(alt)
    for k in keys:
        if k in manifest["chunks"]:
            out[k] = {k}
            continue
        planned: dict[str, set] = {}
        for name in manifest["chunks"]:
            parts = name.split(LEAF_SEP)
            if parts[0] == k and len(parts) > 2:
                planned.setdefault(parts[-1], set()).add(tuple(parts[1:-1]))
        out[k] = set(leaf_for(k, rec, planned.get(tier_for(rec), ())))
    return out


def _front_titles(a):
    """Titles of the front articles: the entries listing/titleOrdered/v1
    (an array of little-endian u32 entry indexes) names."""
    import struct
    for i in range(a.entry_count, a.all_entry_count):
        e = a._get_entry_by_id(i)
        if e.path == "listing/titleOrdered/v1":
            raw = bytes(e.get_item().content)
            ids = struct.unpack(f"<{len(raw) // 4}I", raw)
            return {a._get_entry_by_id(j).title for j in ids}
    raise AssertionError("no listing/titleOrdered/v1")


def _all_records(a, manifest):
    seen = []
    for name in manifest["chunks"]:
        seen += json.loads(_read(a, f"search-data/{name}.json"))
    return seen


@pytest.mark.parametrize("hot", [False, True])
def test_admin_areas_are_added_searchable_with_pages(tmp_path, osm, no_rg, monkeypatch,
                                                     capsys, hot):
    """``hot``: prefix "ka" (Kathmandu) over the split threshold (lowered to
    1 MiB here), so the admin record lands in a character-split leaf."""
    svr = _svr()
    from libzim.reader import Archive
    from streetzim import zim_writer as W
    records = list(SRC_RECORDS)
    if hot:
        monkeypatch.setattr(svr, "SEARCH_HOT_BYTES", 1024 * 1024)
        records += [{"n": f"Kathmandu Shop {i}", "t": "poi", "s": "shop", "a": 2.0,
                     "o": 2.0, "l": "Testland"} for i in range(15000)]
    src = _write_source(tmp_path / "src.zim", records)
    dst = tmp_path / "out" / "dst.zim"
    dst.parent.mkdir()
    _run(svr, src, dst, tmp_path / "spill", monkeypatch, add_admin_areas=osm)
    a = Archive(str(dst))
    m = json.loads(_read(a, "search-data/manifest.json"))
    admin = [r for r in _all_records(a, m) if r["t"] == "admin"]
    names = {r["n"] for r in admin}
    assert names == {"Testland", "Bagmati Province", "Kathmandu"}
    # The count stays exact: the recovered records plus the ones added.
    assert m["total"] == len(records) + 3
    assert m["word_rule"] == 2 and m["keep_me"] == "yes"
    assert bool(m.get("char_split", {}).get("ka")) == hot
    # Every source record is still there, once per prefix it belongs in.
    for r in SRC_RECORDS:
        for k, leaves in _leaves(m, r).items():
            assert any(r in json.loads(_read(a, f"search-data/{lf}.json")) for lf in leaves), (r["n"], k)
    # Each admin record is found under its name's and its other names' keys.
    for r in admin:
        hits = _leaves(m, r)
        assert hits and all(hits.values()), r["n"]
        for k, leaves in hits.items():
            assert any(r in json.loads(_read(a, f"search-data/{lf}.json")) for lf in leaves), (r["n"], k)
    kat_rec = next(r for r in admin if r["n"] == "Kathmandu")
    assert all((LEAF_SEP in lf) == hot for lf in _leaves(m, kat_rec)["ka"])
    bag = next(r for r in admin if r["n"] == "Bagmati Province")
    assert bag["alt"] == ["बागमती प्रदेश"] and bag["al"] == 4 and bag["s"] == "region"
    assert bag["l"] == "Testland" and bag["bb"] == [0.0, 2.0, 4.0, 4.0]
    # The relation's own tags, as they are (no Wikimedia lookup).
    assert bag["w"] == "ne:बागमती प्रदेश" and bag["q"] == "Q2" and "wsrc" not in bag
    assert any(k.startswith("u") for k in _leaves(m, bag))      # the Devanagari key
    # The record is what the writer writes for the feature.
    feats = {f["name"]: f for f in svr._extract_admin(osm, [-1, -1, 4.5, 4.5], tmp_path)}
    assert bag == W.search_record(feats["Bagmati Province"],
                                  W.admin_wiki(feats["Bagmati Province"], None))

    # Kiwix pages, numbered after the source's page-type records (place,
    # water: 2), and their other titles as front-article redirects.
    out = capsys.readouterr().out
    assert "wrote 3 Kiwix page(s)" in out
    first = 2
    order = [f["name"] for f in svr._extract_admin(osm, [-1, -1, 4.5, 4.5], tmp_path)]
    for i, nm in enumerate(order):
        path, title, page_html = W.search_page(feats[nm], first + i)
        e = a.get_entry_by_path(path)
        assert e.title == title and _read(a, path).decode() == page_html
        for k, alt in enumerate(W.kiwix_alt_titles(feats[nm])):
            r = a.get_entry_by_path(f"{path[:-5]}~{k}.html")
            assert r.is_redirect and r.title == alt
            assert r.get_redirect_entry().path == path
    kat = W.search_page(feats["Kathmandu"], first + order.index("Kathmandu"))
    assert kat[1] == "Kathmandu (district)"
    # Front articles (listing/titleOrdered/v1): the pages and their other
    # titles, as a build writes them.
    front = _front_titles(a)
    assert {"Testland (country)", "Bagmati Province (region)", "Kathmandu (district)",
            "Kantipur", "बागमती प्रदेश"} <= front
    # Kiwix's suggestions come from the Xapian title index when there is one,
    # and that is the source's, copied: it does not know the new pages
    # (docs/search-prefix-locality.md#retrofit).
    from libzim.suggestion import SuggestionSearcher
    assert a.has_title_index
    assert SuggestionSearcher(a).suggest("Kantipur").getEstimatedMatches() == 0
    # Front articles are not the main page (a packer that took the last
    # front item for it opened Kathmandu's page instead of the map).
    main = a.main_entry
    main = main.get_redirect_entry() if main.is_redirect else main
    assert main.path == "index.html"
    # The Find chips and the rest are untouched; the viewer is swapped.
    assert _read(a, "category-index/manifest.json") == json.dumps(CAT_MANIFEST).encode()
    assert b"old viewer" not in _read(a, "index.html")
    assert not list(dst.parent.glob("*.pack-stage-*"))

    # Run again on the output: the admin records are not added twice.
    dst2 = tmp_path / "out" / "dst2.zim"
    _run(svr, dst, dst2, tmp_path / "spill2", monkeypatch, add_admin_areas=osm)
    assert "already has 3 administrative-area record(s)" in capsys.readouterr().out
    b = Archive(str(dst2))
    m2 = json.loads(_read(b, "search-data/manifest.json"))
    assert m2["total"] == m["total"]
    assert sorted(r["n"] for r in _all_records(b, m2) if r["t"] == "admin") == \
        sorted(r["n"] for r in _all_records(a, m) if r["t"] == "admin")


def test_a_page_that_would_replace_a_source_entry_stops_before_writing(
        tmp_path, osm, no_rg, monkeypatch):
    svr = _svr()
    # Testland is the first area: search/testland-2.html.
    src = _write_source(tmp_path / "src.zim", SRC_RECORDS,
                        extra_pages=[("search/testland-2.html", "Testland")])

    def no_creator(*a, **k):
        raise AssertionError("the packer was started")
    monkeypatch.setattr(svr, "ManifestCreator", no_creator)
    with pytest.raises(SystemExit, match="testland-2.html"):
        _run(svr, src, tmp_path / "dst.zim", tmp_path / "spill", monkeypatch,
             add_admin_areas=osm)


@pytest.mark.parametrize("fault", ["total", "leaf", "pbf"])
def test_failures_stop_before_the_packer_starts(tmp_path, osm, no_rg, monkeypatch, fault):
    svr = _svr()
    kw = {"add_admin_areas": osm}
    if fault == "total":
        src = _write_source(tmp_path / "src.zim", SRC_RECORDS, total=len(SRC_RECORDS) + 1)
    elif fault == "pbf":
        src = _write_source(tmp_path / "src.zim", SRC_RECORDS)
        kw["add_admin_areas"] = str(tmp_path / "missing.osm.pbf")
    else:
        src = _write_source(tmp_path / "src.zim", SRC_RECORDS)
        real = svr._source_records

        def drop_a_leaf(src_bytes, manifest, spool):
            m = dict(manifest, chunks=dict(manifest["chunks"], zz=1))
            return real(src_bytes, m, spool)
        monkeypatch.setattr(svr, "_source_records", drop_a_leaf)
    created = []
    monkeypatch.setattr(svr, "ManifestCreator", lambda *a, **k: created.append(a))
    with pytest.raises(SystemExit):
        _run(svr, src, tmp_path / "dst.zim", tmp_path / "spill", monkeypatch, **kw)
    assert not created
    assert not list(tmp_path.glob("dst.zim*"))


def test_add_admin_areas_needs_rebuild_search(tmp_path):
    svr = _svr()
    with pytest.raises(SystemExit, match="needs --rebuild-search"):
        svr.swap_viewer_rust("a.zim", "b.zim", add_admin_areas="x.pbf")
    with pytest.raises(SystemExit, match="W,S,E,N"):
        svr._parse_bbox("1,2,3")
