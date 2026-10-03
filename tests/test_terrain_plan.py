"""Terrain without a world DEM (every `streetzim` build, every fresh Zimfarm
task): which zooms are made, which DEM cells each needs, how the download
fails, and the audit that checks every tile against the DEM.

The builds here run on synthetic DEM cells written by a stand-in for the
download (or a stand-in for urlopen), so nothing touches the network."""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

rasterio = pytest.importorskip("rasterio")
mercantile = pytest.importorskip("mercantile")
np = pytest.importorskip("numpy")
from PIL import Image  # noqa: E402

from streetzim import terrain as T  # noqa: E402

MONACO_OLD = (7.40, 43.72, 7.44, 43.76)
MONACO = (7.39, 43.715, 7.46, 43.765)         # the preset since viewer-polish
LUXEMBOURG = (5.732, 49.444, 6.535, 50.187)
SWITZERLAND = (5.935, 45.8, 10.51, 47.83)
NETHERLANDS = (3.04, 50.71, 7.27, 53.77)
FRANCE = (-5.2, 41.3, 9.6, 51.1)


# ------------------------------------------------------------------ the plan


def test_min_zoom_is_two_below_the_lowest_the_viewer_can_show():
    # The viewer pins the view to the bbox (maxBounds, no margin); on a
    # 320-px viewport Monaco never shows below map zoom 11.7, Luxembourg
    # below 8.1. Terrain starts two levels lower: 3D terrain reads DEM tiles
    # one level below the ones it draws (deltaZoom 1), plus a tilted far side.
    assert 11.6 < T.viewer_min_map_zoom(MONACO) < 11.7
    assert 8.1 < T.viewer_min_map_zoom(LUXEMBOURG) < 8.2
    assert T.terrain_min_zoom(MONACO, 12) == 9
    assert T.terrain_min_zoom(MONACO_OLD, 12) == 10
    assert T.terrain_min_zoom(LUXEMBOURG, 12) == 6
    assert T.terrain_min_zoom(SWITZERLAND, 12) == 4
    assert T.terrain_min_zoom(MONACO_OLD, 9) == 9         # never above max zoom
    assert T.terrain_min_zoom((-180, -85, 180, 85), 12) == 0


def test_a_world_dem_keeps_the_production_layout(tmp_path):
    world = tmp_path / "world.tif"
    world.write_bytes(b"x")
    assert T.terrain_min_zoom(MONACO, 12, str(world)) == 0
    plan = T.TerrainPlan(MONACO_OLD, 12, low_zoom_world_vrt=str(world))
    assert not plan.fresh and plan.min_zoom == 0 and plan.low_cells == []
    # bbox + 1 degree of GLO-30, as before.
    assert sorted(plan.glo30_cells) == [(lat, lon) for lat in (42, 43, 44) for lon in (6, 7, 8)]
    assert plan.marker_name == "COMPLETED_z12_7.4_43.72_7.44_43.76"
    assert plan.vrt_for_zoom(7, "regional.vrt", None) == str(world)
    assert plan.vrt_for_zoom(8, "regional.vrt", None) == "regional.vrt"


def test_a_missing_world_dem_is_refused_not_ignored(tmp_path):
    # Silently falling back to the fresh layout would change production's
    # tiles; the flag, the plan and the min zoom all refuse instead.
    missing = str(tmp_path / "no.tif")
    with pytest.raises(FileNotFoundError):
        T.TerrainPlan(MONACO, 12, low_zoom_world_vrt=missing)
    with pytest.raises(FileNotFoundError):
        T.terrain_min_zoom(MONACO, 12, missing)
    with pytest.raises(FileNotFoundError):
        T.TerrainPlan(MONACO, 12, 0, missing)          # min zoom given too
    import create_osm_zim as coz
    with pytest.raises(SystemExit):
        coz.build_parser().parse_args(["--area", "monaco", "--low-zoom-world-vrt", missing])
    ok = tmp_path / "w.tif"
    ok.write_bytes(b"x")
    ns = coz.build_parser().parse_args(["--area", "monaco", "--low-zoom-world-vrt", str(ok)])
    assert ns.low_zoom_world_vrt == str(ok)


