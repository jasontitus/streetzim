"""A* over a spatial-chunked graph. Differential-tested byte-for-byte
against the monolithic find_route on the same source graph — the lazy
cell loading must not perturb routing at all.

Implementation parallels streetzim/routing/astar.py, with two changes:
  1. Edges for a node come from SpatialGraph.edges_of_node() (triggers
     lazy cell load).
  2. geom_idx returned by edges_of_node() is already cell-local, so the
     fingerprint geom sequence records it as (cell_id, local_gi) pairs —
     flattened to the same representation as v4's global geom_idx via a
     stable sort so diff_corpora can compare.

The Route fingerprint is semantically identical to streetzim/routing/astar.Route.
The spatial-chunked representation of geom_idx isn't directly comparable
to v4's global indices, so when running the differential test we compare
only the fields that are format-independent: node_sequence, edge_count,
total_dist_m, total_time_s, road_sequence.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from streetzim.routing.spatial import SpatialGraph
from streetzim.routing.astar import R_EARTH, HEURISTIC_SPEED_MPS, haversine_m, is_no_motor
from streetzim.routing.modes import HEURISTIC_KPH, MODES, edge_cost


@dataclass
class SpatialRoute:
    start_node: int
    end_node: int
    total_dist_m: float
    total_time_s: float
    node_sequence: list
    road_sequence: list       # [(name_idx, flags, dist_m), ...]

    def fingerprint(self) -> dict:
        """Shape matches the FORMAT-INDEPENDENT subset of streetzim/routing/astar.Route.
        Omits "g" (geom_sequence) because geom_idx is cell-local in spatial
        graphs and globally-numbered in monolithic graphs — a direct compare
        is meaningless."""
        return {
            "s": self.start_node,
            "e": self.end_node,
            "d": round(self.total_dist_m, 3),
            "t": round(self.total_time_s, 3),
            "n": self.node_sequence,
            "rd": [[int(ni), int(fl), round(dm, 3)]
                   for (ni, fl, dm) in self.road_sequence],
        }


def find_route_spatial(
    g: SpatialGraph,
    start: int,
    end: int,
    *,
    max_pops: int | None = None,
    travel_mode: str = "drive",
) -> SpatialRoute | None:
    """Fastest route for ``travel_mode`` (streetzim.routing.modes).

    Walking and cycling minimise a cost that penalises busy roads, so
    ``total_time_s`` is the plain travel time summed along the chosen
    path, not the search cost. Driving is unchanged: cost is time.
    """
    if travel_mode not in MODES:
        raise ValueError(f"unknown travel mode {travel_mode!r}")
    drive = travel_mode == "drive"
    heur_mps = HEURISTIC_SPEED_MPS if drive else HEURISTIC_KPH[travel_mode] / 3.6
    if start == end:
        return SpatialRoute(start, end, 0.0, 0.0, [start], [])

    num_nodes = g.num_nodes
    end_lat_e7, end_lon_e7 = g.node_coords_e7(end)
    end_lat = end_lat_e7 / 1e7
    end_lon = end_lon_e7 / 1e7

    INF = math.inf
    gscore = [INF] * num_nodes
    gscore[start] = 0.0
    prev = [-1] * num_nodes
    # Per-node predecessor edge (we store the tuple so we can re-read
    # dist/name/class without re-looking up the source cell later).
    prev_edge: list = [None] * num_nodes
    closed = bytearray(num_nodes)

    start_lat_e7, start_lon_e7 = g.node_coords_e7(start)
    start_lat = start_lat_e7 / 1e7
    start_lon = start_lon_e7 / 1e7
    h0 = haversine_m(start_lat, start_lon, end_lat, end_lon) / heur_mps

    heap: list = []
    heappush = heapq.heappush
    heappop = heapq.heappop
    counter = 0
    heappush(heap, (h0, counter, start))

    # Reuse math-module aliases for the inlined haversine.
    _sin = math.sin
    _cos = math.cos
    _atan2 = math.atan2
    _sqrt = math.sqrt
    _radians = math.radians
    _end_lat_rad = math.radians(end_lat)
    _cos_end_lat = math.cos(_end_lat_rad)
    _r_earth_2 = R_EARTH * 2.0

    pops = 0
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
        for (target, speed_dist, geom_local, name_idx, class_access) in g.edges_of_node(current):
            if drive:
                if is_no_motor(class_access):
                    continue  # car profile: footways / steps / private (bit 9 or ordinal 16..20)
                if closed[target]:
                    continue
                dist_m = (speed_dist & 0xFFFFFF) / 10.0
                speed = speed_dist >> 24
                if speed == 0:
                    continue
                cost = dist_m / (speed / 3.6)
                time_s = cost
            else:
                if closed[target]:
                    continue
                ct = edge_cost(travel_mode, speed_dist, class_access)
                if ct is None:
                    continue
                cost, time_s = ct
                dist_m = (speed_dist & 0xFFFFFF) / 10.0
            new_g = current_g + cost
            if new_g < gscore[target]:
                gscore[target] = new_g
                prev[target] = current
                prev_edge[target] = (dist_m, geom_local, name_idx, class_access, time_s)
                t_lat_e7, t_lon_e7 = g.node_coords_e7(target)
                t_lat = t_lat_e7 / 1e7
                t_lon = t_lon_e7 / 1e7
                dlat = _radians(end_lat - t_lat)
                dlon = _radians(end_lon - t_lon)
                shd = _sin(dlat * 0.5)
                shdlon = _sin(dlon * 0.5)
                a = shd * shd + _cos(_radians(t_lat)) * _cos_end_lat * shdlon * shdlon
                h_m = _r_earth_2 * _atan2(_sqrt(a), _sqrt(1 - a))
                h = h_m / heur_mps
                counter += 1
                heappush(heap, (new_g + h, counter, target))

    if gscore[end] == INF:
        return None

    # Reconstruct node sequence + accumulate dist.
    node_rev = [end]
    edge_rev: list = []
    n = end
    total_dist = 0.0
    while n != start:
        pe = prev_edge[n]
        if pe is None:
            return None
        total_dist += pe[0]
        edge_rev.append(pe)
        n = prev[n]
        node_rev.append(n)

    node_seq = list(reversed(node_rev))
    edge_seq = list(reversed(edge_rev))

    # Road coalesce — matches streetzim/routing/astar.find_route
    roads: list = []
    for (dist_m, _geom_local, name_idx, class_access, _t) in edge_seq:
        ca = class_access
        is_round = (ca >> 8) & 1
        cls = ca & 0x1F
        is_link = 1 if cls in (2, 4, 6, 8, 10) else 0
        flags = is_round | (is_link << 1)
        if roads and roads[-1][0] == name_idx and roads[-1][1] == flags:
            roads[-1] = (name_idx, flags, roads[-1][2] + dist_m)
        else:
            roads.append((name_idx, flags, dist_m))

    return SpatialRoute(
        start_node=start,
        end_node=end,
        total_dist_m=total_dist,
        total_time_s=(gscore[end] if drive
                      else math.fsum(pe[4] for pe in edge_seq)),
        node_sequence=node_seq,
        road_sequence=roads,
    )
