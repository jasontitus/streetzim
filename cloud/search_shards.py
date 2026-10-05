"""Character-path + tier layout for search-data chunks.

See ``docs/search-prefix-locality.md``. A hot 2-character prefix used to be
fanned out by FNV hash of the record name, so a query had to fetch every leaf:
"Caracas" on south-america cost 736 files and 320 MB. Here a leaf is chosen by
the characters that follow the prefix in the word that put the record there,
and by record tier, so the same query reads one 2.6 MB file.

A path is a list of **tokens**, one per character after the prefix: an ASCII
character stands for itself, anything else becomes ``u<hex>`` of its code
point so leaf names stay ASCII. Tokens are joined with ``~`` only when naming
a leaf — ``ca~r~c``, ``ch~u1107~u1175~p`` — never to decide where a path
splits. (Deciding that from the presence of ``~`` exploded the single token
``u1107`` into ``u,1,1,0,7`` and dropped every Korean and Japanese name from
its leaf.)

A leaf that characters cannot divide (a million "Carrera 7"s) is hash-split by
``cloud/repackage_zim.py`` afterwards and keeps its ``-0…-f`` children.

The planner is two-pass and streaming: callers feed records once to an
:class:`Aggregator` (which keeps only per-path counters, not records), then ask
for the leaf set. Neither the build nor the retrofit ever holds a whole prefix
in memory — ``av`` on united-states is 2.93 GB.

The normalisation here is the writer's: ``streetzim/zim_writer.py`` imports
``norm`` and ``prefix_key`` as ``_norm`` and ``_prefix_key`` (and keeps its
own copy of ``_word_re``). It MUST match the viewers'
``keyFor``/``normalizeText``; if it drifts, readers ask for leaves the writer
never wrote.
"""
from __future__ import annotations

import bisect
import json
import re
import unicodedata
from collections.abc import Iterable, Iterator
from typing import Any

# One search record, as written to search-data/*.json (docs/search-records.md).
Record = dict[str, Any]

Path = tuple[str, ...]

# Tier -> the record ``t`` values it holds. Order matters: clients fetch
# tier "c" in full first (it carries the city/place records scoreResults
# ranks with placeSubBonus), then "p", then "s", and "a" only for a digit
# query of >= 4 characters.
TIER_TYPES: dict[str, frozenset[str]] = {
    "c": frozenset({"place", "airport", "peak", "park", "water", "admin"}),
    "s": frozenset({"street"}),
    "a": frozenset({"addr"}),
}
TIER_ORDER = ("c", "p", "s", "a")
DEFAULT_TIER = "p"  # poi, and any future ``t`` value

# Character depth per tier. Tier a is most of the leaf count and is read only
# for digit queries, so capping it keeps the manifest small; whatever is still
# oversized falls through to the hash split.
TIER_MAX_DEPTH = {"c": 4, "p": 4, "s": 3, "a": 2}
SHARD_TARGET_BYTES = 4 * 1024 * 1024
LEAF_SEP = "~"
# "the word ends here". Two characters, so ``token_for`` can never produce it
# (it emits one ASCII character, or ``u<hex>``). It used to be ``_``, which
# also stands for a non-alphanumeric character — and Japanese addresses supply
# both: the word "12" in "1-12-1 Muramatsu" ends at once, while the whole name
# "10-5 …" contributes a path starting with the hyphen's ``_``. Sharing one
# token let the planner split the node and strand every short word under it
# (476 k records on japan "10").
TERMINAL = "_e"

# --- Word rule: how a folded name splits into words ---------------------
# Rule 1 (every ZIM written before 2026-10, manifest without "word_rule"):
# a word is a run of alphanumerics, ``[^\W_]+``. Marks are not
# alphanumeric, so a mark the fold keeps (canonical combining class 0:
# Indic vowel signs, Thai vowels such as U+0E31 and U+0E34-0E37, Khmer and
# Myanmar vowel signs) ENDED the word: "कोलकाता" became "क", "लक", "त" and
# "พัทยา" became "พ", "ทยา". The 1-2 character fragments matched whole
# prefix subtrees, and the name itself was under no key a reader of the
# whole word would compute.
#
# Rule 2 (manifest "word_rule": 2): a word is a maximal run of characters
# that are alphanumeric (str.isalnum) or a mark (category Mn, Mc, Me),
# never "_". Marks CONTINUE a word but never START one: a mark at the start
# of a run (after a space, or a whole name that begins with a stray vowel
# sign) is skipped and the word begins at the first alphanumeric, so a run
# of marks alone is no word. Latin, Cyrillic, Greek, CJK, Hangul and
# pointed Arabic/Hebrew are unchanged: the fold has already removed every
# mark of class != 0, and their remaining class-0 marks are rare. The
# viewers mirror this with /[^\p{L}\p{M}\p{N}]+/u plus a leading-mark strip
# (SEARCH_SHARDS.words), which is the same set of characters as
# isalnum-or-mark-minus-"_" (tests/search_word_rule_js.test.mjs proves it per
# code point). docs/search-prefix-locality.md#word-rule.
WORD_RULE = 2          # what this writer records as manifest["word_rule"]
WORD_RULES = (1, 2)

