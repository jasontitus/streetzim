#!/usr/bin/env python3
"""swap_viewer_rust.py — fast viewer-asset swap for rust-built ZIMs.

Problem: `cloud/repackage_zim.py` uses the python `libzim.writer.Creator`,
which cannot write entries into the 'X' (Xapian) namespace. Rust-built
ZIMs (`create_osm_zim.py --zim-builder=rust --xapian=builder`) store
fulltext+title Xapian glass DBs at paths ``fulltext/xapian`` and
``title/xapian`` in namespace 'X'. The python repackage path drops
these entries, shipping a ZIM with broken search.

This tool emits via :class:`ManifestCreator` (cloud.manifest_writer)
which honours per-item `_namespace='X'`. It walks the source's
`all_entry_count` (not just `entry_count`) so Xapian entries are
picked up, sets `_namespace='X', _compress=False` for them, and
swaps the two viewer files (``index.html`` and ``places.html``).
Everything else copies byte-for-byte from the source.

Scope: viewer swap ONLY. No routing changes, no chip-split, no
search-data rewrites, no terrain refresh. Use `repackage_zim.py` for
those (and accept that it loses Xapian on rust-built sources).

Usage:
    python3 cloud/swap_viewer_rust.py SRC.zim DST.zim
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
VIEWER_DIR = REPO / "resources" / "viewer"

sys.path.insert(0, str(REPO))
from cloud.manifest_writer import ManifestCreator  # noqa: E402


class _Item:
    """Duck-typed Item compatible with ManifestCreator._item_record."""

    def __init__(self, path, mimetype, *, title="", data=None, file_path=None,
                 compress=True, namespace=None, is_front=False):
        self._path = path
        self._title = title
        self._mimetype = mimetype
        self._data = data
        self._file_path = file_path
        self._compress = compress
        self._namespace = namespace
        self._is_front = is_front


def swap_viewer_rust(src_path: str, dst_path: str) -> int:
    from libzim.reader import Archive

    src = Archive(src_path)
    src_total = src.all_entry_count
    src_visible = src.entry_count
    print(f"  source: {src_path} ({os.path.getsize(src_path)/1024/1024:.1f} MB)")
    print(f"  entries: {src_visible} visible / {src_total} total "
          f"(diff = X-namespace + special)")

    # Collect viewer replacements from disk.
    replacements: dict[str, bytes] = {}
    for name in ("index.html", "places.html"):
        p = VIEWER_DIR / name
        if not p.exists():
            print(f"  warning: {p} missing; that viewer file will NOT be swapped")
            continue
        replacements[name] = p.read_bytes()
        print(f"  will swap {name} ← {p} ({len(replacements[name])} B)")

    # Resolve the source's main page (typically a redirect chain ending
    # at index.html). Streetzim-pack needs an explicit main path.
    main_path: str | None = None
    try:
        if src.has_main_entry:
            m = src.main_entry
            while m.is_redirect:
                m = m.get_redirect_entry()
            main_path = m.path
    except Exception as e:
        print(f"  warning: resolve main entry: {e}")
    if main_path is None and src.has_entry_by_path("index.html"):
        main_path = "index.html"
    if main_path is None:
        raise RuntimeError("source ZIM has no resolvable main entry")
    print(f"  main path: {main_path!r}")

    started = time.time()
    creator = ManifestCreator(dst_path, compression_level=22, verbose=True)
    creator.set_mainpath(main_path)
    swapped = 0
    xapian = 0
    metadata_count = 0
    illustration_count = 0
    redirects = 0
    kept = 0

    with creator as c:
        # Copy metadata entries verbatim (Title, Description, Date, Name,
        # Counter, Language, etc.). ManifestCreator distinguishes
        # metadata via add_metadata; iterating metadata_keys gives names
        # only (no namespace prefixes).
        for k in src.metadata_keys:
            try:
                v = src.get_metadata(k)
                if k == "Illustration_48x48@1":
                    # Special — ManifestCreator wants illustration via
                    # add_illustration(size, png_bytes).
                    if isinstance(v, str):
                        v = v.encode("latin-1")  # libzim returns str for binary metadata sometimes
                    c.add_illustration(48, bytes(v))
                    illustration_count += 1
                else:
                    if isinstance(v, bytes):
                        try:
                            v = v.decode("utf-8")
                        except UnicodeDecodeError:
                            pass
                    c.add_metadata(k, v)
                    metadata_count += 1
            except Exception as e:
                print(f"  skip metadata {k}: {e}")

        # Walk ALL entries (incl. X-namespace at ids >= entry_count).
        for i in range(src_total):
            try:
                entry = src._get_entry_by_id(i)
            except Exception as e:
                print(f"  skip entry id={i}: {e}")
                continue
            if entry.is_redirect:
                target = entry.get_redirect_entry()
                try:
                    c.add_redirection(entry.path,
                                      entry.title or entry.path,
                                      target.path)
                    redirects += 1
                except Exception as e:
                    print(f"  skip redirect {entry.path}: {e}")
                continue

            item = entry.get_item()
            path = entry.path
            mime = item.mimetype
            title = entry.title or ""

            # X-namespace heuristic: id >= visible entry_count OR path
            # matches the well-known Xapian paths. Either signal is
            # sufficient — the create_osm_zim.py rust path places these
            # in namespace 'X' with compress=False and an
            # application/octet-stream+xapian mimetype.
            is_xapian = (i >= src_visible
                         or path in ("fulltext/xapian", "title/xapian")
                         or mime.endswith("+xapian"))

            if path in replacements:
                c.add_item(_Item(path, mime, title=title,
                                 data=replacements[path],
                                 compress=True, namespace=None))
                swapped += 1
                continue

            data = bytes(item.content)
            # Drop geo-index entries whose article wasn't actually bundled
            # (enwiki had no page for that title) so the viewer never lists a
            # place that 404s on "Read full article".
            if path == "wiki-geo-index.json":
                try:
                    import json as _json
                    geo = _json.loads(data.decode("utf-8"))
                    before = len(geo)
                    geo = {t: v for t, v in geo.items()
                           if src.has_entry_by_path("wiki-article/" + t)}
                    data = _json.dumps(geo, separators=(",", ":")).encode("utf-8")
                    print(f"  geo-index filtered: {before} -> {len(geo)} "
                          f"(dropped {before - len(geo)} without a bundled article)")
                except Exception as e:
                    print(f"  warning: geo-index filter failed: {e}")
            if is_xapian:
                c.add_item(_Item(path, mime, title=title,
                                 data=data,
                                 compress=False, namespace="X"))
                xapian += 1
            else:
                c.add_item(_Item(path, mime, title=title,
                                 data=data,
                                 compress=True, namespace=None))
                kept += 1

    elapsed = time.time() - started
    print(f"\n  done in {elapsed:.1f}s")
    print(f"    metadata:      {metadata_count}")
    print(f"    illustrations: {illustration_count}")
    print(f"    redirects:     {redirects}")
    print(f"    viewer swaps:  {swapped}")
    print(f"    X-namespace:   {xapian} (Xapian glass DBs preserved)")
    print(f"    passthrough:   {kept}")
    if os.path.isfile(dst_path):
        print(f"  output: {dst_path} "
              f"({os.path.getsize(dst_path)/1024/1024:.1f} MB)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src", help="Source .zim file")
    ap.add_argument("dst", help="Output .zim file")
    args = ap.parse_args()
    return swap_viewer_rust(args.src, args.dst)


if __name__ == "__main__":
    sys.exit(main())
