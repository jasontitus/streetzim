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
import os
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


# --- Review fixes (2026-10-09) -------------------------------------------

def _poly_text(*rings):
    """A .poly from (name, [(x, y), ...]) rings."""
    out = ["t"]
    for name, pts in rings:
        out.append(name)
        out += [f"   {x} {y}" for x, y in pts]
        out.append("END")
    return "\n".join(out + ["END", ""])


SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
HOLE = [(2, 2), (8, 2), (8, 8), (2, 8), (2, 2)]
ISLAND = [(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]


@pytest.mark.parametrize("order", ["island-last", "island-first"])
def test_an_island_in_a_hole_stays_whatever_the_order(order):
    rings = [("1", SQUARE), ("!2", HOLE), ("3", ISLAND)]
    if order == "island-first":
        rings = [("3", ISLAND), ("1", SQUARE), ("!2", HOLE)]
    g = clip.parse_poly(_poly_text(*rings))
    assert g.contains(Point(1, 1)) and g.contains(Point(5, 5))
    assert not g.contains(Point(3, 3))


def test_a_self_crossing_ring_keeps_both_lobes():
    g = clip.parse_poly(_poly_text(("1", [(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])))
    assert g.contains(Point(0.3, 1)) and g.contains(Point(1.7, 1))
    assert g.area == pytest.approx(2.0)


def test_an_outline_with_no_area_is_refused():
    with pytest.raises(ValueError, match="no area"):
        clip.parse_poly(_poly_text(("1", [(0, 0), (1, 1), (2, 2), (0, 0)])))


@pytest.mark.parametrize("ring", [
    [(170, 60), (180, 60), (180, 70), (170, 70), (170, 60)],          # Geofabrik's split at 180
    [(-180, 60), (-170, 60), (-170, 70), (-180, 70), (-180, 60)],
    [(170, 60), (179.9, 60), (-170, 65), (170, 70), (170, 60)],       # jumps across
])
def test_an_outline_at_the_antimeridian_is_refused(ring):
    with pytest.raises(ValueError, match="antimeridian"):
        clip.parse_poly(_poly_text(("1", ring)))


def test_a_buffer_near_the_antimeridian_stays_on_the_globe():
    g = clip.buffer_km(clip.parse_poly(_poly_text(("1", [(170, 0), (179.99, 0), (179.99, 5), (170, 5), (170, 0)]))), 10)
    assert g.bounds[2] <= 180.0


def test_the_buffer_is_the_distance_at_every_latitude():
    # A tall strip like Norway (58-71N): the east-west widening at each end
    # is the asked 10 km within 5%, not 12 km south and 8 km north.
    import math
    g = clip.buffer_km(clip.parse_poly(_poly_text(("1", [(10, 58), (10.5, 58), (10.5, 71), (10, 71), (10, 58)]))), 10)
    for lat in (58.5, 64.5, 70.5):
        deg = 10 / (111.32 * math.cos(math.radians(lat)))     # 10 km east, in degrees
        assert g.contains(Point(10.5 + 0.95 * deg, lat)), lat
        assert not g.contains(Point(10.5 + 1.05 * deg, lat)), lat
        assert g.contains(Point(10.0 - 0.95 * deg, lat)) and not g.contains(Point(10.0 - 1.05 * deg, lat))


def test_the_clip_is_cut_to_the_box(poly_file):
    c = clip.Clip.from_poly_file(str(poly_file), buffer=50, bbox=(29.5, 29.0, 32.0, 31.0))
    assert c.geom.bounds[0] >= 29.5 and c.geom.bounds[2] <= 32.0 and c.geom.bounds[3] <= 31.0
    assert c.border.bounds[2] == pytest.approx(32.5)       # the border itself is not cut
    with pytest.raises(ValueError, match="does not meet"):
        clip.Clip.from_poly_file(str(poly_file), bbox=(0.0, 0.0, 1.0, 1.0))


def test_the_memo_holds_the_outline_not_the_area(poly_file):
    c = clip.Clip.from_poly_file(str(poly_file), buffer=0, min_zoom=10)
    import mercantile
    deep = list(mercantile.tiles(29.6, 30.1, 30.4, 30.35, [13]))   # inside, clear of the hole
    assert len(deep) > 100 and all(c.keeps_tile(t.z, t.x, t.y) for t in deep)
    assert len(c._state) < len(deep) / 4                   # not one entry a tile
    # Every answer still matches a direct test.
    from shapely.geometry import box as _box
    for t in deep[:200]:
        assert c.keeps_tile(t.z, t.x, t.y) == c.geom.intersects(_box(*clip.tile_box(t.z, t.x, t.y)))


def mercantile_tile(lon, lat, z):
    import mercantile
    return mercantile.tile(lon, lat, z)


def _tiles_over(c, z):
    import mercantile
    return list(mercantile.tiles(*c.geom.bounds, [z]))


def _deep_inside_tile(c, z):
    """A tile of zoom z well inside the clip (past its context edge zone)."""
    from shapely.geometry import box as _box
    inner = c.geom.buffer(-3 * clip.CONTEXT_EDGE_DEG)
    return next(t for t in _tiles_over(c, z) if inner.contains(_box(*clip.tile_box(z, t.x, t.y))))


def test_context_tiles_are_the_clip_zoom_tiles_that_reach_past_the_outline(poly_file):
    from shapely.geometry import box as _box
    c = clip.Clip.from_poly_file(str(poly_file), buffer=0, min_zoom=10)
    deep = _deep_inside_tile(c, 10)
    outside = mercantile_tile(33.5, 31.5, 10)
    edge = mercantile_tile(30.0, 30.0, 10)              # on the base line
    assert not c.needs_context(deep.x, deep.y)
    assert c.needs_context(outside.x, outside.y) and c.needs_context(edge.x, edge.y)
    # A tile inside the outline but within its edge zone (the viewer's
    # simplified clipArea may cut it) still gets one; one further in does not.
    t = mercantile_tile(30.0, 30.0, 10)
    w, s_, e, n = clip.tile_box(10, t.x, t.y)
    edgy = clip.Clip(_box(w - 2, s_ - 2, e + 0.005, n + 2), 10)
    assert edgy.geom.contains(_box(w, s_, e, n)) and edgy.needs_context(t.x, t.y)
    assert not edgy.needs_context(t.x - 2, t.y)


def test_activate_clip_needs_a_box_off_the_antimeridian(poly_file, monkeypatch):
    import create_osm_zim
    parser = create_osm_zim.build_parser()
    args = parser.parse_args(["--clip-poly", str(poly_file), "--clip-buffer-km", "7",
                              "--clip-min-zoom", "9"])
    with pytest.raises(SystemExit):
        create_osm_zim._activate_clip(args, None, parser)
    # Fiji's western islands: an outline short of 180 inside a box across it.
    fiji = poly_file.parent / "fiji-west.poly"
    fiji.write_text(_poly_text(("1", [(177, -19), (179.5, -19), (179.5, -16), (177, -16), (177, -19)])))
    fargs = parser.parse_args(["--clip-poly", str(fiji)])
    assert create_osm_zim._activate_clip(fargs, "177,-19,179.9,-16", parser) is not None
    with pytest.raises(SystemExit):
        create_osm_zim._activate_clip(fargs, "177,-19,182,-16", parser)
    seen = {}
    real = clip.Clip.from_poly_file

    def spy(path, **kw):
        seen.update(kw)
        return real(path, **kw)
    monkeypatch.setattr(clip.Clip, "from_poly_file", staticmethod(spy))
    c = create_osm_zim._activate_clip(args, "28,29,34,32.5", parser)
    assert {**seen, "bbox": tuple(seen["bbox"])} == {"buffer": 7.0, "min_zoom": 9,
                                                      "bbox": (28.0, 29.0, 34.0, 32.5)}
    assert c.min_zoom == 9


def test_tilemaker_gets_the_whole_box_and_later_cuts_the_outline(poly_file, tmp_path, monkeypatch):
    # With a clip, the build's own cut stays the box (tilemaker's input: the
    # low-zoom context), and it is not "cut" for the later steps, which then
    # cut to the outline themselves.
    from types import SimpleNamespace
    import create_osm_zim
    calls = []
    monkeypatch.setattr(create_osm_zim, "extract_bbox_from_pbf",
                        lambda src, bbox, out, clip=True: calls.append(clip))
    monkeypatch.setattr(create_osm_zim, "generate_tiles", lambda *a, **kw: None)
    args = SimpleNamespace(mbtiles=None, area=None, store=None, tilemaker_store=None, fast=False)
    kw = {"args": args, "bbox_str": "28,29,34,32.5", "geofabrik_path": None,
          "pbf_path": str(tmp_path / "x.pbf"), "tmpdir": str(tmp_path), "total_steps": 6}
    _, _, cut = create_osm_zim._acquire_tiles(**kw)
    assert (calls, cut) == ([False], True)
    clip.set_active(clip.Clip.from_poly_file(str(poly_file), buffer=0), workdir=str(tmp_path))
    _, _, cut = create_osm_zim._acquire_tiles(**kw)
    assert (calls, cut) == ([False, False], False)
    assert area.osmium_extract_args(BBOX, str(tmp_path), clip=False) == ["-b", "28.0,29.0,34.0,32.5"]


def test_map_config_carries_the_clip(poly_file):
    import create_osm_zim
    args = create_osm_zim.build_parser().parse_args([])
    common = {"args": args, "bbox_str": "28,29,34,32.5", "name": "T", "overture_sources": None,
              "routing_graph_path": None, "satellite_dir": None, "satellite_format": "avif",
              "satellite_max_zoom": None, "satellite_tile_size": 256, "search_features": None,
              "terrain_dir": None, "terrain_max_zoom": None, "total_steps": 6,
              "wiki_cross_refs": None, "wikidata_data": None}
    _, mc = create_osm_zim._build_map_config(**common)
    assert not any(k.startswith("clip") for k in mc)
    clip.set_active(clip.Clip.from_poly_file(str(poly_file), buffer=0, min_zoom=10))
    _, mc = create_osm_zim._build_map_config(**common)
    assert mc["clipMinZoom"] == 10 and mc["clipContext"] == "ctx"
    assert shape(mc["clipArea"]).contains(Point(30.0, 30.2))
    assert shape(mc["clipMask"]).contains(Point(33.5, 31.5))


def test_the_writer_keeps_tiles_by_the_outline_and_redirects_context(poly_file, tmp_path, monkeypatch):
    import mercantile
    from streetzim import zim_writer
    c = clip.Clip.from_poly_file(str(poly_file), buffer=0, min_zoom=10)
    inside10, outside10 = _deep_inside_tile(c, 10), mercantile.tile(33.5, 31.5, 10)
    inside11, outside11 = mercantile.tile(29.9, 30.3, 11), mercantile.tile(33.5, 31.5, 11)
    assert c.keeps_tile(11, inside11.x, inside11.y)
    tiles = [(t.z, t.x, t.y) for t in (inside10, outside10, inside11, outside11)]
    monkeypatch.setattr(zim_writer, "estimate_tile_total", lambda *a, **kw: len(tiles))
    monkeypatch.setattr(zim_writer, "iter_tiles_from_mbtiles",
                        lambda *a, **kw: iter([(z, x, y, b"tile %d" % i) for i, (z, x, y) in enumerate(tiles)]))

    class Creator:
        def __init__(self):
            self.items, self.redirects = [], {}

        def add_item(self, item):
            self.items.append(item.path)

        def add_redirection(self, path, title, target, hints=None):
            self.redirects[path] = target

    class Item:
        def __init__(self, path, *a):
            self.path = path

    cr = Creator()
    clip.set_active(c)
    zim_writer._add_vector_tiles(cr, Item, output_path=tmp_path / "m.zim", tiles=None,
                                 mbtiles_path="tiles", tile_count=len(tiles), bbox=None,
                                 zim_builder="manifest", max_zoom=14)
    p = lambda t: f"tiles/{t.z}/{t.x}/{t.y}.pbf"
    assert set(cr.items) == {p(inside10), p(outside10), p(inside11)}
    # The Rust packer's creator has no aliases: a redirect.
    assert cr.redirects == {f"ctx/10/{outside10.x}/{outside10.y}.pbf": p(outside10)}

    class AliasingCreator(Creator):
        def __init__(self):
            super().__init__()
            self.aliases = {}

        def add_alias(self, path, title, target, hints=None):
            self.aliases[path] = target
    ac = AliasingCreator()
    zim_writer._add_vector_tiles(ac, Item, output_path=tmp_path / "m.zim", tiles=None,
                                 mbtiles_path="tiles", tile_count=len(tiles), bbox=None,
                                 zim_builder="python", max_zoom=14)
    # libzim's creator: an alias, which readers serve as an ordinary item.
    assert ac.aliases == {f"ctx/10/{outside10.x}/{outside10.y}.pbf": p(outside10)}
    assert ac.redirects == {}
    # A clip-zoom tile that is itself an alias (the same bytes as an earlier
    # tile): its context alias points at the item, not at the alias.
    dup = [(10, inside10.x, inside10.y, b"sea"), (10, outside10.x, outside10.y, b"sea")]
    monkeypatch.setattr(zim_writer, "iter_tiles_from_mbtiles", lambda *a, **kw: iter(dup))
    ac = AliasingCreator()
    zim_writer._add_vector_tiles(ac, Item, output_path=tmp_path / "m.zim", tiles=None,
                                 mbtiles_path="tiles", tile_count=2, bbox=None,
                                 zim_builder="python", max_zoom=14)
    assert ac.aliases == {p(outside10): p(inside10),
                          f"ctx/10/{outside10.x}/{outside10.y}.pbf": p(inside10)}


def test_the_search_step_drops_records_outside_the_outline(poly_file, tmp_path):
    import create_osm_zim
    recs = [{"name": "Tanta", "type": "place", "lat": 30.79, "lon": 31.3},
            {"name": "Suez", "type": "place", "lat": 29.97, "lon": 32.55}]
    cache = tmp_path / "cache.jsonl"
    cache.write_text("".join(json.dumps(r) + "\n" for r in recs))
    args = create_osm_zim.build_parser().parse_args(["--search-cache", str(cache)])

    def names():
        out = create_osm_zim._build_search(
            args=args, bbox_str="28,29,34,32.5", mbtiles_path=None, pbf_path=None, tiles=None,
            tmpdir=str(tmp_path), total_steps=6, use_streaming=False, work_pbf=None,
            work_pbf_cut=False)
        path = next(v for v in out if isinstance(v, str) and v.endswith(".jsonl"))
        return [json.loads(ln)["name"] for ln in open(path)]
    assert names() == ["Tanta", "Suez"]
    clip.set_active(clip.Clip.from_poly_file(str(poly_file), buffer=0))
    assert names() == ["Tanta"]


def test_the_terrain_gate_skips_what_the_clip_dropped(poly_file):
    # cloud/check_terrain_coverage.py counts a missing terrain tile over land
    # as a failure; a clipped ZIM has none past the clip zoom outside the
    # outline, by design.
    import importlib.util
    import mercantile
    spec = importlib.util.spec_from_file_location("ctc", ROOT / "cloud" / "check_terrain_coverage.py")
    ctc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctc)
    c = clip.Clip.from_poly_file(str(poly_file), buffer=0)

    class Arc:
        def __init__(self, mc):
            self.mc = mc

        def get_entry_by_path(self, p):
            assert p == "map-config.json"
            if self.mc is None:
                raise KeyError(p)
            data = json.dumps(self.mc).encode()
            return type("E", (), {"get_item": lambda s: type("I", (), {"content": data})()})()
    inside, outside = mercantile.tile(29.9, 30.3, 11), mercantile.tile(33.5, 31.5, 11)
    assert ctc.clip_filter(Arc(None)) is None
    assert ctc.clip_filter(Arc({"minZoom": 0})) is None
    for mc in ({"clipMinZoom": 10, "clipArea": c.area_geojson()},
               {"clipMinZoom": 10, "clipMask": c.mask_geojson(BBOX)}):        # early trial builds
        z, keeps = ctc.clip_filter(Arc(mc))
        assert z == 10
        assert keeps(inside.x, inside.y, 11) and not keeps(outside.x, outside.y, 11)


def test_rings_nest_even_odd_as_osmium_reads_them():
    # A lake (4-6) in an island (3-7) in a lake (2-8) in land (0-10).
    sq = lambda a, b: [(a, a), (b, a), (b, b), (a, b), (a, a)]
    g = clip.parse_poly(_poly_text(("1", sq(0, 10)), ("!2", sq(2, 8)), ("3", sq(3, 7)), ("!4", sq(4, 6))))
    assert [g.contains(Point(v, v)) for v in (1, 2.5, 3.5, 5)] == [True, False, True, False]


def test_a_clip_touching_the_box_edge_writes_a_poly(tmp_path):
    # Part of the outline only touches the box's edge: the cut leaves a line
    # beside the polygon, which write_poly (rings only) could not write.
    p = tmp_path / "touch.poly"
    p.write_text(_poly_text(("1", [(1, 1), (5, 1), (5, 5), (1, 5), (1, 1)]),
                            ("2", [(10, 0), (12, 0), (12, 2), (10, 2), (10, 0)])))
    c = clip.Clip.from_poly_file(str(p), buffer=0, bbox=(0.0, 0.0, 10.0, 10.0))
    assert c.geom.geom_type == "Polygon"
    c.write_poly(str(tmp_path / "out.poly"))
    assert clip.parse_poly((tmp_path / "out.poly").read_text()).equals(c.geom)


def _margin_km(grown, geom, lon_range):
    """(smallest, largest) distance in km from the buffered outline's
    boundary, in lon_range, to `geom`: each sample in its own local plane."""
    import math
    import shapely
    pts = shapely.segmentize(grown.exterior, 0.02).coords
    out = []
    for lon, lat in pts:
        if not lon_range[0] <= lon <= lon_range[1]:
            continue
        k = math.cos(math.radians(lat)) * 111.32
        local = shapely.transform(geom, lambda c, lon=lon, lat=lat, k=k: (c - (lon, lat)) * (k, 111.32))
        out.append(local.distance(Point(0, 0)))
    return min(out), max(out)


def test_the_buffer_holds_far_from_the_middle_meridian():
    # A wide outline at 60N with a diagonal edge 35-40 deg from its middle
    # meridian (Canada's Alaska panhandle): one projection for the whole
    # outline sheared that margin to about 7 km of the asked 10.
    g = clip.parse_poly(_poly_text(("1", [(-140, 58), (-130, 62), (-60, 62), (-60, 55), (-140, 55), (-140, 58)])))
    grown = clip.buffer_km(g, 10)
    lo, hi = _margin_km(grown, g, (-180, 180))
    assert 9.7 < lo and hi < 10.35, (lo, hi)        # 12-degree strips: 9.54 to 10.45


def test_the_terrain_gate_counts_the_clip_zoom_and_the_outline_edge():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ctc", ROOT / "cloud" / "check_terrain_coverage.py")
    ctc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctc)
    gone = (10, lambda x, y, z: False)              # the clip kept nothing past z10
    assert not ctc.clip_skips(gone, 0, 0, 10)       # the clip zoom is always in the ZIM
    assert ctc.clip_skips(gone, 0, 0, 11)
    assert not ctc.clip_skips(None, 0, 0, 11)
    # A tile that meets clipArea only within CLIP_EDGE_DEG of its edge is not
    # counted: the simplified area may run past the outline the writer used.
    import mercantile
    t = mercantile.tile(30.0, 30.0, 12)
    w, s_, e, n = ctc.tile_bounds(t.x, t.y, 12)
    area = {"type": "Polygon", "coordinates": [[[e - 0.005, s_], [e + 1, s_], [e + 1, n], [e - 0.005, n], [e - 0.005, s_]]]}

    class Arc:
        def get_entry_by_path(self, p):
            data = json.dumps({"clipMinZoom": 10, "clipArea": area}).encode()
            return type("E", (), {"get_item": lambda s: type("I", (), {"content": data})()})()
    clipf = ctc.clip_filter(Arc())
    assert ctc.clip_skips(clipf, t.x, t.y, 12)
    assert not ctc.clip_skips(clipf, t.x + 1, t.y, 12)


