#!/usr/bin/env python3
"""Compare two ZIMs entry by entry, to show a change to the builder changed
no output (docs/golden-builds.md).

    python tools/golden_diff.py before.zim after.zim [--control before2.zim]
                                [--decode-tiles] [--coord-tolerance DEG] [--show N]

Every entry is compared by namespace and path, title, MIME type or redirect
target, and content. Paths are reported with a namespace prefix: C/ content,
M/ metadata, W/ libzim's well-known redirects (mainPage), X/ libzim's
listings and Xapian indexes. Each entry lands in one class:

  identical     same bytes;
  volatile      expected to differ between any two builds: the M/Date
                metadata (a YYYY-MM-DD date on both sides), the Xapian
                indexes (libzim does not write them reproducibly, so their
                contents are not compared: only that both sides have them,
                with the same M/Language), and the `buildDate` value of
                map-config.json and streetzim-meta.json (on both sides, as
                date strings of the same shape, e.g. 2026/09 or 2026-09-29;
                everything else in those files compared type-strictly);
  reordered     a list of search records (JSON objects with "t" and "n")
                holding the same records, whose sequence of (type, name) --
                the order the viewer lists them in -- is unchanged: only
                records with the same type and name swapped places (they
                may differ in other fields: two builds of one commit swap
                such records, see docs/golden-builds.md). Also the same
                JSON written differently (escapes), and Kiwix search pages
                (C/search/*.html) whose entries (title, MIME type or
                redirect target, content) moved to other page numbers;
  tiles-equal   (--decode-tiles) a vector tile with the same features in
                another order, as tilemaker writes them;
  moved         (--coord-tolerance) as reordered, but records' coordinates
                ("a", "o") may also have moved by at most DEG degrees, as
                when tilemaker cuts a street differently. Coordinates must be
                finite numbers. The largest shift accepted is printed;
  noise         (--control) the control (a second build of the same code and
                inputs) differs from before.zim too, and after.zim's entry is
                the same as the control's, exactly or up to reordering (no
                coordinate tolerance);
  changed, only-before, only-after
                real differences.

Exit status 0 when nothing is changed, only-before or only-after; 1
otherwise; 2 on a usage error or an archive that cannot be read, is empty,
or has two non-content entries on one path. The archive UUIDs always differ
and are printed for reference only.

Memory: each archive is read once, keeping per entry only its path, title,
MIME type and a SHA-256 of its content; content is re-read from the archive
only for entries whose hashes differ. So memory grows with the number of
entries (roughly 200 bytes each), not their size.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

FAIL = ("changed", "only-before", "only-after")
ORDER = ("identical", "volatile", "reordered", "tiles-equal", "moved", "noise", *FAIL)
# The build date, and nothing else, may differ in these files.
DATED_JSON = {"C/map-config.json": "buildDate", "C/streetzim-meta.json": "buildDate"}
SEARCH_PAGE = re.compile(r"^C/search/.*\.html$")
ISO_DATE = re.compile(rb"\d{4}-\d{2}-\d{2}")
# buildDate: an ISO-style date, month or datetime ("2026/09", "2026-09-29",
# "2026-09-29T12:00:00Z").
BUILD_DATE = re.compile(r"\d{4}[-/]\d{2}(?:[-/]\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z?)?)?")


class Entry:
    """One entry. A redirect has no item, so no MIME type or content: it is
    stored with mime "" and data b"" and compared by its target (and title).

    Entries read from an archive keep only a digest of their content and
    `loader`, which re-reads it on demand; `data` is not cached, so memory
    stays bounded whatever the archive's size."""
    __slots__ = ("_data", "_loader", "digest", "mime", "redirect", "title")

    def __init__(self, mime: str, data: bytes = b"", redirect: str | None = None,
                 title: str = "", loader: Callable[[], bytes] | None = None,
                 digest: str | None = None) -> None:
        self.mime, self.redirect, self.title = mime, redirect, title
        self._data = None if loader else data
        self._loader = loader
        self.digest = digest if digest is not None else hashlib.sha256(data).hexdigest()

    @property
    def data(self) -> bytes:
        if self._data is not None:
            return self._data
        assert self._loader is not None
        return self._loader()

    def key(self) -> tuple[str, str, str | None, str]:
        return (self.mime, self.title, self.redirect, self.digest)


class Log:
    """What the comparison accepted and why: coordinate shifts, and notes on
    entries it could not compare."""

    def __init__(self) -> None:
        self.shifts: list[float] = []
        self.notes: list[str] = []


class ZimError(Exception):
    pass


