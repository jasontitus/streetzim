"""Turn restrictions: OSM relations -> node paths (restrictions.py), the
SZTR trailer in spatial cells, and the router honouring them per mode
(spatial_astar.find_route_spatial)."""
from __future__ import annotations

import pytest

osmium = pytest.importorskip("osmium")

from streetzim.routing import restrictions as tr  # noqa: E402
from streetzim.routing.build import extract_routing_graph  # noqa: E402
from streetzim.routing.reader import load_from_file  # noqa: E402
from streetzim.routing.spatial import build_spatial  # noqa: E402
from streetzim.routing.spatial_astar import find_route_spatial  # noqa: E402
from tests.test_routing_worker_v3 import _spatial_graph_from_dir  # noqa: E402

D = 0.001   # ~110 m
# A plus junction V with arms to S, N, E, W (each a two-way way of its
# own) and a slow ring through the arm ends, so every banned turn has a
# legal (longer) way round.
NODES = {
    "V": (0, 0), "S": (0, -1), "N": (0, 1), "E": (1, 0), "W": (-1, 0),
    "SW": (-2, -2), "NW": (-2, 2), "NE": (2, 2), "SE": (2, -2), "P": (-3, 3),
}
WAYS = {
    "s": ["S", "V"], "n": ["V", "N"], "e": ["V", "E"], "w": ["W", "V"],
    "r1a": ["S", "SW"], "r1b": ["SW", "W"], "r2": ["W", "NW", "N"],
    "r3a": ["N", "NE"], "r3b": ["NE", "E"], "r4a": ["E", "SE"], "r4b": ["SE", "S"],
    "p": ["NW", "P"],          # makes NW a junction inside the two-way r2
}


def _ids():
    nid = {k: i + 1 for i, k in enumerate(NODES)}
    wid = {k: 100 + i for i, k in enumerate(WAYS)}
    return nid, wid


def _graph(tmp_path, monkeypatch, relations, way_tags=None):
    """relations: [(tags, [(type, name, role)])]; returns (spatial graph,
    node index by name, resolved records)."""
    nid, wid = _ids()
    pbf = tmp_path / "x.osm.pbf"
    with osmium.SimpleWriter(str(pbf)) as wr:
        for k, (x, y) in NODES.items():
            wr.add_node(osmium.osm.mutable.Node(
                id=nid[k], location=(7.4 + x * D, 43.7 + y * D), version=1))
        for k, ns in WAYS.items():
            tags = {"highway": "residential" if not k.startswith("r") else "service"}
            tags.update((way_tags or {}).get(k, {}))
            wr.add_way(osmium.osm.mutable.Way(id=wid[k], nodes=[nid[n] for n in ns],
                                              tags=tags, version=1))
        for i, (tags, members) in enumerate(relations):
            ms = [(t, (nid if t == "n" else wid)[name], role) for t, name, role in members]
            wr.add_relation(osmium.osm.mutable.Relation(
                id=1000 + i, members=ms, tags={"type": "restriction", **tags}, version=1))
    monkeypatch.setenv("STREETZIM_NODE_LOC_DIR", str(tmp_path))
    (tmp_path / "g").mkdir()
    path = extract_routing_graph(str(pbf), str(tmp_path / "g"))
    g = load_from_file(path)
    records = tr.read_sidecar(path)
    by_coord = {(int(g.nodes_scaled[2 * i]), int(g.nodes_scaled[2 * i + 1])): i
                for i in range(g.num_nodes)}
    idx = {k: by_coord[(round((43.7 + y * D) * 1e7), round((7.4 + x * D) * 1e7))]
           for k, (x, y) in NODES.items()}
    build_spatial(g, cell_scale=10, output_dir=tmp_path / "cells", restrictions=records)
    sg = _spatial_graph_from_dir(tmp_path / "cells")
    # Spatial ids differ from SZRG ids: map by coordinates again.
    sidx = {}
    for k, (x, y) in NODES.items():
        want = (round((43.7 + y * D) * 1e7), round((7.4 + x * D) * 1e7))
        sidx[k] = next(n for n in range(sg.num_nodes) if sg.node_coords_e7(n) == want)
    return sg, sidx, idx, records


