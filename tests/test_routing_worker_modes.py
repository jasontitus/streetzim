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
            0x20000, 0x40000, 0x80000, 0x100000]
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