def read_zim(path: str) -> tuple[dict[str, Entry], str]:
    """All entries by namespaced path (see the module docstring), and the
    archive UUID. Raises ZimError for an unreadable or empty archive."""
    from libzim.reader import Archive
    try:
        a = Archive(Path(path))
    except (RuntimeError, OSError) as exc:
        raise ZimError(f"cannot open {path}: {exc}") from exc
    meta = set(a.metadata_keys)
    n = getattr(a, "all_entry_count", a.entry_count)
    out: dict[str, Entry] = {}

    def loader(i: int) -> Callable[[], bytes]:
        return lambda: bytes(a._get_entry_by_id(i).get_item().content)

    for i in range(n):
        e = a._get_entry_by_id(i)
        p = e.path
        # python-libzim does not expose the namespace. A content entry is
        # the one get_entry_by_path (content namespace only) finds.
        if a.has_entry_by_path(p) and a.get_entry_by_path(p)._index == i:
            ns = "C"
        elif p in meta:
            ns = "M"
        else:
            ns = "W" if e.is_redirect else "X"
        key = f"{ns}/{p}"
        if key in out:
            raise ZimError(f"{path}: two entries on {key}; cannot tell them apart")
        if e.is_redirect:
            out[key] = Entry("", b"", e.get_redirect_entry().path, e.title)
            continue
        it = e.get_item()
        digest = hashlib.sha256(bytes(it.content)).hexdigest()
        out[key] = Entry(it.mimetype, b"", None, e.title, loader(i), digest)
    if not any(k.startswith("C/") for k in out):
        raise ZimError(f"{path} has no content entries")
    return out, str(a.uuid)


def _canon(x: Any) -> str:
    """JSON text that keeps types apart (true vs 1, 13 vs 13.0)."""
    return json.dumps(x, sort_keys=True)


def _sorted_items(items: Iterable[Any]) -> list[str]:
    return sorted(_canon(x) for x in items)


def _display_keys(items: list[Any]) -> list[str] | None:
    """The (type, name) of each record, or None if the list is not all
    search records."""
    keys: list[str] = []
    for r in items:
        if not (isinstance(r, dict) and "t" in r and "n" in r):
            return None
        keys.append(_canon([r["t"], r["n"]]))
    return keys


def _json_class(path: str, a: bytes, b: bytes, coord_tol: float, log: Log) -> str | None:
    """Return "reordered" or "moved" (see the module docstring) for two JSON
    lists of search records, else None."""
    try:
        ja, jb = json.loads(a), json.loads(b)
    except ValueError:
        return None
    if not (isinstance(ja, list) and isinstance(jb, list)):
        return None
    ka = _display_keys(ja)
    if ka is None or ka != _display_keys(jb):
        return None           # not search records, or the displayed order changed
    if _sorted_items(ja) == _sorted_items(jb):
        return "reordered"
    if coord_tol > 0:
        shift = _records_shift(path, ja, jb, log)
        if shift is not None and shift <= coord_tol:
            log.shifts.append(shift)
            return "moved"
    return None


def _coord(r: dict[str, Any], k: str) -> float | None:
    v = r[k]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _records_shift(path: str, ja: list[Any], jb: list[Any], log: Log) -> float | None:
    """The largest coordinate difference, in degrees, between records that
    are otherwise equal (paired in coordinate order), or None when the
    records differ in anything but their coordinates, or a coordinate is not
    a finite number."""
    def groups(items: list[Any]) -> dict[str, list[tuple[float, float]]] | None:
        out: dict[str, list[tuple[float, float]]] = {}
        for r in items:
            if not (isinstance(r, dict) and "a" in r and "o" in r):
                return None
            lat, lon = _coord(r, "a"), _coord(r, "o")
            if lat is None or lon is None:
                log.notes.append(f"{path}: coordinate not a finite number in {_canon(r)[:200]}")
                return None
            rest = {k: v for k, v in r.items() if k not in ("a", "o")}
            out.setdefault(_canon(rest), []).append((lat, lon))
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


def _is_volatile(path: str, a: Entry, b: Entry) -> bool:
    if path == "M/Date":      # openZIM's format, YYYY-MM-DD, on both sides
        return all(ISO_DATE.fullmatch(e.data) for e in (a, b))
    if path.startswith("X/") and "xapian" in a.mime and "xapian" in b.mime:
        return True
    if path in DATED_JSON:
        key = DATED_JSON[path]
        try:
            ja, jb = json.loads(a.data), json.loads(b.data)
        except ValueError:
            return False
        if not (isinstance(ja, dict) and isinstance(jb, dict) and key in ja and key in jb):
            return False
        da, db = ja[key], jb[key]
        if not (isinstance(da, str) and isinstance(db, str) and BUILD_DATE.fullmatch(da)
                and BUILD_DATE.fullmatch(db)
                and re.sub(r"\d", "9", da) == re.sub(r"\d", "9", db)):
            return False
        ja.pop(key)
        jb.pop(key)
        return _canon(ja) == _canon(jb)
    return False


