"""Unit tests for cloud/search_shards.py — the character-path + tier layout
for search-data chunks (docs/search-prefix-locality.md).

Two properties matter most:

* reader/writer parity — prefix_key must agree with create_osm_zim.py's
  _prefix_key and the viewers' keyFor, or a reader asks for leaves the writer
  never wrote (cross-checked against 7,341 real korea-mongolia records);
* no orphans — every record fed to the planner must reach a leaf. The first
  implementation dropped every CJK/Hangul name ("ch빌딩") because it decided
  where a path split by looking for "~" inside a "u1107" token.
"""
import json
import sys
from pathlib import Path as FsPath

import pytest

sys.path.insert(0, str(FsPath(__file__).resolve().parent.parent))

from cloud.search_shards import (  # noqa: E402
    DEFAULT_TIER, SHARD_TARGET_BYTES, TERMINAL, TIER_MAX_DEPTH, Aggregator,
    char_split_paths, leaf_for, leaf_name, norm, paths_for, prefix_key,
    tier_for, token_for,
)


def _rec(name, t="poi"):
    return {"n": name, "t": t, "a": 0.0, "o": 0.0}


def _size(rec):
    return len(json.dumps(rec, separators=(",", ":"), ensure_ascii=False))


def _plan(records, prefix="ca", target=2048):
    agg = Aggregator(prefix)
    for r in records:
        agg.add(r, _size(r))
    leaves = agg.leaves(target_bytes=target)
    by_tier = {}
    for tier, path, _c, _b in leaves:
        by_tier.setdefault(tier, set()).add(path)
    return leaves, by_tier


def _orphans(records, prefix="ca", target=2048):
    _leaves, by_tier = _plan(records, prefix, target)
    return [r for r in records
            if not list(leaf_for(prefix, r, by_tier.get(tier_for(r), ())))]


# ---------------------------------------------------------------- prefix_key

@pytest.mark.parametrize("word,key", [
    ("Caracas", "ca"), ("caracas", "ca"), ("Cáceres", "ca"),
    ("a", "a_"), ("45 Broadway", "45"), ("a store", "a_"),
    ("_private", "_p"), ("東京", "u6771"), ("пермь", "u43f"), ("", "__"),
])
def test_prefix_key(word, key):
    assert prefix_key(word) == key


def test_prefix_key_second_char_non_ascii_collapses():
    # Accent folding happens FIRST, so "cé" is "ce" by the time the ASCII
    # test runs. The collapse applies to characters folding leaves alone.
    assert prefix_key("cé") == "ce"
    assert prefix_key("cя") == "c_"
    assert prefix_key("c東") == "c_"


def test_norm_folds_accents_and_case():
    assert norm("Cáceres") == "caceres"
    assert norm("ÉCOUEN") == "ecouen"


# --------------------------------------------------------------------- tiers

@pytest.mark.parametrize("t,tier", [
    ("place", "c"), ("airport", "c"), ("peak", "c"), ("park", "c"),
    ("water", "c"), ("poi", "p"), ("street", "s"), ("addr", "a"),
])
def test_tier_for_known_types(t, tier):
    assert tier_for(_rec("x", t)) == tier


def test_unknown_and_missing_type_get_the_default_tier():
    assert tier_for(_rec("x", "transit_stop")) == DEFAULT_TIER
    assert tier_for({"n": "x"}) == DEFAULT_TIER


# --------------------------------------------------------------------- paths

def test_path_comes_from_the_qualifying_word():
    assert paths_for("ca", "Caracas", 1) == {("r",)}
    assert paths_for("ca", "Caracas", 2) == {("r", "a")}


def test_every_qualifying_word_contributes_a_path():
    assert paths_for("ca", "Casa Cabral", 1) == {("s",), ("b",)}


def test_non_qualifying_words_are_ignored():
    assert paths_for("ca", "Plaza Caracas", 1) == {("r",)}


def test_word_equal_to_the_prefix_uses_the_terminal_path():
    assert paths_for("ca", "Ca", 1) == {(TERMINAL,)}


