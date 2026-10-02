"""cloud/swap_viewer_rust.py --rebuild-search: re-derive a ZIM's search index
under the current word rule (docs/search-prefix-locality.md, "Retrofit").

The records are recovered from the source's leaves once per feature (from
the home prefix: the key of the whole name's first two characters, as the
current writer or the pre-2026-09-03 one computed it), re-keyed with
``prefixes_for`` and
emitted by the build's own pass; the manifest gets ``"word_rule": 2`` and
keeps the source's other keys.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unicodedata
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
    spool, total = svr._recover_search(lambda p: blobs[p], manifest, work)
    n = svr._rebuild_search(c, spool, total, manifest, work)
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


# ---- ZIMs written before 6223071 (2026-09-03) ------------------------------
# Those writers keyed the whole name by _prefix_key(name[:2]) on the RAW
# name and split words on the raw name too. When the raw 2nd character is a
# mark the fold drops, that home differs from today's: decomposed "Ủy ban"
# (raw key "u_", folded "uy"), NFD "Écouen" ("e_" vs "ec"), a name opening
# with a Thai tone mark ("__" vs "_p"). Taking records only from today's
# home lost them: 156 records of southeast-asia 2026-05-09.

def _legacy_prefixes(name):
    import re
    from cloud.search_shards import prefix_key
    keys = {prefix_key(name[:2])}
    keys |= {prefix_key(m) for m in re.findall(r"[^\W_]+", name) if len(m) >= 2}
    return keys


LEGACY_RECS = [
    {"n": unicodedata.normalize("NFD", "Écouen"), "t": "place", "a": 1, "o": 1},
    {"n": unicodedata.normalize("NFD", "Ủy ban nhân dân xã"), "t": "poi", "a": 2, "o": 2},
    {"n": unicodedata.normalize("NFD", "Ấp Bình Minh"), "t": "place", "a": 3, "o": 3},
    {"n": "่ Phetvittayakhan School", "t": "poi", "a": 4, "o": 4},
    {"n": "่ Phetvittayakhan School", "t": "poi", "a": 4, "o": 4},  # two features
    {"n": "Cafe Ecouen", "t": "poi", "a": 5, "o": 5},
    {"n": "พัทยา", "t": "place", "a": 6, "o": 6},
]


def _index(recs, keys_for):
    chunks: dict = {}
    for r in recs:
        for k in keys_for(r["n"]):
            chunks.setdefault(k, []).append(r)
    return chunks


def _recover(svr, tmp_path, chunks, **kw):
    manifest = {"total": kw.pop("total", None), "chunks": {k: len(v) for k, v in chunks.items()}}
    blobs = {f"search-data/{k}.json": json.dumps(v).encode() for k, v in chunks.items()}
    spool = tmp_path / "spool.jsonl"
    with open(spool, "w", encoding="utf-8") as f:
        n = svr._source_records(blobs.__getitem__, manifest, f)
    return n, sorted(json.loads(x)["n"] for x in spool.read_text().splitlines())


def test_the_legacy_homes_really_differ():
    svr = _svr()
    for r in LEGACY_RECS[:4]:
        cur, legacy = svr._homes(r["n"])
        assert cur != legacy, r["n"]
        assert legacy in _legacy_prefixes(r["n"])
        assert cur not in _legacy_prefixes(r["n"]) or r["n"].startswith("่")


def test_records_of_a_pre_2026_09_03_index_are_all_recovered(tmp_path):
    svr = _svr()
    n, got = _recover(svr, tmp_path, _index(LEGACY_RECS, _legacy_prefixes))
    assert n == len(LEGACY_RECS)
    assert got == sorted(r["n"] for r in LEGACY_RECS)


def test_a_record_under_both_homes_is_taken_once(tmp_path):
    # A current writer's index plus the legacy home: a record whose homes
    # differ sits in both prefixes. Counted once (twice for two features).
    svr = _svr()
    both = lambda nm: prefixes_for(nm) | _legacy_prefixes(nm)  # noqa: E731
    n, got = _recover(svr, tmp_path, _index(LEGACY_RECS, both))
    assert n == len(LEGACY_RECS)
    assert got == sorted(r["n"] for r in LEGACY_RECS)


def test_an_unreadable_declared_leaf_is_an_error(tmp_path):
    svr = _svr()
    manifest = {"total": 1, "chunks": {"ca": 1, "cb": 1}}
    blobs = {"search-data/ca.json": json.dumps([{"n": "Cafe", "t": "poi"}]).encode()}
    with open(tmp_path / "s", "w") as f, pytest.raises(SystemExit, match="cb"):
        svr._source_records(blobs.__getitem__, manifest, f)


def test_rebuild_refuses_a_record_count_that_differs_from_the_manifest(tmp_path):
    svr = _svr()
    chunks = {"ca": [{"n": "Cafe", "t": "poi", "a": 1, "o": 1}]}
    blobs = {"search-data/ca.json": json.dumps(chunks["ca"]).encode()}

    class C:
        def __init__(self):
            self.items: dict = {}

        def add_item(self, it):
            self.items[it._path] = it._data

    for allow in (False, True):
        work = tmp_path / f"w{allow}"
        work.mkdir()
        manifest = {"total": 2, "chunks": {"ca": 1}}
        if allow:
            spool, total = svr._recover_search(blobs.__getitem__, manifest, work,
                                               allow_total_mismatch=True)
            assert svr._rebuild_search(C(), spool, total, manifest, work) == 1
        else:
            with pytest.raises(SystemExit, match="recovered 1"):
                svr._recover_search(blobs.__getitem__, manifest, work)


def test_search_spill_dir_must_be_named_and_off_the_root_fs(tmp_path, monkeypatch):
    svr = _svr()
    monkeypatch.delenv("TMPDIR", raising=False)
    assert svr._spill_root(None, search=False) is None
    with pytest.raises(SystemExit, match="--tmp"):
        svr._spill_root(None, search=True)
    with pytest.raises(SystemExit, match="not a directory"):
        svr._spill_root(str(tmp_path / "nope"), search=True)
    real = svr._on_root_fs
    assert real("/")
    if os.path.ismount("/storage"):       # the build host
        assert not real("/storage")
    # Same device as / -> refused; another device -> accepted.
    monkeypatch.setattr(svr, "_on_root_fs", lambda p: True)
    with pytest.raises(SystemExit, match="root filesystem"):
        svr._spill_root(str(tmp_path), search=True)
    monkeypatch.setattr(svr, "_on_root_fs", lambda p: False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert svr._spill_root(None, search=True) == str(tmp_path)


# ---- checks before anything is written ----------------------------------------

def test_a_manifest_without_total_is_refused_with_a_clear_message(tmp_path):
    svr = _svr()
    blobs = {"search-data/ca.json": json.dumps([{"n": "Cafe", "t": "poi"}]).encode()}
    with pytest.raises(SystemExit, match="no integer 'total'"):
        svr._recover_search(blobs.__getitem__, {"chunks": {"ca": 1}}, tmp_path)
    spool, n = svr._recover_search(blobs.__getitem__, {"chunks": {"ca": 1}}, tmp_path,
                                   allow_total_mismatch=True)
    assert n == 1 and spool.read_text().count("\n") == 1


def test_the_rebuild_checks_the_records_it_was_handed(tmp_path):
    svr = _svr()
    spool = tmp_path / "s.jsonl"
    spool.write_text('{"n":"Cafe","t":"poi","a":1,"o":1}\n')

    class C:
        def add_item(self, it):
            pass
    with pytest.raises(SystemExit, match="holds 1 record"):
        svr._rebuild_search(C(), spool, 2, {"chunks": {"ca": 1}}, tmp_path)


def test_fs_type_takes_the_longest_mount(tmp_path):
    svr = _svr()
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/sda1 / ext4 rw 0 0\n"
                      "tmpfs /run/user tmpfs rw 0 0\n"
                      "/dev/md0 /stor ext4 rw 0 0\n"
                      "tmpfs /stor/ram\\040disk tmpfs rw 0 0\n")
    assert svr._fs_type("/run/user/1000", str(mounts)) == "tmpfs"
    assert svr._fs_type("/run", str(mounts)) == "ext4"
    assert svr._fs_type("/stor/x", str(mounts)) == "ext4"
    assert svr._fs_type("/storage", str(mounts)) == "ext4"      # not under /stor
    assert svr._fs_type("/stor/ram disk/a", str(mounts)) == "tmpfs"
    assert svr._fs_type("/x", str(tmp_path / "missing")) is None


def test_the_spill_dir_is_checked_up_front(tmp_path, monkeypatch):
    svr = _svr()
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr(svr, "_on_root_fs", lambda p: False)
    monkeypatch.setattr(svr, "_fs_type", lambda p: "ext4")
    assert svr._spill_root(str(tmp_path), search=True) == str(tmp_path)
    # tmpfs is memory: refused for a search spool and for a named --tmp.
    monkeypatch.setattr(svr, "_fs_type", lambda p: "tmpfs")
    with pytest.raises(SystemExit, match="tmpfs"):
        svr._spill_root(str(tmp_path), search=True)
    with pytest.raises(SystemExit, match="tmpfs"):
        svr._spill_root(str(tmp_path), search=False)
    # --tmp is checked without a search option too.
    monkeypatch.setattr(svr, "_fs_type", lambda p: "ext4")
    with pytest.raises(SystemExit, match="not a directory"):
        svr._spill_root(str(tmp_path / "nope"), search=False)
    monkeypatch.setattr(svr, "_on_root_fs", lambda p: True)
    with pytest.raises(SystemExit, match="root filesystem"):
        svr._spill_root(str(tmp_path), search=False)
    # $TMPDIR alone, without a search option, stays the caller's business.
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert svr._spill_root(None, search=False) == str(tmp_path)
