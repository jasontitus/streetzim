"""The address, wiki-tag and routing steps skip their own `osmium extract`
when the build already cut the extract to the same box (create_osm_zim's
area.osm.pbf): same output, one osmium run instead of four (each took
3.7 GB even for Luxembourg)."""
from __future__ import annotations

import filecmp
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("osmium")
if not shutil.which("osmium"):
    pytest.skip("osmium CLI not on PATH", allow_module_level=True)

MONACO = ROOT / "tests" / "fixtures" / "monaco-full" / "monaco.osm.pbf"
BOX = "7.40,43.72,7.44,43.76"


@pytest.fixture(scope="module")
def cut(tmp_path_factory):
    """Monaco cut to BOX, as create_osm_zim cuts it before the tiles."""
    from streetzim import tiles
    out = tmp_path_factory.mktemp("cut") / "area.osm.pbf"
    tiles.extract_bbox_from_pbf(str(MONACO), BOX, str(out))
    return out


@pytest.fixture
def osmium_extracts(monkeypatch):
    """Record every `osmium extract` the code under test runs."""
    calls = []
    real = subprocess.run

    def run(cmd, *a, **kw):
        if list(cmd[:2]) == ["osmium", "extract"]:
            calls.append(cmd)
        return real(cmd, *a, **kw)
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def box():
    from streetzim.common import parse_bbox
    return parse_bbox(BOX)


def test_routing_graph_is_the_same_without_the_second_cut(cut, tmp_path, osmium_extracts):
    from streetzim.routing.build import extract_routing_graph
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = extract_routing_graph(str(cut), str(tmp_path / "a"), bbox=box())
    assert len(osmium_extracts) == 1
    b = extract_routing_graph(str(cut), str(tmp_path / "b"), bbox=box(), precut=True)
    assert len(osmium_extracts) == 1                     # no second cut
    assert filecmp.cmp(a, b, shallow=False)
    assert cut.exists()                                  # the shared cut is left alone


def test_addresses_are_the_same_without_the_second_cut(cut, tmp_path, osmium_extracts):
    from streetzim.addresses import extract_addresses_pbf
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text("")
    b.write_text("")
    na = extract_addresses_pbf(str(cut), str(a), bbox=box())
    assert len(osmium_extracts) == 1
    nb = extract_addresses_pbf(str(cut), str(b), bbox=box(), precut=True)
    assert len(osmium_extracts) == 1
    assert na == nb and na > 0
    assert a.read_bytes() == b.read_bytes()


def test_wiki_tags_are_the_same_without_the_second_cut(cut, osmium_extracts):
    from streetzim.addresses import extract_wiki_tags_pbf
    a = extract_wiki_tags_pbf(str(cut), bbox=box())
    assert len(osmium_extracts) == 1
    b = extract_wiki_tags_pbf(str(cut), bbox=box(), precut=True)
    assert len(osmium_extracts) == 1
    assert a == b and a
