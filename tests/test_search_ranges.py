"""Grouped siblings: small children of a split search node share one leaf
whose last path token is a code-point range (cloud/search_shards.py
GROUP_BYTES, docs/search-prefix-locality.md#grouped-siblings).

Why: china 2026-09-20 rebuilt with word rule 2 shipped 180,032 leaves, 94 k
of them with <= 5 records, because every CJK character after a hot prefix got
a leaf of its own; its 6.47 MB manifest failed validate_zim's 4 MB bar.

What must hold:

* every record still reaches a leaf, and that leaf's path covers one of the
  record's paths (no orphans, no leaf a reader would not compute);
* a range never spans a sibling with a leaf of its own, never takes the
  terminal, never outgrows its budget;
* ``group_bytes=0`` is exactly the planner every ZIM before 2026-10 was
  written with;
* the writer files a prefix with ranges under ``char_ranges`` (which viewers
  that predate ranges ignore) and one without under ``char_split``, byte for
  byte as before; the validator checks both.
"""
import json
import random
import sys
from pathlib import Path as FsPath

import pytest

sys.path.insert(0, str(FsPath(__file__).resolve().parent.parent))

from cloud import search_shards  # noqa: E402
from cloud.search_shards import (  # noqa: E402
    GROUP_BYTES, TERMINAL, TIER_MAX_DEPTH, TIER_ORDER, Aggregator, TierPlan,
    leaf_for, plan_by_tier, prefixes_for, range_bounds, range_token,
    record_paths, split_key, tier_for, token_cp, token_matches,
)
from cloud.validate_zim import _chk_search_data_sizes  # noqa: E402

TYPES = ("place", "poi", "street", "addr")


def _size(rec):
    return len(json.dumps(rec, separators=(",", ":"))) + 1


def _cjk_corpus(prefix_char="大", n=6000, seed=7):
    """One hot CJK prefix whose second character takes ~2,500 values, most
    of them a handful of times — china's shape — plus a few heavy ones."""
    rnd = random.Random(seed)
    recs = []
    for i in range(n):
        second = chr(0x4E00 + int(rnd.paretovariate(0.6)) % 2500)
        third = chr(0x4E00 + rnd.randrange(20000))
        recs.append({"n": f"{prefix_char}{second}{third}路", "t": rnd.choice(TYPES), "i": i})
    for i in range(1500):  # a heavy child among the light ones: 大丨
        recs.append({"n": f"{prefix_char}丨{chr(0x4E00 + i % 400)}", "t": "poi", "i": n + i})
    return recs


def _plan(recs, prefix, target, group_bytes=None):
    agg = Aggregator(prefix)
    for r in recs:
        agg.add(r, _size(r))
    return agg, agg.leaves(target_bytes=target, group_bytes=group_bytes)


# ------------------------------------------------------------ token helpers

def test_range_token_round_trips_and_is_no_other_token():
    assert range_token(0x4E00, 0x4E8B) == "r4e00.4e8b"
    assert range_bounds("r4e00.4e8b") == (0x4E00, 0x4E8B)
    # The tokens a character can produce are never ranges.
    for tok in ("r", "u4e00", TERMINAL, "_", "7", "u72"):
        assert range_bounds(tok) is None, tok
    assert token_cp("a") == 0x61 and token_cp("_") == 0x5F
    assert token_cp("u5927") == 0x5927 and token_cp(TERMINAL) is None


def test_token_matches():
    assert token_matches("u4e01", "u4e01")
    assert token_matches("r4e00.4e8b", "u4e00")
    assert token_matches("r4e00.4e8b", "u4e8b")
    assert not token_matches("r4e00.4e8b", "u4e8c")
    assert not token_matches("r4e00.4e8b", TERMINAL)
    assert token_matches("r61.66", "c") and not token_matches("r61.66", "g")


# ------------------------------------------------------------------ planner

def test_cjk_siblings_share_range_leaves():
    recs = _cjk_corpus()
    target = 64 * 1024
    _agg, grouped = _plan(recs, "u5927", target)
    _agg, flat = _plan(recs, "u5927", target, group_bytes=0)
    assert split_key(grouped) == "char_ranges"
    assert split_key(flat) == "char_split"
    # The point of the change: an order of magnitude fewer leaves.
    assert len(grouped) * 8 < len(flat), (len(grouped), len(flat))


