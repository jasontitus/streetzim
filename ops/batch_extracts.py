#!/usr/bin/env python3
"""Cut the extracts and search files of many regions in one pass each.

A region's build (cloud/rebuild_old_regions.sh) cuts its OSM extract from the
planet (osmium extract, about 55 minutes of reading 95 GB whatever the size
of the region, one build at a time under tmp/.regions-pbf.lock) and reads
the world search file (18 GB) to keep the records in its box (about 40
minutes). For a small region that is most of its build: Bermuda took 165
minutes, 10 of them building the map. ops/cloud/parallel_rebuild.sh runs
this before the round instead, for the queued regions:

  search  one pass over the world search file writes
          world-data/regions/<id>.search.jsonl for every region that has
          none or only a symlink (to the world file), with the .bbox and
          .schema sidecars the driver checks. Each keeps the records within
          MARGIN degrees of the region's box: a superset of what the builder
          keeps (it filters by the box again), so the map is the same.
  pbf     osmium extract --config with --strategy smart, BATCH regions per
          planet pass (each extract holds its own id sets: 17 at once
          needed more than 60 GB), each pass under the drivers' extract
          lock; every file is installed as the driver installs its own
          (.bbox and .strategy marks), so the driver finds it and skips its
          cut. A region whose extract is already complete for its box, or
          whose box crosses the antimeridian (the driver cuts those with a
          two-ring .poly), is left alone.

Usage: batch_extracts.py [--root DIR] [--batch N] [--mem-gb G] [--skip-pbf]
                         [--skip-search] -- ID [ID ...]
IDs are data regions (a variant's parent): the caller resolves variants.
STREETZIM_OSMIUM replaces osmium (tests). Exit 0 unless the arguments are bad;
a failed pass is logged and its regions are left to their builds.
"""
import argparse
import fcntl
import json
import os
import resource
import shutil
import subprocess
import sys
import time

MARGIN = 0.01
WORLD_SEARCH = "search_cache/world-2026-08-31.s2.jsonl"
PLANET = "world-data/planet-2026-08-31.osm.pbf"


def say(msg):
    print(f"[{time.strftime('%F %T')}] {msg}", flush=True)


