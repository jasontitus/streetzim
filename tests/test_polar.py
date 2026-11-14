"""streetzim/polar.py: the positions of a polar map (Antarctica with its
pole). The viewer's copy of the maths (122-polar.js) is held to the same
numbers in tests/viewer_ui_js.test.mjs."""
import math
import random

import pytest

from streetzim import polar

# (lon, lat) -> EPSG:3031 metres, from PROJ.
PROJ = [
    ((0.0, -90.0), (0.0000, 0.0000)),
    ((166.6687, -77.8484), (305536.9348, -1289365.6053)),
    ((0.0, -60.0), (0.0000, 3333134.0276)),
    ((90.0, -71.0), (2082760.1085, 0.0000)),
    ((-63.5, -64.8), (-2489051.5860, 1240995.3423)),
]


@pytest.mark.parametrize("lonlat,xy", PROJ)
def test_matches_proj(lonlat, xy):
    x, y = polar.to_polar(*lonlat)
    assert math.hypot(x - xy[0], y - xy[1]) < 0.01


def test_matches_proj_live():
    warp = pytest.importorskip("rasterio.warp")
    rnd = random.Random(1)
    pts = [(rnd.uniform(-180, 180), rnd.uniform(-90, -50)) for _ in range(500)]
    xs, ys = warp.transform("EPSG:4326", "EPSG:3031", [p[0] for p in pts], [p[1] for p in pts])
    for (lon, lat), x, y in zip(pts, xs, ys):
        px, py = polar.to_polar(lon, lat)
        assert math.hypot(px - x, py - y) < 1e-3


def test_there_and_back():
    rnd = random.Random(2)
    for _ in range(500):
        lon, lat = rnd.uniform(-180, 180), rnd.uniform(-89.999, -50)
        back = polar.to_true(*polar.to_map(lon, lat))
        assert abs(back[1] - lat) < 1e-9
        assert abs((back[0] - lon + 180) % 360 - 180) * math.cos(math.radians(lat)) < 1e-9


def test_the_pole_is_the_centre_and_the_axes_are_the_convention():
    assert polar.to_map(123.0, -90.0) == pytest.approx((0.0, 0.0), abs=1e-12)
    assert polar.to_true(0.0, 0.0) == (0.0, -90.0)
    up, right = polar.to_map(0, -80), polar.to_map(90, -80)
    assert abs(up[0]) < 1e-9 and up[1] > 0          # 0 E is up
    assert right[0] > 0 and abs(right[1]) < 1e-9    # 90 E is right
    # The two ends of the antimeridian are one place.
    assert polar.to_map(180, -78) == pytest.approx(polar.to_map(-180, -78), abs=1e-9)


def test_the_map_fits_web_mercator():
    for lon in range(-180, 180, 5):
        mlon, mlat = polar.to_map(lon, polar.EDGE_LAT)
        assert abs(mlon) < 31 and abs(mlat) < 31


def test_edge_mask_greys_everything_past_the_edge():
    shapely = pytest.importorskip("shapely.geometry")
    mask = shapely.shape(polar.edge_mask([-31, -30, 31, 30]))
    assert mask.is_valid
    assert not mask.contains(shapely.Point(0, 0))                           # the pole
    assert not mask.contains(shapely.Point(*polar.to_map(45, -60.5)))       # just inside
    assert mask.contains(shapely.Point(*polar.to_map(45, -59.5)))           # just outside
    assert mask.contains(shapely.Point(30.9, 29.9))                         # a corner of the box
    assert mask.contains(shapely.Point(60, 0))                              # past the box


def test_map_config_says_the_map_is_polar():
    shapely = pytest.importorskip("shapely.geometry")
    import create_osm_zim
    common = {"bbox_str": "-31,-30,31,30", "name": "T", "overture_sources": None,
              "routing_graph_path": None, "satellite_dir": None, "satellite_format": "avif",
              "satellite_max_zoom": None, "satellite_tile_size": 256, "search_features": None,
              "terrain_dir": None, "terrain_max_zoom": None, "total_steps": 6,
              "wiki_cross_refs": None, "wikidata_data": None}
    parse = create_osm_zim.build_parser().parse_args
    _, mc = create_osm_zim._build_map_config(args=parse([]), **common)
    assert "projection" not in mc and "clipMask" not in mc
    _, mc = create_osm_zim._build_map_config(args=parse(["--projection", polar.NAME]), **common)
    assert mc["projection"] == "antarctic-polar"      # the name the viewer looks for (122-polar.js)
    assert mc["center"] == [0, 0]                     # the pole
    assert mc["clipMinZoom"] == 0
    mask = shapely.shape(mc["clipMask"])
    assert mask.contains(shapely.Point(*polar.to_map(10, -59))) and not mask.contains(shapely.Point(0, 0))
