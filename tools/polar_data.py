#!/usr/bin/env python3
"""The inputs of a polar map (streetzim/polar.py, create_osm_zim --projection).

A polar map is built from data that has already been moved to its "map"
positions. Three steps make that data from a real extract of everything
south of 60 S:

    polar_data.py move REAL.osm.pbf MOVED.osm.pbf
        Every node at its map position. Ways and relations are copied.
    polar_data.py ocean MOVED.osm.pbf OCEAN.geojson
        The sea. The tile profile's sea comes from a world file of water
        polygons in real positions, which is of no use here: this one is
        the disc out to 60 S less the land the extract's coastline closes,
        cut into pieces small enough for a tile to clip.
    polar_data.py tile-config BASE.json OUT.json OCEAN-PATH
        The tile profile's layer file with the sea read from OCEAN-PATH (as
        tilemaker will see it) and without the layers that come from other
        real-position world files (urban areas, ice shelves, glaciers).

tilemaker then runs on MOVED.osm.pbf with OUT.json and the box
-31,-30,31,30, and create_osm_zim on its tiles with --projection.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from streetzim import polar  # noqa: E402

# Nothing north of this belongs on the map; a stray node there (a relation
# member far away) would land outside the Web Mercator square.
NORTH_LIMIT = -45.0
MAX_VERTS = 4000
# Layers of the tile profile read from world files in real positions.
REAL_WORLD_LAYERS = ("urban_areas", "ice_shelf", "glacier")


def move(src, dst):
    """Copy the extract `src` to `dst` with every node moved; (moved, left out)."""
    import osmium

    class Mover(osmium.SimpleHandler):
        def __init__(self, writer):
            super().__init__()
            self.w = writer
            self.moved = self.left_out = 0

        def node(self, n):
            lon, lat = n.location.lon, n.location.lat
            if lat > NORTH_LIMIT:
                self.left_out += 1
                return
            self.w.add_node(n.replace(location=polar.to_map(lon, lat)))
            self.moved += 1

        def way(self, w):
            self.w.add_way(w)

        def relation(self, r):
            self.w.add_relation(r)

    # The writer takes the format from the name's ending.
    part = os.path.join(os.path.dirname(dst) or ".", "part." + os.path.basename(dst))
    if os.path.exists(part):
        os.unlink(part)
    writer = osmium.SimpleWriter(part)
    try:
        h = Mover(writer)
        h.apply_file(src)
    finally:
        writer.close()
    os.replace(part, dst)
    return h.moved, h.left_out


def _split(geom, out, depth=0):
    import shapely
    from shapely.geometry import box
    if geom.is_empty:
        return
    if shapely.get_num_coordinates(geom) <= MAX_VERTS or depth > 12:
        for g in getattr(geom, "geoms", [geom]):
            if g.geom_type == "Polygon" and g.area > 0:
                out.append(g)
        return
    x0, y0, x1, y1 = geom.bounds
    xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
    for b in (box(x0, y0, xm, ym), box(xm, y0, x1, ym), box(x0, ym, xm, y1), box(xm, ym, x1, y1)):
        _split(geom.intersection(b), out, depth + 1)


def sea(coastlines):
    """The sea's polygons, from coastline ways given as lists of map
    positions. Land is every face the ways close."""
    import shapely
    from shapely.geometry import LineString
    from shapely.ops import polygonize, unary_union
    disc = shapely.Polygon(polar.edge_ring())
    lines = [LineString(p) for p in coastlines if len(p) >= 2]
    land = unary_union(list(polygonize(unary_union(lines)))) if lines else shapely.Polygon()
    parts = []
    _split(disc.difference(land), parts)
    return parts, land


def ocean(src, dst):
    """The sea of the moved extract `src`, as GeoJSON at `dst`; (pieces,
    whether the land holds the pole)."""
    import osmium
    from shapely.geometry import Point, mapping

    class Coast(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def way(self, w):
            if w.tags.get("natural") != "coastline":
                return
            self.lines.append([(round(n.lon, 7), round(n.lat, 7))
                               for n in w.nodes if n.location.valid()])

    h = Coast()
    h.apply_file(src, locations=True)
    parts, land = sea(h.lines)
    part = dst + ".part"
    with open(part, "w", encoding="utf-8") as f:
        f.write('{"type":"FeatureCollection","features":[\n')
        for i, g in enumerate(parts):
            f.write(("," if i else "") + json.dumps(
                {"type": "Feature", "properties": {}, "geometry": mapping(g)}) + "\n")
        f.write("]}\n")
    os.replace(part, dst)
    return len(parts), bool(land.contains(Point(0, 0)))


def tile_config(base, ocean_path):
    """The layer file `base` (a dict) for a polar map."""
    cfg = json.loads(json.dumps(base))
    layers = cfg["layers"]
    if "ocean" not in layers:
        raise ValueError("the tile profile has no ocean layer")
    layers["ocean"]["source"] = ocean_path
    for name in REAL_WORLD_LAYERS:
        layers.pop(name, None)
    for name, layer in layers.items():
        if name != "ocean" and "source" in layer:
            raise ValueError(f"layer {name} reads {layer['source']}: a world file "
                             "in real positions has no place on a polar map")
    return cfg


def write_tile_config(base_path, out_path, ocean_path):
    """tile_config() of the layer file at `base_path`, written to `out_path`
    with the layers in the profile's order: a layer's write_to names one
    that comes before it."""
    with open(base_path, encoding="utf-8") as f:
        cfg = tile_config(json.load(f), ocean_path)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 3 and argv[0] == "move":
        moved, left_out = move(argv[1], argv[2])
        print(f"moved {moved} nodes, left out {left_out} north of {-NORTH_LIMIT:g} S", flush=True)
        if not moved:
            raise SystemExit("no node south of the limit: not an Antarctic extract")
    elif len(argv) == 3 and argv[0] == "ocean":
        pieces, pole = ocean(argv[1], argv[2])
        print(f"sea in {pieces} pieces; the land holds the pole: {pole}", flush=True)
        if not pole:
            raise SystemExit("the coastline does not close around the pole: the sea would cover the continent")
    elif len(argv) == 4 and argv[0] == "tile-config":
        write_tile_config(argv[1], argv[2], argv[3])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
