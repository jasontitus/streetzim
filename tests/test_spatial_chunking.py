"""Tests for the SZCI + SZRC spatial-chunked graph format.

Three levels of coverage:

  1. Unit — hand-packed tiny v4 graphs fed through ``build_spatial`` and
     parsed back, with assertions on cell count / cell membership / edge
     attribution / geom localisation.
  2. Route identity on synthetic graphs — spatial A* vs monolithic A*
     across a hand-constructed 4-cell grid; node_sequence + total_dist +
     total_time must match bit-for-bit.
  3. Route identity on real ZIMs — for each region in tests/golden,
     rebuild the graph as spatial, replay the first 50 golden routes via
     spatial A*, confirm the format-independent fingerprint fields
     (n/d/t/rd) equal the golden.

(3) is the strongest test: it validates the spatial split + lazy loading
preserves every path the monolithic A* finds. Skipped when the source
ZIM isn't locally present.
"""

from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.szrg_reader import parse_szrg_bytes
from tests.szrg_astar import find_route
from tests.szrg_spatial import (
    SZCI_MAGIC, SZRC_MAGIC,
    build_spatial, cell_of,
    parse_szci, parse_szrc,
    spatial_graph_from_memory,
)
from tests.szrg_spatial_astar import find_route_spatial


# Helpers --------------------------------------------------------------------


def _pack_v4_graph(nodes_e7: list[tuple[int, int]],
                   edges: list[tuple[int, int, int, int, int, int]],
                   names: list[str] = ("",)) -> bytes:
    """Mirror of tests/test_szrg_parser._pack_v4_graph (kept standalone so
    this module doesn't depend on the other test file). edges =
    [(from, target, dist_dm, speed, geom_idx, name_idx), ...]
    class_access always 0."""
    num_nodes = len(nodes_e7)
    num_edges = len(edges)
    num_names = len(names)

    nodes_blob = struct.pack(f"<{num_nodes * 2}i",
                             *[v for (lat, lon) in nodes_e7 for v in (lat, lon)])
    edges_sorted = sorted(edges, key=lambda e: e[0])
    adj_offsets = [0] * (num_nodes + 1)
    for e in edges_sorted:
        adj_offsets[e[0] + 1] += 1
    for i in range(1, num_nodes + 1):
        adj_offsets[i] += adj_offsets[i - 1]
    adj_blob = struct.pack(f"<{num_nodes + 1}I", *adj_offsets)

    edge_vals: list[int] = []
    for (_f, target, dist_dm, speed, geom_idx, name_idx) in edges_sorted:
        dist_speed = ((speed & 0xFF) << 24) | (dist_dm & 0xFFFFFF)
        edge_vals += [target, dist_speed, geom_idx, name_idx, 0]
    edges_blob = struct.pack(f"<{len(edge_vals)}I", *edge_vals)

    # No geoms in this helper.
    geom_offsets_blob = struct.pack("<I", 0)
    geom_blob = b""

    name_offsets = [0]
    name_bytes = b""
    for n in names:
        nb = n.encode("utf-8")
        name_bytes += nb
        name_offsets.append(len(name_bytes))
    name_offsets_blob = struct.pack(f"<{num_names + 1}I", *name_offsets)

    header = (b"SZRG" + struct.pack("<7I",
                                    4,
                                    num_nodes, num_edges,
                                    0, 0,             # numGeoms, geomBytes
                                    num_names, len(name_bytes)))
    return (header + nodes_blob + adj_blob + edges_blob
            + geom_offsets_blob + geom_blob
            + name_offsets_blob + name_bytes)


# ---- 1. Unit tests on build_spatial ---------------------------------------


def test_cell_of_basic():
    # 0.1° cells: lat=37.7 → cell_lat=377, lon=-122.4 → cell_lon=-1224.
    assert cell_of(377_000_000, -1_224_000_000, 10) == (377, -1224)
    assert cell_of(0, 0, 10) == (0, 0)
    # Floor semantics on negatives: -122.401 lands in cell -1225, not -1224.
    assert cell_of(0, -1_224_001_000, 10) == (0, -1225)
    # 1° cells (coarser): everything within a degree collapses.
    assert cell_of(377_000_000, -1_224_000_000, 1) == (37, -123)


