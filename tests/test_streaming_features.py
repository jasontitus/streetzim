"""The world search-cache tail must not hold every feature in RAM.

_finish_features_streaming replaces an in-memory annotate-then-sort that
reached 106 GB on the 2026-09-05 world build. These tests pin the two
things that refactor could plausibly have broken: the location context
still gets assigned, and the output is still ordered by type priority
then name.
"""
import json
import math
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import create_osm_zim as coz  # noqa: E402

TYPE_ORDER = {"place": 0, "airport": 1, "peak": 2, "park": 3,
              "water": 4, "poi": 5, "street": 6}


def _write_raw(tmp_path, feats):
    p = tmp_path / "search_features.raw.jsonl"
    p.write_text("\n".join(json.dumps(f) for f in feats) + "\n", encoding="utf-8")
    return str(p)


def _feats():
    return [
        {"name": "Zebra Street", "type": "street", "lat": 37.30, "lon": -122.00},
        {"name": "Palo Alto", "type": "place", "subtype": "city", "lat": 37.44, "lon": -122.14},
        {"name": "Alpha Cafe", "type": "poi", "lat": 37.44, "lon": -122.14},
        {"name": "Mount Nowhere", "type": "peak", "lat": 37.45, "lon": -122.15},
        {"name": "Apple Park", "type": "park", "lat": 37.33, "lon": -122.01},
        {"name": "San Jose", "type": "place", "subtype": "city", "lat": 37.33, "lon": -121.88},
    ]


def test_streaming_finisher_sorts_and_annotates(tmp_path):
    raw = _write_raw(tmp_path, _feats())
    out = coz._finish_features_streaming(raw, str(tmp_path), len(_feats()))
    rows = [json.loads(l) for l in Path(out).read_text(encoding="utf-8").splitlines()]

    assert len(rows) == len(_feats()), "every feature must survive the tail"
    keys = [(TYPE_ORDER[r["type"]], r["name"]) for r in rows]
    assert keys == sorted(keys), f"output not in (type, name) order: {keys}"
    # places sort first, and within a type by name
    assert [r["name"] for r in rows if r["type"] == "place"] == ["Palo Alto", "San Jose"]
    # a non-place near a place picks up its location context
    cafe = next(r for r in rows if r["name"] == "Alpha Cafe")
    assert cafe.get("location"), "location context was not assigned"

    # scaffolding must not survive
    assert not (tmp_path / "search_features.raw.jsonl").exists()
    assert not (tmp_path / "search_features.keyed").exists()
    assert not (tmp_path / "search_features.sorted").exists()
    import streetzim.search_extract as se
    assert se._place_grid is None, "place dicts must not outlive annotation"


def test_streaming_finisher_survives_tabs_and_newlines_in_names(tmp_path):
    """The sort key is tab-delimited; a name containing a tab must not
    shift the columns and corrupt or drop the row."""
    feats = [
        {"name": "Weird\tTabbed\tName", "type": "poi", "lat": 37.44, "lon": -122.14},
        {"name": "Normal Place", "type": "place", "lat": 37.44, "lon": -122.14},
    ]
    raw = _write_raw(tmp_path, feats)
    out = coz._finish_features_streaming(raw, str(tmp_path), len(feats))
    rows = [json.loads(l) for l in Path(out).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    # the payload keeps the original name, tabs and all
    assert any(r["name"] == "Weird\tTabbed\tName" for r in rows)


@pytest.mark.parametrize("failure", ["initializer", "worker"])
def test_streaming_grid_is_released_when_annotation_fails(tmp_path, monkeypatch, failure):
    from concurrent.futures import Future
    import concurrent.futures
    import streetzim.search_extract as se

    class FailingPool:
        def __init__(self, **kwargs):
            assert se._place_grid, "the failure must happen after allocating the grid"
            if failure == "initializer":
                raise RuntimeError("annotation failed")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, *args):
            fut = Future()
            fut.set_exception(RuntimeError("annotation failed"))
            return fut

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", FailingPool)
    raw = _write_raw(tmp_path, _feats())
    with pytest.raises(RuntimeError, match="annotation failed"):
        se._finish_features_streaming(raw, str(tmp_path), len(_feats()))
    assert se._place_grid is None
    assert Path(raw).exists(), "failed annotation must retain its input for diagnosis"


def test_streaming_releases_executor_grid_before_external_sort(tmp_path, monkeypatch):
    from concurrent.futures import Future
    import concurrent.futures
    import subprocess
    import streetzim.search_extract as se

    destroyed = []

    class InlinePool:
        def __init__(self, initializer, initargs, **kwargs):
            self.initargs = initargs  # real executors retain these after shutdown
            initializer(*initargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __del__(self):
            destroyed.append(True)

        def submit(self, fn, *args):
            fut = Future()
            fut.set_result(fn(*args))
            return fut

    real_run = subprocess.run

    def sort_without_resident_grid(*args, **kwargs):
        assert se._place_grid is None
        assert destroyed, "closed executor's initargs must not retain the place grid"
        return real_run(*args, **kwargs)

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", InlinePool)
    monkeypatch.setattr(subprocess, "run", sort_without_resident_grid)
    raw = _write_raw(tmp_path, _feats())
    out = se._finish_features_streaming(raw, str(tmp_path), len(_feats()))
    assert len(Path(out).read_text().splitlines()) == len(_feats())


def test_published_search_keeps_places_three_metres_apart(tmp_path):
    """Known source positions expose coarser rounding without assumptions
    about how many shops legitimately share a point in a real country."""
    reader = pytest.importorskip("libzim.reader")
    pytest.importorskip("libzim.writer")
    feats = [
        {"name": "Nearby Cafe North", "type": "poi", "subtype": "cafe",
         "lat": 43.7301393, "lon": 7.4201123},
        {"name": "Nearby Cafe South", "type": "poi", "subtype": "cafe",
         "lat": 43.7301123, "lon": 7.4201123},
    ]
    source = tmp_path / "features.jsonl"
    source.write_text("".join(json.dumps(f) + "\n" for f in feats), encoding="utf-8")
    (tmp_path / "ml.js").write_text("//")
    (tmp_path / "ml.css").write_text("/**/")
    target = tmp_path / "nearby.zim"
    coz.create_zim(
        target, tiles={}, tile_metadata={}, fonts={},
        maplibre_js_path=str(tmp_path / "ml.js"),
        maplibre_css_path=str(tmp_path / "ml.css"),
        viewer_html_path=str(ROOT / "resources/viewer/index.html"),
        map_config={"name": "Nearby places"}, name="OSM - Nearby places",
        bbox=(7.40, 43.72, 7.44, 43.76), search_features_path=str(source),
        xapian_mode="none", zim_workers=1)
    archive = reader.Archive(str(target))
    records = json.loads(bytes(
        archive.get_entry_by_path("search-data/ne.json").get_item().content))
    assert len(records) == 2
    by_name = {r["n"]: r for r in records}
    assert set(by_name) == {f["name"] for f in feats}

    # Local distance in metres, independent of the writer's precision knob.
    metres_per_degree = math.pi * 6_371_000 / 180
    for feat in feats:
        record = by_name[feat["name"]]
        displacement = metres_per_degree * math.hypot(
            record["a"] - feat["lat"],
            (record["o"] - feat["lon"]) * math.cos(math.radians(feat["lat"])))
        assert displacement < 1, f"{feat['name']} pin moved {displacement:.2f} m"
    separation = metres_per_degree * abs(records[0]["a"] - records[1]["a"])
    assert 2 < separation < 4, f"three-metre neighbours now {separation:.2f} m apart"
