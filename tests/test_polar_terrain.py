"""streetzim/polar_terrain.py: the terrain of a polar map, read from DEM
cells at the real place each pixel of a map tile stands for."""
import os

import pytest

np = pytest.importorskip("numpy")
rasterio = pytest.importorskip("rasterio")

from streetzim import polar, terrain  # noqa: E402
from streetzim import polar_terrain as pt  # noqa: E402

ROWS = 12


def _cols(lat0):
    """Fewer columns towards the pole, as in the real cells."""
    return 12 if lat0 >= -70 else 8 if lat0 >= -80 else 4


def _height(lon, lat):
    """A slope that is the same on both sides of 180: bilinear reading of
    its samples gives it back exactly."""
    return 3000.0 + 40.0 * (lat + 90.0) + 2.0 * np.abs(lon)


def _cell(dem_dir, lat0, lon0, height=_height):
    """A cell as Copernicus lays it out: named for its south-west corner,
    rows from lat0 + 1 down, columns from lon0 east."""
    cols = _cols(lat0)
    lat = lat0 + 1 - np.arange(ROWS) / ROWS
    lon = lon0 + np.arange(cols) / cols
    data = height(*np.meshgrid(lon, lat)).astype("float32")
    path = terrain._glo30_path(str(dem_dir), lat0, lon0)
    from rasterio.transform import from_origin
    with rasterio.open(path, "w", driver="GTiff", width=cols, height=ROWS, count=1,
                       dtype="float32", crs="EPSG:4326",
                       transform=from_origin(lon0 - 0.5 / cols, lat0 + 1 + 0.5 / ROWS,
                                             1 / cols, 1 / ROWS)) as dst:
        dst.write(data, 1)


def _sea(dem_dir, lat0, lon0):
    open(terrain._glo30_path(str(dem_dir), lat0, lon0) + ".nodata", "w").close()


def _dem(dem_dir, land, lats=None, lons=None, height=_height):
    for lat0 in lats or range(-90, -60):
        for lon0 in lons or range(-180, 180):
            if land(lat0, lon0):
                _cell(dem_dir, lat0, lon0, height)
            else:
                _sea(dem_dir, lat0, lon0)


def test_the_array_projection_is_the_scalar_one():
    pts = [(0.0, -90.0), (0.0, -60.0), (90.0, -75.0), (-135.0, -88.5), (179.9, -61.0), (-0.1, -89.99)]
    x, y = zip(*(polar.to_polar(lon, lat) for lon, lat in pts))
    lon, lat = pt.from_polar(np.array(x), np.array(y))
    for i, (x_i, y_i) in enumerate(zip(x, y)):
        want = polar.from_polar(x_i, y_i)
        assert (lon[i], lat[i]) == pytest.approx(want, abs=1e-9)


def test_a_tiles_pixels_are_where_the_map_draws_them():
    import mercantile
    z, x, y = 5, 17, 14
    lon, lat = pt.tile_true(z, x, y)
    assert lon.shape == lat.shape == (256, 256)
    b = mercantile.bounds(x, y, z)
    for row, col in [(0, 0), (0, 255), (255, 0), (128, 77)]:
        m_lon, m_lat = polar.to_map(lon[row, col], lat[row, col])
        assert m_lon == pytest.approx(b.west + (col + 0.5) * (b.east - b.west) / 256, abs=1e-9)
        # Rows are even in Mercator metres, not in degrees.
        top, bottom = mercantile.xy(0, b.north)[1], mercantile.xy(0, b.south)[1]
        assert mercantile.xy(0, m_lat)[1] == pytest.approx(top + (row + 0.5) * (bottom - top) / 256, abs=1e-3)
    # Four points in sixteen show the same places.
    lon4, lat4 = pt.tile_true(z, x, y, n=4)
    assert polar.to_map(lon4[1, 2], lat4[1, 2]) == pytest.approx(
        (b.west + 2.5 * (b.east - b.west) / 4, polar.to_map(lon[96, 160], lat[95, 160])[1]), abs=0.2)


def test_the_tile_range_holds_the_edge_circle_and_no_more():
    for z in (0, 1, 5, 9):
        x0, y0, x1, y1 = pt.tile_range(z)
        assert (x0, x1) == (y0, y1)
        size = pt.WORLD / (1 << z)
        rho = polar.to_polar(0.0, polar.EDGE_LAT)[1]
        assert x0 * size <= pt.WORLD / 2 - rho < (x0 + 1) * size
        assert x1 * size <= pt.WORLD / 2 + rho < (x1 + 1) * size
    assert pt.tile_range(0) == (0, 0, 0, 0)
    assert pt.tile_range(1) == (0, 0, 1, 1)