def test_ranges_respect_their_budget_and_never_span_a_standalone_sibling():
    recs = _cjk_corpus()
    target = 64 * 1024
    group = min(GROUP_BYTES, target)
    agg, planned = _plan(recs, "u5927", target)
    n_ranges = 0
    for tier, path, _c, size in planned:
        b = range_bounds(path[-1])
        if b is None:
            continue
        n_ranges += 1
        assert b[0] < b[1]
        assert size <= group, (path, size)
        # Siblings inside [lo, hi]: all grouped, none with a leaf of its own,
        # each small enough to have joined, never the terminal.
        parent = path[:-1]
        for (t, p), (_cnt, sz) in agg.counts.items():
            if t != tier or len(p) != len(path) or p[:-1] != parent:
                continue
            c = token_cp(p[-1])
            if c is not None and b[0] <= c <= b[1]:
                assert sz <= group // 2, (p, sz)
                assert (tier, p) not in {(x, y) for x, y, _1, _2 in planned}
    assert n_ranges >= 5


def test_terminal_is_never_grouped():
    recs = [{"n": f"大{chr(0x4E00 + i)}", "t": "poi"} for i in range(300)]
    recs += [{"n": "大", "t": "poi"}, {"n": "大 x", "t": "poi"}] * 3
    recs += [{"n": "大大", "t": "poi"}]
    _agg, planned = _plan(recs, "u5927", 600)
    paths = {p for _t, p, _c, _b in planned}
    for p in paths:
        assert TERMINAL not in p or range_bounds(p[-1]) is None


def test_a_group_of_one_keeps_its_own_path():
    # big, small, big: the small one cannot join anything.
    recs = ([{"n": "大一" + "x" * 50, "t": "poi"}] * 40
            + [{"n": "大丁", "t": "poi"}]
            + [{"n": "大七" + "x" * 50, "t": "poi"}] * 40)
    _agg, planned = _plan(recs, "u5927", 2000)
    paths = sorted(p for _t, p, _c, _b in planned)
    assert ("u4e01",) in paths
    assert not any(range_bounds(t) for p in paths for t in p)


def test_a_range_stops_at_a_big_sibling():
    # small 丁, big 丂, small 七 and 丄: the range is 七..丄 only.
    recs = ([{"n": "大丁", "t": "poi"}]
            + [{"n": "大丂" + "x" * 50, "t": "poi"}] * 40
            + [{"n": "大七", "t": "poi"}, {"n": "大丄", "t": "poi"}])
    _agg, planned = _plan(recs, "u5927", 2000)
    assert sorted({p[0] for _t, p, _c, _b in planned}) == [
        "r4e03.4e04", "u4e01", "u4e02"]


def test_every_record_reaches_a_leaf_that_covers_one_of_its_paths():
    recs = _cjk_corpus()
    target = 64 * 1024
    _agg, planned = _plan(recs, "u5927", target)
    plans = plan_by_tier(planned)
    for r in recs:
        tier = tier_for(r)
        names = list(leaf_for("u5927", r, plans.get(tier, TierPlan(()))))
        assert names, r
        mine = record_paths("u5927", r, TIER_MAX_DEPTH[tier])
        for name in names:
            leaf = tuple(name.split("~")[1:-1])
            assert any(len(leaf) <= len(p) and all(
                token_matches(d, t) for d, t in zip(leaf, p)) for p in mine), (r, name)


def test_leaf_for_accepts_a_plain_path_set_too():
    recs = _cjk_corpus(n=2000)
    _agg, planned = _plan(recs, "u5927", 16 * 1024)
    plans = plan_by_tier(planned)
    for r in recs[:500]:
        tier = tier_for(r)
        assert (list(leaf_for("u5927", r, plans[tier]))
                == list(leaf_for("u5927", r, set(plans[tier].paths))))


def _old_leaves(agg, target_bytes):
    """Aggregator.leaves as of cf46f4d (before grouping), verbatim."""
    children, roots = {}, {}
    for tier, path in agg.counts:
        if len(path) == 1:
            roots.setdefault(tier, []).append(path)
        else:
            children.setdefault((tier, path[:-1]), []).append(path)
    out = []
    for tier in TIER_ORDER:
        cap = TIER_MAX_DEPTH.get(tier, 1)
        stack = [(p, 1) for p in sorted(roots.get(tier, []), reverse=True)]
        while stack:
            path, depth = stack.pop()
            count, size = agg.counts[(tier, path)]
            kids = children.get((tier, path))
            if size <= target_bytes or depth >= cap or not kids:
                out.append((tier, path, count, size))
                continue
            stack.extend((k, depth + 1) for k in sorted(kids, reverse=True))
    return out