def test_glo30_only_under_the_regions_own_tiles():
    assert T.TerrainPlan(MONACO, 12).glo30_cells == [(43, 7)]      # was 9 cells
    assert sorted(T.TerrainPlan(LUXEMBOURG, 12).glo30_cells) == [
        (49, 5), (49, 6), (50, 5), (50, 6)]                          # was 16
    assert T.TerrainPlan(LUXEMBOURG, 9).glo30_cells == []            # z<=9: GLO-90
    # The z10 squares, not the z12 ones: here they reach into row 48.
    box = (5.0, 49.06, 5.1, 49.16)
    assert sorted(T.TerrainPlan(box, 12).glo30_cells) == [(48, 4), (48, 5), (49, 4), (49, 5)]
    assert sorted(T._cells(T._tiles_footprint(box, 12))) == [(49, 4), (49, 5)]


def test_the_generator_reads_the_low_zoom_mosaic_up_to_z9():
    plan = T.TerrainPlan(LUXEMBOURG, 12)
    assert [plan.vrt_for_zoom(z, "g", "l") for z in (6, 9, 10, 12)] == ["l", "l", "g", "g"]
    assert plan.vrt_for_zoom(9, "g", None) == "g"          # all sea at low zoom
    assert plan.vrt_for_zoom(10, None, "l") == "l"         # all sea under z10
    assert T.LOWRES_MAX_ZOOM == 9


def _covered(box, cells):
    """Every 1-degree cell `box` touches is in `cells`."""
    return all(c in cells for c in T._cells(box))


@pytest.mark.parametrize("bbox", [MONACO, LUXEMBOURG, SWITZERLAND, NETHERLANDS])
def test_every_tile_is_filled_over_its_whole_square(bbox):
    plan = T.TerrainPlan(bbox, 12)
    low, glo30 = set(plan.low_cells), set(plan.glo30_cells)
    for z in plan.zooms():
        cells = low if z <= T.LOWRES_MAX_ZOOM else glo30
        if z <= T.LOWRES_MAX_ZOOM and z < plan.low_zoom:
            continue                    # capped: checked in the next tests
        for t in mercantile.tiles(*bbox, zooms=z):
            b = mercantile.bounds(t)
            assert _covered((b.west, b.south, b.east, b.north), cells), (bbox, z, t)


def test_the_cap_counts_every_cell_the_squares_span():
    # Budget: max(64, 3 x the cells under the z10 tiles), sea included.
    assert (T.LOWZOOM_MIN_CELLS, T.LOWZOOM_CELL_BUDGET) == (64, 3)
    fr = T.TerrainPlan(FRANCE, 12)
    assert (fr.min_zoom, fr.low_zoom, len(fr.low_cells)) == (1, 5, 384)
    assert len(T._cells(T._tiles_footprint(FRANCE, 10))) == 176      # budget 528
    assert len(T._cells(T._tiles_footprint(FRANCE, 4))) == 782       # over it
    nl = T.TerrainPlan(NETHERLANDS, 12)
    assert (nl.min_zoom, nl.low_zoom, len(nl.low_cells)) == (3, 7, 49)
    assert len(T._cells(T._tiles_footprint(NETHERLANDS, 6))) == 104  # budget 72
    lux = T.TerrainPlan(LUXEMBOURG, 12)
    assert (lux.min_zoom, lux.low_zoom, len(lux.low_cells)) == (6, 6, 35)
    # The capped plan still covers the whole area itself.
    assert _covered(FRANCE, set(fr.low_cells))


