"""tools/golden_diff.py: which differences between two builds it accepts
(dates, Xapian, tied search records swapping places, renumbered Kiwix
search pages, tile feature order, small coordinate moves, control-build
noise) and which it reports. The last group of tests mutates real ZIMs
written with python-libzim, one regression per review finding."""
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


# Two records tie on the builder's sort key (type, name): "Monaco" the
# country and "Monaco" the commune.
TIED = [rec("Les Moneghetti"), rec("Monaco", w="fr:Monaco"),
        rec("Monaco", w="fr:Commune de Monaco"), rec("Monte-Carlo")]
BASE = {
    "Date": Entry("text/plain", b"2026-09-28"),
    "Title": Entry("text/plain", b"Monaco"),
    "fulltext/xapian": Entry("application/octet-stream+xapian", b"x1"),
    "map-config.json": J({"name": "Monaco", "buildDate": "2026/09"}),
    "search-data/mo.json": J(TIED),
    "search/monaco-6.html": H("<p>A</p>"),
    "search/monaco-7.html": H("<p>B</p>"),
    "mainPage": Entry("", b"", "index.html"),
}
META = {"Date", "Title"}


def test_identical():
    c = gd.classify(BASE, dict(BASE), META)
    assert len(c["identical"]) == len(BASE)
    assert not any(c[k] for k in gd.FAIL)


def test_expected_differences_are_accepted():
    new = dict(BASE)
    new["Date"] = Entry("text/plain", b"2026-10-01")
    new["fulltext/xapian"] = Entry("application/octet-stream+xapian", b"x2")
    new["map-config.json"] = J({"name": "Monaco", "buildDate": "2026/10"})
    new["search-data/mo.json"] = J([TIED[0], TIED[2], TIED[1], TIED[3]])
    new["search/monaco-6.html"], new["search/monaco-7.html"] = BASE["search/monaco-7.html"], \
        BASE["search/monaco-6.html"]
    c = gd.classify(BASE, new, META)
    assert c["volatile"] == ["Date", "fulltext/xapian", "map-config.json"]
    assert c["reordered"] == ["search-data/mo.json", "search/monaco-6.html",
                              "search/monaco-7.html"]
    assert not any(c[k] for k in gd.FAIL)
    assert gd.report(c, 5, out=lambda _s: None)


@pytest.mark.parametrize("path,entry", [
    ("Title", Entry("text/plain", b"Monte Carlo")),                    # metadata
    ("map-config.json", J({"name": "Monaco", "buildDate": "2026/09", "hasRouting": True})),
    ("search-data/mo.json", J(TIED[:3])),                              # a record lost
    ("search-data/mo.json", J(TIED[::-1])),                            # ranking reversed
    ("search-data/mo.json", J(TIED, title="other")),                   # entry title
    ("search/monaco-6.html", H("<p>C</p>")),                           # page content
    ("mainPage", Entry("", b"", "places.html")),                      # redirect target
    ("fulltext/xapian", Entry("application/octet-stream", b"x1")),    # MIME type
])
def test_real_changes_are_reported(path, entry):
    new = dict(BASE)
    new[path] = entry
    c = gd.classify(BASE, new, META)
    assert c["changed"] == [path]
    assert not gd.report(c, 5, out=lambda _s: None)


def test_only_the_build_date_key_is_volatile():
    old = {"streetzim-meta.json": J({"buildDate": "2026-09-28", "osmDate": "2026-09-01"})}
    date = {"streetzim-meta.json": J({"buildDate": "2026-09-29", "osmDate": "2026-09-01"})}
    other = {"streetzim-meta.json": J({"buildDate": "2026-09-28", "osmDate": "2026-08-01"})}
    assert gd.classify(old, date, set())["volatile"] == ["streetzim-meta.json"]
    assert gd.classify(old, other, set())["changed"] == ["streetzim-meta.json"]


def test_added_and_dropped_entries():
    new = dict(BASE)
    del new["search/monaco-7.html"]
    new["tiles/0/0/0.pbf"] = Entry("application/x-protobuf", b"")
    c = gd.classify(BASE, new, META)
    assert c["only-before"] == ["search/monaco-7.html"]
    assert c["only-after"] == ["tiles/0/0/0.pbf"]


