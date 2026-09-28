"""cloud/search_shards.split_records_recursive / sub_bucket_for_name: the
hot-chunk splitter continent builds rely on (--split-hot-search-chunks-mb).
Monaco-sized builds never reach it, so it is pinned here."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud.search_shards import split_records_recursive, sub_bucket_for_name  # noqa: E402


def test_fnv1a_values_are_pinned():
    # FNV-1a 32: "" is the offset basis 0x811C9DC5, "a" is 0xE40C292C. The
    # viewer (subBucketFor) and mcpzim compute the same numbers.
    assert sub_bucket_for_name("", 1 << 32) == 0x811C9DC5
    assert sub_bucket_for_name("a", 1 << 32) == 0xE40C292C
    assert [sub_bucket_for_name(n, 16) for n in ["Tokyo", "東京", "Café"]] == [9, 15, 9]


def _recs(names):
    return [{"n": n, "t": "poi", "s": "", "a": 0.0, "o": 0.0, "l": ""} for n in names]


def _all(leaves):
    out = []
    for _prefix, blob, count in leaves:
        recs = json.loads(blob)
        assert len(recs) == count
        out.extend(recs)
    return out


def test_small_chunk_is_one_leaf():
    recs = _recs(["a", "b"])
    leaves = split_records_recursive(recs, "ab", 10_000, 16, 3)
    assert [(p, c) for p, _b, c in leaves] == [("ab", 2)]


def test_split_by_name_keeps_every_record_once_and_fits():
    recs = _recs([f"name {i}" for i in range(3000)])
    leaves = split_records_recursive(recs, "na", 20_000, 16, 3)
    assert len(leaves) > 1
    assert sorted(r["n"] for r in _all(leaves)) == sorted(r["n"] for r in recs)
    for prefix, blob, _c in leaves:
        assert len(blob) <= 20_000
        assert prefix.startswith("na-")
    # Name split is by hash: every record lands in its own name's bucket.
    for prefix, blob, _c in leaves:
        first = prefix.split("-")[1]
        for r in json.loads(blob):
            assert format(sub_bucket_for_name(r["n"], 16), "x") == first


def test_identical_names_fall_back_to_index_split():
    recs = _recs(["same"] * 2000)  # every record hashes to one bucket
    leaves = split_records_recursive(recs, "sa", 10_000, 16, 2)
    assert len(leaves) > 1, "degenerate names must still split"
    assert len(_all(leaves)) == 2000


def test_depth_limit_stops_recursion():
    recs = _recs([f"n{i}" for i in range(5000)])
    leaves = split_records_recursive(recs, "nn", 10, 16, 1)  # impossible threshold
    assert all(p.count("-") == 1 for p, _b, _c in leaves)
    assert len(_all(leaves)) == 5000