@pytest.mark.parametrize("prefix,target", [("u5927", 16 * 1024), ("u5927", 2048)])
def test_group_bytes_zero_is_the_old_planner(prefix, target):
    agg, planned = _plan(_cjk_corpus(n=3000), prefix, target, group_bytes=0)
    assert sorted(planned) == sorted(_old_leaves(agg, target))


def test_latin_prefix_with_only_big_children_is_unchanged():
    # a-z under "ca", every child well over GROUP_MEMBER_BYTES: no range,
    # and the same plan as before.
    recs = [{"n": f"ca{c}{i}", "t": "poi", "pad": "x" * 2000}
            for c in "abcdefghijklmnopqrstuvwxyz" for i in range(80)]
    agg, planned = _plan(recs, "ca", 4 * 1024 * 1024)
    assert split_key(planned) == "char_split"
    assert sorted(planned) == sorted(_old_leaves(agg, 4 * 1024 * 1024))


# ------------------------------------------------------------------- writer

class _Item:
    def __init__(self, path, title, mime, data, compress=True, **_kw):
        self.path, self.data = path, data


class _Creator:
    def __init__(self):
        self.items = {}

    def add_item(self, it):
        self.items[it.path] = it.data


def _emit(tmp_path, records, *, split_mb):
    zim_writer = pytest.importorskip("streetzim.zim_writer")
    chunk_tmp = tmp_path / "chunks"
    chunk_tmp.mkdir()
    counts: dict = {}
    for rec in records:
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        for k in prefixes_for(rec["n"]):
            with open(chunk_tmp / f"{k}.jsonl", "a", encoding="utf-8") as f:
                f.write(line)
            counts[k] = counts.get(k, 0) + 1
    c = _Creator()
    zim_writer._search_emit_chunks(
        c, _Item, split_hot_search_chunks_mb=split_mb, chunk_tmp=str(chunk_tmp),
        chunk_counts=counts, total_features=len(records))
    return c.items


class _Arc:
    """What validate_zim's search check reads, over the emitted items."""

    def __init__(self, items):
        self.items = items

    def get_entry_by_path(self, path):
        blob = self.items[path]

        class _It:
            content = blob
            size = len(blob)

        class _E:
            def get_item(self):
                return _It()
        return _E()


def _multi_script(seed=11):
    """Thousands of CJK first characters (most prefixes tiny), hot CJK
    prefixes with thousands of second characters, a hot pinyin prefix, and a
    hot Latin prefix whose children are all big."""
    rnd = random.Random(seed)
    recs = []
    for i in range(3000):                       # 3,000 distinct first chars
        recs.append({"n": chr(0x4E00 + i * 5) + chr(0x4E00 + rnd.randrange(20000)), "t": "poi"})
    for head in "大新中":
        for _ in range(2500):
            recs.append({"n": head + chr(0x4E00 + int(rnd.paretovariate(0.6)) % 3000)
                         + chr(0x4E00 + rnd.randrange(20000)), "t": rnd.choice(TYPES)})
    for _ in range(3000):                        # pinyin under "zh"
        syl = rnd.choice(["ong", "ang", "en", "u", "i", "ao", "ou", "eng", "uan"])
        recs.append({"n": f"Zh{syl}{rnd.choice('bcdfghjklmnpqrstwxyz')}{rnd.randrange(99)} Lu",
                     "t": rnd.choice(TYPES)})
    for c in "abcdefghijklmnopqrstuvwxyz":       # "ca": every child big
        for i in range(60):
            recs.append({"n": f"Ca{c}a {i}", "t": "street", "pad": "y" * 400})
    for i, r in enumerate(recs):
        r["i"] = i
    return recs


