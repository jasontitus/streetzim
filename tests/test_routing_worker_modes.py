"""routing-worker.js walking / cycling against the Python reference
(streetzim/routing/modes.py, spatial_astar, SpatialGraph.nearest_node):
the same cost for every edge, the same snapped vertices, the same route
time and length, and geometry drawn the right way round on records
against a one-way (bit 11)."""
from __future__ import annotations

import itertools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from streetzim.routing.modes import edge_cost
from streetzim.routing.spatial_astar import find_route_spatial
from tests.test_routing_worker_v3 import _DRIVER_JS, _spatial_graph_from_dir

ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / "resources" / "viewer" / "routing-worker.js"

# The shared driver, with the travel mode passed to snap and route, and
# the route's coordinates returned.
_DRIVER = (_DRIVER_JS
           .replace("mode: cfg.modeA || 'origin' })",
                    "mode: cfg.modeA || 'origin', travel: cfg.travel })")
           .replace("mode: cfg.modeB || 'dest' })",
                    "mode: cfg.modeB || 'dest', travel: cfg.travel })")
           .replace("options: st.cfg.options || {} });",
                    "options: Object.assign({ travel: st.cfg.travel }, st.cfg.options || {}) });")
           .replace("coords: r ? r.coords.length : 0,",
                    "coords: r ? r.coords.length : 0, path: r ? r.coords : null,"))
assert _DRIVER.count("travel: ") == 3 and "path:" in _DRIVER


def _node():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")


def _run(data_dir, pairs, configs):
    out = subprocess.run(["node", "-e", _DRIVER, str(WORKER), str(data_dir),
                          json.dumps(pairs), json.dumps(configs)],
                         check=True, capture_output=True, text=True, timeout=300)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_edge_costs_match_python_for_every_bit_combination():
    _node()
    bits = [0x20, 0x40, 0x600, 0x1000, 0x2000, 0x4000, 1 << 15, 2 << 15, 3 << 15,
            0x20000, 0x40000, 0x80000, 0x100000, 0x200000]
    cases = []
    for ordv in range(25):
        for k in range(4):
            for combo in itertools.combinations(bits, k):
                ca = ordv
                for b in combo:
                    ca |= b
                for sd in ((0 << 24) | 12345, (50 << 24) | 7):
                    cases.append([sd, ca])
    js = ("const fs=require('fs'),vm=require('vm');global.self={};"
          "vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));"
          "const cs=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));"
          "const out=[];for(const m of ['walk','bike'])for(const [sd,ca] of cs){"
          "const c=edgeCostWB(m,sd,ca);out.push(c<0?null:[c,_wbTime]);}"
          "console.log(JSON.stringify(out));")
    tmp = Path(subprocess.run(["mktemp", "-d", "-p", str(ROOT / "tests")],
                              capture_output=True, text=True).stdout.strip())
    try:
        (tmp / "cases.json").write_text(json.dumps(cases))
        got = json.loads(subprocess.run(["node", "-e", js, str(WORKER), str(tmp / "cases.json")],
                                        check=True, capture_output=True, text=True).stdout)
    finally:
        shutil.rmtree(tmp)
    want = [edge_cost(m, sd, ca) for m in ("walk", "bike") for sd, ca in cases]
    assert len(got) == len(want) > 10000
    for i, (g, w) in enumerate(zip(got, want)):
        assert (g is None) == (w is None), (i, g, w)
        if w is not None:
            assert g[0] == w[0] and g[1] == w[1], (i, g, w)   # bit-exact


@pytest.fixture(scope="module")
def monaco(tmp_path_factory):
    pytest.importorskip("osmium")
    import os

    from streetzim.routing.build import extract_routing_graph
    from streetzim.routing.reader import load_from_file
    from streetzim.routing.spatial import build_spatial

    tmp = tmp_path_factory.mktemp("monaco-modes")
    old = {k: os.environ.get(k) for k in ("STREETZIM_NODE_LOC_DIR", "STREETZIM_ROUTING_WALKBIKE")}
    os.environ["STREETZIM_NODE_LOC_DIR"] = str(tmp)
    os.environ["STREETZIM_ROUTING_WALKBIKE"] = "1"
    try:
        (tmp / "g").mkdir()
        path = extract_routing_graph(
            str(ROOT / "tests/fixtures/monaco-full/monaco.osm.pbf"), str(tmp / "g"))
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    build_spatial(load_from_file(path), cell_scale=10, output_dir=tmp / "routing-data")
    return tmp, path