def test_a_box_reaching_the_pole_is_planned_inside_web_mercator():
    for bbox in [(-10, 80, 10, 90), (-60, -90, -20, -60)]:
        plan = T.TerrainPlan(bbox, 12)
        f = T._tiles_footprint(bbox, plan.min_zoom)
        assert -85.06 < f[1] < f[3] < 85.06
        assert all(-86 <= lat <= 85 for lat, _ in plan.low_cells + plan.glo30_cells)
        assert T._bbox_tile_total(*bbox, max_zoom=10, min_zoom=plan.min_zoom) > 0


# ------------------------------------------------------ builds on a fake DEM

# z8-z10. Its z9 square lies 70% outside the one GLO-30 cell under its z10
# tiles, so a z9 tile made from the GLO-30 mosaic loses most of its land.
BBOX = "5.72,50.08,5.92,50.28"
CELL_PX = 120


def _elevation(lon, lat):
    """Land everywhere, 300-700 m, varying so the tiles are not constant."""
    return 500.0 + 150.0 * np.sin(lon * 7.0) + 50.0 * np.cos(lat * 11.0)


@pytest.fixture
def fake_dem(tmp_path, monkeypatch):
    """Stand-in for the Copernicus download: writes a synthetic cell for the
    path asked for, reports sea for the cells in `sea`, fails for those in
    `fail`, uses the constant in `level` for those listed, and records it."""
    from rasterio.transform import from_origin
    monkeypatch.setattr(T, "CACHE_DIR", str(tmp_path / "cache"))
    calls = []
    sea, fail, level = set(), set(), {}

    def download(sources, fpath, stats):
        m = re.search(r"(dem|dem90)_([NS])(\d\d)_([EW])(\d\d\d)\.tif$", fpath)
        kind, ns, lat, ew, lon = m.groups()
        lat = int(lat) * (1 if ns == "N" else -1)
        lon = int(lon) * (1 if ew == "E" else -1)
        calls.append((kind, lat, lon, [label for _, label in sources]))
        if (kind, lat, lon) in fail or (lat, lon) in fail:
            return "failed"
        if (lat, lon) in sea:
            return "sea"
        px = CELL_PX // (3 if kind == "dem90" else 1)
        xs = lon + (np.arange(px) + 0.5) / px
        ys = lat + 1 - (np.arange(px) + 0.5) / px
        data = _elevation(xs[None, :], ys[:, None]).astype("float32")
        if (lat, lon) in level:
            data[:] = level[(lat, lon)]
        with rasterio.open(fpath, "w", driver="GTiff", width=px, height=px, count=1,
                           dtype="float32", crs="EPSG:4326",
                           transform=from_origin(lon, lat + 1, 1 / px, 1 / px)) as ds:
            ds.write(data, 1)
        stats.downloaded["GLO-90" if kind == "dem90" else "GLO-30"] += 1
        return "ok"

    monkeypatch.setattr(T, "_download_dem", download)
    monkeypatch.delenv("TERRAIN_BLANK_TOLERATE", raising=False)
    monkeypatch.delenv("TERRAIN_DOWNLOAD_BUDGET_S", raising=False)
    # Set, then removed: monkeypatch restores it (unset) afterwards, so the
    # GDAL_CACHEMAX generate_terrain_tiles sets does not outlive the test.
    monkeypatch.setenv("GDAL_CACHEMAX", "0")
    monkeypatch.delenv("GDAL_CACHEMAX")
    return calls, sea, fail, level


def _elev(path):
    return T._decode_terrain(str(path))


def _plan():
    return T.TerrainPlan(T.parse_bbox(BBOX), 10)


def _build(tmp_path):
    dest = tmp_path / "terrain"
    T.generate_terrain_tiles(BBOX, str(dest), max_zoom=10)
    return dest, _plan()


def _tile(dest, z):
    t = next(iter(mercantile.tiles(*T.parse_bbox(BBOX), zooms=z)))
    return t, dest / str(z) / str(t.x) / f"{t.y}.webp"


