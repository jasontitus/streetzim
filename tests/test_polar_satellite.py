"""streetzim/polar_satellite.py: a polar map's imagery, cut from a picture
that is already in the map's positions."""
import io
import os
import sys

import pytest

np = pytest.importorskip("numpy")
PIL = pytest.importorskip("PIL")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image, features  # noqa: E402

from streetzim import polar, polar_terrain, satellite_sources  # noqa: E402
from streetzim import polar_satellite as ps  # noqa: E402

BOX = (-31.0, -30.0, 31.0, 30.0)
# The four tiles of a small picture, by (row, col): north-west of the pole
# red, north-east green, south-west blue, south-east yellow.
COLOURS = {(0, 0): (200, 30, 30), (0, 1): (30, 160, 30),
           (1, 0): (30, 30, 200), (1, 1): (200, 200, 30)}


def _avif():
    if features.check("avif"):
        return True
    try:
        import pillow_avif  # noqa: F401
    except ImportError:
        return False
    return True


needs_avif = pytest.mark.skipif(not _avif(), reason="no AVIF codec")


def _jpeg(colour, size=ps.SOURCE_TILE):
    out = io.BytesIO()
    Image.new("RGB", (size, size), colour).save(out, "JPEG", quality=95)
    return out.getvalue()


@pytest.fixture
def small(monkeypatch, tmp_path):
    """A picture of 2 x 2 source tiles over the same ground, in a cache."""
    monkeypatch.setattr(ps, "SOURCE_TILES", 2)
    monkeypatch.setattr(ps, "SOURCE_PIXELS", 2 * ps.SOURCE_TILE)
    monkeypatch.setattr(ps, "METRES_PER_PIXEL", ps.HALF / ps.SOURCE_TILE)
    monkeypatch.setattr(ps, "WORLD_METRES_PER_PIXEL", 16 * ps.HALF / ps.SOURCE_TILE)
    cache = tmp_path / "cache"
    for (row, col), colour in COLOURS.items():
        path = ps.source_path(str(cache), row, col)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(_jpeg(colour))
    return str(cache)


def _near(pixel, colour, by=40):
    return all(abs(int(a) - b) <= by for a, b in zip(pixel, colour))


def _tile(folder, z, x, y):
    return np.asarray(Image.open(os.path.join(folder, str(z), str(x), f"{y}.avif")).convert("RGB"))


def test_the_picture_is_the_one_gibs_serves_around_the_pole():
    assert ps.SOURCE is satellite_sources.POLAR
    assert ps.SOURCE_TILES == 32 and ps.SOURCE_PIXELS == 16384
    assert ps.METRES_PER_PIXEL == 512.0
    # Zoom 9 is the first zoom finer than the picture: none of it is lost.
    metres = [polar_terrain.WORLD / (1 << z) / ps.TILE for z in (ps.TOP_ZOOM - 1, ps.TOP_ZOOM)]
    assert metres[0] > ps.METRES_PER_PIXEL > metres[1]
    assert ps.source_path("c", 3, 7) == os.path.join("c", "4", "3", "7.jpeg")
    # The picture reaches past the 60 S circle on every side.
    edge = max(abs(v) for p in polar.edge_ring() for v in polar.lonlat_to_merc(*p))
    assert edge < ps.HALF


@pytest.mark.parametrize("z", range(10))
def test_the_tiles_of_a_zoom_are_the_ones_the_validator_counts(z):
    from cloud import validate_zim
    x0, y0, x1, y1 = ps.tile_range(z, BOX)
    assert (x1 - x0 + 1) * (y1 - y0 + 1) == validate_zim._expected_tile_count(BOX, z)
    n = 1 << z
    assert 0 <= x0 <= x1 < n and 0 <= y0 <= y1 < n


def test_the_tiles_of_a_box_off_the_centre():
    assert ps.tile_range(4, BOX) == (6, 6, 9, 9)
    assert ps.tile_range(0, BOX) == (0, 0, 0, 0)
    # West and north come first: x grows east, y grows south.
    assert ps.tile_range(3, (-100.0, 10.0, -50.0, 60.0)) == (1, 2, 2, 3)
    assert ps.tile_range(2, (-180.0, -89.0, 180.0, 89.0)) == (0, 0, 3, 3)


def test_a_tile_is_a_piece_of_the_picture_around_the_pole():
    # The tile south-east of the pole starts at the picture's centre.
    size = polar_terrain.WORLD / 32 / ps.METRES_PER_PIXEL
    assert ps.tile_box(5, 16, 16, ps.HALF, ps.METRES_PER_PIXEL) == pytest.approx(
        (8192, 8192, 8192 + size, 8192 + size))
    # North is up and west is left.
    assert ps.tile_box(5, 15, 14, ps.HALF, ps.METRES_PER_PIXEL) == pytest.approx(
        (8192 - size, 8192 - 2 * size, 8192, 8192 - size))
    # The one tile of zoom 0 is the whole of a picture of the map square.
    half = polar_terrain.WORLD / 2
    assert ps.tile_box(0, 0, 0, half, polar_terrain.WORLD / 256) == pytest.approx((0, 0, 256, 256))


