#!/usr/bin/env python3
"""Build the polar map of Antarctica (the registry's antarctica-polar).

    build-antarctica-polar.py --extract REAL.osm.pbf --work DIR --output OUT.zim

The queue cannot build this map: its registry box is in map positions
(streetzim/polar.py), and a build by that box would cut a piece of the Gulf
of Guinea from the planet. It is built here and uploaded with
`parallel_rebuild.sh --regate antarctica-polar OUT.zim`.

REAL.osm.pbf is everything south of 60 S in real positions:
    osmium extract --bbox -180,-90,180,-60 --strategy smart planet.osm.pbf -o REAL.osm.pbf
The steps, each kept in DIR and reused while what it was made from is
unchanged:
  1. the extract with every node moved (tools/polar_data.py move);
  2. the sea (tools/polar_data.py ocean);
  3. tiles: tilemaker from the image region-ml-tiles.py would use for this
     checkout, with the tile profile's layers less the real-world files and
     the same label languages as any other map;
  4. terrain: every tile pixel's elevation read from the terrain cache's
     DEM cells at the real place it stands for (streetzim/polar_terrain.py);
  5. imagery: NASA's Blue Marble, which GIBS serves in the map's own
     positions, fetched once and cut into tiles (streetzim/polar_satellite.py);
  6. create_osm_zim.py --projection antarctic-polar, with routing, search,
     terrain, imagery and the sign reader. No Wikipedia: its places are in
     real positions, and nothing moves them yet.
"""
import argparse
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys

CODE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, CODE)
sys.path.insert(0, os.path.join(CODE, "tools"))
import polar_data  # noqa: E402
from streetzim import polar, polar_satellite, polar_terrain  # noqa: E402
from streetzim.satellite import satellite_cache_dirs  # noqa: E402

ID = "antarctica-polar"
NAME = "Antarctica with the South Pole"
BBOX = "-31,-30,31,30"
CONTAINER = "streetzim-polar-build"
# About 38 m a pixel at 71 S: what the 30 m DEM holds.
TERRAIN_ZOOM = 12
XAPIANBUILDER = "/home/ot/experiments/xapianbuilder/target/release/xapianbuilder"


def say(msg):
    print(msg, file=sys.stderr, flush=True)


