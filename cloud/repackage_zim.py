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


def _emit_spatial_graph(creator, graph_bytes: bytes, *,
                        cell_scale: int,
                        passthrough_cls) -> None:
    """Convert a SZRG v4/v5 buffer into the spatial SZCI + SZRC layout
    and add all pieces as ZIM entries. Routing reader decides which cells
    to load at route time; only the index is eager."""
    # Import lazily so this module stays usable in lightweight contexts
    # that don't need the spatial chunker (e.g., viewer-only repackage).
    import sys
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from tests.szrg_reader import parse_szrg_bytes
    from tests.szrg_spatial import build_spatial

    g = parse_szrg_bytes(graph_bytes)
    # v5 in-memory parse yielded has_geoms=False; if the source was v5
    # split we'd need to attach the SZGM companion first. Not supported
    # in the repackage path yet — fail loud so the user knows.
    if g.version == 5 and not g.has_geoms:
        raise RuntimeError(
            "spatial chunking of a v5-split source requires the SZGM "
            "companion too; pass a v4 source graph instead"
        )

    idx_bytes, cells, meta = build_spatial(g, cell_scale=cell_scale)
    idx_mb = len(idx_bytes) / 1e6
    cell_sizes = [len(b) / 1e6 for b in cells.values()]
    total_cell_mb = sum(cell_sizes)
    print(f"  → spatial (cell_scale={cell_scale}): index {idx_mb:.1f} MB + "
          f"{meta['num_cells']} cells totalling {total_cell_mb:.1f} MB "
          f"(max cell {max(cell_sizes):.1f} MB)")

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
    # monolithic graph so fzstd handles them comfortably.
    for cid in sorted(cells.keys()):
        data = cells[cid]
        compress_cell = len(data) < 200 * 1024 * 1024
        creator.add_item(passthrough_cls(
            f"routing-data/graph-cell-{cid:05d}.bin",
            f"Routing Graph Cell {cid}",
            "application/octet-stream",
            data,
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
              split_find_chips: bool = False) -> int:
    from libzim.reader import Archive
    from libzim.writer import Creator, Item, StringProvider, Hint

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
    captured_graph_bytes = None
    # Keyed by the source's chunk path ("routing-data/graph-chunk-0000.bin"
    # etc). Reassembled into a single buffer below when the source
    # shipped chunked but not monolithic.
    captured_graph_chunks: dict | None = None
    with creator as c:
        # Passthrough all entries — Archive iterates titles + paths.
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
            content = bytes(item.content)
            mime = item.mimetype
            title = entry.title or ""
            compress = True
            # Swap viewer HTML for the current version.
            if swap_viewer and path in replacements:
                content = replacements[path]
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
                            content = fh.read()
                        refreshed_terrain += 1
                except (OSError, FileNotFoundError):
                    pass
            # Capture + defer the routing graph when we're upgrading
            # layout — we'll emit split / chunked entries after the
            # passthrough pass so they land contiguously.
            if upgrade_graph and path in (
                "routing-data/graph.bin",
                "routing-data/graph-geoms.bin",
                "routing-data/graph-chunk-manifest.json",
                "routing-data/graph-geoms-chunk-manifest.json",
            ):
                if path == "routing-data/graph.bin":
                    captured_graph_bytes = content
                skipped_routing += 1
                continue
            if upgrade_graph and (
                path.startswith("routing-data/graph-chunk-")
                or path.startswith("routing-data/graph-geoms-chunk-")
                or path.startswith("routing-data/graph-cell-")
                or path == "routing-data/graph-cells-index.bin"
            ):
                # Accumulate source chunks into captured_graph_bytes if
                # the source is already chunked (no graph.bin entry).
                # Without this, the later _emit_upgraded_graph path sees
                # captured_graph_bytes=None and prints a warning — and
                # we SHIP a ZIM with no routing. Caught by validator
                # gate, but only after wasting a full repackage.
                if path.startswith("routing-data/graph-chunk-") and not \
                        path.startswith("routing-data/graph-chunk-manifest"):
                    if captured_graph_chunks is None:
                        captured_graph_chunks = {}
                    captured_graph_chunks[path] = content
                skipped_routing += 1
                continue
            # Hot-chunk splitting: capture the search-data manifest and
            # any oversized chunks, defer their re-emission. Skipped
            # chunks land as sub-files at the end.
            if split_search and path == "search-data/manifest.json":
                try:
                    captured_search_manifest = json.loads(content.decode("utf-8"))
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
                    and len(content) > hot_split_mb * 1024 * 1024):
                prefix = path[len("search-data/"):-len(".json")]
                captured_search_chunks[prefix] = content
                replaced_search_paths.add(path)
                continue
            # Mark the routing graph uncompressed for PWA-fzstd compat
            # (skipped when we're rewriting the graph anyway, above).
            if uncompress_graph and path == "routing-data/graph.bin":
                compress = False
                raw_clusters += 1
            c.add_item(PassthroughItem(path, title, mime, content, compress=compress))
            kept += 1
            if kept % 50_000 == 0:
                print(f"    copied {kept} entries...")

        # If source shipped chunked graph (no graph.bin), reassemble
        # the chunks into a single buffer so the re-emit path below
        # has something to work with. Source chunks are byte-ranges
        # of the original graph, so concatenation is byte-exact.
        if (upgrade_graph and captured_graph_bytes is None
                and captured_graph_chunks):
            parts = [captured_graph_chunks[k]
                     for k in sorted(captured_graph_chunks)]
            captured_graph_bytes = b"".join(parts)
            print(f"  reassembled graph.bin from {len(parts)} source "
                  f"chunks ({len(captured_graph_bytes)/1024/1024:.1f} MB)")

        # Re-emit the routing graph in the requested layout.
        if upgrade_graph and captured_graph_bytes is None:
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
            _emit_spatial_graph(
                c, captured_graph_bytes,
                cell_scale=spatial_chunk_scale,
                passthrough_cls=PassthroughItem,
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
                chip_bytes = json.dumps(dst_records, separators=(",", ":"),
                                         ensure_ascii=False).encode("utf-8")
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
    extra = f"; {refreshed_terrain} terrain tiles refreshed" if refreshed_terrain else ""
    print(f"\n  kept {kept} entries; {swapped} viewer swaps; "
          f"{added_missing} added; {raw_clusters} raw cluster(s){extra}")
    print(f"  output: {dst_path} ({size_mb:.1f} MB)")
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
                     split_find_chips=args.split_find_chips)


if __name__ == "__main__":
    sys.exit(main())
