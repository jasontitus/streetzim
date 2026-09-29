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
to copy that country's tiles. OpenFreeMap's layout (a tiles view over
tiles_shallow and tiles_data, each distinct tile stored once) is kept.
The file's `bounds` metadata is not trusted: an area is always cut.

Stdlib only (streetzim.cli imports it).
"""
from __future__ import annotations

import math
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
    return {k: clean(v, 200) for k, v in rec.items() if v}


def clean(value: str, limit: int = 80) -> str:
    """A metadata value fit for one line of ZIM metadata: control characters
    and runs of spaces collapsed, at most `limit` characters."""
    text = " ".join("".join(c if c.isprintable() else " " for c in value).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"


def license_text(meta: dict[str, str]) -> str:
    """The License metadata's credit for the tiles."""
    home = clean(meta.get("description", ""), 100)
    home = f" ({home})" if home.startswith(("http://", "https://")) and " " not in home else ""
    return f"Vector tiles: {clean(meta.get('name', '')) or 'MBTiles'}{home}"


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


_WHERE = "WHERE zoom_level = ? AND tile_column = ? AND tile_row BETWEEN ? AND ?"
_SELECT = "SELECT zoom_level, tile_column, tile_row, tile_data FROM src.tiles " + _WHERE
# OpenFreeMap's layout: tiles is a view over tiles_shallow (the key and a
# tile_data_id) and tiles_data (each distinct tile once).
_SELECT_SHALLOW = ("SELECT zoom_level, tile_column, tile_row, tile_data_id "
                   "FROM src.tiles_shallow " + _WHERE)
_COPY_DATA = ("INSERT INTO main.tiles_data SELECT d.tile_data_id, d.tile_data FROM "
              "(SELECT DISTINCT tile_data_id AS id FROM main.tiles_shallow ORDER BY id) AS s "
              "CROSS JOIN src.tiles_data AS d ON d.tile_data_id = s.id")
_DEDUP_SCHEMA = """
CREATE TABLE tiles_shallow (zoom_level integer, tile_column integer, tile_row integer,
  tile_data_id integer, primary key(zoom_level, tile_column, tile_row)) without rowid;
CREATE TABLE tiles_data (tile_data_id integer primary key, tile_data blob);
CREATE VIEW tiles AS SELECT tiles_shallow.zoom_level AS zoom_level,
  tiles_shallow.tile_column AS tile_column, tiles_shallow.tile_row AS tile_row,
  tiles_data.tile_data AS tile_data FROM tiles_shallow
  JOIN tiles_data ON tiles_shallow.tile_data_id = tiles_data.tile_data_id;
"""
_PLAIN_SCHEMA = """
CREATE TABLE tiles (zoom_level integer, tile_column integer, tile_row integer,
  tile_data blob, primary key(zoom_level, tile_column, tile_row)) without rowid;
"""


def column_queries(bbox: Sequence[float], min_zoom: int = 0,
                   max_zoom: int = MAX_ZOOM) -> list[tuple[int, int, int, int]]:
    """(zoom, column, first TMS row, last TMS row) for each column to read,
    in key order."""
    out: list[tuple[int, int, int, int]] = []
    for z in range(min_zoom, max_zoom + 1):
        n = 1 << z
        for c0, c1, r0, r1 in tile_ranges(bbox, z):
            out += [(z, x, n - 1 - r1, n - 1 - r0) for x in range(c0, c1 + 1)]
    return out


def _attach(main_uri: str, src: Path) -> sqlite3.Connection:
    """A connection to `main_uri` with `src` attached read-only as "src".
    The new database takes src's text encoding (tilemaker writes UTF-16
    files, and SQLite attaches only databases of the main one's encoding)."""
    probe = _connect(src)
    try:
        encoding = str(probe.execute("PRAGMA encoding").fetchone()[0])
    finally:
        probe.close()
    conn = sqlite3.connect(main_uri, uri=True)
    try:
        conn.execute(f"PRAGMA encoding = '{encoding}'")
        conn.execute("ATTACH DATABASE ? AS src", (_ro_uri(src),))
    except BaseException:
        conn.close()
        raise
    return conn


def _deduplicated(conn: sqlite3.Connection) -> bool:
    """Whether the attached `src` has OpenFreeMap's tiles_shallow/tiles_data."""
    cols: dict[str, set[str]] = {}
    for t in ("tiles_shallow", "tiles_data"):
        cols[t] = {str(r[1]) for r in conn.execute(f"PRAGMA src.table_info({t})")}
    return ({"zoom_level", "tile_column", "tile_row", "tile_data_id"} <= cols["tiles_shallow"]
            and {"tile_data_id", "tile_data"} <= cols["tiles_data"])


def query_plan(path: Path) -> list[str]:
    """SQLite's plan for the cut's reads of `path` (for the docs and tests):
    one column's tiles, and for the deduplicated layout the tile data."""
    conn = _attach("file::memory:", path)
    try:
        args = (14, 0, 0, 0)
        if not _deduplicated(conn):
            return [str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + _SELECT, args)]
        conn.executescript(_DEDUP_SCHEMA)
        return [str(r[-1]) for q in (_SELECT_SHALLOW, _COPY_DATA)
                for r in conn.execute("EXPLAIN QUERY PLAN " + q,
                                      args if q is _SELECT_SHALLOW else ())]
    finally:
        conn.close()


def cut(src: Path, dest: Path, bbox: Sequence[float], *, min_zoom: int = 0,
        max_zoom: int = MAX_ZOOM) -> dict[int, int]:
    """Write to `dest` an MBTiles holding the tiles of `src` that touch
    `bbox`, z`min_zoom` to z`max_zoom`, and its metadata. OpenFreeMap's
    layout is kept, with each distinct tile stored once (its ocean and land
    tiles repeat); other files get a plain tiles table. Returns the tile
    count per zoom. Nothing is left behind if it fails."""
    dest = Path(dest)
    dest.unlink(missing_ok=True)
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    counts: dict[int, int] = {}
    try:
        conn = _attach(part.resolve().as_uri(), src)
        try:
            conn.execute("PRAGMA journal_mode = OFF")
            conn.execute("PRAGMA synchronous = OFF")
            dedup = _deduplicated(conn)
            conn.executescript(_DEDUP_SCHEMA if dedup else _PLAIN_SCHEMA)
            conn.execute("CREATE TABLE metadata (name text, value text)")
            conn.execute("INSERT INTO metadata SELECT name, value FROM src.metadata")
            conn.execute("CREATE UNIQUE INDEX name ON metadata (name)")
            insert = ("INSERT INTO main.tiles_shallow " + _SELECT_SHALLOW if dedup
                      else "INSERT INTO main.tiles " + _SELECT)
            queries = column_queries(bbox, min_zoom, max_zoom)
            for z in range(min_zoom, max_zoom + 1):
                before = conn.total_changes
                conn.executemany(insert, [q for q in queries if q[0] == z])
                counts[z] = conn.total_changes - before
            if dedup:
                conn.execute(_COPY_DATA)
            conn.commit()
        finally:
            conn.close()
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return counts
