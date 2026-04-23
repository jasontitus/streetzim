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
        if chunk_size <= 0 or len(data) <= chunk_size:
            compress = len(data) < 200 * 1024 * 1024
            creator.add_item(passthrough_cls(
                name, title, mime, data, compress=compress))
            print(f"    + {name} ({len(data)/1e6:.1f} MB, "
                  f"{'compressed' if compress else 'raw'})")
            return
        entries, manifest = _chunk_bytes_inmem(data, chunk_size, chunk_prefix)
        creator.add_item(passthrough_cls(
            manifest_name, manifest_title, "application/json",
            json.dumps(manifest, separators=(",", ":")).encode("utf-8"),
            compress=True))
        for ename, edata in entries:
            compress = len(edata) < 200 * 1024 * 1024
            creator.add_item(passthrough_cls(
                f"routing-data/{ename}",
                f"{title} ({ename})",
                "application/octet-stream", edata,
                compress=compress))
        print(f"    + {name} chunked → {len(entries)} entries of "
              f"≤{chunk_graph_mb} MB each")

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
              split_hot_search_chunks_mb: int = 0) -> int:
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
                     or spatial_chunk_scale > 0)
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

        # Re-emit the routing graph in the requested layout.
        if upgrade_graph and captured_graph_bytes is None:
            print("  warning: no routing-data/graph.bin in source — "
                  "nothing to upgrade. Output will have no routing.")
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

    size_mb = os.path.getsize(dst_path) / (1024 * 1024)
    print(f"\n  kept {kept} entries; {swapped} viewer swaps; "
          f"{raw_clusters} raw cluster(s)")
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
    args = p.parse_args()
    return repackage(args.src, args.dst,
                     swap_viewer=not args.no_swap_viewer,
                     uncompress_graph=not args.no_uncompress_graph,
                     split_graph=args.split_graph,
                     chunk_graph_mb=args.chunk_graph_mb,
                     spatial_chunk_scale=args.spatial_chunk_scale,
                     split_hot_search_chunks_mb=args.split_hot_search_chunks_mb)


if __name__ == "__main__":
    sys.exit(main())
