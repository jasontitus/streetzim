#!/usr/bin/env python3
"""Multilingual vector tiles for one region (docs/language-ready.md).

The world tiles (world-data/world-tiles-v3.mbtiles) carry English and each
place's own name only (name:latin, name_int). A language-ready build needs
name:xx for the matrix languages too (streetzim.languages
MATRIX_TILE_LANGUAGES, plus a short-row region's own languages,
default_tile_languages; or $STREETZIM_TILE_LANGUAGES), so this cuts them from
the region's own extract with tilemaker:

- from an image CI published for main (ghcr.io/jasontitus/streetzim:sha-<12
  hex>, .github/workflows/docker-publish.yml; only the main tips CI tested
  have one), with the tile profile inside it (/app/resources/tilemaker). It
  is the newest one on main's first-parent line, at or below the checkout,
  whose resources/tilemaker and Dockerfile (TILEMAKER_REF with it) are the
  checkout's (image_commit). None: the build fails, there is no fallback to
  the world tiles;
- world-data/regions/<src-id>.osm.pbf (a Light variant passes its parent
  as --src-id) and the registry bbox; the coastline and Natural Earth
  shapefiles the world tiles were made with (<root>/coastline,
  <root>/landcover);
- in docker as a guest of the shared host: one run at a time
  (<store-dir>/.ml-tiles.lock), --memory 24g --memory-swap 24g --cpus 6,
  our own uid, nice 19, tilemaker under timeout(1) inside the container, the
  tilemaker store in a per-run folder on the shared NVMe scratch when it has
  room, reserved there while it grows (else <store-dir>), deleted
  afterwards, and the image tag
  removed again if this run pulled it. Under
  the lock, containers and folders a killed run left are removed first.

Output: <out-dir>/<src-id>.ml.mbtiles, written as .ml.part.mbtiles and
renamed once it checks out (an MBTiles file: tilemaker picks its writer by
the extension, and "x.mbtiles.part" made a directory of loose tiles). Its
sidecars follow ops/region-bbox.sh's (one line each):
  .bbox     the registry bbox it was cut for
  .langs    the languages, sorted, comma-separated
  .profile  what decides the tiles in the image: the resources/tilemaker
            tree, the Dockerfile blob and its TILEMAKER_REF (tile_inputs);
            not the image id, which changes with every commit
  .pbf      the extract's size and mtime (a new extract is a new input)
A file whose sidecars all hold this run's inputs is reused without asking
the registry or pulling anything; any other is made again. .run.json
records the run (time, peak memory, image, timeout) for planning; it is not
compared.

Usage: region-ml-tiles.py [--src-id ID] -- <id> <bbox>
Prints the .ml.mbtiles path on stdout; everything else goes to stderr.
Env: STREETZIM_ROOT (the data root), STREETZIM_TILE_LANGUAGES,
STREETZIM_ML_COMMIT (a main commit whose image to use; it must have the
checkout's tile inputs too), STREETZIM_ML_OUT_DIR (default
<root>/world-data/regions), STREETZIM_ML_STORE_DIR (the lock, and the
store when the NVMe scratch has no room; default <root>/tmp), NVME_SCRATCH,
NVME_RESERVE_GB and STREETZIM_ML_NVME=0 (place_store),
STREETZIM_ML_TIMEOUT (seconds; default from the extract's size, see
timeout_for), STREETZIM_ML_IO (normal or idle, see NICE_SHIM),
STREETZIM_ML_IMAGE_REPO, STREETZIM_ML_CHECKOUT and DOCKER (tests).
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
# The git checkout whose tile inputs decide the image (tests point it at a
# scratch repository).
CHECKOUT = os.environ.get("STREETZIM_ML_CHECKOUT") or CODE

IMAGE_REPO = "ghcr.io/jasontitus/streetzim"
NVME_SCRATCH = "/mnt/data/tilemaker/streetzim-scratch"     # build-region-fast.sh's
CONTAINER = "streetzim-mltiles-"
LOCK_LABEL = "streetzim.mltiles.lock"
MEMORY = "24g"
CPUS = 6
SIDECARS = ("bbox", "langs", "profile", "pbf")
# What decides the tiles in an image besides the extract and the languages.
TILE_PATHS = ("resources/tilemaker", "Dockerfile")
# How far image_commit looks back on main, and how many registry lookups.
MAIN_DEPTH = 500
MAX_LOOKUPS = 25
LOOKUP_SECONDS = 600
# The profile inside the image: the tiles are what that commit's builder
# makes, which is the checkout's (image_commit checks the inputs).
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
# The run's time limit when STREETZIM_ML_TIMEOUT is not set: BASE + PER_GB
# per GB of extract. Measured with the image's tilemaker at 6 threads
# (2026-10-13, extracts of 0.07-1.65 GB): 1-5 minutes per region, about 2
# minutes per GB plus a minute's start, slower on a busy disk. An hour plus
# half an hour per GB leaves a large margin (France, 10.4 GB: 6.2 h), up to
# 12 hours; a larger region's first run should be watched (.run.json).
BASE_SECONDS = 3600
PER_GB_SECONDS = 1800
MAX_SECONDS = 12 * 3600
# How often a store on the NVMe scratch rewrites its reservation, and how
# recently a dead run's store must have been written for its reservation to
# still count (its container may outlive its client).
RESERVE_EVERY = 30
STORE_BUSY = 600
# The client waits this much longer than timeout(1) in the container, then
# gives up on it (a hung daemon) and removes the container.
CLIENT_MARGIN = 300


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


def docker(*args, timeout=120):
    """A docker client call; a hung daemon counts as a failure, not a hang."""
    try:
        return subprocess.run([docker_bin(), *args], capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"docker {args[0]}: no answer in {timeout} s")


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


def region_row(rid):
    """The region's own languages, from cloud/region-languages.tsv in this
    checkout ($STREETZIM_REGION_LANGUAGES names another table: tests);
    None when it has no row."""
    table = os.environ.get("STREETZIM_REGION_LANGUAGES") or os.path.join(
        CODE, "cloud", "region-languages.tsv")
    try:
        with open(table, encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or "\t" not in line:
                    continue
                k, v = line.rstrip("\n").split("\t", 1)
                if k == rid:
                    return [x for x in v.split(",") if x]
    except OSError as e:
        fail(f"cannot read {table}: {e}")
    return None


def languages(rid=None):
    """$STREETZIM_TILE_LANGUAGES, else the matrix plus, for a region whose
    row lists few languages, its own (streetzim.languages
    .default_tile_languages: Switzerland adds Romansh)."""
    from streetzim import languages as L
    text = os.environ.get("STREETZIM_TILE_LANGUAGES")
    try:
        if text:
            return L.tile_languages(text)
        return L.default_tile_languages(region_row(rid) if rid else None)
    except ValueError as e:
        fail(f"tile languages{' of ' + rid if rid and not text else ''}: {e}")


def timeout_for(pbf):
    env = os.environ.get("STREETZIM_ML_TIMEOUT")
    if env:
        try:
            return int(env)
        except ValueError:
            fail(f"STREETZIM_ML_TIMEOUT {env!r} is not a number of seconds")
    return int(min(MAX_SECONDS, BASE_SECONDS + PER_GB_SECONDS * os.path.getsize(pbf) / 1e9))


# ---- which image ------------------------------------------------------------

def _git(*args):
    r = subprocess.run(["git", "-C", CHECKOUT, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def tile_inputs(rev):
    """What decides the tiles an image built from ``rev`` makes: its
    resources/tilemaker tree, its Dockerfile blob, and the TILEMAKER_REF in
    it. None when ``rev`` lacks them."""
    tree = _git("rev-parse", "--verify", "--quiet", f"{rev}:resources/tilemaker")
    blob = _git("rev-parse", "--verify", "--quiet", f"{rev}:Dockerfile")
    text = _git("show", f"{rev}:Dockerfile")
    if not tree or not blob or text is None:
        return None
    m = re.search(r"^ARG TILEMAKER_REF=(\S+)", text, re.M)
    return f"tilemaker={tree} dockerfile={blob} ref={m.group(1) if m else 'none'}"


def checkout_inputs():
    """tile_inputs of the checkout, which must have no change to them that
    is not committed (no image has those)."""
    dirty = _git("status", "--porcelain", "--", *TILE_PATHS)       # untracked files too
    if dirty is None:
        fail(f"cannot read the git status of {CHECKOUT}")
    if dirty:
        fail(f"{CHECKOUT} has changes in {' / '.join(TILE_PATHS)} that are not committed "
             f"({dirty.replace(chr(10), '; ')}): no published image has them")
    key = tile_inputs("HEAD")
    if not key:
        fail(f"cannot read {' / '.join(TILE_PATHS)} at {CHECKOUT}'s HEAD")
    return key


def image_repo():
    return os.environ.get("STREETZIM_ML_IMAGE_REPO", IMAGE_REPO)


def tag_of(rev):
    return f"{image_repo()}:sha-{rev[:12]}"


def published(tag):
    """Whether the registry has ``tag`` (a manifest lookup, no pull)."""
    return docker("manifest", "inspect", tag, timeout=120).returncode == 0


def image_commit(want):
    """The main commit whose image makes the tiles, for a checkout whose
    tile inputs are ``want``.

    CI publishes an image (sha-<12 hex>) for each main tip it tests, so a
    commit merged in from elsewhere (the host's torrent commits arrive on
    main as a merge's second parent) or one pushed together with a later
    one has none. So: main's first-parent line, from its newest commit that
    is the checkout's HEAD or an ancestor of it, back to the first commit
    that has the same tile inputs and a published image.
    $STREETZIM_ML_COMMIT names the commit instead; it must have the same
    inputs too."""
    c = os.environ.get("STREETZIM_ML_COMMIT")
    if c:
        full = _git("rev-parse", "--verify", "--quiet", f"{c}^{{commit}}")
        if not full:
            fail(f"STREETZIM_ML_COMMIT {c!r} is not a commit of {CHECKOUT} (fetch it first)")
        have = tile_inputs(full)
        if have != want:
            fail(f"STREETZIM_ML_COMMIT {full[:12]}'s tile inputs ({have}) are not this "
                 f"checkout's ({want}): its image would not make this checkout's tiles")
        if not published(tag_of(full)):
            fail(f"STREETZIM_ML_COMMIT {full[:12]} has no published image ({tag_of(full)})")
        return full
    line = _git("rev-list", "--first-parent", "-n", str(MAIN_DEPTH), "origin/main")
    after = _git("rev-list", "--first-parent", "-n", str(MAIN_DEPTH), "origin/main", "--not", "HEAD")
    if not line or after is None:
        fail(f"cannot read origin/main's history in {CHECKOUT}; set STREETZIM_ML_COMMIT")
    newer = set(after.split())
    below = [r for r in line.split() if r not in newer]
    if not below:
        fail(f"none of origin/main's last {MAIN_DEPTH} first-parent commits is in "
             f"{CHECKOUT}'s HEAD; set STREETZIM_ML_COMMIT")
    head = _git("rev-parse", "HEAD") or "?"
    tried = []
    t0 = time.monotonic()
    for rev in below:
        if tile_inputs(rev) != want:
            continue
        if time.monotonic() - t0 > LOOKUP_SECONDS:
            fail(f"the registry lookups passed {LOOKUP_SECONDS} s ({len(tried)} done: "
                 f"{', '.join(tried)}); is the registry reachable? STREETZIM_ML_COMMIT "
                 "skips the search")
        tried.append(rev[:12])
        if published(tag_of(rev)):
            if rev != head:
                say(f"  image: main's {rev[:12]} (the checkout is at {head[:12]}, on main's "
                    f"line at {below[0][:12]}; same tile inputs)")
            return rev
        if len(tried) >= MAX_LOOKUPS:
            break
    fail(f"no published image for this checkout's tile inputs ({want}): main's "
         f"first-parent commits at or below {below[0][:12]} with these inputs "
         f"({', '.join(tried) or 'none'}) have no {image_repo()}:sha-<12 hex> image. CI "
         "publishes one for each main tip it tests: push to main and wait for CI, or "
         "set STREETZIM_ML_COMMIT")


# ---- freshness ----------------------------------------------------------------

def pbf_identity(path):
    st = os.stat(path)
    return f"{st.st_size} {st.st_mtime_ns}"


def extract_complete(pbf):
    """Whether ``pbf``'s .strategy sidecar marks this very file (size and
    mtime) as cut with osmium --strategy smart (ops/region-bbox.sh's
    pbf_mark_complete)."""
    st = os.stat(pbf)
    return read_sidecar(pbf + ".strategy") == f"smart {st.st_size} {int(st.st_mtime)}"


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
    """Whether ``out`` is a real file (not a symlink) and every sidecar holds
    ``want``'s value (a missing sidecar: stale)."""
    if not os.path.isfile(out) or os.path.islink(out) or os.path.getsize(out) == 0:
        return False
    for k in SIDECARS:
        have = read_sidecar(f"{out}.{k}")
        if have != want[k]:
            say(f"  {os.path.basename(out)}: {k} is {have!r}, this run's is "
                f"{want[k]!r}: stale")
            return False
    return True