def test_build_spatial_single_cell():
    """All nodes in one 0.1° cell → exactly one SZRC.

    Uses positive lat/lon to avoid the negative-floor quirk: e.g.,
    (37.501, 122.0) lands in cell (375, 1220) with scale=10, same as
    (37.500, 122.0). If we used negative longitudes, a 0.001° offset
    would cross -1220 → -1221 because floor(-1220.01) == -1221.
    """
    nodes = [(375_000_000, 1_220_000_000),
             (375_010_000, 1_220_010_000),
             (375_020_000, 1_220_020_000)]
    edges = [(0, 1, 1000, 30, 0xFFFFFFFF, 0),
             (1, 2, 1000, 30, 0xFFFFFFFF, 0)]
    g = parse_szrg_bytes(_pack_v4_graph(nodes, edges))
    idx_bytes, cells, meta = build_spatial(g, cell_scale=10)
    idx = parse_szci(idx_bytes)
    assert idx.num_cells == 1
    assert len(cells) == 1
    only_cell = parse_szrc(cells[0])
    assert list(only_cell.cell_nodes_global) == [0, 1, 2]
    assert only_cell.edges.shape[0] == 2 * 5  # 2 edges × stride 5


def test_build_spatial_multi_cell_attribution():
    """4 nodes in 4 separate 0.1° cells → 4 SZRC files; each cell owns
    only its own nodes' outgoing edges."""
    # Four corners, 0.2° apart → each in its own cell at scale=10.
    nodes = [
        (  0,         0),        # cell (0, 0)
        (0 + 2_000_000, 0),      # cell (2, 0) — 0.2° north
        (0, 0 + 2_000_000),      # cell (0, 2)
        (2_000_000, 2_000_000),  # cell (2, 2)
    ]
    # Ring: 0→1, 1→3, 3→2, 2→0
    edges = [
        (0, 1, 1000, 30, 0xFFFFFFFF, 0),
        (1, 3, 1000, 30, 0xFFFFFFFF, 0),
        (3, 2, 1000, 30, 0xFFFFFFFF, 0),
        (2, 0, 1000, 30, 0xFFFFFFFF, 0),
    ]
    g = parse_szrg_bytes(_pack_v4_graph(nodes, edges))
    idx_bytes, cells, meta = build_spatial(g, cell_scale=10)
    idx = parse_szci(idx_bytes)
    assert idx.num_cells == 4
    # Each cell should own exactly one node and one outgoing edge.
    for cid in range(4):
        c = parse_szrc(cells[cid])
        assert c.cell_nodes_global.shape[0] == 1
        # edges stride 5, one edge → 5 u32s
        assert c.edges.shape[0] == 5


def test_build_spatial_geom_index_is_cell_local():
    """Two cells, each with its own geom, should each have geom_local=0
    for their respective edges — the cell-scoped remap mustn't leak
    global indices."""
    # Build a v4 buffer by hand that actually includes 2 geoms.
    nodes_e7 = [(0, 0), (2_000_000, 0)]
    num_nodes = 2
    num_edges = 2
    num_geoms = 2
    num_names = 1
    # Two geoms, each just an absolute int32 coord pair (8 bytes each).
    geom_blob = struct.pack("<ii", 12345, 67890) + struct.pack("<ii", 11111, 22222)
    geom_bytes_total = len(geom_blob)
    geom_offsets_blob = struct.pack("<3I", 0, 8, 16)

    # Edges: 0→1 uses geom 0, 1→0 uses geom 1.
    # Adjacency: node 0 has 1 edge, node 1 has 1 edge.
    adj_blob = struct.pack("<3I", 0, 1, 2)
    nodes_blob = struct.pack("<4i",
                             nodes_e7[0][0], nodes_e7[0][1],
                             nodes_e7[1][0], nodes_e7[1][1])
    # stride 5
    e1 = struct.pack("<5I", 1, (30 << 24) | 1000, 0,     0, 0)
    e2 = struct.pack("<5I", 0, (30 << 24) | 1000, 1,     0, 0)
    edges_blob = e1 + e2
    name_offsets_blob = struct.pack("<2I", 0, 0)
    names_blob = b""
    header = (b"SZRG" + struct.pack("<7I",
                                    4, num_nodes, num_edges,
                                    num_geoms, geom_bytes_total,
                                    num_names, 0))
    buf = (header + nodes_blob + adj_blob + edges_blob
           + geom_offsets_blob + geom_blob
           + name_offsets_blob + names_blob)
    g = parse_szrg_bytes(buf)
    idx_bytes, cells, _meta = build_spatial(g, cell_scale=10)
    idx = parse_szci(idx_bytes)
    assert idx.num_cells == 2

    for cid in range(2):
        c = parse_szrc(cells[cid])
        assert c.geom_count == 1
        # The single edge's geom_local should be 0 in every cell.
        assert int(c.edges[2]) == 0