def test_a_fresh_build_fetches_only_its_plan_and_passes_the_audit(tmp_path, fake_dem):
    calls = fake_dem[0]
    dest, plan = _build(tmp_path)
    assert (plan.min_zoom, plan.low_zoom, plan.max_zoom) == (8, 8, 10)
    assert plan.glo30_cells == [(50, 5)]
    # GLO-30 (with its GLO-90 fallback) for the cells under the z10 tiles,
    # GLO-90 alone, under its own dem90_ name, for the rest of the squares.
    got30 = {(lat, lon) for kind, lat, lon, _ in calls if kind == "dem"}
    got90 = {(lat, lon) for kind, lat, lon, _ in calls if kind == "dem90"}
    assert got30 == {(50, 5)}
    assert got90 == set(plan.low_cells) - {(50, 5)} and len(got90) == 5
    assert all(labels == ["GLO-30", "GLO-90 fallback"] for k, *_, labels in calls if k == "dem")
    assert all(labels == ["GLO-90"] for k, *_, labels in calls if k == "dem90")
    names = sorted(p for p in os.listdir(T.dem_sources_dir()) if p.endswith(".tif"))
    assert names == ["dem90_N49_E005.tif", "dem90_N49_E006.tif", "dem90_N49_E007.tif",
                     "dem90_N50_E006.tif", "dem90_N50_E007.tif", "dem_N50_E005.tif"]
    # Nothing below the viewer's zoom, and no 0 m anywhere in the squares.
    assert sorted(p for p in os.listdir(dest) if p.isdigit()) == ["10", "8", "9"]
    for z in (8, 9, 10):
        for t in mercantile.tiles(*T.parse_bbox(BBOX), zooms=z):
            assert _elev(dest / str(z) / str(t.x) / f"{t.y}.webp").min() > 200, (z, t)
    T.audit_terrain(plan, str(dest))                      # passes, strictly
    from streetzim import source_report
    assert "Copernicus DEM 1 GLO-30 + 5 GLO-90 downloaded" in source_report.summary()


def _zero(path, cols=slice(None)):
    """Rewrite a tile with 0 m over `cols` (the bbox-edge bug: everything)."""
    e = _elev(path)
    e[:, cols] = 0.0
    _write(path, e)


def _write(path, e):
    enc = np.clip(((e + 10000.0) / 0.1).astype(np.uint32), 0, 16777215)
    rgb = np.stack([(enc >> 16) & 255, (enc >> 8) & 255, enc & 255], axis=-1).astype("uint8")
    Image.fromarray(rgb).save(path, "WEBP", lossless=True)


def test_the_audit_fails_a_tile_that_lost_land(tmp_path, fake_dem, monkeypatch, capsys):
    dest, plan = _build(tmp_path)
    _, tile = _tile(dest, 9)
    _zero(tile, slice(128, None))
    assert os.path.getsize(tile) > 500          # the old size rule missed these
    with pytest.raises(RuntimeError, match="0 m over land"):
        T.audit_terrain(plan, str(dest))
    # The operator's escape hatch still works, loudly.
    monkeypatch.setenv("TERRAIN_BLANK_TOLERATE", "1")
    T.audit_terrain(plan, str(dest))
    assert "TERRAIN_BLANK_TOLERATE=1" in capsys.readouterr().out


def test_one_disagreeing_point_is_not_lost_land(tmp_path, fake_dem):
    # A coastline can put one sample point on 0 m next to land; it takes 3.
    dest, plan = _build(tmp_path)
    _, tile = _tile(dest, 9)
    e = _elev(tile)
    _zero(tile, slice(15, 36))                     # hits one column of points...
    _write(tile, np.where(np.arange(256)[:, None] < 50, _elev(tile), e))  # ...in 1 row
    T.audit_terrain(plan, str(dest))
    _zero(tile, slice(15, 36))                     # the whole column: 5 points
    with pytest.raises(RuntimeError, match="0 m over land"):
        T.audit_terrain(plan, str(dest))


