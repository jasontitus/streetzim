"""tools/golden_diff.py: which differences between two builds it accepts
(dates, Xapian, same-named search records swapping places, renumbered Kiwix
search pages, tile feature order, small coordinate moves, control-build
noise) and which it reports. The second half mutates real ZIMs written with
python-libzim, one regression per review finding."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import golden_diff as gd  # noqa: E402
from tools.golden_diff import Entry  # noqa: E402


def J(obj, title=""):
    return Entry("application/json", json.dumps(obj).encode(), None, title)


def H(text):
    return Entry("text/html", text.encode())


def rec(n, t="place", a=43.73235, o=7.42769, **extra):
    return {"n": n, "t": t, "s": "", "a": a, "o": o, "l": "", **extra}


# Two records share type and name: "Monaco" the country and the commune.
TIED = [rec("Les Moneghetti"), rec("Monaco", w="fr:Monaco"),
        rec("Monaco", w="fr:Commune de Monaco"), rec("Monte-Carlo")]
BASE = {
    "M/Date": Entry("text/plain", b"2026-09-28"),
    "M/Title": Entry("text/plain", b"Monaco"),
    "X/fulltext/xapian": Entry("application/octet-stream+xapian", b"x1"),
    "C/map-config.json": J({"name": "Monaco", "buildDate": "2026/09"}),
    "C/search-data/mo.json": J(TIED),
    "C/search/monaco-6.html": H("<p>A</p>"),
    "C/search/monaco-7.html": H("<p>B</p>"),
    "W/mainPage": Entry("", b"", "index.html"),
}


def test_identical():
    c = gd.classify(BASE, dict(BASE))
    assert len(c["identical"]) == len(BASE)
    assert not any(c[k] for k in gd.FAIL)


def test_expected_differences_are_accepted():
    new = dict(BASE)
    new["M/Date"] = Entry("text/plain", b"2026-10-01")
    new["X/fulltext/xapian"] = Entry("application/octet-stream+xapian", b"x2")
    new["C/map-config.json"] = J({"name": "Monaco", "buildDate": "2026/10"})
    new["C/search-data/mo.json"] = J([TIED[0], TIED[2], TIED[1], TIED[3]])
    new["C/search/monaco-6.html"], new["C/search/monaco-7.html"] = \
        BASE["C/search/monaco-7.html"], BASE["C/search/monaco-6.html"]
    c = gd.classify(BASE, new)
    assert c["volatile"] == ["C/map-config.json", "M/Date", "X/fulltext/xapian"]
    assert c["reordered"] == ["C/search-data/mo.json", "C/search/monaco-6.html",
                              "C/search/monaco-7.html"]
    assert not any(c[k] for k in gd.FAIL)
    assert gd.report(c, 5, out=lambda _s: None)


@pytest.mark.parametrize("path,entry", [
    ("M/Title", Entry("text/plain", b"Monte Carlo")),                  # metadata
    ("M/Date", Entry("text/plain", b"garbage")),                       # not a date
    ("C/map-config.json", J({"name": "Monaco", "buildDate": "2026/09", "hasRouting": True})),
    ("C/map-config.json", J({"name": "Monaco"})),                      # buildDate dropped
    ("C/search-data/mo.json", J(TIED[:3])),                            # a record lost
    ("C/search-data/mo.json", J(TIED[::-1])),                          # ranking reversed
    ("C/search-data/mo.json", J(TIED, title="other")),                 # entry title
    ("C/search/monaco-6.html", H("<p>C</p>")),                         # page content
    ("W/mainPage", Entry("", b"", "places.html")),                    # redirect target
    ("X/fulltext/xapian", Entry("application/octet-stream", b"x1")),  # MIME type
])
def test_real_changes_are_reported(path, entry):
    c = gd.classify(BASE, {**BASE, path: entry})
    assert c["changed"] == [path]
    assert not gd.report(c, 5, out=lambda _s: None)


@pytest.mark.parametrize("old,new", [
    ({"buildDate": "a", "routing": True}, {"buildDate": "b", "routing": 1}),
    ({"buildDate": "a", "zoom": 13}, {"buildDate": "b", "zoom": 13.0}),
    ({"buildDate": "a", "osmDate": "2026-09-01"}, {"buildDate": "b", "osmDate": "2026-08-01"}),
    ({"buildDate": "a", "x": 1}, {"x": 1}),
])
def test_build_date_masking_is_type_strict(old, new):
    c = gd.classify({"C/streetzim-meta.json": J(old)}, {"C/streetzim-meta.json": J(new)})
    assert c["changed"] == ["C/streetzim-meta.json"]


def test_same_name_records_may_swap_but_others_may_not():
    """Two builds of one commit swap records that share type and name but
    differ in label, subtype, coordinates or Wikidata (measured; see
    docs/golden-builds.md), so such swaps are accepted. Any other change of
    the (type, name) sequence is not."""
    p = "C/category-index/chip-food.json"
    a1 = rec("Café", t="poi", l="Monte-Carlo")
    a2 = rec("Café", t="poi", l="Fontvieille")
    b = rec("Bar", t="poi")
    assert gd.classify({p: J([b, a1, a2])}, {p: J([b, a2, a1])})["reordered"] == [p]
    assert gd.classify({p: J([b, a1, a2])}, {p: J([a1, b, a2])})["changed"] == [p]


def test_added_and_dropped_entries():
    new = dict(BASE)
    del new["C/search/monaco-7.html"]
    new["C/tiles/0/0/0.pbf"] = Entry("application/x-protobuf", b"")
    c = gd.classify(BASE, new)
    assert c["only-before"] == ["C/search/monaco-7.html"]
    assert c["only-after"] == ["C/tiles/0/0/0.pbf"]


def test_search_pages_multiset_includes_mime_and_redirects():
    old = {"C/search/a-1.html": Entry("", b"", "places.html"),
           "C/search/a-2.html": H("<p>A</p>")}
    retarget = {"C/search/a-1.html": Entry("", b"", "index.html"),
                "C/search/a-2.html": H("<p>A</p>")}
    remime = {"C/search/a-1.html": Entry("", b"", "places.html"),
              "C/search/a-2.html": Entry("text/plain", b"<p>A</p>")}
    assert gd.classify(old, retarget)["changed"] == ["C/search/a-1.html"]
    assert gd.classify(old, remime)["changed"] == ["C/search/a-2.html"]


def test_control_noise_needs_after_to_match_the_control():
    control = dict(BASE)
    control["C/search-data/mo.json"] = J([rec("Monaco", a=1.0)])
    like_control = dict(control)
    like_neither = dict(BASE)
    like_neither["C/search-data/mo.json"] = J([rec("Monaco", a=2.0)])
    like_neither["M/Title"] = Entry("text/plain", b"Monte Carlo")
    c = gd.classify(BASE, like_control, control=control)
    assert c["noise"] == ["C/search-data/mo.json"] and not c["changed"]
    c = gd.classify(BASE, like_neither, control=control)
    assert c["changed"] == ["C/search-data/mo.json", "M/Title"] and not c["noise"]


def test_decode_tiles_ignores_feature_order():
    mvt = pytest.importorskip("mapbox_vector_tile")
    f1 = {"geometry": "POINT(1 1)", "properties": {"name": "a"}}
    f2 = {"geometry": "POINT(2 2)", "properties": {"name": "b"}}
    t1 = mvt.encode([{"name": "poi", "features": [f1, f2]}])
    t2 = mvt.encode([{"name": "poi", "features": [f2, f1]}])
    t3 = mvt.encode([{"name": "poi", "features": [f1]}])
    p = "C/tiles/1/0/0.pbf"
    old = {p: Entry("application/x-protobuf", t1)}
    same = {p: Entry("application/x-protobuf", t2)}
    fewer = {p: Entry("application/x-protobuf", t3)}
    assert t1 != t2
    assert gd.classify(old, same, decode_tiles=True)["tiles-equal"] == [p]
    assert gd.classify(old, same)["changed"] == [p]
    assert gd.classify(old, fewer, decode_tiles=True)["changed"] == [p]


def test_coord_tolerance_accepts_small_moves_only():
    street = {"n": "Impasse", "t": "street", "s": "path"}
    p = "C/search-data/im.json"
    old = {p: J([{**street, "a": 43.74248, "o": 7.42267}])}
    near = {p: J([{**street, "a": 43.74253, "o": 7.42271}])}
    far = {p: J([{**street, "a": 43.75, "o": 7.42271}])}
    renamed = {p: J([{**street, "n": "Impasse X", "a": 43.74253, "o": 7.42271}])}
    log = gd.Log()
    assert gd.classify(old, near)["changed"] == [p]
    assert gd.classify(old, near, coord_tol=1e-4, log=log)["moved"] == [p]
    assert log.shifts == [pytest.approx(5e-5)]
    assert gd.classify(old, far, coord_tol=1e-4)["changed"] == [p]
    assert gd.classify(old, renamed, coord_tol=1e-4)["changed"] == [p]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "43.74", None, True])
def test_non_numeric_coordinates_are_changed(bad):
    street = {"n": "Impasse", "t": "street", "s": "path", "o": 7.42267}
    p = "C/search-data/im.json"
    old = {p: J([{**street, "a": 43.74248}])}
    new = {p: Entry("application/json",
                    json.dumps([{**street, "a": bad}]).encode())}   # NaN/Infinity allowed
    log = gd.Log()
    assert gd.classify(old, new, coord_tol=1e-4, log=log)["changed"] == [p]
    assert not log.shifts
    if not isinstance(bad, float) or bad == bad:                     # not NaN: noted
        assert log.notes


# --- Regressions on real ZIMs (python-libzim), one per review finding ------

RECORDS = [rec(f"Place {i:02d}", a=43.7 + i / 1000, o=7.4 + i / 1000) for i in range(20)]
RECORDS += [rec("Tied", w="one"), rec("Tied", w="two")]


def _zim(tmp_path, name, *, records=RECORDS, index_title="Monaco", date="2026-09-28",
         meta_extra=None, extra_items=(), redirects=(), empty=False):
    pytest.importorskip("libzim")
    from libzim.writer import Creator, Hint, Item, StringProvider

    class _Item(Item):
        def __init__(self, path, title, mime, data):
            super().__init__()
            self._a = (path, title, mime, data)

        def get_path(self): return self._a[0]
        def get_title(self): return self._a[1]
        def get_mimetype(self): return self._a[2]
        def get_contentprovider(self): return StringProvider(self._a[3])
        def get_hints(self): return {Hint.FRONT_ARTICLE: False, Hint.COMPRESS: True}

    path = tmp_path / name
    meta = {"name": "Monaco", "buildDate": date[:7].replace("-", "/"), **(meta_extra or {})}
    with Creator(str(path)).config_indexing(True, "en") as c:
        c.add_metadata("Title", "t")
        c.add_metadata("Date", date)
        if not empty:
            c.add_item(_Item("index.html", index_title, "text/html", "<p>map</p>"))
            c.add_item(_Item("places.html", "Places", "text/html", "<p>places</p>"))
            c.add_item(_Item("map-config.json", "", "application/json", json.dumps(meta)))
            c.add_item(_Item("search-data/10.json", "Search chunk 10", "application/json",
                             json.dumps(records, separators=(",", ":"))))
            for item in extra_items:
                c.add_item(_Item(*item))
            for src, title, target in redirects:
                c.add_redirection(src, title, target, {Hint.FRONT_ARTICLE: False})
    return str(path)


def _run(capsys, *argv):
    rc = gd.main([*argv, "--show", "50"])
    return rc, capsys.readouterr()


def _in(section, out):
    return out.split(f"{section}:")[1].split("\n\n")[0] if f"{section}:" in out else ""


def test_zim_same_build_passes(tmp_path, capsys):
    a = _zim(tmp_path, "a.zim")
    b = _zim(tmp_path, "b.zim", date="2026-10-02", records=RECORDS[:20] + RECORDS[20:][::-1])
    rc, cap = _run(capsys, a, b)
    assert rc == 0, cap.out
    assert "C/search-data/10.json" in _in("reordered", cap.out)


def test_zim_reversed_search_chunk_fails(tmp_path, capsys):
    """A chunk with its ranking reversed is not a harmless reorder."""
    a = _zim(tmp_path, "a.zim")
    b = _zim(tmp_path, "b.zim", records=RECORDS[::-1])
    rc, cap = _run(capsys, a, b)
    assert rc == 1 and "C/search-data/10.json" in _in("changed", cap.out)


def test_zim_changed_entry_title_fails(tmp_path, capsys):
    a = _zim(tmp_path, "a.zim")
    b = _zim(tmp_path, "b.zim", index_title="Monte Carlo")
    rc, cap = _run(capsys, a, b)
    assert rc == 1 and "C/index.html" in _in("changed", cap.out)


def test_zim_coordinate_shift_above_tolerance_fails(tmp_path, capsys):
    a = _zim(tmp_path, "a.zim")
    shifted = [{**r, "a": round(r["a"] + 0.0009, 5)} for r in RECORDS]
    jitter = [{**r, "a": round(r["a"] + 0.00005, 5)} for r in RECORDS]
    b = _zim(tmp_path, "b.zim", records=shifted)
    j = _zim(tmp_path, "j.zim", records=jitter)
    rc, cap = _run(capsys, a, b, "--coord-tolerance", "1e-4")
    assert rc == 1 and "C/search-data/10.json" in _in("changed", cap.out)
    rc, cap = _run(capsys, a, j, "--coord-tolerance", "1e-4")
    assert rc == 0 and "largest coordinate shift accepted: 0.000050" in cap.out


def test_zim_noise_must_match_the_control(tmp_path, capsys):
    a = _zim(tmp_path, "a.zim")
    control = _zim(tmp_path, "c.zim", records=RECORDS[1:])
    same_as_control = _zim(tmp_path, "s.zim", records=RECORDS[1:])
    broken = _zim(tmp_path, "b.zim", records=RECORDS[5:])
    rc, cap = _run(capsys, a, same_as_control, "--control", control)
    assert rc == 0 and "C/search-data/10.json" in _in("noise", cap.out)
    rc, cap = _run(capsys, a, broken, "--control", control)
    assert rc == 1 and "C/search-data/10.json" in _in("changed", cap.out)


def test_zim_noise_cannot_launder_twice_the_tolerance(tmp_path, capsys):
    """Control +0.00009 and after +0.00018: each within 1e-4 of its
    neighbour, but after is 0.00018 from before."""
    a = _zim(tmp_path, "a.zim")
    c = _zim(tmp_path, "c.zim", records=[{**r, "a": round(r["a"] + 0.00009, 5)} for r in RECORDS])
    b = _zim(tmp_path, "b.zim", records=[{**r, "a": round(r["a"] + 0.00018, 5)} for r in RECORDS])
    rc, cap = _run(capsys, a, b, "--control", c, "--coord-tolerance", "1e-4")
    assert rc == 1 and "C/search-data/10.json" in _in("changed", cap.out)


def test_zim_search_page_redirect_retarget_fails(tmp_path, capsys):
    a = _zim(tmp_path, "a.zim", redirects=[("search/x-1.html", "x", "places.html")])
    b = _zim(tmp_path, "b.zim", redirects=[("search/x-1.html", "x", "index.html")])
    rc, cap = _run(capsys, a, b)
    assert rc == 1 and "C/search/x-1.html" in _in("changed", cap.out)


def test_zim_nan_and_string_coordinates_fail(tmp_path, capsys):
    a = _zim(tmp_path, "a.zim")
    nan = _zim(tmp_path, "n.zim", records=[{**RECORDS[0], "a": float("nan")}, *RECORDS[1:]])
    text = _zim(tmp_path, "t.zim", records=[{**RECORDS[0], "a": "43.7"}, *RECORDS[1:]])
    for bad in (nan, text):
        rc, cap = _run(capsys, a, bad, "--coord-tolerance", "1e-4")
        assert rc == 1 and "C/search-data/10.json" in _in("changed", cap.out)
    assert "note: C/search-data/10.json: coordinate not a finite number" in cap.out


def test_zim_build_date_masking_is_type_strict(tmp_path, capsys):
    a = _zim(tmp_path, "a.zim", meta_extra={"hasRouting": True, "zoom": 13})
    ok = _zim(tmp_path, "ok.zim", date="2026-10-02", meta_extra={"hasRouting": True, "zoom": 13})
    one = _zim(tmp_path, "one.zim", meta_extra={"hasRouting": 1, "zoom": 13})
    flt = _zim(tmp_path, "flt.zim", meta_extra={"hasRouting": True, "zoom": 13.0})
    assert _run(capsys, a, ok)[0] == 0
    for bad in (one, flt):
        rc, cap = _run(capsys, a, bad)
        assert rc == 1 and "C/map-config.json" in _in("changed", cap.out)


def test_zim_unreadable_or_empty_archive_exits_2(tmp_path, capsys):
    a = _zim(tmp_path, "a.zim")
    empty = _zim(tmp_path, "e.zim", empty=True)
    junk = tmp_path / "junk.zim"
    junk.write_bytes(b"not a zim")
    for other in (str(tmp_path / "missing.zim"), str(junk), empty):
        rc, cap = _run(capsys, a, other)
        assert rc == 2 and "golden_diff:" in cap.err


def test_zim_namespaces_are_kept_apart(tmp_path, capsys):
    """A content entry named like a metadata key is its own entry."""
    a = _zim(tmp_path, "a.zim")
    b = _zim(tmp_path, "b.zim", extra_items=[("Title", "t", "text/plain", "t")])
    rc, cap = _run(capsys, a, b)
    assert rc == 1 and "C/Title" in _in("only-after", cap.out)
    assert "M/Title" not in _in("only-after", cap.out)


def test_golden_builds_usage():
    """The wrapper refuses a short command line before doing any work."""
    if shutil.which("bash") is None:
        pytest.skip("no bash")
    for args in ([], ["main", "WORKTREE"], ["main", "WORKTREE", "/nonexistent", "--wikidata"]):
        r = subprocess.run(["bash", str(ROOT / "tools" / "golden_builds.sh"), *args],
                           capture_output=True, text=True)
        assert r.returncode == 2, args
        assert "BEFORE AFTER WORKDIR" in r.stderr


@pytest.mark.parametrize("new,volatile", [
    ("2026/10", True),                       # same shape: YYYY/MM
    ("2026-10", False),                      # different shape
    ("garbage", False),
    (202610, False),
    (None, False),
])
def test_build_date_must_keep_its_date_shape(new, volatile):
    p = "C/map-config.json"
    c = gd.classify({p: J({"buildDate": "2026/09", "x": 1})}, {p: J({"buildDate": new, "x": 1})})
    assert c["volatile" if volatile else "changed"] == [p]


def test_zim_entries_do_not_keep_content_in_memory(tmp_path):
    a = _zim(tmp_path, "a.zim")
    entries, _ = gd.read_zim(a)
    items = [e for e in entries.values() if e.redirect is None]
    assert items and all(e._data is None for e in items)             # only digests kept
    assert json.loads(entries["C/search-data/10.json"].data) == RECORDS  # re-read on demand


class _FakeEntry:
    def __init__(self, index, path):
        self._index, self.path, self.title, self.is_redirect = index, path, "", False

    def get_item(self):
        class _It:
            mimetype, content = "text/plain", b"x"
        return _It()


class _FakeArchive:
    """Two non-content entries on the same path, which no real writer makes."""
    metadata_keys = ("Title",)
    all_entry_count = entry_count = 3
    uuid = "u"

    def __init__(self, _path):
        self._e = [_FakeEntry(0, "index.html"), _FakeEntry(1, "listing/x"),
                   _FakeEntry(2, "listing/x")]

    def _get_entry_by_id(self, i): return self._e[i]
    def has_entry_by_path(self, p): return p == "index.html"
    def get_entry_by_path(self, p): return self._e[0]


def test_colliding_paths_exit_2(monkeypatch, capsys, tmp_path):
    reader = pytest.importorskip("libzim.reader")
    good = _zim(tmp_path, "a.zim")
    monkeypatch.setattr(reader, "Archive", _FakeArchive)
    with pytest.raises(gd.ZimError, match="two entries on X/listing/x"):
        gd.read_zim(good)
    rc, cap = _run(capsys, good, good)
    assert rc == 2 and "two entries" in cap.err