def test_terminal_token_is_distinct_from_the_punctuation_token():
    # japan "10": the word "10" ends at once, while the whole name "10-5 …"
    # starts its path with the hyphen's "_". Sharing one token stranded every
    # short word when the node split.
    assert (TERMINAL,) in paths_for("10", "10", 2)
    assert any(p[0] == "_" and p != (TERMINAL,)
               for p in paths_for("10", "10-5 Street", 2))
    assert token_for("-") == "_" != TERMINAL


def test_whole_name_key_contributes_its_third_character():
    assert ("s",) in paths_for("a_", "a store", 1)


def test_name_with_no_qualifying_word_still_gets_a_path():
    assert paths_for("ca", "", 1) == {(TERMINAL,)}


def test_no_orphans_when_a_word_ends_where_punctuation_continues():
    # The japan "10"/"12" orphan class: 476 k and 113 k records.
    recs = ([_rec("10", "addr")] * 5
            + [_rec(f"10-{i} Chuo Street", "addr") for i in range(80)]
            + [_rec(f"1930-10 Something {i}", "addr") for i in range(40)])
    assert _orphans(recs, prefix="10", target=256) == []


def test_tokens_stay_ascii_for_non_ascii_characters():
    assert token_for("빌") == "u1107".replace("1107", format(ord("빌"), "x"))
    (path,) = paths_for("ch", "ch빌딩", 2)
    assert all(p.isascii() for p in path)
    assert leaf_name("ch", path, "p").isascii()


def test_cjk_token_stays_whole():
    # The bug this guards: "u1107" must stay ONE token, not five characters.
    # NFKD decomposes Hangul syllables into jamo, so "빌딩" is four code
    # points — the writer normalises identically, so reader and writer agree.
    (path,) = paths_for("ch", "ch빌딩", 4)
    assert all(t.startswith("u") and t.isascii() for t in path)
    assert path[0] == "u" + format(ord(norm("빌")[0]), "x")


def test_non_ascii_prefix_consumes_one_character():
    (path,) = paths_for(prefix_key("東京駅"), "東京駅", 2)
    assert path[0] == "u" + format(ord("京"), "x")


def test_paths_are_capped_at_the_requested_depth():
    assert all(len(p) <= 3 for p in paths_for("ca", "Caracas", 3))


# --------------------------------------------------------------- no orphans

def test_no_orphans_for_latin_names():
    recs = [_rec(f"Ca{c}{i} Plaza") for c in "rlsm" for i in range(20)]
    assert _orphans(recs) == []


def test_no_orphans_for_cjk_and_hangul_names():
    # korea-mongolia 'ch' orphaned exactly these shapes: an ASCII prefix
    # followed by non-ASCII characters.
    recs = ([_rec("ch빌딩"), _rec("CH새로빌아파트"), _rec("Ch치유성형외과")]
            + [_rec(f"Chase Bank {i}") for i in range(30)])
    assert all(prefix_key(r["n"]) == "ch" for r in recs)
    assert _orphans(recs, prefix="ch") == []


def test_no_orphans_for_words_that_end_at_a_split_node():
    # "Gim" ends where "Gimpo…" keeps going: when the planner splits that
    # node, the short word still needs a leaf ("gi~m~_~p").
    recs = ([_rec("Gim")] * 3
            + [_rec(f"Gimpo Airport {i}") for i in range(60)]
            + [_rec(f"Gimhae Station {i}") for i in range(60)])
    assert _orphans(recs, prefix="gi", target=512) == []


def test_short_word_gets_a_terminal_path():
    assert paths_for("gi", "Gim", 4) == {("m", TERMINAL)}
    assert paths_for("gi", "Gimpo", 4) == {("m", "p", "o", TERMINAL)}


def test_no_orphans_when_every_tier_is_present():
    recs = ([_rec(f"Carrera {i}", "addr") for i in range(50)]
            + [_rec(f"Calle {i}", "street") for i in range(50)]
            + [_rec("Caracas", "place"), _rec("Café Central", "poi")])
    assert _orphans(recs) == []


def test_no_orphans_at_a_tiny_target_that_forces_max_depth():
    recs = [_rec(f"Caracas {i}", "place") for i in range(200)]
    assert _orphans(recs, target=64) == []


# ----------------------------------------------------------------- aggregate

