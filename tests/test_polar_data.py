"""tools/polar_data.py: the moved extract, the sea and the tile layer file a
polar map is built from."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import polar_data  # noqa: E402
from streetzim import polar  # noqa: E402


def _extract(path):
    """A continent whose coast runs along 80 S (two ways), a hut at 85 S, an
    island at 65 S, and one node in Europe."""
    osmium = pytest.importorskip("osmium")
    node = osmium.osm.mutable.Node
    way = osmium.osm.mutable.Way
    with osmium.SimpleWriter(str(path)) as w:
        ring = list(range(1, 37))
        for i in ring:
            w.add_node(node(id=i, location=((i - 1) * 10 - 180, -80.0), version=1))
        w.add_node(node(id=100, location=(45.0, -85.0), version=1, tags={"place": "hamlet", "name": "Hut"}))
        isle = [(10.0, -65.0), (11.0, -65.0), (11.0, -65.4), (10.0, -65.4)]
        for i, at in enumerate(isle):
            w.add_node(node(id=200 + i, location=at, version=1))
        w.add_node(node(id=300, location=(30.52, 50.45), version=1))
        w.add_way(way(id=1, nodes=ring[:19], version=1, tags={"natural": "coastline"}))
        w.add_way(way(id=2, nodes=ring[18:] + ring[:1], version=1, tags={"natural": "coastline"}))
        w.add_way(way(id=3, nodes=[200, 201, 202, 203, 200], version=1, tags={"natural": "coastline"}))


def test_move_puts_every_node_at_its_map_position(tmp_path):
    osmium = pytest.importorskip("osmium")
    src, dst = tmp_path / "real.osm.pbf", tmp_path / "moved.osm.pbf"
    _extract(src)
    assert polar_data.move(str(src), str(dst)) == (41, 1)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["moved.osm.pbf", "real.osm.pbf"]
    seen = {"nodes": {}, "ways": {}}

    class Read(osmium.SimpleHandler):
        def node(self, n):
            seen["nodes"][n.id] = (n.location.lon, n.location.lat, dict(n.tags))

        def way(self, w):
            seen["ways"][w.id] = ([m.ref for m in w.nodes], dict(w.tags))

    Read().apply_file(str(dst))
    assert 300 not in seen["nodes"] and len(seen["nodes"]) == 41
    lon, lat, tags = seen["nodes"][100]
    assert (lon, lat) == pytest.approx(polar.to_map(45.0, -85.0), abs=1e-6)
    assert polar.to_true(lon, lat) == pytest.approx((45.0, -85.0), abs=1e-5)
    assert tags == {"place": "hamlet", "name": "Hut"}
    assert seen["ways"][3] == ([200, 201, 202, 203, 200], {"natural": "coastline"})


def test_ocean_is_the_disc_less_the_land(tmp_path):
    shapely = pytest.importorskip("shapely")
    src, moved, out = tmp_path / "real.osm.pbf", tmp_path / "moved.osm.pbf", tmp_path / "ocean.geojson"
    _extract(src)
    polar_data.move(str(src), str(moved))
    pieces, pole = polar_data.ocean(str(moved), str(out))
    assert pole and pieces >= 1
    feats = json.loads(out.read_text())["features"]
    assert len(feats) == pieces
    water = shapely.union_all([shapely.geometry.shape(f["geometry"]) for f in feats])

    def wet(lon, lat):
        return water.contains(shapely.Point(*polar.to_map(lon, lat)))

    assert not wet(0, -90) and not wet(45, -85) and not wet(120, -81)     # the continent
    assert wet(120, -75) and wet(-60, -61)                                # the sea
    assert not wet(10.5, -65.2)                                           # the island
    assert not wet(120, -59)                                              # past the map's edge
    assert all(shapely.get_num_coordinates(shapely.geometry.shape(f["geometry"])) <= polar_data.MAX_VERTS
               for f in feats)


def test_ocean_without_a_coast_around_the_pole_says_so(tmp_path):
    pytest.importorskip("shapely")
    osmium = pytest.importorskip("osmium")
    moved = tmp_path / "moved.osm.pbf"
    with osmium.SimpleWriter(str(moved)) as w:
        w.add_node(osmium.osm.mutable.Node(id=1, location=polar.to_map(0, -70), version=1))
    assert polar_data.ocean(str(moved), str(tmp_path / "o.geojson"))[1] is False
    with pytest.raises(SystemExit, match="does not close around the pole"):
        polar_data.main(["ocean", str(moved), str(tmp_path / "o.geojson")])


def test_tile_config_reads_no_real_world_file(tmp_path):
    base = json.loads((ROOT / "resources/tilemaker/config-openmaptiles.json").read_text())
    cfg = polar_data.tile_config(base, "polar/ocean.geojson")
    assert cfg["layers"]["ocean"]["source"] == "polar/ocean.geojson"
    assert [n for n, layer in cfg["layers"].items() if "source" in layer] == ["ocean"]
    assert base["layers"]["ocean"]["source"] != "polar/ocean.geojson"      # the profile's own is untouched
    same = set(base["layers"]) - set(polar_data.REAL_WORLD_LAYERS)
    assert set(cfg["layers"]) == same
    assert all(cfg["layers"][n] == base["layers"][n] for n in same - {"ocean"})
    assert cfg["settings"] == base["settings"]
    base["layers"]["lakes"] = {"minzoom": 0, "maxzoom": 8, "source": "world/lakes.shp"}
    with pytest.raises(ValueError, match="lakes reads world/lakes"):
        polar_data.tile_config(base, "polar/ocean.geojson")
    out = tmp_path / "cfg.json"
    polar_data.main(["tile-config", str(ROOT / "resources/tilemaker/config-openmaptiles.json"),
                     str(out), "polar/ocean.geojson"])
    written = json.loads(out.read_text())
    assert written == cfg
    # in the profile's order: tilemaker refuses a write_to naming a later layer
    assert list(written["layers"]) == [n for n in base["layers"] if n not in polar_data.REAL_WORLD_LAYERS + ("lakes",)]
    for i, (name, layer) in enumerate(written["layers"].items()):
        assert layer.get("write_to", name) in list(written["layers"])[:i + 1], name