def test_the_search_step_drops_records_outside_on_the_pbf_path(poly_file, tmp_path, monkeypatch):
    # The path every real build takes (a PBF beside the search records).
    import create_osm_zim
    monkeypatch.setattr(create_osm_zim, "extract_wiki_tags_pbf", lambda *a, **kw: {})
    recs = [{"name": "Tanta", "type": "place", "lat": 30.79, "lon": 31.3},
            {"name": "Suez", "type": "place", "lat": 29.97, "lon": 32.55}]
    cache = tmp_path / "cache.jsonl"
    cache.write_text("".join(json.dumps(r) + "\n" for r in recs))
    pbf = tmp_path / "region.osm.pbf"
    pbf.write_bytes(b"")
    args = create_osm_zim.build_parser().parse_args(
        ["--search-cache", str(cache), "--skip-address-extract", "--pbf", str(pbf)])
    clip.set_active(clip.Clip.from_poly_file(str(poly_file), buffer=0))
    out = create_osm_zim._build_search(
        args=args, bbox_str="28,29,34,32.5", mbtiles_path=None, pbf_path=str(pbf), tiles=None,
        tmpdir=str(tmp_path), total_steps=6, use_streaming=False, work_pbf=None,
        work_pbf_cut=False)
    path = next(v for v in out if isinstance(v, str) and v.endswith(".jsonl"))
    assert [json.loads(ln)["name"] for ln in open(path)] == ["Tanta"]


