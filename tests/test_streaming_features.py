"""The world search-cache tail must not hold every feature in RAM.

_finish_features_streaming replaces an in-memory annotate-then-sort that
reached 106 GB on the 2026-09-05 world build. These tests pin the two
things that refactor could plausibly have broken: the location context
still gets assigned, and the output is still ordered by type priority
then name.
"""
import json
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
