"""Pure-Python A* that mirrors the viewer's findRoute() closely enough that
two graphs with bit-identical node/edge arrays always produce identical
routes.

Matches resources/viewer/index.html:
  - haversine distance (R = 6_371_000 m)
  - heuristic time = haversine / (100 / 3.6)   (speed = 100 km/h)
  - edge cost time  = dist_m / (speed / 3.6)
  - cost field ``g`` is a float64 (Float64Array in the viewer)

Tie-breaking differs from JS (Python heapq is stable-by-insertion, JS binary
heap isn't). The differential tests don't rely on JS parity — they verify
that two Python runs over v4 and v5 (or the same version round-tripped
through our serializer) return identical routes.

Hot-loop design:
  The inner loop is hit once per explored edge (10s-100s of millions of
  times for a continent-scale route). Rather than go through SZRG's method
  accessors (each is a Python function call + numpy item access), we pull
  raw Python lists of nodes/edges once up-front and hand-inline the field
  decoding. A 10× speedup versus the method-call version is typical.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from tests.szrg_reader import SZRG


R_EARTH = 6_371_000.0
HEURISTIC_SPEED_KPH = 100.0
HEURISTIC_SPEED_MPS = HEURISTIC_SPEED_KPH / 3.6
M_PER_DEG_LAT = math.pi * R_EARTH / 180.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R_EARTH * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@dataclass
class Route:
    start_node: int
    end_node: int
    total_dist_m: float
    total_time_s: float
    node_sequence: list          # [n0, n1, ..., nK] graph-node indices (start→end)
    edge_sequence: list          # [e0, e1, ..., e(K-1)] absolute edge indices
    road_sequence: list          # [(name_idx, flags, dist_m), ...] coalesced
    geom_sequence: list          # [geom_idx or -1, ...] one per edge

    def fingerprint(self) -> dict:
        """Short, JSON-friendly digest suitable for golden-corpus comparison."""
        return {
            "s": self.start_node,
            "e": self.end_node,
            "d": round(self.total_dist_m, 3),
            "t": round(self.total_time_s, 3),
            "n": self.node_sequence,
            "g": self.geom_sequence,
            "rd": [(int(ni), int(fl), round(dm, 3))
                   for (ni, fl, dm) in self.road_sequence],
        }


def find_route(g: SZRG, start: int, end: int, *, max_pops: int | None = None) -> Route | None:
    if start == end:
        return Route(start, end, 0.0, 0.0, [start], [], [], [])

    # Pull out raw list views of the hot columns. Converting numpy -> Python
    # list once is way faster than per-element numpy indexing inside the
    # inner loop (~10× speedup in microbench).
    nodes_scaled = g.nodes_scaled.tolist()           # [lat_e7, lon_e7, ...]
    adj_offsets = g.adj_offsets.tolist()             # [offset_0, ..., offset_n]
    edges_flat = g.edges.tolist()                    # uint32 flattened
    stride = g.edge_stride
    version = g.version

    num_nodes = g.num_nodes
    end_lat = nodes_scaled[end * 2] / 1e7
    end_lon = nodes_scaled[end * 2 + 1] / 1e7

    INF = math.inf
    gscore = [INF] * num_nodes
    gscore[start] = 0.0
    prev = [-1] * num_nodes
    prev_edge = [-1] * num_nodes
    closed = bytearray(num_nodes)

    start_lat = nodes_scaled[start * 2] / 1e7
    start_lon = nodes_scaled[start * 2 + 1] / 1e7
    h0 = haversine_m(start_lat, start_lon, end_lat, end_lon) / HEURISTIC_SPEED_MPS

    counter = 0
    heap: list = []
    heappush = heapq.heappush
    heappop = heapq.heappop
    heappush(heap, (h0, counter, start))
    pops = 0

    # Local aliases — every dotted lookup inside a hot loop costs a frame op.
    _sin = math.sin
    _cos = math.cos
    _atan2 = math.atan2
    _sqrt = math.sqrt
    _radians = math.radians
    _heuristic_speed_mps = HEURISTIC_SPEED_MPS
    _r_earth_2 = R_EARTH * 2.0
    _end_lat_rad = math.radians(end_lat)
    _cos_end_lat = math.cos(_end_lat_rad)

    while heap:
        _, _, current = heappop(heap)
        pops += 1
        if max_pops is not None and pops > max_pops:
            return None
        if current == end:
            break
        if closed[current]:
            continue
        closed[current] = 1

        current_g = gscore[current]
        e_start = adj_offsets[current]
        e_end = adj_offsets[current + 1]

        base = e_start * stride
        for i in range(e_start, e_end):
            target = edges_flat[base]
            if closed[target]:
                base += stride
                continue
            # version-aware field unpack
            if version == 2:
                dist_m = edges_flat[base + 1] / 10.0
                speed_geom = edges_flat[base + 2]
                speed = speed_geom >> 24
            else:
                dist_speed = edges_flat[base + 1]
                dist_m = (dist_speed & 0xFFFFFF) / 10.0
                speed = dist_speed >> 24
            base += stride
            if speed == 0:
                continue
            cost = dist_m / (speed / 3.6)
            new_g = current_g + cost
            if new_g < gscore[target]:
                gscore[target] = new_g
                prev[target] = current
                prev_edge[target] = i
                t_lat = nodes_scaled[target * 2] / 1e7
                t_lon = nodes_scaled[target * 2 + 1] / 1e7
                # inline haversine — keeps math.* alias use in this scope
                dlat = _radians(end_lat - t_lat)
                dlon = _radians(end_lon - t_lon)
                shd = _sin(dlat * 0.5)
                shdlon = _sin(dlon * 0.5)
                a = shd * shd + _cos(_radians(t_lat)) * _cos_end_lat * shdlon * shdlon
                h_m = _r_earth_2 * _atan2(_sqrt(a), _sqrt(1 - a))
                h = h_m / _heuristic_speed_mps
                counter += 1
                heappush(heap, (new_g + h, counter, target))

    if gscore[end] == INF:
        return None

    # Reconstruct — build node + edge sequence start→end. geom_sequence
    # uses the geom_idx column from the edge record, which is present
    # regardless of whether the SZGM companion has been attached.
    no_geom = g.no_geom
    node_seq_rev = [end]
    edge_seq_rev = []
    geom_seq_rev = []
    n = end
    total_dist = 0.0
    while n != start:
        ei = prev_edge[n]
        if ei < 0:
            return None
        edge_seq_rev.append(ei)
        base = ei * stride
        if version == 2:
            dist_m = edges_flat[base + 1] / 10.0
            gi = edges_flat[base + 2] & 0xFFFFFF
        else:
            dist_m = (edges_flat[base + 1] & 0xFFFFFF) / 10.0
            gi = edges_flat[base + 2]
        geom_seq_rev.append(-1 if gi == no_geom else gi)
        total_dist += dist_m
        n = prev[n]
        node_seq_rev.append(n)

    node_seq = list(reversed(node_seq_rev))
    edge_seq = list(reversed(edge_seq_rev))
    geom_seq = list(reversed(geom_seq_rev))

    # Road coalescing (collapses consecutive same-name + same-maneuver segments).
    # class_access (bit 8 = roundabout, bits 0..4 = class) lives in edge column 4
    # on v4 *and* v5 — same stride=5 layout. Older v2/v3 have no such column.
    roads: list = []
    for ei in edge_seq:
        base = ei * stride
        if version == 2:
            dist_m = edges_flat[base + 1] / 10.0
        else:
            dist_m = (edges_flat[base + 1] & 0xFFFFFF) / 10.0
        name_idx = edges_flat[base + 3]
        if version in (4, 5):
            ca = edges_flat[base + 4]
            is_round = (ca >> 8) & 1
            cls = ca & 0x1F
            is_link = 1 if cls in (2, 4, 6, 8, 10) else 0
        else:
            is_round = 0
            is_link = 0
        flags = is_round | (is_link << 1)
        if roads and roads[-1][0] == name_idx and roads[-1][1] == flags:
            roads[-1] = (name_idx, flags, roads[-1][2] + dist_m)
        else:
            roads.append((name_idx, flags, dist_m))

    return Route(
        start_node=start,
        end_node=end,
        total_dist_m=total_dist,
        total_time_s=gscore[end],
        node_sequence=node_seq,
        edge_sequence=edge_seq,
        road_sequence=roads,
        geom_sequence=geom_seq,
    )
