#!/usr/bin/env python3
"""Routing prototype CLI for testing speedup strategies on a streetzim ZIM.

Compares two strategies on the same src/dst pair:

  1. ``astar``  — single-source A* (baseline; what the PWA + Kiwix apps
                  run today).
  2. ``hwy2``   — proper two-pass: full graph for first/last mile,
                  highway-tier (motorway/trunk/primary, including each
                  *_link) in the middle, then full graph for the last
                  mile. No missing segments: the local-in + highway +
                  local-out legs are concatenated and re-measured on
                  real edge data.

Bidirectional A* was prototyped here briefly (2026-04-25) but removed.
It requires a *reverse* adjacency list to be correct on a directed
graph (oneways, restricted access). The streetzim spatial format only
stores forward edges, so the backward half of bidir was effectively
running forward A* from ``end``, producing a wrong path on any region
with oneway streets. Adding a reverse adjacency would ~double the
in-memory edge data — a real CH preprocessing pass would be the right
fix if we ever go down that road.

Reads a spatial-chunked ZIM (created by ``cloud/repackage_zim.py
--spatial-chunk-scale 1`` or ``cloud/build_region.sh`` post-build).
Doesn't require any rebuild — runs against existing ZIMs.

Usage:
  ./venv312/bin/python3 cloud/route_cli.py \\
      --zim osm-west-asia.zim \\
      --src 35.6892,51.3890 \\
      --dst 33.7433,44.6260 \\
      --mode all
"""
from __future__ import annotations

import argparse
import heapq
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np

from tests.szrg_spatial import SpatialGraph, load_spatial_from_zim
from tests.szrg_spatial_astar import find_route_spatial
from tests.szrg_astar import R_EARTH, HEURISTIC_SPEED_MPS, haversine_m


# Bits 0..4 of class_access store the road-class ordinal (see
# create_osm_zim.CLASS_ORDINAL). Highway tier = motorway + trunk + primary,
# including each *_link variant. These are the edges intercity traffic
# would actually use.
CLASS_ORD_MASK = 0x1F
HIGHWAY_TIER_ORDS = frozenset({1, 2, 3, 4, 5, 6})  # motorway..primary_link


def parse_lat_lon(s: str) -> tuple[float, float]:
    parts = s.replace(" ", "").split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"expected 'lat,lon', got {s!r}")
    return float(parts[0]), float(parts[1])


def nearest_node(g: SpatialGraph, lat: float, lon: float) -> tuple[int, float]:
    """Brute-force nearest-node by haversine. Fast enough on ~20M nodes
    (one numpy pass takes a few seconds)."""
    nodes = g.nodes_scaled
    lats = nodes[0::2].astype(np.float64) / 1e7
    lons = nodes[1::2].astype(np.float64) / 1e7
    dlat = np.radians(lats - lat)
    dlon = np.radians(lons - lon)
    a = (np.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat)) * np.cos(np.radians(lats))
         * np.sin(dlon / 2) ** 2)
    d = 2 * R_EARTH * np.arcsin(np.sqrt(a))
    idx = int(np.argmin(d))
    return idx, float(d[idx])


def nearest_node_filtered(g: SpatialGraph, lat: float, lon: float,
                          *, highway_only: bool) -> tuple[int, float]:
    """Nearest node that has at least one outgoing edge passing the
    highway filter (or any node, when ``highway_only`` is False)."""
    if not highway_only:
        return nearest_node(g, lat, lon)
    # Build candidate node ids by scanning a search radius incrementally.
    # In practice the nearest highway node is within a few km of any
    # populated point, so we walk concentric rings rather than scanning
    # all 20M nodes for the filter.
    nodes = g.nodes_scaled
    lats_arr = nodes[0::2].astype(np.float64) / 1e7
    lons_arr = nodes[1::2].astype(np.float64) / 1e7
    dlat = np.radians(lats_arr - lat)
    dlon = np.radians(lons_arr - lon)
    a = (np.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat)) * np.cos(np.radians(lats_arr))
         * np.sin(dlon / 2) ** 2)
    d = 2 * R_EARTH * np.arcsin(np.sqrt(a))
    # Order indices by distance then walk in batches until we find
    # one with a highway edge.
    order = np.argsort(d)
    for cand in order[:50_000]:
        cid = int(cand)
        for (_t, _sd, _gi, _ni, ca) in g.edges_of_node(cid):
            if (ca & CLASS_ORD_MASK) in HIGHWAY_TIER_ORDS:
                return cid, float(d[cid])
    raise RuntimeError("no highway-tier node within 50k nearest candidates")