def test_an_edge_tile_above_z9_is_held_to_its_mosaic(tmp_path, fake_dem):
    dest, plan = _build(tmp_path)
    t, tile = _tile(dest, 10)
    b = mercantile.bounds(t)
    lon0, _, lon1, _ = T.parse_bbox(BBOX)
    assert b.west < lon0 or b.east > lon1          # on the edge of the area
    _zero(tile, slice(128, None))
    assert os.path.getsize(tile) > 500
    with pytest.raises(RuntimeError, match="0 m over land"):
        T.audit_terrain(plan, str(dest))


def test_the_audit_fails_an_interior_blank_tile_by_the_size_rule(tmp_path, fake_dem):
    dest, plan = _build(tmp_path)
    t, tile = _tile(dest, 10)
    zero = np.zeros((256, 256, 3), "uint8")
    zero[..., 0], zero[..., 1], zero[..., 2] = 1, 134, 160      # 0 m everywhere
    Image.fromarray(zero).save(tile, "WEBP", lossless=True)
    assert os.path.getsize(tile) < 500
    dem = rasterio.open(os.path.join(T.dem_sources_dir(), f"mosaic_{plan.vrt_key}.vrt"))
    assert T._blank_over_land(str(tile), t, dem)
    with pytest.raises(RuntimeError, match="over land"):
        T.audit_terrain(plan, str(dest))
    # A missing tile is made again by the audit's repair pass.
    tile.unlink()
    T.audit_terrain(plan, str(dest))
    assert _elev(tile).min() > 200


def test_a_zeroed_tile_below_sea_level_is_caught(tmp_path, fake_dem):
    # The Caspian case: a real DEM at -30 m, a tile that says 0 m.
    level = fake_dem[3]
    for cell in [(49, 5), (49, 6), (49, 7), (50, 5), (50, 6), (50, 7)]:
        level[cell] = -30.0
    dest, plan = _build(tmp_path)
    t, tile = _tile(dest, 9)
    assert abs(_elev(tile) + 30).max() < 0.01          # quantised: -30 m, not 0
    T.audit_terrain(plan, str(dest))
    _zero(tile, slice(128, None))                  # half: the size rule misses it
    assert not T._blank_over_land(str(tile), t, rasterio.open(
        os.path.join(T.dem_sources_dir(), f"lowzoom_{plan.vrt_key}_z8.vrt")))
    with pytest.raises(RuntimeError, match="0 m over land"):
        T.audit_terrain(plan, str(dest))


def test_sea_reads_0_m_and_passes_without_tolerance(tmp_path, fake_dem):
    sea = fake_dem[1]
    # Every cell east of 6E is sea (404 from every source): the tiles there
    # are 0 m, and that is correct, not a gap to tolerate.
    sea.update((lat, lon) for lat in range(40, 60) for lon in range(6, 20))
    dest, plan = _build(tmp_path)
    assert os.path.exists(os.path.join(T.dem_sources_dir(), "dem90_N50_E006.tif.nodata"))
    T.audit_terrain(plan, str(dest))


def test_the_audit_checks_the_disk_not_the_plan(tmp_path, fake_dem, monkeypatch):
    # A plan that forgets a cell (here: the ring cell east of the region)
    # makes tiles with 0 m where that cell is; the coverage check asks the
    # disk about every cell under every square and fails the build.
    real = T.TerrainPlan

    class Forgetful(real):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.low_cells = [c for c in self.low_cells if c != (50, 7)]

    monkeypatch.setattr(T, "TerrainPlan", Forgetful)
    dest = tmp_path / "terrain"
    T.generate_terrain_tiles(BBOX, str(dest), max_zoom=10)
    with pytest.raises(RuntimeError, match=r"not fetched: 50,7"):
        T.audit_terrain(T.TerrainPlan(T.parse_bbox(BBOX), 10), str(dest))


