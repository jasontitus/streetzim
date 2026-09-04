#!/usr/bin/env python3
"""Regenerate search_cache/world-<date>.jsonl from a planet MBTiles.

The regional builds read named features (places, POIs, streets, water,
parks, peaks, airports) from ``search_cache/world.jsonl`` via
``--search-cache`` instead of scanning the 345M-tile world MBTiles on
every build. That cache is tied to the MBTiles it was extracted from
(see Caching.md "Search Features Cache"), so it must be regenerated
whenever the world tiles are regenerated from a new planet.

This is the same ``extract_searchable_features(mbtiles_path=...)`` pass
the world build runs; it just writes the result to a stable path.

Usage:
  venv-linux/bin/python3 cloud/build_search_cache.py \
      --mbtiles world-data/world-tiles-v3.mbtiles \
      --out search_cache/world-2026-08-31.jsonl
Then: ln -sfn world-2026-08-31.jsonl search_cache/world.jsonl
"""
import argparse
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import create_osm_zim  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mbtiles", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--tmpdir", default=os.environ.get("TMPDIR", "/storage/streetzim/tmp"))
    args = p.parse_args()

    if os.path.exists(args.out) and os.path.getsize(args.out) > 0:
        sys.exit(f"refusing to overwrite existing {args.out}")
    os.makedirs(args.tmpdir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="search_cache_", dir=args.tmpdir)
    t0 = time.time()
    print(f"extracting search features from {args.mbtiles} → {work}", flush=True)
    path = create_osm_zim.extract_searchable_features(
        mbtiles_path=args.mbtiles, output_dir=work)
    if not (isinstance(path, str) and os.path.isfile(path)):
        sys.exit(f"extract_searchable_features returned {path!r}, expected a jsonl path")
    n = sum(1 for _ in open(path, "rb"))
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    shutil.move(path, args.out)
    shutil.rmtree(work, ignore_errors=True)
    print(f"wrote {args.out}: {n} features, "
          f"{os.path.getsize(args.out)/1e9:.1f} GB in {(time.time()-t0)/60:.0f} min",
          flush=True)


if __name__ == "__main__":
    main()
