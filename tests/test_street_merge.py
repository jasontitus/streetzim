"""Street records that are pieces of one street are merged.

A street crossing several z14 tiles used to become one search record per
tile ("Avenue Saint-Martin" x27 across Monaco's chunks). merge_street_records
collapses same-name, same-location pieces within STREET_MERGE_KM, and must
never merge different streets that merely share a name.

The first version ran inside extraction and an adversarial review found it
(a) lost Wikipedia links matched on a dropped piece's coordinates, (b) lost
streets at region edges when a search cache was cut to a bbox afterwards and
(c) picked representatives non-deterministically. The merge now runs on the
final file, keeps every piece's point in _pts and is order-independent;
those cases are tested here.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streetzim.search_extract import (  # noqa: E402
    STREET_MAX_EXTENT_KM, STREET_MERGE_KM, STREET_MERGE_MAX_GROUP,
    merge_street_records, merge_streets_in_file)

KM_LAT = 1 / 111.32  # degrees of latitude per km


def _st(lat, lon=7.42, loc="Monaco", name="Avenue Saint-Martin", subtype="minor"):
    return {"name": name, "type": "street", "subtype": subtype,
            "lat": lat, "lon": lon, "location": loc}


def _key(recs):
    return sorted(json.dumps(r, sort_keys=True) for r in recs)


def test_pieces_one_tile_apart_merge_into_the_middle_piece():
    recs = [_st(43.70 + i * 1.2 * KM_LAT) for i in range(5)]  # a 4.8 km chain
    out = merge_street_records(recs)
    assert len(out) == 1
    assert (out[0]["lat"], out[0]["lon"]) == (recs[2]["lat"], recs[2]["lon"])
    assert len(out[0]["_pts"]) == 5, "every piece's point is kept"


def test_unmerged_records_are_returned_untouched():
    recs = [_st(43.0), _st(43.0 + 3 * STREET_MERGE_KM * KM_LAT)]
    out = merge_street_records(recs)
    assert out == recs and all("_pts" not in r for r in out)


def test_same_name_different_place_stays_separate():
    recs = [_st(48.0, loc="Dorf A", name="Hauptstrasse"),
            _st(48.0 + KM_LAT, loc="Dorf B", name="Hauptstrasse")]
    assert merge_street_records(recs) == recs


def test_long_streets_are_capped():
    # A 20 km street in one place becomes several records, each within the
    # extent cap, so a result still lands near the part the user wants.
    recs = [_st(40.0 + i * 2.0 * KM_LAT) for i in range(11)]
    out = merge_street_records(recs)
    assert len(out) > 1
    for r in out:
        lats = [p[0] for p in r.get("_pts", [[r["lat"], r["lon"]]])]
        assert (max(lats) - min(lats)) / KM_LAT <= STREET_MAX_EXTENT_KM + 1e-6
    # And no piece is lost: all points are accounted for exactly once.
    pts = sorted(tuple(p) for r in out for p in r.get("_pts", [[r["lat"], r["lon"]]]))
    assert pts == sorted((r["lat"], r["lon"]) for r in recs)


def test_order_independent():
    rng = random.Random(3)
    recs = [_st(43.73 + rng.uniform(0, 0.05), 7.42 + rng.uniform(0, 0.05),
                loc=rng.choice(["A", "B"])) for _ in range(40)]
    # Include exact ties: two pieces equidistant from their centre.
    recs += [_st(10.0), _st(10.0 + KM_LAT)]
    want = _key(merge_street_records(recs))
    for _ in range(10):
        rng.shuffle(recs)
        assert _key(merge_street_records(recs)) == want


def test_huge_same_place_groups_are_left_alone():
    recs = [_st(43.7 + (i % 50) * 1e-4, 7.4 + (i // 50) * 1e-4)
            for i in range(STREET_MERGE_MAX_GROUP + 1)]
    assert merge_street_records(recs) == recs


def test_latitude_aware_distance():
    assert len(merge_street_records([_st(43.0, 7.40), _st(43.0, 7.44)])) == 2
    assert len(merge_street_records([_st(70.0, 7.40), _st(70.0, 7.44)])) == 1


def _brute_force_clusters(recs):
    """Plain single linkage, no grid and no extent cap (used on data small
    enough that the cap never applies)."""
    import math
    n = len(recs)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            i = parent[i]
        return i
    for i in range(n):
        for j in range(i + 1, n):
            a, b = recs[i], recs[j]
            if a["location"] != b["location"]:
                continue
            kx = 111.32 * math.cos(math.radians((a["lat"] + b["lat"]) / 2))
            if math.hypot((a["lat"] - b["lat"]) * 111.32,
                          (a["lon"] - b["lon"]) * kx) <= STREET_MERGE_KM:
                parent[find(j)] = find(i)
    return len({find(i) for i in range(n)})


def test_grid_never_misses_a_pair_that_brute_force_finds():
    rng = random.Random(7)
    for trial in range(300):
        lat0 = rng.uniform(-85, 85)
        # Spread < extent cap, so the cap cannot split clusters here.
        recs = [_st(lat0 + rng.uniform(-0.012, 0.012), rng.uniform(-0.012, 0.012),
                    loc=rng.choice(["A", "B", ""]))
                for _ in range(rng.randint(2, 25))]
        assert len(merge_street_records(recs)) == _brute_force_clusters(recs), (trial, lat0)


def _write(path, feats):
    path.write_text("".join(json.dumps(f, ensure_ascii=False, separators=(",", ":")) + "\n"
                            for f in feats), encoding="utf-8")


def _file_feats():
    feats = [{"name": "Monaco", "type": "place", "lat": 43.73, "lon": 7.42}]
    feats += [_st(43.73 + i * 0.01) for i in range(4)]
    feats += [_st(43.73, name="Rue Grimaldi"),
              {"name": "Café", "type": "poi", "lat": 43.73, "lon": 7.42}]
    return feats


def test_file_pass_merges_and_copies_everything_else_byte_for_byte(tmp_path):
    p = tmp_path / "f.jsonl"
    _write(p, _file_feats())
    before = p.read_text(encoding="utf-8").splitlines(keepends=True)
    assert merge_streets_in_file(str(p)) == (5, 2)
    after = p.read_text(encoding="utf-8").splitlines(keepends=True)
    kept_untouched = [l for l in after if '"_pts"' not in l]
    assert all(l in before for l in kept_untouched)
    assert sum('"Avenue Saint-Martin"' in l for l in after) == 1
    assert '"Café"' in "".join(after) and '"Monaco"' in "".join(after)


def test_file_pass_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("STREETZIM_MERGE_STREETS", "0")
    p = tmp_path / "f.jsonl"
    _write(p, _file_feats())
    before = p.read_bytes()
    merge_streets_in_file(str(p))
    assert p.read_bytes() == before


def test_bbox_cut_before_merge_keeps_edge_streets(tmp_path):
    # The review's case: a street crossing the region edge. Because the
    # merge now runs after the cut, only in-region pieces are candidates,
    # so the street survives with an in-region point.
    feats = [_st(43.73, 7.410), _st(43.73, 7.425), _st(43.73, 7.440)]
    inside = [f for f in feats if f["lon"] < 7.42]
    p = tmp_path / "f.jsonl"
    _write(p, inside)
    merge_streets_in_file(str(p))
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["lon"] < 7.42
