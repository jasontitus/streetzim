"""Regions across the antimeridian in the production pipeline (alaska).

cloud/regions.tsv writes such a bbox with minlon > maxlon. Each ops path
that consumes a registry bbox must cover both sides of 180, and must do
exactly what it did before for every other row: the checks below run the
changed code on a normal row and compare with the old behaviour.
"""
from __future__ import annotations

import importlib.util
import os
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


def _stubs(tmp_path: Path, py_fails: bool = False) -> dict[str, str]:
    """A PATH whose `osmium` records its arguments (and writes its -o file),
    and a PY that runs the real python, or fails."""
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "osmium").write_text(
        "#!/bin/bash\nprintf '%s\\n' \"$*\" >> \"$STUB_LOG\"\n"
        "while [ $# -gt 0 ]; do [ \"$1\" = -o ] && echo x > \"$2\"; shift; done\n")
    (bin_ / "osmium").chmod(0o755)
    py = tmp_path / "py"
    py.write_text("#!/bin/sh\necho python >> \"$STUB_LOG\"\n" + (
        "exit 1\n" if py_fails else f"PYTHONPATH={ROOT} exec {sys.executable} \"$@\"\n"))
    py.chmod(0o755)
    return {"PATH": f"{bin_}:{os.environ['PATH']}", "PY": str(py),
            "STUB_LOG": str(tmp_path / "calls.log")}


HARNESS = f'''
. {ROOT}/ops/region-bbox.sh
log() {{ echo "LOG $*" >> "$STUB_LOG"; }}
row() {{ echo "ROW $*" >> "$STUB_LOG"; }}
flock() {{ shift; "$@"; }}
n_fail=0
'''


def _osmium(calls: str) -> list[str]:
    return [c for c in calls.splitlines() if c.startswith("extract")]


def _run(tmp_path, script_block, bbox, env, pre=""):
    """Run a real block of an ops script once for one row; returns the
    calls log (osmium argv lines, python calls, log/row lines)."""
    log = Path(env["STUB_LOG"])
    log.unlink(missing_ok=True)
    body = f'{HARNESS}\nID=r; BBOX="$1"\n{pre}\nfor _once in 1; do\n{script_block}\ndone\n'
    subprocess.run(["bash", "-c", body, "-", bbox], env={**os.environ, **env},
                   cwd=tmp_path, check=True, capture_output=True, text=True)
    return log.read_text() if log.exists() else ""


QUEUE = ("ops/build-refresh-queue.sh", '  PBF="$REGDIR/$ID.osm.pbf"', '  log "  PBF:')
REBUILD = ("ops/cloud/rebuild_old_regions.sh", "  PBF=world-data/regions/${ID}.osm.pbf",
           "  # NEVER")


@pytest.mark.parametrize("which", ["queue", "rebuild"])
def test_extract_step(tmp_path, which):
    """The real extract block, with osmium stubbed. A normal row runs the
    same command as before (and no python); alaska gets a .poly and never
    -b; a failed .poly skips the row; sidecars make a PBF cut for another
    bbox stale."""
    script, start, end = QUEUE if which == "queue" else REBUILD
    block = _block(script, start, end)
    (tmp_path / "world-data" / "regions").mkdir(parents=True)
    planet = tmp_path / "planet.osm.pbf"
    planet.write_bytes(b"x")
    os.utime(planet, (1_000_000, 1_000_000))
    pre = f'REGDIR={tmp_path}/world-data/regions; PLANET={planet}; LOG=/dev/null'
    pbf = tmp_path / "world-data" / "regions" / "r.osm.pbf"
    env = _stubs(tmp_path)
    before = {"queue": f"extract -b {NORMAL} {planet} -o {pbf}.part -f pbf --overwrite "
                       "--strategy complete_ways",
              "rebuild": f"extract -b {NORMAL} {planet} -o world-data/regions/r.osm.pbf "
                         "--overwrite"}[which]

    calls = _run(tmp_path, block, NORMAL, env, pre)
    assert [c for c in calls.splitlines() if not c.startswith("LOG")] == [before]
    assert pbf.with_name("r.osm.pbf.bbox").read_text() == NORMAL + "\n"
    # Fresh, sidecar matches: nothing to do.
    assert not _osmium(_run(tmp_path, block, NORMAL, env, pre))
    # An existing extract with no sidecar is trusted for a normal row...
    pbf.with_name("r.osm.pbf.bbox").unlink()
    assert not _osmium(_run(tmp_path, block, NORMAL, env, pre))
    assert pbf.with_name("r.osm.pbf.bbox").exists()
    # ...but not for a row across the antimeridian: warned, re-extracted.
    pbf.with_name("r.osm.pbf.bbox").unlink()
    calls = _run(tmp_path, block, ALASKA, env, pre)
    assert "no .bbox sidecar" in calls and "WARNING" in calls
    ext = [c for c in calls.splitlines() if c.startswith("extract")]
    assert len(ext) == 1 and " -p " in ext[0] and " -b " not in ext[0]
    assert pbf.with_name("r.osm.pbf.bbox").read_text() == ALASKA + "\n"
    # The row's bbox changed since the extract: stale, re-extracted.
    calls = _run(tmp_path, block, "-180.0,51.0,-130.0,72.0", env, pre)
    assert "cut for bbox" in calls and "extract -b -180.0,51.0,-130.0,72.0" in calls
    # The .poly cannot be written: the row fails, osmium is never run.
    pbf.unlink()
    env_bad = _stubs(tmp_path, py_fails=True)
    calls = _run(tmp_path, block, ALASKA, env_bad, pre)
    assert "POLY FAILED" in calls and "ROW r extract-failed" in calls
    assert not any(c.startswith("extract") for c in calls.splitlines())


