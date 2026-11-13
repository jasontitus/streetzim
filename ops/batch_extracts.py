#!/usr/bin/env python3
"""Cut the extracts and search files of many regions in one pass each.

A region's build (cloud/rebuild_old_regions.sh) cuts its OSM extract from the
planet (osmium extract, about 55 minutes of reading 95 GB whatever the size
of the region, one build at a time under tmp/.regions-pbf.lock) and reads
the world search file (18 GB) to keep the records in its box (about 40
minutes). For a small region that is most of its build: Bermuda took 165
minutes, 10 of them building the map. ops/cloud/parallel_rebuild.sh runs
this beside the round instead, for the queued regions:

  search  one pass over the world search file writes
          world-data/regions/<id>.search.jsonl for every region that has
          none, only a symlink (to the world file), or a cut the driver
          would refuse (another box, or no native-script names: that one is
          parked beside it as the driver parks it, never deleted), with the
          .bbox and .schema sidecars the driver checks. Each keeps the
          records within MARGIN degrees of the region's box: a superset of
          what the builder keeps (it filters by the box again), so the map
          is the same.
  pbf     osmium extract --config with --strategy smart, BATCH regions per
          planet pass (each extract holds its own id sets: 17 at once
          needed more than 60 GB), each pass under the drivers' extract
          lock; every file is installed as the driver installs its own
          (.bbox and .strategy marks), so the driver finds it and skips its
          cut. A region whose extract is already complete for its box, or
          whose box crosses the antimeridian (the driver cuts those with a
          two-ring .poly), is left alone. A pass that fails is tried once
          more as two halves (one that ran into the time limit is not); a
          half or a single region that fails is left to its builds, and
          after two of those in a row so is everything not yet cut.

The two run side by side (the search pass in a process of its own), in the
order of the IDs: the first BATCH extracts land after one planet pass, by
when the search files are there too, so give the regions in the order their
builds start. --pending FILE names the extracts still to be cut, one id a
line, rewritten after every pass, and FILE.search the search files still to
be cut; each is removed when its part is done. The runner holds a region
back while its id is in either.

Usage: batch_extracts.py [--root DIR] [--batch N] [--mem-gb G] [--pending FILE]
                         [--skip-pbf] [--skip-search] -- ID [ID ...]
IDs are data regions (a variant's parent): the caller resolves variants.
STREETZIM_OSMIUM replaces osmium (tests). Exit 0 unless the arguments are bad
or another batch is at work in this root (3: one at a time, under
tmp/.batch-extracts.lock and tmp/.batch-search.lock); a failed pass is logged
and its regions are left to their builds.
"""
import argparse
import fcntl
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import time
from datetime import datetime

try:  # the runner's log is in Pacific time; these lines sit beside it
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/Los_Angeles")
except Exception:  # no tz database: local time
    _TZ = None

MARGIN = 0.01
PART = ".batch.part"  # not the drivers' .part: theirs and this never share a file
PASS_TIMEOUT_S = 4 * 3600  # a planet pass takes about an hour
SCHEMA_OK = re.compile(r'"search_schema": *([2-9]|[1-9][0-9]+)[,}]')  # the driver's test
WORLD_SEARCH = "search_cache/world-2026-08-31.s2.jsonl"
PLANET = "world-data/planet-2026-08-31.osm.pbf"


def say(msg):
    now = datetime.now(_TZ) if _TZ else datetime.now().astimezone()
    print(f"{now:%Y-%m-%d %H:%M:%S %Z} {msg}", flush=True)


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


def write_pending(path, ids):
    """The ids still to come, one a line; no file when there are none."""
    if not path:
        return
    if not ids:
        if os.path.exists(path):
            os.remove(path)
        return
    with open(path + ".part", "w") as f:
        f.write("".join(f"{r}\n" for r in ids))
    os.replace(path + ".part", path)


def search_kept(dst, bbox):
    """A real cut the driver keeps: for this box, with native-script names."""
    if not os.path.lexists(dst) or os.path.islink(dst):
        return False
    if read(dst + ".bbox") not in (None, bbox):
        return False
    return bool(SCHEMA_OK.search(read(dst + ".schema") or ""))


def search_wanted(root, regs, ids):
    """The regions of `ids` that need a search cut (none when it cannot be made)."""
    world = os.path.join(root, WORLD_SEARCH)
    why = None
    if not os.path.isfile(world):
        why = f"no {world}"
    elif not SCHEMA_OK.search(read(world + ".schema") or ""):
        why = f"{WORLD_SEARCH} has no native-script names (search schema < 2)"
    if why:
        say(f"search: {why}; skipped")
        return []
    out = os.path.join(root, "world-data/regions")
    want = [r for r in ids if not search_kept(os.path.join(out, f"{r}.search.jsonl"), regs[r])]
    if not want:
        say("search: every region has its own cut")
    return want