# --- Grouped siblings: one leaf for many small children -----------------
# A split node used to give every child its own leaf. A Latin node has at
# most 38 children (a-z, 0-9, "_", the terminal); a CJK node has thousands,
# and most hold a handful of records: china 2026-09-20 rebuilt with rule 2
# shipped 180,032 leaves, 94 k of them with <= 5 records, and a 6.47 MB
# manifest that every page load downloads and parses (validate_zim caps it at
# 4 MB). So when a node is split, children of at most GROUP_MEMBER_BYTES that
# are consecutive in code-point order share one leaf of at most GROUP_BYTES,
# whose last path token is a RANGE: ``r<lo>.<hi>``, the first and last member
# code points in hex (``u5927~r4e00.4e8b~c``). A typed token whose code point
# is in [lo, hi] reads that leaf; the range never spans a sibling that has a
# leaf of its own (a big child, or one split further), and the terminal
# ``_e`` is never grouped. ASCII tokens group by their character's code point
# (``_`` is 0x5f). A range token cannot be mistaken for another token: those
# are one ASCII character, ``_e`` or ``u<hex>``, none with a ".".
#
# Readers that do not know ranges must not see them: a prefix whose plan has
# one is listed under the manifest's ``char_ranges`` instead of
# ``char_split`` (same shape). An older viewer finds no ``char_split`` entry
# for it and reads every leaf through ``sub_chunks`` -- slow, never wrong. A
# plan with no range stays in ``char_split``, byte for byte what the writer
# wrote before. docs/search-prefix-locality.md#grouped-siblings.
GROUP_BYTES = 256 * 1024
GROUP_MEMBER_BYTES = GROUP_BYTES // 2
RANGE_PREFIX = "r"
RANGE_SEP = "."


def _mark_class() -> str:
    """A regex character class body listing every mark (Mn/Mc/Me) in this
    Python's Unicode database, as ranges."""
    ranges: list[list[int]] = []
    for c in range(0x110000):
        if unicodedata.category(chr(c))[0] == "M":
            if ranges and ranges[-1][1] == c - 1:
                ranges[-1][1] = c
            else:
                ranges.append([c, c])
    return "".join(f"\\U{a:08x}" if a == b else f"\\U{a:08x}-\\U{b:08x}"
                   for a, b in ranges)


_WORD_RES = {
    1: re.compile(r"[^\W_]+", re.UNICODE),
    # An alphanumeric, then alphanumerics or marks. (Marks and alphanumerics
    # are disjoint sets, so this never splits a run the char-class view
    # would keep whole.)
    2: re.compile(r"[^\W_](?:[^\W_]|[" + _mark_class() + r"])*", re.UNICODE),
}
# Rule 1's pattern under its historical name (tests and tools import it).
_word_re = _WORD_RES[1]


def word_rule_of(manifest: dict[str, Any] | None) -> int:
    """The word rule a search manifest was written with: its ``word_rule``,
    or 1 when the key is absent (every ZIM written before rule 2)."""
    r = (manifest or {}).get("word_rule", 1)
    if r not in WORD_RULES:
        raise ValueError(f"search manifest has unknown word_rule {r!r}")
    return r


def words(nn: str, rule: int = WORD_RULE) -> list[str]:
    """The words of an already-folded (``norm``) text under ``rule``."""
    return _WORD_RES[rule].findall(nn)


