#!/usr/bin/env python3
"""Repackage an existing streetzim ZIM with small fixes, without a full rebuild.

Motivation: a full rebuild of Japan-size ZIMs runs ~1–2 hours of
planet-PBF extraction, wikidata fetches, tile downloads, xapian
indexing. A repackage just re-emits the ZIM with targeted tweaks:

  * `routing-data/graph.bin` stays raw (uncompressed) so the PWA's
    in-browser fzstd port doesn't choke on GB-scale ZSTD clusters.
  * Stale embedded viewer HTML (`index.html`, `places.html`) is
    swapped for the current version from `resources/viewer/`.

Everything else — search chunks, tiles, satellite, terrain,
wikidata — is copied byte-for-byte from the source ZIM. Takes ~10–20
min for a 4 GB ZIM, vs ~1–2 h for a full rebuild. Tradeoff: any data
changes that require re-reading the planet PBF or re-running merges
(e.g. Overture places enrichment that wasn't in the source ZIM) are
NOT captured — use a real rebuild for those.

Usage:
  python3 cloud/repackage_zim.py \\
      osm-japan-2026-04-22.zim \\
      osm-japan-2026-04-22-fixed.zim
"""
import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent.resolve()
VIEWER_DIR = SCRIPT_DIR / "resources" / "viewer"

# When this script is run as `python3 cloud/repackage_zim.py …`, sys.path[0]
# is the `cloud/` dir, not the repo root, so `from cloud.chip_rules import …`
# fails with ModuleNotFoundError. Put the repo root at the front so sibling
# package imports resolve.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Eager import so a bad sys.path or missing chip_rules fails fast at
# startup, not 30 minutes into a chip-split run. The 2026-04-24 Japan
# "broken v1" regression was caused by this import living inside the
# split_find_chips branch — by the time it threw ModuleNotFoundError,
# the source manifest had already been skipped, leaving the output
# ZIM without ANY category-index/manifest.json.
from cloud.chip_rules import CHIP_RULES, record_matches_chip  # noqa: E402


def _v4_to_v5_bufs(v4_buf: bytes) -> tuple[bytes, bytes]:
    """Split a v4 SZRG buffer into the v5 main + SZGM companion. Mirrors
    extract_routing_graph(split_graph=True) so repackaged ZIMs are
    format-identical to freshly-built ones."""
    import struct
    if v4_buf[:4] != b"SZRG":
        raise ValueError("Not an SZRG buffer")
    version, num_nodes, num_edges, num_geoms, geom_bytes, num_names, names_bytes = \
        struct.unpack_from("<7I", v4_buf, 4)
    if version != 4:
        raise ValueError(f"expected SZRG v4 to upgrade; got v{version}")

    edge_stride = 5
    off = 32
    nodes_len = num_nodes * 2 * 4
    adj_len = (num_nodes + 1) * 4
    edges_len = num_edges * edge_stride * 4
    geom_offsets_len = (num_geoms + 1) * 4
    name_offsets_len = (num_names + 1) * 4

    nodes = v4_buf[off:off + nodes_len]; off += nodes_len
    adj = v4_buf[off:off + adj_len]; off += adj_len
    edges = v4_buf[off:off + edges_len]; off += edges_len
    geom_offsets = v4_buf[off:off + geom_offsets_len]; off += geom_offsets_len
    geom_blob = v4_buf[off:off + geom_bytes]; off += geom_bytes
    name_offsets = v4_buf[off:off + name_offsets_len]; off += name_offsets_len
    names_blob = v4_buf[off:off + names_bytes]

    main_header = b"SZRG" + struct.pack("<7I",
                                        5, num_nodes, num_edges,
                                        num_geoms, 0,
                                        num_names, names_bytes)
    main_buf = main_header + nodes + adj + edges + name_offsets + names_blob
    szgm_buf = (b"SZGM" + struct.pack("<3I", 1, num_geoms, geom_bytes)
                + geom_offsets + geom_blob)
    return main_buf, szgm_buf


def _chunk_bytes_inmem(buf: bytes, chunk_size: int,
                       prefix: str) -> tuple[list[tuple[str, bytes]], dict]:
    """Chunk an in-memory buffer, returning (entries, manifest) where
    entries is [(name, bytes), ...]. Same manifest schema as
    create_osm_zim.chunk_graph_file so the reader needs no branching."""
    import hashlib
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    entries: list[tuple[str, bytes]] = []
    manifest_chunks: list[dict] = []
    for i in range(0, len(buf), chunk_size):
        data = buf[i:i + chunk_size]
        name = f"{prefix}-{i // chunk_size:04d}.bin"
        entries.append((name, data))
        manifest_chunks.append({"path": name, "bytes": len(data)})
    manifest = {
        "schema": 1,
        "total_bytes": len(buf),
        "sha256": hashlib.sha256(buf).hexdigest(),
        "chunks": manifest_chunks,
    }
    return entries, manifest


def _sub_bucket_for_name(name: str, n_buckets: int) -> int:
    """Deterministic, language-agnostic hash mapping a record's name to
    one of ``n_buckets`` sub-chunks. Must match the client-side logic
    in resources/viewer/index.html (``subBucketFor``) and Swift
    (``Geocoder.subBucketFor``).

    Uses FNV-1a 32-bit hash over the UTF-8 bytes of the full name —
    cheap, no external deps, and reproducible across Python / JS / Swift
    to the bit.
    """
    h = 0x811C9DC5  # FNV offset basis (32-bit)
    for b in name.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF  # FNV prime
    return h % n_buckets