def _names(sidx, seq):
    inv = {v: k for k, v in sidx.items()}
    return [inv.get(n, "?") for n in seq]


def _route(sg, sidx, a, b, mode="drive", **kw):
    r = find_route_spatial(sg, sidx[a], sidx[b], travel_mode=mode, **kw)
    return None if r is None else _names(sidx, r.node_sequence)


NO_LEFT = ({"restriction": "no_left_turn"}, [("w", "s", "from"), ("n", "V", "via"),
                                               ("w", "w", "to")])


def test_no_left_turn_resolves_to_a_node_path(tmp_path, monkeypatch):
    sg, sidx, idx, recs = _graph(tmp_path, monkeypatch, [NO_LEFT])
    assert recs == [(tr.CAR | tr.BIKE, [idx["S"], idx["V"], idx["W"]])]


def test_cars_and_bikes_go_round_a_banned_turn_walkers_do_not(tmp_path, monkeypatch):
    sg, sidx, _, _ = _graph(tmp_path, monkeypatch, [NO_LEFT])
    assert _route(sg, sidx, "S", "W", turn_restrictions=False) == ["S", "V", "W"]
    for mode in ("drive", "bike"):
        r = _route(sg, sidx, "S", "W", mode)
        assert r != ["S", "V", "W"] and r[0] == "S" and r[-1] == "W", (mode, r)
        assert "SW" in r or "NW" in r, r              # round the ring
    assert _route(sg, sidx, "S", "W", "walk") == ["S", "V", "W"]
    # Other turns at V are untouched.
    assert _route(sg, sidx, "S", "E") == ["S", "V", "E"]
    assert _route(sg, sidx, "N", "W") == ["N", "V", "W"]


def test_no_u_turn_detour_rather_than_turning_back(tmp_path, monkeypatch):
    """Banned left S->V->W: going V->N and straight back N->V->W is a
    U-turn at N; the penalty makes the ring the better way."""
    sg, sidx, _, _ = _graph(tmp_path, monkeypatch, [NO_LEFT])
    r = _route(sg, sidx, "S", "W")
    assert all(a != c for a, c in zip(r, r[2:])), r


def test_only_straight_on(tmp_path, monkeypatch):
    only = ({"restriction": "only_straight_on"},
            [("w", "s", "from"), ("n", "V", "via"), ("w", "n", "to")])
    sg, sidx, idx, recs = _graph(tmp_path, monkeypatch, [only])
    assert recs == [(tr.CAR | tr.ONLY, [idx["S"], idx["V"], idx["N"]])]  # not bikes
    r = _route(sg, sidx, "S", "E")
    assert r[:3] != ["S", "V", "E"], r
    assert _route(sg, sidx, "S", "N") == ["S", "V", "N"]
    assert _route(sg, sidx, "S", "E", "bike") == ["S", "V", "E"]


def test_except_bicycle_and_mode_specific_tags(tmp_path, monkeypatch):
    rels = [({"restriction": "no_left_turn", "except": "bicycle;psv"}, NO_LEFT[1]),
            ({"restriction:bicycle": "no_right_turn"},
             [("w", "s", "from"), ("n", "V", "via"), ("w", "e", "to")])]
    sg, sidx, _, recs = _graph(tmp_path, monkeypatch, rels)
    assert sorted(f for f, _ in recs) == [tr.CAR, tr.BIKE]
    assert _route(sg, sidx, "S", "W", "bike") == ["S", "V", "W"]
    assert _route(sg, sidx, "S", "W") != ["S", "V", "W"]
    assert _route(sg, sidx, "S", "E") == ["S", "V", "E"]
    assert _route(sg, sidx, "S", "E", "bike") != ["S", "V", "E"]