def test_a_build_starts_and_ends_with_no_clip(poly_file, tmp_path, monkeypatch):
    # A clip from the shell (STREETZIM_CLIP_POLY) or an earlier build in this
    # process must not reach a build without --clip-poly, nor outlive one.
    import create_osm_zim
    monkeypatch.setenv(area.CLIP_POLY_ENV, str(poly_file))
    seen = []

    def stop(**kw):
        seen.append((clip.active(), area.osmium_extract_args(BBOX, str(tmp_path))))
        raise RuntimeError("stop")
    monkeypatch.setattr(create_osm_zim, "_acquire_tiles", stop)
    with pytest.raises(RuntimeError, match="stop"):
        create_osm_zim.main(["--bbox", "28,29,34,32.5", "--name", "T", "--mbtiles", str(tmp_path / "t.mbtiles"),
                             "--output", str(tmp_path / "t.zim")])
    assert seen == [(None, ["-b", "28.0,29.0,34.0,32.5"])]
    # A build with --clip-poly that fails part-way leaves no clip behind.
    with pytest.raises(RuntimeError, match="stop"):
        create_osm_zim.main(["--bbox", "28,29,34,32.5", "--name", "T", "--mbtiles", str(tmp_path / "t.mbtiles"),
                             "--clip-poly", str(poly_file), "--output", str(tmp_path / "t.zim")])
    assert seen[1][0] is not None and seen[1][1][0] == "-p"
    assert clip.active() is None and area.CLIP_POLY_ENV not in os.environ


