"""Kiwix's own search in a --xapian=builder ZIM (what build-region-fast.sh
builds wherever xapianbuilder is installed): the build writes the same Kiwix
pages as a --xapian=libzim build (search/<slug>-<i>.html for the records of a
page type, admin areas' ~<k> redirects), and xapianbuilder's documents are
those pages, so every suggestion and full-text hit opens. From f38cfb4
(2026-05-08) to 2026-10-02 the documents were named s/<n> and nothing was
written there: every Kiwix result of a builder ZIM was a dead link.

The first tests use a stand-in xapianbuilder that keeps its input (no
Xapian needed: the docker suite); the last uses the real one when it is
built (XAPIANBUILDER_BIN or ../xapianbuilder) and searches like Kiwix."""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streetzim import zim_writer as W  # noqa: E402

FEATURES = [
    {"name": "Monte-Carlo", "type": "place", "subtype": "suburb"},
    {"name": "Fontaine du Casino", "type": "water", "subtype": "lake"},
    {"name": "Casino", "type": "poi", "subtype": "grocery"},
    {"name": "Place du Casino", "type": "street", "subtype": "path"},
    {"name": "Monaco", "type": "admin", "subtype": "country", "admin_level": 2,
     "osm": "r1124039", "alt": ["Principauté de Monaco", "Monacu"],
     "bbox": [7.40, 43.72, 7.44, 43.76], "wikipedia": "en:Monaco"},
    {"name": "Moneghetti", "type": "admin", "subtype": "town", "admin_level": 10,
     "osm": "r7", "location": "Monaco", "alt": ["Muneghetti"]},
    {"name": "Jardin Exotique", "type": "park", "subtype": "garden"},
    {"name": "Tête de Chien", "type": "peak", "subtype": "peak"},
]
WIKI_BODY = b"<html><body><p>The principality of zanzibarite cliffs.</p></body></html>"


def _features():
    # Full-precision coordinates, as a build has them.
    return [dict(f, lat=43.7312345678 + i * 1.1e-4, lon=7.4198765432 + i * 3e-5)
            for i, f in enumerate(FEATURES)]


@pytest.fixture
def keeping_xb(tmp_path):
    """A stand-in xapianbuilder: its database is a copy of its input, and the
    input is kept beside it (OUTPUT.in) for the test to read."""
    p = tmp_path / "keeping-xapianbuilder"
    p.write_text(f"#!{sys.executable}\n"
                 "import shutil, sys\n"
                 "a = sys.argv\n"
                 "src, out = a[a.index('--input') + 1], a[a.index('--output') + 1]\n"
                 "shutil.copy(src, out + '.in')\n"
                 "shutil.copy(src, out)\n")
    p.chmod(0o755)
    return str(p)


def _bundler(titles, add_item, **kw):
    from cloud.wiki_articles import _underscore
    stored = set()
    for t in titles:
        add_item(f"wiki-article/{_underscore(t)}", t.split(":", 1)[-1], "text/html", WIKI_BODY)
        add_item(f"wiki-image/{_underscore(t)}.webp", "", "image/webp", b"RIFF")
        stored.add(_underscore(t))
    return {"bundled": len(stored), "bytes": 0, "failed": 0, "stored_titles": stored}


def _build(tmp_path, monkeypatch, name, *, xapian_mode, zim_builder, source="path",
           xb=None, poi_pages=False):
    pytest.importorskip("libzim.writer")
    mvt = pytest.importorskip("mapbox_vector_tile")
    from cloud import wiki_articles as wa
    monkeypatch.setattr(wa, "bundle_wiki_articles", _bundler)
    if zim_builder == "manifest":
        # STREETZIM_PACK_BIN makes the manifest path use the Rust packer.
        monkeypatch.delenv("STREETZIM_PACK_BIN", raising=False)
    d = tmp_path / name
    d.mkdir()
    tile = gzip.compress(mvt.encode([{"name": "poi", "features": [
        {"geometry": "POINT(10 10)", "properties": {"name": "X"}}]}]))
    (d / "ml.js").write_text("//")
    (d / "ml.css").write_text("/**/")
    feats = _features()
    kw = {}
    if source == "path":
        path = d / "features.jsonl"
        path.write_text("".join(json.dumps(f) + "\n" for f in feats))
        kw["search_features_path"] = str(path)
    else:
        kw["search_features"] = feats
    work = d / "work"
    work.mkdir()
    W.create_zim(
        d / "t.zim", tiles={(14, 8529, 5974): tile}, tile_metadata={},
        fonts={("OpenSansRegular", "0-255"): b"g"},
        maplibre_js_path=str(d / "ml.js"), maplibre_css_path=str(d / "ml.css"),
        viewer_html_path=str(ROOT / "resources/viewer/index.html"),
        map_config={"name": "Monaco"}, name="OSM - Monaco",
        bbox=(7.40, 43.72, 7.44, 43.76), xapian_mode=xapian_mode,
        zim_builder=zim_builder, xapianbuilder_bin=xb, xapian_workdir=str(work),
        kiwix_poi_pages=poi_pages, wiki_cross_refs={("x", 1, 2): {"wikipedia": "en:Monaco"}},
        bundle_wiki_articles=True, zim_workers=1, **kw)
    return d / "t.zim", work