def prefixes_for(name: str, rule: int = WORD_RULE) -> set[str]:
    """Every 2-char chunk key a name is indexed under: the whole name's
    first two characters, plus the key of each word of 2+ characters.

    Keys come from the NORMALISED name (accent-folded, lowercased) so they
    agree with what the readers compute from a normalised query. Splitting
    the raw name let a combining accent (NFD "Écouen") cut the word so the
    key was "e_" while every reader asked for "ec".
    """
    nn = norm(name)
    # First-2-of-whole-name: "45 Broadway" -> "45".
    keys = {prefix_key(nn[:2])}
    # One key per word: "cathedral" finds "Washington National Cathedral".
    for m in words(nn, rule):
        if len(m) >= 2:
            keys.add(prefix_key(m))
    return keys


def norm(s: str) -> str:
    """The writer's ``_norm``: accent-fold, then lowercase.

    Drops only characters of canonical combining class != 0 -- not every
    mark: Indic vowel signs and Thai vowels stay. Every published index was
    folded with this, and the viewers mirror it exactly (SEARCH_SHARDS.fold,
    table from tools/gen_combining_marks.py), so it must not change.
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def _ascii_norm(ch: str) -> str:
    return ch if ch.isalnum() or ch == "_" else "_"


def prefix_key(word: str) -> str:
    """The writer's ``_prefix_key``: the 2-char chunk key."""
    pw = norm(word).replace(" ", "_")
    if not pw:
        return "__"
    c0 = pw[0]
    if not c0.isascii():
        return "u" + format(ord(c0), "x")
    k0 = _ascii_norm(c0)
    if len(pw) >= 2:
        c1 = pw[1]
        k1 = _ascii_norm(c1) if c1.isascii() else "_"
    else:
        k1 = "_"
    return k0 + k1


def tier_for(record: Record) -> str:
    t = (record or {}).get("t") or ""
    for tier, types in TIER_TYPES.items():
        if t in types:
            return tier
    return DEFAULT_TIER


def token_for(ch: str) -> str:
    """One path token: an ASCII character, or ``u<hex>`` of its code point."""
    return _ascii_norm(ch) if ch.isascii() else "u" + format(ord(ch), "x")


def token_cp(tok: str) -> int | None:
    """The code point a (non-range) token stands for: ``u<hex>`` its hex, an
    ASCII token its character; None for the terminal or anything else."""
    if len(tok) == 1:
        return ord(tok)
    if tok.startswith("u"):
        try:
            return int(tok[1:], 16)
        except ValueError:
            return None
    return None


def range_token(lo: int, hi: int) -> str:
    return f"{RANGE_PREFIX}{lo:x}{RANGE_SEP}{hi:x}"


# Exactly what range_token writes: lowercase hex, no sign, "0x", "_" or
# space (int(x, 16) would take all of those; the viewers' RANGE_RE is the
# same pattern, so both sides agree on what is a range).
RANGE_RE = re.compile(r"r([0-9a-f]+)\.([0-9a-f]+)")


def range_bounds(tok: str) -> tuple[int, int] | None:
    """``(lo, hi)`` of a range token, or None when ``tok`` is not one."""
    m = RANGE_RE.fullmatch(tok)
    if m is None:
        return None
    return int(m.group(1), 16), int(m.group(2), 16)


def token_matches(declared: str, typed: str) -> bool:
    """Whether a declared path token covers a typed (concrete) token."""
    if declared == typed:
        return True
    b = range_bounds(declared)
    if b is None:
        return False
    c = token_cp(typed)
    return c is not None and b[0] <= c <= b[1]


def has_ranges(paths: Iterable[Path]) -> bool:
    return any(range_bounds(t) is not None for p in paths for t in p)


def paths_for(prefix: str, name: str, depth: int,
              rule: int = WORD_RULE) -> set[Path]:
    """Every character path ``name`` should be indexed under, within ``prefix``.

    A record reaches a prefix through any word whose ``prefix_key`` matches,
    and through the whole name's first two characters. Each contributing word
    yields one path — the tokens of its characters after the prefix, at most
    ``depth`` of them. A word that IS the prefix yields ``("_",)``.
    """
    return _word_paths(prefix, name, depth, rule) or {(TERMINAL,)}


def record_names(record: Record) -> list[str]:
    """Every name a record is indexed and matched under: ``n``, the name in
    its own script ``nn`` (when it differs: "北京大学" beside "Peking
    University"), the English / Latin name ``nl`` of a build in another
    language, and an administrative area's other names ``alt``. The
    viewers' recordNames is the same list (docs/search-records.md)."""
    names = [record.get("n") or ""]
    for k in ("nn", "nl"):
        v = record.get(k)
        if isinstance(v, str) and v:
            names.append(v)
    alt = record.get("alt")
    if isinstance(alt, list):
        names += [a for a in alt if isinstance(a, str)]  # pyright: ignore[reportUnknownVariableType]
    return names


