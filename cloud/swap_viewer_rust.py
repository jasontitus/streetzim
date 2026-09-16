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
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
VIEWER_DIR = REPO / "resources" / "viewer"
STREAMING_THRESHOLD = 64 * 1024 * 1024
CAT_MANIFEST = "category-index/manifest.json"
SEARCH_MANIFEST = "search-data/manifest.json"
PLACE_INDEX = "category-index/place.json"
# Match create_osm_zim.py's CATEGORY_SHARD_MIN_BYTES: past this, the place
# category is sharded so the viewer's reverse geocoder reads the shard around
# the viewport instead of the whole file.
PLACE_SHARD_MIN_BYTES = 8 * 1024 * 1024
# Match the build wrappers' --split-hot-search-chunks-mb.
SEARCH_HOT_BYTES = 10 * 1024 * 1024
SEARCH_LEAF_FD_CAP = 256


def _base_prefix(chunk: str) -> str:
    """The 2-character prefix a chunk name belongs to: 'de-0-1' → 'de',
    'ca~r~c' → 'ca', 'u5927~u5b57~p' → 'u5927'."""
    return re.split(r"[-~]", chunk, 1)[0]


def _hot_prefixes(manifest: dict) -> dict[str, list[str]]:
    """Prefixes whose chunk was split — the ones a query pays for today."""
    groups: dict[str, list[str]] = {}
    for name in manifest.get("chunks", {}):
        groups.setdefault(_base_prefix(name), []).append(name)
    return {p: names for p, names in groups.items()
            if len(names) > 1 or names[0] != p}

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


