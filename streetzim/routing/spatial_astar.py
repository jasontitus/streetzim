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
    turn_restrictions: bool = True,
    uturn_penalty_s: float | None = None,
) -> SpatialRoute | None:
    """Fastest route for ``travel_mode`` (streetzim.routing.modes).

    Walking and cycling minimise a cost that penalises busy roads, so
    ``total_time_s`` is the plain travel time summed along the chosen
    path, not the search cost. Driving is unchanged: cost is time.

    Turn restrictions (streetzim.routing.restrictions; cars and bikes):
    a search state is a node, or a *virtual* state — a node plus the
    restriction paths the route is part-way along. At a path's last via
    node a NO record bans its last step and an ONLY record allows only
    that step (when the mode may use it at all). A closed plain state at
    a node dominates any later virtual state there (its moves are a
    superset, its g no larger: same heuristic). Turning straight back
    (u -> v -> u) costs U_TURN_PENALTY_S unless v has no other way on;
    the penalty steers the search and is not counted in the time.
    routing-worker.js implements the same rules.
    """
    from streetzim.routing.restrictions import U_TURN_PENALTY_S, ONLY, mode_bit

    if travel_mode not in MODES:
        raise ValueError(f"unknown travel mode {travel_mode!r}")
    drive = travel_mode == "drive"
    heur_mps = HEURISTIC_SPEED_MPS if drive else HEURISTIC_KPH[travel_mode] / 3.6
    mbit = mode_bit(travel_mode) if turn_restrictions else 0
    uturn = U_TURN_PENALTY_S[travel_mode] if turn_restrictions else 0.0
    if uturn_penalty_s is not None and turn_restrictions:
        uturn = uturn_penalty_s           # tests
    if start == end:
        return SpatialRoute(start, end, 0.0, 0.0, [start], [])

    num_nodes = g.num_nodes
    end_lat_e7, end_lon_e7 = g.node_coords_e7(end)
    end_lat = end_lat_e7 / 1e7
    end_lon = end_lon_e7 / 1e7

    INF = math.inf
    # Plain states are node ids (arrays); virtual states are ids from
    # num_nodes up (dicts), see vnode / vmatch.
    gscore = [INF] * num_nodes
    gscore[start] = 0.0
    prev = [-1] * num_nodes
    prev_edge: list = [None] * num_nodes
    closed = bytearray(num_nodes)
    vg: dict = {}
    vprev: dict = {}
    vprev_edge: dict = {}
    vclosed: set = set()
    vnode: list = []                  # virtual id - num_nodes -> node
    vmatch: list = []                 # -> ((cell, record, pos), ...)
    vkey: dict = {}

    # Second arrivals ("alt" states, ids -(node + 2), kept in the v* dicts):
    # the U-turn penalty depends on where a node was reached from, so a
    # node keeps, beside its best arrival, its best arrival from another
    # neighbour while that is within one penalty of the best. Expanded, an
    # alt state only goes back toward the best arrival's neighbour (the one
    # move the best would pay the penalty for; every other move is cheaper
    # from the best). With both, the penalty is exact for plain states.
    def node_of(sid):
        if sid < 0:
            return -sid - 2
        return sid if sid < num_nodes else vnode[sid - num_nodes]

    def g_of(sid):
        return gscore[sid] if 0 <= sid < num_nodes else vg.get(sid, INF)

    def prev_of(sid):
        return prev[sid] if 0 <= sid < num_nodes else vprev.get(sid, -1)

    def main_back(node):
        return node_of(prev[node]) if prev[node] >= 0 else -1

    # A second arrival can only matter after a restriction forced a
    # detour: without one nearby, a shortest path never doubles back. So
    # one is offered only when the best arrival's chain passes a virtual
    # state within TAINT_HOPS steps (else +60 % pops on D.C. for nothing).
    TAINT_HOPS = 8

    def tainted(sid):
        for _ in range(TAINT_HOPS):
            if sid == -1:
                return False
            if sid >= num_nodes:
                return True              # a virtual state
            sid = vprev.get(sid, -1) if sid < 0 else prev[sid]
        return False

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

    def cell_turns(node):
        """(cell id, cell) of `node` when its cell holds restrictions."""
        cid = g._index.cell_for_node(node)
        if cid is None:
            return None, None
        cell = g._ensure_cell(cid)
        return (cid, cell) if cell.turns else (None, None)

    def _h_of(node):
        t_lat_e7, t_lon_e7 = g.node_coords_e7(node)
        return haversine_m(t_lat_e7 / 1e7, t_lon_e7 / 1e7, end_lat, end_lon) / heur_mps

    pops = 0
    goal = -1
    while heap:
        _, _, cur_sid = heappop(heap)
        pops += 1
        if max_pops is not None and pops > max_pops:
            return None
        current = node_of(cur_sid)
        if current == end:
            goal = cur_sid
            break
        alt_to = -1
        if cur_sid < 0:
            if cur_sid in vclosed:
                continue
            alt_to = main_back(current)
            if alt_to < 0 or node_of(vprev[cur_sid]) == alt_to:
                continue     # no longer a different neighbour: nothing to add
            vclosed.add(cur_sid)
            match = ()
        elif cur_sid < num_nodes:
            if closed[cur_sid]:
                continue
            closed[cur_sid] = 1
            match = ()
        else:
            if cur_sid in vclosed or closed[current]:
                continue
            vclosed.add(cur_sid)
            match = vmatch[cur_sid - num_nodes]

        current_g = g_of(cur_sid)
        edges = g.edges_of_node(current)

        # Restrictions in force at this node.
        bans = only = None
        cont: dict = {}
        if match:
            for (cid, ri, pos) in match:
                flags, path = g._ensure_cell(cid).turns[ri]
                if pos == len(path) - 2:
                    if flags & ONLY:
                        only = {path[-1]} if only is None else only & {path[-1]}
                    else:
                        bans = {path[-1]} if bans is None else bans | {path[-1]}
                else:
                    cont.setdefault(path[pos + 1], []).append((cid, ri, pos + 1))
        usable = []
        for (target, speed_dist, geom_local, name_idx, class_access) in edges:
            if drive:
                if is_no_motor(class_access) or (speed_dist >> 24) == 0:
                    continue
                dist_m = (speed_dist & 0xFFFFFF) / 10.0
                cost = dist_m / ((speed_dist >> 24) / 3.6)
                time_s = cost
            else:
                ct = edge_cost(travel_mode, speed_dist, class_access)
                if ct is None:
                    continue
                cost, time_s = ct
                dist_m = (speed_dist & 0xFFFFFF) / 10.0
            usable.append((target, cost, time_s, dist_m, geom_local, name_idx, class_access))
        if only is not None and not any(u[0] in only for u in usable):
            only = None          # the allowed turn is closed to this mode
        back = prev_of(cur_sid)
        back = node_of(back) if back >= 0 else -1
        other_exit = uturn and any(u[0] != back for u in usable)

        for (target, cost, time_s, dist_m, geom_local, name_idx, class_access) in usable:
            if alt_to >= 0 and target != alt_to:
                continue
            if bans is not None and target in bans:
                continue
            if only is not None and target not in only:
                continue
            penalty = uturn if (target == back and other_exit) else 0.0
            # The state reached: plain, or virtual if a restriction path
            # continues or starts on this step.
            m2 = cont.get(target, [])
            if mbit:
                tcid, tcell = cell_turns(target)
                if tcell is not None:
                    roots = tcell.turn_roots().get((current, target))
                    if roots:
                        m2 = m2 + [(tcid, ri, 1) for ri in roots
                                   if tcell.turns[ri][0] & mbit]
            if m2:
                if closed[target]:
                    continue     # dominated by the closed plain state
                key = (target, tuple(sorted(m2)))
                t_sid = vkey.get(key)
                if t_sid is None:
                    t_sid = num_nodes + len(vnode)
                    vkey[key] = t_sid
                    vnode.append(target)
                    vmatch.append(key[1])
                if t_sid in vclosed:
                    continue
            else:
                t_sid = target
                new_g = current_g + cost + penalty
                rec = (dist_m, geom_local, name_idx, class_access, time_s, penalty)
                if uturn and (tainted(prev[target]) or tainted(cur_sid)):
                    mb = main_back(target)
                    if not closed[target] and new_g < gscore[target]:
                        # The arrival being replaced may become the alt.
                        if gscore[target] < INF and mb != current:
                            alt_sid = -(target + 2)
                            if alt_sid not in vclosed:
                                vg[alt_sid] = gscore[target]
                                vprev[alt_sid] = prev[target]
                                vprev_edge[alt_sid] = prev_edge[target]
                                counter += 1
                                heappush(heap, (gscore[target] + (_h_of(target)), counter, alt_sid))
                    elif current != mb and new_g < gscore[target] + uturn:
                        alt_sid = -(target + 2)
                        old = vprev.get(alt_sid, -1)
                        if alt_sid not in vclosed and (
                                new_g < vg.get(alt_sid, INF)
                                or (old >= 0 and node_of(old) == mb)):
                            vg[alt_sid] = new_g
                            vprev[alt_sid] = cur_sid
                            vprev_edge[alt_sid] = rec
                            counter += 1
                            heappush(heap, (new_g + _h_of(target), counter, alt_sid))
                        continue
                if closed[target]:
                    continue
            new_g = current_g + cost + penalty
            if new_g < g_of(t_sid):
                rec = (dist_m, geom_local, name_idx, class_access, time_s, penalty)
                if 0 <= t_sid < num_nodes:
                    gscore[t_sid] = new_g
                    prev[t_sid] = cur_sid
                    prev_edge[t_sid] = rec
                else:
                    vg[t_sid] = new_g
                    vprev[t_sid] = cur_sid
                    vprev_edge[t_sid] = rec
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
                heappush(heap, (new_g + h, counter, t_sid))

    if goal < 0:
        return None

    # Reconstruct node sequence + accumulate dist.
    node_rev = [end]
    edge_rev: list = []
    sid = goal
    total_dist = 0.0
    while sid != start:
        pe = prev_edge[sid] if 0 <= sid < num_nodes else vprev_edge.get(sid)
        if pe is None:
            return None
        total_dist += pe[0]
        edge_rev.append(pe)
        sid = prev_of(sid)
        node_rev.append(node_of(sid))

    node_seq = list(reversed(node_rev))
    edge_seq = list(reversed(edge_rev))

    # Road coalesce — matches streetzim/routing/astar.find_route
    roads: list = []
    for (dist_m, _geom_local, name_idx, class_access, _t, _p) in edge_seq:
        ca = class_access
        is_round = (ca >> 8) & 1
        cls = ca & 0x1F
        is_link = 1 if cls in (2, 4, 6, 8, 10) else 0
        flags = is_round | (is_link << 1)
        if roads and roads[-1][0] == name_idx and roads[-1][1] == flags:
            roads[-1] = (name_idx, flags, roads[-1][2] + dist_m)
        else:
            roads.append((name_idx, flags, dist_m))

    penalties = sum(pe[5] for pe in edge_seq)
    return SpatialRoute(
        start_node=start,
        end_node=end,
        total_dist_m=total_dist,
        total_time_s=((g_of(goal) - penalties) if drive
                      else math.fsum(pe[4] for pe in edge_seq)),
        node_sequence=node_seq,
        road_sequence=roads,
    )