def record_paths(prefix: str, record: Record, depth: int,
                 rule: int = WORD_RULE) -> set[Path]:
    """``paths_for`` over every name of the record (``record_names``)."""
    paths: set[Path] = set()
    for nm in record_names(record):
        paths |= _word_paths(prefix, nm, depth, rule)
    return paths or {(TERMINAL,)}


def _word_paths(prefix: str, name: str, depth: int,
                rule: int = WORD_RULE) -> set[Path]:
    nn = norm(name)
    paths: set[Path] = set()
    candidates: list[str] = words(nn, rule)
    whole = nn.replace(" ", "_")
    if whole:
        candidates.append(whole)
    for w in candidates:
        if len(w) < 2 or prefix_key(w) != prefix:
            continue
        # A ``u<hex>`` prefix consumed one character, an ASCII one consumed two.
        rest = list(w)[1:] if prefix.startswith("u") else list(w)[2:]
        toks = [token_for(c) for c in rest[:depth]]
        # A word that ends before ``depth`` gets the terminal token, so it
        # still has a leaf when its node is split: "Gim" is ("m", "_e") beside
        # ("m", "p") for "Gimpo". Without it every short word under a split
        # node reached no leaf at all — 186 k records on korea-mongolia "gi".
        if len(toks) < depth:
            toks.append(TERMINAL)
        paths.add(tuple(toks))
    return paths


class Aggregator:
    """Counts bytes per (tier, path) so leaves can be chosen without keeping
    records. Feed every record once; memory is O(distinct paths)."""

    def __init__(self, prefix: str, max_depth: int | None = None,
                 rule: int = WORD_RULE) -> None:
        self.prefix = prefix
        # The word rule the prefix's records were bucketed with: a retrofit
        # keeps a source's prefixes, so it must keep the source's rule too.
        self.rule = rule
        self.max_depth = max_depth or max(TIER_MAX_DEPTH.values())
        self.counts: dict[tuple[str, Path], list[int]] = {}

    def add(self, record: Record, size: int) -> None:
        tier = tier_for(record)
        depth = min(TIER_MAX_DEPTH.get(tier, 1), self.max_depth)
        for path in record_paths(self.prefix, record, depth, self.rule):
            for d in range(1, len(path) + 1):
                key = (tier, path[:d])
                slot = self.counts.get(key)
                if slot is None:
                    self.counts[key] = [1, size]
                else:
                    slot[0] += 1
                    slot[1] += size

    def leaves(self, target_bytes: int = SHARD_TARGET_BYTES,
               group_bytes: int | None = None,
               ) -> list[tuple[str, Path, int, int]]:
        """``(tier, path, count, bytes)`` per leaf: a node becomes a leaf once
        it fits ``target_bytes``, has no children, or hits its tier's cap.

        The children of a split node (and the prefix's first level, which is
        the prefix split) that hold at most ``group_bytes // 2`` and are
        consecutive in code-point order share a leaf of at most
        ``group_bytes`` whose last token is a range (see GROUP_BYTES). A
        group of one keeps its own path. ``group_bytes=0``: no grouping, the
        layout of every ZIM written before 2026-10. None: GROUP_BYTES."""
        if group_bytes is None:
            group_bytes = GROUP_BYTES
        children: dict[tuple[str, Path], list[Path]] = {}
        roots: dict[str, list[Path]] = {}
        for tier, path in self.counts:
            if len(path) == 1:
                roots.setdefault(tier, []).append(path)
            else:
                children.setdefault((tier, path[:-1]), []).append(path)
        out: list[tuple[str, Path, int, int]] = []
        for tier in TIER_ORDER:
            cap = TIER_MAX_DEPTH.get(tier, 1)
            first = self._group(tier, sorted(roots.get(tier, [])),
                                group_bytes, target_bytes, out)
            stack = [(p, 1) for p in reversed(first)]
            while stack:
                path, depth = stack.pop()
                count, size = self.counts[(tier, path)]
                kids = children.get((tier, path))
                if size <= target_bytes or depth >= cap or not kids:
                    out.append((tier, path, count, size))
                    continue
                rest = self._group(tier, sorted(kids), group_bytes, target_bytes,
                                   out)
                stack.extend((k, depth + 1) for k in reversed(rest))
        return out

    def _group(self, tier: str, kids: list[Path], group_bytes: int,
               target_bytes: int, out: list[tuple[str, Path, int, int]]
               ) -> list[Path]:
        """Append the range leaves for ``kids`` (siblings) to ``out`` and
        return the kids that keep a path of their own. A group never
        outgrows ``target_bytes`` either, and only a kid that fits it (so
        would be a leaf anyway) joins one."""
        if group_bytes <= 0 or len(kids) < 2:
            return kids
        group_bytes = min(group_bytes, target_bytes)
        member_max = group_bytes // 2
        by_cp = sorted((c, k) for k in kids
                       if (c := token_cp(k[-1])) is not None)
        grouped: set[Path] = set()
        run: list[tuple[int, Path]] = []
        run_bytes = 0

        def flush() -> None:
            nonlocal run, run_bytes
            if len(run) >= 2:
                lo, hi = run[0][0], run[-1][0]
                parent = run[0][1][:-1]
                count = sum(self.counts[(tier, k)][0] for _c, k in run)
                size = sum(self.counts[(tier, k)][1] for _c, k in run)
                out.append((tier, parent + (range_token(lo, hi),), count, size))
                grouped.update(k for _c, k in run)
            run = []
            run_bytes = 0

        for c, k in by_cp:
            size = self.counts[(tier, k)][1]
            if size > member_max:
                # Has a leaf (or subtree) of its own: a range never spans it.
                flush()
                continue
            if run_bytes + size > group_bytes:
                flush()
            run.append((c, k))
            run_bytes += size
        flush()
        return [k for k in kids if k not in grouped]


