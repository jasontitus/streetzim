#!/usr/bin/env python3
"""Resume a world search-cache build from its dedup output.

build_search_cache.py runs the 2.5 h z14 tile scan, then dedups to
<tmp>/search_features.raw.jsonl, then annotates + sorts. If the tail is
interrupted (or was too slow and got killed), the raw file survives —
it is only unlinked after annotation completes — so only the tail needs
re-running. This does exactly that.

Usage:
  venv-linux/bin/python3 cloud/finish_search_cache.py \
      --raw tmp/search_cache_XXXX/search_features.raw.jsonl \
      --out search_cache/world-2026-08-31.jsonl
"""
import argparse, os, shutil, sys, time


def _streetzim_root():
    """The streetzim checkout (builder, data, ZIMs): $STREETZIM_ROOT, else the
    nearest directory above this file that holds create_osm_zim.py. (This file
    lives in ops/ and usually runs through a symlink at its old path.)"""
    env = os.environ.get("STREETZIM_ROOT")
    if env:
        return os.path.abspath(env)
    d = os.path.dirname(os.path.realpath(__file__))
    while d != os.path.dirname(d):
        if os.path.isfile(os.path.join(d, "create_osm_zim.py")):
            return d
        d = os.path.dirname(d)
    raise SystemExit("cannot find the streetzim checkout; set STREETZIM_ROOT")


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _streetzim_root())
import create_osm_zim  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if not os.path.isfile(a.raw) or os.path.getsize(a.raw) == 0:
        sys.exit(f"raw file missing/empty: {a.raw}")
    if os.path.exists(a.out) and os.path.getsize(a.out) > 0:
        sys.exit(f"refusing to overwrite {a.out}")
    work = os.path.dirname(os.path.abspath(a.raw))
    n = 0
    with open(a.raw, "rb") as f:
        for _ in f:
            n += 1
    print(f"{n:,} deduped features in {a.raw}", flush=True)
    t0 = time.time()
    path = create_osm_zim._finish_features_streaming(a.raw, work, n)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    shutil.move(path, a.out)
    print(f"wrote {a.out}: {os.path.getsize(a.out)/1e9:.1f} GB in {(time.time()-t0)/60:.0f} min", flush=True)


if __name__ == "__main__":
    main()