@pytest.mark.parametrize("travel", ["walk", "bike", "drive"])
def test_worker_routes_match_python_on_monaco(monaco, travel):
    _node()
    import random

    tmp, _ = monaco
    sg = _spatial_graph_from_dir(tmp / "routing-data")
    rng = random.Random(11)
    lat0, lat1, lon0, lon1 = 43.725, 43.751, 7.409, 7.439
    pairs = [[rng.uniform(lat0, lat1), rng.uniform(lon0, lon1),
              rng.uniform(lat0, lat1), rng.uniform(lon0, lon1)] for _ in range(25)]
    res = _run(tmp, pairs, [{"travel": travel}] * len(pairs))
    routed = 0
    for p, r in zip(pairs, res):
        assert r["ok"], r
        s = sg.nearest_node(round(p[0] * 1e7), round(p[1] * 1e7), "origin", travel_mode=travel)
        e = sg.nearest_node(round(p[2] * 1e7), round(p[3] * 1e7), "dest", travel_mode=travel)
        assert (r["start"], r["end"]) == (s, e), (travel, p)
        ref = find_route_spatial(sg, s, e, travel_mode=travel)
        if ref is None:
            assert r["time"] is None
            continue
        routed += 1
        assert r["time"] == pytest.approx(ref.total_time_s, rel=1e-9)
        assert r["distance"] == pytest.approx(ref.total_dist_m, rel=1e-9)
    assert routed >= 22, routed


def test_walking_against_a_one_way_draws_its_geometry_reversed(tmp_path):
    """A four-node one-way street: the walk route against it is the drive
    route's line, point for point, the other way round."""
    _node()
    osmium = pytest.importorskip("osmium")
    import os

    from streetzim.routing.build import extract_routing_graph
    from streetzim.routing.reader import load_from_file
    from streetzim.routing.spatial import build_spatial

    pts = [(7.40, 43.70), (7.401, 43.7003), (7.402, 43.7001), (7.403, 43.7004)]
    pbf = tmp_path / "ow.osm.pbf"
    with osmium.SimpleWriter(str(pbf)) as wr:
        for i, loc in enumerate(pts):
            wr.add_node(osmium.osm.mutable.Node(id=i + 1, location=loc, version=1))
        wr.add_way(osmium.osm.mutable.Way(id=10, nodes=[1, 2, 3, 4], version=1,
                                          tags={"highway": "residential", "oneway": "yes"}))
    os.environ["STREETZIM_NODE_LOC_DIR"] = str(tmp_path)
    try:
        (tmp_path / "g").mkdir()
        g = load_from_file(extract_routing_graph(str(pbf), str(tmp_path / "g")))
    finally:
        os.environ.pop("STREETZIM_NODE_LOC_DIR", None)
    build_spatial(g, cell_scale=10, output_dir=tmp_path / "routing-data")
    a, b = pts[0], pts[-1]
    drive, walk = _run(tmp_path, [[a[1], a[0], b[1], b[0]], [b[1], b[0], a[1], a[0]]],
                       [{"travel": "drive"}, {"travel": "walk"}])
    assert drive["path"] and len(drive["path"]) == 4
    assert walk["path"] == drive["path"][::-1]
    assert [[round(x, 6) for x in c] for c in drive["path"]] == [list(p) for p in pts]
    assert walk["time"] == pytest.approx(walk["distance"] / (5 / 3.6))


