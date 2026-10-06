"""Spatial-chunked SZRG (SZCI index + SZRC per-cell files).

Splits a v4/v5 SZRG graph into an eagerly-loaded index (names + compact
cell metadata) plus one file per spatial cell (coords + edges + geoms for
that cell's nodes). A* loads cells lazily as its frontier crosses
boundaries, so mobile readers never allocate a region-wide coordinate
table.

SZCI v3 reindexes nodes into cell-major order. Each cell owns one
contiguous global-node range, which lets readers resolve node ownership
with a binary search over the small metadata table. Cells are sequential
IDs; their (lat_cell, lon_cell) keys live in the index.

Companion format to the viewer's reader
(resources/viewer/src/index/510-routing-graph-formats.js) + mcpzim SZRGGraph. This
module is the reference implementation; language ports mirror it.

File formats (little-endian, u32 unless stated):

  SZCI v3 (cells index, 32-byte header then global tables):
    "SZCI" magic (4)
    u32 version = 3
    u32 num_nodes
    u32 num_edges
    u32 num_names
    u32 names_bytes
    u32 num_cells
    i32 cell_scale       -- e.g., 10 ⇒ 0.1° cells; 1 ⇒ 1° cells
    -- Cell metadata (num_cells × 24 bytes):
    foreach cell:
      i32 lat_cell_idx, i32 lon_cell_idx
      u32 base_node, u32 node_count, u32 edge_count, u32 geom_count
    u32[num_names + 1]   -- name_offsets
    bytes[names_bytes]   -- names_blob

  SZRC v2 (one cell, 28-byte header -- magic + 6 x u32 -- then per-cell tables):
    "SZRC" magic (4)
    u32 version = 2
    u32 cell_id
    u32 node_count
    u32 edge_count
    u32 geom_count
    u32 geom_bytes
    Int32[node_count * 2]     -- lat_e7, lon_e7 in cell-major node order
    u32[node_count + 1]       -- cell_adj (offsets into cell_edges)
    u32[edge_count * 5]       -- edges: target_global, speed_dist, geom_local, name, class_access
    u32[geom_count + 1]       -- geom_offsets (bytes into geom_blob)
    bytes[geom_bytes]         -- geom_blob (zigzag-varint polylines, same as SZRG)
"""

from __future__ import annotations

import bisect
import io
import math
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from streetzim.routing.reader import SZRG
from streetzim.routing.modes import edge_cost

# class_access bit 9 (see docs/driving-mode-road-class-warnings.md). Kept
# local rather than imported from szrg_astar to avoid a circular import.
_NO_MOTOR_BIT = 0x200
_NO_MOTOR_ORD_MIN, _NO_MOTOR_ORD_MAX = 16, 20  # path..steps ordinals


def _is_no_motor(class_access: int) -> bool:
    if class_access & _NO_MOTOR_BIT:
        return True
    return _NO_MOTOR_ORD_MIN <= (class_access & 0x1F) <= _NO_MOTOR_ORD_MAX


def _mode_ok(travel_mode: str, speed_dist: int, ca: int) -> bool:
    if travel_mode == "drive":
        return not _is_no_motor(ca) and (speed_dist >> 24) != 0
    return edge_cost(travel_mode, speed_dist, ca) is not None


# Snap shortlist (mirrors routing-worker.js SNAP_*): keep the N nearest
# car-ok vertices, return the first from which a bounded forward BFS
# reaches SNAP_MIN_REACH nodes, as long as it is at most SNAP_MAX_EXTRA_M
# further from the query than the nearest.
SNAP_CANDIDATES = 6
SNAP_CANDIDATES_WIDE = 48   # second pass, when none of the first reaches
SNAP_WIDE_EXTRA_M = 150     # ...and only this far past the nearest (not an island hop)
SNAP_MIN_REACH = 32
SNAP_MAX_EXTRA_M = 1000


SZCI_MAGIC = b"SZCI"
SZRC_MAGIC = b"SZRC"
SZCI_VERSION_INLINE = 1     # legacy: nodes_scaled in the SZCI body
SZCI_VERSION_SHARDED = 2    # nodes_scaled sharded into routing-data/nodes-scaled-NNN.bin
SZCI_VERSION_CELL_COORDS = 3 # coords live in cell payloads; index stores node ranges
SZCI_VERSION = SZCI_VERSION_CELL_COORDS
SZRC_VERSION_LEGACY = 1
SZRC_VERSION_CELL_COORDS = 2
SZRC_VERSION = SZRC_VERSION_CELL_COORDS

DEFAULT_CELL_SCALE = 10  # 0.1° cells — ~11 km lat; lon varies by latitude
_NODE_BLOCK = 1 << 20
# Geometries copied per batch while a cell's geometry section is written.
_GEOM_CHUNK = 16384

# Cap any single ZIM entry well under the 200 MB validator threshold
# (and even further under the libzim 4 GB blob limit). 5 M nodes × 8 B
# = 40 MB per shard, so a 90 M-node continent ZIM produces 18 shards.
DEFAULT_NODES_PER_SHARD = 5_000_000

# Below this, just inline nodes_scaled in the SZCI (v1) so small ZIMs
# don't pay the per-shard ZIM-entry overhead. 2 M nodes × 8 B = 16 MB.
NODES_SCALED_INLINE_MB_THRESHOLD = 50