def find_route_filtered(
    g: SpatialGraph, start: int, end: int,
    *, highway_only: bool,
    max_pops: int | None = None,
):
    """A* with optional class_access filter. Mirrors
    ``tests.szrg_spatial_astar.find_route_spatial`` but drops edges that
    fail ``highway_only``. Edge skipping happens inline so the heap
    never sees them."""
    if start == end:
        return _route_tuple(0.0, 0.0, [start], [], 0)
    nodes = g.nodes_scaled
    num_nodes = g.num_nodes
    end_lat = int(nodes[end * 2]) / 1e7
    end_lon = int(nodes[end * 2 + 1]) / 1e7

    INF = math.inf
    gscore = [INF] * num_nodes
    gscore[start] = 0.0
    prev = [-1] * num_nodes
    closed = bytearray(num_nodes)

    start_lat = int(nodes[start * 2]) / 1e7
    start_lon = int(nodes[start * 2 + 1]) / 1e7
    h0 = haversine_m(start_lat, start_lon, end_lat, end_lon) / HEURISTIC_SPEED_MPS

    heap: list = []
    counter = 0
    heapq.heappush(heap, (h0, counter, start))
    pops = 0

    end_lat_rad = math.radians(end_lat)
    cos_end_lat = math.cos(end_lat_rad)
    R2 = R_EARTH * 2.0

    while heap:
        _, _, current = heapq.heappop(heap)
        pops += 1
        if max_pops is not None and pops > max_pops:
            return None
        if current == end:
            break
        if closed[current]:
            continue
        closed[current] = 1
        current_g = gscore[current]
        for (target, speed_dist, _gi, _ni, class_access) in g.edges_of_node(current):
            if highway_only and (class_access & CLASS_ORD_MASK) not in HIGHWAY_TIER_ORDS:
                continue
            if closed[target]:
                continue
            speed = speed_dist >> 24
            if speed == 0:
                continue
            dist_m = (speed_dist & 0xFFFFFF) / 10.0
            cost = dist_m / (speed / 3.6)
            new_g = current_g + cost
            if new_g < gscore[target]:
                gscore[target] = new_g
                prev[target] = current
                t_lat = int(nodes[target * 2]) / 1e7
                t_lon = int(nodes[target * 2 + 1]) / 1e7
                dlat = math.radians(end_lat - t_lat)
                dlon = math.radians(end_lon - t_lon)
                shd = math.sin(dlat * 0.5)
                shdlon = math.sin(dlon * 0.5)
                a = (shd * shd + math.cos(math.radians(t_lat)) * cos_end_lat
                     * shdlon * shdlon)
                h = (R2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
                     / HEURISTIC_SPEED_MPS)
                counter += 1
                heapq.heappush(heap, (new_g + h, counter, target))

    if gscore[end] == INF:
        return None

    # Reconstruct node sequence, then measure on real edge data.
    rev = [end]
    n = end
    while n != start:
        p = prev[n]
        if p < 0:
            return None
        rev.append(p)
        n = p
    rev.reverse()
    dist_m, time_s = measure_path(g, rev)
    return _route_tuple(dist_m, time_s, rev, [], pops)


@dataclass
class RouteOut:
    distance_m: float
    time_s: float
    nodes: list
    edges: list
    pops: int


def _route_tuple(d, t, nodes, edges, pops):
    return RouteOut(distance_m=d, time_s=t, nodes=nodes, edges=edges, pops=pops)