def test_worker_heuristic_speeds_match_python():
    """An inadmissible JS heuristic would silently return worse routes."""
    _node()
    from streetzim.routing.modes import HEURISTIC_KPH
    js = ("const fs=require('fs'),vm=require('vm');global.self={};"
          "vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));"
          "console.log(JSON.stringify(TRAVEL_HEURISTIC_KMH));")
    got = json.loads(subprocess.run(["node", "-e", js, str(WORKER)], check=True,
                                    capture_output=True, text=True).stdout)
    assert got == HEURISTIC_KPH


@pytest.mark.parametrize("travel", ["walk", "bike"])
def test_walk_bike_never_two_pass_and_fall_back_within_bounds(monaco, travel):
    """?route=two-pass is ignored for walk / bike (the highway tier is for
    cars); shrunken budgets push them through the weighted and greedy
    passes, which stay within their weight of the optimum."""
    _node()
    tmp, _ = monaco
    sg = _spatial_graph_from_dir(tmp / "routing-data")
    pairs = [[43.7275, 7.4120, 43.7480, 7.4370], [43.7300, 7.4200, 43.7440, 7.4290]]
    two = _run(tmp, pairs, [{"travel": travel, "options": {"route": "two-pass"}}] * 2)
    chain = _run(tmp, pairs, [{"travel": travel,
                               "options": {"popLimits": {"fullOptimal": 40, "fullWeighted": 40}}}] * 2)
    for t, c in zip(two, chain):
        ref = find_route_spatial(sg, t["start"], t["end"], travel_mode=travel)
        assert ref is not None
        assert t["time"] == pytest.approx(ref.total_time_s, rel=1e-9)
        assert all("highway" not in p["label"] for p in t["phases"])
        labels = [p["label"] for p in c["phases"]]
        assert any("1.875" in lab and travel in lab for lab in labels), labels
        # (time, not the penalised cost the search minimises, so no lower
        # bound: a fallback may pick a faster but busier road)
        assert c["time"] is not None and c["time"] <= ref.total_time_s * 1.875 * 1.5


@pytest.mark.parametrize("travel", ["walk", "bike"])
def test_walk_bike_bail_without_two_pass_and_greedy_is_greedy(monaco, travel):
    _node()
    tmp, _ = monaco
    pair = [[43.7275, 7.4120, 43.7480, 7.4370]]
    tiny = {"fullOptimal": 20, "fullWeighted": 20, "fullGreedy": 20}
    (b,) = _run(tmp, pair, [{"travel": travel, "options": {"popLimits": tiny}}])
    assert b["time"] is None
    assert b["phases"] and all("highway" not in p["label"] for p in b["phases"]), b["phases"]
    (opt,) = _run(tmp, pair, [{"travel": travel}])
    (gr,) = _run(tmp, pair, [{"travel": travel,
                              "options": {"popLimits": {"fullOptimal": 1, "fullWeighted": 1}}}])
    greedy_pops = [p["pops"] for p in gr["phases"] if "1.875" in p["label"]]
    assert greedy_pops and greedy_pops[0] < opt["phases"][0]["pops"], (greedy_pops, opt["phases"])


def test_walk_bike_never_run_two_pass(tmp_path):
    """All passes bailing would send a car to the two-pass highway
    fallback; walking and cycling must stop there instead."""
    _node()
    from tests.szrg_reader import parse_szrg_bytes
    from tests.szrg_spatial import build_spatial
    from tests.test_routing_worker_v3 import _pack_v4_graph_cls

    nodes = [(400_000_000, -1_050_000_000 + i * 6_000) for i in range(40)]
    edges = []
    for i in range(39):
        cls = 5 if i >= 1 else 11                       # a primary from node 1 on
        edges += [(i, i + 1, 500, 50, 0xFFFFFFFF, 0, cls),
                  (i + 1, i, 500, 50, 0xFFFFFFFF, 0, cls)]
    build_spatial(parse_szrg_bytes(_pack_v4_graph_cls(nodes, edges)),
                  cell_scale=10, output_dir=tmp_path / "routing-data")
    pair = [[40.0, -105.0, 40.0, -105.0 + 39 * 0.0006]]
    tiny = {"fullOptimal": 5, "fullWeighted": 5, "fullGreedy": 5}
    (car,) = _run(tmp_path, pair, [{"travel": "drive", "options": {"popLimits": tiny}}])
    assert any("highway" in p["label"] for p in car["phases"]), car["phases"]
    for travel in ("walk", "bike"):
        (r,) = _run(tmp_path, pair, [{"travel": travel, "options": {"popLimits": tiny}}])
        assert r["time"] is None
        assert not any("highway" in p["label"] for p in r["phases"]), (travel, r["phases"])


