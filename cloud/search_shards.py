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

The normalisation here MUST match ``create_osm_zim.py`` (``_norm``,
``_word_re``, ``_prefix_key``) and the viewers' ``keyFor``/``normalizeText``;
if it drifts, readers ask for leaves the writer never wrote.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Iterator

Path = tuple[str, ...]

# Tier -> the record ``t`` values it holds. Order matters: clients fetch
# tier "c" in full first (it carries the city/place records scoreResults
# ranks with placeSubBonus), then "p", then "s", and "a" only for a digit
# query of >= 4 characters.
TIER_TYPES: dict[str, frozenset[str]] = {
    "c": frozenset({"place", "airport", "peak", "park", "water"}),
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

_word_re = re.compile(r"[^\W_]+", re.UNICODE)


def norm(s: str) -> str:
    """``create_osm_zim.py`` ``_norm``: accent-fold, then lowercase."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def _ascii_norm(ch: str) -> str:
    return ch if ch.isalnum() or ch == "_" else "_"


def prefix_key(word: str) -> str:
    """``create_osm_zim.py`` ``_prefix_key``: the 2-char chunk key."""
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


def tier_for(record: dict) -> str:
    t = (record or {}).get("t") or ""
    for tier, types in TIER_TYPES.items():
        if t in types:
            return tier
    return DEFAULT_TIER


def token_for(ch: str) -> str:
    """One path token: an ASCII character, or ``u<hex>`` of its code point."""
    return _ascii_norm(ch) if ch.isascii() else "u" + format(ord(ch), "x")


def paths_for(prefix: str, name: str, depth: int) -> set[Path]:
    """Every character path ``name`` should be indexed under, within ``prefix``.

    A record reaches a prefix through any word whose ``prefix_key`` matches,
    and through the whole name's first two characters. Each contributing word
    yields one path — the tokens of its characters after the prefix, at most
    ``depth`` of them. A word that IS the prefix yields ``("_",)``.
    """
    nn = norm(name)
    paths: set[Path] = set()
    candidates: list[str] = [m.group(0) for m in _word_re.finditer(nn)]
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
    return paths or {(TERMINAL,)}


class Aggregator:
    """Counts bytes per (tier, path) so leaves can be chosen without keeping
    records. Feed every record once; memory is O(distinct paths)."""

    def __init__(self, prefix: str, max_depth: int | None = None) -> None:
        self.prefix = prefix
        self.max_depth = max_depth or max(TIER_MAX_DEPTH.values())
        self.counts: dict[tuple[str, Path], list[int]] = {}

    def add(self, record: dict, size: int) -> None:
        tier = tier_for(record)
        depth = min(TIER_MAX_DEPTH.get(tier, 1), self.max_depth)
        for path in paths_for(self.prefix, record.get("n") or "", depth):
            for d in range(1, len(path) + 1):
                key = (tier, path[:d])
                slot = self.counts.get(key)
                if slot is None:
                    self.counts[key] = [1, size]
                else:
                    slot[0] += 1
                    slot[1] += size

    def leaves(self, target_bytes: int = SHARD_TARGET_BYTES
               ) -> list[tuple[str, Path, int, int]]:
        """``(tier, path, count, bytes)`` per leaf: a node becomes a leaf once
        it fits ``target_bytes``, has no children, or hits its tier's cap."""
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
            stack = [(p, 1) for p in sorted(roots.get(tier, []), reverse=True)]
            while stack:
                path, depth = stack.pop()
                count, size = self.counts[(tier, path)]
                kids = children.get((tier, path))
                if size <= target_bytes or depth >= cap or not kids:
                    out.append((tier, path, count, size))
                    continue
                stack.extend((k, depth + 1) for k in sorted(kids, reverse=True))
        return out


def leaf_name(prefix: str, path: Path, tier: str) -> str:
    return LEAF_SEP.join((prefix, *path, tier))


def leaf_for(prefix: str, record: dict,
             planned_paths: Iterable[Path]) -> Iterator[str]:
    """Leaf names a record belongs in, given the planned paths for its tier."""
    tier = tier_for(record)
    depth = TIER_MAX_DEPTH.get(tier, 1)
    planned = set(planned_paths)
    emitted: set[str] = set()
    for path in paths_for(prefix, record.get("n") or "", depth):
        for d in range(len(path), 0, -1):
            cand = path[:d]
            if cand in planned:
                name = leaf_name(prefix, cand, tier)
                if name not in emitted:
                    emitted.add(name)
                    yield name
                break


def char_split_paths(leaves: Iterable[tuple[str, Path, int, int]]) -> list[str]:
    """The manifest's ``char_split[prefix]``: the distinct paths as strings,
    sorted. Clients pick the longest matching what was typed; a typed
    character with no path means no records, so they say so at once."""
    return sorted({LEAF_SEP.join(path) for _tier, path, _c, _b in leaves})
