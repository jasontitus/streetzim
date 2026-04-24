"""Fast decode check — verify every recently-touched terrain tile
decodes cleanly. Catches truncated WEBP files left behind by a
killed-mid-write regen worker.

Uses multiprocessing, so needs to be a proper script file (not stdin)
— the workers import it back to find the worker function.
"""
from __future__ import annotations

import glob
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor as Pool
from pathlib import Path

TERRAIN = Path("/Users/jasontitus/experiments/streetzim/terrain_cache")


def check(path: str):
    from PIL import Image
    try:
        im = Image.open(path)
        im.load()  # force full decode; truncated files raise here
        return None
    except Exception as exc:
        return (path, str(exc))


def main() -> int:
    cutoff = time.time() - 6 * 3600
    if len(sys.argv) > 1:
        try:
            cutoff = time.time() - float(sys.argv[1]) * 3600
        except ValueError:
            print(f"usage: {sys.argv[0]} [hours=6]", file=sys.stderr)
            return 2

    jobs = []
    for z in range(0, 13):
        for x_dir in glob.glob(f"{TERRAIN}/{z}/*"):
            try:
                int(os.path.basename(x_dir))
            except ValueError:
                continue
            for f in glob.glob(f"{x_dir}/*.webp"):
                try:
                    if os.path.getmtime(f) >= cutoff:
                        jobs.append(f)
                except OSError:
                    pass

    print(f"checking {len(jobs):,} tiles touched in last "
          f"{(time.time() - cutoff) / 3600:.1f} h...")

    bad = []
    t0 = time.time()
    with Pool(max_workers=16) as pool:
        for r in pool.map(check, jobs, chunksize=256):
            if r:
                bad.append(r)
    dt = time.time() - t0
    print(f"done in {dt:.1f}s ({len(jobs)/max(0.001, dt):,.0f}/s); "
          f"{len(bad)} decode failures")
    for p, e in bad[:50]:
        print(f"  {p}: {e}")

    # Write the bad list so a follow-up `xargs rm -f` can delete, then
    # verify_terrain_freshness --regenerate will rewrite them atomically.
    if bad:
        out = Path(__file__).resolve().parent.parent / ".decode-errors.txt"
        out.write_text("\n".join(p for p, _ in bad))
        print(f"wrote {len(bad)} paths → {out.name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
