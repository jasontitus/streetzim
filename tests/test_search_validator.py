"""Validator rules for the character-split search layout
(docs/search-prefix-locality.md), exercised through a fake Archive so no ZIM
is needed.

The rules exist because of specific failure modes:

* an empty or partial ``sub_chunks`` list makes OLD clients (iOS, in-ZIM apps)
  return nothing instead of merely being slow — 143 united-states prefixes
  already ship an empty list today;
* a ``char_split`` path with no chunk behind it is a query that silently finds
  nothing;
* a name-query leaf (tier c/p) over 16 MB defeats the whole point of the
  split, while street/address leaves are exempt because characters cannot
  divide a million "Carrera 7"s;
* the manifest is parsed before any search can run.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cloud.validate_zim import (  # noqa: E402
    SEARCH_LEAF_FAIL_MB, SEARCH_MANIFEST_FAIL_MB, _chk_category_index,
    _chk_search_data_sizes,
)


class _Item:
    def __init__(self, blob):
        self.content = blob
        self.size = len(blob)


class _Entry:
    def __init__(self, path, blob):
        self.path = path
        self.is_redirect = False
        self._item = _Item(blob)

    def get_item(self):
        return self._item


class _FakeArchive:
    """Only what _chk_search_data_sizes touches: path lookup and sizes.
    ``sizes`` overrides an entry's reported size without materialising it."""

    def __init__(self, manifest, sizes=None, manifest_pad=0):
        blob = json.dumps(manifest, separators=(",", ":")).encode()
        if manifest_pad:
            blob = blob + b" " * manifest_pad
        self._blobs = {"search-data/manifest.json": blob}
        for name in manifest.get("chunks", {}):
            self._blobs[f"search-data/{name}.json"] = b"[]"
        self._sizes = sizes or {}

    def get_entry_by_path(self, path):
        if path not in self._blobs:
            raise KeyError(path)
        e = _Entry(path, self._blobs[path])
        name = path[len("search-data/"):-len(".json")]
        if name in self._sizes:
            e._item.size = self._sizes[name]
        return e


def _manifest(chunks, sub_chunks=None, char_split=None):
    m = {"total": sum(chunks.values()), "chunks": chunks}
    if sub_chunks is not None:
        m["sub_chunks"] = sub_chunks
    if char_split is not None:
        m["char_split"] = char_split
    return m


def _split_manifest(**kw):
    """A valid two-leaf character split: ca~r~c and ca~r~a."""
    chunks = {"ca~r~c": 10, "ca~r~a": 90}
    return _manifest(chunks,
                     sub_chunks={"ca": ["ca~r~c", "ca~r~a"]},
                     char_split={"ca": ["r"]}, **kw)


# ------------------------------------------------------------------ healthy

def test_valid_character_split_passes():
    status, detail = _chk_search_data_sizes(_FakeArchive(_split_manifest()))
    assert status == "pass"
    assert "char-split" in detail


def test_todays_hash_split_layout_still_passes():
    # No char_split key at all: the layout every shipped ZIM has now.
    mani = _manifest({"de-0": 5, "de-1": 5}, sub_chunks={"de": ["de-0", "de-1"]})
    status, _ = _chk_search_data_sizes(_FakeArchive(mani))
    assert status == "pass"


def test_unsplit_prefix_passes():
    status, _ = _chk_search_data_sizes(_FakeArchive(_manifest({"ca": 10})))
    assert status == "pass"


# -------------------------------------------------------------- sub_chunks

def test_empty_sub_chunks_for_a_split_prefix_fails():
    mani = _manifest({"ca~r~c": 10}, sub_chunks={"ca": []},
                     char_split={"ca": ["r"]})
    status, detail = _chk_search_data_sizes(_FakeArchive(mani))
    assert status == "fail"
    assert "sub_chunks" in detail


def test_sub_chunks_missing_a_leaf_fails():
    mani = _manifest({"ca~r~c": 10, "ca~r~a": 90},
                     sub_chunks={"ca": ["ca~r~c"]},
                     char_split={"ca": ["r"]})
    status, detail = _chk_search_data_sizes(_FakeArchive(mani))
    assert status == "fail"
    assert "sub_chunks" in detail


def test_sub_chunks_listing_a_stale_leaf_fails():
    mani = _manifest({"ca~r~c": 10},
                     sub_chunks={"ca": ["ca~r~c", "ca~z~c"]},
                     char_split={"ca": ["r"]})
    status, _ = _chk_search_data_sizes(_FakeArchive(mani))
    assert status == "fail"


def test_hash_children_of_a_leaf_must_be_listed_too():
    mani = _manifest({"ca~r~c": 10, "ca~r~a-0": 40, "ca~r~a-1": 50},
                     sub_chunks={"ca": ["ca~r~c", "ca~r~a-0", "ca~r~a-1"]},
                     char_split={"ca": ["r"]})
    status, _ = _chk_search_data_sizes(_FakeArchive(mani))
    assert status == "pass"


