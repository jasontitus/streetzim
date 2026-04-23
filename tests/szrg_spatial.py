"""Spatial-chunked SZRG (SZCI index + SZRC per-cell files).

Splits a v4/v5 SZRG graph into an eagerly-loaded index (global nodes +
names + cell metadata) plus one file per spatial cell (edges + geoms for
that cell's nodes). A* loads cells lazily as its frontier crosses
boundaries — on a country-scale graph this caps peak memory at the set
of cells touched by the route, not the whole graph.

Node indices are **preserved** from the source graph so v4 golden-corpus
fingerprints diff directly against spatial output — route identity is
the test. Cells are sequential IDs; their (lat_cell, lon_cell) keys live
in the index.

Companion format to resources/viewer/index.html + mcpzim SZRGGraph. This
module is the reference implementation; language ports mirror it.

File formats (little-endian, u32 unless stated):

  SZCI (cells index, 32-byte header then global tables):
    "SZCI" magic (4)
    u32 version = 1
    u32 num_nodes
    u32 num_edges
    u32 num_names
    u32 names_bytes
    u32 num_cells
    i32 cell_scale       -- e.g., 10 ⇒ 0.1° cells; 1 ⇒ 1° cells
    Int32[num_nodes * 2] -- lat_e7, lon_e7 in source order
    -- Cell metadata (num_cells × 20 bytes):
    foreach cell:
      i32 lat_cell_idx, i32 lon_cell_idx
      u32 node_count, u32 edge_count, u32 geom_count
    u32[num_names + 1]   -- name_offsets
    bytes[names_bytes]   -- names_blob

  SZRC (one cell, 24-byte header then per-cell tables):
    "SZRC" magic (4)
    u32 version = 1
    u32 cell_id
    u32 node_count
    u32 edge_count
    u32 geom_count
    u32 geom_bytes
    u32[node_count]           -- cell_nodes_global (sorted ascending)
    u32[node_count + 1]       -- cell_adj (offsets into cell_edges)
    u32[edge_count * 5]       -- edges: target_global, speed_dist, geom_local, name, class_access
    u32[geom_count + 1]       -- geom_offsets (bytes into geom_blob)
    bytes[geom_bytes]         -- geom_blob (zigzag-varint polylines, same as SZRG)
"""

from __future__ import annotations

import bisect
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from tests.szrg_reader import SZRG, parse_szrg_bytes


SZCI_MAGIC = b"SZCI"
SZRC_MAGIC = b"SZRC"
SZCI_VERSION = 1
SZRC_VERSION = 1

DEFAULT_CELL_SCALE = 10  # 0.1° cells — ~11 km lat; lon varies by latitude


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