def test_z9_is_audited_against_the_low_zoom_mosaic(tmp_path, fake_dem, monkeypatch):
    # If the generator read the GLO-30 mosaic at z9 (one cell here), the z9
    # tile would be 0 m over most of its square. The audit picks the
    # low-zoom mosaic by zoom, not through vrt_for_zoom, and catches it.
    real = T.TerrainPlan.vrt_for_zoom
    monkeypatch.setattr(T.TerrainPlan, "vrt_for_zoom",
                        lambda self, z, g, lo: g if z == 9 and g else real(self, z, g, lo))
    dest = tmp_path / "terrain"
    T.generate_terrain_tiles(BBOX, str(dest), max_zoom=10)
    with pytest.raises(RuntimeError, match="0 m over land"):
        T.audit_terrain(_plan(), str(dest))


BIG = "5.6,49.3,7.0,50.1"      # z5-z9, capped at the z7 squares, one inner z9 tile


def test_a_capped_build_passes_and_holds_inner_low_tiles_to_the_dem(tmp_path, fake_dem):
    dest = tmp_path / "terrain"
    T.generate_terrain_tiles(BIG, str(dest), max_zoom=9)
    plan = T.TerrainPlan(T.parse_bbox(BIG), 9)
    assert (plan.min_zoom, plan.low_zoom, len(plan.low_cells)) == (5, 7, 21)
    # z5-z6 squares reach past the capped mosaic: 0 m there is the plan,
    # not a gap, and the coverage check only asks for the capped part.
    assert _elev(dest / "5" / "16" / "10.webp").min() == 0
    T.audit_terrain(plan, str(dest))
    lo = T.parse_bbox(BIG)
    inner = [t for t in mercantile.tiles(*lo, zooms=9)
             if (lambda b: b.west >= lo[0] and b.east <= lo[2]
                 and b.south >= lo[1] and b.north <= lo[3])(mercantile.bounds(t))]
    assert len(inner) == 1
    tile = dest / "9" / str(inner[0].x) / f"{inner[0].y}.webp"
    _zero(tile, slice(128, None))
    with pytest.raises(RuntimeError, match="0 m over land"):
        T.audit_terrain(plan, str(dest))


# ------------------------------------------------------------- the download


def test_a_failed_glo30_cell_stops_the_build_at_once(tmp_path, fake_dem):
    calls, _, fail, _ = fake_dem
    fail.add((50, 5))
    with pytest.raises(T.DemDownloadError) as e:
        T.generate_terrain_tiles(BBOX, str(tmp_path / "terrain"), max_zoom=10)
    assert "--no-terrain" in str(e.value) and '"terrain" to "off"' in str(e.value)
    assert len(calls) == 1                         # fail fast: nothing after it
    assert not [p for p in os.listdir(T.dem_sources_dir()) if p.endswith(".nodata")]


def test_a_failed_ring_cell_stops_the_build_too(tmp_path, fake_dem):
    calls, _, fail, _ = fake_dem
    fail.add(("dem90", 49, 7))
    with pytest.raises(T.DemDownloadError, match=r"dem90_N49_E007\.tif could not"):
        T.generate_terrain_tiles(BBOX, str(tmp_path / "terrain"), max_zoom=10)
    assert not os.path.exists(os.path.join(T.dem_sources_dir(), "dem90_N49_E007.tif.nodata"))


def test_the_download_budget_stops_a_slow_build(tmp_path, fake_dem, monkeypatch):
    monkeypatch.setenv("TERRAIN_DOWNLOAD_BUDGET_S", "1e-9")
    with pytest.raises(T.DemDownloadError, match="TERRAIN_DOWNLOAD_BUDGET_S"):
        T.generate_terrain_tiles(BBOX, str(tmp_path / "terrain"), max_zoom=10)
    assert T.DEM_DOWNLOAD_BUDGET_S > 0


