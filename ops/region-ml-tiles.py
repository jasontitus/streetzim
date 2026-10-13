#!/usr/bin/env python3
"""Multilingual vector tiles for one region (docs/language-ready.md).

The world tiles (world-data/world-tiles-v3.mbtiles) carry English and each
place's own name only (name:latin, name_int). A language-ready build needs
name:xx for the matrix languages too (streetzim.languages
MATRIX_TILE_LANGUAGES, or $STREETZIM_TILE_LANGUAGES), so this cuts them from
the region's own extract with tilemaker:

- the image CI published for the checkout's commit
  (ghcr.io/jasontitus/streetzim:sha-<first 12 hex>,
  .github/workflows/docker-publish.yml), with the tile profile inside it
  (/app/resources/tilemaker); a commit without an image fails, there is no
  fallback to the world tiles;
- world-data/regions/<src-id>.osm.pbf (a Light variant passes its parent
  as --src-id) and the registry bbox; the coastline and Natural Earth
  shapefiles the world tiles were made with (<root>/coastline,
  <root>/landcover);
- in docker as a guest of the shared host: one run at a time
  (<store-dir>/.ml-tiles.lock), --memory 24g --memory-swap 24g --cpus 6,
  our own uid, the tilemaker store in a per-run folder of <store-dir>
  that is deleted afterwards, and the image tag removed again if this run
  pulled it.

Output: <out-dir>/<src-id>.ml.mbtiles, written as .ml.part.mbtiles and
renamed once it checks out (an MBTiles file: tilemaker picks its writer by
the extension, and "x.mbtiles.part" made a directory of loose tiles). Its
sidecars follow ops/region-bbox.sh's (one line each):
  .bbox   the registry bbox it was cut for
  .langs  the languages, sorted, comma-separated
  .image  the image id it was made with
  .pbf    the extract's size and mtime (a new extract is a new input)
A file whose sidecars differ from this run's inputs, or lack one, is stale
and made again; otherwise it is reused. .run.json records the run (time,
peak memory, image tag, commit) for planning; it is not compared.

Usage: region-ml-tiles.py [--src-id ID] -- <id> <bbox>
Prints the .ml.mbtiles path on stdout; everything else goes to stderr.
Env: STREETZIM_ROOT (the data root), STREETZIM_TILE_LANGUAGES,
STREETZIM_ML_COMMIT (default: the main commit the checkout is on, see
commit()), STREETZIM_ML_OUT_DIR
(default <root>/world-data/regions), STREETZIM_ML_STORE_DIR (default
<root>/tmp), STREETZIM_ML_TIMEOUT (seconds, default 10800),
STREETZIM_ML_IO (normal or idle, see NICE_SHIM), STREETZIM_ML_IMAGE_REPO
and DOCKER (tests).
"""
import argparse
import fcntl
import gzip
import importlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time


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


ROOT = _streetzim_root()
# The code is this file's own checkout (ROOT may point at other data).
CODE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, CODE)

IMAGE_REPO = "ghcr.io/jasontitus/streetzim"
MEMORY = "24g"
CPUS = 6
SIDECARS = ("bbox", "langs", "image", "pbf")
# The profile inside the image: the tiles are what that commit's builder
# makes, whatever this checkout's working tree holds.
PROFILE = ("/app/resources/tilemaker/config-openmaptiles.json",
           "/app/resources/tilemaker/process-openmaptiles.lua")
SHAPEFILES = ("coastline/water_polygons.shp",
              "landcover/ne_10m_urban_areas/ne_10m_urban_areas.shp",
              "landcover/ne_10m_antarctic_ice_shelves_polys/ne_10m_antarctic_ice_shelves_polys.shp",
              "landcover/ne_10m_glaciated_areas/ne_10m_glaciated_areas.shp")
# Runs tilemaker at nice 19 and, with STREETZIM_ML_IO=idle, in the idle I/O
# class (the image has no ionice; 251 is ioprio_set on x86-64, the image's
# only platform; best effort: a refused call leaves the default class). Idle
# yields the disk to everything else, which on a busy disk makes the run
# much slower: for a scratch run beside a production build, not for a
# region build in the queue, whose other steps run in the default class.
NICE_SHIM = ("import ctypes, os, sys\n"
             "if sys.argv[1] == 'idle':\n"
             "    ctypes.CDLL(None, use_errno=True).syscall(251, 1, 0, 3 << 13)\n"
             "os.nice(19)\n"
             "os.execvp(sys.argv[2], sys.argv[2:])\n")


