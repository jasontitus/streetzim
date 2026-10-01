"""cloud/swap_viewer_rust.py --rebuild-search: re-derive a ZIM's search index
under the current word rule (docs/search-prefix-locality.md, "Retrofit").

The records are recovered from the source's leaves once per feature (from
the home prefix: the key of the name's first two characters, which every
writer used whatever the word rule), re-keyed with ``prefixes_for`` and
emitted by the build's own pass; the manifest gets ``"word_rule": 2`` and
keeps the source's other keys.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud.search_shards import prefixes_for  # noqa: E402


def _svr():
    pytest.importorskip("libzim")
    pytest.importorskip("streetzim.zim_writer")
    os.environ.setdefault("STREETZIM_ROOT", str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "swap_viewer_rust_under_test", ROOT / "ops" / "cloud" / "swap_viewer_rust.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rule1_zim_chunks(records):
    """search-data as a rule-1 writer laid it out: every record in each of
    its rule-1 prefixes; ``-0``/``-1`` hash children for one prefix."""
    chunks: dict = {}
    for r in records:
        for k in prefixes_for(r["n"], 1):
            chunks.setdefault(k, []).append(r)
    out = {}
    for k, recs in chunks.items():
        if len(recs) > 2:
            out[f"{k}-0"] = recs[::2]
            out[f"{k}-1"] = recs[1::2]
        else:
            out[k] = recs
    return out


def test_source_records_recovers_each_feature_once(tmp_path):
    svr = _svr()
    recs = [{"n": "พัทยา", "t": "place", "a": 1, "o": 2},
            {"n": "ถนน พัทยา", "t": "street", "a": 1, "o": 2},
            {"n": "Cafe Pattaya", "t": "poi", "a": 1, "o": 2},
            {"n": "Cafe Pattaya", "t": "poi", "a": 1, "o": 2},   # two identical features
            {"n": "Pa", "t": "poi", "a": 3, "o": 4},
            {"n": "Pa x", "t": "poi", "a": 3, "o": 4},
            {"n": "Pa y", "t": "poi", "a": 3, "o": 4}]
    chunks = _rule1_zim_chunks(recs)
    manifest = {"total": len(recs), "chunks": {k: len(v) for k, v in chunks.items()}}
    blobs = {f"search-data/{k}.json": json.dumps(v).encode() for k, v in chunks.items()}
    spool = tmp_path / "spool.jsonl"
    with open(spool, "w", encoding="utf-8") as f:
        n = svr._source_records(lambda p: blobs[p], manifest, f)
    got = sorted(json.loads(x)["n"] for x in spool.read_text().splitlines())
    assert n == len(recs)
    assert got == sorted(r["n"] for r in recs)


def test_rebuild_search_rekeys_under_rule_2_and_keeps_manifest_keys(tmp_path):
    svr = _svr()
    recs = [{"n": "พัทยา", "t": "place", "a": 1, "o": 2},
            {"n": "ถนน พัทยา", "t": "street", "a": 1, "o": 2},
            {"n": "कोलकाता", "t": "place", "a": 1, "o": 2},
            {"n": "Cafe", "t": "poi", "a": 1, "o": 2}]
    chunks = _rule1_zim_chunks(recs)
    manifest = {"total": len(recs), "addresses_stripped": True,
                "chunks": {k: len(v) for k, v in chunks.items()}}
    blobs = {f"search-data/{k}.json": json.dumps(v).encode() for k, v in chunks.items()}

    class C:
        def __init__(self):
            self.items: dict = {}

        def add_item(self, it):
            self.items[it._path] = it._data

    c = C()
    work = tmp_path / "w"
    work.mkdir()
    n = svr._rebuild_search(c, lambda p: blobs[p], manifest, work)
    assert n == len(recs)
    m = json.loads(c.items["search-data/manifest.json"])
    assert m["word_rule"] == 2 and m["addresses_stripped"] is True and m["total"] == 4
    want: dict = {}
    for r in recs:
        for k in prefixes_for(r["n"], 2):
            want.setdefault(k, []).append(r["n"])
    assert set(m["chunks"]) == set(want)
    for k, names in want.items():
        assert sorted(x["n"] for x in json.loads(c.items[f"search-data/{k}.json"])) == sorted(names)
    # The rule-1 fragment prefixes are gone.
    assert "ue17" not in m["chunks"] and "u932" not in m["chunks"]


def test_source_records_counts_a_record_once_across_leaves_of_its_prefix(tmp_path):
    # A character-split home prefix holds a record once per leaf one of its
    # words reaches ("Pa Pattaya": the terminal leaf and the "t" leaf); two
    # identical features are two copies in each. Expect 2, not 4.
    svr = _svr()
    r = {"n": "Pa Pattaya", "t": "poi", "a": 1, "o": 2}
    other = {"n": "Pattani", "t": "poi", "a": 5, "o": 6}
    chunks = {"pa~_e~p": [r, r], "pa~t~p": [r, other, r]}
    manifest = {"total": 3, "chunks": {k: len(v) for k, v in chunks.items()},
                "char_split": {"pa": ["_e", "t"]}}
    blobs = {f"search-data/{k}.json": json.dumps(v).encode() for k, v in chunks.items()}
    spool = tmp_path / "spool.jsonl"
    with open(spool, "w", encoding="utf-8") as f:
        n = svr._source_records(lambda p: blobs[p], manifest, f)
    got = sorted(json.loads(x)["n"] for x in spool.read_text().splitlines())
    assert n == 3
    assert got == ["Pa Pattaya", "Pa Pattaya", "Pattani"]
