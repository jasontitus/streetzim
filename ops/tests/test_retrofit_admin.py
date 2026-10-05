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
        for p, title, *body in extra_pages:
            c.add_item(It(p, title, "text/html",
                          body[0] if body else f"<html>{title}</html>", not body))
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


@pytest.fixture
def fake_xb(tmp_path):
    """A stand-in xapianbuilder (writes a placeholder database), for the
    tests that check pages, not Kiwix's search; those use the real one."""
    p = tmp_path / "fake-xapianbuilder"
    p.write_text(f"#!{sys.executable}\n"
                 "import sys\n"
                 "a = sys.argv\n"
                 "open(a[a.index('--output') + 1], 'wb').write(b'not a xapian db')\n")
    p.chmod(0o755)
    return str(p)


@pytest.mark.parametrize("hot", [False, True])
def test_admin_areas_are_added_searchable_with_pages(tmp_path, osm, no_rg, monkeypatch,
                                                     capsys, hot, fake_xb):
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
    _run(svr, src, dst, tmp_path / "spill", monkeypatch, add_admin_areas=osm,
         rebuild_xapian=True, xapianbuilder_bin=fake_xb)
    a = Archive(str(dst))
    m = json.loads(_read(a, "search-data/manifest.json"))
    admin = [r for r in _all_records(a, m) if r["t"] == "admin"]
    names = {r["n"] for r in admin}
    assert names == {"Testland", "Bagmati Province", "Kathmandu"}
    # The count stays exact: the recovered records plus the ones added.
    assert m["total"] == len(records) + 3
    assert m["word_rule"] == 2 and m["keep_me"] == "yes"
    # A plan that groups small siblings is filed under char_ranges.
    split = {**m.get("char_split", {}), **m.get("char_ranges", {})}
    assert bool(split.get("ka")) == hot
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
    # The records of a page type have their pages too (place, water: 0, 1).
    assert {"Kathmandu", "Lake Rara"} <= front
    # Front articles are not the main page (a packer that took the last
    # front item for it opened Kathmandu's page instead of the map).
    main = a.main_entry
    main = main.get_redirect_entry() if main.is_redirect else main
    assert main.path == "index.html"
    # The Find chips and the rest are untouched; the viewer is swapped.
    assert _read(a, "category-index/manifest.json") == json.dumps(CAT_MANIFEST).encode()
    assert b"old viewer" not in _read(a, "index.html")
    assert not list(dst.parent.glob("*.pack-stage-*"))

    # Run again on the output: the admin records are not added twice (and,
    # without --rebuild-xapian, no page is written).
    dst2 = tmp_path / "out" / "dst2.zim"
    _run(svr, dst, dst2, tmp_path / "spill2", monkeypatch, add_admin_areas=osm)
    assert "already has 3 administrative-area record(s)" in capsys.readouterr().out
    b = Archive(str(dst2))
    m2 = json.loads(_read(b, "search-data/manifest.json"))
    assert m2["total"] == m["total"]
    assert sorted(r["n"] for r in _all_records(b, m2) if r["t"] == "admin") == \
        sorted(r["n"] for r in _all_records(a, m) if r["t"] == "admin")


def test_without_rebuild_xapian_no_page_is_written(tmp_path, osm, no_rg, monkeypatch):
    """Kiwix pages are the documents of Kiwix's own search: they come with
    --rebuild-xapian (as a --xapian=builder build writes none), not alone."""
    svr = _svr()
    from libzim.reader import Archive
    src = _write_source(tmp_path / "src.zim", SRC_RECORDS)
    dst = tmp_path / "dst.zim"
    _run(svr, src, dst, tmp_path / "spill", monkeypatch, add_admin_areas=osm)
    a = Archive(str(dst))
    assert not any(a._get_entry_by_id(i).path.startswith("search/")
                   for i in range(a.entry_count))
    m = json.loads(_read(a, "search-data/manifest.json"))
    assert m["total"] == len(SRC_RECORDS) + 3


def test_kiwix_poi_pages_number_like_the_build(tmp_path, osm, no_rg, monkeypatch, fake_xb):
    svr = _svr()
    from libzim.reader import Archive
    from streetzim import zim_writer as W
    src = _write_source(tmp_path / "src.zim", SRC_RECORDS)
    dst = tmp_path / "dst.zim"
    _run(svr, src, dst, tmp_path / "spill", monkeypatch, add_admin_areas=osm,
         rebuild_xapian=True, xapianbuilder_bin=fake_xb, kiwix_poi_pages=True)
    a = Archive(str(dst))
    front = _front_titles(a)
    assert "Cafe Bagmati" in front
    # place, poi, water before the areas: Testland is page 3.
    testland = next(f for f in svr._extract_admin(osm, [-1, -1, 4.5, 4.5], tmp_path)
                    if f["name"] == "Testland")
    assert a.get_entry_by_path(W.search_page(testland, 3)[0]).title == "Testland (country)"