def test_writer_files_ranges_under_char_ranges_and_validates(tmp_path):
    recs = _multi_script()
    items = _emit(tmp_path, recs, split_mb=0.05)
    m = json.loads(items["search-data/manifest.json"])
    cr, cs = m.get("char_ranges", {}), m.get("char_split", {})
    assert {"u5927", "u65b0", "u4e2d"} <= set(cr), sorted(cr)
    assert "ca" in cs and "ca" not in cr
    assert not set(cr) & set(cs)
    # A range path is a real leaf name in chunks, and sub_chunks is the exact
    # leaf union (old clients read it).
    for prefix, paths in cr.items():
        assert any(range_bounds(p.split("~")[-1]) for p in paths)
        assert sorted(m["sub_chunks"][prefix]) == sorted(
            k for k in m["chunks"] if k.split("~", 1)[0] == prefix and "~" in k)
    # Every record is somewhere it can be found from.
    found = set()
    for path, blob in items.items():
        if path.startswith("search-data/") and path != "search-data/manifest.json":
            found |= {r["i"] for r in json.loads(blob)}
    assert found == {r["i"] for r in recs}
    status, detail = _chk_search_data_sizes(_Arc(items))
    assert status == "pass", detail
    assert "grouped siblings" in detail


def test_manifest_size_is_bounded_on_a_cjk_heavy_region(tmp_path, monkeypatch):
    recs = _multi_script()
    (tmp_path / "g").mkdir()
    (tmp_path / "f").mkdir()
    grouped = _emit(tmp_path / "g", recs, split_mb=0.05)
    monkeypatch.setattr(search_shards, "GROUP_BYTES", 0)
    flat = _emit(tmp_path / "f", recs, split_mb=0.05)
    g = len(grouped["search-data/manifest.json"])
    f = len(flat["search-data/manifest.json"])
    leaves_g = sum("~" in k for k in json.loads(grouped["search-data/manifest.json"])["chunks"])
    leaves_f = sum("~" in k for k in json.loads(flat["search-data/manifest.json"])["chunks"])
    # Each of the 3,000 tiny CJK prefixes is one whole chunk either way
    # (~15 bytes each); what grouping removes is the per-character leaves of
    # the hot prefixes.
    assert g < 0.5 * f, (g, f)
    assert leaves_g * 10 < leaves_f, (leaves_g, leaves_f)
    assert g < 130 * 1024, g
    assert "char_ranges" not in json.loads(flat["search-data/manifest.json"])


# ---------------------------------------------------------------- validator

def _fake(manifest):
    items = {"search-data/manifest.json": json.dumps(manifest).encode()}
    for k in manifest["chunks"]:
        items[f"search-data/{k}.json"] = b"[]"
    return _Arc(items)


def _ranges_manifest():
    chunks = {"u5927~r4e00.4e8b~c": 5, "u5927~u5b66~c": 9}
    return {"total": 14, "chunks": chunks,
            "sub_chunks": {"u5927": sorted(chunks)},
            "char_ranges": {"u5927": ["r4e00.4e8b", "u5b66"]}}


def test_validator_passes_char_ranges():
    status, detail = _chk_search_data_sizes(_fake(_ranges_manifest()))
    assert status == "pass", detail


def test_validator_checks_char_ranges_paths_resolve():
    m = _ranges_manifest()
    m["char_ranges"]["u5927"].append("r4e90.4e99")
    status, detail = _chk_search_data_sizes(_fake(m))
    assert status == "fail" and "u5927~r4e90.4e99" in detail


def test_validator_checks_char_ranges_sub_chunks_union():
    m = _ranges_manifest()
    m["sub_chunks"]["u5927"] = ["u5927~u5b66~c"]
    status, detail = _chk_search_data_sizes(_fake(m))
    assert status == "fail" and "u5927" in detail


def test_validator_fails_a_prefix_in_both_keys():
    m = _ranges_manifest()
    m["char_split"] = {"u5927": ["u5b66"]}
    status, detail = _chk_search_data_sizes(_fake(m))
    assert status == "fail" and "both" in detail


def _status(m):
    return _chk_search_data_sizes(_fake(m))


def test_validator_fails_a_range_under_char_split():
    # A viewer that predates ranges compares tokens by equality: "大丁"
    # would find nothing.
    m = _ranges_manifest()
    m["char_split"] = m.pop("char_ranges")
    status, detail = _status(m)
    assert status == "fail" and "under char_split" in detail, detail


@pytest.mark.parametrize("tok", ["r4E00.4e8b", "r0x4e00.4e8b", "r4_e00.4e8b",
                                 "r4e00.4e8b ", "r4e00.", "r.4e8b"])