def registry(root):
    out = {}
    with open(os.path.join(root, "cloud/regions.tsv"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            c = line.rstrip("\n").split("\t")
            if len(c) > 2 and c[0] not in out:
                out[c[0]] = c[2]
    return out


def box_of(text):
    b = [float(v) for v in text.split(",")]
    return b[0], b[1], b[2], b[3]


def crosses(text):
    b = box_of(text)
    return b[0] > b[2]


def in_box(box, lat, lon, margin=MARGIN):
    x0, y0, x1, y1 = box
    if not (y0 - margin <= lat <= y1 + margin):
        return False
    if x0 <= x1:
        return x0 - margin <= lon <= x1 + margin
    return lon >= x0 - margin or lon <= x1 + margin  # across the antimeridian


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def pbf_ready(pbf, bbox):
    """The driver's test: a file, cut for this box, marked smart for its size and mtime."""
    if not os.path.isfile(pbf) or os.path.getsize(pbf) == 0:
        return False
    if read(pbf + ".bbox") not in (None, bbox):
        return False
    st = os.stat(pbf)
    return read(pbf + ".strategy") == f"smart {st.st_size} {int(st.st_mtime)}"


def lat_lon(line):
    i = line.rfind('"lat":')
    try:
        j = line.index(',"lon":', i)
        return float(line[i + 6:j]), float(line[j + 7:line.index("}", j)])
    except ValueError:
        d = json.loads(line)
        return d["lat"], d["lon"]


def cut_search(root, regs, ids):
    world = os.path.join(root, WORLD_SEARCH)
    if not os.path.isfile(world):
        say(f"search: no {world}; skipped")
        return
    out = os.path.join(root, "world-data/regions")
    todo = []
    for rid in ids:
        dst = os.path.join(out, f"{rid}.search.jsonl")
        if os.path.lexists(dst) and not os.path.islink(dst):
            continue  # a real cut: the driver keeps it, or parks it if stale
        todo.append((rid, box_of(regs[rid]), dst, open(dst + ".part", "w", encoding="utf-8")))
    if not todo:
        say("search: every region has its own cut")
        return
    say(f"search: one pass over {WORLD_SEARCH} for {len(todo)} regions")
    t0, n = time.time(), 0
    kept = dict.fromkeys((t[0] for t in todo), 0)
    with open(world, encoding="utf-8", buffering=16 << 20) as src:
        for line in src:
            n += 1
            lat, lon = lat_lon(line)
            for rid, box, _dst, fh in todo:
                if in_box(box, lat, lon):
                    fh.write(line)
                    kept[rid] += 1
    for rid, _box, dst, fh in todo:
        fh.close()
        if os.path.lexists(dst) and not os.path.islink(dst):
            os.remove(dst + ".part")
            say(f"search {rid}: cut meanwhile by its build; mine dropped")
            continue
        with open(dst + ".bbox.part", "w") as b:
            b.write(regs[rid] + "\n")
        if os.path.isfile(world + ".schema"):
            shutil.copyfile(world + ".schema", dst + ".schema.part")
            os.replace(dst + ".schema.part", dst + ".schema")
        os.replace(dst + ".bbox.part", dst + ".bbox")
        os.replace(dst + ".part", dst)  # replaces a symlink to the world file
        say(f"search {rid}: {kept[rid]:,} records")
    say(f"search: {n:,} lines in {time.time() - t0:.0f} s")


def cut_pbfs(root, regs, ids, batch, mem_gb):
    planet = os.path.join(root, PLANET)
    out = os.path.join(root, "world-data/regions")
    work = os.path.join(root, "tmp/batch-extracts")
    osmium = os.environ.get("STREETZIM_OSMIUM", "osmium")
    todo = [r for r in ids if not crosses(regs[r]) and not pbf_ready(os.path.join(out, f"{r}.osm.pbf"), regs[r])]
    if not todo:
        say("pbf: every region has its extract")
        return
    if not os.path.isfile(planet):
        say(f"pbf: no {planet}; skipped")
        return
    os.makedirs(work, exist_ok=True)
    lockpath = os.path.join(root, "tmp/.regions-pbf.lock")

    def limit():  # mem_gb, or less under a caller's own cap
        _soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
        want = mem_gb << 30
        if hard != resource.RLIM_INFINITY:
            want = min(want, hard)
        resource.setrlimit(resource.RLIMIT_DATA, (want, hard))

    for k in range(0, len(todo), batch):
        part = todo[k:k + batch]
        with open(lockpath, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            part = [r for r in part if not pbf_ready(os.path.join(out, f"{r}.osm.pbf"), regs[r])]
            if not part:
                continue
            cfg = {"directory": work, "extracts": [
                {"output": f"{r}.osm.pbf", "output_format": "pbf", "bbox": list(box_of(regs[r]))} for r in part]}
            cfgpath = os.path.join(work, "batch.json")
            with open(cfgpath, "w") as f:
                json.dump(cfg, f, indent=1)
            say(f"pbf: one planet pass for {len(part)}: {' '.join(part)}")
            rc = subprocess.run([osmium, "extract", "-c", cfgpath, "-s", "smart", "--overwrite", planet],
                                preexec_fn=limit).returncode
            if rc != 0:
                say(f"pbf: osmium rc={rc}; {' '.join(part)} left to their builds")
                for r in part:
                    try:
                        os.remove(os.path.join(work, f"{r}.osm.pbf"))
                    except OSError:
                        pass
                continue
            for r in part:
                src, dst = os.path.join(work, f"{r}.osm.pbf"), os.path.join(out, f"{r}.osm.pbf")
                if not os.path.isfile(src) or os.path.getsize(src) == 0:
                    say(f"pbf {r}: no output")
                    continue
                if pbf_ready(dst, regs[r]):
                    os.remove(src)
                    continue
                shutil.move(src, dst + ".part")
                os.replace(dst + ".part", dst)
                with open(dst + ".bbox", "w") as f:
                    f.write(regs[r] + "\n")
                st = os.stat(dst)
                with open(dst + ".strategy", "w") as f:
                    f.write(f"smart {st.st_size} {int(st.st_mtime)}\n")
                say(f"pbf {r}: {st.st_size / 1e6:.1f} MB")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=os.environ.get("STREETZIM_ROOT", "/storage/streetzim"))
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--mem-gb", type=int, default=60)
    ap.add_argument("--skip-pbf", action="store_true")
    ap.add_argument("--skip-search", action="store_true")
    ap.add_argument("ids", nargs="+")
    a = ap.parse_args(argv)
    regs = registry(a.root)
    ids = []
    for r in a.ids:
        if r not in regs:
            say(f"{r}: no registry row; skipped")
        elif r not in ids:
            ids.append(r)
    if not a.skip_search:
        cut_search(a.root, regs, ids)
    if not a.skip_pbf:
        cut_pbfs(a.root, regs, ids, max(1, a.batch), a.mem_gb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