def test_a_failure_before_with_removes_the_pack_stage(tmp_path, monkeypatch):
    svr = _svr()

    def boom(self, path):
        raise RuntimeError("boom")
    monkeypatch.setattr(svr.ManifestCreator, "set_mainpath", boom)
    with pytest.raises(RuntimeError, match="boom"):
        svr._open_creator(str(tmp_path / "dst.zim"), "index.html")
    assert not list(tmp_path.glob("dst.zim.pack-stage-*"))


def test_record_type_reads_the_key_not_a_name():
    svr = _svr()
    for rec in [{"n": 'x"t":"admin', "t": "poi"}, {"n": "\\", "t": "place"},
                {"n": "a", "t": "w\u00e4ter"}, {"n": "b", "s": "t", "t": ""},
                {"n": "a", "cat": "z", "t": "poi"}]:
        line = json.dumps(rec, separators=(",", ":"))
        assert svr._record_type(line) == rec["t"], line


def test_an_antimeridian_bbox_in_rfc7946_form(tmp_path, no_rg):
    svr = _svr()
    pytest.importorskip("osmium")
    if not shutil.which("osmium"):
        pytest.skip("osmium CLI not installed")
    from tests.test_admin_areas import Osm, square
    assert svr._parse_bbox("178,-1,-178,2") == [178.0, -1.0, 182.0, 2.0]
    o = Osm()
    o.way(1, square(179.0, 0.0, 179.5, 1.0))
    o.rel(1, [("w", 1, "outer")], {"name": "East Isle", "admin_level": "6"})
    o.way(2, square(-179.5, 0.0, -179.0, 1.0))
    o.rel(2, [("w", 2, "outer")], {"name": "West Isle", "admin_level": "6"})
    o.way(3, square(10.0, 0.0, 11.0, 1.0))
    o.rel(3, [("w", 3, "outer")], {"name": "Far Isle", "admin_level": "6"})
    pbf = o.write(tmp_path / "am.osm")
    names = {f["name"] for f in svr._extract_admin(pbf, svr._parse_bbox("178,-1,-178,2"),
                                                   tmp_path)}
    assert names == {"East Isle", "West Isle"}


def test_no_area_in_the_box_is_an_error(tmp_path, osm, no_rg, monkeypatch):
    svr = _svr()
    src = _write_source(tmp_path / "src.zim", SRC_RECORDS)
    monkeypatch.setattr(svr, "ManifestCreator", lambda *a, **k: pytest.fail("packer started"))
    with pytest.raises(SystemExit, match="no administrative area"):
        _run(svr, src, tmp_path / "dst.zim", tmp_path / "spill", monkeypatch,
             add_admin_areas=osm, bbox="50,50,51,51")


def test_wikidata_titles_offline_like_resolve_wikidata_titles(tmp_path):
    svr = _svr()
    geo = {"Bagmati_Province": [27.0, 85.0, "place", "Q2", ""]}
    cache = tmp_path / "qid.json"
    cache.write_text(json.dumps({"Q1": "Republic of Testland", "Q9": ""}))
    blobs = {"wiki-geo-index.json": json.dumps(geo).encode()}
    titles = svr._qid_titles(blobs.__getitem__, blobs.__contains__, str(cache))
    assert titles == {"Q1": "Republic of Testland", "Q2": "Bagmati_Province"}
    feats = [
        {"name": "Testland", "type": "admin", "lat": 1, "lon": 1, "osm": "r1",
         "wikidata": "Q1"},                                       # Q-ID only
        {"name": "Bagmati Province", "type": "admin", "lat": 1, "lon": 1, "osm": "r2",
         "wikidata": "Q2", "wikipedia": "ne:बागमती प्रदेश"},          # not English
        {"name": "Kathmandu", "type": "admin", "lat": 1, "lon": 1, "osm": "r3",
         "wikidata": "Q3", "wikipedia": "en:Kathmandu District"},  # English: kept
        {"name": "Nowhere", "type": "admin", "lat": 1, "lon": 1, "osm": "r4",
         "wikidata": "Q9"},                                       # no article
    ]
    spool = tmp_path / "s.jsonl"
    spool.write_text("")
    recs = [json.loads(x) for x in svr._plan_admin(feats, spool, titles)]
    assert recs[0]["w"] == "en:Republic_of_Testland" and recs[0]["wsrc"] == "wd"
    assert recs[1]["w"] == "en:Bagmati_Province" and recs[1]["wsrc"] == "wd"
    assert recs[2]["w"] == "en:Kathmandu District" and "wsrc" not in recs[2]
    assert "w" not in recs[3] and recs[3]["q"] == "Q9"
    assert spool.read_text().count("\n") == 4
    # Without a lookup: the relation's own tags.
    recs = [json.loads(x) for x in svr._plan_admin(feats, spool, {})]
    assert recs[1]["w"] == "ne:बागमती प्रदेश" and "w" not in recs[0]


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