@pytest.mark.parametrize("tags", [
    {"restriction:conditional": "no_left_turn @ (Mo-Fr 07:00-09:00)"},
    {"restriction": "no_right_turn_on_red"},
    {"restriction:hgv": "no_left_turn"},
])
def test_left_out(tmp_path, monkeypatch, tags):
    sg, sidx, _, recs = _graph(tmp_path, monkeypatch, [(tags, NO_LEFT[1])])
    assert recs == []
    assert _route(sg, sidx, "S", "W") == ["S", "V", "W"]


def test_via_way(tmp_path, monkeypatch):
    """From r1b (SW -> W, one-way) via the way w (W -> V) to s: the
    junction path SW, W, V, S."""
    rel = ({"restriction": "no_left_turn"},
           [("w", "r1b", "from"), ("w", "w", "via"), ("w", "s", "to")])
    sg, sidx, idx, recs = _graph(tmp_path, monkeypatch, [rel],
                                 way_tags={"r1b": {"oneway": "yes"},
                                           "r1a": {"access": "no"}})
    assert recs == [(tr.CAR | tr.BIKE, [idx["SW"], idx["W"], idx["V"], idx["S"]])]
    # SW -> W -> V -> S is banned; SW -> W -> V -> N is fine.
    r = _route(sg, sidx, "SW", "S")
    assert r[:4] != ["SW", "W", "V", "S"], r
    assert _route(sg, sidx, "SW", "N")[:4] == ["SW", "W", "V", "N"]
    # Arriving at W from NW (not the from-way) is not restricted.
    for a in ("NW", "W", "V"):
        assert _route(sg, sidx, a, "S") == _route(sg, sidx, a, "S", turn_restrictions=False)


def test_two_way_pass_through_is_left_out(tmp_path, monkeypatch):
    """A from-way that runs on through the via node, two-way: which side
    the turn is from is ambiguous — dropped and counted."""
    rel = ({"restriction": "no_left_turn"},
           [("w", "r2", "from"), ("n", "NW", "via"), ("w", "p", "to")])
    sg, sidx, _, recs = _graph(tmp_path, monkeypatch, [rel])
    assert recs == []


def test_pack_round_trip():
    recs = [(tr.CAR, [1, 2, 3]), (tr.CAR | tr.BIKE | tr.ONLY, [4, 5, 6, 7])]
    assert tr.unpack(tr.pack(recs)) == recs
    assert tr.pack([]) == b"" and tr.unpack(b"") == []
    assert tr.unpack(bytes(8) + tr.pack(recs), 8) == recs


def test_cells_without_restrictions_are_byte_identical(tmp_path, monkeypatch):
    sg, sidx, idx, recs = _graph(tmp_path, monkeypatch, [])
    a = sorted(p.read_bytes() for p in (tmp_path / "cells").glob("graph-cell-*.bin"))
    from streetzim.routing.reader import load_from_file as lf
    g = lf(str(tmp_path / "g" / "routing-graph.bin"))
    build_spatial(g, cell_scale=10, output_dir=tmp_path / "c2")
    b = sorted(p.read_bytes() for p in (tmp_path / "c2").glob("graph-cell-*.bin"))
    assert a == b


# ---- optimality against a brute-force oracle -----------------------------

def _oracle(adj, recs, s, t, mbit, uturn):
    """Dijkstra over (last K nodes): every restriction is checked by
    matching the history suffix, so no partial-match bookkeeping."""
    import heapq
    K = max([len(p) for _f, p in recs] + [2]) - 1
    best = {}
    heap = [(0.0, (s,))]
    while heap:
        c, hist = heapq.heappop(heap)
        if hist in best:
            continue
        best[hist] = c
        v = hist[-1]
        if v == t:
            return c
        outs = adj.get(v, {})
        back = hist[-2] if len(hist) > 1 else None
        for w, cost in outs.items():
            seq = hist + (w,)
            banned = False
            for f, p in recs:
                if not f & mbit:
                    continue
                n = len(p)
                if f & 1:   # ONLY: arriving with p[:-1] you must take p[-1]
                    if tuple(seq[-n:-1]) == tuple(p[:-1]) and len(seq) >= n \
                            and w != p[-1] and p[-1] in outs:
                        banned = True
                elif len(seq) >= n and tuple(seq[-n:]) == tuple(p):
                    banned = True
            if banned:
                continue
            pen = uturn if (w == back and any(x != back for x in outs)) else 0.0
            heapq.heappush(heap, (c + cost + pen, seq[-K:]))
    return None