def _emit_split_search(creator, manifest: dict, hot_chunks: dict[str, bytes],
                       *, n_sub_buckets: int, threshold_mb: int,
                       passthrough_cls, src_arc) -> None:
    """Re-emit search-data with hot chunks sub-split by name-hash.

    - Any prefix whose chunk bytes we captured above goes through the
      split path: records are bucketed by FNV-1a hash of the record's
      ``n`` field, written as ``search-data/{prefix}-{0..f}.json``.
    - The new manifest replaces the old ``chunks[prefix] = count`` entry
      with ``chunks[prefix-0] = sub_count`` etc., and records the
      original prefix → [sub_prefixes] in a new ``sub_chunks`` section
      so clients know which queries to spread across sub-files.
    """
    import hashlib
    new_manifest = {
        "chunks": dict(manifest.get("chunks", {})),
        "sub_chunks": dict(manifest.get("sub_chunks", {})),
    }
    for key, value in manifest.items():
        if key in ("chunks", "sub_chunks"):
            continue
        new_manifest[key] = value

    split_total_emitted = 0
    for prefix, raw in hot_chunks.items():
        try:
            records = json.loads(raw.decode("utf-8"))
        except Exception as ex:
            print(f"  warning: hot chunk {prefix} unparseable: {ex}; "
                  "keeping original")
            creator.add_item(passthrough_cls(
                f"search-data/{prefix}.json", f"search chunk {prefix}",
                "application/json", raw,
                compress=True,
            ))
            continue
        # Bucket records by hash.
        buckets: list[list] = [[] for _ in range(n_sub_buckets)]
        for rec in records:
            name = rec.get("n", "") or ""
            buckets[_sub_bucket_for_name(name, n_sub_buckets)].append(rec)

        hex_width = len(format(n_sub_buckets - 1, "x"))
        sub_prefix_list: list[str] = []
        for i, bucket in enumerate(buckets):
            if not bucket:
                continue
            sub_prefix = f"{prefix}-{format(i, f'0{hex_width}x')}"
            sub_prefix_list.append(sub_prefix)
            sub_bytes = json.dumps(bucket, separators=(",", ":"),
                                   ensure_ascii=False).encode("utf-8")
            creator.add_item(passthrough_cls(
                f"search-data/{sub_prefix}.json",
                f"search chunk {sub_prefix}",
                "application/json", sub_bytes,
                compress=True,
            ))
            new_manifest["chunks"][sub_prefix] = len(bucket)
            split_total_emitted += 1
        # Drop the original prefix entry from chunks[] and note the split
        # in sub_chunks[].
        new_manifest["chunks"].pop(prefix, None)
        new_manifest["sub_chunks"][prefix] = sub_prefix_list
        print(f"  split {prefix!r} ({len(records):,} records, "
              f"{len(raw)/1e6:.0f} MB) → {len(sub_prefix_list)} sub-chunks")

    # Emit updated manifest (now covers remaining passthroughs too). We
    # count on the passthrough loop having SKIPPED every hot chunk + the
    # manifest itself (so we re-emit here).
    creator.add_item(passthrough_cls(
        "search-data/manifest.json",
        "search manifest",
        "application/json",
        json.dumps(new_manifest, separators=(",", ":")).encode("utf-8"),
        compress=True,
    ))
    print(f"  search-data manifest rewritten; "
          f"{split_total_emitted} sub-chunks emitted across "
          f"{len(hot_chunks)} split prefixes; threshold {threshold_mb} MB")


def _emit_spatial_graph(creator, graph_path: str | Path, *,
                        cell_scale: int,
                        passthrough_cls,
                        file_passthrough_cls,
                        spill_dir: str | Path) -> None:
    """Convert a spilled SZRG v4/v5 file into the spatial SZCI + SZRC layout
    and add all pieces as ZIM entries.

    Streaming throughout: ``graph_path`` is the on-disk routing graph
    captured during passthrough (avoids the 8.6 GB bytes object), and
    ``spill_dir`` receives the per-cell SZRC files as they're built.
    Cells are then added to the ZIM via ``file_passthrough_cls`` (libzim
    ``FileProvider``) so libzim streams the bytes off disk without ever
    materializing them in the producer queue.

    Memory profile on continent-scale (US): ~12 GB peak vs ~80 GB for
    the legacy in-memory path.
    """
    # Import lazily so this module stays usable in lightweight contexts
    # that don't need the spatial chunker (e.g., viewer-only repackage).
    import sys
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from tests.szrg_reader import load_from_file
    from tests.szrg_spatial import build_spatial

    g = load_from_file(graph_path)
    # v5 in-memory parse yielded has_geoms=False; if the source was v5
    # split we'd need to attach the SZGM companion first. Not supported
    # in the repackage path yet — fail loud so the user knows.
    if g.version == 5 and not g.has_geoms:
        raise RuntimeError(
            "spatial chunking of a v5-split source requires the SZGM "
            "companion too; pass a v4 source graph instead"
        )

    spill_dir = Path(spill_dir)
    idx_bytes, cell_paths, meta = build_spatial(
        g, cell_scale=cell_scale, output_dir=spill_dir,
    )
    # Free the parsed graph eagerly — we still hold idx_bytes (small) and
    # the on-disk cell files. The numpy arrays under `g` reference the
    # source bytes via views; dropping `g` lets the source bytes go too.
    del g

    idx_mb = len(idx_bytes) / 1e6
    cell_sizes_mb = [os.path.getsize(p) / 1e6 for p in cell_paths.values()]
    total_cell_mb = sum(cell_sizes_mb)
    max_cell_mb = max(cell_sizes_mb) if cell_sizes_mb else 0.0
    print(f"  → spatial (cell_scale={cell_scale}): index {idx_mb:.1f} MB + "
          f"{meta['num_cells']} cells totalling {total_cell_mb:.1f} MB "
          f"(max cell {max_cell_mb:.1f} MB) — streamed via {spill_dir}")

    # Index — eager, compress when small enough for fzstd.
    compress_idx = idx_mb < 200
    creator.add_item(passthrough_cls(
        "routing-data/graph-cells-index.bin",
        "Routing Cells Index",
        "application/octet-stream",
        idx_bytes,
        compress=compress_idx,
    ))

    # Each cell — compress individually; cells are smaller than the
    # monolithic graph so fzstd handles them comfortably. Add via
    # FileProvider so libzim reads from disk, not RAM.
    for cid in sorted(cell_paths.keys()):
        path = cell_paths[cid]
        size = os.path.getsize(path)
        compress_cell = size < 200 * 1024 * 1024
        creator.add_item(file_passthrough_cls(
            f"routing-data/graph-cell-{cid:05d}.bin",
            f"Routing Graph Cell {cid}",
            "application/octet-stream",
            path,
            compress=compress_cell,
        ))


