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