# ---- docker -------------------------------------------------------------------

def image_id(tag):
    r = docker("image", "inspect", tag, "--format",
               '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}')
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
            fail(f"cannot pull {tag} (rc={rc}). Not falling back to the world tiles.")
    iid, label = image_id(tag)
    if not iid:
        fail(f"{tag} is not in docker after the pull")
    if not label or not (label.startswith(rev) or rev.startswith(label)):
        fail(f"{tag} says it was built from {label or 'no commit'}, not {rev}")
    return iid


def drop_tag(tag, pulled):
    """Remove the tag this run pulled (never one that was there before);
    docker refuses while a container uses it, so a just-removed container
    gets a few seconds."""
    if not pulled:
        return
    for attempt in range(6):
        r = docker("rmi", tag)
        if r.returncode == 0:
            say(f"  removed image tag {tag}")
            return
        if "conflict" not in r.stderr or attempt == 5:
            break
        time.sleep(5)
    say(f"  image tag {tag} kept: {r.stderr.strip()[:200]}")


def remove_container(name):
    r = docker("rm", "-f", name)
    if r.returncode != 0 and "No such container" not in r.stderr:
        say(f"  could not remove container {name}: {r.stderr.strip()[:200]}")


def nvme_dir():
    """The shared NVMe scratch build-region-fast.sh stages tiles on
    ($NVME_SCRATCH), unless STREETZIM_ML_NVME=0."""
    if os.environ.get("STREETZIM_ML_NVME", "1") == "0":
        return None
    return os.environ.get("NVME_SCRATCH") or NVME_SCRATCH