def _node_shard_ranges(num_nodes: int,
                       nodes_per_shard: int = DEFAULT_NODES_PER_SHARD,
                       ) -> list[tuple[int, int]]:
    """Return list of (start_node_idx, end_node_idx) tuples that partition
    [0, num_nodes) into shards of at most ``nodes_per_shard`` nodes each.
    The final shard may be shorter."""
    out: list[tuple[int, int]] = []
    start = 0
    while start < num_nodes:
        end = min(start + nodes_per_shard, num_nodes)
        out.append((start, end))
        start = end
    return out


# ---------------------------------------------------------------------------
# Writer: v4/v5 SZRG → (SZCI bytes, {cell_id: SZRC bytes})
# ---------------------------------------------------------------------------


def cell_of(lat_e7: int, lon_e7: int, scale: int) -> tuple[int, int]:
    """Deterministic cell key for a coord. scale=10 ⇒ 0.1° × 0.1° cells.

    Uses floor semantics so a node exactly on the boundary of two cells
    always lands in the same one (matters for reproducibility when two
    implementations disagree on rounding of negative latitudes).
    """
    return (lat_e7 * scale) // 10_000_000, (lon_e7 * scale) // 10_000_000


def _group_nodes(nodes_arr, cell_scale):
    """Stable cell-major permutation and its inverse, both uint32.

    Only the keys and sort permutation span every node during sorting.
    Coordinate arithmetic, boundary detection and inverse assignment use
    bounded blocks. In particular, np.unique(return_index=True) would sort
    the already sorted keys again and allocate several full-size arrays.
    """
    n = len(nodes_arr) // 2
    if n > 0xFFFFFFFF:
        raise ValueError("SZCI node count exceeds uint32")
    bias = 1 << 30
    cell_key = np.empty(n, dtype=np.uint64)
    for s in range(0, n, _NODE_BLOCK):
        e = min(s + _NODE_BLOCK, n)
        lat = nodes_arr[s * 2:e * 2:2].astype(np.int64)
        lon = nodes_arr[s * 2 + 1:e * 2:2].astype(np.int64)
        lat *= cell_scale
        lon *= cell_scale
        lat //= 10_000_000
        lon //= 10_000_000
        lat += bias
        lon += bias
        cell_key[s:e] = ((lat.astype(np.uint64) << 32)
                         | (lon & 0xFFFFFFFF).astype(np.uint64))
        del lat, lon
    order = np.argsort(cell_key, kind="stable").astype(np.uint32)
    start_parts, key_parts = [], []
    previous = None
    for s in range(0, n, _NODE_BLOCK):
        e = min(s + _NODE_BLOCK, n)
        keys = cell_key[order[s:e]]
        changed = np.empty(e - s, dtype=bool)
        changed[0] = previous is None or keys[0] != previous
        changed[1:] = keys[1:] != keys[:-1]
        offsets = np.flatnonzero(changed)
        start_parts.append(offsets + s)
        key_parts.append(keys[offsets])
        previous = keys[-1]
        del keys, changed, offsets
    del cell_key
    starts = (np.concatenate(start_parts) if start_parts
              else np.empty(0, dtype=np.int64))
    unique_keys = (np.concatenate(key_parts) if key_parts
                   else np.empty(0, dtype=np.uint64))
    del start_parts, key_parts
    lat_cells = ((unique_keys >> 32).astype(np.int64) - bias).astype(np.int32)
    lon_cells = ((unique_keys & 0xFFFFFFFF).astype(np.int64) - bias).astype(np.int32)
    ends = np.concatenate((starts[1:], [n])) if len(starts) else starts.copy()
    old_to_new = np.empty(n, dtype=np.uint32)
    for s in range(0, n, _NODE_BLOCK):
        e = min(s + _NODE_BLOCK, n)
        old_to_new[order[s:e]] = np.arange(s, e, dtype=np.uint32)
    return order, old_to_new, starts, ends, lat_cells, lon_cells