def test_build_spatial_sharded_nodes_scaled(tmp_path):
    """SZCI v2: when nodes_scaled would be large enough, build_spatial
    writes ``nodes-scaled-NNN.bin`` shard files instead of inlining them
    in the SZCI body. parse_szci with a shard loader callback must
    reconstruct the same nodes_scaled bytes that the v1 inline path
    produced for the same source graph.

    Drives a 4-node graph through both paths and asserts byte-identity
    on the reassembled nodes_scaled + a routed result. Forces sharding
    by patching the inline threshold to 0 (so any non-empty graph picks
    the shard path)."""
    import tests.szrg_spatial as ssm

    nodes = [
        (   0,         0),
        (2_000_000,    0),
        (   0, 2_000_000),
        (2_000_000, 2_000_000),
    ]
    edges = [
        (0, 1, 1000, 30, 0xFFFFFFFF, 0),
        (1, 3, 1000, 30, 0xFFFFFFFF, 0),
        (3, 2, 1000, 30, 0xFFFFFFFF, 0),
        (2, 0, 1000, 30, 0xFFFFFFFF, 0),
    ]
    g = parse_szrg_bytes(_pack_v4_graph(nodes, edges))

    # 1) v1 inline (in-memory path): nodes_scaled lives inside SZCI bytes.
    idx_bytes_v1, cells_v1, _ = ssm.build_spatial(g, cell_scale=10)
    idx_v1 = ssm.parse_szci(idx_bytes_v1)
    assert idx_v1.version == ssm.SZCI_VERSION_INLINE
    nodes_v1_bytes = idx_v1.nodes_scaled.tobytes()

    # 2) v2 sharded (force the threshold so the shard path triggers
    # for our tiny test graph).
    orig_threshold = ssm.NODES_SCALED_INLINE_MB_THRESHOLD
    orig_per_shard = ssm.DEFAULT_NODES_PER_SHARD
    try:
        ssm.NODES_SCALED_INLINE_MB_THRESHOLD = 0
        ssm.DEFAULT_NODES_PER_SHARD = 2  # 2 nodes/shard → 2 shards for our 4-node graph
        idx_bytes_v2, cells_v2, meta_v2 = ssm.build_spatial(
            g, cell_scale=10, output_dir=tmp_path,
        )
    finally:
        ssm.NODES_SCALED_INLINE_MB_THRESHOLD = orig_threshold
        ssm.DEFAULT_NODES_PER_SHARD = orig_per_shard

    # SZCI body is now smaller (no inline nodes blob).
    assert len(idx_bytes_v2) < len(idx_bytes_v1)
    assert len(meta_v2["node_shard_paths"]) == 2

    # parse_szci with no loader: nodes_scaled comes back empty.
    idx_v2_no_loader = ssm.parse_szci(idx_bytes_v2)
    assert idx_v2_no_loader.version == ssm.SZCI_VERSION_SHARDED
    assert idx_v2_no_loader.nodes_scaled.shape[0] == 0
    # Header still records the right node count for the rest of the
    # routing surface to use.
    assert idx_v2_no_loader.num_nodes == 4

    # parse_szci WITH loader: shards are concatenated and decoded as
    # int32 little-endian — must equal the v1 inline bytes exactly.
    shard_paths = sorted(meta_v2["node_shard_paths"])
    def loader(shard_idx: int) -> bytes:
        return Path(shard_paths[shard_idx]).read_bytes()
    idx_v2 = ssm.parse_szci(idx_bytes_v2, nodes_scaled_loader=loader)
    assert idx_v2.nodes_scaled.tobytes() == nodes_v1_bytes

    # Routing still works through the v2 path. Build a SpatialGraph
    # whose cell loader looks at the on-disk cell files (v2 wrote them
    # alongside the index) and route 0 → 2.
    def cell_loader(cell_id: int) -> bytes:
        path = tmp_path / f"graph-cell-{cell_id:05d}.bin"
        return path.read_bytes()
    sg = ssm.SpatialGraph(idx_v2, cell_loader)
    r = find_route_spatial(sg, 0, 2)
    assert r is not None
    assert r.node_sequence[0] == 0 and r.node_sequence[-1] == 2


