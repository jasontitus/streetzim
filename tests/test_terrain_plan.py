"""Terrain without a world DEM (every `streetzim` build, every fresh Zimfarm
task): which zooms are made, which DEM cells each needs, and the audit that
checks every tile against the mosaic it was made from.

The builds here run on synthetic DEM cells written by a stand-in for the
download, so nothing touches the network."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

rasterio = pytest.importorskip("rasterio")
mercantile = pytest.importorskip("mercantile")
np = pytest.importorskip("numpy")
from PIL import Image  # noqa: E402

from streetzim import terrain as T  # noqa: E402

MONACO = (7.40, 43.72, 7.44, 43.76)
LUXEMBOURG = (5.732, 49.444, 6.535, 50.187)
SWITZERLAND = (5.935, 45.8, 10.51, 47.83)
NETHERLANDS = (3.04, 50.71, 7.27, 53.77)
FRANCE = (-5.2, 41.3, 9.6, 51.1)


# ------------------------------------------------------------------ the plan


def test_min_zoom_is_the_lowest_the_viewer_can_show():
    # The viewer pins the view to the bbox; on a 320-px viewport Monaco
    # never shows below map zoom 12.5, Luxembourg below 8.1.
    assert 12 < T.viewer_min_map_zoom(MONACO) < 13
    assert 8 < T.viewer_min_map_zoom(LUXEMBOURG) < 8.5
    # One level for 3D terrain (it reads DEM tiles at the map zoom, the
    # hillshade one above) and one for the far side of a tilted view.
    assert T.terrain_min_zoom(MONACO, 12) == 11
    assert T.terrain_min_zoom(LUXEMBOURG, 12) == 7
    assert T.terrain_min_zoom(MONACO, 10) == 10          # never above max zoom
    assert T.terrain_min_zoom((-180, -85, 180, 85), 12) == 0


def test_a_world_dem_keeps_the_production_layout(tmp_path):
    world = tmp_path / "world.tif"
    world.write_bytes(b"x")
    plan = T.TerrainPlan(MONACO, 12, low_zoom_world_vrt=str(world))
    assert not plan.fresh and plan.min_zoom == 0 and plan.low_cells == []
    # bbox + 1 degree of GLO-30, as before.
    assert sorted(plan.glo30_cells) == [(lat, lon) for lat in (42, 43, 44) for lon in (6, 7, 8)]
    assert plan.marker_name == "COMPLETED_z12_7.4_43.7_7.4_43.8"
    assert plan.vrt_for_zoom(7, "regional.vrt", None) == str(world)
    assert plan.vrt_for_zoom(8, "regional.vrt", None) == "regional.vrt"
    # A world DEM that is not there: the fresh layout, not a silent fallback.
    assert T.TerrainPlan(MONACO, 12, low_zoom_world_vrt=str(tmp_path / "no.tif")).fresh


def test_glo30_only_under_the_regions_own_tiles():
    assert T.TerrainPlan(MONACO, 12).glo30_cells == [(43, 7)]      # was 9 cells
    assert sorted(T.TerrainPlan(LUXEMBOURG, 12).glo30_cells) == [
        (49, 5), (49, 6), (50, 5), (50, 6)]                          # was 16
    assert T.TerrainPlan(LUXEMBOURG, 9).glo30_cells == []            # z<=9: GLO-90


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
            continue                    # capped: checked in the next test
        for t in mercantile.tiles(*bbox, zooms=z):
            b = mercantile.bounds(t)
            assert _covered((b.west, b.south, b.east, b.north), cells), (bbox, z, t)


def test_the_low_zoom_ring_is_capped_for_large_areas():
    plan = T.TerrainPlan(FRANCE, 12)
    core = len(T._cells(T._tiles_footprint(FRANCE, T.LOWRES_MAX_ZOOM + 1)))
    budget = max(T.LOWZOOM_MIN_CELLS, T.LOWZOOM_CELL_BUDGET * core)
    assert len(plan.low_cells) <= budget
    assert plan.min_zoom < plan.low_zoom <= T.LOWRES_MAX_ZOOM
    # Below the capped zoom the tiles are still made (the viewer can reach
    # them) and still cover the whole region itself.
    assert _covered(FRANCE, set(plan.low_cells))
    # Small regions are never capped: Luxembourg's z7 square is 12 cells.
    lux = T.TerrainPlan(LUXEMBOURG, 12)
    assert lux.low_zoom == lux.min_zoom == 7 and len(lux.low_cells) == 12


# ------------------------------------------------------ builds on a fake DEM

BBOX = "6.0,49.5,6.2,49.7"          # z9-z10: one low-zoom and one GLO-30 zoom
CELL_PX = 120


def _elevation(lon, lat):
    """Land everywhere, 300-700 m, varying so the tiles are not constant."""
    return 500.0 + 150.0 * np.sin(lon * 7.0) + 50.0 * np.cos(lat * 11.0)


@pytest.fixture
def fake_dem(tmp_path, monkeypatch):
    """Stand-in for the Copernicus download: writes a synthetic cell for the
    path asked for (or reports sea for the cells in `sea`), and records it."""
    from rasterio.transform import from_origin
    monkeypatch.setattr(T, "CACHE_DIR", str(tmp_path / "cache"))
    calls = []
    sea = set()

    def download(sources, fpath, stats):
        m = re.search(r"(dem|dem90)_([NS])(\d\d)_([EW])(\d\d\d)\.tif$", fpath)
        kind, ns, lat, ew, lon = m.groups()
        lat = int(lat) * (1 if ns == "N" else -1)
        lon = int(lon) * (1 if ew == "E" else -1)
        calls.append((kind, lat, lon, [label for _, label in sources]))
        if (lat, lon) in sea:
            return "sea"
        px = CELL_PX // (3 if kind == "dem90" else 1)
        xs = lon + (np.arange(px) + 0.5) / px
        ys = lat + 1 - (np.arange(px) + 0.5) / px
        data = _elevation(xs[None, :], ys[:, None]).astype("float32")
        with rasterio.open(fpath, "w", driver="GTiff", width=px, height=px, count=1,
                           dtype="float32", crs="EPSG:4326",
                           transform=from_origin(lon, lat + 1, 1 / px, 1 / px)) as ds:
            ds.write(data, 1)
        return "ok"

    monkeypatch.setattr(T, "_download_dem", download)
    monkeypatch.delenv("TERRAIN_BLANK_TOLERATE", raising=False)
    return calls, sea


def _elev(path):
    return T._decode_terrain(str(path))


def _build(tmp_path):
    dest = tmp_path / "terrain"
    T.generate_terrain_tiles(BBOX, str(dest), max_zoom=10)
    plan = T.TerrainPlan(T.parse_bbox(BBOX), 10)
    return dest, plan


def test_a_fresh_build_fetches_only_its_plan_and_passes_the_audit(tmp_path, fake_dem):
    calls, _ = fake_dem
    dest, plan = _build(tmp_path)
    assert (plan.min_zoom, plan.max_zoom) == (9, 10)
    # GLO-30 (with its GLO-90 fallback) for the cells under the z10 tiles,
    # GLO-90 alone, under its own name, for the rest of the z9 squares.
    got30 = {(lat, lon) for kind, lat, lon, _ in calls if kind == "dem"}
    got90 = {(lat, lon) for kind, lat, lon, _ in calls if kind == "dem90"}
    assert got30 == set(plan.glo30_cells)
    assert got90 == set(plan.low_cells) - set(plan.glo30_cells)
    assert all(labels == ["GLO-90"] for kind, *_, labels in calls if kind == "dem90")
    # Nothing below the viewer's zoom, and no 0 m anywhere in the squares.
    assert sorted(p for p in os.listdir(dest) if p.isdigit()) == ["10", "9"]
    for z in (9, 10):
        for t in mercantile.tiles(*T.parse_bbox(BBOX), zooms=z):
            e = _elev(dest / str(z) / str(t.x) / f"{t.y}.webp")
            assert e.min() > 200, (z, t)
    T.audit_terrain(plan, str(dest))                      # passes, strictly


def _stripe(path):
    """Rewrite a tile as the bbox-edge bug left it: real elevation on the
    left, 0 m on the right."""
    e = _elev(path)
    e[:, 128:] = 0.0
    enc = np.clip(((e + 10000.0) / 0.1).astype(np.uint32), 0, 16777215)
    rgb = np.stack([(enc >> 16) & 255, (enc >> 8) & 255, enc & 255], axis=-1).astype("uint8")
    Image.fromarray(rgb).save(path, "WEBP", lossless=True)


def test_the_audit_fails_a_tile_that_lost_land(tmp_path, fake_dem, monkeypatch, capsys):
    dest, plan = _build(tmp_path)
    t = next(iter(mercantile.tiles(*T.parse_bbox(BBOX), zooms=9)))
    tile = dest / "9" / str(t.x) / f"{t.y}.webp"
    _stripe(tile)
    assert os.path.getsize(tile) > 500          # the old size rule missed these
    with pytest.raises(RuntimeError, match="0 m over land"):
        T.audit_terrain(plan, str(dest))
    # The operator's escape hatch still works, loudly.
    monkeypatch.setenv("TERRAIN_BLANK_TOLERATE", "1")
    T.audit_terrain(plan, str(dest))
    assert "TERRAIN_BLANK_TOLERATE=1" in capsys.readouterr().out


def test_the_audit_fails_a_blank_and_a_missing_tile(tmp_path, fake_dem):
    dest, plan = _build(tmp_path)
    t = next(iter(mercantile.tiles(*T.parse_bbox(BBOX), zooms=10)))
    tile = dest / "10" / str(t.x) / f"{t.y}.webp"
    zero = np.full((256, 256, 3), 0, "uint8")
    zero[..., 0], zero[..., 1], zero[..., 2] = 1, 134, 160      # 0 m
    Image.fromarray(zero).save(tile, "WEBP", lossless=True)
    with pytest.raises(RuntimeError, match="over land"):
        T.audit_terrain(plan, str(dest))
    # A missing tile is made again by the audit's repair pass.
    tile.unlink()
    T.audit_terrain(plan, str(dest))
    assert _elev(tile).min() > 200


def test_sea_reads_0_m_and_passes_without_tolerance(tmp_path, fake_dem):
    calls, sea = fake_dem
    # Every cell east of 6E is sea (404 from every source): the tiles there
    # are 0 m, and that is correct, not a gap to tolerate.
    sea.update((lat, lon) for lat in range(40, 60) for lon in range(6, 20))
    dest, plan = _build(tmp_path)
    assert any(os.path.exists(os.path.join(T.dem_sources_dir(), f"dem_N49_E{lon:03d}.tif.nodata"))
               for lon in range(6, 8))
    T.audit_terrain(plan, str(dest))


def test_a_transient_failure_is_never_taken_for_sea(tmp_path, fake_dem, monkeypatch):
    monkeypatch.setattr(T, "_download_dem", lambda sources, fpath, stats: "failed")
    with pytest.raises(RuntimeError, match="could not be downloaded"):
        T.generate_terrain_tiles(BBOX, str(tmp_path / "terrain"), max_zoom=10)
    assert not [p for p in os.listdir(T.dem_sources_dir()) if p.endswith(".nodata")]


def test_marker_fast_path_reuses_a_complete_build(tmp_path, fake_dem):
    calls, _ = fake_dem
    dest, plan = _build(tmp_path)
    assert (dest / plan.marker_name).is_file()
    n = len(calls)
    before = {p: os.path.getmtime(p) for p in dest.rglob("*.webp")}
    T.generate_terrain_tiles(BBOX, str(dest), max_zoom=10)
    assert len(calls) == n                       # nothing fetched again
    assert {p: os.path.getmtime(p) for p in dest.rglob("*.webp")} == before


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
    import argparse
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
        assert cfg["terrainMinZoom"] == 7
