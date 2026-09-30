#!/usr/bin/env python3
"""Derive per-region mbtiles from the world mbtiles in ONE sequential scan.

Generalises world-data/extract-region-mbtiles.py (which had nine regions
hard-coded) to every region in cloud/regions.tsv, and points at whichever
world file you pass.

Why bother: a regional build reads its mbtiles randomly during the
tile-add phase. Against the 114 GB world file on a spinning disk that
caps at ~120 tiles/s; against a ~1 GB regional slice the OS page-cache
holds the whole thing and it runs at 1600+/s. One sequential scan here
pays for itself many times over across 49 builds.

Sequential-by-rowid is deliberate: tilemaker writes zoom-major, so a
rowid scan reads the file in physical order — the one access pattern an
HDD is good at.

Usage:
  ./derive-region-mbtiles.py [--src world-data/world-tiles-v3.mbtiles]
                             [--only a,b] [--max-zoom 14]
Outputs world-data/regions/<id>.mbtiles, replacing any existing file
only once its own scan succeeds.
"""
import argparse
import os
import sqlite3
import sys
import time

import mercantile

ROOT = "/storage/streetzim"
DST_DIR = os.path.join(ROOT, "world-data", "regions")
TMP = os.path.join(ROOT, "tmp")


def load_regions(registry, only=None):
    out = {}
    with open(registry, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            rid, _name, bbox = parts[0], parts[1], parts[2]
            if only and rid not in only:
                continue
            out[rid] = tuple(float(v) for v in bbox.split(","))
    return out


def precompute_ranges(bbox, max_zoom):
    """{z: (min_col, max_col, min_tms_row, max_tms_row)} — source rows are TMS."""
    minlon, minlat, maxlon, maxlat = bbox
    ranges = {}
    for z in range(0, max_zoom + 1):
        tiles = list(mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=z))
        if not tiles:
            continue
        n = 1 << z
        ranges[z] = (min(t.x for t in tiles), max(t.x for t in tiles),
                     min(n - 1 - t.y for t in tiles), max(n - 1 - t.y for t in tiles))
    return ranges


def open_output(path):
    if os.path.exists(path):
        os.unlink(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute(f"PRAGMA temp_store_directory='{TMP}'")
    conn.execute("CREATE TABLE metadata (name text, value text, UNIQUE (name))")
    conn.execute("CREATE TABLE tiles (zoom_level integer, tile_column integer, "
                 "tile_row integer, tile_data blob)")
    return conn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(ROOT, "world-data", "world-tiles-v3.mbtiles"))
    ap.add_argument("--registry", default=os.path.join(ROOT, "cloud", "regions.tsv"))
    ap.add_argument("--only", default="")
    ap.add_argument("--max-zoom", type=int, default=14)
    ap.add_argument("--batch", type=int, default=5000)
    a = ap.parse_args()

    only = set(x for x in a.only.split(",") if x) or None
    regions = load_regions(a.registry, only)
    if not regions:
        sys.exit("no regions matched")
    if not os.path.exists(a.src):
        sys.exit(f"missing source mbtiles: {a.src}")
    os.environ["TMPDIR"] = TMP
    os.makedirs(DST_DIR, exist_ok=True)

    print(f"[1/4] per-zoom ranges for {len(regions)} regions (max-zoom {a.max_zoom})", flush=True)
    ranges = {rid: precompute_ranges(bbox, a.max_zoom) for rid, bbox in regions.items()}

    # Pivot to per-zoom lists so the hot loop only walks regions that
    # actually cover this row's zoom, and touches tuples not dicts.
    by_zoom = {}
    for rid, rz in ranges.items():
        for z, r in rz.items():
            by_zoom.setdefault(z, []).append((rid, r[0], r[1], r[2], r[3]))

    print(f"[2/4] opening {len(regions)} outputs under {DST_DIR}", flush=True)
    part = {rid: os.path.join(DST_DIR, f"{rid}.mbtiles.part") for rid in regions}
    conns = {rid: open_output(p) for rid, p in part.items()}

    src = sqlite3.connect(a.src)
    src.execute(f"PRAGMA temp_store_directory='{TMP}'")
    print("[3/4] copying metadata", flush=True)
    meta = src.execute("SELECT name, value FROM metadata").fetchall()
    for rid, conn in conns.items():
        conn.executemany("INSERT INTO metadata VALUES (?, ?)",
                         [(n, str(a.max_zoom) if n == "maxzoom" else v) for n, v in meta])
        conn.commit()

    print(f"[4/4] sequential scan of {os.path.basename(a.src)} -> {len(regions)} regions", flush=True)
    cur = src.execute("SELECT zoom_level, tile_column, tile_row, tile_data "
                      "FROM tiles WHERE zoom_level <= ? ORDER BY rowid", (a.max_zoom,))
    batches = {rid: [] for rid in regions}
    counts = {rid: 0 for rid in regions}
    rows = 0
    t0 = last = time.time()
    for z, x, tms, data in cur:
        rows += 1
        for rid, c0, c1, r0, r1 in by_zoom.get(z, ()):
            if c0 <= x <= c1 and r0 <= tms <= r1:
                b = batches[rid]
                b.append((z, x, tms, data))
                counts[rid] += 1
                if len(b) >= a.batch:
                    conns[rid].executemany("INSERT INTO tiles VALUES (?,?,?,?)", b)
                    b.clear()
        if rows % 2_000_000 == 0:
            now = time.time()
            if now - last > 30:
                print(f"      {rows:,} rows ({rows/(now-t0):.0f}/s)", flush=True)
                last = now

    print("      flushing", flush=True)
    for rid, b in batches.items():
        if b:
            conns[rid].executemany("INSERT INTO tiles VALUES (?,?,?,?)", b)
        conns[rid].commit()

    print("      indexing + promoting", flush=True)
    for rid, conn in conns.items():
        conn.execute("CREATE UNIQUE INDEX tile_index ON tiles "
                     "(zoom_level, tile_column, tile_row)")
        conn.commit()
        conn.close()
        final = os.path.join(DST_DIR, f"{rid}.mbtiles")
        # Only now replace whatever was there (often a symlink to the world
        # file, or the previous round's derived slice).
        if os.path.islink(final) or os.path.exists(final):
            os.unlink(final)
        os.rename(part[rid], final)
        sz = os.path.getsize(final) / 1e9
        print(f"      {rid:28s} {counts[rid]:>12,} tiles  {sz:6.2f} GB", flush=True)

    print(f"\nDone in {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