def build_spatial(g: SZRG, *, cell_scale: int = DEFAULT_CELL_SCALE,
                  output_dir: str | Path | None = None,
                  restrictions: list | None = None,
                  ) -> tuple[bytes, dict, dict]:
    """Split `g` into (SZCI index bytes, cells, meta).

    With ``output_dir``: writes ``graph-cells-index.bin`` + per-cell
    ``graph-cell-{cid:05d}.bin`` files into the directory; the returned
    cells dict is ``{cell_id: file_path_str}``. Use load_from_file(mapped=True)
    to keep the source graph reclaimable. Grouping uses compact node arrays,
    and cell sections go straight to disk without concatenating cell copies.

    Without ``output_dir``: returns cells as ``{cell_id: bytes}`` (legacy
    in-memory API used by tests). Suitable for graphs ≤ a few million
    edges; larger sizes will OOM in the per-cell-bytes accumulator.

    Output is SZCI v3 + SZRC v2. Route semantics remain identical to the
    source graph after translating source IDs to cell-major IDs.

    ``restrictions`` ([(flags, node path)] in source numbering,
    streetzim.routing.restrictions) are renumbered and appended as an SZTR
    trailer to the cell holding each path's first via node; a cell
    without any gets no trailer (byte-identical to before).
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
    geom_blob = g.geom_blob          # bytes or read-only mapped view

    # Stable ties retain source-node order, including across block boundaries.
    order, old_to_new, cell_starts, cell_ends, cell_lat_arr, cell_lon_arr = \
        _group_nodes(nodes_arr, cell_scale)
    num_cells = len(cell_starts)

    # Turn restrictions per cell (cell of path[1], in cell-major ids).
    from streetzim.routing import restrictions as _tr
    cell_turns: dict = {}
    for flags, path in restrictions or ():
        new = [int(old_to_new[n]) for n in path]
        c = int(np.searchsorted(cell_starts, new[1], side="right")) - 1
        cell_turns.setdefault(c, []).append((flags, new))

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
        cell_coords = nodes_arr.reshape(-1, 2)[cell_nodes]
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
            cell_edge_data = edges_view[edge_idx]  # fancy indexing already copies
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

                # Offsets only: write the referenced geometry ranges below,
                # without a list of copied bytes and a joined geometry blob.
                geom_starts = geom_offsets_arr[geoms_in_encounter_order]
                geom_ends = geom_offsets_arr[geoms_in_encounter_order + 1]
                if np.any(geom_ends < geom_starts) or int(geom_ends.max()) > len(geom_blob):
                    raise ValueError("geometry offsets outside source blob")
                lengths = geom_ends.astype(np.uint64) - geom_starts
                cumulative = np.cumsum(lengths, dtype=np.uint64)
                if int(cumulative[-1]) > 0xFFFFFFFF:
                    raise OverflowError("cell geometry blob exceeds uint32")
                local_geom_offsets = np.empty(g_count + 1, dtype=np.uint32)
                local_geom_offsets[0] = 0
                local_geom_offsets[1:] = cumulative
                del lengths, cumulative
            else:
                g_count = 0
                local_geom_offsets = np.array([0], dtype=np.uint32)
                geoms_in_encounter_order = np.empty(0, dtype=np.uint32)
                geom_starts = geom_ends = np.empty(0, dtype=np.uint32)
            del real_geoms, no_geom_mask, geom_col
        else:
            cell_edge_data = np.empty((0, stride), dtype=np.uint32)
            g_count = 0
            local_geom_offsets = np.array([0], dtype=np.uint32)
            geoms_in_encounter_order = np.empty(0, dtype=np.uint32)
            geom_starts = geom_ends = np.empty(0, dtype=np.uint32)

        cell_node_counts[cid] = n_count
        cell_edge_counts[cid] = e_count
        cell_geom_counts[cid] = g_count

        # SZRC sections are contiguous buffers. Avoid body + cell_bytes,
        # which kept two more whole-cell copies beside the gathered arrays.
        cell_header = SZRC_MAGIC + struct.pack(
            "<6I",
            SZRC_VERSION,
            cid,
            n_count,
            e_count,
            g_count,
            int(local_geom_offsets[-1]),
        )
        if output_dir is not None:
            cell_path = output_dir / f"graph-cell-{cid:05d}.bin"
            output = cell_path.open("wb")
        else:
            output = io.BytesIO()
        with output:
            output.write(cell_header)
            for section in (cell_coords, cell_adj, cell_edge_data, local_geom_offsets):
                if section.size:
                    output.write(section.data.cast("B"))
            source_geoms = memoryview(geom_blob)
            # Python ints avoid two NumPy scalar conversions per geometry;
            # bound the temporary lists even for a very dense/coarse cell.
            for start in range(0, g_count, _GEOM_CHUNK):
                for gs, ge in zip(geom_starts[start:start + _GEOM_CHUNK].tolist(),
                                  geom_ends[start:start + _GEOM_CHUNK].tolist()):
                    output.write(source_geoms[gs:ge])
            if cid in cell_turns:
                output.write(_tr.pack(sorted(cell_turns.pop(cid))))
            del source_geoms, section
            if isinstance(output, io.BytesIO):
                cells_out[cid] = output.getvalue()
            else:
                cells_out[cid] = str(cell_path)

        # Free per-cell buffers so peak doesn't accumulate across cells.
        del (cell_nodes, cell_coords, cell_adj, cell_edge_data, local_geom_offsets,
             geoms_in_encounter_order, geom_starts, geom_ends, cell_header,
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


# ---------------------------------------------------------------------------
# Reader: lazy cell loader + data structures
# ---------------------------------------------------------------------------


@dataclass
class SZRCCell:
    """In-memory form of one SZRC cell file."""
    cell_id: int
    base_node: int
    node_count: int
    cell_nodes_global: np.ndarray   # legacy v1 only; empty for v2
    nodes_scaled: np.ndarray        # v2 int32[node_count*2]; empty for v1
    cell_adj: np.ndarray            # uint32[node_count+1]
    edges: np.ndarray               # uint32[edge_count*5]
    geom_offsets: np.ndarray        # uint32[geom_count+1]
    geom_blob: bytes                # varint polyline blob
    geom_count: int
    # Turn restrictions held here: [(flags, path)], path[1] in this cell
    # (streetzim.routing.restrictions). turn_roots: (path[0], path[1]) ->
    # the records starting there, built on first use.
    turns: list = field(default_factory=list)
    _turn_roots: dict | None = None

    def turn_roots(self) -> dict:
        if self._turn_roots is None:
            roots: dict = {}
            for i, (_flags, path) in enumerate(self.turns):
                roots.setdefault((path[0], path[1]), []).append(i)
            self._turn_roots = roots
        return self._turn_roots

    def local_idx_for(self, global_node_idx: int) -> int | None:
        """Return the cell-local index for a global node; None if absent."""
        if self.nodes_scaled.shape[0]:
            local = global_node_idx - self.base_node
            return local if 0 <= local < self.node_count else None
        arr = self.cell_nodes_global
        lo, hi = 0, arr.shape[0]
        while lo < hi:
            mid = (lo + hi) // 2
            v = int(arr[mid])
            if v < global_node_idx:
                lo = mid + 1
            elif v > global_node_idx:
                hi = mid
            else:
                return mid
        return None


@dataclass
class SZCIIndex:
    version: int
    num_nodes: int
    num_edges: int
    num_names: int
    num_cells: int
    cell_scale: int
    nodes_scaled: np.ndarray            # int32[num_nodes*2]
    name_offsets: np.ndarray
    names_blob: bytes
    # Cell metadata (parallel arrays for fast indexing)
    cell_lat_idx: np.ndarray            # int32[num_cells]
    cell_lon_idx: np.ndarray            # int32[num_cells]
    cell_base_node: np.ndarray           # uint32[num_cells], v3 only
    cell_node_count: np.ndarray         # uint32[num_cells]
    cell_edge_count: np.ndarray
    cell_geom_count: np.ndarray
    # Lookup: (lat_cell_idx, lon_cell_idx) → cell_id
    cell_id_by_key: dict[tuple[int, int], int] = field(default_factory=dict)

    def cell_for_node(self, node_idx: int) -> int | None:
        """Resolve the owning cell for a global node."""
        if self.version == SZCI_VERSION_CELL_COORDS:
            cid = bisect.bisect_right(self.cell_base_node, node_idx) - 1
            if cid < 0:
                return None
            base = int(self.cell_base_node[cid])
            return cid if node_idx < base + int(self.cell_node_count[cid]) else None
        lat_e7 = int(self.nodes_scaled[node_idx * 2])
        lon_e7 = int(self.nodes_scaled[node_idx * 2 + 1])
        key = cell_of(lat_e7, lon_e7, self.cell_scale)
        return self.cell_id_by_key.get(key)

    def get_name(self, name_idx: int) -> str:
        if name_idx <= 0 or name_idx >= self.num_names:
            return ""
        s = int(self.name_offsets[name_idx])
        e = int(self.name_offsets[name_idx + 1])
        return self.names_blob[s:e].decode("utf-8", errors="replace")


def parse_szci(buf: bytes,
               nodes_scaled_loader=None) -> SZCIIndex:
    """Parse SZCI index bytes.

    For version 1 (inline): nodes_scaled is read from the buffer directly.

    For version 2 (sharded): the SZCI body has no nodes_scaled blob;
    pass ``nodes_scaled_loader(shard_idx) -> bytes`` to fetch each
    ``routing-data/nodes-scaled-NNN.bin`` shard. The shards are
    concatenated and re-typed as int32 little-endian. If
    ``nodes_scaled_loader`` is None on a v2 index, ``nodes_scaled`` is
    populated as an empty array — useful only for inspecting cell
    metadata; routing requires the loader.

    For version 3 (cell coords): coordinates are absent from the index
    entirely. Readers fetch them lazily from SZRC v2 cells.
    """
    if buf[:4] != SZCI_MAGIC:
        raise ValueError("Not a SZCI index (bad magic)")
    version = struct.unpack_from("<I", buf, 4)[0]
    if version == SZCI_VERSION_INLINE:
        (_, num_nodes, num_edges, num_names, names_bytes, num_cells,
         _cell_scale_unsigned) = struct.unpack_from("<7I", buf, 4)
        cell_scale_signed = struct.unpack_from("<i", buf, 4 + 6 * 4)[0]
        off = 32  # 4 magic + 7×4 fields
        nodes_scaled = np.frombuffer(buf, dtype="<i4",
                                     count=num_nodes * 2, offset=off)
        off += num_nodes * 2 * 4
    elif version == SZCI_VERSION_SHARDED:
        (_, num_nodes, num_edges, num_names, names_bytes, num_cells,
         _cell_scale_unsigned, num_node_shards,
         _nodes_per_shard) = struct.unpack_from("<9I", buf, 4)
        cell_scale_signed = struct.unpack_from("<i", buf, 4 + 6 * 4)[0]
        off = 4 + 9 * 4  # 40
        if nodes_scaled_loader is None:
            # Caller wants metadata only — leave nodes_scaled empty so
            # this path doesn't silently allocate when we can't load.
            nodes_scaled = np.empty(0, dtype=np.int32)
        else:
            parts: list[bytes] = []
            for shard_idx in range(num_node_shards):
                parts.append(nodes_scaled_loader(shard_idx))
            joined = b"".join(parts)
            nodes_scaled = np.frombuffer(joined, dtype="<i4")
            if nodes_scaled.shape[0] != num_nodes * 2:
                raise ValueError(
                    f"node shards yielded {nodes_scaled.shape[0]//2} nodes "
                    f"in {num_node_shards} files, header expected {num_nodes}")
    elif version == SZCI_VERSION_CELL_COORDS:
        (_, num_nodes, num_edges, num_names, names_bytes, num_cells,
         _cell_scale_unsigned) = struct.unpack_from("<7I", buf, 4)
        cell_scale_signed = struct.unpack_from("<i", buf, 4 + 6 * 4)[0]
        off = 32
        nodes_scaled = np.empty(0, dtype=np.int32)
    else:
        raise ValueError(f"Unsupported SZCI version: {version}")

    cell_lat_idx = np.empty(num_cells, dtype=np.int32)
    cell_lon_idx = np.empty(num_cells, dtype=np.int32)
    cell_base_node = np.zeros(num_cells, dtype=np.uint32)
    cell_node_count = np.empty(num_cells, dtype=np.uint32)
    cell_edge_count = np.empty(num_cells, dtype=np.uint32)
    cell_geom_count = np.empty(num_cells, dtype=np.uint32)
    cell_id_by_key: dict[tuple[int, int], int] = {}
    for cid in range(num_cells):
        if version == SZCI_VERSION_CELL_COORDS:
            la, lo, base, nc, ec, gc = struct.unpack_from("<iiIIII", buf, off)
            cell_base_node[cid] = base
            off += 24
        else:
            la, lo, nc, ec, gc = struct.unpack_from("<iiIII", buf, off)
            off += 20
        cell_lat_idx[cid] = la
        cell_lon_idx[cid] = lo
        cell_node_count[cid] = nc
        cell_edge_count[cid] = ec
        cell_geom_count[cid] = gc
        cell_id_by_key[(la, lo)] = cid

    name_offsets = np.frombuffer(buf, dtype="<u4",
                                 count=num_names + 1, offset=off)
    off += (num_names + 1) * 4
    names_blob = bytes(buf[off:off + names_bytes])

    return SZCIIndex(
        version=version,
        num_nodes=num_nodes,
        num_edges=num_edges,
        num_names=num_names,
        num_cells=num_cells,
        cell_scale=cell_scale_signed,
        nodes_scaled=nodes_scaled,
        name_offsets=name_offsets,
        names_blob=names_blob,
        cell_lat_idx=cell_lat_idx,
        cell_lon_idx=cell_lon_idx,
        cell_base_node=cell_base_node,
        cell_node_count=cell_node_count,
        cell_edge_count=cell_edge_count,
        cell_geom_count=cell_geom_count,
        cell_id_by_key=cell_id_by_key,
    )


def parse_szrc(buf: bytes, *, base_node: int = 0) -> SZRCCell:
    if buf[:4] != SZRC_MAGIC:
        raise ValueError("Not a SZRC cell (bad magic)")
    (version, cell_id, node_count, edge_count, geom_count,
     geom_bytes) = struct.unpack_from("<6I", buf, 4)
    if version not in (SZRC_VERSION_LEGACY, SZRC_VERSION_CELL_COORDS):
        raise ValueError(f"Unsupported SZRC version: {version}")
    off = 28
    if version == SZRC_VERSION_LEGACY:
        cell_nodes_global = np.frombuffer(buf, dtype="<u4",
                                          count=node_count, offset=off)
        nodes_scaled = np.empty(0, dtype=np.int32)
        off += node_count * 4
    else:
        cell_nodes_global = np.empty(0, dtype=np.uint32)
        nodes_scaled = np.frombuffer(buf, dtype="<i4",
                                     count=node_count * 2, offset=off)
        off += node_count * 2 * 4
    cell_adj = np.frombuffer(buf, dtype="<u4",
                             count=node_count + 1, offset=off)
    off += (node_count + 1) * 4
    edges = np.frombuffer(buf, dtype="<u4",
                          count=edge_count * 5, offset=off)
    off += edge_count * 5 * 4
    geom_offsets = np.frombuffer(buf, dtype="<u4",
                                 count=geom_count + 1, offset=off)
    off += (geom_count + 1) * 4
    geom_blob = bytes(buf[off:off + geom_bytes])
    from streetzim.routing.restrictions import unpack as _unpack_turns
    turns = _unpack_turns(buf, off + geom_bytes)
    return SZRCCell(
        cell_id=cell_id,
        base_node=base_node,
        node_count=node_count,
        cell_nodes_global=cell_nodes_global,
        nodes_scaled=nodes_scaled,
        cell_adj=cell_adj,
        edges=edges,
        geom_offsets=geom_offsets,
        geom_blob=geom_blob,
        geom_count=geom_count,
        turns=turns,
    )


# ---------------------------------------------------------------------------
# Lazy graph façade — exposes SZRG-like interface, loads cells on demand
# ---------------------------------------------------------------------------


class SpatialGraph:
    """Read-side view over a spatial-chunked graph.

    Holds the SZCI index in memory (names + cell metadata) plus a growing
    cache of SZRC cell data. ``load_cell(cid)`` is invoked
    lazily by ``edges_of_node``; callers can bound residency via ``cache_limit``
    (LRU eviction). For the test harness this is effectively unbounded.
    """

    NO_GEOM = 0xFFFFFFFF

    def __init__(self, index: SZCIIndex,
                 cell_loader,
                 cache_limit: int | None = None):
        """cell_loader: callable (cell_id: int) -> bytes (SZRC buffer)."""
        self._index = index
        self._loader = cell_loader
        self._cells: dict[int, SZRCCell] = {}
        self._lru: list[int] = []  # simple LRU for eviction
        self._cache_limit = cache_limit
        # Diagnostics: which cells the caller has ever touched, how often
        # we re-fetched one that was evicted, and the peak cache size the
        # LRU held. The cells_loaded property (below) is the *current*
        # cache size — useful for memory estimation but misleading when
        # quoted as "cells the routes touched" (LRU may have cycled).
        self._cells_ever_loaded: set[int] = set()
        self._cells_ever_accessed: set[int] = set()
        self._loader_call_count = 0
        self._cache_peak = 0

    # --- SZRG-compat accessors --------------------------------------------
    @property
    def version(self) -> int:
        return 100  # sentinel: spatial format
    @property
    def num_nodes(self) -> int:
        return self._index.num_nodes
    @property
    def num_edges(self) -> int:
        return self._index.num_edges
    @property
    def num_geoms(self) -> int:
        # Geoms are partitioned across cells; this is the union count.
        return int(self._index.cell_geom_count.sum())
    @property
    def num_names(self) -> int:
        return self._index.num_names
    @property
    def nodes_scaled(self) -> np.ndarray:
        """Compatibility accessor for offline tools that need every node.

        SZCI v3 routing itself uses ``node_coords_e7`` and remains lazy.
        Calling this property on v3 intentionally materializes all cells.
        """
        if (self._index.version == SZCI_VERSION_CELL_COORDS
                and self._index.nodes_scaled.shape[0] == 0):
            nodes = np.empty(self.num_nodes * 2, dtype=np.int32)
            for cid in range(self._index.num_cells):
                c = self._ensure_cell(cid)
                s = c.base_node * 2
                nodes[s:s + c.node_count * 2] = c.nodes_scaled
            self._index.nodes_scaled = nodes
        return self._index.nodes_scaled
    @property
    def has_geoms(self) -> bool:
        return True
    @property
    def no_geom(self) -> int:
        return self.NO_GEOM

    def get_name(self, name_idx: int) -> str:
        return self._index.get_name(name_idx)

    # --- Stats ------------------------------------------------------------
    @property
    def cells_loaded(self) -> int:
        """Cells currently resident (bounded by ``cache_limit``). For
        memory planning, use this together with an average cell size."""
        return len(self._cells)

    @property
    def stats(self) -> dict:
        """Lifetime stats that capture LRU cycling, not just the current
        cache occupancy. ``unique_cells_touched`` is the honest answer to
        "how much of the graph did this session actually visit?"."""
        return {
            "cells_currently_cached": len(self._cells),
            "cache_peak": self._cache_peak,
            "unique_cells_touched": len(self._cells_ever_accessed),
            "unique_cells_loaded": len(self._cells_ever_loaded),
            "loader_invocations": self._loader_call_count,
            "cache_misses": self._loader_call_count,
            "cache_hits": max(
                0,
                # Every _ensure_cell call either hit cache or missed (counted).
                # We track touches via edges_of_node side-effect on
                # _cells_ever_accessed; a hit is (touches - misses).
                0,
            ),
        }

    # --- Cell lookup -------------------------------------------------------
    def _ensure_cell(self, cell_id: int) -> SZRCCell:
        self._cells_ever_accessed.add(cell_id)
        c = self._cells.get(cell_id)
        if c is not None:
            # Touch LRU — move to end.
            try:
                self._lru.remove(cell_id)
            except ValueError:
                pass
            self._lru.append(cell_id)
            return c
        buf = self._loader(cell_id)
        self._loader_call_count += 1
        self._cells_ever_loaded.add(cell_id)
        c = parse_szrc(buf, base_node=int(self._index.cell_base_node[cell_id]))
        if c.cell_id != cell_id:
            raise ValueError(
                f"cell_id mismatch: loader returned {c.cell_id} for {cell_id}"
            )
        self._cells[cell_id] = c
        self._lru.append(cell_id)
        if len(self._cells) > self._cache_peak:
            self._cache_peak = len(self._cells)
        if (self._cache_limit is not None
                and len(self._cells) > self._cache_limit):
            evict = self._lru.pop(0)
            self._cells.pop(evict, None)
        return c

    def node_coords_e7(self, global_node_idx: int) -> tuple[int, int]:
        """Return one node coordinate pair without loading unrelated cells."""
        cell_id = self._index.cell_for_node(global_node_idx)
        if cell_id is None:
            raise IndexError(f"node out of range: {global_node_idx}")
        if self._index.version != SZCI_VERSION_CELL_COORDS:
            nodes = self._index.nodes_scaled
            return (int(nodes[global_node_idx * 2]),
                    int(nodes[global_node_idx * 2 + 1]))
        cell = self._ensure_cell(cell_id)
        local = global_node_idx - cell.base_node
        return (int(cell.nodes_scaled[local * 2]),
                int(cell.nodes_scaled[local * 2 + 1]))

    def nearest_node(self, lat_e7: int, lon_e7: int, mode: str = "origin",
                     *, raw: bool = False, travel_mode: str = "drive") -> int:
        """Find the nearest usable node while loading only plausible cells.

        ``raw=True`` returns the plain nearest vertex with no car-ok /
        reach filtering — for harnesses that replay golden (s, e) vertex
        pairs by coordinate and must land on exactly that vertex, not on
        the vertex the viewer would pick for a tap there.

        Distances are planar with longitude scaled by cos(lat), matching
        the JS snappers. Nodes whose every outgoing edge is closed to
        motor vehicles are skipped, and among the nearest few car-ok
        vertices the first whose forward component is at least
        ``SNAP_MIN_REACH`` nodes wins (so a four-node pier fragment next
        to a road doesn't yield "no route"). With ``mode="dest"`` an
        edgeless vertex that has a drivable incoming edge in its own cell
        (end of a one-way spur) is accepted as well — the forward reach
        test is the wrong question for a destination.

        Ties in distance keep scan order (nearer cell first, ascending
        local index within a cell) exactly like the JS shortlist insert,
        so both snappers pick the same vertex on equal distances.

        ``travel_mode`` "walk" / "bike" keeps only vertices with an
        out-edge the mode may use (streetzim.routing.modes.edge_cost),
        counting records against a one-way. There is no edgeless case:
        a graph built for walking and cycling gives every vertex they
        can reach a way out (the record back along a one-way), so a
        vertex without one is a motorway end or similar.
        """
        for_dest = (mode == "dest")
        drive = travel_mode == "drive"
        scale = self._index.cell_scale
        cos_lat = max(0.05, math.cos(math.radians(lat_e7 / 1e7)))
        candidates: list[tuple[float, int]] = []
        for cid in range(self._index.num_cells):
            la = int(self._index.cell_lat_idx[cid])
            lo = int(self._index.cell_lon_idx[cid])
            lat_min = (la * 10_000_000) // scale
            lat_max = ((la + 1) * 10_000_000) // scale
            lon_min = (lo * 10_000_000) // scale
            lon_max = ((lo + 1) * 10_000_000) // scale
            dlat = lat_min - lat_e7 if lat_e7 < lat_min else (
                lat_e7 - lat_max if lat_e7 > lat_max else 0)
            dlon = lon_min - lon_e7 if lon_e7 < lon_min else (
                lon_e7 - lon_max if lon_e7 > lon_max else 0)
            dlon *= cos_lat
            candidates.append((dlat * dlat + dlon * dlon, cid))
        candidates.sort()
        def shortlist(width: int, bound: float = math.inf) -> tuple[list[float], list[int], list[bool]]:
            # The `width` nearest usable vertices, ascending (ties: scan order).
            best_d: list[float] = []   # distances ascending (bisect key)
            best_n: list[int] = []     # parallel global node ids
            best_e: list[bool] = []    # parallel "edgeless" flags
            worst_kept = bound
            for lower_bound, cid in candidates:
                if lower_bound > worst_kept:
                    break
                cell = self._ensure_cell(cid)
                adj = cell.cell_adj
                edges = cell.edges
                for local in range(cell.node_count):
                    dlat = int(cell.nodes_scaled[local * 2]) - lat_e7
                    dlon = (int(cell.nodes_scaled[local * 2 + 1]) - lon_e7) * cos_lat
                    dist = dlat * dlat + dlon * dlon
                    if dist >= worst_kept:
                        continue
                    e_start = int(adj[local])
                    e_end = int(adj[local + 1])
                    if raw:
                        # Plain nearest: keep a one-entry shortlist, no filters.
                        if not best_d or dist < best_d[0]:
                            best_d, best_n, best_e = [dist], [cell.base_node + local], [False]
                            worst_kept = dist
                        continue
                    # A speed-0 out-edge (walk/bike against a one-way) counts
                    # as absent: a one-way's end stays a sink.
                    if not drive:
                        for ei in range(e_start, e_end):
                            if edge_cost(travel_mode, int(edges[ei * 5 + 1]),
                                         int(edges[ei * 5 + 4])) is not None:
                                break
                        else:
                            continue
                        k = bisect.bisect_right(best_d, dist)
                        best_d.insert(k, dist)
                        best_n.insert(k, cell.base_node + local)
                        best_e.insert(k, False)
                        if len(best_d) > width:
                            best_d.pop(); best_n.pop(); best_e.pop()
                        if len(best_d) == width:
                            worst_kept = best_d[-1]
                        continue
                    real = 0
                    car_ok = False
                    for ei in range(e_start, e_end):
                        if int(edges[ei * 5 + 1]) >> 24 == 0:
                            continue
                        real += 1
                        if not _is_no_motor(int(edges[ei * 5 + 4])):
                            car_ok = True
                            break
                    if real == 0:
                        car_ok = True
                    if not car_ok:
                        continue
                    # bisect_right on dist only == the JS "insert after equal
                    # distances" loop: scan order decides ties.
                    k = bisect.bisect_right(best_d, dist)
                    best_d.insert(k, dist)
                    best_n.insert(k, cell.base_node + local)
                    best_e.insert(k, real == 0)
                    if len(best_d) > width:
                        best_d.pop(); best_n.pop(); best_e.pop()
                    if len(best_d) == width:
                        worst_kept = best_d[-1]
            return best_d, best_n, best_e

        # The nearest SNAP_CANDIDATES first; only when none of them reaches
        # the network, the nearest SNAP_CANDIDATES_WIDE (a big station:
        # Zurich HB's dozen nearest walk vertices are all two-node platform
        # and escalator fragments), and then only SNAP_WIDE_EXTRA_M past the
        # nearest: a tap on a small island stays there (the re-snap decides
        # about islands). The fallback stays the nearest vertex.
        fallback = -1
        prev: list[float] | None = None
        for k, extra_m in ((SNAP_CANDIDATES, SNAP_MAX_EXTRA_M),
                           (SNAP_CANDIDATES_WIDE, SNAP_WIDE_EXTRA_M)):
            # The wide pass (as the worker): pass 1's candidates are its
            # first ones and all failed, so start after them, and scan only
            # inside its distance limit.
            start = 0
            if prev is not None:
                lim2 = math.sqrt(prev[0]) + extra_m * 90
                if len(prev) < SNAP_CANDIDATES or math.sqrt(prev[-1]) > lim2:
                    break
                best_d, best_n, best_e = shortlist(k, lim2 * lim2 * (1 + 1e-9) + 1)
                start = len(prev)
            else:
                best_d, best_n, best_e = shortlist(k)
            prev = best_d
            if not best_d:
                return -1
            if raw:
                return best_n[0]
            if fallback < 0:
                fallback = best_n[0]
            limit_r = math.sqrt(best_d[0]) + extra_m * 90
            for dist, node, edgeless in zip(best_d[start:], best_n[start:], best_e[start:]):
                if math.sqrt(dist) > limit_r:
                    break
                if self._reaches_at_least(node, SNAP_MIN_REACH, travel_mode):
                    return node
                if for_dest and edgeless and self._has_drivable_incoming_in_own_cell(
                        node, travel_mode):
                    return node
        return fallback

    def _has_drivable_incoming_in_own_cell(self, node: int,
                                           travel_mode: str = "drive") -> bool:
        """Mirrors routing-worker.js _hasDrivableIncomingInOwnCell: some
        edge in the node's own cell targets it and is drivable."""
        cid = self._index.cell_for_node(node)
        if cid is None:
            return False
        cell = self._ensure_cell(cid)
        edges = cell.edges
        for ei in range(edges.shape[0] // 5):
            if int(edges[ei * 5]) != node:
                continue
            if not _mode_ok(travel_mode, int(edges[ei * 5 + 1]), int(edges[ei * 5 + 4])):
                continue
            return True
        return False

    def _reaches_at_least(self, node: int, limit: int,
                          travel_mode: str = "drive") -> bool:
        """Bounded forward BFS over edges usable by ``travel_mode``
        (mirrors the worker)."""
        seen = {node}
        queue = [node]
        head = 0
        while head < len(queue):
            if len(seen) >= limit:
                return True
            cur = queue[head]
            head += 1
            for (target, speed_dist, _gi, _ni, ca) in self.edges_of_node(cur):
                if not _mode_ok(travel_mode, speed_dist, ca):
                    continue
                if target not in seen:
                    seen.add(target)
                    queue.append(target)
                    if len(seen) >= limit:
                        return True
        return len(seen) >= limit

    def edges_of_node(self, global_node_idx: int) -> list[tuple[int, int, int, int, int]]:
        """Return list of edges for a global node as (target, speed_dist,
        geom_local, name_idx, class_access) tuples. Empty if the node has
        no outgoing edges (or belongs to a cell that's empty — which
        shouldn't happen since the writer skips empty cells)."""
        cell_id = self._index.cell_for_node(global_node_idx)
        if cell_id is None:
            return []
        cell = self._ensure_cell(cell_id)
        local = cell.local_idx_for(global_node_idx)
        if local is None:
            return []
        e_start = int(cell.cell_adj[local])
        e_end = int(cell.cell_adj[local + 1])
        edges = cell.edges
        out = []
        for ei in range(e_start, e_end):
            base = ei * 5
            out.append((
                int(edges[base]),
                int(edges[base + 1]),
                int(edges[base + 2]),
                int(edges[base + 3]),
                int(edges[base + 4]),
            ))
        return out

    def decode_geom_for_edge(
        self,
        global_node_idx: int,
        edge_idx_in_node: int,
        geom_local: int,
    ) -> list[tuple[float, float]] | None:
        """Decode the polyline attached to a particular edge, resolving the
        cell-local geom index against the cell the source node lives in.
        Returns list of (lon, lat) or None if the edge has no geom."""
        if geom_local == self.NO_GEOM:
            return None
        cell_id = self._index.cell_for_node(global_node_idx)
        if cell_id is None:
            return None
        cell = self._ensure_cell(cell_id)
        gstart = int(cell.geom_offsets[geom_local])
        gend = int(cell.geom_offsets[geom_local + 1])
        if gend <= gstart + 8:
            # One absolute int32 pair only.
            lon0, lat0 = struct.unpack_from("<ii", cell.geom_blob, gstart)
            return [(lon0 / 1e7, lat0 / 1e7)]
        lon0, lat0 = struct.unpack_from("<ii", cell.geom_blob, gstart)
        coords: list[tuple[float, float]] = [(lon0 / 1e7, lat0 / 1e7)]
        i = gstart + 8
        blob = cell.geom_blob
        while i < gend:
            # zigzag varint — lon delta
            raw = 0
            shift = 0
            while True:
                b = blob[i]
                i += 1
                raw |= (b & 0x7F) << shift
                if (b & 0x80) == 0:
                    break
                shift += 7
            dlon = (raw >> 1) ^ -(raw & 1)
            raw = 0
            shift = 0
            while True:
                b = blob[i]
                i += 1
                raw |= (b & 0x7F) << shift
                if (b & 0x80) == 0:
                    break
                shift += 7
            dlat = (raw >> 1) ^ -(raw & 1)
            lon0 += dlon
            lat0 += dlat
            coords.append((lon0 / 1e7, lat0 / 1e7))
        return coords


def spatial_graph_from_memory(index_bytes: bytes,
                              cells: dict[int, bytes]) -> SpatialGraph:
    """Convenience factory used by tests: constructs a SpatialGraph whose
    cell loader just looks up in an in-memory dict."""
    idx = parse_szci(index_bytes)
    def loader(cell_id: int) -> bytes:
        try:
            return cells[cell_id]
        except KeyError as e:
            raise KeyError(f"SpatialGraph cell {cell_id} not in memory dict") from e
    return SpatialGraph(idx, loader)


def load_spatial_from_zim(zim_path: str | Path,
                          cache_limit: int | None = None) -> SpatialGraph:
    """Open a spatial-chunked ZIM. Eager-loads the SZCI index (and any
    nodes_scaled shards); cell files are fetched lazily via the ZIM
    archive when A* walks into them."""
    from libzim.reader import Archive
    arc = Archive(Path(zim_path))
    idx_entry = arc.get_entry_by_path("routing-data/graph-cells-index.bin")
    idx_buf = bytes(idx_entry.get_item().content)

    def shard_loader(shard_idx: int) -> bytes:
        # 3-digit zero-pad matches the writer in build_spatial.
        path = f"routing-data/nodes-scaled-{shard_idx:03d}.bin"
        return bytes(arc.get_entry_by_path(path).get_item().content)

    idx = parse_szci(idx_buf, nodes_scaled_loader=shard_loader)

    def loader(cell_id: int) -> bytes:
        # 5-digit zero-pad matches the writer in cloud/repackage_zim.py.
        path = f"routing-data/graph-cell-{cell_id:05d}.bin"
        entry = arc.get_entry_by_path(path)
        return bytes(entry.get_item().content)

    return SpatialGraph(idx, loader, cache_limit=cache_limit)