def say(msg):
    print(msg, file=sys.stderr, flush=True)


def fail(msg):
    raise SystemExit(f"FATAL (multilingual tiles): {msg}")


def io_class():
    io = os.environ.get("STREETZIM_ML_IO") or "normal"
    if io not in ("normal", "idle"):
        fail(f"STREETZIM_ML_IO must be normal or idle, not {io!r}")
    return io


def docker_bin():
    return os.environ.get("DOCKER", "docker")


def parse_bbox(bbox):
    """The four numbers of a registry bbox. A box across the antimeridian
    (minlon > maxlon, alaska) is refused: tilemaker's --bbox cannot hold it,
    and without one it would tile the extract's whole header box."""
    try:
        w, s, e, n = (float(v) for v in bbox.split(","))
    except ValueError:
        fail(f"bbox {bbox!r} is not minlon,minlat,maxlon,maxlat")
    if w > e or e > 180:
        fail(f"bbox {bbox} crosses the antimeridian: not supported yet, build "
             "this region without STREETZIM_ML_TILES")
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        fail(f"bbox {bbox} is not a box")
    return w, s, e, n


def languages():
    from streetzim import languages as L
    text = os.environ.get("STREETZIM_TILE_LANGUAGES") or ",".join(L.MATRIX_TILE_LANGUAGES)
    try:
        return L.tile_languages(text)
    except ValueError as e:
        fail(f"STREETZIM_TILE_LANGUAGES: {e}")


# What decides the tiles besides the image: a checkout whose copy of these
# differs from the main commit the image is for would not get its own tiles.
TILE_INPUTS = ("resources/tilemaker", "Dockerfile")


