"""In-place upgrader for already-spatial ZIMs (SZCI v1 → v2 + recursive
search-chunk split).

Use this when the source ZIM was repackaged before the
``shard nodes_scaled`` + ``recursive search-chunk splitter`` fixes
shipped (commit ff7fa28). Building from the planet PBF would take 10+ h;
this upgrader rewrites just the index + oversized chunks and pass-throughs
everything else (cells, tiles, satellite, terrain, search-data leaves
already under threshold, wikidata, viewer, metadata).

The source must already be in spatial form — i.e., it has
``routing-data/graph-cells-index.bin`` (SZCI v1) and
``routing-data/graph-cell-NNNNN.bin`` files. We don't need the original
monolithic ``routing-data/graph.bin`` and don't reconstruct it; cells
are passed through byte-identical.

Usage::

    venv-linux/bin/python3 cloud/upgrade_spatial_zim.py SRC.zim DST.zim \\
        [--split-hot-search-chunks-mb 10] [--keep-spill]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from libzim.reader import Archive  # noqa: E402
from libzim.writer import (  # noqa: E402
    Creator, Item, ContentProvider, StringProvider, FileProvider, Blob, Hint,
)

from tests.szrg_spatial import (  # noqa: E402
    SZCI_MAGIC, SZCI_VERSION_INLINE, SZCI_VERSION_SHARDED,
    DEFAULT_NODES_PER_SHARD, _node_shard_ranges, parse_szci,
)
from cloud.repackage_zim import (  # noqa: E402
    _split_records_recursive,
)


# --- libzim Item adapters (mirror cloud/repackage_zim.py) ------------------


class PassthroughItem(Item):
    """Eager item — bytes already in hand."""
    def __init__(self, path, title, mimetype, data, compress=True):
        super().__init__()
        self._path = path
        self._title = title
        self._mimetype = mimetype
        self._data = data
        self._compress = compress

    def get_path(self):      return self._path
    def get_title(self):     return self._title
    def get_mimetype(self):  return self._mimetype
    def get_contentprovider(self): return StringProvider(self._data)
    def get_hints(self):
        return {Hint.FRONT_ARTICLE: False, Hint.COMPRESS: self._compress}


class FilePathItem(Item):
    """File-backed item — libzim FileProvider reads bytes off disk lazily."""
    def __init__(self, path, title, mimetype, file_path, compress=True):
        super().__init__()
        self._path = path
        self._title = title
        self._mimetype = mimetype
        self._file_path = str(file_path)
        self._compress = compress

    def get_path(self):      return self._path
    def get_title(self):     return self._title
    def get_mimetype(self):  return self._mimetype
    def get_contentprovider(self): return FileProvider(self._file_path)
    def get_hints(self):
        return {Hint.FRONT_ARTICLE: False, Hint.COMPRESS: self._compress}


class LazyZimEntryProvider(ContentProvider):
    """gen_blob() reads the source entry only at libzim cluster-compress
    time, keeping producer-side memory bounded. ``feed()`` segfaults from
    libzim worker threads when overridden in Python; gen_blob is the
    only path that works."""
    def __init__(self, src_arc, entry_path, size):
        super().__init__()
        self._src = src_arc
        self._path = entry_path
        self._size = size

    def get_size(self):
        return self._size

    def gen_blob(self):
        entry = self._src.get_entry_by_path(self._path)
        yield Blob(bytes(entry.get_item().content))


class LazyPassthroughItem(Item):
    """Passthrough whose bytes are read from the source ZIM at
    cluster-compress time. Source Archive must outlive the Creator."""
    def __init__(self, src_arc, path, title, mimetype, size, compress=True):
        super().__init__()
        self._src = src_arc
        self._path = path
        self._title = title
        self._mimetype = mimetype
        self._size = size
        self._compress = compress

    def get_path(self):      return self._path
    def get_title(self):     return self._title
    def get_mimetype(self):  return self._mimetype
    def get_contentprovider(self):
        return LazyZimEntryProvider(self._src, self._path, self._size)
    def get_hints(self):
        return {Hint.FRONT_ARTICLE: False, Hint.COMPRESS: self._compress}


# --- v1 SZCI → v2 SZCI rewriter ---------------------------------------------


def _rewrite_szci_v1_to_v2(idx_v1_bytes: bytes,
                           output_dir: Path,
                           ) -> tuple[bytes, list[Path]]:
    """Read a v1 SZCI bytes blob (with inline nodes_scaled), strip the
    nodes_scaled out into per-file shards on disk, and return the new
    v2 SZCI bytes + the list of shard paths.

    The body order in v1 is::
        magic(4) + 7×u32 header + nodes_scaled(num_nodes*8) +
        cell_meta(num_cells*20) + name_offsets((num_names+1)*4) +
        names_blob(names_bytes)

    In v2 the nodes_scaled blob is gone from the index and lives in
    ``output_dir/nodes-scaled-NNN.bin`` shards instead. The header
    grows from 7×u32 to 9×u32 (adds num_node_shards + nodes_per_shard).
    """
    if idx_v1_bytes[:4] != SZCI_MAGIC:
        raise ValueError("source index is not SZCI")
    v = struct.unpack_from("<I", idx_v1_bytes, 4)[0]
    if v == SZCI_VERSION_SHARDED:
        # Source is already v2. Return its bytes unchanged and signal
        # "no shards to rewrite" — the caller still copies the existing
        # nodes-scaled-NNN.bin entries via LazyPassthrough. Useful when
        # the upgrader is being invoked solely for the overture-sources
        # injection or another non-routing fix on a ZIM that already
        # has the v2 layout applied.
        return idx_v1_bytes, []
    if v != SZCI_VERSION_INLINE:
        raise ValueError(
            f"source index is SZCI v{v}, expected v{SZCI_VERSION_INLINE} "
            f"or v{SZCI_VERSION_SHARDED} (got v{v})"
        )
    (_v, num_nodes, num_edges, num_names, names_bytes, num_cells,
     _cs_unsigned) = struct.unpack_from("<7I", idx_v1_bytes, 4)
    cell_scale_signed = struct.unpack_from("<i", idx_v1_bytes, 4 + 6 * 4)[0]

    # Bytes layout offsets in v1
    nodes_off = 32
    nodes_size = num_nodes * 2 * 4
    after_nodes = nodes_off + nodes_size

    # 1) Slice nodes_scaled out and write shards
    shard_ranges = _node_shard_ranges(num_nodes, DEFAULT_NODES_PER_SHARD)
    nodes_per_shard = DEFAULT_NODES_PER_SHARD
    num_node_shards = len(shard_ranges)
    shard_paths: list[Path] = []
    for shard_idx, (lo, hi) in enumerate(shard_ranges):
        # Each node = 8 bytes (lat/lon int32 LE).
        slice_start = nodes_off + lo * 8
        slice_end = nodes_off + hi * 8
        shard_path = output_dir / f"nodes-scaled-{shard_idx:03d}.bin"
        shard_path.write_bytes(idx_v1_bytes[slice_start:slice_end])
        shard_paths.append(shard_path)

    # 2) Build v2 header (40 bytes) and concat with the rest of the v1
    # body (cell_meta + name_offsets + names_blob — already correctly
    # ordered after nodes_scaled in v1).
    new_header = SZCI_MAGIC + struct.pack(
        "<7I 2I",
        SZCI_VERSION_SHARDED,
        num_nodes, num_edges,
        num_names, names_bytes,
        num_cells,
        cell_scale_signed if cell_scale_signed >= 0 else 0,
        num_node_shards,
        nodes_per_shard,
    )
    v2_bytes = new_header + idx_v1_bytes[after_nodes:]
    return v2_bytes, shard_paths


# --- main upgrade pass ------------------------------------------------------


def _detect_overture_in_search_data(src: Archive, *, sample_chunks: int = 16,
                                    sample_records: int | None = None) -> list[str]:
    """Sample search-data chunks in the source ZIM for ``"source":"overture"``
    markers. Returns inferred theme list (subset of ``["addresses","places"]``).

    Used by the salvage-build retrofit: a build run with
    ``--skip-address-extract`` ships search records sourced from a prior
    Overture merge but never writes ``overture-sources.json`` and never
    sets ``streetzim-meta.json:hasOvertureAddresses``. zimcheck flags the
    static link in ``index.html`` even though the runtime conditional
    keeps it hidden — and downstream users miss the Overture credit.
    Sampling the search chunks lets the upgrader auto-correct both.
    """
    themes: set[str] = set()
    chunks_seen = 0
    for i in range(src.entry_count):
        if chunks_seen >= sample_chunks:
            break
        try:
            e = src._get_entry_by_id(i)
            p = e.path
        except Exception:
            continue
        if not (p.startswith("search-data/") and p.endswith(".json")):
            continue
        if p == "search-data/manifest.json":
            continue
        chunks_seen += 1
        try:
            records = json.loads(bytes(e.get_item().content).decode("utf-8"))
        except Exception:
            continue
        # Search-data chunks use abbreviated keys (``t`` = type, ``s`` =
        # subtype, ``a``/``o`` = lat/lon, ``cat``, ``source``). The chunker
        # in create_osm_zim.py shortens keys to shrink chunk size. The
        # salvage-cache jsonl uses long-form keys; sample both so this
        # function works on either input. Records are typically sorted
        # alphabetically inside a chunk, so overture POIs (often
        # mid-alphabet) won't appear in the first 200; scan the whole
        # chunk by default and let the early-exit short-circuit save time.
        record_iter = (records[:sample_records]
                       if sample_records is not None else records)
        for r in record_iter:
            if not isinstance(r, dict):
                continue
            if r.get("source") != "overture":
                continue
            t = r.get("t") or r.get("type")
            if t == "poi":
                themes.add("places")
            if (r.get("addr") or r.get("housenumber") or r.get("street")
                    or r.get("h")):
                themes.add("addresses")
            if "places" in themes and "addresses" in themes:
                return sorted(themes)
    return sorted(themes)


def _build_overture_stub(themes: list[str]) -> bytes:
    """Build the placeholder overture-sources.json content for salvage
    rebuilds where the upstream dataset list isn't retained."""
    if themes == ["addresses"]:
        themes_phrase = ("Address data is derived from the Overture "
                         "addresses theme")
    elif themes == ["places"]:
        themes_phrase = ("Place info (POIs, websites, phones, socials, "
                         "brand, categories) is derived from the Overture "
                         "places theme")
    else:
        themes_phrase = ("Address + place info (POIs, websites, phones, "
                         "socials, brand, categories) are derived from the "
                         "Overture addresses + places themes")
    doc = {
        "release": "2026-04-15.0",
        "themes": themes,
        "attribution": (
            "© OpenStreetMap contributors and Overture Maps "
            f"Foundation (overturemaps.org). {themes_phrase}; "
            "see canonicalCredits URL for the upstream dataset list."
        ),
        "datasets": [],
        "_note": ("Salvage rebuild — upstream dataset list not retained "
                  "from prior search cache. The data is present in this "
                  "ZIM's search index, but the per-feed list lives in the "
                  "original Overture parquet metadata which the salvage "
                  "cache didn't preserve."),
        "canonicalCredits": "https://docs.overturemaps.org/attribution/",
    }
    return json.dumps(doc, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def upgrade(src_path: str, dst_path: str, *,
            split_hot_search_chunks_mb: int = 10,
            keep_spill: bool = False,
            inject_overture_sources: bool = False,
            swap_viewer: bool = True,
            ) -> None:
    src_path = str(src_path)
    dst_path = str(dst_path)
    print(f"=== upgrade {src_path} → {dst_path}", flush=True)
    src = Archive(src_path)
    print(f"  source: {os.path.getsize(src_path)/1e9:.2f} GB, "
          f"{src.entry_count} entries", flush=True)

    preferred_tmp = Path("/storage/streetzim/tmp")
    spill = Path(tempfile.mkdtemp(
        prefix="upgrade_spill_",
        dir=str(preferred_tmp) if preferred_tmp.is_dir() else None,
    ))
    print(f"  spill dir: {spill}", flush=True)

    threshold_bytes = split_hot_search_chunks_mb * 1024 * 1024

    # ---- Phase 1: read v1 SZCI, build v2 SZCI + node shards ----------------
    print(f"  reading v1 SZCI ...", flush=True)
    t0 = time.time()
    idx_entry = src.get_entry_by_path("routing-data/graph-cells-index.bin")
    idx_v1_bytes = bytes(idx_entry.get_item().content)
    print(f"    v1 SZCI: {len(idx_v1_bytes)/1e6:.1f} MB read in "
          f"{time.time()-t0:.1f}s", flush=True)

    t0 = time.time()
    v2_bytes, shard_paths = _rewrite_szci_v1_to_v2(idx_v1_bytes, spill)
    del idx_v1_bytes  # free
    shard_total_mb = sum(p.stat().st_size for p in shard_paths) / 1e6
    print(f"    v2 SZCI: {len(v2_bytes)/1e6:.1f} MB index + "
          f"{len(shard_paths)} shards ({shard_total_mb:.1f} MB total) "
          f"in {time.time()-t0:.1f}s", flush=True)

    # ---- Phase 2: scan source for oversized search chunks + manifest -------
    print(f"  scanning search-data ...", flush=True)
    t0 = time.time()
    captured_search_manifest: dict | None = None
    captured_search_chunks: dict[str, bytes] = {}
    replaced_search_paths: set[str] = set()
    n_search_chunks = 0
    biggest_pre = 0
    for i in range(src.entry_count):
        try:
            e = src._get_entry_by_id(i)
            p = e.path
        except Exception:
            continue
        if p == "search-data/manifest.json":
            replaced_search_paths.add(p)
            captured_search_manifest = json.loads(
                bytes(e.get_item().content).decode("utf-8")
            )
            continue
        if not (p.startswith("search-data/") and p.endswith(".json")):
            continue
        n_search_chunks += 1
        size = e.get_item().size
        if size > biggest_pre:
            biggest_pre = size
        if size > threshold_bytes:
            replaced_search_paths.add(p)
            # Strip ``search-data/`` prefix and ``.json`` suffix to
            # recover the chunk's logical prefix (e.g. ``ro-0-0-0``).
            prefix = p[len("search-data/"):-len(".json")]
            captured_search_chunks[prefix] = bytes(e.get_item().content)
    print(f"    {n_search_chunks} chunks; biggest {biggest_pre/1e6:.1f} MB; "
          f"will resplit {len(captured_search_chunks)} oversized "
          f"(threshold {split_hot_search_chunks_mb} MB) "
          f"in {time.time()-t0:.1f}s", flush=True)

    # ---- Phase 2b: detect overture content for stub injection -------------
    overture_stub_themes: list[str] = []
    overture_stub_bytes: bytes | None = None
    patched_meta_bytes: bytes | None = None
    has_existing_overture_sources = False
    try:
        src.get_entry_by_path("overture-sources.json")
        has_existing_overture_sources = True
    except Exception:
        pass
    if inject_overture_sources and not has_existing_overture_sources:
        t0 = time.time()
        overture_stub_themes = _detect_overture_in_search_data(src)
        if overture_stub_themes:
            overture_stub_bytes = _build_overture_stub(overture_stub_themes)
            print(f"  overture content detected (themes={overture_stub_themes}); "
                  f"will inject overture-sources.json "
                  f"({len(overture_stub_bytes)} B) and patch streetzim-meta.json "
                  f"in {time.time()-t0:.1f}s", flush=True)
            # Patch streetzim-meta.json: set hasOvertureAddresses=True so
            # the viewer's runtime conditional un-hides the credits section
            # (otherwise the JSON we just emitted would never be reachable
            # via the static link in index.html).
            try:
                meta_entry = src.get_entry_by_path("streetzim-meta.json")
                meta = json.loads(bytes(meta_entry.get_item().content)
                                  .decode("utf-8"))
                meta["hasOvertureAddresses"] = True
                patched_meta_bytes = json.dumps(
                    meta, separators=(",", ":"),
                    ensure_ascii=False).encode("utf-8")
                replaced_search_paths.add("streetzim-meta.json")
            except Exception as ex:
                print(f"  warning: could not patch streetzim-meta.json: {ex}",
                      flush=True)
        else:
            print(f"  no overture content detected in sample; skipping stub "
                  f"injection (took {time.time()-t0:.1f}s)", flush=True)
    elif inject_overture_sources and has_existing_overture_sources:
        print(f"  overture-sources.json already present in source; "
              f"no injection needed", flush=True)

    # ---- Phase 2c: collect viewer replacements ----------------------------
    # The upgrader normally LazyPassthroughs every entry byte-for-byte,
    # which means the source's viewer index.html stays baked in. When the
    # source is older than the current resources/viewer/ on disk (e.g.
    # the SZCI v2 viewer fix from 2026-05-05 that lets iOS read sharded
    # nodes_scaled), the upgraded ZIM ships the stale viewer and users hit
    # the same bug we already fixed. Mirrors repackage_zim.py's
    # ``--no-swap-viewer`` convention: default-on, opt out for forensic
    # repros.
    viewer_replacements: dict[str, bytes] = {}
    if swap_viewer:
        viewer_dir = REPO / "resources" / "viewer"
        for name in ("index.html", "places.html", "routing-worker.js"):
            disk = viewer_dir / name
            if not disk.exists():
                continue
            data = disk.read_bytes()
            viewer_replacements[name] = data
            replaced_search_paths.add(name)
            print(f"  will add/swap {name} ← {disk} ({len(data)} B)", flush=True)

    # ---- Phase 3: emit output ZIM -----------------------------------------
    if os.path.exists(dst_path):
        print(f"  removing existing {dst_path}", flush=True)
        os.remove(dst_path)
    print(f"  starting libzim Creator ...", flush=True)
    creator = Creator(dst_path)
    # python-libzim API: ``config_compression`` + ``config_clustersize``
    # (no ``configure_*`` aliases in current binding).
    try:
        creator.config_compression(zstd_level=int(os.environ.get("ZSTD_CLEVEL", "22")))
    except Exception:
        # zstd_level kwarg may not be supported — fall back to default.
        try:
            creator.config_compression()
        except Exception:
            pass
    try:
        creator.config_clustersize(2 * 1024 * 1024 * 1024)
    except Exception:
        pass
    # Re-enable native libzim fulltext + title indexing so the upgraded
    # ZIM has the same `fulltext/xapian` and `listing/titleOrdered/v1`
    # entries the source had. The original v1 ZIM was built with
    # config_indexing on; copying its existing Xapian DB through
    # add_item segfaults libzim during finalize ("set index" phase),
    # so we rebuild from scratch instead. Language is read from the
    # source's Language metadata (ISO-639-3, e.g. "eng").
    try:
        src_lang = bytes(src.get_metadata("Language")).decode().strip() or "eng"
    except Exception:
        src_lang = "eng"
    print(f"  config_indexing(True, {src_lang!r}) — rebuild Xapian fulltext + title index", flush=True)
    creator.config_indexing(True, src_lang)
    # Mainpath = same as source's main entry path.
    if src.has_main_entry:
        main = src.main_entry
        while main.is_redirect:
            main = main.get_redirect_entry()
        creator.set_mainpath(main.path)

    with creator as c:
        # 3a) Carry over metadata (Title, Description, ...).
        for k in ("Title", "Description", "Language", "Creator", "Publisher",
                  "Date", "Tags", "Name", "Flavour", "Scraper", "License"):
            try:
                v = src.get_metadata(k)
                if isinstance(v, bytes):
                    c.add_metadata(k, v.decode("utf-8", errors="replace"))
                else:
                    c.add_metadata(k, v)
            except Exception:
                pass
        try:
            illus = src.get_metadata("Illustration_48x48@1")
            if illus:
                c.add_metadata("Illustration_48x48@1", illus)
        except Exception:
            pass

        # 3b) New v2 SZCI index (bytes — small).
        c.add_item(PassthroughItem(
            "routing-data/graph-cells-index.bin",
            "Routing Cells Index",
            "application/octet-stream",
            v2_bytes,
            compress=True,
        ))
        # 3c) nodes-scaled-NNN.bin shards (FileProvider — disk-backed).
        for shard_path in shard_paths:
            basename = os.path.basename(shard_path)
            c.add_item(FilePathItem(
                f"routing-data/{basename}",
                f"Routing Nodes Shard {basename}",
                "application/octet-stream",
                shard_path,
                compress=True,
            ))

        # 3d) Recursively re-split oversized search chunks. Reuses the
        # same code path repackage_zim.py uses on fresh builds; emits
        # leaves into ``search-data/{leaf-prefix}.json`` and tracks
        # ``sub_chunks[prefix]`` for the manifest update below.
        new_manifest = None
        if captured_search_manifest is not None:
            new_manifest = {
                "chunks": dict(captured_search_manifest.get("chunks", {})),
                "sub_chunks": dict(captured_search_manifest.get("sub_chunks", {})),
            }
            for k, v in captured_search_manifest.items():
                if k in ("chunks", "sub_chunks"):
                    continue
                new_manifest[k] = v
            for prefix, raw in captured_search_chunks.items():
                try:
                    records = json.loads(raw.decode("utf-8"))
                except Exception as ex:
                    print(f"  warning: chunk {prefix} unparseable: {ex}; "
                          "passing through", flush=True)
                    c.add_item(PassthroughItem(
                        f"search-data/{prefix}.json",
                        f"search chunk {prefix}",
                        "application/json", raw, compress=True))
                    continue
                leaves = _split_records_recursive(
                    records, prefix, threshold_bytes,
                    n_buckets=16, max_depth=5,
                )
                if len(leaves) == 1 and leaves[0][0] == prefix:
                    # Already small (shouldn't happen given the size gate
                    # above, but be safe).
                    c.add_item(PassthroughItem(
                        f"search-data/{prefix}.json",
                        f"search chunk {prefix}",
                        "application/json", leaves[0][1], compress=True))
                    new_manifest["chunks"][prefix] = len(records)
                    continue
                sub_prefix_list = []
                max_leaf_mb = 0.0
                for sub_prefix, sub_bytes in leaves:
                    sub_prefix_list.append(sub_prefix)
                    c.add_item(PassthroughItem(
                        f"search-data/{sub_prefix}.json",
                        f"search chunk {sub_prefix}",
                        "application/json", sub_bytes, compress=True))
                    try:
                        leaf_count = len(json.loads(sub_bytes.decode("utf-8")))
                    except Exception:
                        leaf_count = 0
                    new_manifest["chunks"][sub_prefix] = leaf_count
                    mb = len(sub_bytes) / 1e6
                    if mb > max_leaf_mb:
                        max_leaf_mb = mb
                new_manifest["chunks"].pop(prefix, None)
                new_manifest["sub_chunks"][prefix] = sub_prefix_list
                print(f"  split {prefix!r} ({len(records):,} records, "
                      f"{len(raw)/1e6:.0f} MB) → {len(sub_prefix_list)} "
                      f"leaves (largest {max_leaf_mb:.1f} MB)", flush=True)

            # Updated manifest (always write — sub_chunks may have grown
            # even if chunks didn't shrink past threshold).
            c.add_item(PassthroughItem(
                "search-data/manifest.json",
                "search manifest",
                "application/json",
                json.dumps(new_manifest, separators=(",", ":")).encode("utf-8"),
                compress=True,
            ))

        # 3d.1) Inject overture-sources.json + patched streetzim-meta.json
        # for salvage-build retrofits where the original create_osm_zim.py
        # run skipped both because it didn't go through the merge_overture_*
        # code path. See _detect_overture_in_search_data above.
        if overture_stub_bytes is not None:
            c.add_item(PassthroughItem(
                "overture-sources.json",
                "Overture Dataset Credits",
                "application/json",
                overture_stub_bytes,
                compress=True,
            ))
            print(f"  injected overture-sources.json "
                  f"({len(overture_stub_bytes)} B)", flush=True)
        if patched_meta_bytes is not None:
            c.add_item(PassthroughItem(
                "streetzim-meta.json",
                "StreetZim Meta",
                "application/json",
                patched_meta_bytes,
                compress=True,
            ))
            print(f"  patched streetzim-meta.json "
                  f"(set hasOvertureAddresses=True)", flush=True)

        # 3d.2) Refresh viewer HTML from disk so the upgraded ZIM picks
        # up viewer fixes (e.g. SZCI v2 reader for iOS, Sources panel
        # tweaks). Skipped under ``--no-swap-viewer``.
        for name, data in viewer_replacements.items():
            mime = ("application/javascript"
                    if name.endswith(".js") else "text/html")
            c.add_item(PassthroughItem(
                name, name, mime, data, compress=True,
            ))
            print(f"  swapped {name} ({len(data)} B from disk)", flush=True)

        # 3e) Lazy passthrough for everything else. metadata entries,
        # the v1 cells index, and the items we re-emitted are all in
        # ``replaced_search_paths`` or are filtered out below.
        replaced_search_paths.add("routing-data/graph-cells-index.bin")
        replaced_search_paths.add("search-data/manifest.json")
        copied = 0
        skipped_meta = 0
        for i in range(src.entry_count):
            try:
                e = src._get_entry_by_id(i)
                p = e.path
            except Exception:
                continue
            if p in replaced_search_paths:
                continue
            # Skip ZIM-internal metadata entries — those come through
            # via add_metadata above. Their paths start with "M/" or
            # are the well-known "Counter", "Title", etc.
            if not p or p == "Counter" or p.startswith("M/"):
                skipped_meta += 1
                continue
            # Skip redirects — libzim handles those via add_redirection,
            # but our minimal upgrader does not preserve redirect targets.
            # In practice the source ZIM has at most a few redirects (e.g.
            # "main" → "index.html"); set_mainpath above covers the main
            # one. If more accumulate we'd need a redirect pass.
            try:
                if e.is_redirect:
                    skipped_meta += 1
                    continue
            except Exception:
                pass
            try:
                item = e.get_item()
                size = item.size
                mime = item.mimetype if hasattr(item, "mimetype") \
                    else "application/octet-stream"
            except Exception:
                continue
            c.add_item(LazyPassthroughItem(
                src, p, e.title or p, mime, size, compress=True))
            copied += 1
            if copied % 100_000 == 0:
                print(f"  ... queued {copied:,} entries", flush=True)
        print(f"  queued {copied:,} entries via LazyPassthrough; "
              f"{skipped_meta} skipped (redirects/metadata)", flush=True)
        print(f"  draining libzim cluster compression "
              f"(this is the long one) ...", flush=True)

    print(f"=== upgrade complete @ {dst_path} "
          f"({os.path.getsize(dst_path)/1e9:.2f} GB)", flush=True)

    if not keep_spill:
        shutil.rmtree(spill, ignore_errors=True)
        print(f"  cleaned spill dir {spill}", flush=True)
    else:
        print(f"  spill dir kept at {spill}", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("src", help="Source spatial ZIM (SZCI v1)")
    p.add_argument("dst", help="Output upgraded ZIM (SZCI v2)")
    p.add_argument("--split-hot-search-chunks-mb", type=int, default=10,
                   metavar="N",
                   help="Threshold in MB for re-splitting search chunks "
                        "(default: 10)")
    p.add_argument("--no-swap-viewer", action="store_true",
                   help="Skip swapping resources/viewer/index.html and "
                        "places.html into the output. Default is to swap "
                        "(matches repackage_zim.py) so iOS-compat viewer "
                        "fixes reach upgraded ZIMs.")
    p.add_argument("--inject-overture-sources", action="store_true",
                   help="If the source ZIM contains overture-tagged search "
                        "records but is missing overture-sources.json, emit "
                        "a stub credits file and set hasOvertureAddresses=True "
                        "in streetzim-meta.json. Fixes the broken-link "
                        "zimcheck flag on salvage-built ZIMs without rebuild.")
    p.add_argument("--keep-spill", action="store_true",
                   help="Keep the spill tmpdir for debugging")
    args = p.parse_args()
    upgrade(args.src, args.dst,
            split_hot_search_chunks_mb=args.split_hot_search_chunks_mb,
            keep_spill=args.keep_spill,
            inject_overture_sources=args.inject_overture_sources,
            swap_viewer=not args.no_swap_viewer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