class TierPlan:
    """One tier's planned paths, with its ranges indexed so a concrete path
    finds its leaf without scanning. Build once per prefix and tier."""

    def __init__(self, paths: Iterable[Path]) -> None:
        self.paths: set[Path] = set()
        self.ranges: dict[Path, list[tuple[int, int, str]]] = {}
        for p in paths:
            self.paths.add(p)
            b = range_bounds(p[-1]) if p else None
            if b is not None:
                self.ranges.setdefault(p[:-1], []).append((b[0], b[1], p[-1]))
        for v in self.ranges.values():
            v.sort()

    def __iter__(self) -> Iterator[Path]:
        return iter(self.paths)

    def __len__(self) -> int:
        return len(self.paths)

    def leaf_path(self, path: Path) -> Path | None:
        """The planned path holding concrete ``path``: the deepest exact
        prefix of it, or the range covering a token at that depth."""
        for d in range(len(path), 0, -1):
            cand = path[:d]
            if cand in self.paths:
                return cand
            rs = self.ranges.get(path[:d - 1])
            if rs:
                c = token_cp(path[d - 1])
                if c is not None:
                    i = bisect.bisect_right(rs, (c, 0x110000, "")) - 1
                    if i >= 0 and rs[i][0] <= c <= rs[i][1]:
                        return path[:d - 1] + (rs[i][2],)
        return None


def plan_by_tier(planned: Iterable[tuple[str, Path, int, int]]
                 ) -> dict[str, TierPlan]:
    """``Aggregator.leaves`` output as one :class:`TierPlan` per tier."""
    by: dict[str, list[Path]] = {}
    for tier, path, _c, _b in planned:
        by.setdefault(tier, []).append(path)
    return {t: TierPlan(ps) for t, ps in by.items()}


def split_key(planned: Iterable[tuple[str, Path, int, int]]) -> str:
    """The manifest key a prefix's paths go under: ``char_ranges`` when its
    plan has a range (readers that predate ranges must not see it),
    ``char_split`` otherwise."""
    return ("char_ranges" if has_ranges(p for _t, p, _c, _b in planned)
            else "char_split")


def leaf_name(prefix: str, path: Path, tier: str) -> str:
    return LEAF_SEP.join((prefix, *path, tier))


def leaf_for(prefix: str, record: Record,
             planned_paths: Iterable[Path],
             rule: int = WORD_RULE) -> Iterator[str]:
    """Leaf names a record belongs in, given the planned paths for its tier
    (a :class:`TierPlan`, or any iterable of paths)."""
    tier = tier_for(record)
    depth = TIER_MAX_DEPTH.get(tier, 1)
    plan = planned_paths if isinstance(planned_paths, TierPlan) \
        else TierPlan(planned_paths)
    emitted: set[str] = set()
    for path in record_paths(prefix, record, depth, rule):
        cand = plan.leaf_path(path)
        if cand is not None:
            name = leaf_name(prefix, cand, tier)
            if name not in emitted:
                emitted.add(name)
                yield name