def test_link_world_parks_a_slice_cut_for_another_bbox(tmp_path):
    fn = _block("ops/build-refresh-queue.sh", "link_world() {", "\n}\n") + "\n}\n"
    env = _stubs(tmp_path)
    world = tmp_path / "world.mbtiles"
    world.write_bytes(b"w")
    os.utime(world, (1_000_000, 1_000_000))
    sl = tmp_path / "r.mbtiles"
    run = lambda bbox: _run(tmp_path, f'link_world "{world}" "{sl}"', bbox, env,
                            f"{fn}\nTODAY=t")
    sl.write_bytes(b"slice")
    run(NORMAL)                                    # trusted, sidecar written
    assert not sl.is_symlink() and sl.with_name("r.mbtiles.bbox").exists()
    run(NORMAL)
    assert not sl.is_symlink()
    run(ALASKA)                                    # another bbox: parked
    assert sl.is_symlink() and (tmp_path / "r.mbtiles.stale-t").exists()


def test_extract_region_pbfs_config(tmp_path):
    """The osmium config entry: the same "bbox" entry for a normal row, a
    two-part multipolygon (valid JSON osmium reads) for alaska."""
    snippet = _block("ops/extract-region-pbfs.sh", "if bbox_crosses", "    done\n")

    def entry(bbox):
        return subprocess.run(["bash", "-c", f'. {ROOT}/ops/region-bbox.sh; id=x; bbox="$1"\n{snippet}',
                               "-", bbox], capture_output=True, text=True, check=True).stdout

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


def test_osmium_b_with_a_crossing_box_takes_the_complement(tmp_path):
    """Why a crossing bbox must never reach -b: osmium extracts the band
    round the other side of the world, and exits 0."""
    if not shutil.which("osmium"):
        pytest.skip("osmium CLI not on PATH")
    opl = tmp_path / "p.opl"
    opl.write_text("n1 v1 x179.5 y60\nn2 v1 x-150 y61\nn3 v1 x0 y60\n")
    out = tmp_path / "o.opl"
    r = subprocess.run(["osmium", "extract", "-b", ALASKA, str(opl), "-o", str(out),
                        "--overwrite"], capture_output=True)
    if r.returncode == 0:
        assert re.findall(r"^n(\d+)", out.read_text(), re.M) != ["1", "2"]


OLD_SQL = """
    COPY (
      SELECT {cols}
      FROM read_parquet('{source}', hive_partitioning=1)
      WHERE bbox.xmin >= {minlon} AND bbox.xmax <= {maxlon}
        AND bbox.ymin >= {minlat} AND bbox.ymax <= {maxlat}
    ) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD);
    """


def test_overture_sql(tmp_path):
    m = _load("download_overture_data.py", "download_overture_data")
    minlon, minlat, maxlon, maxlat = _box(NORMAL)
    # Byte-identical to the statement before this change, for a normal row.
    assert m.overture_sql("a, b", "s3://x/*.parquet", _box(NORMAL), "o.parquet") == \
        OLD_SQL.format(cols="a, b", source="s3://x/*.parquet", minlon=minlon, maxlon=maxlon,
                       minlat=minlat, maxlat=maxlat, out="o.parquet")
    sql = m.overture_sql("id", "src", _box(ALASKA), "o.parquet")
    assert sql.count("SELECT") == 2 and "UNION ALL" in sql and " OR " not in sql
    duckdb = pytest.importorskip("duckdb")
    src = tmp_path / "src.parquet"
    con = duckdb.connect()
    con.execute(f"""COPY (SELECT * FROM (VALUES
        (1, {{'xmin': 179.5, 'xmax': 179.6, 'ymin': 60.0, 'ymax': 60.1}}),
        (2, {{'xmin': -150.0, 'xmax': -149.9, 'ymin': 61.0, 'ymax': 61.1}}),
        (3, {{'xmin': 0.0, 'xmax': 0.1, 'ymin': 60.0, 'ymax': 60.1}})) t(id, bbox))
        TO '{src}' (FORMAT PARQUET)""")
    out = tmp_path / "o.parquet"
    con.execute(m.overture_sql("id", str(src), _box(ALASKA), str(out)).replace(
        ", hive_partitioning=1", ""))
    assert sorted(r[0] for r in con.execute(f"SELECT id FROM '{out}'").fetchall()) == [1, 2]


def test_derive_region_search_writes_sidecars(tmp_path, monkeypatch):
    m = _load("ops/derive-region-search.py", "derive_region_search_run")
    reg = tmp_path / "regions.tsv"
    reg.write_text(f"alaska\tAlaska\t{ALASKA}\tlocal\n")
    src = tmp_path / "w.jsonl"
    src.write_text('{"lat": 60, "lon": 179.5}\n{"lat": 61, "lon": -150}\n{"lat": 60, "lon": 0}\n')
    monkeypatch.setattr(m, "DST_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["d", "--src", str(src), "--registry", str(reg)])
    m.main()
    assert len((tmp_path / "alaska.search.jsonl").read_text().splitlines()) == 2
    assert (tmp_path / "alaska.search.jsonl.bbox").read_text() == ALASKA + "\n"
