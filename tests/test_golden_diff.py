"""tools/golden_diff.py: which differences between two builds it accepts
(dates, Xapian, reordered JSON lists, renumbered Kiwix search pages, tile
feature order, control-build noise) and which it reports."""
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


def J(obj):
    return Entry("application/json", json.dumps(obj).encode())


def H(text):
    return Entry("text/html", text.encode())


BASE = {
    "Date": Entry("text/plain", b"2026-09-28"),
    "Title": Entry("text/plain", b"Monaco"),
    "fulltext/xapian": Entry("application/octet-stream+xapian", b"x1"),
    "map-config.json": J({"name": "Monaco", "buildDate": "2026/09"}),
    "search-data/mo.json": J([{"n": "Monaco"}, {"n": "Commune de Monaco"}]),
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
    new["search-data/mo.json"] = J([{"n": "Commune de Monaco"}, {"n": "Monaco"}])
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
    ("search-data/mo.json", J([{"n": "Monaco"}])),                     # a record lost
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


def test_added_and_dropped_entries():
    new = dict(BASE)
    del new["search/monaco-7.html"]
    new["tiles/0/0/0.pbf"] = Entry("application/x-protobuf", b"")
    c = gd.classify(BASE, new, META)
    assert c["only-before"] == ["search/monaco-7.html"]
    assert c["only-after"] == ["tiles/0/0/0.pbf"]


def test_control_marks_run_to_run_noise():
    new = dict(BASE)
    new["search-data/mo.json"] = J([{"n": "Monaco", "a": 1.0}])
    new["Title"] = Entry("text/plain", b"Monte Carlo")
    control = dict(BASE)
    control["search-data/mo.json"] = J([{"n": "Monaco", "a": 2.0}])
    c = gd.classify(BASE, new, META, control=control)
    assert c["noise"] == ["search-data/mo.json"]
    assert c["changed"] == ["Title"]      # the control did not change it


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


def test_main_on_real_zims(tmp_path, capsys):
    pytest.importorskip("libzim")
    from libzim.writer import Creator, Hint, Item, StringProvider

    class _Item(Item):
        def __init__(self, path, data):
            super().__init__()
            self._p, self._d = path, data

        def get_path(self): return self._p
        def get_title(self): return self._p
        def get_mimetype(self): return "application/json"
        def get_contentprovider(self): return StringProvider(self._d)
        def get_hints(self): return {Hint.FRONT_ARTICLE: False, Hint.COMPRESS: True}

    def build(name, date, records):
        path = tmp_path / name
        with Creator(str(path)).config_indexing(True, "en") as c:
            c.add_metadata("Title", "t")
            c.add_metadata("Date", date)
            c.add_item(_Item("search-data/mo.json", json.dumps(records)))
        return str(path)

    a = build("a.zim", "2026-09-28", [1, 2])
    b = build("b.zim", "2026-09-29", [2, 1])
    d = build("d.zim", "2026-09-28", [1, 2, 3])
    assert gd.main([a, b]) == 0
    assert "no differences beyond the expected ones" in capsys.readouterr().out
    assert gd.main([a, d]) == 1
    assert "search-data/mo.json" in capsys.readouterr().out


def test_coord_tolerance_accepts_small_moves_only():
    rec = {"n": "Impasse", "t": "street", "s": "path"}
    old = {"search-data/im.json": J([{**rec, "a": 43.74248, "o": 7.42267}])}
    near = {"search-data/im.json": J([{**rec, "a": 43.74253, "o": 7.42271}])}
    far = {"search-data/im.json": J([{**rec, "a": 43.75, "o": 7.42271}])}
    renamed = {"search-data/im.json": J([{**rec, "n": "Impasse X", "a": 43.74253, "o": 7.42271}])}
    assert gd.classify(old, near, set())["changed"] == ["search-data/im.json"]
    assert gd.classify(old, near, set(), coord_tol=0.001)["moved"] == ["search-data/im.json"]
    assert gd.classify(old, far, set(), coord_tol=0.001)["changed"] == ["search-data/im.json"]
    assert gd.classify(old, renamed, set(), coord_tol=0.001)["changed"] == ["search-data/im.json"]


def test_golden_builds_usage():
    """The wrapper refuses a short command line before doing any work."""
    if shutil.which("bash") is None:
        pytest.skip("no bash")
    for args in ([], ["main", "WORKTREE"], ["main", "WORKTREE", "/nonexistent", "--wikidata"]):
        r = subprocess.run(["bash", str(ROOT / "tools" / "golden_builds.sh"), *args],
                           capture_output=True, text=True)
        assert r.returncode == 2, args
        assert "BEFORE AFTER WORKDIR" in r.stderr
