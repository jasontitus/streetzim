#!/usr/bin/env python3
"""Compare two ZIMs entry by entry, to show a change to the builder changed
no output (docs/golden-builds.md).

    python tools/golden_diff.py before.zim after.zim [--control before2.zim]
                                [--decode-tiles] [--coord-tolerance DEG] [--show N]

Every entry (content, metadata and libzim's own listings and indexes) is
compared by path, title, MIME type and content. Each entry lands in one
class:

  identical     same bytes;
  volatile      expected to differ between any two builds: the `Date`
                metadata, the Xapian indexes (libzim does not write them
                reproducibly), and the `buildDate` key of map-config.json
                and streetzim-meta.json;
  reordered     a list of search records (JSON objects with "t" and "n")
                with the same records, whose (type, name) sequence -- the
                order the builder sorts them in, and the order the viewer
                shows them in -- is unchanged: only records that tie on
                that key swapped places. Also a Kiwix search page
                (search/*.html) whose content moved to another page number;
  tiles-equal   (--decode-tiles) a vector tile with the same features in
                another order, as tilemaker writes them;
  moved         (--coord-tolerance) as reordered, but records may also have
                coordinates ("a", "o") that moved by at most DEG degrees,
                as when tilemaker cuts a street differently. The largest
                shift accepted is printed;
  noise         (--control) differs from before.zim, and so does the
                control (a second build of the same code and inputs), and
                after.zim's entry equals the control's (or is equivalent to
                it by the rules above);
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
# The build date, and nothing else, may differ in these files.
DATED_JSON = {"map-config.json": "buildDate", "streetzim-meta.json": "buildDate"}
SEARCH_PAGE = re.compile(r"^search/.*\.html$")


class Entry:
    """One entry. A redirect has no item, so no MIME type or content: it is
    stored with mime "" and data b"" and compared by its target (and title)."""
    __slots__ = ("data", "mime", "redirect", "title")

    def __init__(self, mime: str, data: bytes, redirect: str | None = None,
                 title: str = "") -> None:
        self.mime, self.data, self.redirect, self.title = mime, data, redirect, title

    def key(self) -> tuple[str, str, str | None, bytes]:
        return (self.mime, self.title, self.redirect, self.data)


def read_zim(path: str) -> tuple[dict[str, Entry], set[str], str]:
    """All entries by path, the metadata keys, and the archive UUID."""
    from libzim.reader import Archive
    a = Archive(Path(path))
    n = getattr(a, "all_entry_count", a.entry_count)
    out: dict[str, Entry] = {}
    for i in range(n):
        e = a._get_entry_by_id(i)
        if e.is_redirect:
            out[e.path] = Entry("", b"", e.get_redirect_entry().path, e.title)
            continue
        it = e.get_item()
        out[e.path] = Entry(it.mimetype, bytes(it.content), None, e.title)
    return out, set(a.metadata_keys), str(a.uuid)


def _sorted_items(items: Iterable[Any]) -> list[str]:
    return sorted(json.dumps(x, sort_keys=True) for x in items)


def _sort_keys(items: list[Any]) -> list[tuple[Any, Any]] | None:
    """The (type, name) of each record, or None if the list is not all
    search records."""
    keys: list[tuple[Any, Any]] = []
    for r in items:
        if not (isinstance(r, dict) and "t" in r and "n" in r):
            return None
        keys.append((r["t"], r["n"]))
    return keys


def _json_class(a: bytes, b: bytes, coord_tol: float, shifts: list[float]) -> str | None:
    """Return "reordered" or "moved" (see the module docstring) for two JSON
    lists of search records, else None. Appends the largest coordinate shift
    of a "moved" list to `shifts`."""
    try:
        ja, jb = json.loads(a), json.loads(b)
    except ValueError:
        return None
    if not (isinstance(ja, list) and isinstance(jb, list)):
        return None
    ka = _sort_keys(ja)
    if ka is None or ka != _sort_keys(jb):
        return None           # not search records, or the displayed order changed
    if _sorted_items(ja) == _sorted_items(jb):
        return "reordered"
    if coord_tol > 0:
        shift = _records_shift(ja, jb)
        if shift is not None and shift <= coord_tol:
            shifts.append(shift)
            return "moved"
    return None


def _records_shift(ja: list[Any], jb: list[Any]) -> float | None:
    """The largest coordinate difference, in degrees, between records that
    are otherwise equal (paired in coordinate order), or None when the
    records differ in anything but their coordinates."""
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
        return None
    worst = 0.0
    for key, pa in ga.items():
        pb = gb[key]
        if len(pa) != len(pb):
            return None
        for (lat1, lon1), (lat2, lon2) in zip(sorted(pa), sorted(pb)):
            worst = max(worst, abs(lat1 - lat2), abs(lon1 - lon2))
    return worst


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
        try:
            ja, jb = json.loads(a.data), json.loads(b.data)
        except ValueError:
            return False
        if not (isinstance(ja, dict) and isinstance(jb, dict)):
            return False
        ja.pop(DATED_JSON[path], None)
        jb.pop(DATED_JSON[path], None)
        return ja == jb
    return False


def _equivalent(path: str, a: Entry, b: Entry, meta: set[str], decode_tiles: bool,
                coord_tol: float, shifts: list[float]) -> str | None:
    """The accepted class of a difference between `a` and `b`, or None."""
    if a.key() == b.key():
        return "identical"
    if a.mime != b.mime or a.redirect != b.redirect or a.title != b.title:
        return None
    if _is_volatile(path, a, b, meta):
        return "volatile"
    if a.mime == "application/json":
        return _json_class(a.data, b.data, coord_tol, shifts)
    if decode_tiles and a.mime == "application/x-protobuf" \
            and _tile_features(a.data) == _tile_features(b.data):
        return "tiles-equal"
    return None


def classify(before: dict[str, Entry], after: dict[str, Entry], meta: set[str],
             decode_tiles: bool = False,
             control: dict[str, Entry] | None = None,
             coord_tol: float = 0.0,
             shifts: list[float] | None = None) -> dict[str, list[str]]:
    """Map each class name to the sorted paths in it. The largest shift of
    each "moved" entry is appended to `shifts`."""
    if shifts is None:
        shifts = []
    classes: dict[str, list[str]] = {k: [] for k in ORDER}
    classes["only-before"] = sorted(set(before) - set(after))
    classes["only-after"] = sorted(set(after) - set(before))
    differing: list[str] = []
    for path in sorted(set(before) & set(after)):
        kind = _equivalent(path, before[path], after[path], meta, decode_tiles,
                           coord_tol, shifts)
        if kind:
            classes[kind].append(path)
        else:
            differing.append(path)

    # Kiwix search pages: which page number a group of results gets can
    # change between runs, so compare the differing pages as a multiset.
    pages = [p for p in differing if SEARCH_PAGE.match(p)]
    if pages and Counter((before[p].title, before[p].data) for p in pages) \
            == Counter((after[p].title, after[p].data) for p in pages):
        classes["reordered"].extend(pages)
        renumbered = set(pages)
        differing = [p for p in differing if p not in renumbered]

    # Noise: the control shows this entry varies between runs, and after.zim
    # has one of the variants the control produced.
    for path in differing:
        c = control.get(path) if control is not None else None
        if c is not None and c.key() != before[path].key() \
                and _equivalent(path, c, after[path], meta, decode_tiles, coord_tol, []):
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
    shifts: list[float] = []
    classes = classify(before, after, meta | meta_b, args.decode_tiles, control,
                       args.coord_tolerance, shifts)
    if shifts:
        print(f"largest coordinate shift accepted: {max(shifts):.6f} degrees")
    return 0 if report(classes, args.show) else 1


if __name__ == "__main__":
    sys.exit(main())