def test_elevation_is_read_between_the_four_samples_around_a_place(tmp_path):
    _dem(tmp_path, lambda lat0, lon0: True, lats=range(-90, -60), lons=[-180, -179, 10, 11, 12, 178, 179])
    cells = pt.Cells(str(tmp_path))
    pts = [
        (10.5, -65.5), (10.01, -65.99),
        (10.999, -65.3),            # past the last column: the cell to the east
        (11.3, -65.999),            # under the last row: the cell to the south
        (10.999, -65.999),          # both
        (10.97, -70.99),            # the cell to the south has fewer columns
        (11.4, -80.995), (10.2, -85.5),
        (179.99, -75.5), (-179.99, -75.5), (-180.0, -75.5),   # across 180
        (10.5, -60.0),
    ]
    lon, lat = (np.array(v) for v in zip(*pts))
    got = pt.elevation(lon, lat, cells)
    assert got == pytest.approx(_height(lon, lat), abs=0.01)
    # Under the last row of the last cell there is nothing: that row again.
    last = _height(11.5, -90.0 + 1 / ROWS)
    assert list(pt.elevation(np.array([11.5, 11.5]), np.array([-89.99, -90.0]), cells)) == [
        pytest.approx(last, abs=0.01)] * 2
    # 180 itself is the western edge.
    assert pt.elevation(np.array([180.0]), np.array([-75.5]), cells)[0] == pytest.approx(
        _height(180.0, -75.5), abs=0.01)


def test_sea_and_everything_north_of_the_edge_is_zero(tmp_path):
    _dem(tmp_path, lambda lat0, lon0: lon0 == 10, lats=[-66, -65, -62, -61], lons=[9, 10, 11])
    cells = pt.Cells(str(tmp_path))
    lon = np.array([9.5, 11.5, 10.5, 10.5, 10.5])
    lat = np.array([-64.5, -64.5, -64.5, -59.999, -60.5])
    got = pt.elevation(lon, lat, cells)
    assert list(got[:2]) == [0.0, 0.0]
    assert got[2] == pytest.approx(_height(10.5, -64.5), abs=0.01)
    assert got[3] == 0.0
    assert got[4] == pytest.approx(_height(10.5, -60.5), abs=0.01)
    # Beside the sea a cell's last column falls to 0, as the coast does.
    edge = pt.elevation(np.array([10.0 + 11.5 / 12]), np.array([-64.5]), cells)[0]
    assert edge == pytest.approx(_height(10.0 + 11 / 12, -64.5) / 2, abs=0.01)


def test_a_cell_that_is_neither_there_nor_marked_sea_stops_the_run(tmp_path):
    _cell(tmp_path, -65, 10)
    _sea(tmp_path, -66, 10)
    _sea(tmp_path, -66, 11)
    cells = pt.Cells(str(tmp_path))
    with pytest.raises(pt.MissingCell, match="dem_S65_E011"):
        pt.elevation(np.array([10.5]), np.array([-64.5]), cells)
    with pytest.raises(pt.MissingCell, match="dem_S70_W020"):
        cells.is_land(-70, -20)


def test_cells_are_dropped_once_they_pass_the_budget(tmp_path):
    _dem(tmp_path, lambda lat0, lon0: True, lats=[-66, -65], lons=range(10, 16))
    one = (ROWS + 1) * 13 * 4
    cells = pt.Cells(str(tmp_path), budget=2 * one)
    for lon0 in (10, 11, 12, 10, 13):
        cells.get(-65, lon0)
    assert list(cells._kept) == [(-65, 10), (-65, 13)]
    assert cells._bytes == 2 * one
    assert cells.get(-65, 10) is cells.get(-65, 10)


def _land(lat0, lon0):
    return lat0 < -86 or (0 <= lon0 < 40 and lat0 < -66)


@pytest.fixture(scope="module")
def dem(tmp_path_factory):
    """A whole DEM folder: land around the pole and in one sector."""
    d = tmp_path_factory.mktemp("dem")
    _dem(d, _land)
    return d