def _search_entries(a):
    """path -> (title, redirect target or None, content or None, front) of every
    search/ entry."""
    out = {}
    for i in range(a.entry_count):
        e = a._get_entry_by_id(i)
        if not e.path.startswith("search/"):
            continue
        if e.is_redirect:
            out[e.path] = (e.title, e.get_redirect_entry().path, None)
        else:
            out[e.path] = (e.title, None, bytes(e.get_item().content))
    return out


def _corpus(work, mode):
    return [json.loads(x) for x in
            (work / f"X-{mode}-xapian.glass.in").read_text(encoding="utf-8").splitlines()]


def _packers():
    out = ["manifest"]
    try:
        from cloud.manifest_writer import resolve_pack_binary
        resolve_pack_binary()
        out.append("rust")
    except Exception:
        out.append(pytest.param("rust", marks=pytest.mark.skip(
            reason="no built Rust streetzim-pack (STREETZIM_PACK_BIN)")))
    return out


@pytest.mark.parametrize("source", ["path", "in-memory"])
@pytest.mark.parametrize("zim_builder", _packers())
def test_builder_corpus_points_at_the_pages_libzim_mode_writes(
        tmp_path, monkeypatch, keeping_xb, source, zim_builder):
    from libzim.reader import Archive
    ref, _ = _build(tmp_path, monkeypatch, "libzim", xapian_mode="libzim",
                    zim_builder="python", source=source)
    zim, work = _build(tmp_path, monkeypatch, "builder", xapian_mode="builder",
                       zim_builder=zim_builder, source=source, xb=keeping_xb)
    r, a = Archive(str(ref)), Archive(str(zim))
    pages = _search_entries(a)
    # The same pages and redirects as a --xapian=libzim build, byte for byte
    # (numbering in feature order, full-precision coordinates).
    assert pages == _search_entries(r)
    # Numbered from 0 in feature order over the records of a page type.
    paged = [f for f in _features() if f["type"] in W.KIWIX_PAGE_TYPES]
    assert {p for p, (_, tgt, _) in pages.items() if tgt is None} == {
        W.search_page(f, i)[0] for i, f in enumerate(paged)}
    assert "search/monte-carlo-0.html" in pages
    names = {t for t, tgt, _ in pages.values() if tgt is None}
    assert names == {"Monte-Carlo", "Fontaine du Casino", "Monaco (country)",
                     "Moneghetti (town)", "Jardin Exotique", "Tête de Chien"}
    redirects = {p: (t, tgt) for p, (t, tgt, _) in pages.items() if tgt}
    assert {t for t, _ in redirects.values()} == {
        "Principauté de Monaco", "Monacu", "Muneghetti", "Town of Moneghetti"}
    # The main page stays index.html (17c82fc: front pages must not replace
    # it). Front articles (the A-Z listing): the main page, the pages and
    # their redirects, as in the libzim build. The Rust packer ignores the
    # FRONT_ARTICLE hint (zimru lists every HTML item and redirect itself),
    # so its listing also has places.html and the Wikipedia article: Kiwix's
    # suggestions come from the title index, which is the same.
    assert a.main_entry.get_item().path == "index.html"
    assert r.article_count == 1 + len(pages)
    assert a.article_count == r.article_count + (2 if zim_builder == "rust" else 0)

    ft, ti = _corpus(work, "fulltext"), _corpus(work, "title")
    page_paths = {p for p, (_, tgt, _) in pages.items() if tgt is None}
    # Title index: every page at its path, every redirect title at its
    # redirect (target_path: the page), nothing else.
    assert {d["path"] for d in ti if not d["target_path"]} == page_paths
    assert {d["path"]: (d["title"], d["target_path"]) for d in ti if d["target_path"]} \
        == redirects
    # Full text: the pages and the bundled Wikipedia article (libzim indexes
    # it; not a front article, so not in the title index); no redirect.
    assert {d["path"] for d in ft} == page_paths | {"wiki-article/Monaco"}
    assert not any(d["target_path"] for d in ft)
    art = next(d for d in ft if d["path"] == "wiki-article/Monaco")
    assert art["title"] == "Monaco" and "zanzibarite" in art["body"]
    for d in ft + ti:
        assert a.has_entry_by_path(d["path"]), d["path"]
        assert not d["path"].startswith("s/")
        e = a.get_entry_by_path(d["path"])
        assert e.title == d["title"]
        if d["target_path"]:
            assert e.is_redirect and e.get_redirect_entry().path == d["target_path"]
        else:
            assert not e.is_redirect
    # A page's documents carry its record (name, alt names, geo position).
    mon = next(d for d in ft if d["title"] == "Monaco (country)")
    assert "Monacu" in mon["body"] and 'content="43.7316' in mon["body"]
    # The two databases are in the archive (namespace X), as xapianbuilder
    # wrote them.
    assert zim.stat().st_size > 0
    raw = zim.read_bytes()
    assert (work / "X-fulltext-xapian.glass").read_bytes() in raw
    assert (work / "X-title-xapian.glass").read_bytes() in raw
    # The corpus files are scratch, gone once the build is done.
    assert not list(work.rglob("_xapian-*.jsonl"))


