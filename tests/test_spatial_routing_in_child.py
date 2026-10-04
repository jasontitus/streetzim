"""The ZIM step's spatial chunking of the routing graph runs in a child
process (it loads the whole graph: China's is 4.9 GB, on top of the ZIM
writer's own memory, which took China in 16 GB to 15.6 GB): the same
items and the same cell files as in this process."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("osmium")

from streetzim import zim_writer  # noqa: E402
from streetzim.routing.build import extract_routing_graph  # noqa: E402
from tests.test_routing_build_memory import _network  # noqa: E402


class _Item:
    def __init__(self, path, title, mime, content, compress=True):
        self.path, self.content, self.compress = path, content, compress


class _Creator:
    def __init__(self):
        self.items = []

    def add_item(self, item):
        self.items.append(item)


def _chunk(tmp_path, name):
    out = tmp_path / name
    out.mkdir()
    graph = extract_routing_graph(str(_network(tmp_path / f"{name}.osm.pbf", 5)), str(out))
    creator = _Creator()
    zim_writer._add_routing_graph(creator, _Item, routing_graph_path=graph,
                                  routing_graph_chunk_mb=0, spatial_chunk_scale=10)
    return {i.path: (Path(i.content).read_bytes(), i.compress) for i in creator.items}


def test_spatial_chunking_in_a_child_matches_in_process(tmp_path, monkeypatch):
    from streetzim import isolate
    calls = []
    real = isolate.run_in_child

    def recording(fn, *a, **k):
        calls.append(fn.__name__)
        return real(fn, *a, **k)

    monkeypatch.setattr(isolate, "run_in_child", recording)
    child = _chunk(tmp_path, "child")
    assert calls == ["_spatial_cell_files"]
    monkeypatch.setattr(isolate, "run_in_child", lambda fn, *a, **k: fn(*a, **k))
    here = _chunk(tmp_path, "here")
    assert child == here
    assert "routing-data/graph-cells-index.bin" in child
    assert sum(p.startswith("routing-data/graph-cell-") for p in child) >= 1