def _git(*args):
    r = subprocess.run(["git", "-C", CODE, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def commit():
    """The main commit whose image makes the tiles: $STREETZIM_ML_COMMIT, else
    the checkout's HEAD where it is on origin/main, else the main commit it
    is based on (the host commits torrents and lists on top of main), as long
    as the checkout's tile profile and Dockerfile, committed or not, are
    that commit's."""
    c = os.environ.get("STREETZIM_ML_COMMIT")
    if c:
        if not re.fullmatch(r"[0-9a-f]{12,40}", c):
            fail(f"STREETZIM_ML_COMMIT {c!r} is not 12 to 40 lowercase hex digits")
        return c
    head = _git("rev-parse", "HEAD")
    base = _git("merge-base", "HEAD", "origin/main")
    if not head or not base:
        fail(f"cannot find the main commit of {CODE} (git merge-base HEAD origin/main); "
             "set STREETZIM_ML_COMMIT")
    changed = _git("diff", "--name-only", base, "--", *TILE_INPUTS)
    if changed is None or changed:
        fail(f"{CODE}'s {' / '.join(TILE_INPUTS)} differ from main's {base[:12]} "
             f"({(changed or 'git diff failed').replace(chr(10), ', ')}): its image would "
             "not make this checkout's tiles. Commit to main first, or set STREETZIM_ML_COMMIT")
    if base != head:
        say(f"  checkout {head[:12]} is not on main: using main's {base[:12]} "
            "(same tile profile and Dockerfile)")
    return base


def pbf_identity(path):
    st = os.stat(path)
    return f"{st.st_size} {st.st_mtime_ns}"


def read_sidecar(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().rstrip("\n")
    except OSError:
        return None


def write_sidecar(path, value):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(value + "\n")
    os.replace(tmp, path)


def is_fresh(out, want):
    """Whether ``out`` exists as a file and every sidecar holds ``want``'s
    value (a missing sidecar: stale)."""
    if not os.path.isfile(out) or os.path.islink(out) or os.path.getsize(out) == 0:
        return False
    for k in SIDECARS:
        have = read_sidecar(f"{out}.{k}")
        if have != want[k]:
            say(f"  {os.path.basename(out)}: {k} is {have!r}, this run's is "
                f"{want[k]!r}: stale")
            return False
    return True


def image_id(tag):
    r = subprocess.run([docker_bin(), "image", "inspect", tag, "--format",
                        '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None, None
    parts = r.stdout.split()
    return (parts[0] if parts else None), (parts[1] if len(parts) > 1 else "")


def pull(tag, rev):
    """Pull ``tag`` (unless docker has it) and return its image id. The
    image must say it was built from ``rev``."""
    if image_id(tag)[0]:
        say(f"  image {tag}: present")
    else:
        say(f"  image {tag}: pulling")
        try:
            r = subprocess.run([docker_bin(), "pull", "-q", tag], stdout=sys.stderr,
                               stderr=sys.stderr, timeout=1800)
            rc = r.returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
        if rc != 0:
            fail(f"cannot pull {tag} (rc={rc}). CI publishes an image per main "
                 "commit (.github/workflows/docker-publish.yml): is this commit on "
                 "main, and has its CI run finished? STREETZIM_ML_COMMIT picks "
                 "another commit. Not falling back to the world tiles.")
    iid, label = image_id(tag)
    if not iid:
        fail(f"{tag} is not in docker after the pull")
    if not label or not (label.startswith(rev) or rev.startswith(label)):
        fail(f"{tag} says it was built from {label or 'no commit'}, not {rev}")
    return iid


def drop_tag(tag, pulled):
    """Remove the tag this run pulled (never one that was there before);
    docker refuses while a container uses it, which is fine."""
    if not pulled:
        return
    for attempt in range(6):        # a container just removed can still hold it
        r = subprocess.run([docker_bin(), "rmi", tag], capture_output=True, text=True)
        if r.returncode == 0:
            say(f"  removed image tag {tag}")
            return
        if "conflict" not in r.stderr or attempt == 5:
            break
        time.sleep(5)
    say(f"  image tag {tag} kept: {r.stderr.strip()[:200]}")


class MemorySampler(threading.Thread):
    """Peak memory of the container (cgroup v2 memory.peak, which counts
    the page cache of tilemaker's mmapped store, and the largest anon seen)."""

    def __init__(self, cidfile):
        super().__init__(daemon=True)
        self.cidfile = cidfile
        self.peak = 0
        self.anon = 0
        self.stop = threading.Event()

    def run(self):
        cg = None
        while not self.stop.wait(5):
            if cg is None:
                cid = read_sidecar(self.cidfile)
                if cid:
                    cg = f"/sys/fs/cgroup/system.slice/docker-{cid}.scope"
                continue
            try:
                with open(f"{cg}/memory.peak") as f:
                    self.peak = max(self.peak, int(f.read()))
                with open(f"{cg}/memory.stat") as f:
                    for line in f:
                        if line.startswith("anon "):
                            self.anon = max(self.anon, int(line.split()[1]))
            except (OSError, ValueError):
                pass


def reap(out_dir, store_dir):
    """Remove the part folders and stores of runs that died without
    cleaning up (SIGKILL, a reboot): their process is gone."""
    for d, prefix in ((out_dir, ".ml-part-"), (store_dir, "ml-store-")):
        for n in os.listdir(d):
            pid = n.rsplit("-", 1)[-1]
            if n.startswith(prefix) and pid.isdigit() and not os.path.exists(f"/proc/{pid}"):
                say(f"  removing {os.path.join(d, n)}, left by a dead run")
                shutil.rmtree(os.path.join(d, n), ignore_errors=True)


def run_tilemaker(*, iid, name, pbf, bbox, langs, part, store, timeout):
    """tilemaker in the image; True on success. The container is removed
    however this ends (a timeout kills only the client)."""
    cidfile = os.path.join(store, "cid")
    store_in = os.path.join(store, "tm")
    os.makedirs(store_in)
    cmd = [docker_bin(), "run", "--rm", "--init", "--name", name,
           "--cidfile", cidfile,
           "--user", f"{os.getuid()}:{os.getgid()}",
           "--memory", MEMORY, "--memory-swap", MEMORY, "--cpus", str(CPUS),
           "-v", f"{os.path.realpath(pbf)}:/in/region.osm.pbf:ro",
           "-v", f"{os.path.join(ROOT, 'coastline')}:/w/coastline:ro",
           "-v", f"{os.path.join(ROOT, 'landcover')}:/w/landcover:ro",
           "-v", f"{os.path.dirname(part)}:/out", "-v", f"{store_in}:/store", "-w", "/w",
           "-e", f"STREETZIM_TILE_LANGUAGES={','.join(langs)}",
           "--entrypoint", "python3", iid, "-c", NICE_SHIM, io_class(),
           "tilemaker", "--input", "/in/region.osm.pbf",
           "--output", f"/out/{os.path.basename(part)}",
           "--config", PROFILE[0], "--process", PROFILE[1],
           "--bbox", bbox, "--store", "/store", "--threads", str(CPUS),
           "--skip-integrity"]
    say("  " + " ".join(cmd).replace(NICE_SHIM, "<nice shim>"))
    sampler = MemorySampler(cidfile)
    sampler.start()
    t0 = time.time()
    try:
        r = subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr, timeout=timeout)
        rc = r.returncode
    except subprocess.TimeoutExpired:
        say(f"  tilemaker passed {timeout} s: stopping it")
        rc = None
    finally:
        sampler.stop.set()
        subprocess.run([docker_bin(), "rm", "-f", name], capture_output=True)
    secs = time.time() - t0
    gib = 1 << 30
    say(f"  tilemaker rc={rc} in {secs:.0f} s; peak memory {sampler.peak / gib:.1f} GiB "
        f"(anon {sampler.anon / gib:.1f} GiB)")
    return rc == 0, {"seconds": round(secs), "peak_bytes": sampler.peak,
                     "peak_anon_bytes": sampler.anon}


def _tile_keys(blob):
    pb2 = importlib.import_module("mapbox_vector_tile.Mapbox.vector_tile_pb2")
    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
    t = pb2.tile()
    t.ParseFromString(blob)
    return {k for layer in t.layers for k in layer.keys}


def check_tiles(path, langs, bbox):
    """Refuse output that is not a usable MBTiles of these languages: a
    sqlite file with tiles at zoom 0, 6, 10 and 14, pbf format, bounds that
    are the bbox (tilemaker took --bbox), and among the low-zoom tiles
    name:xx keys for some of the languages and for no other (the profile
    writes only those it was asked for)."""
    if not os.path.isfile(path):
        fail(f"tilemaker produced {'a directory' if os.path.isdir(path) else 'nothing'} "
             f"at {path}, not an MBTiles file")
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        meta = dict(c.execute("select name, value from metadata"))
        if meta.get("format") != "pbf":
            fail(f"{path}: metadata format {meta.get('format')!r}, not pbf")
        try:
            bounds = [float(v) for v in
                      str(meta.get("bounds", "")).strip("[]").split(",")]
        except ValueError:
            bounds = []
        want = [float(v) for v in bbox.split(",")]
        if len(bounds) != 4 or any(abs(a - b) > 1e-6 for a, b in zip(bounds, want)):
            fail(f"{path}: bounds {meta.get('bounds')!r}, not the bbox {bbox}")
        for z in (0, 6, 10, 14):
            if not c.execute("select 1 from tiles where zoom_level=? limit 1", (z,)).fetchone():
                fail(f"{path}: no tile at zoom {z}")
        keys = set()
        for (blob,) in c.execute("select tile_data from tiles where zoom_level <= 8"):
            keys |= {k for k in _tile_keys(blob) if k.startswith("name:")}
    finally:
        c.close()
    keys.discard("name:latin")
    found = sorted(k[5:] for k in keys)
    stray = sorted(set(found) - set(langs))
    if stray:
        fail(f"{path}: tiles carry name:{','.join(stray)}, which were not asked for")
    if not found:
        fail(f"{path}: no name:xx for any of {','.join(langs)} in the zoom 0-8 tiles; "
             "the profile did not get STREETZIM_TILE_LANGUAGES")
    say(f"  check: zooms 0/6/10/14 present; zoom 0-8 tiles carry {len(found)} of "
        f"{len(langs)} languages ({','.join(found)})")


def make(rid, src_id, bbox, *, out_dir=None, store_dir=None, timeout=None):
    parse_bbox(bbox)
    io_class()
    langs = languages()
    rev = commit()
    repo = os.environ.get("STREETZIM_ML_IMAGE_REPO", IMAGE_REPO)
    tag = f"{repo}:sha-{rev[:12]}"
    out_dir = os.path.abspath(out_dir or os.environ.get("STREETZIM_ML_OUT_DIR")
                              or os.path.join(ROOT, "world-data", "regions"))
    store_dir = os.path.abspath(store_dir or os.environ.get("STREETZIM_ML_STORE_DIR")
                                or os.path.join(ROOT, "tmp"))
    timeout = timeout or int(os.environ.get("STREETZIM_ML_TIMEOUT") or 10800)
    pbf = os.path.join(ROOT, "world-data", "regions", f"{src_id}.osm.pbf")
    if not os.path.isfile(pbf) or os.path.getsize(pbf) == 0:
        fail(f"no extract {pbf}")
    if os.path.islink(pbf):
        fail(f"{pbf} is a symlink: tiles need a real extract (a planet link would "
             "tile the planet)")
    cut_for = read_sidecar(pbf + ".bbox")
    if cut_for is not None and cut_for != bbox:
        fail(f"{pbf} was cut for bbox {cut_for}, not {bbox}: extract it again first")
    for shp in SHAPEFILES:
        if not os.path.isfile(os.path.join(ROOT, shp)):
            fail(f"{os.path.join(ROOT, shp)} is missing (the tiles would have no "
                 "ocean or landcover)")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(store_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{src_id}.ml.mbtiles")
    say(f"=== multilingual tiles for {rid}"
        + (f" (from {src_id})" if src_id != rid else "")
        + f": {len(langs)} languages, bbox {bbox}, image {tag}")

    # One run at a time on the host; the freshness check is under the lock
    # too, so a run that waited reuses what the one before it made.
    lock = open(os.path.join(store_dir, ".ml-tiles.lock"), "w")
    t_wait = time.time()
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        say("  waiting for another multilingual tile run (one at a time)")
        fcntl.flock(lock, fcntl.LOCK_EX)
        say(f"  waited {time.time() - t_wait:.0f} s")
    reap(out_dir, store_dir)
    # The tag is removed afterwards only when this run is what pulled it.
    pulled = image_id(tag)[0] is None
    # tilemaker writes into a folder of its own beside the output (so the
    # rename is atomic and the container sees nothing else of out_dir).
    partdir = os.path.join(out_dir, f".ml-part-{src_id}-{os.getpid()}")
    part = os.path.join(partdir, f"{src_id}.ml.part.mbtiles")
    store = os.path.join(store_dir, f"ml-store-{src_id}-{os.getpid()}")
    name = f"streetzim-mltiles-{src_id}"
    try:
        iid = pull(tag, rev)
        want = {"bbox": bbox, "langs": ",".join(langs), "image": iid,
                "pbf": pbf_identity(pbf)}
        if is_fresh(out, want):
            say(f"  {out}: fresh (same bbox, languages, image and extract), reused")
            return out
        for k in SIDECARS + ("run.json",):    # they describe the old file
            try:
                os.unlink(f"{out}.{k}")
            except FileNotFoundError:
                pass
        for d in (partdir, store):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d)
        subprocess.run([docker_bin(), "rm", "-f", name], capture_output=True)
        ok, stats = run_tilemaker(iid=iid, name=name, pbf=pbf, bbox=bbox, langs=langs,
                                  part=part, store=store, timeout=timeout)
        if not ok:
            fail(f"tilemaker failed for {rid}; see above")
        check_tiles(part, langs, bbox)
        os.replace(part, out)
        for k in SIDECARS:
            write_sidecar(f"{out}.{k}", want[k])
        stats.update(image=tag, image_id=iid, commit=rev, bbox=bbox, langs=langs,
                     pbf_bytes=os.path.getsize(pbf), mbtiles_bytes=os.path.getsize(out))
        write_sidecar(f"{out}.run.json", json.dumps(stats))
        say(f"  wrote {out} ({os.path.getsize(out) / 1e6:.0f} MB)")
        return out
    finally:
        for d in (partdir, store):
            shutil.rmtree(d, ignore_errors=True)
        drop_tag(tag, pulled)
        lock.close()


def _end(signum, _frame):
    # Once: a second signal (timeout's, a shell's) must not cut the cleanup
    # short, as one did on 2026-10-13 and left the image tag behind.
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, signal.SIG_IGN)
    raise SystemExit(128 + signum)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src-id", help="the region whose extract is used (a variant's parent)")
    ap.add_argument("id")
    ap.add_argument("bbox")
    a = ap.parse_args(argv)
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, _end)
    print(make(a.id, a.src_id or a.id, a.bbox))
    return 0


if __name__ == "__main__":
    sys.exit(main())