def cut_search(root, regs, ids, pending=None):
    world = os.path.join(root, WORLD_SEARCH)
    out = os.path.join(root, "world-data/regions")
    want = search_wanted(root, regs, ids)
    write_pending(pending, want)
    if not want:
        return
    try:
        _cut_search(world, regs, [(rid, box_of(regs[rid]), os.path.join(out, f"{rid}.search.jsonl")) for rid in want])
    finally:
        write_pending(pending, [])


def _cut_search(world, regs, todo):
    say(f"search: one pass over {WORLD_SEARCH} for {len(todo)} regions")
    t0, n = time.time(), 0
    kept = dict.fromkeys((t[0] for t in todo), 0)
    files = []
    try:
        for rid, box, dst in todo:
            files.append((rid, box, open(dst + PART, "w", encoding="utf-8")))
        with open(world, encoding="utf-8", buffering=16 << 20) as src:
            for line in src:
                n += 1
                lat, lon = lat_lon(line)
                for rid, box, fh in files:
                    if in_box(box, lat, lon):
                        fh.write(line)
                        kept[rid] += 1
        for _rid, _box, fh in files:
            fh.close()
        for rid, _box, dst in todo:
            if search_kept(dst, regs[rid]):
                os.remove(dst + PART)
                say(f"search {rid}: cut meanwhile by its build; mine dropped")
                continue
            if os.path.lexists(dst) and not os.path.islink(dst):  # stale: parked, as the driver parks it
                park = f"{dst}.stale-{time.strftime('%Y-%m-%d-%H%M%S')}"
                os.rename(dst, park)
                for side in ("bbox", "schema"):
                    if os.path.exists(f"{dst}.{side}"):
                        os.rename(f"{dst}.{side}", f"{park}.{side}")
                say(f"search {rid}: the old cut (another box, or search schema < 2) parked as {os.path.basename(park)}")
            with open(dst + ".bbox" + PART, "w") as b:
                b.write(regs[rid] + "\n")
            shutil.copyfile(world + ".schema", dst + ".schema" + PART)
            os.replace(dst + ".schema" + PART, dst + ".schema")
            os.replace(dst + ".bbox" + PART, dst + ".bbox")
            os.replace(dst + PART, dst)  # replaces a symlink to the world file
            say(f"search {rid}: {kept[rid]:,} records")
    finally:  # whatever was not installed (an error, a signal) is not left behind
        for _rid, _box, fh in files:
            fh.close()
        for _rid, _box, dst in todo:
            for part in (dst + PART, dst + ".bbox" + PART, dst + ".schema" + PART):
                if os.path.exists(part):
                    os.remove(part)
    say(f"search: {n:,} lines in {time.time() - t0:.0f} s")


