"""Street records that are pieces of one street are merged.

A street crossing several z14 tiles used to become one search record per
tile ("Avenue Saint-Martin" x9 in Monaco). merge_street_records collapses
same-name, same-location pieces chained within STREET_MERGE_KM, and must
never merge different streets that merely share a name.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streetzim.search_extract import (  # noqa: E402
    STREET_MERGE_KM, _finish_features_streaming, merge_street_records)

KM_LAT = 1 / 111.32  # degrees of latitude per km


def _st(lat, lon=7.42, loc="Monaco", name="Avenue Saint-Martin"):
    return {"name": name, "type": "street", "lat": lat, "lon": lon, "location": loc}


def test_pieces_one_tile_apart_merge_into_one_member():
    recs = [_st(43.70 + i * 2.0 * KM_LAT) for i in range(5)]  # a 8 km chain
    out = merge_street_records(recs)
    assert len(out) == 1
    assert out[0] is recs[2], "keep the member nearest the centre"


def test_single_linkage_chains():
    # A-B and B-C are within range, A-C is not: still one street.
    d = STREET_MERGE_KM * 0.9 * KM_LAT
    recs = [_st(43.0), _st(43.0 + d), _st(43.0 + 2 * d)]
    assert len(merge_street_records(recs)) == 1


def test_same_name_far_apart_stays_separate():
    recs = [_st(43.0), _st(43.0 + 3 * STREET_MERGE_KM * KM_LAT)]
    assert merge_street_records(recs) == recs


def test_same_name_different_place_stays_separate():
    # "Hauptstrasse" in two neighbouring villages 1 km apart.
    recs = [_st(48.0, loc="Dorf A", name="Hauptstrasse"),
            _st(48.0 + KM_LAT, loc="Dorf B", name="Hauptstrasse")]
    assert merge_street_records(recs) == recs


def test_longitude_distance_uses_latitude():
    # 0.04 deg of longitude is ~3.3 km at 43N but ~1.4 km at 70N.
    assert len(merge_street_records([_st(43.0, 7.40), _st(43.0, 7.44)])) == 2
    assert len(merge_street_records([_st(70.0, 7.40), _st(70.0, 7.44)])) == 1


def test_order_and_identity_preserved():
    recs = [_st(40.0, name="X"), _st(10.0, name="X"), _st(10.0 + KM_LAT, name="X")]
    out = merge_street_records(recs)
    assert out[0] is recs[0] and len(out) == 2
    assert all(any(o is r for r in recs) for o in out)


def _run_tail(tmp_path, feats, monkeypatch, merge=True):
    if not merge:
        monkeypatch.setenv("STREETZIM_MERGE_STREETS", "0")
    raw = tmp_path / "raw.jsonl"
    raw.write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in feats),
                   encoding="utf-8")
    out = _finish_features_streaming(str(raw), str(tmp_path), len(feats))
    return [json.loads(l) for l in Path(out).read_text(encoding="utf-8").splitlines()]


def _tail_feats():
    feats = [{"name": "Monaco", "type": "place", "subtype": "city", "lat": 43.73, "lon": 7.42}]
    feats += [{"name": "Avenue Saint-Martin", "type": "street", "lat": 43.73 + i * 0.01,
               "lon": 7.42} for i in range(4)]
    feats += [{"name": "Rue Grimaldi", "type": "street", "lat": 43.73, "lon": 7.42},
              {"name": "Café", "type": "poi", "lat": 43.73, "lon": 7.42}]
    return feats


def test_streaming_tail_merges_and_keeps_everything_else(tmp_path, monkeypatch):
    rows = _run_tail(tmp_path, _tail_feats(), monkeypatch)
    names = [(r["type"], r["name"]) for r in rows]
    assert names.count(("street", "Avenue Saint-Martin")) == 1
    assert ("street", "Rue Grimaldi") in names
    assert ("poi", "Café") in names and ("place", "Monaco") in names
    assert len(rows) == 4


def test_streaming_tail_merge_can_be_disabled(tmp_path, monkeypatch):
    rows = _run_tail(tmp_path, _tail_feats(), monkeypatch, merge=False)
    assert len(rows) == len(_tail_feats())


def _brute_force_clusters(recs):
    """All-pairs single linkage with the same distance rule, no grid."""
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
            if (a.get("location") or "") != (b.get("location") or ""):
                continue
            kx = 111.32 * math.cos(math.radians((a["lat"] + b["lat"]) / 2))
            if math.hypot((a["lat"] - b["lat"]) * 111.32,
                          (a["lon"] - b["lon"]) * kx) <= STREET_MERGE_KM:
                parent[find(j)] = find(i)
    return len({find(i) for i in range(n)})


def test_grid_never_misses_a_pair_that_brute_force_finds():
    import random
    rng = random.Random(7)
    for trial in range(300):
        lat0 = rng.uniform(-85, 85)
        recs = [_st(lat0 + rng.uniform(-0.1, 0.1), rng.uniform(-0.3, 0.3),
                    loc=rng.choice(["A", "B", ""]))
                for _ in range(rng.randint(2, 25))]
        assert len(merge_street_records(recs)) == _brute_force_clusters(recs), (trial, lat0)


def test_in_memory_path_merges_too(monkeypatch):
    # Regions under 5 GB of MBTiles take the in-memory path, not the
    # streaming tail; the first version of the merge only covered streaming.
    from streetzim.search_extract import merge_streets_in_sorted
    feats = [{"name": "Monaco", "type": "place", "lat": 43.73, "lon": 7.42}]
    feats += [_st(43.73 + i * 0.01) for i in range(4)]
    feats += [_st(43.73, name="Rue Grimaldi")]
    out = merge_streets_in_sorted(feats)
    assert [f["name"] for f in out] == ["Monaco", "Avenue Saint-Martin", "Rue Grimaldi"]
    monkeypatch.setenv("STREETZIM_MERGE_STREETS", "0")
    assert merge_streets_in_sorted(feats) == feats