def test_builder_poi_pages(tmp_path, monkeypatch, keeping_xb):
    """--kiwix-poi-pages: the POIs get pages and documents too."""
    from libzim.reader import Archive
    zim, work = _build(tmp_path, monkeypatch, "b", xapian_mode="builder",
                       zim_builder="manifest", xb=keeping_xb, poi_pages=True)
    a = Archive(str(zim))
    titles = {t for t, tgt, _ in _search_entries(a).values() if tgt is None}
    assert "Casino" in titles and "Place du Casino" not in titles
    ft = _corpus(work, "fulltext")
    assert "Casino" in {d["title"] for d in ft}
    assert all(a.has_entry_by_path(d["path"]) for d in ft)


def _xapianbuilder():
    try:
        return W._resolve_xapianbuilder_binary(None)
    except FileNotFoundError:
        pytest.skip("xapianbuilder not built (XAPIANBUILDER_BIN)")


@pytest.mark.parametrize("zim_builder", _packers())
def test_kiwix_search_of_a_builder_zim_opens_every_result(tmp_path, monkeypatch, zim_builder):
    """With the real xapianbuilder: every title suggestion and full-text hit
    python-libzim (Kiwix's library) returns is an entry of the archive, and
    the pages are found by name, by other name and by the formal title."""
    from libzim.reader import Archive
    from libzim.search import Query, Searcher
    from libzim.suggestion import SuggestionSearcher
    xb = _xapianbuilder()
    zim, _ = _build(tmp_path, monkeypatch, "b", xapian_mode="builder",
                    zim_builder=zim_builder, xb=xb)
    a = Archive(str(zim))
    assert a.has_title_index and a.has_fulltext_index
    assert a.main_entry.get_item().path == "index.html"

    def hits(text):
        s = SuggestionSearcher(a).suggest(text)
        f = Searcher(a).search(Query().set_query(text))
        sugg, full = list(s.getResults(0, 50)), list(f.getResults(0, 50))
        for p in sugg + full:
            assert a.has_entry_by_path(p), (text, p)
        title = lambda p: a.get_entry_by_path(p).get_item().title  # noqa: E731
        return {title(p) for p in sugg}, {title(p) for p in full}

    assert "Fontaine du Casino" in hits("Fontaine")[0]
    assert "Fontaine du Casino" in hits("casino")[1]
    assert "Monaco (country)" in hits("Monaco")[0]
    # Other names: the redirect title leads to the page.
    assert "Monaco (country)" in hits("Monacu")[0]
    assert "Monaco (country)" in hits("Principauté")[0]
    assert "Moneghetti (town)" in hits("Town of Moneghetti")[0]
    assert "Moneghetti (town)" in hits("Town of Moneghetti")[1]
    assert "Moneghetti (town)" in hits("Muneghetti")[1]
    # The Wikipedia article: full text only.
    sugg, full = hits("zanzibarite")
    assert full == {"Monaco"} and sugg == set()
    # Streets and (without --kiwix-poi-pages) POIs have no page.
    assert hits("Place du")[0] == set()
    assert "Casino" not in hits("casino")[1]
