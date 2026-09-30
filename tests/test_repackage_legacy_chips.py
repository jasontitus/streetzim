"""`repackage_zim --split-find-chips` must not drop chips a ZIM already
ships just because cloud/chip_rules.py no longer defines them.

ZIMs built before the 2026-09-16 food merge carry chip-restaurants.json and
chip-cafes.json and no chip-food.json; --no-llm-bundle builds also ship no
poi.json to rebuild "food" from. The retrofit used to drop the old pair
with a warning, leaving the region with no food chip at all. The viewers
map the old pair onto their single Food & Drink button, so it is carried
over unchanged.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_validator_regression import _make_minimal_zim, _mk_item  # noqa: E402


def _records(n, s):
    return [{"n": f"{s} {i}", "t": "poi", "s": s, "a": 43.7 + i * 1e-4,
             "o": 7.42} for i in range(n)]


def test_pre_merge_chips_survive_split_find_chips(tmp_path: Path):
    pytest.importorskip("libzim")
    from libzim.reader import Archive
    from cloud.repackage_zim import repackage

    chips = {
        "restaurants": _records(3, "restaurant"),
        "cafes": _records(2, "cafe"),
        "bars": _records(4, "bar"),
    }
    manifest = {"total": 9, "categories": {},
                "chips": {cid: {"label": cid.title(), "count": len(recs),
                                "bytes": len(json.dumps(recs))}
                          for cid, recs in chips.items()}}
    items = [_mk_item("category-index/manifest.json", "application/json",
                      json.dumps(manifest).encode())]
    items += [_mk_item(f"category-index/chip-{cid}.json", "application/json",
                       json.dumps(recs).encode())
              for cid, recs in chips.items()]
    src = _make_minimal_zim(tmp_path, "src.zim", extra_items=items)
    dst = tmp_path / "dst.zim"

    repackage(str(src), str(dst), swap_viewer=False, split_find_chips=True)

    arc = Archive(str(dst))
    out = json.loads(bytes(arc.get_entry_by_path(
        "category-index/manifest.json").get_item().content))
    assert set(out["chips"]) == {"restaurants", "cafes", "bars"}
    assert out["chips"]["restaurants"]["count"] == 3
    assert out["chips"]["cafes"]["label"] == "Cafes"
    got = json.loads(bytes(arc.get_entry_by_path(
        "category-index/chip-cafes.json").get_item().content))
    assert sorted(r["n"] for r in got) == ["cafe 0", "cafe 1"]


def test_legacy_pair_dropped_when_food_is_rebuilt(tmp_path: Path):
    """A pre-merge ZIM that still ships poi.json gets a rebuilt "food" chip;
    carrying restaurants/cafes as well would store the records twice."""
    pytest.importorskip("libzim")
    from libzim.reader import Archive
    from cloud.repackage_zim import repackage

    poi = _records(3, "restaurant") + _records(2, "cafe") + _records(4, "bar")
    chips = {"restaurants": _records(3, "restaurant"), "cafes": _records(2, "cafe")}
    manifest = {"total": 9, "categories": {"poi": len(poi)},
                "chips": {cid: {"label": cid.title(), "count": len(r),
                                "bytes": len(json.dumps(r))}
                          for cid, r in chips.items()}}
    items = [_mk_item("category-index/manifest.json", "application/json",
                      json.dumps(manifest).encode()),
             _mk_item("category-index/poi.json", "application/json",
                      json.dumps(poi).encode())]
    items += [_mk_item(f"category-index/chip-{cid}.json", "application/json",
                       json.dumps(r).encode()) for cid, r in chips.items()]
    src = _make_minimal_zim(tmp_path, "src.zim", extra_items=items)
    dst = tmp_path / "dst.zim"
    repackage(str(src), str(dst), swap_viewer=False, split_find_chips=True)

    out = json.loads(bytes(Archive(str(dst)).get_entry_by_path(
        "category-index/manifest.json").get_item().content))
    assert out["chips"]["food"]["count"] == 5
    assert "restaurants" not in out["chips"] and "cafes" not in out["chips"]