def test_build_writes_the_top_zoom_from_the_dem_and_the_rest_from_it(tmp_path, dem):
    out = tmp_path / "terrain"
    said = []
    z = 5
    counts = pt.build(str(out), z, str(dem), say=said.append)
    assert sorted(counts) == [0, 1, 2, 3, 4, 5]
    assert counts[0] == 1 and counts[1] == 4
    cells = pt.Cells(str(dem))
    x0, y0, x1, y1 = pt.tile_range(z)
    there = set(pt._tiles_of(str(out), z))
    assert len(there) == counts[z]
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            want = terrain.quantize_terrain(pt.tile_elevation(z, x, y, cells))
            if (x, y) in there:
                got = terrain._decode_terrain(str(out / str(z) / str(x) / f"{y}.webp"))
                assert np.abs(got - want).max() < 0.06, (x, y)
            else:
                assert not want.any(), (x, y)
    # The pole is the corner of four tiles: this one's first pixel is just
    # beside it, towards 135 E.
    pole = terrain._decode_terrain(str(out / "5" / "16" / "16.webp"))
    assert pole[0, 0] == pytest.approx(_height(135.0, -90.0), abs=10.0)
    # Sea: the tile at 100 E, 62 S.
    mx, my = polar.to_polar(100.0, -62.0)
    size = pt.WORLD / 32
    assert (int((mx + pt.WORLD / 2) // size), int((pt.WORLD / 2 - my) // size)) not in there
    # A tile below the top is the mean of the four under it.
    px, py = 8, 8
    big = np.zeros((512, 512))
    for dx in (0, 1):
        for dy in (0, 1):
            p = out / "5" / str(2 * px + dx) / f"{2 * py + dy}.webp"
            if p.exists():
                big[dy * 256:(dy + 1) * 256, dx * 256:(dx + 1) * 256] = terrain._decode_terrain(str(p))
    want = terrain.quantize_terrain(big.reshape(256, 2, 256, 2).mean(axis=(1, 3)))
    assert np.abs(terrain._decode_terrain(str(out / "4" / "8" / "8.webp")) - want).max() < 0.06
    assert big.any()
    assert any("z5" in line for line in said)


def test_a_stopped_build_goes_on_where_it_was(tmp_path, dem, monkeypatch):
    out = tmp_path / "terrain"
    monkeypatch.setattr(pt, "BLOCK", 4)
    z = 5
    first = pt.build(str(out), z, str(dem), say=lambda _m: None)
    marks = sorted(os.listdir(out / ".squares"))
    x0, _, x1, _ = pt.tile_range(z)
    assert len(marks) == (x1 // 4 - x0 // 4 + 1) ** 2
    # One finished square loses a tile and keeps its mark; another loses
    # its mark too.
    kept = out / "5" / "16" / "16.webp"
    redone = out / "5" / "15" / "15.webp"
    kept.unlink()
    redone.unlink()
    os.unlink(out / ".squares" / "5-3-3")
    made = []
    real = pt._block
    monkeypatch.setattr(pt, "_block", lambda a: (made.append(a[3:5]), real(a))[1])
    again = pt.build(str(out), z, str(dem), say=lambda _m: None)
    assert made == [(3, 3)]
    assert redone.exists() and not kept.exists()
    assert again[z] == first[z] - 1


def test_build_with_workers_gives_the_same_tiles(tmp_path, dem):
    one = pt.build(str(tmp_path / "a"), 4, str(dem), say=lambda _m: None)
    two = pt.build(str(tmp_path / "b"), 4, str(dem), workers=2, say=lambda _m: None)
    assert one == two
    for x, y in pt._tiles_of(str(tmp_path / "a"), 4):
        rel = os.path.join("4", str(x), f"{y}.webp")
        with open(tmp_path / "a" / rel, "rb") as fa, open(tmp_path / "b" / rel, "rb") as fb:
            assert fa.read() == fb.read()


def test_made_from_is_the_zoom_and_the_cells_south_of_60(tmp_path):
    d = tmp_path / "dem"
    d.mkdir()
    _sea(d, -61, 0)
    _sea(d, -75, -120)
    (d / "dem_S90_E000.tif").write_bytes(b"cell")
    first = pt.made_from(str(d), 12)
    assert first == pt.made_from(str(d), 12)
    assert first != pt.made_from(str(d), 11)
    # Cells of the rest of the world, and the cache's other files, are not read.
    _sea(d, -60, 0)
    _sea(d, 10, 10)
    (d / "comprehensive.vrt").write_text("x")
    (d / "dem_S70_E000.tif.aux.xml").write_text("x")
    assert pt.made_from(str(d), 12) == first
    (d / "dem_S90_E000.tif").write_bytes(b"another cell")
    second = pt.made_from(str(d), 12)
    assert second != first
    os.utime(d / "dem_S61_E000.tif.nodata", ns=(1, 1))
    third = pt.made_from(str(d), 12)
    assert third != second
    _sea(d, -61, 1)
    assert pt.made_from(str(d), 12) != third


def test_tiles_made_from_other_cells_are_not_kept(tmp_path):
    d = tmp_path / "dem"
    d.mkdir()
    _dem(d, lambda lat0, lon0: lat0 < -84)
    out = tmp_path / "terrain"
    counts = pt.build(str(out), 3, str(d), say=lambda _m: None)
    assert counts[3] == 4
    kept = out / ".squares" / "kept"
    kept.write_text("")
    other = out / "notes.txt"
    other.write_text("not a tile")
    assert pt.build(str(out), 3, str(d), say=lambda _m: None) == counts
    assert kept.exists()
    # The cap of land grows by a row of cells: the old tiles are of no use.
    # A tile that no run would make again must go as well.
    stale = out / "3" / "0" / "0.webp"
    stale.parent.mkdir()
    stale.write_bytes(b"old")
    for lon0 in range(-180, 180):
        os.unlink(terrain._glo30_path(str(d), -84, lon0) + ".nodata")
        _cell(d, -84, lon0)
    again = pt.build(str(out), 3, str(d), say=lambda _m: None)
    assert not kept.exists() and other.exists() and not stale.exists()
    assert again[3] == 4
    cells = pt.Cells(str(d))
    for x, y in pt._tiles_of(str(out), 3):
        want = terrain.quantize_terrain(pt.tile_elevation(3, x, y, cells))
        got = terrain._decode_terrain(str(out / "3" / str(x) / f"{y}.webp"))
        assert np.abs(got - want).max() < 0.06, (x, y)
    # Another zoom is another set of tiles too.
    kept.write_text("")
    pt.build(str(out), 2, str(d), say=lambda _m: None)
    assert not kept.exists() and not (out / "3").exists()


def _zim_args(*extra):
    import create_osm_zim
    return create_osm_zim.build_parser().parse_args(list(extra))


def _take_terrain(args, zoom=3):
    import create_osm_zim
    return create_osm_zim._satellite_and_terrain(
        args=args, bbox_str="-31,-30,31,30", include_routing=False, include_satellite=False,
        include_terrain=args.terrain, include_wikidata=False, satellite_download_zoom=None,
        satellite_format="avif", satellite_quality=None, satellite_tile_size=256,
        terrain_max_zoom=zoom, total_steps=5)


def test_a_polar_build_takes_its_terrain_tiles_as_they_are(tmp_path, monkeypatch, capsys):
    import create_osm_zim
    made = []
    monkeypatch.setattr(create_osm_zim, "generate_terrain_tiles",
                        lambda *a, **k: made.append(a))
    tiles = tmp_path / "terrain"
    (tiles / "3" / "4").mkdir(parents=True)
    (tiles / "3" / "4" / "4.webp").write_bytes(b"tile")
    args = _zim_args("--projection", polar.NAME, "--terrain", "--terrain-dir", str(tiles))
    assert _take_terrain(args) == (None, str(tiles))
    assert not made
    assert str(tiles) in capsys.readouterr().out
    # The folder has to hold the zoom the map will say it has.
    with pytest.raises(RuntimeError, match="zoom 4"):
        _take_terrain(args, zoom=4)
    (tiles / "4").mkdir()
    with pytest.raises(RuntimeError, match="zoom 4"):
        _take_terrain(args, zoom=4)
    # Without terrain there is nothing to take.
    assert _take_terrain(_zim_args("--projection", polar.NAME)) == (None, None)


@pytest.mark.parametrize("extra, said", [
    (["--terrain"], "--terrain-dir"),
    (["--terrain", "--terrain-zoom", "9"], "tools/polar_data.py terrain"),
])
def test_a_polar_build_refuses_what_it_cannot_make(extra, said, capsys):
    import create_osm_zim
    with pytest.raises(SystemExit) as e:
        create_osm_zim.main(["--mbtiles", "none.mbtiles", "--bbox=-31,-30,31,30",
                             "--projection", polar.NAME, "--name", "x", *extra])
    assert e.value.code == 2
    assert said in capsys.readouterr().err