def test_fetch_gets_what_is_missing_and_only_that(small, tmp_path):
    cache = str(tmp_path / "new")
    asked = []

    def get(url):
        asked.append(url)
        return _jpeg((9, 9, 9))

    said = []
    assert ps.fetch(cache, get=get, say=said.append) == 4
    base = satellite_sources.POLAR.url.split("{z}")[0]
    # Row then column, as GIBS names them.
    assert sorted(asked) == [f"{base}4/{r}/{c}.jpeg" for r in (0, 1) for c in (0, 1)]
    assert "4 source tiles" in said[0]
    for r in (0, 1):
        for c in (0, 1):
            with open(ps.source_path(cache, r, c), "rb") as f:
                assert f.read() == _jpeg((9, 9, 9))
    assert not [n for _d, _s, names in os.walk(cache) for n in names if n.endswith(".part")]
    os.unlink(ps.source_path(cache, 1, 0))
    del asked[:], said[:]
    assert ps.fetch(cache, get=get, say=said.append) == 1
    assert asked == [f"{base}4/1/0.jpeg"]
    del asked[:], said[:]
    assert ps.fetch(cache, get=get, say=said.append) == 0 and not asked and not said


def test_fetch_tries_again_and_then_gives_up(small, tmp_path):
    cache = str(tmp_path / "new")
    calls, waits = [], []

    def flaky(url):
        calls.append(url)
        if url.endswith("/0/1.jpeg") and calls.count(url) < 3:
            raise OSError("reset")
        return _jpeg((9, 9, 9))

    assert ps.fetch(cache, get=flaky, workers=1, say=lambda _m: None, wait=waits.append) == 4
    assert waits == [1, 2] and len(calls) == 6

    def never(url):
        calls.append(url)
        raise OSError("gone")

    del calls[:], waits[:]
    cache = str(tmp_path / "none")
    with pytest.raises(RuntimeError, match=r"/4/0/0\.jpeg: gone"):
        ps.fetch(cache, get=never, workers=1, say=lambda _m: None, wait=waits.append)
    assert calls.count(calls[0]) == 4 and waits[:3] == [1, 2, 4]
    assert not os.path.exists(ps.source_path(cache, 0, 0))


@pytest.mark.parametrize("data", [b"not a picture", _jpeg((9, 9, 9), size=256)])
def test_fetch_keeps_nothing_that_is_not_a_source_tile(small, tmp_path, data):
    cache = str(tmp_path / "new")
    with pytest.raises(RuntimeError):
        ps.fetch(cache, get=lambda _u: data, workers=1, say=lambda _m: None, wait=lambda _s: None)
    assert not [n for _d, _s, names in os.walk(cache) for n in names]


def test_made_from_tells_one_set_of_tiles_from_another(small):
    was = ps.made_from(small, BOX)
    assert was == ps.made_from(small, list(BOX)) and len(was) == 64
    assert ps.made_from(small, (-31.0, -30.0, 31.0, 29.0)) != was
    assert ps.made_from(small, BOX, max_zoom=8) != was
    assert ps.made_from(small, BOX, quality=50) != was
    with open(ps.source_path(small, 1, 1), "ab") as f:
        f.write(b"x")
    grown = ps.made_from(small, BOX)
    assert grown != was
    os.unlink(ps.source_path(small, 1, 1))
    assert ps.made_from(small, BOX) not in (was, grown)