# ---- --rebuild-xapian ---------------------------------------------------------

def _xapianbuilder():
    from streetzim.zim_writer import _resolve_xapianbuilder_binary
    try:
        return _resolve_xapianbuilder_binary(None)
    except FileNotFoundError:
        pytest.skip("xapianbuilder not built (XAPIANBUILDER_BIN)")


def _kiwix_hits(a, text):
    """Paths Kiwix's title suggestions and full-text search return."""
    from libzim.search import Query, Searcher
    from libzim.suggestion import SuggestionSearcher
    s = SuggestionSearcher(a).suggest(text)
    sugg = list(s.getResults(0, 50))
    f = Searcher(a).search(Query().set_query(text))
    return sugg, list(f.getResults(0, 50))


def _title(a, path):
    e = a.get_entry_by_path(path)
    return e.title, (e.get_redirect_entry().path if e.is_redirect else None)


@pytest.mark.parametrize("admin", [True, False])
def test_rebuild_xapian_points_kiwix_search_at_pages_that_exist(
        tmp_path, osm, no_rg, monkeypatch, admin):
    svr = _svr()
    from libzim.reader import Archive
    from streetzim import zim_writer as W
    xb = _xapianbuilder()
    src = _write_source(tmp_path / "src.zim", SRC_RECORDS, extra_pages=[
        ("wiki-article/Rara_Lake", "Rara Lake",
         "<html><body><p>The deepest lake of the Himalayan zanzibarite.</p></body></html>")])
    dst = tmp_path / "out" / "dst.zim"
    dst.parent.mkdir()
    kw = {"add_admin_areas": osm} if admin else {}
    _run(svr, src, dst, tmp_path / "spill", monkeypatch, rebuild_xapian=True,
         xapianbuilder_bin=xb, **kw)
    a = Archive(str(dst))
    assert a.has_title_index and a.has_fulltext_index
    # A page for each record of a page type (place, water; not the street or
    # the cafe), as --xapian=libzim writes them, and none for the others.
    titles = {}
    for i in range(a.entry_count):
        e = a._get_entry_by_id(i)
        if e.path.startswith("search/") and not e.is_redirect:
            titles[e.title] = e.path
    want = {"Kathmandu", "Lake Rara"} | (
        {"Testland (country)", "Bagmati Province (region)", "Kathmandu (district)"}
        if admin else set())
    assert set(titles) == want
    page = _read(a, titles["Lake Rara"]).decode()
    assert '<p class="kind">Lake</p>' in page and "map=14/1.0/1.0" in page
    # Every suggestion and full-text hit is an entry of the archive (the
    # builder's s/<n> documents had none), and the pages are found by name.
    queries = ["Lake Rara", "Kathmandu", "Rara"]
    if admin:
        queries += ["Kantipur", "बागमती प्रदेश", "Bagmati Province", "Testland"]
    found = {}
    for q in queries:
        sugg, full = _kiwix_hits(a, q)
        for p in sugg + full:
            assert a.has_entry_by_path(p), (q, p)
            assert not p.startswith("s/")
        found[q] = ({_title(a, p)[0] for p in sugg}, {_title(a, p)[0] for p in full})
    assert "Lake Rara" in found["Lake Rara"][0] and "Lake Rara" in found["Rara"][1]
    # The bundled Wikipedia articles are in the full text, as libzim indexes
    # them, and not in the title index (not front articles).
    sugg, full = _kiwix_hits(a, "zanzibarite")
    assert full == ["wiki-article/Rara_Lake"] and sugg == []
    assert not any(p.startswith("wiki-article/") for p in _kiwix_hits(a, "Rara Lake")[0])
    assert "Kathmandu" in found["Kathmandu"][0]
    assert not any("Cafe" in t for t in found["Kathmandu"][0] | found["Kathmandu"][1])
    if admin:
        assert "Kathmandu (district)" in found["Kathmandu"][0]
        # The other names: the redirect title, which leads to the page.
        assert "Kantipur" in found["Kantipur"][0]
        assert "Bagmati Province (region)" in found["बागमती प्रदेश"][1]
        assert "बागमती प्रदेश" in found["बागमती प्रदेश"][0]
        assert "Testland (country)" in found["Testland"][0]
        kat = W.search_page(next(f for f in svr._extract_admin(osm, [-1, -1, 4.5, 4.5], tmp_path)
                                 if f["name"] == "Kathmandu"), 2 + 1 + 1)
        assert _title(a, kat[0])[0] == "Kathmandu (district)"
    main = a.main_entry
    assert (main.get_redirect_entry() if main.is_redirect else main).path == "index.html"