def test_another_prefixs_leaves_do_not_count_as_ours():
    # "cat~…" must not satisfy prefix "ca" (exact first token only).
    mani = _manifest({"ca~r~c": 10, "cat~r~c": 10},
                     sub_chunks={"ca": ["ca~r~c"], "cat": ["cat~r~c"]},
                     char_split={"ca": ["r"], "cat": ["r"]})
    status, _ = _chk_search_data_sizes(_FakeArchive(mani))
    assert status == "pass"


# ------------------------------------------------------------- char_split

def test_char_split_path_with_no_chunk_fails():
    mani = _manifest({"ca~r~c": 10},
                     sub_chunks={"ca": ["ca~r~c"]},
                     char_split={"ca": ["r", "z"]})
    status, detail = _chk_search_data_sizes(_FakeArchive(mani))
    assert status == "fail"
    assert "char_split" in detail


# ------------------------------------------------------------- leaf sizes

def test_oversized_name_query_leaf_fails():
    big = (SEARCH_LEAF_FAIL_MB + 1) * 1024 * 1024
    status, detail = _chk_search_data_sizes(
        _FakeArchive(_split_manifest(), sizes={"ca~r~c": big}))
    assert status == "fail"
    assert "name-query leaf" in detail


def test_oversized_address_leaf_is_exempt_from_the_leaf_bar():
    # Characters cannot divide a million "Carrera 7"s, and addresses are read
    # only for digit queries — so the 16 MB name-query bar does not apply.
    over_leaf_bar = (SEARCH_LEAF_FAIL_MB + 1) * 1024 * 1024
    status, _ = _chk_search_data_sizes(
        _FakeArchive(_split_manifest(), sizes={"ca~r~a": over_leaf_bar}))
    assert status == "pass"


def test_a_very_large_address_leaf_still_warns():
    # The pre-existing 50-200 MB band stays in force for every chunk.
    status, detail = _chk_search_data_sizes(
        _FakeArchive(_split_manifest(), sizes={"ca~r~a": 56 * 1024 * 1024}))
    assert status == "warn"
    assert "50" in detail


def test_hash_child_of_a_name_query_leaf_is_checked():
    big = (SEARCH_LEAF_FAIL_MB + 1) * 1024 * 1024
    mani = _manifest({"ca~r~p-0": 10},
                     sub_chunks={"ca": ["ca~r~p-0"]},
                     char_split={"ca": ["r"]})
    status, detail = _chk_search_data_sizes(
        _FakeArchive(mani, sizes={"ca~r~p-0": big}))
    assert status == "fail"
    assert "name-query leaf" in detail


# ---------------------------------------------------------- manifest size

def test_manifest_over_the_cap_fails():
    pad = SEARCH_MANIFEST_FAIL_MB * 1024 * 1024 + 1024
    status, detail = _chk_search_data_sizes(
        _FakeArchive(_split_manifest(), manifest_pad=pad))
    assert status == "fail"
    assert "manifest" in detail


# ------------------------------------------------- sharded categories

def _cat_arc(manifest, present):
    """Fake archive whose category-index entries are only those in `present`."""
    class _A(_FakeArchive):
        def get_entry_by_path(self, path):
            if path == "category-index/manifest.json" or path in present:
                return super().get_entry_by_path("category-index/manifest.json")
            raise KeyError(path)

    a = _A(_manifest({"x": 1}))
    a._blobs["category-index/manifest.json"] = json.dumps(
        manifest, separators=(",", ":")).encode()
    return a


def _place_shards(subs=("g000", "g001")):
    return {"label": "place", "count": 10, "bytes": 10,
            "sub_chunks": list(subs), "layout": "geo",
            "shards": [[0, 0, 1, 1, 5, 5]] * len(subs)}


def _place_manifest(shards):
    return {"total": 10, "categories": {"place": 10},
            "category_shards": {"place": shards}}


def test_sharded_place_with_all_shards_present_passes():
    status, _ = _chk_category_index(_cat_arc(
        _place_manifest(_place_shards()),
        {"category-index/place-g000.json", "category-index/place-g001.json"}))
    assert status == "pass"


def test_sharded_place_with_a_missing_shard_fails():
    # Otherwise the manifest declares shards, every other check passes, and
    # the Find page silently stops naming the nearest city.
    status, detail = _chk_category_index(_cat_arc(
        _place_manifest(_place_shards()), {"category-index/place-g000.json"}))
    assert status == "fail"
    assert "place-g001.json" in detail


def test_sharded_place_declaring_no_shards_fails():
    status, detail = _chk_category_index(_cat_arc(
        _place_manifest({"label": "place", "count": 10, "bytes": 10}), set()))
    assert status == "fail"
    assert "no shards" in detail


def test_unparseable_manifest_fails():
    class _Broken(_FakeArchive):
        def get_entry_by_path(self, path):
            if path.endswith("manifest.json"):
                return _Entry(path, b"{not json")
            return super().get_entry_by_path(path)

    status, _ = _chk_search_data_sizes(_Broken(_split_manifest()))
    assert status == "fail"
