"""Ready-made OpenMapTiles MBTiles as a tile source (`--mbtiles`,
`--mbtiles-url`): checking a file is an MBTiles, describing where it came
from, and cutting it to the area.

The cut keeps the tiles that touch the area's bounding box, z0 to z14: the
rule maps2zim's TileFilter.tile_intersects applies (a tile's edges count as
touching). A box across the antimeridian is cut as its two sides
(streetzim/area.py). It never reads the whole file: each column of the area
is one index search on the tiles' (zoom_level, tile_column, tile_row) key,
so its cost follows the area's tile count, not the file's. OpenFreeMap's
planet (about 276 million tiles) is cut to a country in the time it takes
to copy that country's tiles.

Stdlib only (streetzim.cli imports it).
"""
from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from streetzim import area

SQLITE_MAGIC = b"SQLite format 3\x00"
MAX_ZOOM = 14
# Web Mercator's latitude limit: rows stop there.
_MERC_LAT = math.degrees(math.atan(math.sinh(math.pi)))

TileRange = tuple[int, int, int, int]       # zoom, column, first and last TMS row


def looks_like_sqlite(head: bytes) -> bool:
    """Whether the first bytes of a file are an SQLite header."""
    return head[:len(SQLITE_MAGIC)] == SQLITE_MAGIC


def _ro_uri(path: Path) -> str:
    return f"{Path(path).resolve().as_uri()}?mode=ro"


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(_ro_uri(path), uri=True)


def check(path: Path) -> dict[str, str]:
    """The MBTiles metadata of `path`; ValueError unless it is an MBTiles
    (an SQLite file with a `tiles` table or view and a `metadata` table)."""
    path = Path(path)
    with open(path, "rb") as f:
        if not looks_like_sqlite(f.read(len(SQLITE_MAGIC))):
            raise ValueError(f"{path} is not an MBTiles file (not SQLite)")
    try:
        conn = _connect(path)
        try:
            kinds = dict(conn.execute(
                "SELECT name, type FROM sqlite_master WHERE name IN ('tiles', 'metadata')"
            ).fetchall())
            if kinds.get("tiles") not in ("table", "view") or kinds.get("metadata") not in (
                    "table", "view"):
                raise ValueError(f"{path} is not an MBTiles file (no tiles and "
                                 "metadata tables)")
            rows: list[tuple[object, object]] = conn.execute(
                "SELECT name, value FROM metadata").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        raise ValueError(f"{path} is not a readable MBTiles file: {e}") from e
    return {str(k): str(v) for k, v in rows if k is not None and v is not None}


def describe(meta: dict[str, str]) -> str:
    """One line for the build log: what the tiles are and how fresh."""
    parts = [meta.get("name") or "unnamed"]
    if meta.get("version"):
        parts.append(f"version {meta['version']}")
    if meta.get("planetiler:version"):
        parts.append(f"planetiler {meta['planetiler:version']}")
    if osm_date(meta):
        parts.append(f"OSM data {osm_date(meta)}")
    if meta.get("bounds"):
        parts.append(f"bounds {meta['bounds']}")
    return ", ".join(parts)


def osm_date(meta: dict[str, str]) -> str:
    """The date of the OSM data in the tiles, when the file says (planetiler
    writes osm_date and the replication timestamp)."""
    stamp = meta.get("osm_date") or meta.get("planetiler:osm:osmosisreplicationtime", "")
    return stamp[:10]


def source_record(meta: dict[str, str], url: str | None = None) -> dict[str, str]:
    """What the ZIM records about its tiles (map-config.json's tileSource)."""
    rec = {"name": meta.get("name", ""), "version": meta.get("version", ""),
           "osmDate": osm_date(meta)}
    if meta.get("planetiler:version"):
        rec["generator"] = f"planetiler {meta['planetiler:version']}"
    if meta.get("planetiler:buildtime"):
        rec["buildTime"] = meta["planetiler:buildtime"]
    if meta.get("description", "").startswith(("http://", "https://")):
        rec["homepage"] = meta["description"]
    if url:
        rec["url"] = url
    return {k: v for k, v in rec.items() if v}


def license_text(meta: dict[str, str]) -> str:
    """The License metadata's credit for the tiles."""
    home = meta.get("description", "")
    home = f" ({home})" if home.startswith(("http://", "https://")) else ""
    return f"Vector tiles: {meta.get('name') or 'MBTiles'}{home}"


# ---------------------------------------------------------------- the cut


def _west(x: int, n: int) -> float:
    return (x / n) * 360.0 - 180.0


def _east(x: int, n: int) -> float:
    return ((x + 1) / n) * 360.0 - 180.0


def _north(y: int, n: int) -> float:
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))


def _south(y: int, n: int) -> float:
    return _north(y + 1, n)


def _columns(w: float, e: float, n: int) -> tuple[int, int] | None:
    """Columns whose tile touches [w, e] (w <= e, both in [-180, 180])."""
    lo = min(max(int((w + 180.0) / 360.0 * n) - 1, 0), n - 1)
    while lo < n - 1 and _east(lo, n) < w:
        lo += 1
    while lo > 0 and _east(lo - 1, n) >= w:
        lo -= 1
    hi = min(max(int((e + 180.0) / 360.0 * n) + 1, 0), n - 1)
    while hi > 0 and _west(hi, n) > e:
        hi -= 1
    while hi < n - 1 and _west(hi + 1, n) <= e:
        hi += 1
    if _east(lo, n) < w or _west(hi, n) > e or lo > hi:
        return None
    return lo, hi