@pytest.mark.parametrize("seed", range(6))
def test_optimal_against_the_oracle(tmp_path, seed):
    import random

    from tests.szrg_reader import parse_szrg_bytes
    from tests.test_routing_worker_v3 import _pack_v4_graph_cls

    rng = random.Random(seed)
    W = H = 7
    nodes = [(400_000_000 + y * 9_000, -1_050_000_000 + x * 12_000)
             for y in range(H) for x in range(W)]
    edges, adj = [], {}
    for y in range(H):
        for x in range(W):
            a = y * W + x
            for b in ((a + 1) if x + 1 < W else None, (a + W) if y + 1 < H else None):
                if b is None or rng.random() < 0.1:
                    continue
                d = 1000 + rng.randrange(400)
                for (u, v) in ((a, b), (b, a)):
                    sp = rng.choice((30, 40, 50))
                    edges.append((u, v, d, sp, 0xFFFFFFFF, 0, 11))
                    adj.setdefault(u, {})[v] = (d / 10) / (sp / 3.6)
    recs = []
    for _ in range(25):
        u = rng.randrange(W * H)
        path = [u]
        for _k in range(rng.choice((2, 2, 3))):
            nxt = [w for w in adj.get(path[-1], {}) if w not in path[1:]]
            if not nxt:
                break
            path.append(rng.choice(nxt))
        if len(path) >= 3 and len(set(path[1:-1])) == len(path) - 2:
            recs.append((tr.CAR | (tr.ONLY if rng.random() < 0.25 else 0), path))
    g = parse_szrg_bytes(_pack_v4_graph_cls(nodes, edges))
    build_spatial(g, cell_scale=10, output_dir=tmp_path / "c", restrictions=recs)
    sg = _spatial_graph_from_dir(tmp_path / "c")
    # Spatial renumbering: map by coordinates.
    to_s = {}
    for n in range(sg.num_nodes):
        to_s[sg.node_coords_e7(n)] = n
    sid = [to_s[c] for c in nodes]
    worse = changed = 0
    for _ in range(25):
        a, b = rng.randrange(W * H), rng.randrange(W * H)
        want = _oracle(adj, recs, a, b, tr.CAR, 45.0)
        r = find_route_spatial(sg, sid[a], sid[b], uturn_penalty_s=45.0)
        if want is None:
            assert r is None
            continue
        assert r is not None, (a, b)
        # Reported time leaves the U-turn penalty out; compare costs by
        # re-adding it from the node sequence.
        seq = r.node_sequence
        got = r.total_time_s + 45.0 * sum(
            1 for i in range(2, len(seq)) if seq[i] == seq[i - 2])
        assert got >= want - 1e-6, (a, b, got, want)      # never beats the optimum
        inv = {v: k for k, v in enumerate(sid)}
        orig = [inv[n] for n in seq]
        for f, p in recs:
            n = len(p)
            for i in range(len(orig) - n + 1):
                if f & tr.ONLY:
                    assert not (orig[i:i + n - 1] == p[:-1] and orig[i + n - 1] != p[-1]
                                and p[-1] in adj.get(p[-2], {})), (orig, p)
                else:
                    assert orig[i:i + n] != p, (orig, p)
        free = find_route_spatial(sg, sid[a], sid[b], turn_restrictions=False)
        changed += free is not None and free.node_sequence != seq
        if got > want + 1e-6:
            worse += 1
    # The node-keyed predecessor makes the U-turn penalty approximate;
    # allow a rare suboptimal route, never an illegal one (checked above
    # by the oracle bound) — and say how often.
    assert worse == 0, worse
    assert changed >= 1, "no query exercised a restriction"


