"""Compact Natural Earth 1:50m lakes for the viewer's zoom 0-5 lake layer.

The tiles only carry lakes from z6 (resources/tilemaker/config-openmaptiles.json
"water" minzoom 6), so Baikal, Superior, Victoria... vanish when zoomed out.
Natural Earth is public domain. Source (not committed; cache/ is ignored):
  curl -fL -o cache/natural-earth/ne_50m_lakes.geojson \
    https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_lakes.geojson
  python3 tools/natural_earth/build_lowzoom_lakes.py \
    cache/natural-earth/ne_50m_lakes.geojson cache/natural-earth/lowzoom-lakes.json
then paste the JSON into _SZ_LAKES in resources/viewer/index.html.

Coordinates are rounded to 0.01 deg (~1 km,
under a pixel at z5) and each feature keeps only NE's min_zoom so small lakes
appear progressively, as they do on Natural-Earth-based basemaps.
"""
import json, sys

def q(pt):
    return [round(pt[0], 2), round(pt[1], 2)]

TOL = 0.01   # deg; a z5 pixel is ~0.043 deg of longitude

def dp(pts, tol):
    """Douglas-Peucker, iterative."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]; bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        L = (dx * dx + dy * dy) ** 0.5
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            d = abs(dy * px - dx * py + bx * ay - by * ax) / L if L else ((px-ax)**2 + (py-ay)**2) ** 0.5
            if d > best:
                best, bi = d, i
        if best > tol:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]

def ring(r):
    out = []
    for p in dp(r, TOL):
        p = q(p)
        if not out or out[-1] != p:
            out.append(p)
    if out and out[0] != out[-1]:
        out.append(out[0])
    return out if len(out) >= 4 else None

def poly(rings):
    rs = [x for x in (ring(r) for r in rings) if x]
    return rs if rs else None

src, dst = sys.argv[1], sys.argv[2]
feats = []
for f in json.load(open(src))["features"]:
    g = f["geometry"]
    if g["type"] == "Polygon":
        c = poly(g["coordinates"])
        geom = c and {"type": "Polygon", "coordinates": c}
    elif g["type"] == "MultiPolygon":
        c = [p for p in (poly(p) for p in g["coordinates"]) if p]
        geom = c and {"type": "MultiPolygon", "coordinates": c}
    else:
        geom = None
    if not geom:
        continue
    mz = f["properties"].get("min_zoom")
    if mz is not None and mz >= 6:
        continue            # the tiles take over at z6; never drawn
    feats.append({"type": "Feature", "properties": {"z": float(mz) if mz is not None else 0.0},
                  "geometry": geom})
# Output: [[min_zoom, [ring, ring...]], ...] per polygon, each ring a
# polyline-encoded string (precision 2, i.e. 0.01 deg). ~4x smaller than
# GeoJSON; the viewer decodes it (_szDecodeLakes in index.html).
def enc_num(v):
    v = ~(v << 1) if v < 0 else (v << 1)
    out = ""
    while v >= 0x20:
        out += chr((0x20 | (v & 0x1f)) + 63)
        v >>= 5
    return out + chr(v + 63)

def enc_ring(r):
    px = py = 0
    out = ""
    for lon, lat in r:
        x, y = round(lon * 100), round(lat * 100)
        out += enc_num(y - py) + enc_num(x - px)
        px, py = x, y
    return out

polys = []
for f in feats:
    g = f["geometry"]
    parts = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
    for rings in parts:
        polys.append([f["properties"]["z"], [enc_ring(r) for r in rings]])
out = json.dumps(polys, separators=(",", ":"))
open(dst, "w").write(out)
print(f"{len(feats)} lakes, {len(polys)} polygons, {len(out)} bytes")