class _Resp(io.BytesIO):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.headers = {}
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


@pytest.mark.parametrize("code, want", [(404, "sea"), (500, "failed"), (None, "ok")])
def test_http_404_is_sea_and_anything_else_is_a_failure(tmp_path, monkeypatch, code, want):
    seen = []

    def urlopen(req, timeout):
        seen.append((req.full_url, timeout))
        if code:
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
        return _Resp(b"II*\x00" + b"\0" * 2000)

    monkeypatch.setattr(T.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(T.time, "sleep", lambda s: None)
    fpath = str(tmp_path / "dem_N50_E005.tif")
    stats = T._DemStats()
    got = T._resolve_cell(fpath, T._glo30_sources(50, 5), stats)
    assert got == {"sea": None, "failed": "failed", "ok": fpath}[want]
    assert os.path.exists(fpath + ".nodata") == (want == "sea")
    assert all(t == T.DEM_HTTP_TIMEOUT_S == 30 for _, t in seen)
    # 404: each source once. 500: three attempts per source.
    assert len(seen) == {"sea": 2, "failed": 6, "ok": 1}[want]
    assert "copernicus-dem-30m" in seen[0][0]
    # Past the budget, no further request is made at all.
    seen.clear()
    stats.deadline = 0.0
    got = T._resolve_cell(str(tmp_path / "dem_N51_E005.tif"), T._glo30_sources(51, 5), stats)
    assert seen == [] and got == "failed"


# ----------------------------------------------------- markers and wiring


def test_fresh_and_production_markers_and_caches_stay_apart(tmp_path, fake_dem):
    calls = fake_dem[0]
    dest = tmp_path / "terrain"
    dest.mkdir()
    plan = _plan()
    prod = f"COMPLETED_z10_{plan.key}"
    assert plan.marker_name == prod + "_from8_full"
    (dest / prod).write_text("1\n")                # a production build's marker
    T.generate_terrain_tiles(BBOX, str(dest), max_zoom=10)
    assert calls and (dest / plan.marker_name).is_file()
    # ...and a production build's plan does not read the fresh marker.
    world = tmp_path / "w.tif"
    world.write_bytes(b"x")
    assert T.TerrainPlan(plan.bbox, 10, low_zoom_world_vrt=str(world)).marker_name == prod
    # The fresh marker's fast path reuses the build without fetching again.
    n = len(calls)
    before = {p: os.path.getmtime(p) for p in dest.rglob("*.webp")}
    T.generate_terrain_tiles(BBOX, str(dest), max_zoom=10)
    assert len(calls) == n
    assert {p: os.path.getmtime(p) for p in dest.rglob("*.webp")} == before


def test_a_fresh_build_does_not_take_old_tiles_for_its_own(tmp_path, fake_dem):
    # A cache full of tiles made the old way (every zoom present, no fresh
    # marker) must not satisfy a fresh build: it still fetches its DEM.
    calls = fake_dem[0]
    dest = tmp_path / "terrain"
    for z in range(8, 11):
        for t in mercantile.tiles(*T.parse_bbox(BBOX), zooms=z):
            d = dest / str(z) / str(t.x)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{t.y}.webp").write_bytes(b"x" * 300)
    T.generate_terrain_tiles(BBOX, str(dest), max_zoom=10)
    assert calls


@pytest.mark.parametrize("world", [False, True])
def test_create_osm_zim_runs_the_fresh_audit_only_without_a_world_dem(
        tmp_path, monkeypatch, world):
    import create_osm_zim as coz
    ran = []
    monkeypatch.setattr(coz._terrain, "audit_terrain", lambda plan, d: ran.append(plan))
    monkeypatch.setattr(coz._terrain, "CACHE_DIR", str(tmp_path))
    wv = tmp_path / "world.tif"
    wv.write_bytes(b"x")
    args = argparse.Namespace(low_zoom_world_vrt=str(wv) if world else None)
    coz._verify_terrain(args=args, bbox_str=",".join(map(str, LUXEMBOURG)),
                        include_terrain=True, terrain_dir=str(tmp_path / "terrain"),
                        terrain_max_zoom=12)
    assert len(ran) == (0 if world else 1)
    if ran:
        assert ran[0].fresh and ran[0].min_zoom == 6


def test_across_the_antimeridian_both_sides_share_the_min_zoom(tmp_path, fake_dem,
                                                               monkeypatch):
    seen = []
    real = T.TerrainPlan

    class Spy(real):
        """Records each side's plan, and fetches nothing for it."""
        def __init__(self, bbox, max_zoom, min_zoom=None, low_zoom_world_vrt=None):
            super().__init__(bbox, max_zoom, min_zoom, low_zoom_world_vrt)
            seen.append((tuple(bbox), self.min_zoom))
            self.glo30_cells, self.low_cells = [], []

    monkeypatch.setattr(T, "TerrainPlan", Spy)
    fiji = "172.8,-23.2,-176.5,-11.2"
    assert T.generate_terrain_tiles(fiji, str(tmp_path / "terrain"), max_zoom=12) == 0
    whole = T.terrain_min_zoom(T.parse_bbox(fiji), 12)
    # One zoom for the whole area (the viewer's bounds), not one per side.
    assert [z for _, z in seen] == [whole, whole]
    assert all(bbox[2] <= 180 for bbox, _ in seen)
    assert any(T.terrain_min_zoom(bbox, 12) > whole for bbox, _ in seen)


@pytest.mark.parametrize("world", [False, True])
def test_map_config_says_where_terrain_starts(tmp_path, world):
    import create_osm_zim as coz
    (tmp_path / "ter").mkdir()
    wv = tmp_path / "world.tif"
    wv.write_bytes(b"x")
    args = argparse.Namespace(map_center=None, map_zoom=None, max_zoom=14,
                              low_zoom_world_vrt=str(wv) if world else None)
    _, cfg = coz._build_map_config(
        args=args, bbox_str="5.732,49.444,6.535,50.187", name="Luxembourg",
        overture_sources=None, routing_graph_path=None, satellite_dir=None,
        satellite_format="webp", satellite_max_zoom=None, satellite_tile_size=256,
        search_features=[], terrain_dir=str(tmp_path / "ter"), terrain_max_zoom=12,
        total_steps=1, wiki_cross_refs=None, wikidata_data=None)
    assert cfg["hasTerrain"] and cfg["terrainMaxZoom"] == 12
    if world:
        assert "terrainMinZoom" not in cfg     # production map-configs unchanged
    else:
        assert cfg["terrainMinZoom"] == 6


def test_no_tile_is_drawn_in_the_builds_own_process(tmp_path, fake_dem):
    """Even a zoom of one tile goes to a pool's process: at low zoom a tile
    reads the whole area's DEM, and this process would keep that memory for
    the rest of the build (China's z0-z4 left 3.7 GB, 2026-10-03)."""
    T._DEM_HANDLES.clear()
    dest, _ = _build(tmp_path)
    assert (dest / "0").is_dir() or any(dest.iterdir())
    assert T._DEM_HANDLES == {}


def test_the_gdal_cache_is_capped_unless_the_operator_set_it(tmp_path, fake_dem, monkeypatch):
    _build(tmp_path)
    assert os.environ["GDAL_CACHEMAX"] == str(T.TERRAIN_GDAL_CACHE_MB)
    monkeypatch.setenv("GDAL_CACHEMAX", "1024")
    T.generate_terrain_tiles(BBOX, str(tmp_path / "again"), max_zoom=10)
    assert os.environ["GDAL_CACHEMAX"] == "1024"