def _run_opts(data_dir, pairs, configs):
    drv = _DRIVER.replace("coords: r ? r.coords.length : 0,",
                          "coords: r ? r.coords.length : 0, endMoved: r ? r.endMoved : null,"
                          " startMoved: r ? r.startMoved : null,")
    out = subprocess.run(["node", "-e", drv, str(WORKER), str(data_dir),
                          json.dumps(pairs), json.dumps(configs)],
                         check=True, capture_output=True, text=True, timeout=300)
    return json.loads(out.stdout.strip().splitlines()[-1])


def _pocket_graph(tmp_path, pocket_gap):
    """A 40-node two-way road (the network) and, `pocket_gap` east of its
    far end, a 40-node two-way stub joined to nothing (a road the extract
    cut off)."""
    from tests.szrg_reader import parse_szrg_bytes
    from tests.szrg_spatial import build_spatial
    from tests.test_routing_worker_v3 import _pack_v4_graph_cls
    lat = 400_000_000
    road = [(lat, -1_050_000_000 + i * 6_000) for i in range(40)]
    x0 = road[-1][1] + pocket_gap
    stub = [(lat + 3_000, x0 + i * 6_000) for i in range(40)]
    edges = []
    for base in (0, 40):
        for i in range(39):
            edges += [(base + i, base + i + 1, 500, 30, 0xFFFFFFFF, 0, 11),
                      (base + i + 1, base + i, 500, 30, 0xFFFFFFFF, 0, 11)]
    build_spatial(parse_szrg_bytes(_pack_v4_graph_cls(road + stub, edges)),
                  cell_scale=10, output_dir=tmp_path / "routing-data")
    return road, stub


def test_an_end_in_a_sealed_pocket_is_snapped_again(tmp_path):
    """The tap is nearest a stub cut off from the network, and the network's
    end is about as close: the route goes there and says so (endMoved /
    startMoved). Without the picked points (older viewers) it is no route."""
    _node()
    road, stub = _pocket_graph(tmp_path, 6_000)
    tap = [(stub[0][0] + 2_000) / 1e7, (stub[0][1] - 1_000) / 1e7]
    start = [road[0][0] / 1e7, road[0][1] / 1e7]
    plain, moved, back = _run_opts(tmp_path, [start + tap, start + tap, tap + start], [
        {"travel": "drive"},
        {"travel": "drive", "options": {"destQuery": {"lat": tap[0], "lon": tap[1]}}},
        {"travel": "drive", "options": {"originQuery": {"lat": tap[0], "lon": tap[1]}}}])
    assert plain["time"] is None
    assert moved["time"] is not None and moved["endMoved"]["lat"] == pytest.approx(road[-1][0] / 1e7)
    assert back["time"] is not None and back["startMoved"]["lon"] == pytest.approx(road[-1][1] / 1e7)


def test_a_far_away_island_stays_no_route(tmp_path):
    """A pocket 5 km from the network: tapping it must not route to the
    mainland shore (the re-snap is only taken when about as close)."""
    _node()
    road, stub = _pocket_graph(tmp_path, 600_000)
    tap = [stub[5][0] / 1e7, stub[5][1] / 1e7]
    start = [road[0][0] / 1e7, road[0][1] / 1e7]
    (r,) = _run_opts(tmp_path, [start + tap],
                     [{"travel": "drive", "options": {"destQuery": {"lat": tap[0], "lon": tap[1]}}}])
    assert r["time"] is None and not r["endMoved"]
