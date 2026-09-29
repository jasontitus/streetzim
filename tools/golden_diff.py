#!/usr/bin/env python3
"""Compare two ZIMs entry by entry, to show a change to the builder changed
no output (docs/golden-builds.md).

    python tools/golden_diff.py before.zim after.zim [--control before2.zim]
                                [--decode-tiles] [--coord-tolerance DEG] [--show N]

Every entry (content, metadata and libzim's own listings and indexes) is
compared by path, MIME type and content. Each entry lands in one class:

  identical     same bytes;
  volatile      expected to differ between any two builds: the `Date`
                metadata, the Xapian indexes (libzim does not write them
                reproducibly), and dates inside map-config.json and
                streetzim-meta.json;
  reordered     a JSON list with the same items in another order, or a
                Kiwix search page (search/*.html) whose content moved to
                another page number;
  tiles-equal   (--decode-tiles) a vector tile with the same features in
                another order, as tilemaker writes them;
  moved         (--coord-tolerance) a JSON list of search records that are
                the same apart from coordinates ("a", "o") that moved by at
                most DEG degrees, as when tilemaker cuts a street differently;
  noise         (--control) also differs between before.zim and the
                control, a second build of the same code and inputs;
  changed, only-before, only-after
                real differences.

Exit status 0 when nothing is changed, only-before or only-after; 1
otherwise; 2 on a usage error. The archive UUIDs always differ and are
printed for reference only.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

FAIL = ("changed", "only-before", "only-after")
ORDER = ("identical", "volatile", "reordered", "tiles-equal", "moved", "noise", *FAIL)
DATE_RE = re.compile(rb"20\d\d[-/]\d\d(?:[-/]\d\d)?(?:[T ][0-9:.]+(?:Z|[+-]\d\d:?\d\d)?)?")
DATED_JSON = {"map-config.json", "streetzim-meta.json"}
SEARCH_PAGE = re.compile(r"^search/.*\.html$")


class Entry:
    __slots__ = ("data", "mime", "redirect")

    def __init__(self, mime: str, data: bytes, redirect: str | None = None) -> None:
        self.mime, self.data, self.redirect = mime, data, redirect

    def key(self) -> tuple[str, str | None, bytes]:
        return (self.mime, self.redirect, self.data)


def read_zim(path: str) -> tuple[dict[str, Entry], set[str], str]:
    """All entries by path, the metadata keys, and the archive UUID."""
    from libzim.reader import Archive
    a = Archive(Path(path))
    n = getattr(a, "all_entry_count", a.entry_count)
    out: dict[str, Entry] = {}
    for i in range(n):
        e = a._get_entry_by_id(i)
        if e.is_redirect:
            out[e.path] = Entry("", b"", e.get_redirect_entry().path)
            continue
        it = e.get_item()
        out[e.path] = Entry(it.mimetype, bytes(it.content))
    return out, set(a.metadata_keys), str(a.uuid)


def _sorted_items(items: Iterable[Any]) -> list[str]:
    return sorted(json.dumps(x, sort_keys=True) for x in items)


def _json_class(a: bytes, b: bytes, coord_tol: float) -> str | None:
    """Return "reordered" or "moved" (see the module docstring) for two JSON lists,
    else None."""
    try:
        ja, jb = json.loads(a), json.loads(b)
    except ValueError:
        return None
    if not (isinstance(ja, list) and isinstance(jb, list)):
        return None
    if _sorted_items(ja) == _sorted_items(jb):
        return "reordered"
    if coord_tol > 0 and _records_near(ja, jb, coord_tol):
        return "moved"
    return None


def _records_near(ja: list[Any], jb: list[Any], tol: float) -> bool:
    """Same records apart from their coordinates, each within `tol` degrees
    (records with equal other fields are paired in coordinate order)."""
    def groups(items: list[Any]) -> dict[str, list[tuple[float, float]]] | None:
        out: dict[str, list[tuple[float, float]]] = {}
        for r in items:
            if not (isinstance(r, dict) and "a" in r and "o" in r):
                return None
            rest = {k: v for k, v in r.items() if k not in ("a", "o")}
            out.setdefault(json.dumps(rest, sort_keys=True), []).append((r["a"], r["o"]))
        return out
    ga, gb = groups(ja), groups(jb)
    if ga is None or gb is None or ga.keys() != gb.keys():
        return False
    for key, pa in ga.items():
        pb = gb[key]
        if len(pa) != len(pb):
            return False
        for (lat1, lon1), (lat2, lon2) in zip(sorted(pa), sorted(pb)):
            if abs(lat1 - lat2) > tol or abs(lon1 - lon2) > tol:
                return False
    return True


def _tile_features(data: bytes) -> list[str]:
    import mapbox_vector_tile
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    decoded = mapbox_vector_tile.decode(data)
    return sorted(json.dumps([name, f], sort_keys=True)
                  for name, layer in decoded.items() for f in layer["features"])


def _is_volatile(path: str, a: Entry, b: Entry, meta: set[str]) -> bool:
    if path == "Date" and path in meta:
        return True
    if "xapian" in a.mime and "xapian" in b.mime:
        return True
    if path in DATED_JSON:
        return DATE_RE.sub(b"DATE", a.data) == DATE_RE.sub(b"DATE", b.data)
    return False


def classify(before: dict[str, Entry], after: dict[str, Entry], meta: set[str],
             decode_tiles: bool = False,
             control: dict[str, Entry] | None = None,
             coord_tol: float = 0.0) -> dict[str, list[str]]:
    """Map each class name to the sorted paths in it."""
    classes: dict[str, list[str]] = {k: [] for k in ORDER}
    classes["only-before"] = sorted(set(before) - set(after))
    classes["only-after"] = sorted(set(after) - set(before))
    differing: list[str] = []
    for path in sorted(set(before) & set(after)):
        a, b = before[path], after[path]
        if a.key() == b.key():
            classes["identical"].append(path)
        elif a.mime != b.mime or a.redirect != b.redirect:
            differing.append(path)
        elif _is_volatile(path, a, b, meta):
            classes["volatile"].append(path)
        elif a.mime == "application/json" and (kind := _json_class(a.data, b.data, coord_tol)):
            classes[kind].append(path)
        elif decode_tiles and a.mime == "application/x-protobuf" \
                and _tile_features(a.data) == _tile_features(b.data):
            classes["tiles-equal"].append(path)
        else:
            differing.append(path)

    # Kiwix search pages: which page number a group of results gets can
    # change between runs, so compare the differing pages as a multiset.
    pages = [p for p in differing if SEARCH_PAGE.match(p)]
    if pages and Counter(before[p].data for p in pages) == Counter(after[p].data for p in pages):
        classes["reordered"].extend(pages)
        renumbered = set(pages)
        differing = [p for p in differing if p not in renumbered]

    for path in differing:
        c = control.get(path) if control is not None else None
        if c is not None and c.key() != before[path].key():
            classes["noise"].append(path)
        else:
            classes["changed"].append(path)
    for k in ("reordered", "noise", "changed"):
        classes[k].sort()
    return classes


def report(classes: dict[str, list[str]], show: int,
           out: Callable[[str], None] = print) -> bool:
    """Print the summary; True when there is no real difference."""
    for k in ORDER:
        out(f"  {k:<12} {len(classes[k]):>7,d}")
    for k in ("volatile", "reordered", "tiles-equal", "moved", "noise", *FAIL):
        paths = classes[k]
        if paths and show:
            out(f"\n{k}:")
            for p in paths[:show]:
                out(f"  {p}")
            if len(paths) > show:
                out(f"  ... and {len(paths) - show:,d} more")
    ok = not any(classes[k] for k in FAIL)
    out("\nRESULT: " + ("no differences beyond the expected ones" if ok
                        else "DIFFERENT (see changed / only-before / only-after)"))
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--control", help="a second build of `before` (same code, same inputs); "
                                      "entries that differ there too are reported as noise")
    ap.add_argument("--decode-tiles", action="store_true",
                    help="compare differing vector tiles by their decoded features")
    ap.add_argument("--coord-tolerance", type=float, default=0.0, metavar="DEG",
                    help="accept search records whose coordinates moved by at most DEG "
                         "degrees (for tilemaker builds; default 0: exact)")
    ap.add_argument("--show", type=int, default=20, help="paths to list per class (default 20)")
    args = ap.parse_args(argv)

    before, meta, uuid_a = read_zim(args.before)
    after, meta_b, uuid_b = read_zim(args.after)
    control = read_zim(args.control)[0] if args.control else None
    print(f"before {args.before}: {len(before):,d} entries, uuid {uuid_a}")
    print(f"after  {args.after}: {len(after):,d} entries, uuid {uuid_b}")
    if control is not None:
        print(f"control {args.control}: {len(control):,d} entries")
    classes = classify(before, after, meta | meta_b, args.decode_tiles, control,
                       args.coord_tolerance)
    return 0 if report(classes, args.show) else 1


if __name__ == "__main__":
    sys.exit(main())