def test_rebuild_xapian_refuses_a_zim_that_has_its_pages(tmp_path, monkeypatch, fake_xb):
    svr = _svr()
    xb = fake_xb
    src = _write_source(tmp_path / "src.zim", SRC_RECORDS,
                        extra_pages=[("search/kathmandu-0.html", "Kathmandu")])
    monkeypatch.setattr(svr, "ManifestCreator", lambda *a, **k: pytest.fail("packer started"))
    with pytest.raises(SystemExit, match="already has Kiwix search pages"):
        _run(svr, src, tmp_path / "dst.zim", tmp_path / "spill", monkeypatch,
             rebuild_xapian=True, xapianbuilder_bin=xb)


def test_rebuild_xapian_needs_rebuild_search():
    svr = _svr()
    with pytest.raises(SystemExit, match="needs --rebuild-search"):
        svr.swap_viewer_rust("a.zim", "b.zim", rebuild_xapian=True)


def test_feature_of_inverts_search_record():
    svr = _svr()
    from streetzim import zim_writer as W
    feat = {"name": "Kathmandu", "type": "admin", "subtype": "district", "lat": 27.70832,
            "lon": 85.32058, "location": "Bagmati", "admin_level": 6, "osm": "r1",
            "bbox": [85.1, 27.5, 85.5, 27.8], "alt": ["Kantipur"], "cat": "x", "brand": "B"}
    assert svr._feature_of(W.search_record(feat)) == feat


def test_a_control_character_in_a_name_is_not_in_its_page_title(tmp_path, monkeypatch, fake_xb):
    """Both packers refuse a title with a control character; OSM has names
    with a newline ("Tunda\\nBhuj (hot spring?!)", himalayas)."""
    svr = _svr()
    from libzim.reader import Archive
    from streetzim import zim_writer as W
    recs = SRC_RECORDS + [{"n": "Tunda\nBhuj", "t": "place", "s": "village", "a": 1.5,
                           "o": 1.5, "l": "Testland", "alt": ["x\ty"]}]
    src = _write_source(tmp_path / "src.zim", recs)
    dst = tmp_path / "dst.zim"
    _run(svr, src, dst, tmp_path / "spill", monkeypatch, rebuild_xapian=True,
         xapianbuilder_bin=fake_xb)
    assert "Tunda Bhuj" in _front_titles(Archive(str(dst)))
    assert W.kiwix_alt_titles({"name": "A\nB", "type": "admin", "subtype": "town",
                               "alt": ["C\tD"]}) == ["Town of A B", "C D"]


def test_the_extraction_runs_in_a_child_with_its_scratch_in_the_spill(
        tmp_path, osm, monkeypatch):
    svr = _svr()
    import subprocess
    real = subprocess.run
    seen = []

    def run(cmd, **kw):
        seen.append((cmd, (kw.get("env") or {}).get("TMPDIR")))
        return real(cmd, **kw)
    monkeypatch.setattr(subprocess, "run", run)
    work = tmp_path / "work"
    work.mkdir()
    feats = svr._extract_admin(osm, [-1, -1, 4.5, 4.5], work)
    assert {f["name"] for f in feats} == {"Testland", "Bagmati Province", "Kathmandu"}
    assert len(seen) == 1 and seen[0][0][0] == sys.executable and seen[0][1] == str(work)


def test_a_redirect_path_that_exists_stops_the_plan(tmp_path, fake_xb):
    svr = _svr()
    from streetzim import zim_writer as W
    feat = {"name": "Kathmandu", "type": "admin", "subtype": "district", "lat": 1.0,
            "lon": 1.0, "location": "B", "admin_level": 6, "osm": "r3", "alt": ["Kantipur"]}
    spool = tmp_path / "s.jsonl"
    spool.write_text(json.dumps(W.search_record(feat), separators=(",", ":")) + "\n")
    page = W.search_page(feat, 0)[0]
    taken = {page[:-5] + "~0.html"}
    with pytest.raises(SystemExit, match="~0.html"):
        svr._plan_xapian(spool, [], 0, tmp_path, taken.__contains__, fake_xb,
                         W.KIWIX_PAGE_TYPES)