def test_control_noise_needs_after_to_match_the_control():
    control = dict(BASE)
    control["search-data/mo.json"] = J([rec("Monaco", a=1.0)])
    like_control = dict(BASE)
    like_control["search-data/mo.json"] = J([rec("Monaco", a=1.0)])
    like_neither = dict(BASE)
    like_neither["search-data/mo.json"] = J([rec("Monaco", a=2.0)])
    like_neither["Title"] = Entry("text/plain", b"Monte Carlo")
    c = gd.classify(BASE, like_control, META, control=control)
    assert c["noise"] == ["search-data/mo.json"] and not c["changed"]
    c = gd.classify(BASE, like_neither, META, control=control)
    assert c["changed"] == ["Title", "search-data/mo.json"] and not c["noise"]


def test_decode_tiles_ignores_feature_order():
    mvt = pytest.importorskip("mapbox_vector_tile")
    f1 = {"geometry": "POINT(1 1)", "properties": {"name": "a"}}
    f2 = {"geometry": "POINT(2 2)", "properties": {"name": "b"}}
    t1 = mvt.encode([{"name": "poi", "features": [f1, f2]}])
    t2 = mvt.encode([{"name": "poi", "features": [f2, f1]}])
    t3 = mvt.encode([{"name": "poi", "features": [f1]}])
    old = {"tiles/1/0/0.pbf": Entry("application/x-protobuf", t1)}
    same = {"tiles/1/0/0.pbf": Entry("application/x-protobuf", t2)}
    fewer = {"tiles/1/0/0.pbf": Entry("application/x-protobuf", t3)}
    assert t1 != t2
    assert gd.classify(old, same, set(), decode_tiles=True)["tiles-equal"] == ["tiles/1/0/0.pbf"]
    assert gd.classify(old, same, set())["changed"] == ["tiles/1/0/0.pbf"]
    assert gd.classify(old, fewer, set(), decode_tiles=True)["changed"] == ["tiles/1/0/0.pbf"]


def test_coord_tolerance_accepts_small_moves_only():
    street = {"n": "Impasse", "t": "street", "s": "path"}
    old = {"search-data/im.json": J([{**street, "a": 43.74248, "o": 7.42267}])}
    near = {"search-data/im.json": J([{**street, "a": 43.74253, "o": 7.42271}])}
    far = {"search-data/im.json": J([{**street, "a": 43.75, "o": 7.42271}])}
    renamed = {"search-data/im.json": J([{**street, "n": "Impasse X", "a": 43.74253,
                                          "o": 7.42271}])}
    shifts: list[float] = []
    assert gd.classify(old, near, set())["changed"] == ["search-data/im.json"]
    assert gd.classify(old, near, set(), coord_tol=1e-4, shifts=shifts)["moved"] \
        == ["search-data/im.json"]
    assert shifts == [pytest.approx(5e-5)]
    assert gd.classify(old, far, set(), coord_tol=1e-4)["changed"] == ["search-data/im.json"]
    assert gd.classify(old, renamed, set(), coord_tol=1e-4)["changed"] == ["search-data/im.json"]


# --- Regressions on real ZIMs (python-libzim), one per review finding ------

RECORDS = [rec(f"Place {i:02d}", a=43.7 + i / 1000, o=7.4 + i / 1000) for i in range(20)]
RECORDS += [rec("Tied", w="one"), rec("Tied", w="two")]


def _zim(tmp_path, name, *, records=RECORDS, index_title="Monaco", date="2026-09-28",
         meta_extra=None):
    libzim = pytest.importorskip("libzim")
    from libzim.writer import Creator, Hint, Item, StringProvider
    del libzim

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
        c.add_item(_Item("index.html", index_title, "text/html", "<p>map</p>"))
        c.add_item(_Item("map-config.json", "", "application/json", json.dumps(meta)))
        c.add_item(_Item("search-data/10.json", "Search chunk 10", "application/json",
                         json.dumps(records, separators=(",", ":"))))
    return str(path)