def ml_tiles():
    """ops/region-ml-tiles.py as a module: the image and language rules."""
    spec = importlib.util.spec_from_file_location(
        "region_ml_tiles", os.path.join(CODE, "ops", "region-ml-tiles.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def identity(*paths):
    """What a step was made from: each file's size, time and name."""
    h = hashlib.sha256()
    for p in paths:
        st = os.stat(p)
        h.update(f"{os.path.abspath(p)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def step(out, made_from, make):
    """Run make() unless `out` is there and its .from sidecar says it was
    made from the same things."""
    mark = out + ".from"
    try:
        with open(mark, encoding="utf-8") as f:
            if f.read().strip() == made_from and os.path.getsize(out) > 0:
                say(f"  {out}: reused")
                return
    except OSError:
        pass
    if os.path.exists(mark):
        os.unlink(mark)
    make()
    with open(mark, "w", encoding="utf-8") as f:
        f.write(made_from + "\n")


def tiles(ml, moved, ocean_dir, cfg, out, langs, cpus):
    profile = ml.checkout_inputs()
    rev = ml.image_commit(profile)
    tag = ml.tag_of(rev)
    pulled = ml.image_id(tag)[0] is None
    iid = ml.pull(tag, rev)
    work = os.path.dirname(out)
    store = os.path.join(work, "store")
    part = os.path.join(work, "part.polar.mbtiles")
    shutil.rmtree(store, ignore_errors=True)
    os.makedirs(store)
    if os.path.exists(part):
        os.unlink(part)
    ml.remove_container(CONTAINER)
    cmd = [ml.docker_bin(), "run", "--rm", "--init", "--name", CONTAINER,
           "--user", f"{os.getuid()}:{os.getgid()}",
           "--memory", "12g", "--memory-swap", "12g", "--cpus", str(cpus),
           "-v", f"{os.path.realpath(moved)}:/in/region.osm.pbf:ro",
           "-v", f"{os.path.realpath(ocean_dir)}:/w/polar:ro",
           "-v", f"{os.path.realpath(os.path.dirname(cfg))}:/cfg:ro",
           "-v", f"{os.path.realpath(work)}:/out", "-v", f"{store}:/store", "-w", "/w",
           "-e", f"STREETZIM_TILE_LANGUAGES={','.join(langs)}",
           "--entrypoint", "timeout", iid, "--kill-after=60", "3600",
           "tilemaker", "--input", "/in/region.osm.pbf", "--output", f"/out/{os.path.basename(part)}",
           "--config", f"/cfg/{os.path.basename(cfg)}", "--process", ml.PROFILE[1],
           "--bbox", BBOX, "--store", "/store", "--threads", str(cpus), "--skip-integrity"]
    say("  " + " ".join(cmd))
    try:
        rc = subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr, timeout=3900).returncode
    except subprocess.TimeoutExpired:
        rc = "timeout"
    finally:
        ml.remove_container(CONTAINER)
        shutil.rmtree(store, ignore_errors=True)
        ml.drop_tag(tag, pulled)
    if rc != 0:
        raise SystemExit(f"FATAL: tilemaker failed (rc={rc})")
    ml.check_tiles(part, langs, BBOX)
    os.replace(part, out)
    return tag


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--extract", required=True, help="everything south of 60 S, real positions")
    ap.add_argument("--work", required=True, help="where the moved data and the tiles are kept")
    ap.add_argument("--output", required=True, help="the ZIM")
    ap.add_argument("--dem", default=os.path.join(CODE, "terrain_cache", "dem_sources"),
                    help="the terrain cache's DEM cells")
    ap.add_argument("--imagery", default=satellite_cache_dirs(polar_satellite.SOURCE.key)[0],
                    help="where the imagery's source tiles are kept")
    ap.add_argument("--cpus", type=int, default=6)
    ap.add_argument("--name", default=NAME)
    a, extra = ap.parse_known_args(argv)

    ml = ml_tiles()
    region = ml.region_row(ID)
    if not region:
        raise SystemExit(f"FATAL: no row for {ID} in cloud/region-languages.tsv")
    langs = ml.languages(ID)
    work = os.path.abspath(a.work)
    os.makedirs(os.path.join(work, "polar"), exist_ok=True)
    os.makedirs(os.path.join(work, "cfg"), exist_ok=True)
    moved = os.path.join(work, "moved.osm.pbf")
    ocean = os.path.join(work, "polar", "ocean.geojson")
    cfg = os.path.join(work, "cfg", "config-polar.json")
    mbtiles = os.path.join(work, "polar.mbtiles")
    terrain = os.path.join(work, "terrain")
    satellite = os.path.join(work, "satellite")
    box = tuple(float(v) for v in BBOX.split(","))
    if not os.path.isdir(a.dem):
        raise SystemExit(f"FATAL: no DEM cells at {a.dem} (--dem)")

    def do_move():
        n, left = polar_data.move(a.extract, moved)
        say(f"  moved {n} nodes, left out {left}")
        if not n:
            raise SystemExit(f"FATAL: {a.extract} has no node south of {-polar_data.NORTH_LIMIT:g} S")

    def do_ocean():
        pieces, pole = polar_data.ocean(moved, ocean)
        say(f"  sea in {pieces} pieces")
        if not pole:
            raise SystemExit("FATAL: the coastline does not close around the pole")

    def do_terrain():
        counts = polar_terrain.build(terrain, TERRAIN_ZOOM, a.dem, workers=a.cpus, say=say)
        if not counts[TERRAIN_ZOOM]:
            raise SystemExit(f"FATAL: no terrain tile from {a.dem}: not the DEM of the far south")

    def do_satellite():
        polar_satellite.build(satellite, a.imagery, box, workers=a.cpus, say=say)

    say("[1/6] moving the extract")
    step(moved, identity(a.extract), do_move)
    say("[2/6] the sea")
    step(ocean, identity(moved), do_ocean)
    say("[3/6] tiles")
    polar_data.write_tile_config(os.path.join(CODE, *ml.PROFILE[0].split("/")[2:]), cfg, "polar/ocean.geojson")
    with open(cfg, "rb") as f:
        cfg_hash = hashlib.sha256(f.read()).hexdigest()
    made_from = "\0".join([identity(moved, ocean), cfg_hash, ml.checkout_inputs(), ",".join(langs)])
    made_from = hashlib.sha256(made_from.encode()).hexdigest()
    step(mbtiles, made_from, lambda: tiles(ml, moved, os.path.dirname(ocean), cfg, mbtiles, langs, a.cpus))

    say("[4/6] terrain")
    step(terrain, polar_terrain.made_from(a.dem, TERRAIN_ZOOM), do_terrain)

    say("[5/6] imagery")
    try:
        polar_satellite.fetch(a.imagery, say=say)
    except RuntimeError as e:
        raise SystemExit(f"FATAL: the imagery could not be fetched: {e}") from None
    step(satellite, polar_satellite.made_from(a.imagery, box), do_satellite)

    say("[6/6] the ZIM")
    writer = ["--zim-builder=python", "--xapian=libzim"]
    if os.access(XAPIANBUILDER, os.X_OK):
        writer = ["--zim-builder=manifest", "--xapian=builder", f"--xapianbuilder-bin={XAPIANBUILDER}"]
    cmd = [sys.executable, os.path.join(CODE, "create_osm_zim.py"),
           "--pbf", moved, "--mbtiles", mbtiles, f"--bbox={BBOX}",
           "--projection", polar.NAME, "--name", a.name, "--map-zoom", "3",
           "--cpus", str(a.cpus), "--no-admin-areas",
           "--terrain", "--terrain-zoom", str(TERRAIN_ZOOM), "--terrain-dir", terrain,
           "--satellite", "--satellite-zoom", str(polar_satellite.TOP_ZOOM),
           "--satellite-dir", satellite,
           "--routing", "--spatial-chunk-scale", "10",
           "--split-hot-search-chunks-mb", "10", "--split-find-chips",
           "--ocr", "--no-llm-bundle",
           "--tile-languages", ",".join(langs), "--region-languages", ",".join(region),
           *writer, *extra, "--output", os.path.abspath(a.output)]
    say("  " + " ".join(cmd))
    raise SystemExit(subprocess.run(cmd, cwd=CODE).returncode)


if __name__ == "__main__":
    main()
