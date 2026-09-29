#!/usr/bin/env python3
"""Per-region search caches from the world search jsonl, one sequential pass.

Generalises world-data/extract-region-search.py (nine hard-coded regions,
March source) to every region in cloud/regions.tsv and an explicit --src.
A regional build passes --search-cache regions/<id>.search.jsonl; it can
bbox-filter the whole 18 GB world file itself, but that is a full scan per
build, so do it once here for all 49.

Usage: ./derive-region-search.py --src search_cache/world-2026-08-31.jsonl [--only a,b]
Writes world-data/regions/<id>.search.jsonl via .part + rename (replacing
symlinks to the world file), only once the scan has completed.
"""
import argparse, json, os, sys, time

ROOT = "/storage/streetzim"
DST_DIR = os.path.join(ROOT, "world-data", "regions")


def load_regions(registry, only=None):
    out = {}
    with open(registry, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            rid, bbox = parts[0], parts[2]
            if only and rid not in only:
                continue
            out[rid] = tuple(float(v) for v in bbox.split(","))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--registry", default=os.path.join(ROOT, "cloud", "regions.tsv"))
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    only = set(x for x in a.only.split(",") if x) or None
    regions = load_regions(a.registry, only)
    if not regions:
        sys.exit("no regions matched")
    if not os.path.isfile(a.src) or os.path.getsize(a.src) == 0:
        sys.exit(f"source missing/empty: {a.src}")
    os.makedirs(DST_DIR, exist_ok=True)

    # (rid, minlon, minlat, maxlon, maxlat) tuples: the hot loop is
    # 123M lines x 49 regions, keep it to tuple indexing.
    boxes = [(rid, b[0], b[1], b[2], b[3]) for rid, b in regions.items()]
    part = {rid: os.path.join(DST_DIR, f"{rid}.search.jsonl.part") for rid in regions}
    outs = {rid: open(p, "w", encoding="utf-8") for rid, p in part.items()}
    counts = {rid: 0 for rid in regions}
    total = bad = 0
    t0 = last = time.time()
    print(f"scanning {a.src} -> {len(regions)} regions", flush=True)
    with open(a.src, encoding="utf-8") as f:
        for line in f:
            total += 1
            try:
                obj = json.loads(line)
                lat, lon = obj["lat"], obj["lon"]
            except Exception:
                bad += 1
                continue
            for rid, mnlon, mnlat, mxlon, mxlat in boxes:
                if mnlat <= lat <= mxlat and mnlon <= lon <= mxlon:
                    outs[rid].write(line)
                    counts[rid] += 1
            if total % 5_000_000 == 0:
                now = time.time()
                if now - last > 30:
                    print(f"  {total:,} lines ({total/(now-t0):.0f}/s)", flush=True)
                    last = now
    for rid, fh in outs.items():
        fh.close()
        final = os.path.join(DST_DIR, f"{rid}.search.jsonl")
        if os.path.islink(final) or os.path.exists(final):
            os.unlink(final)
        os.rename(part[rid], final)
    print(f"\nDone in {time.time()-t0:.0f}s; {total:,} features scanned, {bad} unparseable", flush=True)
    for rid in regions:
        sz = os.path.getsize(os.path.join(DST_DIR, f"{rid}.search.jsonl"))
        print(f"  {rid:28s} {counts[rid]:>12,}  {sz/1e6:>8.1f} MB", flush=True)


if __name__ == "__main__":
    main()
