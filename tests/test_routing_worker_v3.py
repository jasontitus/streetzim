"""Exercise the browser routing worker against a generated SZCI v3 graph."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.szrg_reader import parse_szrg_bytes
from tests.szrg_spatial import build_spatial
from tests.test_spatial_chunking import _pack_v4_graph


ROOT = Path(__file__).resolve().parent.parent


def test_worker_snaps_and_routes_szci_v3(tmp_path: Path):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")

    nodes = [
        (0, 0),
        (2_000_000, 0),
        (4_000_000, 0),
    ]
    edges = [
        (0, 1, 10_000, 60, 0xFFFFFFFF, 0),
        (1, 2, 10_000, 60, 0xFFFFFFFF, 0),
    ]
    graph = parse_szrg_bytes(_pack_v4_graph(nodes, edges))
    routing_dir = tmp_path / "routing-data"
    build_spatial(graph, cell_scale=10, output_dir=routing_dir)

    script = r"""
const fs = require('fs');
const vm = require('vm');
const workerPath = process.argv[1];
const dataDir = process.argv[2];
const messages = [];
let timer = setTimeout(() => {
  console.error(JSON.stringify(messages));
  process.exit(2);
}, 5000);

function arrayBufferFor(buf) {
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
}
global.performance = { now: () => Date.now() };
global.fetch = async function(url) {
  const path = new URL(url).pathname;
  const buf = fs.readFileSync(path);
  return {
    ok: true,
    headers: { get: () => null },
    arrayBuffer: async () => arrayBufferFor(buf),
  };
};
global.self = {
  postMessage(msg) {
    messages.push(msg);
    if (msg.type === 'ready') {
      self.onmessage({data: {cmd: 'snap', id: 1, lat: 0.0001, lon: 0}});
    } else if (msg.type === 'snap-done' && msg.id === 1) {
      self.onmessage({data: {cmd: 'snap', id: 2, lat: 0.3999, lon: 0}});
    } else if (msg.type === 'snap-done' && msg.id === 2) {
      self.onmessage({data: {
        cmd: 'route', id: 3,
        start: messages.find(m => m.type === 'snap-done' && m.id === 1).node,
        end: msg.node,
      }});
    } else if (msg.type === 'route-done') {
      clearTimeout(timer);
      console.log(JSON.stringify(messages));
    }
  },
};
vm.runInThisContext(fs.readFileSync(workerPath, 'utf8'), {filename: workerPath});
self.onmessage({data: {cmd: 'init', baseUrl: 'file://' + dataDir + '/'}});
"""
    result = subprocess.run(
        ["node", "-e", script,
         str(ROOT / "resources" / "viewer" / "routing-worker.js"),
         str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    messages = json.loads(result.stdout.strip().splitlines()[-1])
    ready = next(m for m in messages if m["type"] == "ready")
    route = next(m for m in messages if m["type"] == "route-done")
    assert ready["format"] == "spatial-v3"
    assert route["ok"] is True
    assert route["result"]["distance"] == pytest.approx(2000.0)
    assert route["result"]["coords"][0] == [0, 0]
    assert route["result"]["coords"][-1] == [0, 0.4]


# ---------------------------------------------------------------------------
# Differential test: the worker's "optimal" pass must return the same travel
# time as the Python reference A* (tests/szrg_spatial_astar.py) on a random
# multi-cell grid with mixed speeds (30/50/100 km/h). This is what caught the
# inadmissible 80 km/h heuristic — with motorway edges at 100 km/h the JS
# engine returned routes a few percent slower than optimal on ~half the pairs.
# ---------------------------------------------------------------------------

_DRIVER_JS = r"""
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const workerPath = process.argv[1];
const dataDir = process.argv[2];
const pairs = JSON.parse(process.argv[3]);
function arrayBufferFor(buf) {
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
}
global.performance = { now: () => Date.now() };
global.fetch = async function(url) {
  const p = new URL(url).pathname;
  let buf;
  try { buf = fs.readFileSync(p); }
  catch (e) { return { ok: false, status: 404, headers: { get: () => null } }; }
  return { ok: true, status: 200, headers: { get: () => null },
           arrayBuffer: async () => arrayBufferFor(buf) };
};
console.warn = function() {};  // keep stdout clean
const results = [];
let cur = -1, st = null, msgId = 0;
const timer = setTimeout(() => { console.error('timeout'); process.exit(2); }, 60000);
function post(m) { self.onmessage({ data: m }); }
function next() {
  cur++;
  if (cur >= pairs.length) {
    clearTimeout(timer);
    console.log(JSON.stringify(results));
    return;
  }
  const p = pairs[cur];
  st = { a: null, b: null };
  post({ cmd: 'snap', id: ++msgId, lat: p[0], lon: p[1] }); st.idA = msgId;
  post({ cmd: 'snap', id: ++msgId, lat: p[2], lon: p[3] }); st.idB = msgId;
}
global.self = {
  postMessage(msg) {
    if (msg.type === 'ready') next();
    else if (msg.type === 'init-error') { console.error(msg.error); process.exit(3); }
    else if (msg.type === 'snap-done') {
      if (msg.error) { console.error(msg.error); process.exit(4); }
      if (msg.id === st.idA) st.a = msg;
      if (msg.id === st.idB) st.b = msg;
      if (st.a && st.b) {
        post({ cmd: 'route', id: ++msgId, start: st.a.node, end: st.b.node });
      }
    } else if (msg.type === 'route-done') {
      const r = msg.result;
      results.push({ start: st.a.node, end: st.b.node, ok: msg.ok,
                     error: msg.error || null,
                     time: r ? r.time : null, distance: r ? r.distance : null,
                     coords: r ? r.coords.length : 0 });
      next();
    }
  },
};
vm.runInThisContext(fs.readFileSync(workerPath, 'utf8'), { filename: workerPath });
post({ cmd: 'init', baseUrl: 'file://' + path.resolve(dataDir) + '/' });
"""


def _grid_graph(w, h, seed, *, step=0.005, lat0=40.0, lon0=-105.0,
                drop_prob=0.08, cut_column=None):
    """Random 4-connected grid with mixed speeds. `cut_column` removes
    every edge crossing that column so the graph splits in two."""
    import random
    from tests.szrg_astar import haversine_m

    rnd = random.Random(seed)
    nodes = []
    for j in range(h):
        for i in range(w):
            lat = lat0 + j * step + rnd.uniform(-step * 0.2, step * 0.2)
            lon = lon0 + i * step + rnd.uniform(-step * 0.2, step * 0.2)
            nodes.append((int(round(lat * 1e7)), int(round(lon * 1e7))))
    names = ["", "Main St", "I-25", "Oak Ave"]
    edges = []

    def add(a, b):
        if rnd.random() < drop_prob:
            return
        la, lo = nodes[a]
        lb, lob = nodes[b]
        dist_dm = max(1, int(round(haversine_m(la / 1e7, lo / 1e7,
                                                lb / 1e7, lob / 1e7) * 10)))
        speed = rnd.choice([30, 30, 50, 60, 100])
        name = rnd.randrange(len(names))
        edges.append((a, b, dist_dm, speed, 0xFFFFFFFF, name))
        if rnd.random() > 0.15:
            edges.append((b, a, dist_dm, speed, 0xFFFFFFFF, name))

    for j in range(h):
        for i in range(w):
            n = j * w + i
            if i + 1 < w and not (cut_column is not None and i + 1 == cut_column):
                add(n, n + 1)
            if j + 1 < h:
                add(n, n + w)
    return nodes, edges, names


def _run_worker(tmp_path: Path, pairs):
    result = subprocess.run(
        ["node", "-e", _DRIVER_JS,
         str(ROOT / "resources" / "viewer" / "routing-worker.js"),
         str(tmp_path), json.dumps(pairs)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _spatial_graph_from_dir(routing_dir: Path):
    from tests.szrg_spatial import spatial_graph_from_memory

    idx = (routing_dir / "graph-cells-index.bin").read_bytes()
    cells = {int(p.stem.split("-")[-1]): p.read_bytes()
             for p in routing_dir.glob("graph-cell-*.bin")}
    return spatial_graph_from_memory(idx, cells)


def test_worker_optimal_pass_matches_python_reference(tmp_path: Path):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    import random
    from tests.szrg_spatial_astar import find_route_spatial

    nodes, edges, names = _grid_graph(60, 60, seed=7)
    graph = parse_szrg_bytes(_pack_v4_graph(nodes, edges, names))
    routing_dir = tmp_path / "routing-data"
    build_spatial(graph, cell_scale=10, output_dir=routing_dir)
    assert len(list(routing_dir.glob("graph-cell-*.bin"))) > 1, "want a multi-cell graph"

    rnd = random.Random(99)
    lat_lo, lat_hi = 40.0, 40.0 + 59 * 0.005
    lon_lo, lon_hi = -105.0, -105.0 + 59 * 0.005
    pairs = [[rnd.uniform(lat_lo, lat_hi), rnd.uniform(lon_lo, lon_hi),
              rnd.uniform(lat_lo, lat_hi), rnd.uniform(lon_lo, lon_hi)]
             for _ in range(8)]

    results = _run_worker(tmp_path, pairs)
    sg = _spatial_graph_from_dir(routing_dir)
    checked = 0
    for r in results:
        assert r["ok"] is True, r
        ref = find_route_spatial(sg, r["start"], r["end"])
        if ref is None:
            assert r["time"] is None, r
            continue
        assert r["time"] is not None, r
        assert r["time"] == pytest.approx(ref.total_time_s, rel=1e-9), r
        assert r["distance"] == pytest.approx(ref.total_dist_m, rel=1e-6), r
        assert r["coords"] >= 2
        checked += 1
    assert checked >= 5, "most random pairs should be routable"


def test_worker_reports_unreachable_as_ok_with_null_result(tmp_path: Path):
    """A destination in a disconnected component must come back as
    ok:true + result:null (search completed, no route) — NOT ok:false,
    which the bridge treats as an engine failure and answers by re-running
    the whole search on the main thread."""
    if shutil.which("node") is None:
        pytest.skip("node is not installed")

    nodes, edges, names = _grid_graph(20, 10, seed=3, drop_prob=0.0, cut_column=10)
    graph = parse_szrg_bytes(_pack_v4_graph(nodes, edges, names))
    build_spatial(graph, cell_scale=10, output_dir=tmp_path / "routing-data")

    west = [40.02, -105.0 + 2 * 0.005]
    east = [40.02, -105.0 + 17 * 0.005]
    results = _run_worker(tmp_path, [west + east, west + [40.03, -105.0 + 3 * 0.005]])
    unreachable, reachable = results
    assert unreachable["ok"] is True and unreachable["error"] is None
    assert unreachable["time"] is None
    assert reachable["ok"] is True and reachable["time"] is not None


def _pack_v4_graph_cls(nodes_e7, edges, names=("",)):
    """Like _pack_v4_graph but each edge carries a 7th element,
    class_access, so tests can mark footway ordinals / bit 9."""
    import struct
    num_nodes = len(nodes_e7)
    nodes_blob = struct.pack(f"<{num_nodes * 2}i",
                             *[v for (lat, lon) in nodes_e7 for v in (lat, lon)])
    edges_sorted = sorted(edges, key=lambda e: e[0])
    adj = [0] * (num_nodes + 1)
    for e in edges_sorted:
        adj[e[0] + 1] += 1
    for i in range(1, num_nodes + 1):
        adj[i] += adj[i - 1]
    vals = []
    for (_f, target, dist_dm, speed, geom_idx, name_idx, cls) in edges_sorted:
        vals += [target, ((speed & 0xFF) << 24) | (dist_dm & 0xFFFFFF),
                 geom_idx, name_idx, cls]
    name_offsets = [0]
    name_bytes = b""
    for n in names:
        name_bytes += n.encode("utf-8")
        name_offsets.append(len(name_bytes))
    header = b"SZRG" + struct.pack("<7I", 4, num_nodes, len(edges), 0, 0,
                                   len(names), len(name_bytes))
    return (header + nodes_blob + struct.pack(f"<{num_nodes + 1}I", *adj)
            + struct.pack(f"<{len(vals)}I", *vals) + struct.pack("<I", 0)
            + struct.pack(f"<{len(names) + 1}I", *name_offsets) + name_bytes)


def test_worker_snap_skips_footway_vertex_and_isolated_fragment(tmp_path: Path):
    """The nearest vertex to a tap is often unusable for a car: a footway
    vertex (class ordinal 17 on pre-bit-9 ZIMs) or a tiny fragment the
    A* can never leave. Both the worker and the Python snapper must skip
    them for the connected road a few metres further away."""
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    from tests.szrg_spatial import SpatialGraph  # noqa: F401  (import check)

    # A 60-node two-way road running east along lat 40.0000 at 50 m steps.
    nodes = [(400_000_000, -1_050_000_000 + i * 6_000) for i in range(60)]
    edges = []
    for i in range(59):
        edges.append((i, i + 1, 500, 50, 0xFFFFFFFF, 0, 11))
        edges.append((i + 1, i, 500, 50, 0xFFFFFFFF, 0, 11))
    # Query point 20 m north of road node 30.
    q_lat, q_lon = 400_001_800, nodes[30][1]
    # (a) footway-only vertex 5 m from the query (ordinal 17, bit 9 clear,
    #     exactly what a June-2026 ZIM contains).
    foot = len(nodes)
    nodes.append((q_lat + 450, q_lon))
    nodes.append((q_lat + 450, q_lon + 3_000))
    edges.append((foot, foot + 1, 250, 5, 0xFFFFFFFF, 0, 17))
    edges.append((foot + 1, foot, 250, 5, 0xFFFFFFFF, 0, 17))
    # (b) a three-node private-drive fragment 8 m from the query,
    #     car-ok edges but disconnected from the road.
    frag = len(nodes)
    nodes.append((q_lat - 720, q_lon))
    nodes.append((q_lat - 720, q_lon + 2_000))
    nodes.append((q_lat - 720, q_lon + 4_000))
    for a, b in ((frag, frag + 1), (frag + 1, frag + 2)):
        edges.append((a, b, 150, 20, 0xFFFFFFFF, 0, 14))
        edges.append((b, a, 150, 20, 0xFFFFFFFF, 0, 14))

    graph = parse_szrg_bytes(_pack_v4_graph_cls(nodes, edges))
    routing_dir = tmp_path / "routing-data"
    build_spatial(graph, cell_scale=10, output_dir=routing_dir)

    # Worker: snap the query and route to the far end of the road.
    out = _run_worker(tmp_path, [[q_lat / 1e7, q_lon / 1e7,
                                  nodes[59][0] / 1e7, nodes[59][1] / 1e7]])
    r = out[0]
    assert r["start"] == 30, f"snapped to {r['start']} instead of road node 30"
    assert r["end"] == 59
    assert r["ok"] and r["time"] is not None

    # Python reference snapper agrees.
    sg = _spatial_graph_from_dir(routing_dir)
    assert sg.nearest_node(q_lat, q_lon) == 30
    # And the reference A* never uses the footway (ordinal rule).
    from tests.szrg_spatial_astar import find_route_spatial
    ref = find_route_spatial(sg, 30, 59)
    assert ref is not None
    assert abs(ref.total_time_s - r["time"]) < 1e-6