@needs_avif
def test_the_tiles_show_the_picture_where_the_map_has_the_place(small, tmp_path):
    out = str(tmp_path / "satellite")
    said = []
    counts = ps.build(out, small, BOX, max_zoom=5, workers=2, say=said.append)
    assert counts == {5: 36, 4: 16, 3: 4, 2: 4, 1: 4, 0: 1}
    assert said[0] == "  satellite z5: 36 tiles" and len(said) == 6
    assert not os.path.exists(out + ".part")
    for z, n in counts.items():
        files = [f for _d, _s, names in os.walk(os.path.join(out, str(z))) for f in names]
        assert len(files) == n and all(f.endswith(".avif") for f in files)
    # Zoom 5 is cut from the picture; the pole is the corner four tiles share.
    for (x, y), colour in {(15, 15): COLOURS[0, 0], (16, 15): COLOURS[0, 1],
                           (15, 16): COLOURS[1, 0], (16, 16): COLOURS[1, 1]}.items():
        tile = _tile(out, 5, x, y)
        assert tile.shape == (256, 256, 3)
        assert _near(tile.reshape(-1, 3).mean(axis=0), colour, by=12), (x, y)
        assert _near(tile[128, 128], colour), (x, y)
    # Zoom 4 reaches past the picture: the same places, from the small copy.
    for (x, y), colour in {(7, 7): COLOURS[0, 0], (8, 7): COLOURS[0, 1],
                           (7, 8): COLOURS[1, 0], (8, 8): COLOURS[1, 1]}.items():
        tile = _tile(out, 4, x, y)
        assert _near(tile[40:216, 40:216].reshape(-1, 3).mean(axis=0), colour, by=12), (x, y)
    # The pole is where the copy's four colours meet, not half a pixel off:
    # at a tile's side the two colours are mixed half and half.
    for (x, y, at), pair in {(7, 7, (128, 255)): ((0, 0), (0, 1)), (8, 7, (128, 0)): ((0, 0), (0, 1)),
                             (7, 7, (255, 128)): ((0, 0), (1, 0)), (7, 8, (0, 128)): ((0, 0), (1, 0))}.items():
        mixed = np.mean([COLOURS[pair[0]], COLOURS[pair[1]]], axis=0)
        assert _near(_tile(out, 4, x, y)[at], mixed, by=30), (x, y, at)
    # Past the picture: the colour of its rim.
    rim = np.mean(list(COLOURS.values()), axis=0)
    assert _near(_tile(out, 4, 6, 6)[10, 10], rim, by=12)
    whole = _tile(out, 0, 0, 0)
    assert _near(whole[5, 5], rim, by=12) and _near(whole[250, 250], rim, by=12)
    # The picture is 8,389 km of the 40,075 km square: 54 pixels of 256.
    assert _near(whole[114, 114], COLOURS[0, 0]) and _near(whole[114, 141], COLOURS[0, 1])
    assert _near(whole[141, 114], COLOURS[1, 0]) and _near(whole[141, 141], COLOURS[1, 1])
    assert _near(whole[128, 98], rim, by=25) and _near(whole[128, 158], rim, by=25)


@needs_avif
def test_a_zoom_inside_the_picture_is_cut_from_all_of_its_detail(small, tmp_path):
    # A line four pixels wide, south-east of the pole. The small copy that
    # the coarse zooms are cut from has a sixteenth of the detail: in it the
    # line is a faint shade.
    tile = Image.new("RGB", (ps.SOURCE_TILE, ps.SOURCE_TILE), COLOURS[1, 1])
    tile.paste((0, 0, 0), (88, 0, 92, ps.SOURCE_TILE))
    out = io.BytesIO()
    tile.save(out, "JPEG", quality=95)
    with open(ps.source_path(small, 1, 1), "wb") as f:
        f.write(out.getvalue())
    dest = str(tmp_path / "satellite")
    ps.build(dest, small, BOX, max_zoom=5, say=lambda _m: None)
    # A zoom 5 tile is 152.9 pixels of this picture: the line is at 147 to 154.
    row = _tile(dest, 5, 16, 16)[128].astype(int).sum(axis=1)
    assert row[150] < 120 and row[151] < 120
    assert row[135] > 350 and row[166] > 350
    coarse = _tile(dest, 4, 8, 8)[128].astype(int).sum(axis=1)
    assert coarse.min() > 250


@needs_avif
def test_the_folder_is_replaced_whole_or_not_at_all(small, tmp_path, monkeypatch):
    out = tmp_path / "satellite"
    (out / "7" / "1").mkdir(parents=True)
    (out / "7" / "1" / "1.avif").write_bytes(b"old")
    (tmp_path / "satellite.part" / "9").mkdir(parents=True)
    (tmp_path / "satellite.part" / "9" / "left").write_bytes(b"left")
    real = ps._tile_path

    def fails(dest, z, x, y):
        if z == 0:
            raise OSError("disk full")
        return real(dest, z, x, y)

    monkeypatch.setattr(ps, "_tile_path", fails)
    with pytest.raises(OSError, match="disk full"):
        ps.build(str(out), small, BOX, max_zoom=1, say=lambda _m: None)
    assert (out / "7" / "1" / "1.avif").read_bytes() == b"old" and not (out / "1").exists()
    monkeypatch.setattr(ps, "_tile_path", real)
    # A trailing slash names the same folder.
    assert ps.build(str(out) + "/", small, BOX, max_zoom=1, say=lambda _m: None) == {1: 4, 0: 1}
    assert not (out / "7").exists() and not (out / "9").exists()
    assert (out / "1" / "0" / "0.avif").stat().st_size > 0
    assert not (tmp_path / "satellite.part").exists()


