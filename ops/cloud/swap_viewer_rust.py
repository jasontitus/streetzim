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

``--reshard-search`` re-splits hot search prefixes by character path,
keeping the source's prefixes and its word rule. ``--rebuild-search``
re-derives the whole search index from the source's records under the
current word rule (marks continue a word; manifest ``word_rule`` 2): the
retrofit for ZIMs whose names were split at Indic/Thai vowel signs
(docs/search-prefix-locality.md#word-rule).

Scope otherwise: no routing changes, no terrain
refresh. Use `repackage_zim.py` for those (and accept that it loses Xapian
on rust-built sources).

Usage:
    python3 cloud/swap_viewer_rust.py SRC.zim DST.zim [--reshard-chips]
        [--reshard-search | --rebuild-search [--allow-total-mismatch]]
        [--tmp DIR]   (required, off the root fs, for the search options;
                       default $TMPDIR)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path


def _streetzim_root():
    """The streetzim checkout (builder, data, ZIMs): $STREETZIM_ROOT, else the
    nearest directory above this file that holds create_osm_zim.py. (This file
    lives in ops/ and usually runs through a symlink at its old path.)"""
    env = os.environ.get("STREETZIM_ROOT")
    if env:
        return os.path.abspath(env)
    d = os.path.dirname(os.path.realpath(__file__))
    while d != os.path.dirname(d):
        if os.path.isfile(os.path.join(d, "create_osm_zim.py")):
            return d
        d = os.path.dirname(d)
    raise SystemExit("cannot find the streetzim checkout; set STREETZIM_ROOT")


REPO = Path(_streetzim_root())
VIEWER_DIR = REPO / "resources" / "viewer"
STREAMING_THRESHOLD = 64 * 1024 * 1024
CAT_MANIFEST = "category-index/manifest.json"
SEARCH_MANIFEST = "search-data/manifest.json"
PLACE_INDEX = "category-index/place.json"
# Match create_osm_zim.py's CATEGORY_SHARD_MIN_BYTES: past this, the place
# category is sharded so the viewer's reverse geocoder reads the shard around
# the viewport instead of the whole file.
PLACE_SHARD_MIN_BYTES = 8 * 1024 * 1024
# Shard files are "<slug>-<suffix>.json". chip_shards._suffixes() emits
# f"g{i:0{width}x}" -- HEXADECIMAL, width >= 3: g000..g009, g00a..g00f,
# and wider for more leaves. A \d+ pattern silently misses a-f, which
# dropped 6 of every 16 shards. Match the generator, not a sample of it.
_SHARD_SUFFIX_RE = __import__("re").compile(r"-g[0-9a-f]{3,}$")
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


def _cat_index_slug(path: str) -> str | None:
    """`category-index/park.json` -> `park`; None for chips and the manifest.

    Whole-category files are what the Find page's legacy loader fetches. Any
    of them can be oversized -- europe ships park.json at 53 MB and china
    place.json at 109 MB -- so the re-shard must not be hard-coded to place.
    """
    if not path.startswith("category-index/") or not path.endswith(".json"):
        return None
    slug = path[len("category-index/"):-len(".json")]
    if slug == "manifest" or slug.startswith("chip-"):
        return None
    # `place-g000.json` is a SHARD of `place`, not a category called
    # "place-g000". Treating it as one made the walk drop every existing
    # shard while the emit loop skipped the category for already being
    # sharded -- europe lost all 256 place shards that way, caught by the
    # post-reshard category check.
    if _SHARD_SUFFIX_RE.search(slug):
        return None
    return slug or None


def _is_chip_entry(path: str) -> bool:
    return path.startswith("category-index/chip-") and path.endswith(".json")


# Fixed-size viewer slots: see cloud/viewer_slots.py for the layout and the
# reasoning. Padding each viewer file to a fixed, UNCOMPRESSED slot lets
# cloud/patch_viewer_inplace.py overwrite it later without re-packing the ZIM
# (~2h for europe, 0.1s patched). Uncompressed is required: bytes in a
# compressed cluster do not map to file offsets.
from cloud.viewer_slots import pad_to_slot as _pad_to_slot  # noqa: E402


def _homes(name: str) -> tuple[str, str]:
    """The prefixes a record's name was ALWAYS written under: its home.

    ``current``: ``prefix_key(norm(name)[:2])``, what every writer since
    6223071 (2026-09-03) keys the whole name by. ``legacy``: what the writers
    before it computed, ``_prefix_key(name[:2])`` on the RAW name -- its body
    is today's ``prefix_key`` (fold, then key), applied to the first two raw
    characters. They differ when the raw name's 2nd character is a mark the
    fold drops: decomposed Vietnamese "Ủy ban" ("u" + U+031B, raw key "u_",
    folded "uy"), "Écouen" in NFD, a name opening with a Thai tone mark.
    """
    from cloud.search_shards import norm, prefix_key
    nm = name or ""
    return prefix_key(norm(nm)[:2]), prefix_key(nm[:2])


def _source_records(src_bytes, manifest: dict, spool) -> int:
    """Write every distinct source search record once per feature to
    ``spool`` (JSON lines); returns the count.

    The writer puts a record in every prefix one of its names reaches, and in
    every leaf of a character-split prefix one of its paths reaches. A record
    is taken only from its HOME prefix (``_homes``: the key of its whole
    name's first two characters, by the current rule or by the one writers
    used before 2026-09-03 -- a ZIM holds the record under whichever its
    writer computed). Inside one leaf a record appears once per feature (two
    identical features: twice), so its feature count is the most copies any
    one home leaf holds; a record whose two homes differ is counted across
    both through one global table, so it is never taken twice. A leaf the
    manifest declares but the source cannot give is an error: skipping it
    would drop its records silently.
    """
    groups: dict[str, list[str]] = {}
    for name in manifest["chunks"]:
        groups.setdefault(_base_prefix(name), []).append(name)
    n = 0
    # Records whose current and legacy homes differ (rare: 156 of 26.7 M on
    # southeast-asia 2026-05-09), across every prefix.
    best_two: dict[bytes, int] = {}
    for prefix in sorted(groups):
        # Keyed by a digest: a hot prefix holds millions of records.
        best: dict[bytes, int] = {}
        for leaf in sorted(groups[prefix]):
            try:
                blob = src_bytes(f"search-data/{leaf}.json")
            except Exception as exc:
                raise SystemExit(f"--rebuild-search: declared leaf {leaf!r} is "
                                 f"unreadable ({exc}); its records would be lost")
            here: dict[str, int] = {}
            two: set[str] = set()
            for rec in json.loads(blob):
                cur, legacy = _homes(rec.get("n") or "")
                if prefix != cur and prefix != legacy:
                    continue
                # Serialised as the writer's pass 1 does (ASCII escapes), so
                # the planner sizes leaves exactly as a fresh build would.
                line = json.dumps(rec, separators=(",", ":"))
                here[line] = here.get(line, 0) + 1
                if cur != legacy:
                    two.add(line)
            for line, k in here.items():
                d = hashlib.blake2b(line.encode("utf-8"), digest_size=16).digest()
                table = best_two if line in two else best
                m = table.get(d, 0)
                if k > m:
                    spool.write((line + "\n") * (k - m))
                    n += k - m
                    table[d] = k
            del here
        del best
    return n


def _rebuild_search(c, src_bytes, manifest: dict, work: Path,
                    allow_total_mismatch: bool = False) -> int:
    """Re-derive search-data from the source's records with the current
    writer: keys and leaf paths under ``WORD_RULE``, then the same emit pass
    a build runs (zim_writer._search_emit_chunks, hot prefixes character-
    split at SEARCH_HOT_BYTES). Every other source manifest key is kept."""
    from cloud.search_shards import prefixes_for
    from streetzim.zim_writer import _search_emit_chunks
    spool_path = work / "search-records.jsonl"
    with open(spool_path, "w", encoding="utf-8") as spool:
        total = _source_records(src_bytes, manifest, spool)
    want = manifest.get("total")
    print(f"  search: {total:,} source record(s) recovered "
          f"(source manifest total {want})", flush=True)
    if total != want and not allow_total_mismatch:
        # A record not recovered is a place nobody can find again. (A ZIM
        # whose addresses were stripped by derive_zim, or one with records
        # under a home no known writer used, needs the override -- and a
        # look at why first.)
        raise SystemExit(f"--rebuild-search: recovered {total:,} record(s) but the "
                         f"source manifest says {want}; refusing to drop or invent "
                         f"records (--allow-total-mismatch overrides)")
    chunk_tmp = work / "search-rebuild"
    chunk_tmp.mkdir()
    counts: dict[str, int] = {}
    fds: dict[str, object] = {}
    with open(spool_path, encoding="utf-8") as spool:
        for line in spool:
            rec = json.loads(line)
            keys = prefixes_for(rec.get("n") or "")
            alt = rec.get("alt")
            if isinstance(alt, list):
                for a in alt:
                    if isinstance(a, str):
                        keys |= prefixes_for(a)
            for k in sorted(keys):
                fd = fds.get(k)
                if fd is None:
                    if len(fds) >= SEARCH_LEAF_FD_CAP:
                        fds.pop(next(iter(fds))).close()
                    fd = open(chunk_tmp / f"{k}.jsonl", "a", encoding="utf-8")
                    fds[k] = fd
                    counts.setdefault(k, 0)
                else:
                    fds[k] = fds.pop(k)
                fd.write(line)
                counts[k] += 1
    for fd in fds.values():
        fd.close()
    spool_path.unlink()

    def _map_item(path, title, mime, data, compress=True):
        return _Item(path, mime, title=title or "", data=data, compress=compress)

    extra = {k: v for k, v in manifest.items()
             if k not in ("total", "chunks", "sub_chunks", "char_split", "word_rule")}
    _search_emit_chunks(c, _map_item,
                        split_hot_search_chunks_mb=SEARCH_HOT_BYTES // (1024 * 1024),
                        chunk_tmp=str(chunk_tmp), chunk_counts=counts,
                        total_features=total, manifest_extra=extra)
    return total


def _on_root_fs(path: str) -> bool:
    return os.stat(path).st_dev == os.stat("/").st_dev


def _spill_root(tmp_dir: str | None, search: bool) -> str | None:
    """Where the spill directory goes: ``--tmp``, else $TMPDIR. A search
    rewrite spools the whole index there (several GB on a continent), so it
    must be named explicitly and must not be on the root filesystem -- the
    build host's / is small (and /tmp is on it)."""
    root = tmp_dir or os.environ.get("TMPDIR")
    if not search:
        return root
    if not root:
        raise SystemExit("--reshard-search/--rebuild-search spool the search index "
                         "(GBs): pass --tmp DIR or set TMPDIR to a directory under "
                         "/storage, never the default /tmp")
    if not os.path.isdir(root):
        raise SystemExit(f"--tmp/TMPDIR {root!r} is not a directory")
    if _on_root_fs(root):
        raise SystemExit(f"--tmp/TMPDIR {root!r} is on the root filesystem; point it "
                         f"at /storage (the search spool is several GB)")
    return root


def swap_viewer_rust(src_path: str, dst_path: str, reshard_chips: bool = False,
                     reshard_search: bool = False,
                     rebuild_search: bool = False,
                     tmp_dir: str | None = None,
                     allow_total_mismatch: bool = False) -> int:
    from libzim.reader import Archive

    spill_root = _spill_root(tmp_dir, reshard_search or rebuild_search)

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
    if reshard_search and rebuild_search:
        raise SystemExit("--reshard-search and --rebuild-search are exclusive "
                         "(--rebuild-search re-splits every prefix itself)")
    if rebuild_search:
        if not src.has_entry_by_path(SEARCH_MANIFEST):
            raise SystemExit(f"--rebuild-search: {src_path} has no {SEARCH_MANIFEST}")
        search_manifest = json.loads(_src_bytes(SEARCH_MANIFEST))
        search_manifest_title = (src.get_entry_by_path(SEARCH_MANIFEST).title
                                 or search_manifest_title)
        if not isinstance(search_manifest, dict) or not search_manifest.get("chunks"):
            raise SystemExit(f"--rebuild-search: {src_path} declares no search chunks")
        from cloud.search_shards import WORD_RULE, word_rule_of
        print(f"  will rebuild search-data: {len(search_manifest['chunks'])} source "
              f"chunk(s), word rule {word_rule_of(search_manifest)} → {WORD_RULE}")
    if reshard_search:
        if not src.has_entry_by_path(SEARCH_MANIFEST):
            raise SystemExit(f"--reshard-search: {src_path} has no {SEARCH_MANIFEST}")
        search_manifest = json.loads(_src_bytes(SEARCH_MANIFEST))
        search_manifest_title = (src.get_entry_by_path(SEARCH_MANIFEST).title
                                 or search_manifest_title)
        if not isinstance(search_manifest, dict) or not search_manifest.get("chunks"):
            raise SystemExit(f"--reshard-search: {src_path} declares no search chunks")
        if search_manifest.get("char_split"):
            # Not idempotent: _base_prefix maps ca~r~c back to ca and a record
            # is yielded once per leaf it occupies, so a second pass would
            # duplicate every multi-leaf record (+0.7% measured).
            raise SystemExit(f"--reshard-search: {src_path} already has the "
                             f"character-split layout — nothing to do")
        hot_prefixes = _hot_prefixes(search_manifest)
        print(f"  will re-split {len(hot_prefixes)} hot search prefix(es): "
              f"{', '.join(sorted(hot_prefixes)[:8])}"
              f"{'…' if len(hot_prefixes) > 8 else ''}")

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
        # Idempotent: categories already carrying shards keep them.
        src_sharded_cats = {
            k for k, v in (cat_manifest.get("category_shards") or {}).items() if v
        }
        if src_sharded_cats:
            print(f"  already sharded, keeping: {', '.join(sorted(src_sharded_cats))}")

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

    with tempfile.TemporaryDirectory(prefix="swap_viewer_rust_",
                                     dir=spill_root) as spill_dir:
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

                _cat_slug_drop = _cat_index_slug(path) if reshard_chips else None
                if _cat_slug_drop and _cat_slug_drop not in src_sharded_cats:
                    # Replaced by <slug>-g000.json… below; carrying the whole
                    # file too would keep the bytes the shards exist to avoid.
                    # (If the plan turns out to be under the threshold, the
                    # single unsharded file is re-emitted under the same name.)
                    dropped_chip_files += 1
                    continue

                if (reshard_search or rebuild_search) \
                        and path.startswith("search-data/") \
                        and path.endswith(".json"):
                    # Every search chunk and the manifest are rewritten below;
                    # carrying the old hash leaves too would ship both layouts.
                    if path != SEARCH_MANIFEST:
                        dropped_search_files += 1
                    continue

                if path in replacements:
                    c.add_item(_Item(path, mime, title=title,
                                     data=_pad_to_slot(path, replacements[path]),
                                     compress=False, namespace=None))
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
                c.add_item(_Item(path, mime, title=title,
                                 data=_pad_to_slot(path, data),
                                 compress=False, namespace=None))
                swapped += 1

            if rebuild_search:
                n_out = _rebuild_search(c, _src_bytes, search_manifest,
                                        spill_dir_path,
                                        allow_total_mismatch=allow_total_mismatch)
                print(f"  search: dropped {dropped_search_files} old file(s), "
                      f"rebuilt {n_out} record(s)", flush=True)

            if reshard_search:
                from cloud.search_shards import (Aggregator, SHARD_TARGET_BYTES,
                                                 char_split_paths, leaf_for,
                                                 tier_for, word_rule_of)
                # The prefixes are the source's, so the paths inside them must
                # follow the rule the source was bucketed with: planning a
                # rule-1 ZIM's prefixes with rule 2 strands every record whose
                # rule-2 words no longer reach the prefix it sits in.
                src_rule = word_rule_of(search_manifest)
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
                    agg = Aggregator(prefix, rule=src_rule)
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
                    seen_leaves: set[str] = set()
                    orphans = 0
                    first_orphan = ""
                    for rec in _src_records(names):
                        lnames = list(leaf_for(prefix, rec,
                                               planned_paths.get(tier_for(rec), ()),
                                               rule=src_rule))
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
                                seen_leaves.add(ln)
                                fd = open(pdir / f"{ln}.jsonl", "a", encoding="utf-8")
                                fds[ln] = fd
                            else:
                                # Refresh recency, or the cache evicts FIFO and
                                # a prefix with more leaves than the cap
                                # reopens a file on nearly every record.
                                fds[ln] = fds.pop(ln)
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
                        lrecs = []
                        # split("\n"), NOT splitlines(): with
                        # ensure_ascii=False a name containing U+2028 LINE
                        # SEPARATOR is written raw (legal JSON), and
                        # splitlines() treats it as a line break, cutting the
                        # record in two. east-coast-us has exactly one such
                        # name — "8 444 Lundy's Lane, Niagara Falls" —
                        # which failed the whole region twice.
                        for lineno, x in enumerate(
                                lpath.read_text(encoding="utf-8").split("\n")):
                            if not x:
                                continue
                            try:
                                lrecs.append(json.loads(x))
                            except Exception as exc:
                                # A leaf that does not round-trip means a
                                # record was written incomplete. Seen twice on
                                # east-coast-us prefix 44 and NOT reproducible
                                # afterwards, so the cause is still open —
                                # until it is, refuse to build a ZIM whose
                                # search data would silently lose a place.
                                raise SystemExit(
                                    f"--reshard-search: {prefix}: leaf {ln} "
                                    f"line {lineno} did not round-trip "
                                    f"({exc}); {len(x)} bytes: {x[:80]!r}. "
                                    f"Refusing to emit a corrupt search index.")
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
                # Whole-category index files are what the Find page's legacy
                # loader fetches, and any of them can be oversized: europe
                # ships park.json at 53 MB (over validate_zim's 48 MB
                # error-severity cap, which blocked europe's upload entirely
                # and left the live map on 2026-05-06) and china place.json at
                # 109 MB. Shard each one the way chips are sharded so the
                # viewer reads only the shard around the viewport.
                #
                # This was place-only until 2026-09-21. The restriction was
                # arbitrary -- the shard format and the viewer's reader are
                # keyed on the manifest, not the slug.
                new_cat_shards = dict(cat_manifest.get("category_shards") or {})
                cats = cat_manifest.get("categories") or {}
                for _slug in sorted(cats):
                    if _slug in src_sharded_cats:
                        continue          # already sharded; the walk kept it
                    _cpath = f"category-index/{_slug}.json"
                    if not src.has_entry_by_path(_cpath):
                        continue
                    try:
                        _recs = json.loads(_src_bytes(_cpath))
                    except Exception as exc:
                        # Cosmetic indexes (the reverse geocoder's city name,
                        # the legacy Find filter path). Losing one is not worth
                        # discarding a finished multi-GB copy -- but the walk
                        # already dropped the original, so say so loudly.
                        print(f"  warning: {_cpath} unreadable ({exc}); "
                              f"category '{_slug}' will be MISSING from the output",
                              flush=True)
                        continue
                    _blob = json.dumps(_recs, separators=(",", ":"),
                                       ensure_ascii=False).encode("utf-8")
                    if len(_blob) > PLACE_SHARD_MIN_BYTES:
                        _plan = plan_chip(_recs)
                        _n = 0
                        for fpath, ftitle, blob in _plan.files(
                                _slug, _slug, name_prefix="",
                                title_kind="Category index"):
                            c.add_item(_Item(fpath, "application/json", title=ftitle,
                                             data=blob, compress=True, namespace=None))
                            _n += 1
                        # plan_chip decides for itself whether the records
                        # warrant splitting (CHIP_SHARD_TARGET_BYTES). When it
                        # declines, files() re-emits the single whole file --
                        # so recording a category_shards entry would declare a
                        # sharded layout that has no shard files behind it.
                        # Caught on hispaniola with the threshold forced down:
                        # park showed sub_chunks=0, layout=None, 0 files.
                        if _plan.sharded:
                            new_cat_shards[_slug] = _plan.manifest_entry(_slug)
                            print(f"  {_slug}: {_plan.count:,} records "
                                  f"({_plan.bytes/1048576:.1f} MB) → {_n} shard(s)", flush=True)
                        else:
                            new_cat_shards.pop(_slug, None)
                            print(f"  {_slug}: {_plan.count:,} records "
                                  f"({_plan.bytes/1048576:.1f} MB) — plan declined to split",
                                  flush=True)
                        del _plan
                    else:
                        # Under the threshold: put the single file back. The
                        # walk already dropped it, and a region whose Find page
                        # cannot name the nearest city is worse than one that
                        # fetches a few MB to do it.
                        c.add_item(_Item(_cpath, "application/json",
                                         title=f"Category index {_slug}",
                                         data=_blob, compress=True, namespace=None))
                        print(f"  {_slug}: {len(_recs):,} records "
                              f"({len(_blob)/1048576:.1f} MB) — kept as one file",
                              flush=True)
                    del _recs, _blob

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
    ap.add_argument("--rebuild-search", action="store_true",
                    help="Re-derive the whole search index from the source's "
                         "records under the current word rule (manifest "
                         "word_rule 2: Indic/Thai vowel signs continue a word), "
                         "re-bucketing every record and re-planning every hot "
                         "prefix.")
    ap.add_argument("--tmp", metavar="DIR", default=None,
                    help="Spill directory (default $TMPDIR). Required, and not on "
                         "the root filesystem, with --reshard-search or "
                         "--rebuild-search: the search spool is several GB.")
    ap.add_argument("--allow-total-mismatch", action="store_true",
                    help="--rebuild-search: proceed when the records recovered "
                         "differ from the source manifest's total (e.g. a ZIM "
                         "whose addresses derive_zim stripped). Otherwise that "
                         "is an error.")
    args = ap.parse_args()
    return swap_viewer_rust(args.src, args.dst,
                            reshard_chips=args.reshard_chips,
                            reshard_search=args.reshard_search,
                            rebuild_search=args.rebuild_search,
                            tmp_dir=args.tmp,
                            allow_total_mismatch=args.allow_total_mismatch)


if __name__ == "__main__":
    sys.exit(main())