def test_validator_fails_a_malformed_range(tok):
    chunks = {f"u5927~{tok}~c": 5, "u5927~u5b66~c": 9}
    m = {"total": 14, "chunks": chunks, "sub_chunks": {"u5927": sorted(chunks)},
         "char_ranges": {"u5927": [tok, "u5b66", "r4f00.4f10"]}}
    m["chunks"]["u5927~r4f00.4f10~c"] = 1
    m["sub_chunks"]["u5927"] = sorted(m["chunks"])
    status, detail = _status(m)
    assert status == "fail" and "malformed range" in detail, (tok, detail)


def test_validator_fails_lo_above_hi():
    m = _ranges_manifest()
    m["chunks"] = {"u5927~r4e8b.4e00~c": 5, "u5927~u5b66~c": 9}
    m["sub_chunks"]["u5927"] = sorted(m["chunks"])
    m["char_ranges"]["u5927"] = ["r4e8b.4e00", "u5b66"]
    status, detail = _status(m)
    assert status == "fail" and "lo > hi" in detail, detail


def test_validator_fails_a_range_that_is_not_the_last_token():
    chunks = {"u5927~r4e00.4e8b~x~c": 5, "u5927~r4f00.4f01~c": 1}
    m = {"total": 6, "chunks": chunks, "sub_chunks": {"u5927": sorted(chunks)},
         "char_ranges": {"u5927": ["r4e00.4e8b~x", "r4f00.4f01"]}}
    status, detail = _status(m)
    assert status == "fail" and "not the last token" in detail, detail


def test_validator_fails_overlapping_ranges_in_one_tier():
    chunks = {"u5927~r4e00.4e8b~c": 5, "u5927~r4e80.4e90~c": 3}
    m = {"total": 8, "chunks": chunks, "sub_chunks": {"u5927": sorted(chunks)},
         "char_ranges": {"u5927": ["r4e00.4e8b", "r4e80.4e90"]}}
    status, detail = _status(m)
    assert status == "fail" and "overlap" in detail, detail


def test_overlapping_ranges_in_different_tiers_pass():
    chunks = {"u5927~r4e00.4e8b~c": 5, "u5927~r4e80.4e90~p": 3}
    m = {"total": 8, "chunks": chunks, "sub_chunks": {"u5927": sorted(chunks)},
         "char_ranges": {"u5927": ["r4e00.4e8b", "r4e80.4e90"]}}
    status, detail = _status(m)
    assert status == "pass", detail


def test_validator_fails_a_range_containing_a_sibling_leaf():
    chunks = {"u5927~r4e00.4e8b~c": 5, "u5927~u4e01~c": 3}
    m = {"total": 8, "chunks": chunks, "sub_chunks": {"u5927": sorted(chunks)},
         "char_ranges": {"u5927": ["r4e00.4e8b", "u4e01"]}}
    status, detail = _status(m)
    assert status == "fail" and "contains sibling U+4E01" in detail, detail
    # The same sibling in another tier is no conflict.
    chunks = {"u5927~r4e00.4e8b~c": 5, "u5927~u4e01~p": 3}
    m = {"total": 8, "chunks": chunks, "sub_chunks": {"u5927": sorted(chunks)},
         "char_ranges": {"u5927": ["r4e00.4e8b", "u4e01"]}}
    assert _status(m)[0] == "pass"


def test_validator_fails_char_ranges_without_a_range():
    chunks = {"u5927~u5b66~c": 9}
    m = {"total": 9, "chunks": chunks, "sub_chunks": {"u5927": sorted(chunks)},
         "char_ranges": {"u5927": ["u5b66"]}}
    status, detail = _status(m)
    assert status == "fail" and "with no range" in detail, detail


@pytest.mark.parametrize("key", ["char_ranges", "char_split", "sub_chunks"])
@pytest.mark.parametrize("bad", [["u5927"], {"u5927": "r4e00.4e8b"}, "x"])
def test_validator_fails_cleanly_on_a_non_map(key, bad):
    m = _ranges_manifest()
    m[key] = bad
    status, detail = _status(m)
    assert status == "fail" and key in detail, detail


def test_range_bounds_is_strict_like_the_viewer():
    for tok in ("r4E00.4e8b", "r0x4e00.4e8b", "r4_e00.4e8b", "r 4e00.4e8b",
                "r+4e00.4e8b", "r4e00.4e8b\n", "r4e00..4e8b"):
        assert range_bounds(tok) is None, tok
    assert range_bounds("r0.10ffff") == (0, 0x10FFFF)