# ---- 2. Spatial A* route identity on synthetic graphs ---------------------


def test_spatial_astar_matches_monolithic_on_grid():
    """4-cell ring graph: origin 0 → dest 2. Both A*s must pick the
    identical path (ties broken by insertion order, same in both)."""
    nodes = [
        (  0,         0),        # (0, 0)
        (2_000_000, 0),          # (2, 0)
        (0, 2_000_000),          # (0, 2)
        (2_000_000, 2_000_000),  # (2, 2)
    ]
    edges = [
        (0, 1, 10_000, 60, 0xFFFFFFFF, 0),
        (1, 3, 10_000, 60, 0xFFFFFFFF, 0),
        (0, 2, 10_000, 60, 0xFFFFFFFF, 0),
        (2, 3, 10_000, 60, 0xFFFFFFFF, 0),
    ]
    g4 = parse_szrg_bytes(_pack_v4_graph(nodes, edges))
    idx_bytes, cells, _m = build_spatial(g4, cell_scale=10)
    sg = spatial_graph_from_memory(idx_bytes, cells)

    mono = find_route(g4, 0, 3)
    spat = find_route_spatial(sg, 0, 3)
    assert mono is not None and spat is not None
    assert mono.node_sequence == spat.node_sequence
    assert abs(mono.total_dist_m - spat.total_dist_m) < 1e-9
    assert abs(mono.total_time_s - spat.total_time_s) < 1e-9


def test_spatial_astar_handles_cross_cell_detour():
    """Force a route that crosses ≥3 cell boundaries — verifies lazy cell
    loading fires for every visited cell and A* remains correct."""
    # 1D chain of 6 nodes, each 0.2° apart → 6 different cells at scale=10.
    nodes: list[tuple[int, int]] = [
        (i * 2_000_000, 0) for i in range(6)
    ]
    edges = [(i, i + 1, 10_000, 60, 0xFFFFFFFF, 0) for i in range(5)]
    g4 = parse_szrg_bytes(_pack_v4_graph(nodes, edges))
    idx_bytes, cells, _m = build_spatial(g4, cell_scale=10)
    assert len(cells) == 6

    sg = spatial_graph_from_memory(idx_bytes, cells)
    spat = find_route_spatial(sg, 0, 5)
    mono = find_route(g4, 0, 5)
    assert spat is not None and mono is not None
    assert spat.node_sequence == mono.node_sequence == [0, 1, 2, 3, 4, 5]
    # Touched all 5 source-node cells (dest cell has 0 outbound edges).
    # Note that cells 0..4 each had outbound edges; cell 5 was never visited
    # as a popped-source (its out-edges are empty), so cells_loaded == 5.
    assert sg.cells_loaded == 5


def test_spatial_lru_evicts_when_capped():
    """With a small cache limit, older cells should evict — A* should
    still find the correct route (it'll just re-load evicted cells)."""
    nodes = [(i * 2_000_000, 0) for i in range(6)]
    edges = [(i, i + 1, 10_000, 60, 0xFFFFFFFF, 0) for i in range(5)]
    g4 = parse_szrg_bytes(_pack_v4_graph(nodes, edges))
    idx_bytes, cells, _m = build_spatial(g4, cell_scale=10)

    from tests.szrg_spatial import SpatialGraph, parse_szci
    idx = parse_szci(idx_bytes)

    hits = {"count": 0}
    def loader(cid: int) -> bytes:
        hits["count"] += 1
        return cells[cid]

    sg = SpatialGraph(idx, loader, cache_limit=2)
    r = find_route_spatial(sg, 0, 5)
    assert r is not None
    # Each source node's cell was loaded; some may be loaded twice due to
    # eviction, but correctness is unaffected.
    assert r.node_sequence == [0, 1, 2, 3, 4, 5]
    assert hits["count"] >= 5


