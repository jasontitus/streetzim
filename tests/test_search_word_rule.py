"""The search index's word rule (cloud/search_shards.py ``words``).

Rule 1 (every ZIM before 2026-10; manifest without ``word_rule``): a word is
a run of alphanumerics, so a mark the fold keeps -- an Indic vowel sign, a
Thai vowel, both canonical combining class 0 -- ENDED the word: "कोलकाता"
was indexed as "क" "लक" "त", "พัทยา" as "พ" "ทยา", and the 1-2 character
fragments matched whole prefix subtrees (70 leaves / 7.1 MB per keystroke for
พัทยา on southeast-asia).

Rule 2 (``"word_rule": 2``): marks continue a word and never start one.
Scripts without class-0 marks (Latin, Cyrillic, Greek, CJK, Hangul, pointed
Arabic and Hebrew after the fold) must come out byte-identical.
docs/search-prefix-locality.md#word-rule.
"""
from __future__ import annotations

import json
import os
import sys
import unicodedata

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cloud.search_shards import (  # noqa: E402
    WORD_RULE, Aggregator, char_split_paths, leaf_for, norm, paths_for,
    prefix_key, prefixes_for, record_paths, word_rule_of, words,
)


def w2(name):
    return words(norm(name), 2)


def w1(name):
    return words(norm(name), 1)


# ---- rule 2: whole words in scripts with class-0 marks --------------------
WHOLE = [
    # (name, the words rule 2 must produce)
    ("कोलकाता", ["कोलकाता"]),                       # Devanagari matras (Mc)
    ("नई दिल्ली", ["नई", "दिलली"]),                  # virama (ccc 9) folded away
    ("मुंबई", ["मुंबई"]),                             # anusvara Mn ccc 0
    ("เชียงใหม่", ["เชียงใหม"]),                      # Thai; tone mark (ccc 107) folded
    ("พัทยา", ["พัทยา"]),                           # U+0E31 MAI HAN-AKAT, ccc 0
    ("กรุงเทพมหานคร", ["กรงเทพมหานคร"]),            # sara u (ccc 103) folded
    ("วัดพระแก้ว", ["วัดพระแกว"]),
    ("சென்னை", ["செனனை"]),                          # Tamil: vowel signs kept
    ("தமிழ்நாடு", ["தமிழநாடு"]),
    ("কলকাতা", ["কলকাতা"]),                         # Bengali
    ("ঢাকা", ["ঢাকা"]),
    ("කොළඹ", ["කොළඹ"]),                             # Sinhala
    ("ភ្នំពេញ", ["ភនំពេញ"]),                          # Khmer; coeng (ccc 9) folded
    ("ရန်ကုန်", ["ရနကုန"]),                           # Myanmar; asat (ccc 9) folded
    ("ວຽງຈັນ", ["ວຽງຈັນ"]),                           # Lao
    ("ལྷ་ས", ["ལྷ"]),                                # Tibetan: tsheg separates; "ས" 1 char
]


@pytest.mark.parametrize("name,want", WHOLE)
def test_rule2_keeps_class0_marks_inside_the_word(name, want):
    got = [w for w in w2(name) if len(w) >= 2]
    assert got == [norm(x) for x in want]
    # ... and every character the fold kept is in some word: nothing of the
    # name is lost to a split at a vowel sign.
    nn = norm(name)
    assert "".join(w2(name)) == "".join(
        c for c in nn if c.isalnum() or unicodedata.category(c)[0] == "M")


# Names whose only marks the fold already drops (sara u in กรุงเทพ is ccc
# 103): rule 1 kept them whole too.
NOT_FRAGMENTED = ("กรุงเทพมหานคร",)


@pytest.mark.parametrize("name", [n for n, _ in WHOLE if n not in NOT_FRAGMENTED])
def test_rule1_fragmented_these_names(name):
    # The bug rule 2 fixes, pinned so the docs' description stays true.
    assert w1(name) != w2(name)


@pytest.mark.parametrize("name", NOT_FRAGMENTED)
def test_names_without_class0_marks_were_already_whole(name):
    assert w1(name) == w2(name)