@pytest.mark.parametrize("seed", range(3))
def test_worker_matches_python_with_restrictions(tmp_path, seed):
    """routing-worker.js and find_route_spatial agree on random grids with
    random restrictions: same route time, the same legal path."""
    import random
    import shutil

    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    from tests.szrg_reader import parse_szrg_bytes
    from tests.test_routing_worker_modes import _run
    from tests.test_routing_worker_v3 import _pack_v4_graph_cls

    rng = random.Random(100 + seed)
    W = H = 7
    nodes = [(400_000_000 + y * 9_000, -1_050_000_000 + x * 12_000)
             for y in range(H) for x in range(W)]
    edges, adj = [], {}
    for y in range(H):
        for x in range(W):
            a = y * W + x
            for b in ((a + 1) if x + 1 < W else None, (a + W) if y + 1 < H else None):
                if b is None or rng.random() < 0.1:
                    continue
                d = 1000 + rng.randrange(400)
                for (u, v) in ((a, b), (b, a)):
                    edges.append((u, v, d, rng.choice((30, 40, 50)), 0xFFFFFFFF, 0, 11))
                    adj.setdefault(u, set()).add(v)
    recs = []
    for _ in range(25):
        path = [rng.randrange(W * H)]
        for _k in range(rng.choice((2, 2, 3))):
            nxt = [w for w in adj.get(path[-1], ()) if w not in path[1:]]
            if not nxt:
                break
            path.append(rng.choice(nxt))
        if len(path) >= 3 and len(set(path[1:-1])) == len(path) - 2:
            recs.append(((tr.CAR | tr.BIKE) | (tr.ONLY if rng.random() < 0.25 else 0), path))
    g = parse_szrg_bytes(_pack_v4_graph_cls(nodes, edges))
    # Cells of 0.001 degrees: a restriction's nodes and edges span cells,
    # so the worker's lookup in the next node's cell is exercised.
    build_spatial(g, cell_scale=1000, output_dir=tmp_path / "routing-data", restrictions=recs)
    sg = _spatial_graph_from_dir(tmp_path / "routing-data")
    assert sg._index.num_cells > 20
    pairs = []
    for _ in range(20):
        a, b = nodes[rng.randrange(W * H)], nodes[rng.randrange(W * H)]
        pairs.append([a[0] / 1e7, a[1] / 1e7, b[0] / 1e7, b[1] / 1e7])
    for travel in ("drive", "bike"):
        res = _run(tmp_path, pairs, [{"travel": travel}] * len(pairs))
        differs = 0
        for r in res:
            assert r["ok"], r
            ref = find_route_spatial(sg, r["start"], r["end"], travel_mode=travel)
            free = find_route_spatial(sg, r["start"], r["end"], travel_mode=travel,
                                      turn_restrictions=False)
            if ref is None:
                assert r["time"] is None
                continue
            assert r["time"] == pytest.approx(ref.total_time_s, rel=1e-9), (travel, r)
            assert r["distance"] == pytest.approx(ref.total_dist_m, rel=1e-9), (travel, r)
            differs += free.total_dist_m != ref.total_dist_m
        assert differs >= 1, travel


def test_resolver_keeps_kinds_and_modes_apart():
    """A car ban and a bike only_* on one path stay separate records; two
    only_* at one junction drop only the modes they share; one pair of a
    relation that cannot be placed does not drop the others."""
    c = tr.Collector()
    # ways: 1 = a->v (two-way), 2 = v->b, 3 = v->c, 9 = elsewhere
    seq = {1: ([(10, 100), (11, 101)], 0), 2: ([(11, 101), (12, 102)], 0),
           3: ([(11, 101), (13, 103)], 0), 9: ([(20, 200), (21, 201)], 0)}
    c.raw = [({"car": "no", "bike": "only"}, [1], 101, [], [2]),
             ({"car": "only", "bike": None}, [1], 101, [], [3]),
             ({"car": "only", "bike": None}, [1], 101, [], [2]),
             ({"car": "no", "bike": None}, [1, 9], 101, [], [3])]
    recs, dropped = tr.resolve(c, seq)
    got = {(f, tuple(p)) for f, p in recs}
    # car only_ to 12 and to 13 conflict: cars drop both; the bike only_ stays.
    assert (tr.ONLY | tr.BIKE, (10, 11, 12)) in got
    assert not any(f & tr.ONLY and f & tr.CAR for f, _ in got)
    assert (tr.CAR, (10, 11, 12)) in got                    # the car ban, unmerged
    assert (tr.CAR, (10, 11, 13)) in got                    # way 9 unplaceable, 1 kept
    assert dropped["from/to pairs of a placed relation"] == 1
    assert dropped["conflicting only_* at one junction"] == 2