def dir_bytes(path):
    """The disk space the files under ``path`` take (allocated blocks: the
    store's mapped files are sparse)."""
    total = 0
    for top, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(top, f)).st_blocks * 512
            except OSError:
                pass
    return total


def proc_start(pid):
    """When process ``pid`` started (/proc/<pid>/stat field 22, clock ticks
    since boot), or None if there is none: with the pid it names a process
    even when the pid has been reused."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        return stat[stat.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _reservation(path):
    """(bytes, start time) of a .reserve-ml-<pid> file, or None."""
    try:
        b, start = (read_sidecar(path) or "").split()
        return int(b), start
    except ValueError:
        return None


def _store_busy(nvme, pid):
    """Whether a store of ``pid`` on the NVMe scratch had a file written in
    the last STORE_BUSY seconds: a run whose client was SIGKILLed has a
    container still writing there."""
    cutoff = time.time() - STORE_BUSY
    for n in os.listdir(nvme):
        if n.startswith("ml-store-") and n.rsplit("-", 1)[-1] == pid:
            for top, _dirs, files in os.walk(os.path.join(nvme, n)):
                for f in files:
                    try:
                        if os.lstat(os.path.join(top, f)).st_mtime > cutoff:
                            return True
                    except OSError:
                        pass
    return False


def nvme_reserved(nvme):
    """Bytes the stores on the NVMe scratch may still grow into: the
    .reserve-ml-<pid> files of live runs (the pid with the start time the
    file records), and of a dead run whose store is still being written.
    Deletes nothing: only the reap does, after it has removed the
    containers. build-region-fast.sh's stagers count the same
    (nvme_ml_reserved)."""
    total = 0
    for n in os.listdir(nvme):
        if not n.startswith(".reserve-ml-"):
            continue
        pid = n.rsplit("-", 1)[-1]
        r = _reservation(os.path.join(nvme, n))
        if r and (proc_start(pid) == r[1] or _store_busy(nvme, pid)):
            total += r[0]
    return total


class Reservation(threading.Thread):
    """<scratch>/.reserve-ml-<pid>: the bytes this run's store on the NVMe
    scratch may still take (the estimate less what it holds), rewritten
    every RESERVE_EVERY seconds. Stagers and other runs count it as used,
    so a store growing for hours is not overbooked by a stager that checked
    the free space before it grew. The file holds the bytes and this
    process's start time, so a reused pid does not pass for this run.
    Removed when the run ends, also on a signal; a dead run's by the next
    run's reap."""

    def __init__(self, nvme, store, need):
        super().__init__(daemon=True)
        self.path = os.path.join(nvme, f".reserve-ml-{os.getpid()}")
        self.since = proc_start(os.getpid())    # not .start: a Thread method
        self.store = store
        self.need = int(need)
        self.halt = threading.Event()
        self.write()

    def remaining(self):
        return max(0, self.need - dir_bytes(self.store))

    def write(self):
        # The temporary name must not match the readers' .reserve-ml-* glob.
        tmp = os.path.join(os.path.dirname(self.path), f".ml-reserve-tmp-{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"{self.remaining()} {self.since}\n")
        os.replace(tmp, self.path)

    def run(self):
        while not self.halt.wait(RESERVE_EVERY):
            try:
                self.write()
            except OSError:
                pass

    def release(self):
        self.halt.set()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def place_store(pbf, fallback, src_id):
    """Make this run's tilemaker store folder and say where: (path,
    Reservation or None). The store is random I/O, so on the NVMe scratch
    when it has room for FREE_FACTOR (streetzim/tilemaker_store.py) times
    the extract beside what live stores there have reserved and above
    build-region-fast.sh's reserve ($NVME_RESERVE_GB, 120); decided, made
    and reserved under the stagers' .stage.lock, as the volume is shared
    with another tenant. Else in ``fallback``."""
    name = f"ml-store-{src_id}-{os.getpid()}"
    nvme = nvme_dir()
    if nvme:
        from streetzim.tilemaker_store import FREE_FACTOR
        try:
            os.makedirs(nvme, exist_ok=True)
            with open(os.path.join(nvme, ".stage.lock"), "w") as lk:
                deadline = time.monotonic() + 900
                while True:
                    try:
                        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            raise OSError("its stage lock was busy for 15 min") from None
                        time.sleep(5)
                need = FREE_FACTOR * os.path.getsize(pbf)
                reserve = float(os.environ.get("NVME_RESERVE_GB") or 120) * 1e9
                free = shutil.disk_usage(nvme).free
                held = nvme_reserved(nvme)
                gb = 1e9
                if free - held - need >= reserve:
                    store = os.path.join(nvme, name)
                    shutil.rmtree(store, ignore_errors=True)
                    os.makedirs(store)
                    resv = Reservation(nvme, store, need)
                    say(f"  store on NVMe {nvme}: {free / gb:.0f} GB free, {held / gb:.0f} "
                        f"reserved by other stores, needs {need / gb:.0f} "
                        f"+ {reserve / gb:.0f} reserve")
                    return store, resv
                say(f"  store on {fallback}: NVMe has {free / gb:.0f} GB free, {held / gb:.0f} "
                    f"reserved, needs {need / gb:.0f} + {reserve / gb:.0f} reserve")
        except (OSError, ValueError) as e:
            say(f"  store on {fallback}: NVMe {nvme} not usable ({e})")
    store = os.path.join(fallback, name)
    shutil.rmtree(store, ignore_errors=True)
    os.makedirs(store)
    return store, None


def reap(out_dir, store_dir, lock_path):
    """Under the lock, nothing of an earlier run may still be going: remove
    the containers runs under this lock left (a SIGKILLed client leaves its
    container running), then the part folders and stores of runs whose
    process is gone (a run stopped by a signal leaves them to this)."""
    r = docker("ps", "-aq", "--filter", f"label={LOCK_LABEL}={lock_path}")
    for cid in (r.stdout.split() if r.returncode == 0 else []):
        say(f"  removing container {cid}, left by an earlier run")
        remove_container(cid)
    for d, prefix in ((out_dir, ".ml-part-"), (store_dir, "ml-store-")):
        for n in os.listdir(d):
            pid = n.rsplit("-", 1)[-1]
            if n.startswith(prefix) and pid.isdigit() and not os.path.exists(f"/proc/{pid}"):
                say(f"  removing {os.path.join(d, n)}, left by a dead run")
                shutil.rmtree(os.path.join(d, n), ignore_errors=True)
    # On the NVMe scratch a run is live while its reservation's pid has the
    # start time recorded: a store with none is a dead run's (a store there
    # is made and reserved together, under the stagers' lock). Only this
    # reap deletes them, after the containers above; the stagers never do.
    # A dead run's store still being written (an orphaned container under
    # another lock, which the removal above did not reach) is left, with its
    # reservation, as nvme_reserved still counts it.
    nvme = nvme_dir()
    if nvme and os.path.isdir(nvme):
        live = set()
        for n in os.listdir(nvme):
            path = os.path.join(nvme, n)
            if not n.startswith(".reserve-ml-") or not os.path.isfile(path):
                continue
            pid = n.rsplit("-", 1)[-1]
            r = _reservation(path)
            if r and proc_start(pid) == r[1]:
                live.add(pid)
            elif not _store_busy(nvme, pid):
                say(f"  removing {path}, left by a dead run")
                try:
                    os.unlink(path)
                except OSError:
                    pass
        for n in os.listdir(nvme):
            pid = n.rsplit("-", 1)[-1]
            if n.startswith("ml-store-") and pid not in live and not _store_busy(nvme, pid):
                say(f"  removing {os.path.join(nvme, n)}, left by a dead run")
                shutil.rmtree(os.path.join(nvme, n), ignore_errors=True)


class MemorySampler(threading.Thread):
    """Peak memory of the container (cgroup v2 memory.peak, which counts
    the page cache of tilemaker's mmapped store, and the largest anon seen)."""

    def __init__(self, cidfile):
        super().__init__(daemon=True)
        self.cidfile = cidfile
        self.peak = 0
        self.anon = 0
        self.store = 0              # the store's largest size on disk
        self.stop = threading.Event()

    def run(self):
        cg = None
        while not self.stop.wait(5):
            self.store = max(self.store, dir_bytes(os.path.dirname(self.cidfile)))
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


def run_tilemaker(*, iid, name, lock_path, pbf, bbox, langs, part, store, timeout):
    """tilemaker in the image; True on success. It runs under timeout(1)
    inside the container, so a container whose client was killed still
    stops; the client waits a little longer, and the container is removed
    however this ends."""
    cidfile = os.path.join(store, "cid")
    store_in = os.path.join(store, "tm")
    os.makedirs(store_in)
    cmd = [docker_bin(), "run", "--rm", "--init", "--name", name,
           "--label", f"{LOCK_LABEL}={lock_path}", "--cidfile", cidfile,
           "--user", f"{os.getuid()}:{os.getgid()}",
           "--memory", MEMORY, "--memory-swap", MEMORY, "--cpus", str(CPUS),
           "-v", f"{os.path.realpath(pbf)}:/in/region.osm.pbf:ro",
           "-v", f"{os.path.join(ROOT, 'coastline')}:/w/coastline:ro",
           "-v", f"{os.path.join(ROOT, 'landcover')}:/w/landcover:ro",
           "-v", f"{os.path.dirname(part)}:/out", "-v", f"{store_in}:/store", "-w", "/w",
           "-e", f"STREETZIM_TILE_LANGUAGES={','.join(langs)}",
           "--entrypoint", "python3", iid, "-c", NICE_SHIM, io_class(),
           "timeout", "--kill-after=60", str(timeout),
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
        r = subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr, timeout=timeout + CLIENT_MARGIN)
        rc = r.returncode
        if rc == 124:
            say(f"  tilemaker passed {timeout} s: stopped in the container")
    except subprocess.TimeoutExpired:
        say(f"  tilemaker passed {timeout} s: stopping it")
        rc = None
    finally:
        sampler.stop.set()
        remove_container(name)
    secs = time.time() - t0
    gib = 1 << 30
    say(f"  tilemaker rc={rc} in {secs:.0f} s; peak memory {sampler.peak / gib:.1f} GiB "
        f"(anon {sampler.anon / gib:.1f} GiB); store up to {sampler.store / gib:.1f} GiB")
    return rc == 0, {"seconds": round(secs), "timeout": timeout, "peak_bytes": sampler.peak,
                     "peak_anon_bytes": sampler.anon, "store_peak_bytes": sampler.store}


# ---- the check ----------------------------------------------------------------

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
    are the bbox (tilemaker took --bbox), and among the zoom 0-8 tiles
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


# ---- the step -----------------------------------------------------------------

def make(rid, src_id, bbox, *, out_dir=None, store_dir=None):
    parse_bbox(bbox)
    io_class()
    # The extract's region decides them (a Light variant has its parent's).
    langs = languages(src_id)
    profile = checkout_inputs()
    out_dir = os.path.abspath(out_dir or os.environ.get("STREETZIM_ML_OUT_DIR")
                              or os.path.join(ROOT, "world-data", "regions"))
    store_dir = os.path.abspath(store_dir or os.environ.get("STREETZIM_ML_STORE_DIR")
                                or os.path.join(ROOT, "tmp"))
    pbf = os.path.join(ROOT, "world-data", "regions", f"{src_id}.osm.pbf")
    if not os.path.isfile(pbf) or os.path.getsize(pbf) == 0:
        fail(f"no extract {pbf}")
    if os.path.islink(pbf):
        fail(f"{pbf} is a symlink: tiles need a real extract (a planet link would "
             "tile the planet)")
    cut_for = read_sidecar(pbf + ".bbox")
    if cut_for is not None and cut_for != bbox:
        fail(f"{pbf} was cut for bbox {cut_for}, not {bbox}: extract it again first")
    if not extract_complete(pbf):
        fail(f"{pbf} is not marked as cut with osmium --strategy smart ({pbf}.strategy): a lake or "
             "forest crossing the box would be lost; extract it again first (ops/region-bbox.sh)")
    for shp in SHAPEFILES:
        if not os.path.isfile(os.path.join(ROOT, shp)):
            fail(f"{os.path.join(ROOT, shp)} is missing (the tiles would have no "
                 "ocean or landcover)")
    timeout = timeout_for(pbf)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(store_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{src_id}.ml.mbtiles")
    say(f"=== multilingual tiles for {rid}"
        + (f" (from {src_id})" if src_id != rid else "")
        + f": {len(langs)} languages, bbox {bbox}")

    # One run at a time on the host; the freshness check is under the lock
    # too, so a run that waited reuses what the one before it made.
    lock_path = os.path.realpath(os.path.join(store_dir, ".ml-tiles.lock"))
    lock = open(lock_path, "w")
    t_wait = time.time()
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        say("  waiting for another multilingual tile run (one at a time)")
        fcntl.flock(lock, fcntl.LOCK_EX)
        say(f"  waited {time.time() - t_wait:.0f} s")
    partdir = os.path.join(out_dir, f".ml-part-{src_id}-{os.getpid()}")
    part = os.path.join(partdir, f"{src_id}.ml.part.mbtiles")
    store, resv = None, None
    name = f"{CONTAINER}{src_id}"
    tag, pulled = None, False
    try:
        reap(out_dir, store_dir, lock_path)
        want = {"bbox": bbox, "langs": ",".join(langs), "profile": profile,
                "pbf": pbf_identity(pbf)}
        if is_fresh(out, want):
            say(f"  {out}: fresh (same bbox, languages, tile profile and extract), reused")
            return out
        rev = image_commit(profile)
        tag = tag_of(rev)
        # The tag is removed afterwards only when this run is what pulled it.
        pulled = image_id(tag)[0] is None
        iid = pull(tag, rev)
        for k in SIDECARS + ("image", "run.json"):    # they describe the old file
            try:
                os.unlink(f"{out}.{k}")
            except FileNotFoundError:
                pass
        shutil.rmtree(partdir, ignore_errors=True)
        os.makedirs(partdir)
        store, resv = place_store(pbf, store_dir, src_id)
        if resv:
            resv.start()
        remove_container(name)
        ok, stats = run_tilemaker(iid=iid, name=name, lock_path=lock_path, pbf=pbf, bbox=bbox,
                                  langs=langs, part=part, store=store, timeout=timeout)
        if not ok:
            fail(f"tilemaker failed for {rid}; see above")
        check_tiles(part, langs, bbox)
        os.replace(part, out)
        for k in SIDECARS:
            write_sidecar(f"{out}.{k}", want[k])
        stats.update(image=tag, image_id=iid, commit=rev, bbox=bbox, langs=langs,
                     profile=profile, store=os.path.dirname(store),
                     pbf_bytes=os.path.getsize(pbf), mbtiles_bytes=os.path.getsize(out),
                     seconds_per_gb=round(stats["seconds"] / max(os.path.getsize(pbf) / 1e9, 0.001)))
        write_sidecar(f"{out}.run.json", json.dumps(stats))
        say(f"  wrote {out} ({os.path.getsize(out) / 1e6:.0f} MB)")
        return out
    finally:
        # After a signal, quickly, as whoever sent it may not wait: a store
        # on the shared NVMe goes at once (a few large files), one on
        # /storage and the part folder are left to the next run's reap.
        if resv:
            resv.release()
        for d, on_nvme in ((partdir, False), (store, resv is not None)):
            if d and (on_nvme or not _SIGNALLED):
                shutil.rmtree(d, ignore_errors=True)
        if tag:
            drop_tag(tag, pulled)
        lock.close()


_SIGNALLED = False


def _end(signum, _frame):
    # Once: a second signal (timeout's, a shell's) must not cut the cleanup
    # short, as one did on 2026-10-13 and left the image tag behind.
    global _SIGNALLED
    _SIGNALLED = True
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
