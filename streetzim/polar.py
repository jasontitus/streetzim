"""A map of Antarctica with the South Pole on it.

Web Mercator ends at 85 S, so a map in it can never show the pole. A polar
map (create_osm_zim --projection antarctic-polar) has its data moved first:
every position goes to Antarctic polar stereographic (EPSG:3031) metres, and
those metres are read as Web Mercator metres. That gives each place a "map"
longitude and latitude near (0, 0): the pole at the centre, 0 E up, 90 E to
the right, 60 S a circle about 30 degrees out. Tiles, search records and the
routing graph are all cut from the moved data, and the viewer, which only
knows Web Mercator, draws Antarctica in its polar projection. The viewer
converts back wherever a real position shows (resources/viewer, 122-polar.js,
which mirrors the maths here).

    to_map(lon, lat)            -> (map_lon, map_lat)
    to_true(map_lon, map_lat)   -> (lon, lat)
"""
import math

A = 6378137.0                      # WGS 84
F = 1 / 298.257223563
E2 = F * (2 - F)
E = math.sqrt(E2)
LAT_TS = 71.0                      # standard parallel, degrees south
R = 6378137.0                      # the Web Mercator sphere


def _t(phi):
    """Snyder's t for a latitude `phi` (radians) counted towards the pole."""
    s = math.sin(phi)
    return math.tan(math.pi / 4 - phi / 2) / ((1 - E * s) / (1 + E * s)) ** (E / 2)


_PHI_C = math.radians(LAT_TS)
_MC = math.cos(_PHI_C) / math.sqrt(1 - E2 * math.sin(_PHI_C) ** 2)
_K = A * _MC / _t(_PHI_C)          # rho = _K * t


def to_polar(lon, lat):
    """EPSG:3031 metres: 0 deg E up, 90 deg E to the right."""
    rho = _K * _t(math.radians(-lat))
    lam = math.radians(lon)
    return rho * math.sin(lam), rho * math.cos(lam)


def from_polar(x, y):
    rho = math.hypot(x, y)
    if rho == 0:
        return 0.0, -90.0
    t = rho / _K
    chi = math.pi / 2 - 2 * math.atan(t)          # conformal latitude
    phi = chi
    for _ in range(8):
        s = math.sin(phi)
        phi = math.pi / 2 - 2 * math.atan(t * ((1 - E * s) / (1 + E * s)) ** (E / 2))
    return math.degrees(math.atan2(x, y)), -math.degrees(phi)


def merc_to_lonlat(x, y):
    return math.degrees(x / R), math.degrees(math.atan(math.sinh(y / R)))


def lonlat_to_merc(lon, lat):
    return R * math.radians(lon), R * math.asinh(math.tan(math.radians(lat)))


def to_map(lon, lat):
    return merc_to_lonlat(*to_polar(lon, lat))


def to_true(map_lon, map_lat):
    return from_polar(*lonlat_to_merc(map_lon, map_lat))


NAME = "antarctic-polar"
EDGE_LAT = -60.0                   # the map reaches this far north


def edge_ring(lat=EDGE_LAT, step=0.5):
    """The parallel `lat` as a closed ring of map positions."""
    n = int(round(360 / step))
    ring = [to_map(i * step, lat) for i in range(n)]
    return ring + [ring[0]]


def edge_mask(bbox, digits=5):
    """GeoJSON of what lies past the map's edge inside (and a little around)
    `bbox`: the viewer greys it, as it does the outside of a border cut."""
    w, s, e, n = bbox
    pad = max(e - w, n - s)
    outer = [[w - pad, s - pad], [e + pad, s - pad], [e + pad, n + pad], [w - pad, n + pad], [w - pad, s - pad]]
    hole = [[round(x, digits), round(y, digits)] for x, y in edge_ring()][::-1]
    return {"type": "Polygon", "coordinates": [outer, hole]}