def build_spatial(g: SZRG, *, cell_scale: int = DEFAULT_CELL_SCALE,
                  ) -> tuple[bytes, dict[int, bytes], dict]:
    """Split `g` into (SZCI index bytes, {cell_id: SZRC bytes}, meta).

    Returns:
      index_bytes: SZCI buffer, eager-loaded at startup.
      cells: per-cell SZRC buffers keyed by sequential cell_id.
      meta: {'num_cells': n, 'cell_coords': [(lat_cell, lon_cell), ...],
             'total_bytes': int} for diagnostics.
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

    # Materialize hot columns once as Python lists — row-wise dereference
    # through numpy inside tight loops is 10× slower than list access.
    nodes = g.nodes_scaled.tolist()           # [lat_e7, lon_e7, ...]
    adj = g.adj_offsets.tolist()
    edges_flat = g.edges.tolist()             # stride 5 per edge
    geom_offsets = g.geom_offsets.tolist()
    geom_blob = g.geom_blob
    stride = g.edge_stride

    # --- Pass 1: assign each node to its cell, bucket by (lat_idx, lon_idx)
    # node_cell_key[node_idx] = (lat_cell_idx, lon_cell_idx) for later lookup.
    node_cell_key: list[tuple[int, int]] = [None] * num_nodes  # type: ignore[list-item]
    cell_buckets: dict[tuple[int, int], list[int]] = {}
    for n in range(num_nodes):
        key = cell_of(nodes[n * 2], nodes[n * 2 + 1], cell_scale)
        node_cell_key[n] = key
        bucket = cell_buckets.get(key)
        if bucket is None:
            cell_buckets[key] = [n]
        else:
            bucket.append(n)

    # Sequential cell IDs — ordered by (lat_cell_idx, lon_cell_idx) for a
    # stable-across-builds ID assignment. Keeps diffs of the index file
    # readable when someone re-builds with a tweaked cell scale.
    cell_coords_sorted = sorted(cell_buckets.keys())
    cell_id_of: dict[tuple[int, int], int] = {
        k: i for i, k in enumerate(cell_coords_sorted)
    }

    # --- Pass 2: per-cell edge + geom collection.
    # Walk the global adj in source order, attribute each edge to the cell
    # of its SOURCE node. Remap geom_idx into a cell-local index (cells
    # have disjoint geom ranges — a geom only appears on edges from a
    # single source node, hence in a single cell).
    num_cells = len(cell_coords_sorted)
    cell_sorted_nodes: list[list[int]] = [sorted(cell_buckets[k])
                                          for k in cell_coords_sorted]
    cell_adj: list[list[int]] = [[0] for _ in range(num_cells)]
    cell_edges: list[list[int]] = [[] for _ in range(num_cells)]
    cell_geom_map: list[dict[int, int]] = [dict() for _ in range(num_cells)]
    cell_geom_order: list[list[int]] = [[] for _ in range(num_cells)]

    NO_GEOM = g.no_geom

    for n in range(num_nodes):
        cid = cell_id_of[node_cell_key[n]]
        e_start = adj[n]
        e_end = adj[n + 1]
        cedges = cell_edges[cid]
        cmap = cell_geom_map[cid]
        corder = cell_geom_order[cid]
        for ei in range(e_start, e_end):
            base = ei * stride
            target = edges_flat[base]
            speed_dist = edges_flat[base + 1]
            geom_idx = edges_flat[base + 2]
            name_idx = edges_flat[base + 3]
            class_access = edges_flat[base + 4]
            # Translate geom_idx to cell-local. 0xFFFFFFFF stays as the
            # sentinel (it means "no geom"); real indices get a local
            # number assigned on first use.
            if geom_idx == NO_GEOM:
                local_gi = 0xFFFFFFFF
            else:
                local_gi = cmap.get(geom_idx)
                if local_gi is None:
                    local_gi = len(corder)
                    cmap[geom_idx] = local_gi
                    corder.append(geom_idx)
            cedges.append(target)
            cedges.append(speed_dist)
            cedges.append(local_gi)
            cedges.append(name_idx)
            cedges.append(class_access)
        cell_adj[cid].append(len(cedges) // 5)

    # Convert each node-index-in-cell offset in cell_adj from "end position
    # after this node's edges in the flat 5-stride array" to local indexing.
    # The above loop already pushes one entry per node (post-emit length),
    # but we need the STABLE ordering of cell_sorted_nodes (sorted ascending),
    # not the source-order traversal. We therefore rewrite cell_adj based on
    # an explicit per-cell node iteration below.

    # Rebuild cell_adj via explicit per-cell iteration so adjacency matches
    # cell_sorted_nodes order (sorted ascending). This is cheaper than
    # tracking per-node positions during pass 2.
    cell_adj_sorted: list[list[int]] = [[0] for _ in range(num_cells)]
    cell_edges_sorted: list[list[int]] = [[] for _ in range(num_cells)]
    cell_geom_map_sorted: list[dict[int, int]] = [dict() for _ in range(num_cells)]
    cell_geom_order_sorted: list[list[int]] = [[] for _ in range(num_cells)]

    for cid in range(num_cells):
        nodes_in_cell = cell_sorted_nodes[cid]
        cedges = cell_edges_sorted[cid]
        cmap = cell_geom_map_sorted[cid]
        corder = cell_geom_order_sorted[cid]
        cadj = cell_adj_sorted[cid]
        for n in nodes_in_cell:
            e_start = adj[n]
            e_end = adj[n + 1]
            for ei in range(e_start, e_end):
                base = ei * stride
                target = edges_flat[base]
                speed_dist = edges_flat[base + 1]
                geom_idx = edges_flat[base + 2]
                name_idx = edges_flat[base + 3]
                class_access = edges_flat[base + 4]
                if geom_idx == NO_GEOM:
                    local_gi = 0xFFFFFFFF
                else:
                    local_gi = cmap.get(geom_idx)
                    if local_gi is None:
                        local_gi = len(corder)
                        cmap[geom_idx] = local_gi
                        corder.append(geom_idx)
                cedges.append(target)
                cedges.append(speed_dist)
                cedges.append(local_gi)
                cedges.append(name_idx)
                cedges.append(class_access)
            cadj.append(len(cedges) // 5)

    # Use the sorted variants, drop the source-order ones.
    cell_adj = cell_adj_sorted
    cell_edges = cell_edges_sorted
    cell_geom_order = cell_geom_order_sorted

    # --- Serialize SZCI ------------------------------------------------------
    header = SZCI_MAGIC + struct.pack("<7I",
                                      SZCI_VERSION,
                                      num_nodes, num_edges,
                                      num_names, len(g.names_blob),
                                      num_cells,
                                      cell_scale if cell_scale >= 0 else 0)
    # Global nodes (same order as source).
    nodes_blob = g.nodes_scaled.tobytes()  # int32 little-endian, same as file format
    # Cell metadata table
    cell_meta_parts = []
    for cid, (lat_cell, lon_cell) in enumerate(cell_coords_sorted):
        cell_meta_parts.append(struct.pack(
            "<iiIII",
            lat_cell, lon_cell,
            len(cell_sorted_nodes[cid]),
            len(cell_edges[cid]) // 5,
            len(cell_geom_order[cid]),
        ))
    cell_meta_blob = b"".join(cell_meta_parts)

    name_offsets_blob = g.name_offsets.tobytes()
    names_blob = g.names_blob

    index_bytes = (header + nodes_blob + cell_meta_blob
                   + name_offsets_blob + names_blob)

    # --- Serialize each SZRC -------------------------------------------------
    cell_bufs: dict[int, bytes] = {}
    for cid in range(num_cells):
        nodes_in_cell = cell_sorted_nodes[cid]
        cedges = cell_edges[cid]
        cgeom_order = cell_geom_order[cid]
        # Build this cell's geom table by concatenating the referenced
        # geoms' byte ranges from the source blob.
        local_geom_blob = bytearray()
        local_geom_offsets: list[int] = [0]
        for src_gi in cgeom_order:
            gstart = geom_offsets[src_gi]
            gend = geom_offsets[src_gi + 1]
            local_geom_blob.extend(geom_blob[gstart:gend])
            local_geom_offsets.append(len(local_geom_blob))

        cell_header = SZRC_MAGIC + struct.pack(
            "<6I",
            SZRC_VERSION,
            cid,
            len(nodes_in_cell),
            len(cedges) // 5,
            len(cgeom_order),
            len(local_geom_blob),
        )
        body = (
            struct.pack(f"<{len(nodes_in_cell)}I", *nodes_in_cell)
            + struct.pack(f"<{len(cell_adj[cid])}I", *cell_adj[cid])
            + struct.pack(f"<{len(cedges)}I", *cedges)
            + struct.pack(f"<{len(local_geom_offsets)}I", *local_geom_offsets)
            + bytes(local_geom_blob)
        )
        cell_bufs[cid] = cell_header + body

    total_bytes = len(index_bytes) + sum(len(b) for b in cell_bufs.values())
    meta = {
        "num_cells": num_cells,
        "cell_coords": cell_coords_sorted,
        "total_bytes": total_bytes,
        "cell_scale": cell_scale,
    }
    return index_bytes, cell_bufs, meta


# ---------------------------------------------------------------------------
# Reader: lazy cell loader + data structures
# ---------------------------------------------------------------------------


@dataclass
class SZRCCell:
    """In-memory form of one SZRC cell file."""
    cell_id: int
    cell_nodes_global: np.ndarray   # uint32[node_count], sorted ascending
    cell_adj: np.ndarray            # uint32[node_count+1]
    edges: np.ndarray               # uint32[edge_count*5]
    geom_offsets: np.ndarray        # uint32[geom_count+1]
    geom_blob: bytes                # varint polyline blob
    geom_count: int

    def local_idx_for(self, global_node_idx: int) -> int | None:
        """Binary search for a node in cell_nodes_global; None if absent."""
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
    cell_node_count: np.ndarray         # uint32[num_cells]
    cell_edge_count: np.ndarray
    cell_geom_count: np.ndarray
    # Lookup: (lat_cell_idx, lon_cell_idx) → cell_id
    cell_id_by_key: dict[tuple[int, int], int] = field(default_factory=dict)

    def cell_for_node(self, node_idx: int) -> int | None:
        """Compute cell_id for a global node by re-bucketing its coords."""
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


def parse_szci(buf: bytes) -> SZCIIndex:
    if buf[:4] != SZCI_MAGIC:
        raise ValueError("Not a SZCI index (bad magic)")
    (version, num_nodes, num_edges, num_names, names_bytes, num_cells,
     cell_scale) = struct.unpack_from("<7I", buf, 4)
    if version != SZCI_VERSION:
        raise ValueError(f"Unsupported SZCI version: {version}")
    cell_scale_signed = struct.unpack_from("<i", buf, 4 + 6 * 4)[0]

    off = 32
    nodes_scaled = np.frombuffer(buf, dtype="<i4", count=num_nodes * 2, offset=off)
    off += num_nodes * 2 * 4

    cell_lat_idx = np.empty(num_cells, dtype=np.int32)
    cell_lon_idx = np.empty(num_cells, dtype=np.int32)
    cell_node_count = np.empty(num_cells, dtype=np.uint32)
    cell_edge_count = np.empty(num_cells, dtype=np.uint32)
    cell_geom_count = np.empty(num_cells, dtype=np.uint32)
    cell_id_by_key: dict[tuple[int, int], int] = {}
    for cid in range(num_cells):
        la, lo, nc, ec, gc = struct.unpack_from("<iiIII", buf, off)
        cell_lat_idx[cid] = la
        cell_lon_idx[cid] = lo
        cell_node_count[cid] = nc
        cell_edge_count[cid] = ec
        cell_geom_count[cid] = gc
        cell_id_by_key[(la, lo)] = cid
        off += 20

    name_offsets = np.frombuffer(buf, dtype="<u4", count=num_names + 1, offset=off)
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
        cell_node_count=cell_node_count,
        cell_edge_count=cell_edge_count,
        cell_geom_count=cell_geom_count,
        cell_id_by_key=cell_id_by_key,
    )


def parse_szrc(buf: bytes) -> SZRCCell:
    if buf[:4] != SZRC_MAGIC:
        raise ValueError("Not a SZRC cell (bad magic)")
    (version, cell_id, node_count, edge_count, geom_count,
     geom_bytes) = struct.unpack_from("<6I", buf, 4)
    if version != SZRC_VERSION:
        raise ValueError(f"Unsupported SZRC version: {version}")
    off = 28
    cell_nodes_global = np.frombuffer(buf, dtype="<u4",
                                       count=node_count, offset=off)
    off += node_count * 4
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
    return SZRCCell(
        cell_id=cell_id,
        cell_nodes_global=cell_nodes_global,
        cell_adj=cell_adj,
        edges=edges,
        geom_offsets=geom_offsets,
        geom_blob=geom_blob,
        geom_count=geom_count,
    )


# ---------------------------------------------------------------------------
# Lazy graph façade — exposes SZRG-like interface, loads cells on demand
# ---------------------------------------------------------------------------


class SpatialGraph:
    """Read-side view over a spatial-chunked graph.

    Holds the SZCI index in memory (global nodes + names + cell metadata)
    plus a growing cache of SZRC cell data. ``load_cell(cid)`` is invoked
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
        c = parse_szrc(buf)
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
    """Open a spatial-chunked ZIM. Eager-loads the SZCI index; cell files
    are fetched lazily via the ZIM archive when A* walks into them."""
    from libzim.reader import Archive
    arc = Archive(str(zim_path))
    idx_entry = arc.get_entry_by_path("routing-data/graph-cells-index.bin")
    idx = parse_szci(bytes(idx_entry.get_item().content))

    def loader(cell_id: int) -> bytes:
        # 5-digit zero-pad matches the writer in cloud/repackage_zim.py.
        path = f"routing-data/graph-cell-{cell_id:05d}.bin"
        entry = arc.get_entry_by_path(path)
        return bytes(entry.get_item().content)

    return SpatialGraph(idx, loader, cache_limit=cache_limit)