def test_the_geo_index_wins_over_the_title_cache(tmp_path):
    svr = _svr()
    geo = {"Bagmati_Province": [27.0, 85.0, "place", "Q2", ""]}
    cache = tmp_path / "qid.json"
    cache.write_text(json.dumps({"Q2": "Bagmati Pradesh (old title)"}))
    blobs = {"wiki-geo-index.json": json.dumps(geo).encode()}
    assert svr._qid_titles(blobs.__getitem__, blobs.__contains__, str(cache)) == {
        "Q2": "Bagmati_Province"}


def test_title_text_collapses_the_spaces_a_control_character_leaves():
    from streetzim.zim_writer import _title_text
    assert _title_text("a\t\tb") == "a b"
    assert _title_text("a \n b") == "a b"
    assert _title_text(" x\ny ") == "x y"
    assert _title_text("plain  two") == "plain  two"      # no control character: as is


def test_geonames_credit_of_the_sources_admin_records(tmp_path):
    svr = _svr()
    from streetzim import zim_writer as W
    recs = [
        {"n": "Nepal", "t": "admin", "al": 2, "a": 28, "o": 84, "l": "", "osm": "r1",
         "bb": [80, 26, 88, 31]},
        {"n": "Bagmati", "t": "admin", "al": 4, "a": 27.5, "o": 85.3, "l": "Nepal",
         "osm": "r2", "bb": [84, 26.5, 86.5, 28.5]},
        # Region from the polygons: no credit.
        {"n": "Kathmandu", "t": "admin", "al": 6, "a": 27.7, "o": 85.3, "l": "Bagmati",
         "osm": "r3", "bb": [85.1, 27.5, 85.6, 27.9]},
        # Region named by GeoNames (no area of that name holds it): credit.
        {"n": "Lalitpur", "t": "admin", "al": 6, "a": 27.6, "o": 85.3, "l": "Central",
         "osm": "r4", "bb": [85.2, 27.4, 85.5, 27.7]},
        # Clipped (no box): its point may be GeoNames'.
        {"n": "Clip", "t": "admin", "al": 8, "a": 27.6, "o": 85.3, "l": "Bagmati",
         "osm": "r5"},
        # Named after an area that does not hold it.
        {"n": "Far", "t": "admin", "al": 6, "a": 30.5, "o": 81, "l": "Bagmati",
         "osm": "r6", "bb": [80.9, 30.4, 81.1, 30.6]},
        {"n": "Cafe", "t": "poi", "a": 1, "o": 1, "osm": "r7"},
    ]
    spool = tmp_path / "s.jsonl"
    spool.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs))
    assert svr._geonames_credited(spool) == {"r4", "r5", "r6"}
    # ... and a credited source record's rebuilt page carries the credit.
    pages, n = svr._plan_xapian(spool, [], 0, tmp_path, lambda p: False,
                                _fake_builder(tmp_path), W.KIWIX_PAGE_TYPES,
                                geonames={"r4"})
    rows = {json.loads(x)["f"]["name"]: json.loads(x) for x in pages.read_text().splitlines()}
    html = W.search_page(rows["Lalitpur"]["f"], rows["Lalitpur"]["i"])[2]
    assert W.GEONAMES_CREDIT in html
    assert W.GEONAMES_CREDIT not in W.search_page(rows["Kathmandu"]["f"], 0)[2]


def _fake_builder(tmp_path):
    p = tmp_path / "fxb"
    p.write_text(f"#!{sys.executable}\nimport sys\na = sys.argv\n"
                 "open(a[a.index('--output') + 1], 'wb').write(b'x')\n")
    p.chmod(0o755)
    return str(p)


def test_feature_of_keeps_other_names_in_the_page_title():
    svr = _svr()
    from streetzim import zim_writer as W
    feat = {"name": "Haidian", "name_native": "海淀区", "type": "place", "subtype": "suburb",
            "lat": 39.96, "lon": 116.29, "location": ""}
    back = svr._feature_of(W.search_record(feat))
    assert W.kiwix_page_title(back) == W.kiwix_page_title(feat) == "Haidian · 海淀区"
    fr = {"name": "Eiffel Tower", "display_name": "Tour Eiffel", "display_latin": "Eiffel Tower",
          "type": "park", "subtype": "park", "lat": 48.85, "lon": 2.29, "location": ""}
    back = svr._feature_of(W.search_record(fr))
    assert W.shown_names(back) == ("Tour Eiffel", None, "Eiffel Tower")
