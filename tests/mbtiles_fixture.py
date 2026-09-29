"""Small MBTiles files for tests, in either layout: a plain `tiles` table
(tilemaker) or OpenFreeMap's `tiles` view over tiles_shallow/tiles_data."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

META = {"name": "OpenFreeMap", "description": "https://openfreemap.org",
        "version": "3.16.0", "format": "pbf", "planetiler:version": "0.10.3",
        "osm_date": "2026-09-27"}


def make_mbtiles(path: Path, tiles: Iterable[tuple[int, int, int]], *,
                 ofm: bool = True, meta: dict[str, str] | None = None) -> Path:
    """An MBTiles holding XYZ `tiles`; each tile's data is "z/x/y"."""
    path = Path(path)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE metadata (name text, value text)")
    conn.executemany("INSERT INTO metadata VALUES (?, ?)", list((meta or META).items()))
    rows = [(z, x, (1 << z) - 1 - y, f"{z}/{x}/{y}".encode()) for z, x, y in tiles]
    if ofm:
        conn.executescript("""
            CREATE TABLE tiles_shallow (zoom_level integer, tile_column integer,
              tile_row integer, tile_data_id integer,
              primary key(zoom_level, tile_column, tile_row)) without rowid;
            CREATE TABLE tiles_data (tile_data_id integer primary key, tile_data blob);
            CREATE VIEW tiles AS SELECT tiles_shallow.zoom_level AS zoom_level,
              tiles_shallow.tile_column AS tile_column, tiles_shallow.tile_row AS tile_row,
              tiles_data.tile_data AS tile_data FROM tiles_shallow
              JOIN tiles_data ON tiles_shallow.tile_data_id = tiles_data.tile_data_id;
        """)
        conn.executemany("INSERT INTO tiles_data VALUES (?, ?)",
                         [(i, r[3]) for i, r in enumerate(rows)])
        conn.executemany("INSERT INTO tiles_shallow VALUES (?, ?, ?, ?)",
                         [(r[0], r[1], r[2], i) for i, r in enumerate(rows)])
    else:
        conn.execute("CREATE TABLE tiles (zoom_level integer, tile_column integer, "
                     "tile_row integer, tile_data blob)")
        conn.execute("CREATE UNIQUE INDEX tile_index ON tiles "
                     "(zoom_level, tile_column, tile_row)")
        conn.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def read_tiles(path: Path) -> set[tuple[int, int, int]]:
    """The XYZ tiles in an MBTiles."""
    conn = sqlite3.connect(str(path))
    try:
        return {(z, x, (1 << z) - 1 - r) for z, x, r in
                conn.execute("SELECT zoom_level, tile_column, tile_row FROM tiles")}
    finally:
        conn.close()