def _emit_upgraded_graph(creator, graph_bytes: bytes, *,
                         split_graph: bool, chunk_graph_mb: int,
                         passthrough_cls) -> None:
    """Write the (possibly split, possibly chunked) routing graph entries
    to an open Creator. Guards: split_graph requires a v4 source; chunking
    operates on whatever main/companion files we just produced."""
    import json
    import struct

    if split_graph:
        main_buf, szgm_buf = _v4_to_v5_bufs(graph_bytes)
        print(f"  → v5 split: main {len(main_buf)/1e6:.1f} MB + geoms {len(szgm_buf)/1e6:.1f} MB")
    else:
        main_buf = graph_bytes
        szgm_buf = None
        # Sanity: the source needs to be a SZRG of any supported version
        if main_buf[:4] != b"SZRG":
            print("  warning: routing graph doesn't start with SZRG magic")

    chunk_size = chunk_graph_mb * 1024 * 1024 if chunk_graph_mb > 0 else 0

    def emit_blob(name: str, title: str, mime: str, data: bytes,
                  chunk_prefix: str, manifest_name: str,
                  manifest_title: str):
        # Always emit the monolithic blob — Kiwix iOS / Desktop /
        # mcpzim all read ``routing-data/graph.bin`` natively via
        # libzim's cluster decompression (no fzstd dependency, so
        # the 500 MB PWA cap doesn't apply). Skipping this entry
        # broke iOS routing on Egypt 2026-04-24 and Iran 2026-04-24.
        #
        # When the data exceeds the fzstd per-cluster ceiling we ALSO
        # emit chunks for the PWA. PWA prefers the manifest; Kiwix
        # ignores chunks. Both coexist cheaply (~30% ZIM-size cost
        # for the routing data, which itself is ~5% of a typical ZIM).
        compress = len(data) < 200 * 1024 * 1024
        creator.add_item(passthrough_cls(
            name, title, mime, data, compress=compress))
        print(f"    + {name} ({len(data)/1e6:.1f} MB, "
              f"{'compressed' if compress else 'raw'})")

        if chunk_size > 0 and len(data) > chunk_size:
            entries, manifest = _chunk_bytes_inmem(data, chunk_size, chunk_prefix)
            creator.add_item(passthrough_cls(
                manifest_name, manifest_title, "application/json",
                json.dumps(manifest, separators=(",", ":")).encode("utf-8"),
                compress=True))
            for ename, edata in entries:
                c2 = len(edata) < 200 * 1024 * 1024
                creator.add_item(passthrough_cls(
                    f"routing-data/{ename}",
                    f"{title} ({ename})",
                    "application/octet-stream", edata,
                    compress=c2))
            print(f"    + {name} ALSO chunked → {len(entries)} entries "
                  f"of ≤{chunk_graph_mb} MB each (for PWA fzstd cap)")

    emit_blob(
        "routing-data/graph.bin",
        "Routing Graph",
        "application/octet-stream",
        main_buf,
        chunk_prefix="graph-chunk",
        manifest_name="routing-data/graph-chunk-manifest.json",
        manifest_title="Routing Graph Manifest",
    )
    if szgm_buf is not None:
        emit_blob(
            "routing-data/graph-geoms.bin",
            "Routing Graph Geoms",
            "application/octet-stream",
            szgm_buf,
            chunk_prefix="graph-geoms-chunk",
            manifest_name="routing-data/graph-geoms-chunk-manifest.json",
            manifest_title="Routing Geoms Manifest",
        )


