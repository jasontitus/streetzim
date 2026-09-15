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
swaps the viewer files (``index.html``, ``places.html``, and
``routing-worker.js``). Large routing entries stay uncompressed so Kiwix
WebViews do not stall while inflating oversized clusters.

``--reshard-chips`` also rewrites the Find chips as geographic shards
(cloud/chip_shards.py): every ``category-index/chip-*.json`` is dropped,
each chip the source manifest declares is re-read whatever its layout and
re-planned, and ``category-index/manifest.json`` gets the new chip entries
with every other key kept. This is the chip retrofit for shipped ZIMs:
`repackage_zim.py --split-find-chips` does the same re-shard but loses the
title index, so Kiwix search suggestions come back empty.

Scope otherwise: no routing changes, no search-data rewrites, no terrain
refresh. Use `repackage_zim.py` for those (and accept that it loses Xapian
on rust-built sources).

Usage:
    python3 cloud/swap_viewer_rust.py SRC.zim DST.zim [--reshard-chips]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
VIEWER_DIR = REPO / "resources" / "viewer"
STREAMING_THRESHOLD = 64 * 1024 * 1024
CAT_MANIFEST = "category-index/manifest.json"

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


def _is_chip_entry(path: str) -> bool:
    return path.startswith("category-index/chip-") and path.endswith(".json")


