"""--clip-poly: a build clipped to a region's outline (streetzim/clip.py).

What must hold: tiles up to the clip's min zoom are all kept (the context);
past it a tile is kept exactly when it touches the outline, whatever order
tiles are asked in; the outline is widened by about the asked distance;
the .poly written for osmium is the outline; the viewer's mask covers the
box outside the outline and nothing inside; search records outside are
dropped and the rest kept as they were; and with no clip active, nothing
changes (osmium still gets the box).
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

shapely = pytest.importorskip("shapely")
from shapely.geometry import Point, box, shape  # noqa: E402

from streetzim import area, clip  # noqa: E402

# A triangle around the Nile delta, with a square hole near its middle.
POLY = """delta
1
   29.0   30.0
   32.5   30.0
   30.8   31.6
   29.0   30.0
END
!2
   30.5   30.4
   31.0   30.4
   31.0   30.8
   30.5   30.8
   30.5   30.4
END
END
"""
BBOX = (28.0, 29.0, 34.0, 32.5)


@pytest.fixture
def poly_file(tmp_path):
    p = tmp_path / "delta.poly"
    p.write_text(POLY)
    return p


@pytest.fixture(autouse=True)
def no_active_clip():
    yield
    clip.set_active(None)


def test_parse_poly_outer_ring_minus_hole(poly_file):
    g = clip.parse_poly(poly_file.read_text())
    assert g.contains(Point(30.0, 30.2))
    assert not g.contains(Point(30.75, 30.6))      # in the hole
    assert not g.contains(Point(33.0, 31.0))


def test_parse_poly_rejects_rings_past_the_antimeridian():
    text = "x\n1\n   179 0\n   181 0\n   180 1\nEND\nEND\n"
    with pytest.raises(ValueError, match="antimeridian"):
        clip.parse_poly(text)


def test_tiles_up_to_min_zoom_are_all_kept(poly_file):
    c = clip.Clip.from_poly_file(str(poly_file), buffer=0, min_zoom=10)
    for z in range(0, 11):
        n = 2 ** z
        for x, y in ((0, 0), (n - 1, n - 1), (n // 2, n // 3)):
            assert c.keeps_tile(z, x, y)


def test_keeps_tile_matches_a_direct_test_in_any_order(poly_file):
    c = clip.Clip.from_poly_file(str(poly_file), buffer=0, min_zoom=10)
    g = clip.parse_poly(poly_file.read_text())
    rnd = random.Random(7)
    tiles = []
    for z in range(11, 15):
        n = 2 ** z
        x0, x1 = int((BBOX[0] + 180) / 360 * n), int((BBOX[2] + 180) / 360 * n)
        for _ in range(400):
            x = rnd.randint(x0, x1)
            # Rows across the box's latitudes (y grows southwards).
            import math
            def row(lat, n=n):
                r = math.radians(lat)
                return int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
            y = rnd.randint(row(BBOX[3]), row(BBOX[1]))
            tiles.append((z, x, y))
    rnd.shuffle(tiles)                              # children before parents too
    for z, x, y in tiles:
        want = g.intersects(box(*clip.tile_box(z, x, y)))
        assert c.keeps_tile(z, x, y) == want, (z, x, y)
    assert any(c.keeps_tile(*t) for t in tiles) and not all(c.keeps_tile(*t) for t in tiles)


def test_buffer_widens_by_about_the_distance(poly_file):
    # 5 km south of the triangle's base (lat 30.0); 1 deg lat = 111 km.
    p = Point(30.5, 30.0 - 5 / 111.32)
    assert not clip.Clip.from_poly_file(str(poly_file), buffer=0).geom.contains(p)
    assert not clip.Clip.from_poly_file(str(poly_file), buffer=3).geom.contains(p)
    assert clip.Clip.from_poly_file(str(poly_file), buffer=7).geom.contains(p)
    # East-west too: 5 km east of the base's east end, scaled by cos(lat).
    import math
    q = Point(32.5 + 5 / (111.32 * math.cos(math.radians(30.0))), 30.0 + 0.001)
    assert not clip.Clip.from_poly_file(str(poly_file), buffer=3).geom.contains(q)
    assert clip.Clip.from_poly_file(str(poly_file), buffer=7).geom.contains(q)


def test_written_poly_is_the_outline(poly_file, tmp_path):
    c = clip.Clip.from_poly_file(str(poly_file), buffer=0)
    out = c.write_poly(str(tmp_path / "out.poly"))
    back = clip.parse_poly(Path(out).read_text())
    assert back.symmetric_difference(c.geom).area < 1e-9
    assert not back.contains(Point(30.75, 30.6))    # the hole survives


def test_mask_covers_outside_and_nothing_inside(poly_file):
    c = clip.Clip.from_poly_file(str(poly_file), buffer=0)
    m = shape(c.mask_geojson(BBOX, tolerance=0))
    assert m.contains(Point(33.5, 29.5))            # in the box, outside
    assert m.contains(Point(BBOX[0] - 0.5, 30.0))   # past the box edge too
    assert not m.contains(Point(30.0, 30.2))        # inside the outline
    assert m.contains(Point(30.75, 30.6))           # the hole is outside


def test_osmium_gets_the_outline_only_while_a_clip_is_active(poly_file, tmp_path):
    assert area.osmium_extract_args(BBOX, str(tmp_path)) == ["-b", "28.0,29.0,34.0,32.5"]
    c = clip.Clip.from_poly_file(str(poly_file), buffer=0)
    clip.set_active(c, workdir=str(tmp_path))
    args = area.osmium_extract_args(BBOX, str(tmp_path))
    assert args[0] == "-p" and Path(args[1]).is_file()
    assert clip.active() is c
    clip.set_active(None)
    assert area.osmium_extract_args(BBOX, str(tmp_path)) == ["-b", "28.0,29.0,34.0,32.5"]
    assert clip.active() is None


def test_a_spawned_child_cuts_to_the_outline_too(poly_file, tmp_path):
    # Routing runs in a spawned child (streetzim/isolate.py), which does not
    # inherit module state: the Egypt trial's first routing graph was cut to
    # the box, all the way to Tel Aviv.
    from streetzim.isolate import run_in_child
    c = clip.Clip.from_poly_file(str(poly_file), buffer=0)
    clip.set_active(c, workdir=str(tmp_path))
    args = run_in_child(area.osmium_extract_args, BBOX, str(tmp_path))
    assert args == ["-p", c.poly_path]
    clip.set_active(None)
    assert run_in_child(area.osmium_extract_args, BBOX, str(tmp_path)) == \
        ["-b", "28.0,29.0,34.0,32.5"]


def test_search_records_outside_are_dropped(poly_file, tmp_path):
    import create_osm_zim
    recs = [
        {"name": "Tanta", "type": "place", "lat": 30.79, "lon": 31.3},     # inside (east of hole)
        {"name": "Hole", "type": "place", "lat": 30.6, "lon": 30.75},      # in the hole
        {"name": "Suez", "type": "place", "lat": 29.97, "lon": 32.55},     # outside
        {"name": "No point", "type": "admin"},                             # kept
    ]
    path = tmp_path / "search.jsonl"
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)
    path.write_text(text)
    create_osm_zim._clip_search_features(str(path))             # no clip: unchanged
    assert path.read_text() == text
    clip.set_active(clip.Clip.from_poly_file(str(poly_file), buffer=0))
    create_osm_zim._clip_search_features(str(path))
    kept = [json.loads(ln)["name"] for ln in path.read_text().splitlines()]
    assert kept == ["Tanta", "No point"]