def cut_pbfs(root, regs, ids, batch, mem_gb, pending=None):
    planet = os.path.join(root, PLANET)
    out = os.path.join(root, "world-data/regions")
    work = os.path.join(root, "tmp/batch-extracts")
    osmium = os.environ.get("STREETZIM_OSMIUM", "osmium")
    todo = [r for r in ids if not crosses(regs[r]) and not pbf_ready(os.path.join(out, f"{r}.osm.pbf"), regs[r])]
    if not todo:
        say("pbf: every region has its extract")
    elif not os.path.isfile(planet):
        say(f"pbf: no {planet}; skipped")
        todo = []
    write_pending(pending, todo)
    if not todo:
        return
    os.makedirs(work, exist_ok=True)
    lockpath = os.path.join(root, "tmp/.regions-pbf.lock")

    def limit():  # mem_gb, or less under a caller's own cap
        _soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
        want = mem_gb << 30
        if hard != resource.RLIM_INFINITY:
            want = min(want, hard)
        resource.setrlimit(resource.RLIMIT_DATA, (want, hard))

    def one_pass(part):
        """One planet pass for `part`: "done" (its extracts installed here),
        "failed" or "timeout"."""
        cfg = {"directory": work, "extracts": [
            {"output": f"{r}.osm.pbf", "output_format": "pbf", "bbox": list(box_of(regs[r]))} for r in part]}
        cfgpath = os.path.join(work, "batch.json")
        with open(cfgpath, "w") as f:
            json.dump(cfg, f, indent=1)
        say(f"pbf: one planet pass for {len(part)}: {' '.join(part)}")
        t0 = time.time()
        try:
            rc = subprocess.run([osmium, "extract", "-c", cfgpath, "-s", "smart", "--overwrite", planet],
                                preexec_fn=limit, timeout=PASS_TIMEOUT_S).returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
        peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1e6
        if rc != 0:
            say(f"pbf: osmium rc={rc} after {(time.time() - t0) / 60:.0f} min")
            for r in part:
                try:
                    os.remove(os.path.join(work, f"{r}.osm.pbf"))
                except OSError:
                    pass
            return "timeout" if rc == "timeout" else "failed"
        for r in part:
            src, dst = os.path.join(work, f"{r}.osm.pbf"), os.path.join(out, f"{r}.osm.pbf")
            if not os.path.isfile(src) or os.path.getsize(src) == 0:
                say(f"pbf {r}: no output")
                continue
            if pbf_ready(dst, regs[r]):
                os.remove(src)
                continue
            # The marks first, the file last: an extract is never there
            # without them (the runner and the drivers would take it for an
            # incomplete one and cut it again).
            shutil.move(src, dst + ".part")
            st = os.stat(dst + ".part")
            with open(dst + ".strategy", "w") as f:
                f.write(f"smart {st.st_size} {int(st.st_mtime)}\n")
            with open(dst + ".bbox", "w") as f:
                f.write(regs[r] + "\n")
            os.replace(dst + ".part", dst)
            say(f"pbf {r}: {st.st_size / 1e6:.1f} MB")
        say(f"pbf: pass took {(time.time() - t0) / 60:.0f} min; osmium peak {peak:.1f} GB so far")
        return "done"

    # (regions, whether a failure is tried again as halves)
    passes = [(todo[k:k + batch], True) for k in range(0, len(todo), batch)]
    failed = 0  # failures in a row with nothing smaller to try
    while passes:
        part, split = passes.pop(0)
        with open(lockpath, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            part = [r for r in part if not pbf_ready(os.path.join(out, f"{r}.osm.pbf"), regs[r])]
            got = one_pass(part) if part else None
        if got == "done":
            failed = 0
        elif got == "failed" and split and len(part) > 1:
            # Too many for the memory, or one bad region: the halves tell.
            half = (len(part) + 1) // 2
            say(f"pbf: trying those {len(part)} again as two passes")
            passes[:0] = [(part[:half], False), (part[half:], False)]
        elif got:
            failed += 1
            if failed >= 2:
                part = part + [r for p, _s in passes for r in p]
                passes = []
                say(f"pbf: two failed passes in a row; left to their builds: {' '.join(part)}")
            else:
                say(f"pbf: left to their builds: {' '.join(part)}")
        write_pending(pending, [r for p, _s in passes for r in p])


def only_one(root, name):
    """The lock of the one batch allowed in `root` (kept open by the caller),
    or None when another holds it: a batch that outlived its runner would
    write the same files as the next runner's."""
    os.makedirs(os.path.join(root, "tmp"), exist_ok=True)
    lock = open(os.path.join(root, "tmp", name), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None
    return lock


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=os.environ.get("STREETZIM_ROOT", "/storage/streetzim"))
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--mem-gb", type=int, default=60)
    ap.add_argument("--pending")
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
    pending_search = a.pending + ".search" if a.pending else None
    # Held to the end of this process; the search pass of a full batch takes
    # its own in its own process.
    locks = [only_one(a.root, name) for name, skipped in
             ((".batch-extracts.lock", a.skip_pbf), (".batch-search.lock", not a.skip_pbf or a.skip_search))
             if not skipped]
    if None in locks:  # the lists are the other batch's to keep
        say("another batch is cutting in this root; nothing done")
        for lock in locks:
            if lock:
                lock.close()
        return 3
    search = None
    try:
        if a.skip_search:
            write_pending(pending_search, [])
        elif a.skip_pbf:
            cut_search(a.root, regs, ids, pending_search)
        else:  # beside the planet passes, in a process of its own
            want = search_wanted(a.root, regs, ids)
            write_pending(pending_search, want)
            if want:
                search = subprocess.Popen(
                    [sys.executable, os.path.abspath(__file__), "--root", a.root, "--skip-pbf",
                     *(["--pending", a.pending] if a.pending else []), "--", *want])
        if not a.skip_pbf:
            cut_pbfs(a.root, regs, ids, max(1, a.batch), a.mem_gb, a.pending)
    finally:
        if not a.skip_pbf:
            write_pending(a.pending, [])
        if search:
            if search.wait() != 0:
                say(f"search: rc={search.returncode}")
            write_pending(pending_search, [])
        for lock in locks:
            lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
