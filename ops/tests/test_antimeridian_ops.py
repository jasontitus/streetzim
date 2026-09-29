"""Regions across the antimeridian in the production pipeline (alaska).

cloud/regions.tsv writes such a bbox with minlon > maxlon. Each ops path
that consumes a registry bbox must cover both sides of 180, and must do
exactly what it did before for every other row: the checks below run the
changed code on a normal row and compare with the old behaviour.
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ALASKA = "172.0,51.0,-130.0,72.0"
NORMAL = "5.4,45.7,11.2,48.2"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _box(s: str):
    return tuple(float(v) for v in s.split(","))


def test_registry_alaska_row_crosses_and_keeps_its_anchor():
    row = next(ln.split("\t") for ln in (ROOT / "cloud" / "regions.tsv").read_text().splitlines()
               if ln.startswith("alaska\t"))
    assert len(row) == 8 and row[2] == ALASKA and row[6] == "Anchorage"
    import create_osm_zim as c
    from streetzim.common import parse_bbox
    anchor = c.registry_anchor(parse_bbox(row[2]))
    assert anchor == ([-149.9003, 61.2181], "Alaska")
    # A normal row still matches exactly as before.
    assert c.registry_anchor(parse_bbox(NORMAL)) is not None


def test_derive_region_mbtiles_ranges():
    m = _load("ops/derive-region-mbtiles.py", "derive_region_mbtiles")
    import mercantile
    # Normal row: the same {z: tuple} as the old code computed.
    got = m.precompute_ranges(_box(NORMAL), 10)
    for z, r in got.items():
        ts = list(mercantile.tiles(*_box(NORMAL), zooms=z))
        n = 1 << z
        assert r == (min(t.x for t in ts), max(t.x for t in ts),
                     min(n - 1 - t.y for t in ts), max(n - 1 - t.y for t in ts))
    # Alaska: both ends of the row, z0 once.
    got = m.precompute_ranges(_box(ALASKA), 10)
    assert len(got[0]) == 1 and got[0][0][:2] == (0, 0)
    z = 10
    cols = sorted(got[z])
    assert len(cols) == 2
    assert cols[0][0] == 0 and cols[1][1] == (1 << z) - 1       # -180.. and ..180
    assert cols[0][1] == mercantile.tile(-130.0, 60, z).x
    assert cols[1][0] == mercantile.tile(172.0, 60, z).x


def test_derive_region_search_boxes():
    m = _load("ops/derive-region-search.py", "derive_region_search")
    assert m._sides(_box(NORMAL)) == [_box(NORMAL)]
    assert m._sides(_box(ALASKA)) == [(172.0, 51.0, 180.0, 72.0), (-180.0, 51.0, -130.0, 72.0)]


def test_terrain_gate_columns():
    m = _load("ops/cloud/check_terrain_coverage.py", "check_terrain_coverage") \
        if importlib.util.find_spec("rasterio") else pytest.skip("rasterio missing")
    w, s, e, n = _box(NORMAL)
    x0, x1 = m.deg2tile(n, w, 10)[0], m.deg2tile(s, e, 10)[0]
    assert list(m.tile_columns(w, s, e, n, 10)) == list(range(min(x0, x1), max(x0, x1) + 1))
    cols = m.tile_columns(*_box(ALASKA), 6)
    assert 0 in cols and 63 in cols and 32 not in cols        # not through Africa


def _block(script: str, start: str, end: str) -> str:
    text = (ROOT / script).read_text()
    i = text.index(start)
    return text[i:text.index(end, i)]


@pytest.mark.parametrize("script,start,end,var", [
    ("ops/build-refresh-queue.sh", 'AREA=(-b "$BBOX")', "if ! osmium extract", "BBOX"),
    ("ops/cloud/rebuild_old_regions.sh", 'AREA=(-b "$BBOX")', "flock ", "BBOX"),
])
def test_extract_commands(tmp_path, script, start, end, var):
    """The osmium arguments these scripts build: `-b BBOX` exactly as before
    for a normal row (and no Python call); a two-ring .poly for alaska."""
    snippet = _block(script, start, end)
    py = tmp_path / "py"
    py.write_text(f"#!/bin/sh\necho called >> {tmp_path}/py.log\ncd {ROOT}\n"
                  f"exec {sys.executable} \"$@\"\n")
    py.chmod(0o755)

    def run(bbox):
        out = subprocess.run(
            ["bash", "-c", f'{var}="$1"; PBF="$2"; PY="$3"\n{snippet}\nprintf "%s\\n" "${{AREA[@]}}"',
             "-", bbox, str(tmp_path / "r.osm.pbf"), str(py)],
            capture_output=True, text=True, check=True)
        return out.stdout.split("\n")[:-1]

    assert run(NORMAL) == ["-b", NORMAL]
    assert run("-180.0,51.0,-130.0,72.0") == ["-b", "-180.0,51.0,-130.0,72.0"]
    assert not (tmp_path / "py.log").exists()
    assert run(ALASKA) == ["-p", str(tmp_path / "r.osm.pbf.poly")]
    poly = (tmp_path / "r.osm.pbf.poly").read_text()
    assert poly.count("END") == 3 and "-130.0000000" in poly and "172.0000000" in poly


def test_extract_region_pbfs_config(tmp_path):
    """The osmium config entry: the same "bbox" entry for a normal row, a
    two-part multipolygon (valid JSON osmium reads) for alaska."""
    snippet = _block("ops/extract-region-pbfs.sh", "if awk -F, '{ exit", "    done\n")

    def entry(bbox):
        return subprocess.run(["bash", "-c", f'id=x; bbox="$1"\n{snippet}', "-", bbox],
                              capture_output=True, text=True, check=True).stdout

    assert entry(NORMAL) == \
        '  {"output": "x.osm.pbf.part", "output_format": "pbf", "bbox": [%s]}' % NORMAL
    import json
    e = json.loads(entry(ALASKA))
    west, east = e["multipolygon"]
    assert west[0][0] == [172.0, 51.0] and [180, 72.0] in west[0]
    assert east[0][0] == [-180, 51.0] and [-130.0, 72.0] in east[0]
    if shutil.which("osmium"):
        # osmium accepts it: extract a tiny file with points each side.
        opl = tmp_path / "p.opl"
        opl.write_text("n1 v1 x179.5 y60\nn2 v1 x-150 y61\nn3 v1 x0 y60\n")
        cfg = tmp_path / "c.json"
        cfg.write_text('{"directory": "%s", "extracts": [%s]}' % (tmp_path, entry(ALASKA)))
        subprocess.run(["osmium", "extract", "-c", str(cfg), str(opl), "--overwrite",
                        "-s", "complete_ways"], check=True, capture_output=True)
        subprocess.run(["osmium", "cat", "-F", "pbf", str(tmp_path / "x.osm.pbf.part"), "-o",
                        str(tmp_path / "o.opl"), "--overwrite"], check=True)
        ids = re.findall(r"^n(\d+)", (tmp_path / "o.opl").read_text(), re.M)
        assert sorted(ids) == ["1", "2"]


def test_overture_filter():
    m = _load("download_overture_data.py", "download_overture_data")
    assert m.bbox_where(5.4, 45.7, 11.2, 48.2) == (
        "bbox.xmin >= 5.4 AND bbox.xmax <= 11.2\n"
        "        AND bbox.ymin >= 45.7 AND bbox.ymax <= 48.2")
    assert m.bbox_where(*_box(ALASKA)) == (
        "(bbox.xmin >= 172.0 AND bbox.xmax <= 180.0 AND bbox.ymin >= 51.0 AND bbox.ymax <= 72.0)"
        " OR (bbox.xmin >= -180.0 AND bbox.xmax <= -130.0 AND bbox.ymin >= 51.0 AND bbox.ymax <= 72.0)")