def measure_path(g: SpatialGraph, node_sequence: list[int]) -> tuple[float, float]:
    """Sum actual edge distances + times along a reconstructed path.

    A* algorithms here populate g-scores from edge time-cost; when we
    want to *report* distance we have to walk the path again and look
    up real edge dist_m. Same for time (avoids the heuristic-speed
    approximation that produced the 25% over-estimate in earlier
    prototype output).
    """
    total_dist = 0.0
    total_time = 0.0
    for i in range(len(node_sequence) - 1):
        u, v = node_sequence[i], node_sequence[i + 1]
        for (t_node, speed_dist, _gi, _ni, _ca) in g.edges_of_node(u):
            if t_node != v:
                continue
            dist_m = (speed_dist & 0xFFFFFF) / 10.0
            speed = speed_dist >> 24
            total_dist += dist_m
            if speed > 0:
                total_time += dist_m / (speed / 3.6)
            break
    return total_dist, total_time


def _find_route_bidir_DEPRECATED_unsafe(
    g: SpatialGraph, start: int, end: int,
    *, max_pops: int | None = None,
):
    """Bidirectional A* over the spatial graph. Forward A* expands from
    ``start`` toward ``end``; a parallel backward A* expands from
    ``end`` toward ``start``. We track the best meeting node and stop
    when the sum of the two front-frontier f-scores exceeds the best
    candidate so far. With a tight haversine heuristic, this typically
    halves pops on long-distance routes.

    Note: the backward search uses the SAME edge data (we don't have
    a reverse graph). For a bidirectional CH this would be wrong, but
    A* with the haversine heuristic only requires that the heuristic
    be *consistent* (admissible) — and a symmetric haversine is fine
    for our undirected/oneway-relaxed test purposes. (Production CH
    would need a true reverse adjacency list.)
    """
    if start == end:
        return _route_tuple(0.0, 0.0, [start], [], 0)

    nodes = g.nodes_scaled
    num_nodes = g.num_nodes
    INF = math.inf

    def latlon(n):
        return (int(nodes[n * 2]) / 1e7, int(nodes[n * 2 + 1]) / 1e7)

    def heur(a, b):
        a_lat, a_lon = latlon(a)
        b_lat, b_lon = latlon(b)
        return haversine_m(a_lat, a_lon, b_lat, b_lon) / HEURISTIC_SPEED_MPS

    # Forward + backward search state
    g_fwd = [INF] * num_nodes
    g_bwd = [INF] * num_nodes
    g_fwd[start] = 0.0
    g_bwd[end] = 0.0
    closed_fwd = bytearray(num_nodes)
    closed_bwd = bytearray(num_nodes)
    prev_fwd = [-1] * num_nodes
    prev_bwd = [-1] * num_nodes

    h_start_end = heur(start, end)
    heap_fwd: list = []
    heap_bwd: list = []
    heapq.heappush(heap_fwd, (h_start_end, 0, start))
    heapq.heappush(heap_bwd, (h_start_end, 0, end))
    counter = [1]

    best_meet = INF
    meet_node = -1
    pops = 0

    def step(heap, gscore, closed, prev, other_g, other_closed, target_anchor):
        """One A* expansion on the side passed in. Updates best_meet/meet_node
        via closure on outer scope by returning candidate updates."""
        nonlocal best_meet, meet_node, pops
        if not heap:
            return False
        f_top, _, current = heapq.heappop(heap)
        pops += 1
        if max_pops is not None and pops > max_pops:
            return False
        if closed[current]:
            return True  # try next iter
        closed[current] = 1
        # Termination check: when this node is closed on this side AND the
        # opposite side has already closed it, we have a candidate meeting.
        if other_closed[current]:
            cand = gscore[current] + other_g[current]
            if cand < best_meet:
                best_meet = cand
                meet_node = current
        # Standard early-stop: if popped f >= best_meet, this side is done.
        if f_top >= best_meet:
            return False
        for (t_node, speed_dist, _gi, _ni, _ca) in g.edges_of_node(current):
            if closed[t_node]:
                continue
            speed = speed_dist >> 24
            if speed == 0:
                continue
            dist_m = (speed_dist & 0xFFFFFF) / 10.0
            cost = dist_m / (speed / 3.6)
            ng = gscore[current] + cost
            if ng < gscore[t_node]:
                gscore[t_node] = ng
                prev[t_node] = current
                f = ng + heur(t_node, target_anchor)
                counter[0] += 1
                heapq.heappush(heap, (f, counter[0], t_node))
                # Update meeting if other side has reached this neighbor.
                other_d = other_g[t_node]
                if other_d != INF:
                    cand = ng + other_d
                    if cand < best_meet:
                        best_meet = cand
                        meet_node = t_node
        return True

    while True:
        adv_f = step(heap_fwd, g_fwd, closed_fwd, prev_fwd, g_bwd, closed_bwd, end)
        adv_b = step(heap_bwd, g_bwd, closed_bwd, prev_bwd, g_fwd, closed_fwd, start)
        if not (adv_f or adv_b):
            break

    if meet_node < 0:
        return None

    # Reconstruct: walk prev_fwd from meet back to start, then prev_bwd
    # from meet forward to end. prev_bwd[n] is the *successor* of n on
    # the backward search (since we expanded from end), so chasing it
    # gets us toward end.
    fwd_chain: list[int] = [meet_node]
    n = meet_node
    while n != start:
        p = prev_fwd[n]
        if p < 0:
            return None
        fwd_chain.append(p)
        n = p
    fwd_chain.reverse()

    bwd_chain: list[int] = []
    n = meet_node
    while n != end:
        p = prev_bwd[n]
        if p < 0:
            return None
        bwd_chain.append(p)
        n = p
    bwd_chain.append(end)

    seq: list[int] = []
    for n in fwd_chain + bwd_chain:
        if not seq or seq[-1] != n:
            seq.append(n)
    # Re-measure on actual edge data to avoid heuristic-speed inflation.
    dist_m, time_s = measure_path(g, seq)
    return _route_tuple(dist_m, time_s, seq, [], pops)