def _row(lat: float, n: int) -> int:
    lat = max(min(lat, _MERC_LAT), -_MERC_LAT)
    t = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return min(max(int((1 - t / math.pi) / 2 * n), 0), n - 1)


def _rows(s: float, nth: float, n: int) -> tuple[int, int] | None:
    """XYZ rows whose tile touches [s, nth]."""
    lo = _row(nth, n)
    while lo < n - 1 and _south(lo, n) > nth:
        lo += 1
    while lo > 0 and _south(lo - 1, n) <= nth:
        lo -= 1
    hi = _row(s, n)
    while hi > 0 and _north(hi, n) < s:
        hi -= 1
    while hi < n - 1 and _north(hi + 1, n) >= s:
        hi += 1
    if _south(lo, n) > nth or _north(hi, n) < s or lo > hi:
        return None
    return lo, hi


def tile_ranges(bbox: Sequence[float], zoom: int) -> list[tuple[int, int, int, int]]:
    """The tiles at `zoom` touching `bbox` (unwrapped, streetzim/area.py), as
    (first column, last column, first XYZ row, last XYZ row) blocks: one, or
    two across the antimeridian (joined when they meet)."""
    n = 1 << zoom
    b = area.normalize(bbox)
    rows = _rows(b[1], b[3], n)
    if rows is None:
        return []
    cols = sorted(c for c in (_columns(p[0], p[2], n) for p in area.split(b)) if c)
    merged: list[tuple[int, int]] = []
    for lo, hi in cols:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return [(lo, hi, rows[0], rows[1]) for lo, hi in merged]


def tile_touches(bbox: Sequence[float], z: int, x: int, y: int) -> bool:
    """Whether XYZ tile z/x/y touches `bbox` (what tile_ranges selects)."""
    return any(c0 <= x <= c1 and r0 <= y <= r1 for c0, c1, r0, r1 in tile_ranges(bbox, z))


def _parse_bounds(value: str) -> tuple[float, float, float, float] | None:
    parts = [p for p in re.split(r"[,\s]+", value.strip()) if p]
    if len(parts) != 4:
        return None
    try:
        w, s, e, n = (float(p) for p in parts)
    except ValueError:
        return None
    return w, s, e, n


def covers_more(meta: dict[str, str], bbox: Sequence[float]) -> bool:
    """Whether the MBTiles may hold tiles beyond `bbox`: True unless its
    `bounds` metadata lies inside the box (then every tile touches it)."""
    bounds = _parse_bounds(meta.get("bounds", ""))
    if bounds is None:
        return True
    w, s, e, n = bounds
    b = area.normalize(bbox)
    if not (b[1] <= s and n <= b[3]) or w > e:
        return True
    ww, ee = area.unwrap_lon(b, w), area.unwrap_lon(b, e)
    return not (b[0] <= ww <= ee <= b[2])


_SELECT = ("SELECT zoom_level, tile_column, tile_row, tile_data FROM src.tiles "
           "WHERE zoom_level = ? AND tile_column = ? AND tile_row BETWEEN ? AND ?")


def column_queries(bbox: Sequence[float], min_zoom: int = 0,
                   max_zoom: int = MAX_ZOOM) -> list[tuple[int, int, int, int]]:
    """(zoom, column, first TMS row, last TMS row) for each column to read."""
    out: list[tuple[int, int, int, int]] = []
    for z in range(min_zoom, max_zoom + 1):
        n = 1 << z
        for c0, c1, r0, r1 in tile_ranges(bbox, z):
            out += [(z, x, n - 1 - r1, n - 1 - r0) for x in range(c0, c1 + 1)]
    return out


def query_plan(path: Path) -> list[str]:
    """SQLite's plan for one column read of the cut (for the docs and tests)."""
    conn = sqlite3.connect("file::memory:", uri=True)
    try:
        conn.execute("ATTACH DATABASE ? AS src", (_ro_uri(path),))
        return [str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + _SELECT,
                                                 (14, 0, 0, 0))]
    finally:
        conn.close()


def cut(src: Path, dest: Path, bbox: Sequence[float], *, min_zoom: int = 0,
        max_zoom: int = MAX_ZOOM) -> dict[int, int]:
    """Write to `dest` a plain MBTiles holding the tiles of `src` that touch
    `bbox`, z`min_zoom` to z`max_zoom`, and its metadata. Returns the tile
    count per zoom."""
    dest = Path(dest)
    dest.unlink(missing_ok=True)
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    conn = sqlite3.connect(part.resolve().as_uri(), uri=True)
    counts: dict[int, int] = {}
    try:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("ATTACH DATABASE ? AS src", (_ro_uri(src),))
        conn.execute("CREATE TABLE metadata (name text, value text)")
        conn.execute("CREATE TABLE tiles (zoom_level integer, tile_column integer, "
                     "tile_row integer, tile_data blob)")
        conn.execute("INSERT INTO metadata SELECT name, value FROM src.metadata")
        queries = column_queries(bbox, min_zoom, max_zoom)
        for z in range(min_zoom, max_zoom + 1):
            before = conn.total_changes
            conn.executemany("INSERT INTO main.tiles " + _SELECT,
                             [q for q in queries if q[0] == z])
            counts[z] = conn.total_changes - before
        conn.execute("CREATE UNIQUE INDEX name ON metadata (name)")
        conn.execute("CREATE UNIQUE INDEX tile_index ON tiles "
                     "(zoom_level, tile_column, tile_row)")
        conn.commit()
    finally:
        conn.close()
    part.replace(dest)
    return counts
