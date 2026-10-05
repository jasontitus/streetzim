"""Frozen spatial writer from main 29ec7da, for byte-for-byte differential tests.

Keep independent of the production implementation: this is the allocation-heavy
writer whose ordering, geometry sharing and binary output must be preserved.
"""
from __future__ import annotations
import os
import struct
from pathlib import Path
import numpy as np
from streetzim.routing.reader import SZRG
SZCI_MAGIC = b"SZCI"
SZRC_MAGIC = b"SZRC"
SZRC_VERSION = 2
SZCI_VERSION_CELL_COORDS = 3
DEFAULT_CELL_SCALE = 10

def cell_of(lat_e7: int, lon_e7: int, scale: int) -> tuple[int, int]:
    """Deterministic cell key for a coord. scale=10 ⇒ 0.1° × 0.1° cells.

    Uses floor semantics so a node exactly on the boundary of two cells
    always lands in the same one (matters for reproducibility when two
    implementations disagree on rounding of negative latitudes).
    """
    return (lat_e7 * scale) // 10_000_000, (lon_e7 * scale) // 10_000_000


def build_spatial(g: SZRG, *, cell_scale: int = DEFAULT_CELL_SCALE,
                  output_dir: str | Path | None = None,
                  ) -> tuple[bytes, dict, dict]:
    """Split `g` into (SZCI index bytes, cells, meta).

    With ``output_dir``: writes ``graph-cells-index.bin`` + per-cell
    ``graph-cell-{cid:05d}.bin`` files into the directory; the returned
    cells dict is ``{cell_id: file_path_str}``. Memory-efficient for
    continent-scale graphs (US: ~12 GB peak vs ~80 GB with the
    in-memory return path).

    Without ``output_dir``: returns cells as ``{cell_id: bytes}`` (legacy
    in-memory API used by tests). Suitable for graphs ≤ a few million
    edges; larger sizes will OOM in the per-cell-bytes accumulator.

    Output is SZCI v3 + SZRC v2. Route semantics remain identical to the
    source graph after translating source IDs to cell-major IDs.
    """
    if g.version not in (4, 5):
        raise ValueError(f"spatial writer needs SZRG v4 or v5, got v{g.version}")
    if g.edge_stride != 5:
        raise ValueError("spatial writer expects v4/v5 edge stride (5)")
    if not g.has_geoms and g.version == 5:
        raise ValueError("v5 source must have geoms attached (attach_geoms first)")

    num_nodes = g.num_nodes
    num_edges = g.num_edges
    num_names = g.num_names
    stride = g.edge_stride
    NO_GEOM = g.no_geom

    # Source views — numpy arrays, NOT .tolist()'d (the legacy code's
    # 10× speedup via Python-list materialization cost ~40 GB on the
    # US graph and was the primary OOM trigger).
    nodes_arr = g.nodes_scaled       # int32 [num_nodes*2] = lat0,lon0,...
    adj_arr = g.adj_offsets          # uint32 [num_nodes+1]
    edges_arr = g.edges              # uint32 [num_edges*stride]
    geom_offsets_arr = g.geom_offsets  # uint32
    geom_blob = g.geom_blob          # bytes

    # ---- Pass 1: vectorized cell assignment + group-sort by cell ----------
    # cell_of(lat, lon) = (lat*scale // 1e7, lon*scale // 1e7). Promote to
    # int64 so lat_e7 (~9e8) × cell_scale doesn't overflow int32.
    lats = nodes_arr[0::2].astype(np.int64, copy=False)
    lons = nodes_arr[1::2].astype(np.int64, copy=False)
    lat_cell_idx = (lats * cell_scale) // 10_000_000
    lon_cell_idx = (lons * cell_scale) // 10_000_000
    del lats, lons

    # Pack (lat_cell, lon_cell) into a single uint64 sortable key. Bias
    # both halves so negatives sort below positives in unsigned space.
    BIAS = 1 << 30
    cell_key = (((lat_cell_idx + BIAS).astype(np.uint64) << 32)
                | ((lon_cell_idx + BIAS) & 0xFFFFFFFF).astype(np.uint64))
    del lat_cell_idx, lon_cell_idx

    # Stable sort so ties (same cell_key) preserve source-node-idx order.
    # The sorted position is the SZCI v3 global node ID. Reindexing nodes
    # cell-major makes every cell a contiguous range and removes the
    # region-wide coordinate table from the read path.
    order = np.argsort(cell_key, kind='stable')
    old_to_new = np.empty(num_nodes, dtype=np.uint32)
    old_to_new[order] = np.arange(num_nodes, dtype=np.uint32)
    sorted_keys = cell_key[order]
    unique_keys, cell_starts = np.unique(sorted_keys, return_index=True)
    num_cells = int(len(unique_keys))
    cell_starts = cell_starts.astype(np.int64, copy=False)
    cell_ends = np.concatenate([cell_starts[1:],
                                np.array([num_nodes], dtype=np.int64)])

    # Decode (lat_cell, lon_cell) for the SZCI cell-metadata table.
    cell_lat_arr = ((unique_keys >> 32).astype(np.int64) - BIAS).astype(np.int32)
    cell_lon_arr = (((unique_keys & 0xFFFFFFFF).astype(np.int64) - BIAS)
                    .astype(np.int32))
    del cell_key, sorted_keys, unique_keys

    # ---- Streaming output prep --------------------------------------------
    if output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
    cells_out: dict = {}

    # Cell metadata accumulators — fed into the SZCI index after the loop.
    cell_node_counts = np.zeros(num_cells, dtype=np.uint32)
    cell_edge_counts = np.zeros(num_cells, dtype=np.uint32)
    cell_geom_counts = np.zeros(num_cells, dtype=np.uint32)
    cell_base_nodes = cell_starts.astype(np.uint32, copy=True)

    # ---- Pass 2: per-cell SZRC build + serialize --------------------------
    edges_view = edges_arr.reshape(-1, stride)  # (num_edges, stride) view

    for cid in range(num_cells):
        s = int(cell_starts[cid])
        e = int(cell_ends[cid])
        # Keep source IDs only while gathering source adjacency and coords.
        # Serialized edges are rewritten to cell-major SZCI v3 IDs.
        cell_nodes = order[s:e].astype(np.uint32, copy=False)
        cell_coords = nodes_arr.reshape(-1, 2)[cell_nodes].copy()
        n_count = e - s

        # Per-node edge ranges + cumulative cell_adj.
        node_e_starts = adj_arr[cell_nodes].astype(np.int64, copy=False)
        node_e_ends = adj_arr[cell_nodes + 1].astype(np.int64, copy=False)
        per_node_counts = (node_e_ends - node_e_starts).astype(np.uint32)
        e_count = int(per_node_counts.sum())

        cell_adj = np.empty(n_count + 1, dtype=np.uint32)
        cell_adj[0] = 0
        np.cumsum(per_node_counts, out=cell_adj[1:])

        if e_count > 0:
            # Build the gather-index for all edges in this cell, in
            # cell_nodes order with each node's edges in source order.
            offsets_per_edge = np.repeat(node_e_starts, per_node_counts)
            cumul_per_edge = np.repeat(cell_adj[:-1].astype(np.int64),
                                       per_node_counts)
            within_node_pos = (np.arange(e_count, dtype=np.int64)
                               - cumul_per_edge)
            edge_idx = offsets_per_edge + within_node_pos
            del offsets_per_edge, cumul_per_edge, within_node_pos

            # One-shot fancy-index gather of all 5 columns (~e_count*5*4 B).
            cell_edge_data = edges_view[edge_idx].copy()  # (e_count, stride)
            del edge_idx
            cell_edge_data[:, 0] = old_to_new[cell_edge_data[:, 0]]

            # Cell-local geom mapping in encounter order. NO_GEOM stays as
            # the 0xFFFFFFFF sentinel; only real geom_idx values get a
            # local index assigned on first occurrence.
            geom_col = cell_edge_data[:, 2]
            no_geom_mask = geom_col == NO_GEOM
            real_geoms = geom_col[~no_geom_mask]

            if real_geoms.size > 0:
                # np.unique returns sorted unique + first-occurrence indices.
                # Sort uniques by first_idx to recover encounter order.
                uniq_sorted, first_idx = np.unique(real_geoms,
                                                   return_index=True)
                encounter_order = np.argsort(first_idx, kind='stable')
                geoms_in_encounter_order = uniq_sorted[encounter_order]
                g_count = int(geoms_in_encounter_order.size)

                # src_geom_idx → local_idx via two indirections:
                # searchsorted on uniq_sorted (sorted), then through the
                # inverse of encounter_order to reach the local index.
                inverse_of_encounter = np.empty(g_count, dtype=np.uint32)
                inverse_of_encounter[encounter_order] = np.arange(
                    g_count, dtype=np.uint32)
                pos_in_sorted = np.searchsorted(uniq_sorted, real_geoms)
                real_local = inverse_of_encounter[pos_in_sorted]

                local_geom_col = np.full(e_count, 0xFFFFFFFF, dtype=np.uint32)
                local_geom_col[~no_geom_mask] = real_local
                cell_edge_data[:, 2] = local_geom_col
                del (uniq_sorted, first_idx, encounter_order,
                     inverse_of_encounter, pos_in_sorted, real_local,
                     local_geom_col)

                # Per-cell geom blob: concat referenced byte ranges.
                local_geom_offsets = np.empty(g_count + 1, dtype=np.uint32)
                local_geom_offsets[0] = 0
                local_geom_parts: list[bytes] = []
                running = 0
                for gi in geoms_in_encounter_order.tolist():
                    gs = int(geom_offsets_arr[gi])
                    ge = int(geom_offsets_arr[gi + 1])
                    chunk = bytes(geom_blob[gs:ge])
                    local_geom_parts.append(chunk)
                    running += len(chunk)
                    local_geom_offsets[len(local_geom_parts)] = running
                local_geom_buf = b"".join(local_geom_parts)
                del local_geom_parts, geoms_in_encounter_order
            else:
                g_count = 0
                local_geom_offsets = np.array([0], dtype=np.uint32)
                local_geom_buf = b""
            del real_geoms, no_geom_mask, geom_col
        else:
            cell_edge_data = np.empty((0, stride), dtype=np.uint32)
            g_count = 0
            local_geom_offsets = np.array([0], dtype=np.uint32)
            local_geom_buf = b""

        cell_node_counts[cid] = n_count
        cell_edge_counts[cid] = e_count
        cell_geom_counts[cid] = g_count

        # SZRC serialize — write directly to bytes; layout per file format
        # spec at the top of this module.
        cell_header = SZRC_MAGIC + struct.pack(
            "<6I",
            SZRC_VERSION,
            cid,
            n_count,
            e_count,
            g_count,
            len(local_geom_buf),
        )
        body = (
            cell_coords.reshape(-1).tobytes()
            + cell_adj.tobytes()
            + cell_edge_data.reshape(-1).tobytes()
            + local_geom_offsets.tobytes()
            + local_geom_buf
        )
        cell_bytes = cell_header + body

        if output_dir is not None:
            cell_path = output_dir / f"graph-cell-{cid:05d}.bin"
            cell_path.write_bytes(cell_bytes)
            cells_out[cid] = str(cell_path)
        else:
            cells_out[cid] = cell_bytes

        # Free per-cell buffers so peak doesn't accumulate across cells.
        del (cell_nodes, cell_coords, cell_adj, cell_edge_data, local_geom_offsets,
             local_geom_buf, cell_header, body, cell_bytes,
             per_node_counts, node_e_starts, node_e_ends)

    # Free Pass 1 working arrays once cells are emitted.
    del order, old_to_new, cell_starts, cell_ends

    # ---- Build SZCI index -------------------------------------------------
    # SZCI v3 keeps only compact metadata in the eagerly-loaded index.
    # Coordinates live in SZRC v2 cells and are fetched on demand.
    version = SZCI_VERSION_CELL_COORDS
    shard_paths: list[Path] = []
    header = SZCI_MAGIC + struct.pack(
        "<7I",
        version,
        num_nodes, num_edges,
        num_names, len(g.names_blob),
        num_cells,
        cell_scale if cell_scale >= 0 else 0,
    )

    # Cell metadata table — 24 bytes per cell (i32 ×2, u32 ×4).
    cell_meta_dt = np.dtype([
        ('lat_cell', '<i4'),
        ('lon_cell', '<i4'),
        ('base_node', '<u4'),
        ('node_count', '<u4'),
        ('edge_count', '<u4'),
        ('geom_count', '<u4'),
    ])
    cell_meta = np.empty(num_cells, dtype=cell_meta_dt)
    cell_meta['lat_cell'] = cell_lat_arr
    cell_meta['lon_cell'] = cell_lon_arr
    cell_meta['base_node'] = cell_base_nodes
    cell_meta['node_count'] = cell_node_counts
    cell_meta['edge_count'] = cell_edge_counts
    cell_meta['geom_count'] = cell_geom_counts
    cell_meta_blob = cell_meta.tobytes()

    name_offsets_blob = g.name_offsets.tobytes()
    names_blob = g.names_blob

    index_bytes = (header + cell_meta_blob
                   + name_offsets_blob + names_blob)

    if output_dir is not None:
        (output_dir / "graph-cells-index.bin").write_bytes(index_bytes)

    cell_coords_sorted = list(zip(cell_lat_arr.tolist(), cell_lon_arr.tolist()))

    if output_dir is not None:
        total_bytes = (len(index_bytes)
                       + sum(os.path.getsize(p) for p in cells_out.values())
                       + sum(p.stat().st_size for p in shard_paths))
    else:
        total_bytes = len(index_bytes) + sum(len(b) for b in cells_out.values())

    meta = {
        "num_cells": num_cells,
        "cell_coords": cell_coords_sorted,
        "total_bytes": total_bytes,
        "cell_scale": cell_scale,
        "node_shard_paths": [str(p) for p in shard_paths],
    }
    return index_bytes, cells_out, meta