def test_a_cache_without_a_tile_makes_no_picture(small):
    os.unlink(ps.source_path(small, 0, 1))
    with pytest.raises(OSError):
        ps.picture(small)


def _zim_args(*extra):
    import create_osm_zim
    return create_osm_zim.build_parser().parse_args(list(extra))


def _take(args, zoom=9):
    import create_osm_zim
    return create_osm_zim._satellite_and_terrain(
        args=args, bbox_str="-31,-30,31,30", include_routing=False,
        include_satellite=args.satellite, include_terrain=args.terrain, include_wikidata=False,
        satellite_download_zoom=zoom, satellite_format="avif", satellite_quality=None,
        satellite_tile_size=256, terrain_max_zoom=3, total_steps=5)


def test_a_polar_build_takes_its_imagery_tiles_as_they_are(tmp_path, monkeypatch, capsys):
    import create_osm_zim
    fetched = []
    monkeypatch.setattr(create_osm_zim, "download_satellite_tiles",
                        lambda *a, **k: fetched.append(a))
    tiles = tmp_path / "satellite"
    (tiles / "9" / "1").mkdir(parents=True)
    (tiles / "9" / "1" / "1.avif").write_bytes(b"tile")
    args = _zim_args("--projection", polar.NAME, "--satellite", "--satellite-dir", str(tiles))
    assert _take(args) == (str(tiles), None)
    assert not fetched
    assert f"Satellite tiles: {tiles}, as made for this map" in capsys.readouterr().out
    # The folder has to hold the zoom the map will say it has.
    with pytest.raises(RuntimeError, match=r"--satellite-dir .* zoom 10 \(--satellite-zoom\)"):
        _take(args, zoom=10)
    (tiles / "10").mkdir()
    with pytest.raises(RuntimeError, match="zoom 10"):
        _take(args, zoom=10)
    # Both kinds of tile, each from its own folder.
    terrain = tmp_path / "terrain"
    (terrain / "3" / "4").mkdir(parents=True)
    (terrain / "3" / "4" / "4.webp").write_bytes(b"tile")
    both = _zim_args("--projection", polar.NAME, "--satellite", "--satellite-dir", str(tiles),
                     "--terrain", "--terrain-dir", str(terrain))
    assert _take(both) == (str(tiles), str(terrain))
    out = capsys.readouterr().out
    assert "Satellite tiles" in out and f"Terrain tiles: {terrain}" in out
    # A map without imagery takes none, whatever folder is named.
    none = _zim_args("--projection", polar.NAME, "--satellite-dir", str(tiles))
    assert _take(none) == (None, None)
    assert not fetched


@pytest.mark.parametrize("extra, said", [
    (["--satellite-dir", "x"], "is for a map with --projection"),
    (["--projection", polar.NAME, "--satellite"], "--satellite needs --satellite-dir"),
    (["--projection", polar.NAME, "--satellite", "--satellite-dir", "x"],
     "--satellite-zoom, its deepest zoom"),
    (["--projection", polar.NAME, "--satellite", "--satellite-zoom", "9"],
     "tools/polar_data.py satellite"),
    (["--projection", polar.NAME, "--satellite", "--satellite-dir", "x", "--satellite-zoom", "9",
      "--satellite-format", "webp"], "256-pixel AVIF"),
    (["--projection", polar.NAME, "--satellite", "--satellite-dir", "x", "--satellite-zoom", "9",
      "--satellite-tile-size", "512"], "256-pixel AVIF"),
])
def test_imagery_a_build_cannot_take_is_refused(extra, said, capsys):
    import create_osm_zim
    with pytest.raises(SystemExit) as e:
        create_osm_zim.main(["--mbtiles", "none.mbtiles", "--bbox=-31,-30,31,30",
                             "--name", "x", *extra])
    assert e.value.code == 2
    assert said in capsys.readouterr().err


@pytest.mark.parametrize("extra", [[], ["--satellite-source", "s2cloudless-2016"]])
def test_a_polar_map_has_blue_marble_whatever_source_is_named(extra, monkeypatch, capsys, tmp_path):
    import create_osm_zim

    class Far(Exception):
        pass

    def stop():
        raise Far

    monkeypatch.setattr(create_osm_zim, "log_viewer_freshness", stop)
    with pytest.raises(Far):
        create_osm_zim.main(["--mbtiles", "none.mbtiles", "--bbox=-31,-30,31,30", "--name", "x",
                             "--output", str(tmp_path / "x.zim"),
                             "--projection", polar.NAME, "--satellite", "--satellite-dir", "x",
                             "--satellite-zoom", "9", *extra])
    assert ("Including Blue Marble satellite imagery (z0-9, avif q40 256px); "
            "bluemarble-antarctic, Public domain") in capsys.readouterr().out
