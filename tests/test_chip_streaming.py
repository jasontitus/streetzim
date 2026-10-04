"""Find chips split a chip at a time from the category files on disk
(cloud.chip_rules.split_jsonl_by_chip), not from every poi record held
as a dict (over 8 GB on China, which killed its 16 GB build): the same
records, and the same chip files in the ZIM."""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud.chip_rules import (  # noqa: E402
    CHIP_RULES,
    read_jsonl,
    split_jsonl_by_chip,
    split_records_by_chip,
)

SUBTYPES = sorted({s for c in CHIP_RULES for s in c.subtypes}
                  | {"bench", "attraction", "toy_store", "store", "memorial"})


def _records(rng, cat, n):
    out = []
    for i in range(n):
        out.append({"t": cat, "s": rng.choice(SUBTYPES),
                    "n": rng.choice(["Royal Museum", "Café 東京", "x", "Library \"Q\""]) + str(i),
                    "a": round(rng.uniform(18, 53), 5), "o": round(rng.uniform(73, 135), 5)})
    return out


def _write(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")


@pytest.fixture
def cats(tmp_path):
    rng = random.Random(7)
    recs = {"poi": _records(rng, "poi", 3000), "park": _records(rng, "park", 400),
            "place": _records(rng, "place", 300)}
    for cat, rs in recs.items():
        _write(tmp_path / f"{cat}.jsonl", rs)
    return tmp_path, recs


def test_the_stream_gives_every_chip_the_same_records(cats):
    tmp, recs = cats
    out = tmp / "chips"
    out.mkdir()
    paths = split_jsonl_by_chip({c: str(tmp / f"{c}.jsonl") for c in ("poi", "park")}, str(out))
    want = split_records_by_chip({"poi": recs["poi"], "park": recs["park"]})
    assert set(paths) == {c.id for c in CHIP_RULES}
    for chip in CHIP_RULES:
        assert read_jsonl(paths[chip.id]) == want[chip.id], chip.id
    assert sum(len(v) for v in want.values()) > 1000


class _Item:
    def __init__(self, path, title, mime, blob):
        self.path, self.blob = path, blob


class _Creator:
    def __init__(self):
        self.items = {}

    def add_item(self, item):
        self.items[item.path] = item.blob


@pytest.mark.parametrize("no_llm_bundle", [True, False])
def test_the_zim_gets_the_same_chip_files(cats, no_llm_bundle):
    """Every chip file and the manifest as the old in-memory split made them."""
    from cloud.chip_shards import plan_chip
    from streetzim.zim_writer import _search_category_index
    tmp, recs = cats
    creator = _Creator()
    _search_category_index(
        creator, _Item, split_find_chips=True, no_llm_bundle=no_llm_bundle, wiki_geo={},
        cat_chunk_counts={c: len(r) for c, r in recs.items()}, cat_dir=str(tmp),
        cat_shards={}, CATEGORY_SHARD_MIN_BYTES=64 << 10)
    want = {}
    for chip in CHIP_RULES:
        rs = split_records_by_chip({"poi": recs["poi"], "park": recs["park"]})[chip.id]
        for path, _, blob in plan_chip(rs).files(chip.id, chip.label):
            want[path] = blob
    got = {p: b for p, b in creator.items.items() if p in want}
    assert got == want
    manifest = json.loads(creator.items["category-index/manifest.json"])
    assert manifest["total"] == sum(len(r) for r in recs.values())
    assert {k: v["count"] for k, v in manifest["chips"].items()} == {
        c.id: len(split_records_by_chip({"poi": recs["poi"], "park": recs["park"]})[c.id])
        for c in CHIP_RULES}
    assert sorted(p.name for p in tmp.iterdir()) == []


def test_poi_records_are_not_all_held_at_once(tmp_path):
    """60,000 poi records in no chip, 200 in one: the step's peak is about
    the chip, not the category (as dicts, the category is ~40 MB here)."""
    import tracemalloc

    from streetzim.zim_writer import _search_category_index
    rng = random.Random(3)
    poi = [{"t": "poi", "s": "bench", "n": f"bench {i}", "a": rng.uniform(18, 53),
            "o": rng.uniform(73, 135), "pad": "x" * 200} for i in range(60000)]
    poi += [{"t": "poi", "s": "fuel", "n": f"fuel {i}", "a": 30.0, "o": 100.0}
            for i in range(200)]
    _write(tmp_path / "poi.jsonl", poi)
    size = (tmp_path / "poi.jsonl").stat().st_size
    del poi
    tracemalloc.start()
    try:
        _search_category_index(
            _Creator(), _Item, split_find_chips=True, no_llm_bundle=True, wiki_geo={},
            cat_chunk_counts={"poi": 60200}, cat_dir=str(tmp_path), cat_shards={},
            CATEGORY_SHARD_MIN_BYTES=64 << 10)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < size / 2, (peak, size)