@pytest.mark.parametrize("k", [4, 12, 15])
def test_turning_round_far_from_the_restriction(tmp_path, k):
    """A banned left S -> V -> W, a dead-end street of `k` junctions on from
    V, and a long legal way round: the best route drives to the dead end
    and back (a free U-turn there), not a penalised U-turn mid-street.
    That needs the second arrivals (alt states) along the whole street, in
    both routers (review of c710dc8: a cliff at 8 junctions)."""
    import json
    import shutil
    import subprocess

    from tests.szrg_reader import parse_szrg_bytes
    from tests.test_routing_worker_modes import WORKER, _DRIVER
    from tests.test_routing_worker_v3 import _pack_v4_graph_cls

    nodes = [(399_998_000, -1_050_000_000), (400_000_000, -1_050_000_000),
             (400_000_000, -1_050_002_000)]
    edges, adj = [], {}

    def add2(a, b, d, sp=50):
        for u, v in ((a, b), (b, a)):
            edges.append((u, v, d, sp, 0xFFFFFFFF, 0, 11))
            adj.setdefault(u, {})[v] = (d / 10) / (sp / 3.6)
    add2(0, 1, 1000)
    add2(1, 2, 1000)
    last = 1
    for i in range(1, k + 1):
        nodes.append((400_000_000 + 150 * i, -1_050_000_000))
        add2(last, len(nodes) - 1, 100)
        last = len(nodes) - 1
    nodes.append((400_000_000 + 150 * k, -1_049_999_700))
    add2(last, len(nodes) - 1, 100)                       # the dead end
    nodes.append((399_998_000, -1_050_040_000))
    ring = len(nodes) - 1
    add2(0, ring, 50000, 30)
    add2(ring, 2, 50000, 30)
    recs = [(tr.CAR | tr.BIKE, [0, 1, 2])]
    build_spatial(parse_szrg_bytes(_pack_v4_graph_cls(nodes, edges)), cell_scale=10,
                  output_dir=tmp_path / "routing-data", restrictions=recs)
    sg = _spatial_graph_from_dir(tmp_path / "routing-data")
    sid = {sg.node_coords_e7(n): n for n in range(sg.num_nodes)}
    s, t = sid[nodes[0]], sid[nodes[2]]
    want = _oracle(adj, recs, 0, 2, tr.CAR, 45.0)
    r = find_route_spatial(sg, s, t)
    seq = r.node_sequence
    mid_uturns = sum(1 for i in range(2, len(seq))
                     if seq[i] == seq[i - 2] and len(sg.edges_of_node(seq[i - 1])) > 1)
    assert mid_uturns == 0 and r.total_time_s == pytest.approx(want), (seq, want)
    if shutil.which("node"):
        p = [[nodes[0][0] / 1e7, nodes[0][1] / 1e7, nodes[2][0] / 1e7, nodes[2][1] / 1e7]]
        out = subprocess.run(["node", "-e", _DRIVER, str(WORKER), str(tmp_path), json.dumps(p),
                              json.dumps([{"travel": "drive"}])],
                             check=True, capture_output=True, text=True)
        js = json.loads(out.stdout.strip().splitlines()[-1])[0]
        assert js["time"] == pytest.approx(want)