def find_route_two_pass(
    g: SpatialGraph, start: int, end: int,
    src_lat: float, src_lon: float,
    dst_lat: float, dst_lon: float,
    *, max_pops: int | None = None,
):
    """Proper hierarchical routing: full graph for first/last mile,
    highway-tier for the cross-country middle.

    Algorithm:
      1. Pick a highway entry node near src (nearest by haversine, must
         have ≥1 outgoing motorway/trunk/primary edge).
      2. Pick a highway exit node near dst, same way.
      3. Phase A: A* on full graph, ``start → hw_src``.
      4. Phase B: A* with highway-tier filter, ``hw_src → hw_dst``.
      5. Phase C: A* on full graph, ``hw_dst → end``.
      6. Concatenate node sequences (drop duplicate join nodes), then
         measure_path() over the union for accurate dist/time.

    If start IS the hw_src (e.g. test case is dropped onto a highway
    intersection), Phase A degenerates to the empty path. Same for C.
    """
    hw_src, src_d = nearest_node_filtered(g, src_lat, src_lon, highway_only=True)
    hw_dst, dst_d = nearest_node_filtered(g, dst_lat, dst_lon, highway_only=True)

    pops_total = 0

    def _run(s, e, *, highway_only):
        nonlocal pops_total
        r = find_route_filtered(g, s, e, highway_only=highway_only,
                                max_pops=max_pops)
        if r is None:
            return None
        pops_total += r.pops
        return r

    # Skip degenerate phases (e.g. when start == hw_src).
    seq: list[int] = []
    legs = []  # for diagnostics: (label, dist_km)

    if start != hw_src:
        a = _run(start, hw_src, highway_only=False)
        if a is None: return None
        seq.extend(a.nodes)
        legs.append(("local_in", a.distance_m / 1000))
    else:
        seq.append(start)
        legs.append(("local_in", 0.0))

    if hw_src != hw_dst:
        b = _run(hw_src, hw_dst, highway_only=True)
        if b is None: return None
        # Drop duplicate join node.
        if seq and seq[-1] == b.nodes[0]:
            seq.extend(b.nodes[1:])
        else:
            seq.extend(b.nodes)
        legs.append(("highway", b.distance_m / 1000))
    else:
        legs.append(("highway", 0.0))

    if hw_dst != end:
        c = _run(hw_dst, end, highway_only=False)
        if c is None: return None
        if seq and seq[-1] == c.nodes[0]:
            seq.extend(c.nodes[1:])
        else:
            seq.extend(c.nodes)
        legs.append(("local_out", c.distance_m / 1000))
    else:
        legs.append(("local_out", 0.0))

    dist_m, time_s = measure_path(g, seq)
    out = _route_tuple(dist_m, time_s, seq, [], pops_total)
    out.legs = legs  # type: ignore[attr-defined]
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--zim", required=True)
    p.add_argument("--src", required=True, type=parse_lat_lon, metavar="LAT,LON")
    p.add_argument("--dst", required=True, type=parse_lat_lon, metavar="LAT,LON")
    p.add_argument("--mode", default="all",
                   choices=["astar", "hwy2", "all"],
                   help="Which strategy to time. 'all' runs both. "
                        "'hwy2' is the proper two-pass: local-first-mile + "
                        "highway-tier middle + local-last-mile, with full "
                        "graph at the endpoints so no segments are missing.")
    p.add_argument("--max-pops", type=int, default=None,
                   help="Bail if A* visits more than N nodes. Default: unlimited.")
    args = p.parse_args()

    print(f"Loading {args.zim}...")
    t0 = time.time()
    g = load_spatial_from_zim(args.zim)
    print(f"  loaded in {time.time() - t0:.1f}s "
          f"({g.num_nodes:,} nodes, {g._index.num_cells} cells)")

    print("Resolving endpoints...")
    src_lat, src_lon = args.src
    dst_lat, dst_lon = args.dst
    src_node, src_d = nearest_node(g, src_lat, src_lon)
    dst_node, dst_d = nearest_node(g, dst_lat, dst_lon)
    print(f"  src node #{src_node}: {src_d:.0f} m from ({src_lat},{src_lon})")
    print(f"  dst node #{dst_node}: {dst_d:.0f} m from ({dst_lat},{dst_lon})")
    crow = haversine_m(src_lat, src_lon, dst_lat, dst_lon)
    print(f"  crow-fly: {crow / 1000:.0f} km")

    modes = ["astar", "hwy2"] if args.mode == "all" else [args.mode]
    results = []
    for mode in modes:
        # Re-load graph so cell-cache stats are fair across modes.
        g = load_spatial_from_zim(args.zim)

        print(f"\n=== mode: {mode} ===")
        t0 = time.time()
        if mode == "astar":
            r = find_route_spatial(g, src_node, dst_node, max_pops=args.max_pops)
        else:  # hwy2
            r = find_route_two_pass(
                g, src_node, dst_node,
                src_lat, src_lon, dst_lat, dst_lon,
                max_pops=args.max_pops,
            )
        elapsed = time.time() - t0
        cells_loaded = g._cache_peak
        cells_accessed = len(g._cells_ever_accessed)
        if r is None:
            print(f"  no route (after {elapsed:.1f}s)")
            results.append((mode, elapsed, None, cells_loaded, cells_accessed))
            continue
        if mode == "astar":
            d_km = r.total_dist_m / 1000
            t_h = r.total_time_s / 3600
            pops = "?"  # find_route_spatial doesn't expose pops
            nodes_n = len(r.node_sequence)
            legs = None
        else:
            d_km = r.distance_m / 1000
            t_h = r.time_s / 3600
            pops = r.pops
            nodes_n = len(r.nodes)
            legs = getattr(r, "legs", None)
        print(f"  route OK in {elapsed:.1f}s")
        print(f"    distance: {d_km:.1f} km")
        print(f"    time: {t_h:.1f} h ({t_h * 60:.0f} min)")
        print(f"    nodes: {nodes_n:,}  pops: {pops}")
        if legs:
            print(f"    legs: " + ", ".join(f"{label}={km:.1f}km" for label, km in legs))
        print(f"    cells loaded: {cells_loaded}  accessed-ever: {cells_accessed}")
        results.append((mode, elapsed, d_km, cells_loaded, cells_accessed))

    if len(results) > 1:
        print("\n=== summary ===")
        print(f"{'mode':10s}  {'time':>9s}  {'dist':>8s}  {'cells':>6s}")
        for m, t, d, cl, ca in results:
            d_str = f"{d:.1f}km" if d is not None else "n/a"
            print(f"{m:10s}  {t:>7.1f}s   {d_str:>8s}  {cl:>6d}")


if __name__ == "__main__":
    main()