def _run(capsys, *argv):
    rc = gd.main([*argv, "--show", "50"])
    return rc, capsys.readouterr().out


def test_zim_same_build_passes(tmp_path, capsys):
    a = _zim(tmp_path, "a.zim")
    b = _zim(tmp_path, "b.zim", date="2026-10-02", records=RECORDS[:20] + RECORDS[20:][::-1])
    rc, out = _run(capsys, a, b)
    assert rc == 0, out
    assert "reordered          1" in out.replace(",", "")


def test_zim_reversed_search_chunk_fails(tmp_path, capsys):
    """HIGH: a chunk with its ranking reversed is not a harmless reorder."""
    a = _zim(tmp_path, "a.zim")
    b = _zim(tmp_path, "b.zim", records=RECORDS[::-1])
    rc, out = _run(capsys, a, b)
    assert rc == 1 and "search-data/10.json" in out.split("changed:")[1]


def test_zim_changed_entry_title_fails(tmp_path, capsys):
    """MEDIUM a: an entry's title is part of the output."""
    a = _zim(tmp_path, "a.zim")
    b = _zim(tmp_path, "b.zim", index_title="Monte Carlo")
    rc, out = _run(capsys, a, b)
    assert rc == 1 and "index.html" in out.split("changed:")[1]


def test_zim_coordinate_shift_above_tolerance_fails(tmp_path, capsys):
    """MEDIUM b: +0.0009 degrees on every record is not tilemaker jitter."""
    a = _zim(tmp_path, "a.zim")
    shifted = [{**r, "a": round(r["a"] + 0.0009, 5)} for r in RECORDS]
    jitter = [{**r, "a": round(r["a"] + 0.00005, 5)} for r in RECORDS]
    b = _zim(tmp_path, "b.zim", records=shifted)
    j = _zim(tmp_path, "j.zim", records=jitter)
    rc, out = _run(capsys, a, b, "--coord-tolerance", "1e-4")
    assert rc == 1 and "search-data/10.json" in out.split("changed:")[1]
    rc, out = _run(capsys, a, j, "--coord-tolerance", "1e-4")
    assert rc == 0 and "largest coordinate shift accepted: 0.000050" in out


def test_zim_noise_must_match_the_control(tmp_path, capsys):
    """MEDIUM c: the control varying does not excuse an unrelated change."""
    a = _zim(tmp_path, "a.zim")
    control = _zim(tmp_path, "c.zim", records=RECORDS[1:])
    same_as_control = _zim(tmp_path, "s.zim", records=RECORDS[1:])
    broken = _zim(tmp_path, "b.zim", records=RECORDS[5:])
    rc, out = _run(capsys, a, same_as_control, "--control", control)
    assert rc == 0 and "search-data/10.json" in out.split("noise:")[1]
    rc, out = _run(capsys, a, broken, "--control", control)
    assert rc == 1 and "search-data/10.json" in out.split("changed:")[1]


def test_zim_only_build_date_is_masked(tmp_path, capsys):
    """LOW: another date-like value in map-config.json is compared."""
    a = _zim(tmp_path, "a.zim", meta_extra={"dataDate": "2026-09-01"})
    b = _zim(tmp_path, "b.zim", date="2026-10-02", meta_extra={"dataDate": "2026-09-01"})
    d = _zim(tmp_path, "d.zim", meta_extra={"dataDate": "2026-08-01"})
    assert _run(capsys, a, b)[0] == 0
    rc, out = _run(capsys, a, d)
    assert rc == 1 and "map-config.json" in out.split("changed:")[1]


def test_golden_builds_usage():
    """The wrapper refuses a short command line before doing any work."""
    if shutil.which("bash") is None:
        pytest.skip("no bash")
    for args in ([], ["main", "WORKTREE"], ["main", "WORKTREE", "/nonexistent", "--wikidata"]):
        r = subprocess.run(["bash", str(ROOT / "tools" / "golden_builds.sh"), *args],
                           capture_output=True, text=True)
        assert r.returncode == 2, args
        assert "BEFORE AFTER WORKDIR" in r.stderr