def char_split_paths(leaves: Iterable[tuple[str, Path, int, int]]) -> list[str]:
    """The manifest's ``char_split[prefix]``: the distinct paths as strings,
    sorted. Clients pick the longest matching what was typed; a typed
    character with no path means no records, so they say so at once."""
    return sorted({LEAF_SEP.join(path) for _tier, path, _c, _b in leaves})


# --- Hash splitting of oversized chunks (--split-hot-search-chunks-mb) ---
# Moved from cloud/repackage_zim.py so the builder and repackage share one
# copy without importing each other.

def sub_bucket_for_name(name: str, n_buckets: int) -> int:
    """Deterministic, language-agnostic hash mapping a record's name to
    one of ``n_buckets`` sub-chunks. Swift (``Geocoder.subBucketFor``)
    must compute the same bucket. The web viewer does not compute it: it
    fetches every sub-chunk the manifest's ``sub_chunks`` lists for a
    prefix and filters by the query (resources/viewer/src/index/300-search.js).

    Uses FNV-1a 32-bit hash over the UTF-8 bytes of the full name —
    cheap, no external deps, and reproducible across Python / JS / Swift
    to the bit.
    """
    h = 0x811C9DC5  # FNV offset basis (32-bit)
    for b in name.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF  # FNV prime
    return h % n_buckets


def split_records_recursive(
    records: list[Record], prefix: str, threshold_bytes: int,
    n_buckets: int, max_depth: int,
) -> list[tuple[str, bytes, int]]:
    """Recursively split records into sub-chunks until each fits under
    ``threshold_bytes`` or ``max_depth`` is reached. Returns a list of
    ``(leaf_prefix, serialized_bytes, record_count)`` tuples — only leaves, no
    intermediate nodes.

    Strategy:
      1. Try FNV-1a hash by record's ``n`` field across ``n_buckets``.
      2. If degenerate (≥75% of records collapsed into one bucket — happens
         when records share an empty/identical name), fall back to
         **size-based slicing**: distribute records by index modulo
         n_buckets. Client behavior is unaffected because the client
         already fetches every sub-chunk under a prefix and filters by
         query content (see ``expandPrefix`` in resources/viewer/src/index/300-search.js).

    The serialized payload is checked against ``threshold_bytes`` before
    splitting — if the chunk is already small enough (or recursion is
    exhausted), it's returned as a leaf.
    """
    serialized = json.dumps(records, separators=(",", ":"),
                            ensure_ascii=False).encode("utf-8")
    if len(serialized) <= threshold_bytes or max_depth <= 0 or len(records) <= 1:
        return [(prefix, serialized, len(records))]

    # By-name FNV-1a bucketing (deterministic; preferred when distribution
    # is reasonable).
    by_name: list[list[Record]] = [[] for _ in range(n_buckets)]
    for rec in records:
        name = rec.get("n", "") or ""
        by_name[sub_bucket_for_name(name, n_buckets)].append(rec)
    largest_share = max(len(b) for b in by_name) / max(1, len(records))

    if largest_share <= 0.75:
        buckets = by_name
        strategy = "name"
    else:
        # Degenerate — anonymous records (empty `n`) or 1.5M records sharing
        # the same `n`. Split by record index instead, breaking the FNV tie.
        buckets: list[list[Record]] = [[] for _ in range(n_buckets)]
        for i, rec in enumerate(records):
            buckets[i % n_buckets].append(rec)
        strategy = "index"

    out: list[tuple[str, bytes, int]] = []
    hex_width = len(format(n_buckets - 1, "x"))
    for i, bucket in enumerate(buckets):
        if not bucket:
            continue
        sub_prefix = f"{prefix}-{format(i, f'0{hex_width}x')}"
        out.extend(split_records_recursive(
            bucket, sub_prefix, threshold_bytes, n_buckets, max_depth - 1,
        ))
    if strategy == "index":
        # Surfaced for log diagnostics; the actual recursion already wrote
        # uniform sub-chunks. (No extra effect — just informational.)
        pass
    return out