def _equivalent(path: str, a: Entry, b: Entry, decode_tiles: bool,
                coord_tol: float, log: Log) -> str | None:
    """The accepted class of a difference between `a` and `b`, or None."""
    if a.key() == b.key():
        return "identical"
    if a.mime != b.mime or a.redirect != b.redirect or a.title != b.title:
        return None
    if _is_volatile(path, a, b):
        return "volatile"
    if a.mime == "application/json":
        return _json_class(path, a.data, b.data, coord_tol, log)
    if decode_tiles and a.mime == "application/x-protobuf" \
            and _tile_features(a.data) == _tile_features(b.data):
        return "tiles-equal"
    return None


def classify(before: dict[str, Entry], after: dict[str, Entry],
             decode_tiles: bool = False,
             control: dict[str, Entry] | None = None,
             coord_tol: float = 0.0,
             log: Log | None = None) -> dict[str, list[str]]:
    """Map each class name to the sorted paths in it. Paths carry their
    namespace prefix (C/, M/, W/, X/)."""
    if log is None:
        log = Log()
    classes: dict[str, list[str]] = {k: [] for k in ORDER}
    classes["only-before"] = sorted(set(before) - set(after))
    classes["only-after"] = sorted(set(after) - set(before))
    differing: list[str] = []
    for path in sorted(set(before) & set(after)):
        try:
            kind = _equivalent(path, before[path], after[path], decode_tiles, coord_tol, log)
        except Exception as exc:  # a malformed entry is a difference, not a crash
            log.notes.append(f"{path}: could not compare ({exc!r})")
            kind = None
        if kind:
            classes[kind].append(path)
        else:
            differing.append(path)

    # Kiwix search pages: which page number a group of results gets can
    # change between runs, so compare the differing pages (whole entries:
    # title, MIME type or redirect target, content) as a multiset.
    pages = [p for p in differing if SEARCH_PAGE.match(p)]
    if pages and Counter(before[p].key() for p in pages) == Counter(after[p].key() for p in pages):
        classes["reordered"].extend(pages)
        renumbered = set(pages)
        differing = [p for p in differing if p not in renumbered]

    # Noise: the control shows this entry varies between runs, and after.zim
    # has exactly the variant the control produced (up to reordering; no
    # coordinate tolerance, which would let after drift twice as far).
    for path in differing:
        c = control.get(path) if control is not None else None
        noise = False
        if c is not None and c.key() != before[path].key():
            try:
                noise = _equivalent(path, c, after[path], decode_tiles, 0.0, Log()) is not None
            except Exception:
                noise = False
        classes["noise" if noise else "changed"].append(path)
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
    ap = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n\n")[0],
        epilog="Keeps a SHA-256 per entry, not the content: memory grows with the number "
               "of entries (a few hundred bytes each), time with archive size (every "
               "cluster is decompressed once). Xapian index contents are not compared. "
               "See docs/golden-builds.md.")
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--control", help="a second build of `before` (same code, same inputs); "
                                      "entries where after matches it are reported as noise")
    ap.add_argument("--decode-tiles", action="store_true",
                    help="compare differing vector tiles by their decoded features")
    ap.add_argument("--coord-tolerance", type=float, default=0.0, metavar="DEG",
                    help="accept search records whose coordinates moved by at most DEG "
                         "degrees (for tilemaker builds; default 0: exact)")
    ap.add_argument("--show", type=int, default=20, help="paths to list per class (default 20)")
    args = ap.parse_args(argv)

    try:
        before, uuid_a = read_zim(args.before)
        after, uuid_b = read_zim(args.after)
        control = read_zim(args.control)[0] if args.control else None
    except ZimError as exc:
        print(f"golden_diff: {exc}", file=sys.stderr)
        return 2
    print(f"before {args.before}: {len(before):,d} entries, uuid {uuid_a}")
    print(f"after  {args.after}: {len(after):,d} entries, uuid {uuid_b}")
    if control is not None:
        print(f"control {args.control}: {len(control):,d} entries")
    log = Log()
    classes = classify(before, after, args.decode_tiles, control, args.coord_tolerance, log)
    for note in log.notes:
        print(f"note: {note}")
    if log.shifts:
        print(f"largest coordinate shift accepted: {max(log.shifts):.6f} degrees")
    return 0 if report(classes, args.show) else 1


if __name__ == "__main__":
    sys.exit(main())