def swap_viewer_rust(src_path: str, dst_path: str, reshard_chips: bool = False) -> int:
    from libzim.reader import Archive

    src = Archive(src_path)
    src_total = src.all_entry_count
    src_visible = src.entry_count
    print(f"  source: {src_path} ({os.path.getsize(src_path)/1024/1024:.1f} MB)")
    print(f"  entries: {src_visible} visible / {src_total} total "
          f"(diff = X-namespace + special)")

    def _src_bytes(path: str) -> bytes:
        return bytes(src.get_entry_by_path(path).get_item().content)

    # Chip retrofit: read the source manifest up front and refuse before
    # writing anything if there are no chips to re-shard (writing on would
    # ship a ZIM whose Find page has no chips at all).
    cat_manifest: dict | None = None
    cat_manifest_title = "Category Index Manifest"
    if reshard_chips:
        if not src.has_entry_by_path(CAT_MANIFEST):
            raise SystemExit(f"--reshard-chips: {src_path} has no {CAT_MANIFEST}")
        cat_manifest = json.loads(_src_bytes(CAT_MANIFEST))
        cat_manifest_title = src.get_entry_by_path(CAT_MANIFEST).title or cat_manifest_title
        if not isinstance(cat_manifest, dict) or not isinstance(cat_manifest.get("chips"), dict) \
                or not cat_manifest["chips"]:
            raise SystemExit(f"--reshard-chips: {src_path} declares no chips")
        # Read and count every chip now, before hours of copying: a missing
        # bucket or a count mismatch found after the walk aborts inside the
        # creator and strands a multi-GB .pack-stage directory.
        from cloud.chip_shards import read_chip_records
        for cid, meta in cat_manifest["chips"].items():
            if not isinstance(meta, dict):
                raise SystemExit(f"--reshard-chips: chip {cid} manifest entry is not an object")
            n = len(read_chip_records(_src_bytes, cid, meta))
            if isinstance(meta.get("count"), int) and meta["count"] != n:
                raise SystemExit(f"--reshard-chips: chip {cid} has {n} records "
                                 f"but the source manifest says {meta['count']}")
        print(f"  will re-shard {len(cat_manifest['chips'])} chip(s): "
              f"{', '.join(cat_manifest['chips'])}")

    # Collect viewer replacements from disk.
    replacements: dict[str, bytes] = {}
    for name in ("index.html", "places.html", "routing-worker.js"):
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
    dropped_chip_files = 0
    replaced_paths: set[str] = set()

    with tempfile.TemporaryDirectory(prefix="swap_viewer_rust_") as spill_dir:
        spill_dir_path = Path(spill_dir)

        def _stage_large(path: str, data: bytes) -> tuple[bytes | None, str | None]:
            if len(data) < STREAMING_THRESHOLD:
                return data, None
            safe = path.replace("/", "__")
            out = spill_dir_path / safe
            out.write_bytes(data)
            return None, str(out)

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
                    if i >= src_visible:
                        # W-namespace mainPage redirect — recreated by
                        # set_mainpath above, not as a content redirect.
                        continue
                    target = entry.get_redirect_entry()
                    if reshard_chips and (_is_chip_entry(entry.path)
                                          or _is_chip_entry(target.path)):
                        dropped_chip_files += 1
                        continue
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

                # Entries at ids >= entry_count are the M (metadata), W
                # (mainPage) and X (indexes) namespaces, returned with the
                # namespace stripped. Only the two Xapian databases are
                # worth carrying over; metadata was re-added above,
                # mainPage is set via set_mainpath, and the title
                # listings (`listing/titleOrdered/*`) are arrays of SOURCE
                # entry indexes — garbage in the repacked archive. The
                # old "everything past entry_count is Xapian" heuristic
                # emitted X/Title, X/Counter, X/Illustration… duplicates
                # and copied the stale listings verbatim.
                is_xapian = (path in ("fulltext/xapian", "title/xapian")
                             or mime.endswith("+xapian"))
                if i >= src_visible and not is_xapian:
                    continue

                if reshard_chips and (path == CAT_MANIFEST or _is_chip_entry(path)):
                    # Re-emitted below from the re-sharded plan.
                    if path != CAT_MANIFEST:
                        dropped_chip_files += 1
                    continue

                if path in replacements:
                    c.add_item(_Item(path, mime, title=title,
                                     data=replacements[path],
                                     compress=True, namespace=None))
                    replaced_paths.add(path)
                    swapped += 1
                    continue

                data = bytes(item.content)
                # Drop geo-index entries whose article wasn't actually bundled
                # (enwiki had no page for that title) so the viewer never lists a
                # place that 404s on "Read full article".
                if path == "wiki-geo-index.json":
                    try:
                        geo = json.loads(data.decode("utf-8"))
                        before = len(geo)
                        geo = {t: v for t, v in geo.items()
                               if src.has_entry_by_path("wiki-article/" + t)}
                        data = json.dumps(geo, separators=(",", ":")).encode("utf-8")
                        print(f"  geo-index filtered: {before} -> {len(geo)} "
                              f"(dropped {before - len(geo)} without a bundled article)")
                    except Exception as e:
                        print(f"  warning: geo-index filter failed: {e}")

                staged_data, staged_file = _stage_large(path, data)
                if is_xapian:
                    c.add_item(_Item(path, mime, title=title,
                                     data=staged_data, file_path=staged_file,
                                     compress=False, namespace="X"))
                    xapian += 1
                else:
                    # Large routing entries must remain raw. Kiwix WebViews
                    # can time out while decompressing a huge fzstd cluster.
                    compress = not (
                        path == "routing-data/graph-cells-index.bin"
                        or path.startswith("routing-data/graph-cell-")
                    ) or len(data) < 200 * 1024 * 1024
                    c.add_item(_Item(path, mime, title=title,
                                     data=staged_data, file_path=staged_file,
                                     compress=compress, namespace=None))
                    kept += 1
                del data

            for path, data in replacements.items():
                if path in replaced_paths:
                    continue
                mime = ("application/javascript"
                        if path.endswith(".js") else "text/html")
                title = ("Routing Worker" if path.endswith(".js")
                         else "Map" if path == "index.html" else "Find places")
                c.add_item(_Item(path, mime, title=title, data=data,
                                 compress=True, namespace=None))
                swapped += 1

            if reshard_chips:
                from cloud.chip_shards import plan_chip, read_chip_records
                new_chips: dict = {}
                n_files = 0
                for cid, meta in cat_manifest["chips"].items():
                    label = meta.get("label", cid) if isinstance(meta, dict) else cid
                    records = read_chip_records(_src_bytes, cid, meta)
                    want = meta.get("count") if isinstance(meta, dict) else None
                    if isinstance(want, int) and want != len(records):
                        raise SystemExit(f"--reshard-chips: chip {cid} has {len(records)} "
                                         f"records but the source manifest says {want}")
                    plan = plan_chip(records)
                    del records
                    nf = 0
                    for fpath, ftitle, blob in plan.files(cid, label):
                        c.add_item(_Item(fpath, "application/json", title=ftitle,
                                         data=blob, compress=True, namespace=None))
                        nf += 1
                    new_chips[cid] = plan.manifest_entry(label)
                    n_files += nf
                    print(f"  chip-{cid}: {plan.count:,} records "
                          f"({plan.bytes/1048576:.1f} MB) → {nf} file(s)", flush=True)
                    del plan
                payload = dict(cat_manifest)
                payload["chips"] = new_chips
                c.add_item(_Item(CAT_MANIFEST, "application/json", title=cat_manifest_title,
                                 data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                                 compress=True, namespace=None))
                print(f"  chips: dropped {dropped_chip_files} old file(s), wrote {n_files} "
                      f"for {len(new_chips)} chip(s)", flush=True)

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
    ap.add_argument("--reshard-chips", action="store_true",
                    help="Rewrite the Find chips as geographic shards "
                         "(cloud/chip_shards.py); keeps the Xapian indexes.")
    args = ap.parse_args()
    return swap_viewer_rust(args.src, args.dst, reshard_chips=args.reshard_chips)


if __name__ == "__main__":
    sys.exit(main())
