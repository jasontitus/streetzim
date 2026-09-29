"""Audit every DEM source tile in ``terrain_cache/dem_sources/``.

Flags:
  * file opens + decodes (rasterio)
  * uniform content (vmin == vmax across all valid pixels) — the
    failure mode we suspect for Iran's 33-34°N 3D-mode band
  * near-uniform (vmax - vmin < 3 m)
  * all-nodata
  * suspicious size (< 1 MB is short; most real DEMs are 10-50 MB)
  * .tif + .tif.nodata collision (both exist for same coord)

Writes a JSON report to ``/tmp/dem_audit.json`` + prints a summary.
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from pathlib import Path

import numpy as np
import rasterio


ROOT = Path(__file__).resolve().parent.parent
DEM_DIR = ROOT / "terrain_cache" / "dem_sources"


def audit_one(fname: str) -> dict:
    p = DEM_DIR / fname
    size = p.stat().st_size
    try:
        ns = fname[4]
        lat = int(fname[5:7]) * (1 if ns == "N" else -1)
        ew = fname[8]
        lon = int(fname[9:12]) * (1 if ew == "E" else -1)
    except Exception:
        return {"fname": fname, "status": "bad_name", "size": size}
    nodata_collides = (DEM_DIR / (fname + ".nodata")).exists()
    try:
        with rasterio.open(p) as src:
            arr = src.read(1, masked=False)
            nodata_val = src.nodata
        if nodata_val is not None:
            mask = arr == nodata_val
            valid = arr[~mask]
        else:
            valid = arr.ravel()
        n_total = arr.size
        n_valid = int(valid.size)
        if n_valid == 0:
            return {"fname": fname, "lat": lat, "lon": lon, "size": size,
                    "status": "all_nodata", "nodata_collides": nodata_collides}
        vmin = int(valid.min())
        vmax = int(valid.max())
        if vmin == vmax:
            return {"fname": fname, "lat": lat, "lon": lon, "size": size,
                    "status": "uniform", "vmin": vmin, "vmax": vmax,
                    "n_valid": n_valid, "nodata_collides": nodata_collides}
        if vmax - vmin < 3:
            return {"fname": fname, "lat": lat, "lon": lon, "size": size,
                    "status": "near_uniform", "vmin": vmin, "vmax": vmax,
                    "n_valid": n_valid, "nodata_collides": nodata_collides}
        nodata_frac = 1 - n_valid / n_total
        return {"fname": fname, "lat": lat, "lon": lon, "size": size,
                "status": "ok", "vmin": vmin, "vmax": vmax,
                "nodata_frac": round(nodata_frac, 3),
                "nodata_collides": nodata_collides}
    except Exception as exc:
        return {"fname": fname, "lat": lat, "lon": lon, "size": size,
                "status": f"decode_err:{type(exc).__name__}",
                "err": str(exc)[:120]}


def main() -> int:
    tifs = sorted(f for f in os.listdir(DEM_DIR)
                  if f.startswith("dem_") and f.endswith(".tif"))
    print(f"auditing {len(tifs)} DEMs with thread pool…", flush=True)
    t0 = time.time()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for i, rec in enumerate(ex.map(audit_one, tifs, chunksize=100)):
            results.append(rec)
            if (i + 1) % 2000 == 0:
                print(f"  {i+1}/{len(tifs)} ({time.time()-t0:.0f}s)", flush=True)
    print(f"done in {time.time()-t0:.0f}s")
    print()
    cats = Counter(r["status"].split(":")[0] for r in results)
    for status, count in cats.most_common():
        print(f"  {status}: {count}")
    print()

    # Report non-ok over inhabited latitudes (|lat| ≤ 70)
    probs = [r for r in results if not r["status"].startswith("ok")]
    inhab_probs = [r for r in probs
                   if "lat" in r and abs(r["lat"]) <= 70]
    print(f"{len(probs)} non-ok DEMs; {len(inhab_probs)} at |lat| ≤ 70°")
    print()
    print("first 40 non-ok DEMs in inhabited latitudes:")
    for r in inhab_probs[:40]:
        desc = r["status"]
        if "vmin" in r:
            desc += f" ({r['vmin']}-{r['vmax']}m, size {r['size']/1e3:.0f}KB)"
        print(f"  N{r['lat']:+03d}E{r['lon']:+04d}  {r['fname']}  {desc}")

    # Target question: any uniform DEMs in Iran's 33°N band?
    iran_band = [r for r in results
                 if r.get("lat") in (33, 34) and 43 <= r.get("lon", 999) <= 63
                 and not r["status"].startswith("ok")]
    if iran_band:
        print()
        print(f"*** Iran 33–34°N band problems ({len(iran_band)}):")
        for r in iran_band:
            print(f"  {r}")

    out = ROOT.parent / "dem_audit.json"
    if not out.parent.exists():
        out = Path("/tmp/dem_audit.json")
    Path("/tmp/dem_audit.json").write_text(json.dumps(results, indent=2))
    print(f"\nfull report → /tmp/dem_audit.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
