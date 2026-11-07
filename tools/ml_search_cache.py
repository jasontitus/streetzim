#!/usr/bin/env python3
"""A region's search cache from its own multilingual tiles
(docs/search-records.md, "A search cache from the region's own multilingual
tiles"): the search features of world-data/regions/<id>.ml.mbtiles, each
with its names in the tiles' languages (<id>.ml.mbtiles.langs), so a build
gives places and streets their names in other languages (`ln`).

    python tools/ml_search_cache.py REGION [--out DIR]

writes DIR/search_features.jsonl (and its .schema); DIR defaults to
/storage/streetzim/search_cache/ml-<today>/<REGION>. A build takes it as
STREETZIM_SEARCH_CACHE (a line in the runner's rebuild-par/env/<REGION>).
Seconds for a city, minutes for a country (Egypt 146 s, Ukraine 986 s).
The extraction forks workers: run it as this script, not pasted into
`python -c` (multiprocessing needs the __main__ guard below).
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGIONS = Path(os.environ.get("STREETZIM_ROOT", "/storage/streetzim")) / "world-data" / "regions"


def tile_languages(region: str, regions: Path = REGIONS) -> str:
    """The tiles' languages, as their .langs file lists them ("ar,de,…")."""
    langs = (regions / f"{region}.ml.mbtiles.langs").read_text(encoding="utf-8").strip()
    if not langs:
        raise ValueError(f"{region}.ml.mbtiles.langs is empty")
    return langs


def default_out(region: str, today: datetime.date | None = None) -> Path:
    day = (today or datetime.date.today()).isoformat()
    return Path("/storage/streetzim/search_cache") / f"ml-{day}" / region


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("region")
    ap.add_argument("--out", type=Path, help="the folder to write (default: search_cache/ml-<today>/<region>)")
    ap.add_argument("--regions", type=Path, default=REGIONS, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    mbtiles = a.regions / f"{a.region}.ml.mbtiles"
    if not mbtiles.is_file():
        print(f"no multilingual tiles: {mbtiles}", file=sys.stderr)
        return 1
    # Read by the extraction (search_extract._languages), so set before importing it.
    os.environ["STREETZIM_TILE_LANGUAGES"] = tile_languages(a.region, a.regions)
    out = a.out or default_out(a.region)
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    from streetzim.search_extract import extract_searchable_features
    t0 = time.time()
    path = extract_searchable_features(mbtiles_path=str(mbtiles), output_dir=str(out))
    print(f"{a.region}: {path} in {time.time() - t0:.0f} s; "
          f"STREETZIM_SEARCH_CACHE={path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