def swap_viewer_rust(src_path: str, dst_path: str, reshard_chips: bool = False,
                     reshard_search: bool = False) -> int:
    from libzim.reader import Archive

    src = Archive(src_path)
    src_total = src.all_entry_count
    src_visible = src.entry_count
    print(f"  source: {src_path} ({os.path.getsize(src_path)/1024/1024:.1f} MB)")
    print(f"  entries: {src_visible} visible / {src_total} total "
          f"(diff = X-namespace + special)")

    def _src_bytes(path: str) -> bytes:
        return bytes(src.get_entry_by_path(path).get_item().content)

    # Search retrofit: hot prefixes are hash-fanned today, so a query fetches
    # every leaf (docs/search-prefix-locality.md). Read the manifest up front
    # and refuse before writing anything.
    search_manifest: dict | None = None
    search_manifest_title = "Search Manifest"
    if reshard_search:
        if not src.has_entry_by_path(SEARCH_MANIFEST):
            raise SystemExit(f"--reshard-search: {src_path} has no {SEARCH_MANIFEST}")
        search_manifest = json.loads(_src_bytes(SEARCH_MANIFEST))
        search_manifest_title = (src.get_entry_by_path(SEARCH_MANIFEST).title
                                 or search_manifest_title)
        if not isinstance(search_manifest, dict) or not search_manifest.get("chunks"):
            raise SystemExit(f"--reshard-search: {src_path} declares no search chunks")
        hot_prefixes = _hot_prefixes(search_manifest)
        print(f"  will re-split {len(hot_prefixes)} hot search prefix(es): "
              f"{', '.join(sorted(hot_prefixes)[:8])}"
              f"{'…' if len(hot_prefixes) > 8 else ''}")

    # Chip retrofit: read the source manifest up front and refuse before
    # writing anything if there are no chips to re-shard (writing on would
    # ship a ZIM whose Find page has no chips at all).
    cat_manifest: dict | None = None
    cat_manifest_title = "Category Index Manifest"
    # Read by the entry walk below, which runs in every mode — a
    # --reshard-search-only run never enters the chip preflight that sets it.
    src_place_sharded = True
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
        # Idempotent: a ZIM already carrying place shards keeps them.
        src_place_sharded = bool(
            (cat_manifest.get("category_shards") or {}).get("place"))
        if src_place_sharded:
            print("  place category already sharded — keeping it")

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
    dropped_search_files = 0
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

                if reshard_chips and path == PLACE_INDEX and not src_place_sharded:
                    # Replaced by place-g000.json… below; carrying the whole
                    # file too would keep the 109 MB the shards exist to avoid.
                    # (If the plan turns out to be under the threshold, the
                    # single unsharded file is re-emitted under the same name.)
                    dropped_chip_files += 1
                    continue

                if reshard_search and path.startswith("search-data/") \
                        and path.endswith(".json"):
                    # Every search chunk and the manifest are rewritten below;
                    # carrying the old hash leaves too would ship both layouts.
                    if path != SEARCH_MANIFEST:
                        dropped_search_files += 1
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

            if reshard_search:
                from cloud.search_shards import (Aggregator, SHARD_TARGET_BYTES,
                                                 char_split_paths, leaf_for,
                                                 tier_for)
                from cloud.repackage_zim import _split_records_recursive
                new_chunks: dict[str, int] = {}
                new_sub: dict[str, list[str]] = {}
                new_char: dict[str, list[str]] = {}
                search_leaves_written = 0
                target = min(SEARCH_HOT_BYTES, SHARD_TARGET_BYTES)
                groups: dict[str, list[str]] = {}
                for name in search_manifest["chunks"]:
                    groups.setdefault(_base_prefix(name), []).append(name)

                def _src_records(names):
                    """Stream a prefix's existing leaves, one at a time."""
                    for nm in names:
                        try:
                            blob = _src_bytes(f"search-data/{nm}.json")
                        except Exception:
                            continue
                        for rec in json.loads(blob):
                            yield rec

                for prefix in sorted(groups):
                    names = groups[prefix]
                    # Pass 1: size it without holding it. `av` on
                    # united-states is 2.93 GB of JSON.
                    agg = Aggregator(prefix)
                    total = 0
                    for rec in _src_records(names):
                        size = len(json.dumps(rec, separators=(",", ":"),
                                              ensure_ascii=False).encode("utf-8"))
                        total += size
                        agg.add(rec, size)
                    if total <= SEARCH_HOT_BYTES:
                        # Small enough to be one file again.
                        recs = list(_src_records(names))
                        c.add_item(_Item(f"search-data/{prefix}.json",
                                         "application/json",
                                         title=f"Search chunk {prefix}",
                                         data=json.dumps(recs, separators=(",", ":"),
                                                         ensure_ascii=False).encode("utf-8")))
                        new_chunks[prefix] = len(recs)
                        search_leaves_written += 1
                        continue
                    planned = agg.leaves(target_bytes=target)
                    planned_paths: dict[str, set] = {}
                    for _t, _p, _c2, _b in planned:
                        planned_paths.setdefault(_t, set()).add(_p)
                    # Pass 2: bucket into per-leaf temp files, fd-capped.
                    pdir = spill_dir_path / f"search-{prefix}"
                    pdir.mkdir(parents=True, exist_ok=True)
                    fds: dict[str, object] = {}
                    seen_leaves: list[str] = []
                    orphans = 0
                    first_orphan = ""
                    for rec in _src_records(names):
                        lnames = list(leaf_for(prefix, rec,
                                               planned_paths.get(tier_for(rec), ())))
                        if not lnames:
                            orphans += 1
                            if not first_orphan:
                                first_orphan = (rec.get("n") or "")[:60]
                            continue
                        line = json.dumps(rec, separators=(",", ":"),
                                          ensure_ascii=False) + "\n"
                        for ln in lnames:
                            fd = fds.get(ln)
                            if fd is None:
                                if len(fds) >= SEARCH_LEAF_FD_CAP:
                                    fds.pop(next(iter(fds))).close()
                                if ln not in seen_leaves:
                                    seen_leaves.append(ln)
                                fd = open(pdir / f"{ln}.jsonl", "a", encoding="utf-8")
                                fds[ln] = fd
                            fd.write(line)
                    for fd in fds.values():
                        fd.close()
                    if orphans:
                        # A record that reaches no leaf is a place the user can
                        # never find again, and no gate downstream would notice.
                        raise SystemExit(
                            f"--reshard-search: {prefix}: {orphans} record(s) "
                            f"matched no leaf (first: {first_orphan!r})")
                    leaf_names: list[str] = []
                    for ln in sorted(seen_leaves):
                        lpath = pdir / f"{ln}.jsonl"
                        lrecs = [json.loads(x) for x in
                                 lpath.read_text(encoding="utf-8").splitlines() if x]
                        lpath.unlink()
                        lblob = json.dumps(lrecs, separators=(",", ":"),
                                           ensure_ascii=False).encode("utf-8")
                        if len(lblob) > SEARCH_HOT_BYTES:
                            # Characters could not divide it ("Carrera 7" a
                            # million times) — hash-split as before.
                            for sub, sub_bytes, sub_count in _split_records_recursive(
                                    lrecs, ln, SEARCH_HOT_BYTES, n_buckets=16,
                                    max_depth=5):
                                c.add_item(_Item(f"search-data/{sub}.json",
                                                 "application/json",
                                                 title=f"Search chunk {sub}",
                                                 data=sub_bytes))
                                new_chunks[sub] = sub_count
                                leaf_names.append(sub)
                        else:
                            c.add_item(_Item(f"search-data/{ln}.json",
                                             "application/json",
                                             title=f"Search chunk {ln}",
                                             data=lblob))
                            new_chunks[ln] = len(lrecs)
                            leaf_names.append(ln)
                        del lrecs
                    pdir.rmdir()
                    search_leaves_written += len(leaf_names)
                    # Old clients resolve sub_chunks and fetch every leaf: as
                    # slow as before, never wrong. Never ship an empty list.
                    new_sub[prefix] = leaf_names
                    new_char[prefix] = char_split_paths(planned)
                    print(f"  search {prefix}: {len(names)} old leaf/leaves "
                          f"({total/1048576:.1f} MB) → {len(leaf_names)}", flush=True)
                payload = dict(search_manifest)
                payload["chunks"] = new_chunks
                payload["sub_chunks"] = new_sub
                payload["char_split"] = new_char
                mblob = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                c.add_item(_Item(SEARCH_MANIFEST, "application/json",
                                 title=search_manifest_title, data=mblob))
                print(f"  search: dropped {dropped_search_files} old file(s), "
                      f"wrote {search_leaves_written} for {len(groups)} prefix(es); "
                      f"manifest {len(mblob)/1048576:.2f} MB", flush=True)

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
                # places.html fetches category-index/place.json whole on load
                # just to name the nearest city — 109 MB on china, 480 MB on
                # europe. Shard it like a chip so the viewer reads only the
                # shard around the viewport.
                new_cat_shards = dict(cat_manifest.get("category_shards") or {})
                cats = cat_manifest.get("categories") or {}
                if "place" in cats and not src_place_sharded:
                    try:
                        place_records = json.loads(_src_bytes("category-index/place.json"))
                    except Exception as exc:
                        raise SystemExit(f"--reshard-chips: category-index/place.json "
                                         f"unreadable: {exc}")
                    place_blob = json.dumps(place_records, separators=(",", ":"),
                                            ensure_ascii=False).encode("utf-8")
                    if len(place_blob) > PLACE_SHARD_MIN_BYTES:
                        pplan = plan_chip(place_records)
                        pn = 0
                        for fpath, ftitle, blob in pplan.files(
                                "place", "place", name_prefix="",
                                title_kind="Category index"):
                            c.add_item(_Item(fpath, "application/json", title=ftitle,
                                             data=blob, compress=True, namespace=None))
                            pn += 1
                        new_cat_shards["place"] = pplan.manifest_entry("place")
                        print(f"  place: {pplan.count:,} records "
                              f"({pplan.bytes/1048576:.1f} MB) → {pn} shard(s)", flush=True)
                        del pplan
                    else:
                        # Under the threshold: put the single file back. The
                        # walk already dropped it, and a region whose Find
                        # page cannot name the nearest city is worse than one
                        # that fetches 7 MB to do it.
                        c.add_item(_Item(PLACE_INDEX, "application/json",
                                         title="Category index place",
                                         data=place_blob, compress=True,
                                         namespace=None))
                        print(f"  place: {len(place_records):,} records "
                              f"({len(place_blob)/1048576:.1f} MB) — kept as one file",
                              flush=True)
                    del place_records, place_blob

                payload = dict(cat_manifest)
                payload["chips"] = new_chips
                if new_cat_shards:
                    payload["category_shards"] = new_cat_shards
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
    ap.add_argument("--reshard-search", action="store_true",
                    help="Re-split hot search-data prefixes by character path "
                         "and record tier (cloud/search_shards.py), so a query "
                         "reads one leaf instead of every leaf.")
    args = ap.parse_args()
    return swap_viewer_rust(args.src, args.dst,
                            reshard_chips=args.reshard_chips,
                            reshard_search=args.reshard_search)


if __name__ == "__main__":
    sys.exit(main())