def test_repackage_keeps_context_entries_one_tile_each(tmp_path):
    # A libzim build writes ctx/ as aliases, which read back as items; a
    # repack must not store each as a full copy of its z10 tile.
    pytest.importorskip("libzim")
    from libzim.reader import Archive
    from libzim.writer import Creator, Hint, Item, StringProvider
    from cloud.repackage_zim import repackage

    class It(Item):
        def __init__(self, path, data, mime="application/x-protobuf"):
            super().__init__()
            self.a = (path, data, mime)

        def get_path(self): return self.a[0]
        def get_title(self): return ""
        def get_mimetype(self): return self.a[2]
        def get_contentprovider(self): return StringProvider(self.a[1])
        def get_hints(self): return {Hint.FRONT_ARTICLE: False}
    tile = bytes(range(256)) * 16                    # far past the alias table's size cap
    src = tmp_path / "src.zim"
    with Creator(str(src)) as c:
        for k, v in {"Title": "t", "Description": "d", "Language": "eng", "Creator": "c",
                     "Publisher": "p", "Date": "2026-10-09", "Name": "n"}.items():
            c.add_metadata(k, v)
        c.add_item(It("index.html", b"<html></html>", "text/html"))
        c.add_item(It("tiles/10/5/6.pbf", tile))
        c.add_alias("ctx/10/5/6.pbf", "", "tiles/10/5/6.pbf", {Hint.FRONT_ARTICLE: False})
        c.set_mainpath("index.html")
    assert not Archive(str(src)).get_entry_by_path("ctx/10/5/6.pbf").is_redirect
    dst = tmp_path / "dst.zim"
    repackage(str(src), str(dst), swap_viewer=False)
    arc = Archive(str(dst))
    ctx = arc.get_entry_by_path("ctx/10/5/6.pbf")
    assert ctx.is_redirect and ctx.get_redirect_entry().path == "tiles/10/5/6.pbf"
    assert bytes(ctx.get_item().content) == tile