def test_the_measured_thai_and_hindi_fragments():
    assert w1("कोलकाता") == ["क", "लक", "त"]
    assert w1("เชียงใหม่") == ["เช", "ยงใหม"]
    assert w1("พัทยา") == ["พ", "ทยา"]
    assert prefixes_for("พัทยา", 1) == {"ue1e", "ue17"}
    assert prefixes_for("พัทยา", 2) == {"ue1e"}
    assert prefixes_for("ถนน พัทยา", 1) == {"ue16", "ue17"}   # "พ" alone: no key
    assert prefixes_for("ถนน พัทยา", 2) == {"ue16", "ue1e"}


def test_a_mark_never_starts_a_word():
    # After a space (a stray vowel sign), at the start of the name, and a
    # run of marks alone: the word starts at the first alphanumeric.
    assert w2("ัabc") == ["abc"]
    assert w2("x ัิก") == ["x", "ก"]
    assert w2("ाि") == []
    assert w2("का-ाख") == ["का", "ख"]


def test_underscore_and_punctuation_still_end_words():
    assert w2("a_b") == ["a", "b"]
    assert w2("foo-bar.baz") == ["foo", "bar", "baz"]
    assert w2("क_ाख") == ["क", "ख"]


def test_digits_of_every_script_are_word_characters():
    assert w2("ซอย ๑๒") == ["ซอย", "๑๒"]
    assert w2("شارع ١٢٣") == ["شارع", "١٢٣"]
    assert w2("गली ४२") == ["गली", "४२"]
    assert w2("ª") == ["a"]                 # NFKD to "a"; Lo anyway


def test_rule2_word_characters_are_isalnum_or_marks_minus_underscore():
    # Per code point over the whole space -- the property the viewer's
    # /[\p{L}\p{M}\p{N}]/u mirrors (tests/search_word_rule_js.test.mjs).
    bad = []
    for c in range(0x110000):
        ch = chr(c)
        cat = unicodedata.category(ch)
        if cat in ("Cs",):
            continue
        want = (ch.isalnum() or cat[0] == "M") and ch != "_"
        got = words("x" + ch, 2) == ["x" + ch]
        if got != want:
            bad.append(hex(c))
    assert bad == []


# ---- scripts rule 2 must leave byte-identical -------------------------------
UNCHANGED = [
    "Café São Paulo", "Écouen", "Zürich Hauptbahnhof", "Łódź", "Ærøskøbing",
    "Straße", "İstanbul", "Ñuñoa", "Hà Nội", "Thành phố Hồ Chí Minh", "Đà Nẵng",
    "Rue de l'Église", "St. John's Wood", "1-12-1 Muramatsu", "45 Broadway",
    "Washington National Cathedral", "O'Hare Int'l", "ﬁnca", "①番地", "Ⅻ",
    "Москва", "Йошкар-Ола", "улица Ленина 12", "Αθήνα", "Θεσσαλονίκη", "ΟΔΟΣ ΕΡΜΟΥ",
    "東京都", "北京市朝阳区", "서울특별시", "빌딩", "ガソリンスタンド", "ぎんざ", "ｶﾞｿﾘﾝ",
    "القَاهِرَة", "مَكَّة المُكَرَّمَة", "شارع الملك فهد", "יְרוּשָׁלַיִם", "תֵּל אָבִיב",
    "Tbilisi თბილისი", "Երևան", "ኢትዮጵያ", "𠀀𠀁 astral", "a_b c",
]


@pytest.mark.parametrize("name", UNCHANGED)
def test_scripts_without_class0_marks_are_unchanged(name):
    nn = norm(name)
    assert words(nn, 2) == words(nn, 1)
    assert prefixes_for(name, 2) == prefixes_for(name, 1)
    for k in prefixes_for(name, 1):
        for depth in (1, 2, 3, 4):
            assert paths_for(k, name, depth, 2) == paths_for(k, name, depth, 1)


def test_default_rule_is_2_and_is_what_the_planner_uses():
    assert WORD_RULE == 2
    rec = {"n": "พัทยา", "t": "place"}
    assert record_paths("ue1e", rec, 4) == record_paths("ue1e", rec, 4, 2)
    assert record_paths("ue1e", rec, 4) == {("ue31", "ue17", "ue22", "ue32")}


