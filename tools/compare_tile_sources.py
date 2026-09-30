#!/usr/bin/env python3
"""Compare what two OpenMapTiles MBTiles offer for search and labels.

Used to weigh StreetZim's own tilemaker tiles against OpenFreeMap's (the
tiles openzim/maps ships); results are in docs/tile-sources.md.

    python tools/compare_tile_sources.py ours.mbtiles theirs.mbtiles \\
        --labels tilemaker openfreemap [--json out.json]

Only z14 tiles present in BOTH files are compared, so a different extract
area can't skew the numbers. Reported per layer: features and distinct
names. For POIs and streets: distinct names overall, names that look like
junk (only digits/punctuation, or under 3 characters), and names by class.
It also runs StreetZim's real search extraction on the shared tiles and
reports searchable records by type.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import os
import re
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

JUNK = re.compile(r"^[\W\d_]+$")


def _z14(path):
    con = sqlite3.connect(path)
    return {(x, y): d for x, y, d in con.execute(
        "SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level = 14")}


def _decode(blob):
    import mapbox_vector_tile as mvt
    try:
        blob = gzip.decompress(blob)
    except OSError:
        pass
    return mvt.decode(blob)


def _name(props):
    return props.get("name:latin") or props.get("name")


def layer_stats(tiles, keys):
    out = collections.defaultdict(lambda: {"features": 0, "names": set(),
                                           "by_class": collections.defaultdict(set)})
    for k in keys:
        for layer, content in _decode(tiles[k]).items():
            L = out[layer]
            for f in content["features"]:
                L["features"] += 1
                n = _name(f["properties"])
                if n:
                    L["names"].add(n)
                    L["by_class"][f["properties"].get("class", "")].add(n)
    return out


def search_counts(tiles, keys):
    """Run StreetZim's extraction on an MBTiles holding only the shared
    tiles. Returns {type: records}; streets also counted by distinct name."""
    from streetzim.search_extract import extract_searchable_features
    with tempfile.TemporaryDirectory() as tmp:
        mb = os.path.join(tmp, "shared.mbtiles")
        con = sqlite3.connect(mb)
        con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        con.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, "
                    "tile_row INT, tile_data BLOB)")
        con.executemany("INSERT INTO tiles VALUES (14, ?, ?, ?)",
                        [(x, y, tiles[(x, y)]) for x, y in keys])
        con.commit()
        con.close()
        path = extract_searchable_features(mbtiles_path=mb, output_dir=tmp)
        counts = collections.Counter()
        streets = set()
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                counts[r["type"]] += 1
                if r["type"] == "street":
                    streets.add(r["name"])
        counts["street (distinct names)"] = len(streets)
        return dict(counts)


def pct(a, b):
    return f"{(a - b) / b * 100:+.0f}%" if b else "n/a"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--labels", nargs=2, default=["A", "B"])
    ap.add_argument("--json")
    args = ap.parse_args()
    la, lb = args.labels
    A, B = _z14(args.a), _z14(args.b)
    keys = sorted(set(A) & set(B))
    print(f"shared z14 tiles: {len(keys)} ({la} {len(A)}, {lb} {len(B)})")
    sa, sb = layer_stats(A, keys), layer_stats(B, keys)
    report = {"shared_tiles": len(keys), "layers": {}, "search": {}}

    print(f"\n{'layer':22s} {'features ' + la:>16s} {lb:>12s}   {'names ' + la:>12s} {lb:>8s}  change")
    for layer in sorted(set(sa) | set(sb)):
        x, y = sa.get(layer), sb.get(layer)
        fx, fy = (x or {}).get("features", 0), (y or {}).get("features", 0)
        nx, ny = len((x or {}).get("names", ())), len((y or {}).get("names", ()))
        print(f"{layer:22s} {fx:16d} {fy:12d}   {nx:12d} {ny:8d}  {pct(nx, ny)}")
        report["layers"][layer] = {"features": [fx, fy], "names": [nx, ny]}

    for layer in ("poi", "transportation_name"):
        x, y = sa[layer]["names"], sb[layer]["names"]
        jx = {n for n in x if JUNK.match(n) or len(n) < 3}
        jy = {n for n in y if JUNK.match(n) or len(n) < 3}
        cx, cy = x - jx, y - jy
        print(f"\n{layer}: distinct names {la} {len(x)} vs {lb} {len(y)} ({pct(len(x), len(y))});"
              f" excluding junk-looking names {len(cx)} vs {len(cy)} ({pct(len(cx), len(cy))})")
        print(f"  only in {la}: {len(cx - cy)}   only in {lb}: {len(cy - cx)}   in both: {len(cx & cy)}")
        classes = collections.Counter()
        for cls, names in sa[layer]["by_class"].items():
            classes[cls] += len(names - sb[layer]["names"])
        print(f"  classes of names only in {la}: {classes.most_common(8)}")
        classes = collections.Counter()
        for cls, names in sb[layer]["by_class"].items():
            classes[cls] += len(names - sa[layer]["names"])
        print(f"  classes of names only in {lb}: {classes.most_common(8)}")
        report["layers"][layer].update({
            "names_excluding_junk": [len(cx), len(cy)],
            "only_a": len(cx - cy), "only_b": len(cy - cx), "both": len(cx & cy)})

    qa, qb = search_counts(A, keys), search_counts(B, keys)
    print("\nsearchable records (StreetZim extraction on the shared tiles)")
    for t in sorted(set(qa) | set(qb)):
        print(f"  {t:24s} {qa.get(t, 0):7d} {qb.get(t, 0):7d}  {pct(qa.get(t, 0), qb.get(t, 0))}")
    report["search"] = {la: qa, lb: qb}
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