def test_leaves_split_until_under_target():
    recs = [_rec(f"Ca{c}{chr(97 + i % 26)}{i} Street") for c in "rl"
            for i in range(200)]
    leaves, _ = _plan(recs, target=4096)
    assert leaves
    assert all(b <= 4096 or len(p) >= TIER_MAX_DEPTH["p"]
               for _t, p, _c, b in leaves)


def test_a_small_prefix_stays_one_leaf_per_character():
    leaves, _ = _plan([_rec("Caracas", "place"), _rec("Calle Uno", "street")])
    paths = {(t, p) for t, p, _c, _b in leaves}
    assert ("c", ("r",)) in paths
    assert ("s", ("l",)) in paths


def test_tier_depth_caps_are_respected():
    recs = [_rec(f"Carrera {i}", "addr") for i in range(400)]
    leaves, _ = _plan(recs, target=64)
    assert all(len(p) <= TIER_MAX_DEPTH["a"] for _t, p, _c, _b in leaves)
    # Oversized after the cap → the caller hash-splits it.
    assert any(b > 64 for _t, _p, _c, b in leaves)


def test_aggregator_is_deterministic():
    recs = [_rec(f"Ca{chr(97 + i % 13)}{i} Plaza") for i in range(200)]
    assert _plan(recs)[0] == _plan(recs)[0]


def test_counts_match_what_was_fed():
    recs = [_rec("Caracas", "place"), _rec("Caracas Norte", "place"),
            _rec("Cali", "place")]
    leaves, _ = _plan(recs)
    counts = {(t, p): c for t, p, c, _b in leaves}
    assert counts[("c", ("r",))] == 2
    assert counts[("c", ("l",))] == 1


def test_leaf_for_picks_the_deepest_planned_path():
    recs = ([_rec(f"Cara {i}") for i in range(40)]
            + [_rec(f"Carb {i}") for i in range(40)])
    _leaves, by_tier = _plan(recs, target=1024)
    got = list(leaf_for("ca", _rec("Cara 7"), by_tier["p"]))
    # Two paths, both wanted: the word "cara" (typed as "car…") and the whole
    # name "cara_7" (typed as "cara 7"), which is how "45 Broadway" stays
    # findable by "45 b". This is the measured 1.12 leaves/record duplication.
    assert len(got) == 2
    assert all(g.startswith("ca~r~a") and g.endswith("~p") for g in got)
    assert any(g.endswith(f"~{TERMINAL}~p") for g in got)


def test_leaf_for_returns_one_leaf_per_qualifying_word():
    got = set(leaf_for("ca", _rec("Casa Cabral"), {("s",), ("b",)}))
    assert got == {"ca~s~p", "ca~b~p"}


def test_leaf_for_does_not_repeat_a_leaf():
    # Two words landing on the same path must not write the record twice.
    got = list(leaf_for("ca", _rec("Cara Cara"), {("r",)}))
    assert got == ["ca~r~p"]


def test_char_split_paths_are_sorted_strings():
    leaves = [("c", ("r",), 1, 1), ("p", ("r", "a"), 1, 1), ("p", ("a",), 1, 1)]
    assert char_split_paths(leaves) == ["a", "r", "r~a"]


def test_leaf_name_shape():
    assert leaf_name("ca", ("r", "a"), "p") == "ca~r~a~p"


def test_record_with_unicode_line_separator_round_trips_through_a_temp_file(tmp_path):
    """U+2028/U+2029 are legal inside a JSON string and ensure_ascii=False
    writes them raw, but str.splitlines() treats them as line breaks — which
    cut one east-coast-us address ("8 444 Lundy's Lane, Niagara Falls")
    in half and failed the whole region twice. The retrofit and the writer
    both read their per-leaf temp files back, so they must split on "\\n"
    alone."""
    recs = [_rec("8 444 Lundy's Lane, Niagara Falls", "addr"),
            _rec("8 Paragraph Road", "addr"),
            _rec("Plain Street", "addr")]
    p = tmp_path / "leaf.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n")

    raw = p.read_text(encoding="utf-8")
    assert len(raw.splitlines()) > len(recs)          # the trap
    got = [json.loads(x) for x in raw.split("\n") if x]
    assert got == recs
    assert SHARD_TARGET_BYTES == 4 * 1024 * 1024
