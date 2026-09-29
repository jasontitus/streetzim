#!/usr/bin/env python3
"""Download the newest OpenFreeMap MBTiles for an area (the tiles openzim/maps
uses), so a ZIM can be built without running tilemaker:

    python scripts/fetch-openfreemap-mbtiles.py monaco -o monaco.mbtiles
    python create_osm_zim.py --mbtiles monaco.mbtiles --bbox 7.40,43.72,7.44,43.76 \\
        --name Monaco -o osm-monaco.zim

OpenFreeMap publishes `monaco` (a small test area) and `planet` (~90 GB).
With the planet file, `--bbox` selects the region, the same way maps2zim
cuts it. Routing still needs an OSM PBF (`--pbf` or `--geofabrik`), because
vector tiles don't carry the road graph.

The newest build is found the same way maps2zim finds it: the latest
`areas/<area>/<timestamp>_*/tiles.mbtiles` line in files.txt.
"""
from __future__ import annotations

import argparse
import shutil
import urllib.request

BASE = "https://btrfs.openfreemap.com"
# The server answers Python's default "Python-urllib/x.y" agent with 403.
HEADERS = {"User-Agent": "streetzim (+https://github.com/jasontitus/streetzim)"}


def _open(url: str, timeout: int):
    return urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS),
                                  timeout=timeout)


def latest_mbtiles_url(area: str) -> str:
    with _open(f"{BASE}/files.txt", 60) as resp:
        lines = resp.read().decode("utf-8").split("\n")
    best = None
    for line in lines:
        parts = line.strip().split("/")
        if (len(parts) == 4 and parts[0] == "areas" and parts[1] == area
                and parts[3] == "tiles.mbtiles"):
            stamp = parts[2].split("_")[0]
            if best is None or stamp > best[0]:
                best = (stamp, line.strip())
    if best is None:
        raise SystemExit(f"no tiles.mbtiles for area {area!r} in {BASE}/files.txt")
    return f"{BASE}/{best[1]}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("area", help="OpenFreeMap area: monaco or planet")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()
    url = latest_mbtiles_url(args.area)
    print(f"downloading {url}", flush=True)
    part = args.output + ".part"
    with _open(url, 120) as resp, open(part, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    shutil.move(part, args.output)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