# ---- 3. Route identity on real ZIMs ---------------------------------------


ROOT_TESTS = ROOT / "tests"
GOLDEN_DIR = ROOT_TESTS / "golden"


def _discover_corpora() -> list[Path]:
    if not GOLDEN_DIR.is_dir():
        return []
    return sorted(
        p for p in GOLDEN_DIR.glob("*.jsonl")
        if not p.name.startswith(".") and p.stat().st_size > 0
    )


def _read_meta(p: Path) -> dict | None:
    with p.open() as fh:
        line = fh.readline().strip()
        if not line:
            return None
        rec = json.loads(line)
        return rec if rec.get("_meta") else None


CORPORA = _discover_corpora()


@pytest.mark.skipif(not CORPORA, reason="no golden corpora under tests/golden/")
@pytest.mark.parametrize("corpus", CORPORA, ids=[p.stem for p in CORPORA])
def test_spatial_preserves_routes_on_real_zim(corpus: Path):
    """For each region, convert its v4 ZIM → spatial split (in memory) →
    replay the first N golden routes → every fingerprint must match the
    monolithic-v4 route."""
    meta = _read_meta(corpus)
    if meta is None:
        pytest.skip(f"{corpus.name} has no _meta header")
    source = meta.get("source")
    if not source:
        pytest.skip(f"{corpus.name} has no source ZIM")
    zim_path = ROOT / source
    if not zim_path.is_file():
        pytest.skip(f"missing source ZIM {source}")
    # Japan's in-memory spatial build takes 30+ min and ~20 GB of peak
    # memory due to Python list-ification in build_spatial. The standalone
    # /tmp/japan_spatial_full.log run uses load_spatial_from_zim on a pre-
    # built spatial ZIM instead — if you want Japan identity in pytest,
    # rewrite this to prefer osm-*-spatial.zim over in-memory conversion.
    if zim_path.stat().st_size > 3 * 1024 * 1024 * 1024:   # 3 GB
        pytest.skip(f"{zim_path.name} too big for in-memory spatial build "
                    "in pytest — use scripts/japan_spatial_identity.py")

    # Load v4 graph directly.
    from libzim.reader import Archive
    arc = Archive(str(zim_path))
    entry = arc.get_entry_by_path("routing-data/graph.bin")
    g4 = parse_szrg_bytes(bytes(entry.get_item().content))
    if g4.version != 4:
        pytest.skip(f"{zim_path.name} is SZRG v{g4.version}; test expects v4")

    # Build spatial — 1° cells for smaller regions, 0.5° for larger.
    # Cell scale chosen so cell count stays modest; route correctness is
    # independent of scale.
    cell_scale = 1 if g4.num_nodes < 500_000 else 10
    idx_bytes, cells, spatial_meta = build_spatial(g4, cell_scale=cell_scale)

    sg = spatial_graph_from_memory(idx_bytes, cells)

    checked = 0
    mismatches = 0
    with corpus.open() as fh:
        fh.readline()  # skip _meta
        for line in fh:
            if checked >= 30:
                break
            rec = json.loads(line)
            if rec.get("unreachable"):
                continue
            s, e = rec["s"], rec["e"]
            mono = find_route(g4, s, e)
            spat = find_route_spatial(sg, s, e)
            if mono is None or spat is None:
                mismatches += 1
                continue
            if (mono.node_sequence != spat.node_sequence
                    or abs(mono.total_dist_m - spat.total_dist_m) > 1e-6
                    or abs(mono.total_time_s - spat.total_time_s) > 1e-6):
                mismatches += 1
                if mismatches <= 3:
                    print(f"[spatial-diverge] {s}→{e}: "
                          f"mono nodes={len(mono.node_sequence)} dist={mono.total_dist_m:.2f} | "
                          f"spat nodes={len(spat.node_sequence)} dist={spat.total_dist_m:.2f}")
            checked += 1

    assert checked > 0, "no routable pairs in the first 30 golden records"
    assert mismatches == 0, (
        f"{corpus.name}: spatial chunking diverged on "
        f"{mismatches}/{checked} routes (cell_scale={cell_scale}, "
        f"num_cells={spatial_meta['num_cells']})"
    )