def repackage(src_path: str, dst_path: str,
              swap_viewer: bool = True,
              uncompress_graph: bool = True,
              split_graph: bool = False,
              chunk_graph_mb: int = 0,
              spatial_chunk_scale: int = 0,
              split_hot_search_chunks_mb: int = 0,
              refresh_terrain_dir: str | None = None,
              unchunk_graph: bool = False,
              split_find_chips: bool = False,
              rewrite_search_links: bool = True,
              drop_llm_bundle: bool = True,
              chip_split_threshold_mb: int = 10) -> int:
    from libzim.reader import Archive
    from libzim.writer import (
        Creator, Item, StringProvider, FileProvider, Hint,
        ContentProvider, Blob,
    )

    src = Archive(src_path)
    print(f"  Source:   {src_path} ({os.path.getsize(src_path)/1024/1024:.1f} MB)")
    print(f"  Entries:  {src.entry_count}")
    print(f"  Target:   {dst_path}")
    print(f"  swap viewer: {swap_viewer}")
    print(f"  uncompress routing-data/graph.bin: {uncompress_graph}")
    print(f"  split graph (v5): {split_graph}")
    print(f"  chunk graph MB:   {chunk_graph_mb}")
    print(f"  spatial chunk scale: {spatial_chunk_scale} "
          f"(0=disabled; 10 = 0.1° cells, 1 = 1° cells)")
    if refresh_terrain_dir:
        print(f"  refresh terrain tiles from: {refresh_terrain_dir}")

    # Collect viewer replacements (only for known paths — anything
    # else passes through from the source ZIM unchanged).
    replacements = {}
    if swap_viewer:
        for name in ("index.html", "places.html"):
            p = VIEWER_DIR / name
            if p.exists():
                replacements[name] = p.read_bytes()
                print(f"  will swap {name} ← {p} ({len(replacements[name])} B)")

    class PassthroughItem(Item):
        """An item copied from the source ZIM, preserving its bytes."""
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
        """An item whose content lives on disk — libzim ``FileProvider``
        reads bytes lazily during cluster compression, so the producer
        side never holds the payload in memory. Used for the spatial
        cell files (~hundreds of MB each on continent-scale graphs)."""
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
        """Reads an entry's bytes from the source archive only when libzim
        invokes ``gen_blob()`` during cluster compression. The producer-
        side queue therefore holds just (src, path, size) for each entry
        instead of the bytes — for a 49 GB US source with 30 M+ entries
        that drops queue RSS from ~80 GB to a few hundred MB.

        Source Archive must outlive the libzim Creator's __exit__ (it
        does — ``src`` is a function-scope local that stays alive until
        ``repackage`` returns).

        Note: implements ``gen_blob`` (generator), not ``feed``. The
        Cython binding's ``feed`` override doesn't dispatch to Python
        subclasses cleanly and segfaults on ``__exit__`` when called
        from compression worker threads."""
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
        """Passthrough item that defers reading the source bytes to
        ``feed()`` time via :class:`LazyZimEntryProvider`. Use for any
        entry whose content is being copied byte-for-byte; switch to
        :class:`PassthroughItem` only when the bytes have to be
        modified before emit (viewer swap, search-link rewrite,
        terrain-tile refresh)."""
        def __init__(self, src_arc, path, title, mimetype, size,
                     compress=True):
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

    # Return the raw metadata bytes. The illustration entry is a PNG,
    # not UTF-8 — decoding would raise and my earlier version silently
    # dropped it, so Kiwix refused the rewrapped ZIM.
    def meta_bytes(key, default=None):
        try:
            v = src.get_metadata(key)
            if isinstance(v, (bytes, bytearray)):
                return bytes(v)
            if isinstance(v, str):
                return v.encode("utf-8")
            return default
        except Exception:
            return default

    def meta_str(key, default=""):
        b = meta_bytes(key, None)
        if b is None:
            return default
        try: return b.decode("utf-8")
        except UnicodeDecodeError: return default

    creator = Creator(dst_path)
    creator.config_indexing(True, "en")
    # Match source cluster size (2 MiB). Individual items with
    # COMPRESS=0 get their own uncompressed cluster regardless.
    creator.config_clustersize(2 * 1024 * 1024)
    # Mirror the source's main page — Kiwix refuses to open ZIMs
    # without one. Most of our builds use ``mainPage`` as a redirect to
    # ``index.html``; but that redirect lives in the ZIM metadata
    # namespace and is unreachable after we passthrough only the content
    # namespace. Resolve the redirect chain up-front and set the main
    # path to the actual content target (``index.html`` for streetzim).
    # Fall back to ``index.html`` when the source has no declared main —
    # all streetzim ZIMs carry it, and Kiwix Desktop needs *something*
    # resolvable to open the book.
    main_path_to_set: str | None = None
    try:
        if src.has_main_entry:
            main = src.main_entry
            while main.is_redirect:
                main = main.get_redirect_entry()
            main_path_to_set = main.path
    except Exception as e:
        print(f"  warning: resolving source main entry: {e}")
    if main_path_to_set is None:
        # Heuristic fallback — confirm the entry exists in the source so
        # we don't set a path that won't resolve in the output either.
        try:
            src.get_entry_by_path("index.html")
            main_path_to_set = "index.html"
        except Exception:
            pass
    if main_path_to_set is not None:
        try:
            creator.set_mainpath(main_path_to_set)
            print(f"  main path: {main_path_to_set!r}")
        except Exception as e:
            print(f"  warning: set_mainpath({main_path_to_set!r}): {e}")
    else:
        print("  warning: no suitable main path found — output ZIM may "
              "fail Kiwix Desktop's book-open check")

    # If we're upgrading the routing-data layout (split to v5, byte-
    # chunked, or spatial-chunked), skip the original graph.bin in the
    # passthrough and emit the new entries after. Capture its bytes first.
    upgrade_graph = (split_graph
                     or chunk_graph_mb > 0
                     or spatial_chunk_scale > 0
                     or unchunk_graph)
    captured_graph_bytes: bytes | None = None

    # Continent-scale graphs (US: 8.6 GB) blow past 100 GB RSS when held
    # as Python bytes through the libzim producer queue + spatial chunker.
    # Spill the captured graph to a tempfile and stream cells through
    # libzim FileProvider; only the spatial path needs this today (other
    # emit paths see <2 GB graphs in practice). spill_dir auto-cleans at
    # the bottom of the function once libzim has consumed the cells.
    needs_disk_spill = upgrade_graph and spatial_chunk_scale > 0
    spill_dir: Path | None = None
    graph_spill_path: Path | None = None
    graph_chunk_spill_paths: dict[str, Path] | None = None
    if needs_disk_spill:
        import tempfile as _tempfile
        spill_dir = Path(_tempfile.mkdtemp(prefix="repackage_spill_"))
        graph_spill_path = spill_dir / "graph.bin"
        graph_chunk_spill_paths = {}
        print(f"  spatial-chunk spill dir: {spill_dir}")

    # Hot-chunk splitting: if a search-data/*.json exceeds the threshold,
    # sub-split by hashing record's first-word-prefix into N buckets.
    # We capture the original chunks up-front, rewrite manifest, then
    # emit the new layout after the passthrough. The sub-chunk mapping
    # goes into the manifest as ``sub_chunks`` so clients can route
    # queries to the right sub-file.
    hot_split_mb = split_hot_search_chunks_mb
    hot_split_N = 16  # 16 sub-buckets per oversized chunk — ~32 MB each
                      # when splitting a 500 MB chunk, well under 50 MB cap
    split_search: bool = hot_split_mb > 0
    captured_search_manifest: dict | None = None
    captured_search_chunks: dict[str, bytes] = {}
    # The original manifest names we're replacing; skip them in the
    # passthrough below so we can emit the split layout at the end.
    replaced_search_paths: set[str] = set()

    kept = 0
    swapped = 0
    raw_clusters = 0
    skipped_routing = 0
    refreshed_terrain = 0
    rewritten_search = 0

    # Per-section timing — printed at the end so future runs can
    # eyeball where the wall-clock is going. Build pipeline is the
    # natural next target for parallelization; knowing whether
    # passthrough, chip emission, graph rewrite, or finalize
    # dominates determines what to optimize first.
    import time as _time
    _t_start = _time.time()
    _section_times: dict[str, float] = {}
    def _tick(name: str):
        now = _time.time()
        _section_times[name] = now - (_section_times.get("_last", _t_start))
        _section_times["_last"] = now
    captured_graph_bytes = None
    # Keyed by the source's chunk path ("routing-data/graph-chunk-0000.bin"
    # etc). Reassembled into a single buffer below when the source
    # shipped chunked but not monolithic.
    captured_graph_chunks: dict | None = None
    with creator as c:
        _tick("setup")
        # Passthrough all entries. The bulk of entries (tiles, search-
        # data chunks, wikidata, terrain when not refreshing) pass
        # through byte-for-byte; we route those to LazyPassthroughItem
        # so libzim reads bytes from the source archive only at cluster-
        # compression time. Without this the producer queue holds the
        # bytes for every queued entry and continent-scale sources
        # (US: 30 M+ entries) push RSS past 80 GB.
        #
        # Eager PassthroughItem is still used for the small subset of
        # entries we actively rewrite (viewer swap, terrain refresh,
        # search-link fix) and for capture branches that read content
        # for spill / hot-chunk-split.
        for i in range(src.entry_count):
            entry = src._get_entry_by_id(i) if hasattr(src, "_get_entry_by_id") else src.get_entry_by_path_id(i)
            # Redirect entries carry no content; we recreate them via Creator.add_redirection
            if entry.is_redirect:
                target = entry.get_redirect_entry()
                try:
                    c.add_redirection(entry.path, entry.title or entry.path, target.path)
                except Exception as e:
                    print(f"    skip redirect {entry.path}: {e}")
                continue
            item = entry.get_item()
            path = entry.path
            mime = item.mimetype
            size = item.size  # cheap — no content read
            title = entry.title or ""
            compress = True
            # Bytes only when a modifier or capture branch needs them.
            # Set to a non-None value to switch this entry to the eager
            # PassthroughItem path at the end of the loop.
            modified_content: bytes | None = None

            # Swap viewer HTML for the current version.
            if swap_viewer and path in replacements:
                modified_content = replacements[path]
                swapped += 1
            # Optionally swap terrain tiles for the filesystem version.
            # Used after ``cloud/fix_stale_terrain_tiles.py`` regenerates
            # cached tiles — ``--refresh-terrain-tiles terrain_cache``
            # lets the repackaged ZIM carry those fresh tiles without a
            # full rebuild. Strict byte-replace: only entries that exist
            # both in source and on disk are swapped; extras are ignored.
            if refresh_terrain_dir and path.startswith("terrain/") and path.endswith(".webp"):
                rel = path[len("terrain/"):]  # e.g. "5/20/12.webp"
                disk = os.path.join(refresh_terrain_dir, rel)
                try:
                    disk_mtime = os.path.getmtime(disk)
                    # Only swap if the filesystem copy is strictly newer
                    # than the source ZIM itself — otherwise we churn
                    # every terrain entry needlessly and inflate
                    # repackage time for no gain.
                    src_mtime = os.path.getmtime(src_path)
                    if disk_mtime > src_mtime:
                        with open(disk, "rb") as fh:
                            modified_content = fh.read()
                        size = len(modified_content)
                        refreshed_terrain += 1
                except (OSError, FileNotFoundError):
                    pass
            # Capture + defer the routing graph when we're upgrading
            # layout — we'll emit split / chunked entries after the
            # passthrough pass so they land contiguously. When spilling
            # to disk (needs_disk_spill), the bytes go straight to
            # tempfiles so they're not held in the producer's RSS.
            if upgrade_graph and path in (
                "routing-data/graph.bin",
                "routing-data/graph-geoms.bin",
                "routing-data/graph-chunk-manifest.json",
                "routing-data/graph-geoms-chunk-manifest.json",
            ):
                if path == "routing-data/graph.bin":
                    if needs_disk_spill:
                        # Stream-copy bytes from the source archive into
                        # the spill file without keeping the buffer alive.
                        with open(graph_spill_path, "wb") as gf:
                            gf.write(bytes(item.content))
                    else:
                        captured_graph_bytes = bytes(item.content)
                skipped_routing += 1
                continue
            if upgrade_graph and (
                path.startswith("routing-data/graph-chunk-")
                or path.startswith("routing-data/graph-geoms-chunk-")
                or path.startswith("routing-data/graph-cell-")
                or path == "routing-data/graph-cells-index.bin"
            ):
                # Accumulate source chunks for later reassembly if the
                # source shipped chunked-only (no graph.bin entry). Each
                # chunk is a contiguous byte range of the original graph
                # so concatenation is byte-exact. Without this the later
                # spatial/upgrade path sees a None graph and SHIPS a ZIM
                # with no routing — caught by validator but only after
                # wasting a full repackage.
                if path.startswith("routing-data/graph-chunk-") and not \
                        path.startswith("routing-data/graph-chunk-manifest"):
                    if needs_disk_spill:
                        chunk_spill = spill_dir / f"src-{path.split('/')[-1]}"
                        with open(chunk_spill, "wb") as cf:
                            cf.write(bytes(item.content))
                        graph_chunk_spill_paths[path] = chunk_spill
                    else:
                        if captured_graph_chunks is None:
                            captured_graph_chunks = {}
                        captured_graph_chunks[path] = bytes(item.content)
                skipped_routing += 1
                continue
            # Hot-chunk splitting: capture the search-data manifest and
            # any oversized chunks, defer their re-emission. Skipped
            # chunks land as sub-files at the end.
            if split_search and path == "search-data/manifest.json":
                try:
                    captured_search_manifest = json.loads(
                        bytes(item.content).decode("utf-8"))
                except Exception as ex:
                    print(f"  warning: couldn't parse search manifest: {ex}")
                    captured_search_manifest = None
                replaced_search_paths.add(path)
                continue
            # When we're emitting a new category-index/manifest.json
            # at the end (with the chip counts), skip the source one so
            # the new one doesn't collide.
            if split_find_chips and path == "category-index/manifest.json":
                continue
            if (split_search and path.startswith("search-data/")
                    and path.endswith(".json")
                    and path != "search-data/manifest.json"
                    and size > hot_split_mb * 1024 * 1024):
                prefix = path[len("search-data/"):-len(".json")]
                captured_search_chunks[prefix] = bytes(item.content)
                replaced_search_paths.add(path)
                continue
            # LLM-bundle drop: addr.json / poi.json / street.json hold
            # the raw category source records (not what the viewer
            # reads — that's chip-X.json). They're a few hundred MB
            # to ~5 GB per region and sit dormant in the ZIM, but
            # phones could OOM if anything ever fetched them
            # accidentally. Default DROP; opt back in with
            # `--include-llm-bundle` for bundle consumers.
            # Don't drop place.json — places.html's reverse-geocode
            # reads it on viewport-origin mode.
            if drop_llm_bundle and path in (
                "category-index/addr.json",
                "category-index/poi.json",
                "category-index/street.json",
            ):
                continue
            # When --split-find-chips is on we re-derive chips from
            # poi.json + park.json below; skip the source chip-*.json
            # passthrough so the new files don't collide with the old
            # at the same paths.
            if (split_find_chips
                    and path.startswith("category-index/chip-")
                    and path.endswith(".json")):
                continue
            # Search-detail link rewrite: every search/<slug>.html
            # baked before 2026-04-26 had bare `href="index.html#..."`
            # which from inside the search/ subdir resolves to
            # `search/index.html` (404). zimcheck flagged hundreds of
            # these as broken internal URLs and Kiwix's library validator
            # marks the whole ZIM as Fail. The fresh-build template now
            # uses `../index.html`; this pass retrofits the same fix
            # into already-shipped ZIMs without a full create_osm_zim
            # rebuild. Cheap: only touches the search/ namespace, all
            # other entries remain byte-for-byte passthrough.
            if (rewrite_search_links and path.startswith("search/")
                    and path.endswith(".html")):
                # Rewrite the two broken hrefs the template emits.
                # Use a literal-bytes match (no regex) so an attacker-
                # controlled name field can't break out of the
                # rewriter and so we don't accidentally touch
                # legitimate `index.html` strings inside <code> blocks.
                src_bytes = bytes(item.content)
                fixed = (src_bytes
                    .replace(b'href="index.html#dest=',
                             b'href="../index.html#dest=')
                    .replace(b'href="index.html#map=',
                             b'href="../index.html#map='))
                if fixed != src_bytes:
                    modified_content = fixed
                    size = len(modified_content)
                    rewritten_search += 1
            # Mark the routing graph uncompressed for PWA-fzstd compat
            # (skipped when we're rewriting the graph anyway, above).
            if uncompress_graph and path == "routing-data/graph.bin":
                compress = False
                raw_clusters += 1
            # Mirror the per-item compress thresholds that
            # `_emit_spatial_graph` applies at fresh-build time, so a
            # passthrough doesn't silently re-compress what was raw.
            # Specifically: graph-cells-index.bin >= 200 MB and any
            # individual graph-cell-*.bin >= 200 MB land in raw
            # clusters. Without this, repacking a Midwest-sized ZIM
            # (212 MB cells-index) produces a compressed cluster that
            # Kiwix Desktop's WebView can't decompress in bounded
            # time — Directions hangs on "loading routing data" and
            # eventually errors out. Source ZIMs that stored these
            # raw work fine; repacks that re-compressed them did not.
            if (path == "routing-data/graph-cells-index.bin"
                    or (path.startswith("routing-data/graph-cell-")
                        and path.endswith(".bin"))):
                if size >= 200 * 1024 * 1024:
                    compress = False
                    raw_clusters += 1
            if modified_content is not None:
                c.add_item(PassthroughItem(
                    path, title, mime, modified_content, compress=compress))
            else:
                c.add_item(LazyPassthroughItem(
                    src, path, title, mime, size, compress=compress))
            kept += 1
            if kept % 50_000 == 0:
                print(f"    copied {kept} entries...")

        # If source shipped chunked graph (no graph.bin), reassemble
        # the chunks into a single buffer/file so the re-emit path below
        # has something to work with. Source chunks are byte-ranges
        # of the original graph, so concatenation is byte-exact.
        if upgrade_graph and needs_disk_spill:
            if not graph_spill_path.exists() and graph_chunk_spill_paths:
                # Stream-concat the chunk files into graph_spill_path
                # without ever holding the full graph in memory.
                with open(graph_spill_path, "wb") as out:
                    for k in sorted(graph_chunk_spill_paths):
                        with open(graph_chunk_spill_paths[k], "rb") as src_chunk:
                            while True:
                                buf = src_chunk.read(64 * 1024 * 1024)
                                if not buf:
                                    break
                                out.write(buf)
                # Source-chunk spill files no longer needed.
                for chunk_path in graph_chunk_spill_paths.values():
                    try:
                        chunk_path.unlink()
                    except OSError:
                        pass
                print(f"  reassembled graph.bin (on disk) from "
                      f"{len(graph_chunk_spill_paths)} source chunks "
                      f"({os.path.getsize(graph_spill_path)/1024/1024:.1f} MB)")
        elif (upgrade_graph and captured_graph_bytes is None
                and captured_graph_chunks):
            parts = [captured_graph_chunks[k]
                     for k in sorted(captured_graph_chunks)]
            captured_graph_bytes = b"".join(parts)
            print(f"  reassembled graph.bin from {len(parts)} source "
                  f"chunks ({len(captured_graph_bytes)/1024/1024:.1f} MB)")

        _tick("passthrough")
        # Re-emit the routing graph in the requested layout.
        graph_missing = (
            (needs_disk_spill and (graph_spill_path is None
                                   or not graph_spill_path.exists()))
            or (not needs_disk_spill and captured_graph_bytes is None)
        )
        if upgrade_graph and graph_missing:
            print("  warning: no routing-data/graph.bin in source — "
                  "nothing to upgrade. Output will have no routing.")
        elif upgrade_graph and unchunk_graph and not split_graph \
                and chunk_graph_mb == 0 and spatial_chunk_scale == 0:
            # --unchunk-graph alone: emit monolithic graph.bin only.
            # Useful to retrofit Kiwix-compat into a chunked-only ZIM
            # whose graph size doesn't actually need chunking (<500MB).
            size_mb = len(captured_graph_bytes) / (1024 * 1024)
            compress = size_mb < 200
            c.add_item(PassthroughItem(
                "routing-data/graph.bin",
                "Routing Graph",
                "application/octet-stream",
                captured_graph_bytes,
                compress=compress,
            ))
            if not compress:
                raw_clusters += 1
            print(f"  emitted monolithic graph.bin ({size_mb:.1f} MB, "
                  f"{'compressed' if compress else 'raw'})")
        elif upgrade_graph and spatial_chunk_scale > 0:
            # Streaming spatial path — graph is on disk, cells are written
            # to disk as they're built, libzim FileProvider reads them
            # lazily during cluster compression. Peak RSS bounded ~12 GB
            # for US-scale (was ~80 GB with the legacy in-memory path).
            cells_dir = spill_dir / "cells"
            _emit_spatial_graph(
                c, graph_spill_path,
                cell_scale=spatial_chunk_scale,
                passthrough_cls=PassthroughItem,
                file_passthrough_cls=FilePathItem,
                spill_dir=cells_dir,
            )
        elif upgrade_graph:
            _emit_upgraded_graph(
                c, captured_graph_bytes,
                split_graph=split_graph,
                chunk_graph_mb=chunk_graph_mb,
                passthrough_cls=PassthroughItem,
            )

        # Re-emit search-data with hot chunks split into sub-buckets.
        # Any chunk > split_hot_search_chunks_mb becomes prefix-0.json
        # … prefix-f.json (hot_split_N buckets, default 16). The manifest
        # gains ``sub_chunks: {prefix: [sub_prefix, ...]}`` so clients
        # know which prefixes were split. Splitting is deterministic
        # per-record via a hash of (record name) so all clients pick the
        # same sub-bucket.
        if split_search and captured_search_manifest is not None:
            _emit_split_search(
                c, captured_search_manifest, captured_search_chunks,
                n_sub_buckets=hot_split_N,
                threshold_mb=hot_split_mb,
                passthrough_cls=PassthroughItem,
                src_arc=src,
            )

        _tick("graph_rewrite")
        # Preserve ZIM-level metadata. Illustration is binary (PNG),
        # everything else text; treat both correctly.
        for k in ("Title", "Description", "Language", "Creator",
                  "Publisher", "Date", "Tags", "Name", "Flavour",
                  "Scraper", "License"):
            v = meta_str(k)
            if v:
                try: c.add_metadata(k, v)
                except Exception as e: print(f"  warning: metadata {k}: {e}")
        # Illustration_48x48@1 is the 48x48 PNG icon Kiwix shows in
        # its library. python-libzim's add_metadata wants bytes for
        # binary fields.
        illus = meta_bytes("Illustration_48x48@1")
        if illus:
            try: c.add_metadata("Illustration_48x48@1", illus)
            except Exception as e: print(f"  warning: illustration: {e}")

        _tick("metadata")
        # Chip-split retrofit: if requested, read the source's
        # category-index/poi.json + park.json, split by Find-page chip
        # via cloud/chip_rules, and emit one chip-{id}.json per chip.
        # places.html fetches chip-* instead of the full poi.json (which
        # is 1 GB on Japan and OOM'd Chrome). Runs AFTER the passthrough
        # so the new entries always get into the output.
        # CHIP_RULES / record_matches_chip are imported at module top
        # so failures surface before any copy work starts.
        if split_find_chips:
            # Load the per-cat bundles the chips pull from.
            records_by_cat: dict[str, list] = {}
            for cat in {chip.from_cat for chip in CHIP_RULES}:
                path = f"category-index/{cat}.json"
                try:
                    raw = bytes(src.get_entry_by_path(path).get_item().content)
                    records_by_cat[cat] = json.loads(raw)
                except Exception:
                    records_by_cat[cat] = []
            new_chips_meta: dict[str, dict] = {}
            for chip in CHIP_RULES:
                src_records = records_by_cat.get(chip.from_cat, [])
                if not src_records:
                    continue
                dst_records = [r for r in src_records
                               if record_matches_chip(r, chip)]
                if not dst_records:
                    continue
                # Note: experimented with dropping the constant `t`
                # field (always "poi" inside a poi-derived chip) —
                # 0.5 % / 75 KB saving after ZSTD22 on a 110 MB JSON.
                # ZSTD eats the repetition. Not worth the schema
                # break; consumers (mcpzim, future viewer code) keep
                # the same shape.
                chip_bytes = json.dumps(dst_records, separators=(",", ":"),
                                         ensure_ascii=False).encode("utf-8")
                threshold_b = chip_split_threshold_mb * 1024 * 1024
                # Sub-bucket fat chips. Japan's restaurants chip was
                # 164 MB and Canada's shops chip 137 MB — both under
                # iOS heap but loading + parsing pushes the page
                # near discard. Split via FNV-1a hash on record name
                # (same scheme as search-data) so phones fetch only
                # the sub-bucket they need. The viewer's loadChipFile
                # reads `manifest.chips[id].sub_chunks` and fans out
                # parallel fetches.
                if (chip_split_threshold_mb > 0 and
                        len(chip_bytes) > threshold_b):
                    n_sub = 1
                    while True:
                        n_sub *= 2
                        buckets = [[] for _ in range(n_sub)]
                        for r in dst_records:
                            name = r.get("n", "") or ""
                            buckets[_sub_bucket_for_name(name, n_sub)].append(r)
                        bucket_blobs = [
                            json.dumps(b, separators=(",", ":"),
                                       ensure_ascii=False).encode("utf-8")
                            for b in buckets
                        ]
                        biggest = max(len(b) for b in bucket_blobs)
                        # Cap depth at 256 sub-buckets — beyond that
                        # the records have low cardinality on `n` and
                        # further splitting won't help.
                        if biggest <= threshold_b or n_sub >= 256:
                            break
                    sub_paths = []
                    hex_w = max(1, len(format(n_sub - 1, "x")))
                    for i, blob in enumerate(bucket_blobs):
                        if not blob or blob == b"[]":
                            continue
                        sub_id = format(i, f"0{hex_w}x")
                        sub_path = f"category-index/chip-{chip.id}-{sub_id}.json"
                        c.add_item(PassthroughItem(
                            sub_path,
                            f"Find chip: {chip.label} (bucket {sub_id})",
                            "application/json",
                            blob,
                            compress=True,
                        ))
                        sub_paths.append(sub_id)
                    print(f"  chip-{chip.id}: {len(dst_records):,} records "
                          f"({len(chip_bytes)/1024/1024:.1f} MB) "
                          f"→ {len(sub_paths)} sub-buckets, "
                          f"biggest {biggest/1024/1024:.1f} MB")
                    new_chips_meta[chip.id] = {
                        "label": chip.label,
                        "count": len(dst_records),
                        "bytes": len(chip_bytes),
                        "sub_chunks": sub_paths,
                        "n_sub_buckets": n_sub,
                    }
                else:
                    entry_path = f"category-index/chip-{chip.id}.json"
                    c.add_item(PassthroughItem(
                        entry_path,
                        f"Find chip: {chip.label}",
                        "application/json",
                        chip_bytes,
                        compress=True,
                    ))
                    new_chips_meta[chip.id] = {
                        "label": chip.label,
                        "count": len(dst_records),
                        "bytes": len(chip_bytes),
                    }
                    print(f"  chip-{chip.id}: {len(dst_records):,} records "
                          f"({len(chip_bytes)/1024/1024:.1f} MB)")
            # Re-emit category-index/manifest.json with the new chips
            # section so places.html can enumerate them.
            try:
                old_mani = json.loads(bytes(
                    src.get_entry_by_path("category-index/manifest.json"
                                          ).get_item().content))
            except Exception:
                old_mani = {}
            old_mani["chips"] = new_chips_meta
            c.add_item(PassthroughItem(
                "category-index/manifest.json",
                "Category Index Manifest",
                "application/json",
                json.dumps(old_mani, separators=(",", ":")).encode("utf-8"),
                compress=True,
            ))

        _tick("chips")
        # Add viewer assets that existed in `replacements` but weren't
        # present in the source ZIM. Several 2026-04-22 regional builds
        # (east-coast-us, australia-nz, west-coast-us) shipped without
        # `places.html`, so clicking "Find" in Kiwix showed "Unable to
        # load the article requested." Adding-on-absence is the same
        # intent as the swap — make the output carry the current viewer
        # set — just generalised to new entries.
        added_missing = 0
        for name, data in replacements.items():
            if name in replaced_search_paths:  # paranoia: never collide
                continue
            try:
                src.get_entry_by_path(name)
                continue  # Already swapped above
            except Exception:
                pass
            mime = "text/html"
            title = "Map" if name == "index.html" else "Find places"
            is_front = (name == "index.html")
            class NewViewerItem(Item):
                def __init__(self, path, data, title, mime, is_front):
                    super().__init__()
                    self._p = path; self._d = data; self._t = title
                    self._m = mime; self._front = is_front
                def get_path(self): return self._p
                def get_title(self): return self._t
                def get_mimetype(self): return self._m
                def get_contentprovider(self):
                    return StringProvider(self._d)
                def get_hints(self):
                    return {Hint.FRONT_ARTICLE: self._front, Hint.COMPRESS: True}
            c.add_item(NewViewerItem(name, data, title, mime, is_front))
            added_missing += 1
            print(f"  added missing {name} from viewer set ({len(data)} B)")

    size_mb = os.path.getsize(dst_path) / (1024 * 1024)
    _tick("finalize")
    extra = f"; {refreshed_terrain} terrain tiles refreshed" if refreshed_terrain else ""
    if rewritten_search:
        extra += f"; {rewritten_search} search detail page(s) link-fixed"
    print(f"\n  kept {kept} entries; {swapped} viewer swaps; "
          f"{added_missing} added; {raw_clusters} raw cluster(s){extra}")
    # Per-section timing — useful when planning what to optimize
    # next. The libzim cluster compressor (run during finalize when
    # the Creator __exit__ flushes pending clusters) usually
    # dominates; passthrough is mostly I/O-bound; chip emission and
    # graph rewrite are CPU/memory-bound but bounded by file size.
    total = sum(v for k, v in _section_times.items() if not k.startswith("_"))
    print(f"\n  per-section wall time:")
    for sect in ("setup", "passthrough", "graph_rewrite", "metadata",
                 "chips", "finalize"):
        secs = _section_times.get(sect, 0.0)
        pct = (100.0 * secs / total) if total > 0 else 0.0
        print(f"    {sect:<14} {secs:>7.1f} s  ({pct:>5.1f}%)")
    print(f"    {'total':<14} {total:>7.1f} s  (100.0%)")
    print(f"  output: {dst_path} ({size_mb:.1f} MB)")

    # Cleanup the spatial-chunk spill dir now that libzim has consumed
    # everything (`with creator as c:` exited above, all FileProvider
    # reads are done).
    if spill_dir is not None:
        import shutil as _shutil
        try:
            _shutil.rmtree(spill_dir, ignore_errors=True)
            print(f"  cleaned spill dir: {spill_dir}")
        except OSError:
            pass

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("src",  help="Source .zim file")
    p.add_argument("dst",  help="Output .zim file")
    p.add_argument("--no-swap-viewer",     action="store_true",
                   help="Don't swap index.html / places.html")
    p.add_argument("--no-uncompress-graph", action="store_true",
                   help="Don't mark routing-data/graph.bin as COMPRESS=0")
    p.add_argument("--split-graph", action="store_true",
                   help="Upgrade SZRG v4 routing graph to v5 split "
                        "(main + geoms companion) so the PWA can defer "
                        "geom loading. Source must be SZRG v4.")
    p.add_argument("--chunk-graph-mb", type=int, default=0, metavar="N",
                   help="Also chunk the routing graph (and geoms companion, "
                        "if --split-graph) into N-MB ZIM entries so each "
                        "lands in its own cluster — avoids fzstd's per-"
                        "cluster cap for continental ZIMs.")
    p.add_argument("--spatial-chunk-scale", type=int, default=0, metavar="N",
                   help="Split the routing graph by N×(1/10)° spatial cells "
                        "(10 = 0.1° cells, 1 = 1° cells). Each cell becomes "
                        "its own ZIM entry — routing loads only the cells "
                        "touched by the frontier, capping peak memory at the "
                        "route's bbox. Supersedes --split-graph / "
                        "--chunk-graph-mb on the main graph when set. "
                        "Source ZIM must carry SZRG v4 (monolithic).")
    p.add_argument("--split-hot-search-chunks-mb", type=int, default=0, metavar="N",
                   help="Sub-split any search-data/*.json > N MB into 16 "
                        "hash-bucketed sub-chunks. Fixes Kiwix Desktop "
                        "crashes on regions with super-dense Latin prefixes "
                        "(av/ro/st on US regions) or single CJK codepoints "
                        "(e.g. Japan's 大 = 500 MB). Updates the manifest's "
                        "``sub_chunks`` section so clients know to fan out "
                        "queries to the right sub-file. Typical threshold: 50.")
    p.add_argument("--refresh-terrain-tiles", metavar="DIR", default=None,
                   help="Swap every terrain/z/x/y.webp entry whose "
                        "filesystem counterpart in DIR is strictly newer "
                        "than the source ZIM. Use after "
                        "cloud/fix_stale_terrain_tiles.py regenerates "
                        "cached tiles, to roll them into the ZIM without "
                        "a full rebuild. Typical DIR: terrain_cache")
    p.add_argument("--unchunk-graph", action="store_true",
                   help="Reassemble a chunked routing-data/graph-chunk-*.bin "
                        "into a single graph.bin. Use when graph total is "
                        "under 500 MB (Kiwix iOS app can't parse chunked-"
                        "only layout but handles monolithic fine). Under "
                        "500 MB the chunk split is unnecessary anyway.")
    p.add_argument("--split-find-chips", action="store_true",
                   help="Read category-index/poi.json + park.json and "
                        "emit per-chip category-index/chip-{id}.json "
                        "files (restaurants, cafes, museums, …). "
                        "places.html fetches the chip file directly "
                        "instead of the full 1 GB poi.json which OOMs "
                        "Chrome on Japan.")
    p.add_argument("--no-rewrite-search-links", action="store_true",
                   help="Disable the search/<slug>.html href fix. By "
                        "default the script rewrites bare "
                        "`href=\"index.html#...\"` to `href=\"../index.html#...\"` "
                        "so the link reaches the viewer at the ZIM root "
                        "rather than 404'ing on `search/index.html`. "
                        "Disable only when re-emitting an old ZIM that "
                        "needs to stay byte-identical for diffing.")
    p.add_argument("--include-llm-bundle", action="store_true",
                   help="Keep category-index/{addr,poi,street}.json in the "
                        "output ZIM. By default these are DROPPED — the "
                        "viewer doesn't read them, they're hundreds of MB "
                        "each (5 GB+ on US regions), and a phone fetching "
                        "one would OOM. Enable only when emitting a ZIM "
                        "for an offline LLM consumer that ingests the raw "
                        "category bundles directly.")
    p.add_argument("--chip-split-threshold-mb", type=int, default=10,
                   metavar="N",
                   help="Sub-bucket category-index/chip-{id}.json files "
                        "larger than N MB into FNV-bucketed sub-files. "
                        "Mirrors the --split-hot-search-chunks-mb logic. "
                        "Default 10 — Japan's restaurants chip was 164 MB "
                        "and Canada's shops 137 MB without sub-bucketing, "
                        "tight against iOS heap. Set to 0 to disable.")
    args = p.parse_args()
    return repackage(args.src, args.dst,
                     swap_viewer=not args.no_swap_viewer,
                     uncompress_graph=not args.no_uncompress_graph,
                     split_graph=args.split_graph,
                     chunk_graph_mb=args.chunk_graph_mb,
                     spatial_chunk_scale=args.spatial_chunk_scale,
                     split_hot_search_chunks_mb=args.split_hot_search_chunks_mb,
                     refresh_terrain_dir=args.refresh_terrain_tiles,
                     unchunk_graph=args.unchunk_graph,
                     split_find_chips=args.split_find_chips,
                     rewrite_search_links=not args.no_rewrite_search_links,
                     drop_llm_bundle=not args.include_llm_bundle,
                     chip_split_threshold_mb=args.chip_split_threshold_mb)


if __name__ == "__main__":
    sys.exit(main())