def test_aggregator_and_leaf_for_follow_their_rule():
    # A rule-1 prefix planned with rule 1 places every record; planning the
    # same prefix with rule 2 strands the ones only rule 1 put there --
    # which is why a retrofit must keep the source's rule.
    recs = [{"n": "พัทยา", "t": "place"}, {"n": "ถนน พัทยา", "t": "street"},
            {"n": "ทยาลัย", "t": "poi"}]
    for rule in (1, 2):
        agg = Aggregator("ue17", rule=rule)
        mine = [r for r in recs if "ue17" in prefixes_for(r["n"], rule)]
        for r in mine:
            agg.add(r, 10)
        planned = agg.leaves(target_bytes=1)
        by_tier: dict = {}
        for t, p, _c, _b in planned:
            by_tier.setdefault(t, set()).add(p)
        from cloud.search_shards import tier_for
        for r in mine:
            assert list(leaf_for("ue17", r, by_tier.get(tier_for(r), ()), rule)), (rule, r)
    # Rule 1 put พัทยา in ue17 through the "ทยา" fragment; under rule 2 it
    # has no word there, only the whole-name fallback.
    assert record_paths("ue17", recs[0], 4, 1) == {("ue22", "ue32", "_e")}
    assert record_paths("ue17", recs[0], 4, 2) == {("_e",)}


def test_word_rule_of_a_manifest():
    assert word_rule_of({}) == 1
    assert word_rule_of(None) == 1
    assert word_rule_of({"chunks": {}}) == 1
    assert word_rule_of({"word_rule": 2}) == 2
    with pytest.raises(ValueError):
        word_rule_of({"word_rule": 3})


def test_prefix_key_is_rule_independent():
    # Only the split changed; a word's key is computed as before.
    assert prefix_key("พัทยา") == "ue1e"
    assert prefix_key("cafe") == "ca"


# ---- the manifest records the rule ------------------------------------------

class _Item:
    def __init__(self, path, title, mime, data, compress=True, **_kw):
        self.path, self.data = path, data


class _Creator:
    def __init__(self):
        self.items = {}

    def add_item(self, it):
        self.items[it.path] = it.data


def _emit(tmp_path, records, *, split_mb=10, extra=None):
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
        chunk_counts=counts, total_features=len(records), manifest_extra=extra)
    return c.items


def test_writer_manifest_records_word_rule_2(tmp_path):
    items = _emit(tmp_path, [{"n": "พัทยา", "t": "place"}, {"n": "Cafe", "t": "poi"}])
    m = json.loads(items["search-data/manifest.json"])
    assert m["word_rule"] == 2
    assert m["total"] == 2
    assert set(m["chunks"]) == {"ue1e", "ca"}


def test_manifest_extra_is_kept_but_cannot_override(tmp_path):
    items = _emit(tmp_path, [{"n": "Cafe", "t": "poi"}],
                  extra={"addresses_stripped": True, "word_rule": 1, "total": 99})
    m = json.loads(items["search-data/manifest.json"])
    assert m["addresses_stripped"] is True
    assert m["word_rule"] == 2 and m["total"] == 1


def test_in_memory_writer_records_word_rule_too():
    zim_writer = pytest.importorskip("streetzim.zim_writer")
    c = _Creator()
    zim_writer._add_search_in_memory(
        c, _Item, search_features=[{"name": "Cafe", "type": "poi", "lat": 1.0, "lon": 2.0}],
        loc_lookup=None, page_types=())
    assert json.loads(c.items["search-data/manifest.json"])["word_rule"] == 2


def test_split_prefix_char_split_follows_rule_2(tmp_path):
    # A hot Thai prefix: its char_split paths start with the vowel sign
    # (u0e31) that rule 1 cut the word at.
    recs = [{"n": f"พัทยา {i}", "t": "addr", "s": "x" * 200} for i in range(400)]
    recs += [{"n": "พัทยา", "t": "place"}]
    items = _emit(tmp_path, recs, split_mb=0.01)
    m = json.loads(items["search-data/manifest.json"])
    assert "ue1e" in m["char_split"]
    assert all(p.split("~")[0] == "ue31" for p in m["char_split"]["ue1e"])
    assert char_split_paths([("c", ("ue31",), 1, 1)]) == ["ue31"]


# ---- the retrofit (cloud/swap_viewer_rust.py --rebuild-search) --------------

def _svr():
    pytest.importorskip("libzim")
    pytest.importorskip("streetzim.zim_writer")
    os.environ.setdefault("STREETZIM_ROOT", ROOT)
    import importlib
    return importlib.import_module("cloud.swap_viewer_rust")


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
