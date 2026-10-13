"""Multilingual region tiles (ops/region-ml-tiles.py) and how the build
wrappers use them (build-region-fast.sh's STREETZIM_ML_TILES block and its
traps, the rebuild driver's marker). docker is a stub here that records its
calls, answers registry lookups from a list, keeps a list of containers and
plays tilemaker: it writes an MBTiles with name:xx for the languages it is
given, or fails in the ways the step must catch. The checkout whose tile
inputs choose the image is a scratch git repository.

What must hold: the tiles come from a published main image with the
checkout's tile profile and Dockerfile (the newest on main's first-parent
line below the checkout), pulled (its tag removed afterwards only if this
run pulled it) and run as a guest (24g, no swap, 6 cpus, our uid, --rm
--init, tilemaker under timeout(1), the extract read-only); the output
appears only whole and checked, with sidecars, and is reused, without the
registry or a pull, only while its bbox, languages, tile inputs and extract
are the same; a failed lookup, pull, run or check leaves no output and no
fallback; containers, stores and part folders never outlive a run (or are
removed by the next one); and the wrapper switches to the tiles only with
STREETZIM_ML_TILES=1 and passes a signal on to the step.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops" / "region-ml-tiles.py"
REPO = "ghcr.io/jasontitus/streetzim"
BOX = "-74.5,17.5,-68.3,20.1"
LANGS = "ar,de,el,es,fa,fr,he,hi,id,it,ja,ko,nl,pl,pt,ru,th,tr,uk,vi,zh"
pytest.importorskip("mapbox_vector_tile")

FAKE_DOCKER = r'''
import json, os, sqlite3, sys, time
st = os.environ["FAKE_STATE"]
argv = sys.argv[1:]
with open(os.path.join(st, "calls"), "a") as f:
    f.write(json.dumps(argv) + "\n")
def lines(name):
    p = os.path.join(st, name)
    return open(p).read().split("\n") if os.path.exists(p) else []
present = os.path.join(st, "present")
if argv[:2] == ["image", "inspect"]:
    if not os.path.exists(present):
        sys.exit(1)
    print(os.environ.get("FAKE_IMAGE_ID", "sha256:aaa"), os.environ.get("FAKE_REV", ""))
    sys.exit(0)
if argv[:2] == ["manifest", "inspect"]:
    sys.exit(0 if argv[2] in lines("published") else 1)
if argv[0] == "ps":
    want = argv[argv.index("--filter") + 1].split("=", 1)[1]
    for row in lines("containers"):
        if row and row.split("\t")[1] == want:
            print(row.split("\t")[0])
    sys.exit(0)
if argv[0] == "pull":
    if os.environ.get("FAKE_PULL_FAIL"):
        sys.exit(1)
    open(present, "w").close()
    sys.exit(0)
if argv[0] == "rmi":
    n = os.path.join(st, "rmi-conflicts")
    left = int(open(n).read()) if os.path.exists(n) else 0
    if left:
        open(n, "w").write(str(left - 1))
        print("Error response from daemon: conflict: unable to delete (must be forced) - container x",
              file=sys.stderr)
        sys.exit(1)
    os.unlink(present)
    sys.exit(0)
if argv[0] == "rm":
    time.sleep(float(os.environ.get("FAKE_RM_SECS", "0")))
    open(os.path.join(st, "rm-done"), "a").write("x")
    rows = [r for r in lines("containers") if r and r.split("\t")[0] != argv[-1]]
    open(os.path.join(st, "containers"), "w").write("\n".join(rows))
    sys.exit(0)
assert argv[0] == "run", argv
mounts, env = {}, {}
i = 0
while i < len(argv):
    a = argv[i]
    if a == "-v":
        host, ctr = argv[i + 1].split(":")[:2]
        mounts[ctr] = host
    elif a == "-e":
        k, v = argv[i + 1].split("=", 1)
        env[k] = v
    elif a == "--cidfile":
        open(argv[i + 1], "w").write("fakecid")
    i += 1
def opt(name):
    return argv[argv.index(name) + 1]
open(os.path.join(mounts["/store"], "mmap_0.dat"), "w").write("store")
out = os.path.join(mounts["/out"], os.path.basename(opt("--output")))
limit = int(argv[argv.index("timeout") + 2])
mode = os.environ.get("FAKE_TM", "ok")
if mode == "fail":
    sys.exit(3)
if mode == "sleep":                     # timeout(1) inside the container stops it
    time.sleep(min(60, limit))
    sys.exit(124)
if mode == "hang":                      # a container that never answers
    time.sleep(60)
if mode == "dir":
    os.makedirs(os.path.join(out, "14"))
    sys.exit(0)
import mapbox_vector_tile as mvt
langs = env["STREETZIM_TILE_LANGUAGES"].split(",")
props = {"name:latin": "Santo Domingo", "class": "city"}
if mode != "nolang":
    props.update({f"name:{lg}": f"SD-{lg}" for lg in langs})
if mode == "stray":
    props["name:fi"] = "SD-fi"
def tile(p):
    return mvt.encode([{"name": "place", "features": [{"geometry": "POINT(10 10)", "properties": p}]}])
c = sqlite3.connect(out)
c.execute("create table metadata (name text, value text)")
c.execute("create table tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob)")
bounds = opt("--bbox") if mode != "bounds" else "0,0,1,1"
for k, v in (("format", "png" if mode == "format" else "pbf"), ("bounds", bounds), ("maxzoom", "14")):
    c.execute("insert into metadata values (?, ?)", (k, v))
# The zoom-0 tile names nothing but the English: name:xx first appear at zoom 6.
c.execute("insert into tiles values (0, 0, 0, ?)", (tile({"name:latin": "Caribbean", "class": "sea"}),))
for z in (6, 10) if mode == "nozoom" else (6, 10, 14):
    c.execute("insert into tiles values (?, 0, 0, ?)", (z, tile(props)))
c.commit()
c.close()
open(os.path.join(st, "part-ino"), "w").write(str(os.stat(out).st_ino))
'''


def _gitenv():
    return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class Checkout:
    """A scratch git checkout with a tile profile and a Dockerfile; its
    origin/main is set explicitly."""

    def __init__(self, path: Path):
        self.path = path
        (path / "resources" / "tilemaker").mkdir(parents=True)
        (path / "resources" / "tilemaker" / "process-openmaptiles.lua").write_text("-- profile A\n")
        (path / "Dockerfile").write_text("FROM x\nARG TILEMAKER_REF=v3.2.0\n")
        (path / "rebuild-old.list").write_text("a\n")
        self.git("init", "-q", "-b", "main")
        self.commit("main 1")
        self.main()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.path), *args], check=True,
                              capture_output=True, text=True, env=_gitenv()).stdout.strip()

    def commit(self, msg, files=None):
        for rel, text in (files or {}).items():
            (self.path / rel).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", msg, "--allow-empty")
        return self.head()

    def head(self):
        return self.git("rev-parse", "HEAD")

    def main(self, rev="HEAD"):
        self.git("update-ref", "refs/remotes/origin/main", rev)


class Host(type(Path())):           # tmp_path that also knows its checkout
    co: Checkout


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A data root with an extract and the shapefiles, a stub docker, a
    checkout whose HEAD has a published image, and the environment the step
    reads."""
    root = tmp_path / "root"
    regions = root / "world-data" / "regions"
    regions.mkdir(parents=True)
    (regions / "hispaniola.osm.pbf").write_bytes(b"pbf" * 100)
    (regions / "africa.osm.pbf").write_bytes(b"pbf" * 100)
    for shp in ("coastline/water_polygons.shp",
                "landcover/ne_10m_urban_areas/ne_10m_urban_areas.shp",
                "landcover/ne_10m_antarctic_ice_shelves_polys/ne_10m_antarctic_ice_shelves_polys.shp",
                "landcover/ne_10m_glaciated_areas/ne_10m_glaciated_areas.shp"):
        (root / shp).parent.mkdir(parents=True, exist_ok=True)
        (root / shp).write_bytes(b"shp")
    state = tmp_path / "docker"
    state.mkdir()
    docker = tmp_path / "docker.py"
    docker.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
    docker.chmod(0o755)
    co = Checkout(tmp_path / "checkout")
    env = {"STREETZIM_ROOT": str(root), "DOCKER": str(docker), "FAKE_STATE": str(state),
           "FAKE_REV": co.head(), "STREETZIM_ML_CHECKOUT": str(co.path),
           "STREETZIM_ML_STORE_DIR": str(tmp_path / "store")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in ("STREETZIM_TILE_LANGUAGES", "STREETZIM_ML_OUT_DIR", "STREETZIM_ML_IO",
              "STREETZIM_ML_TIMEOUT", "STREETZIM_ML_COMMIT", "FAKE_TM", "FAKE_PULL_FAIL",
              "FAKE_IMAGE_ID", "FAKE_RM_SECS"):
        monkeypatch.delenv(k, raising=False)
    publish(tmp_path, co.head())
    h = Host(tmp_path)
    h.co = co
    return h


def publish(host, rev):
    with open(host / "docker" / "published", "a") as f:
        f.write(f"{REPO}:sha-{rev[:12]}\n")


def tag(rev):
    return f"{REPO}:sha-{rev[:12]}"


def _load():
    spec = importlib.util.spec_from_file_location("region_ml_tiles", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _calls(host) -> list[list[str]]:
    p = host / "docker" / "calls"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def _runs(host):
    return [c for c in _calls(host) if c[0] == "run"]


def _out(host, rid="hispaniola") -> Path:
    return host / "root" / "world-data" / "regions" / f"{rid}.ml.mbtiles"


def _leftovers(host):
    regions = host / "root" / "world-data" / "regions"
    store = host / "store"
    return ([p.name for p in regions.iterdir() if p.name.startswith(".ml-part-")]
            + [p.name for p in store.iterdir() if p.name.startswith("ml-store-")]
            if store.exists() else [])


def _inputs(co: Checkout, rev="HEAD"):
    tree = co.git("rev-parse", f"{rev}:resources/tilemaker")
    blob = co.git("rev-parse", f"{rev}:Dockerfile")
    return f"tilemaker={tree} dockerfile={blob} ref=v3.2.0"


# ---- the run -------------------------------------------------------------------

def test_builds_from_the_commits_image_as_a_guest(host):
    co = host.co
    out = _load().make("hispaniola", "hispaniola", BOX)
    assert out == str(_out(host))
    assert Path(out + ".bbox").read_text() == BOX + "\n"
    assert Path(out + ".langs").read_text() == LANGS + "\n"
    assert Path(out + ".profile").read_text() == _inputs(co) + "\n"
    pbf = host / "root" / "world-data" / "regions" / "hispaniola.osm.pbf"
    st = pbf.stat()
    assert Path(out + ".pbf").read_text() == f"{st.st_size} {st.st_mtime_ns}\n"
    run_info = json.loads(Path(out + ".run.json").read_text())
    assert run_info["commit"] == co.head() and run_info["image"] == tag(co.head())
    run, = _runs(host)
    flags = " ".join(run)
    assert run[1:4] == ["--rm", "--init", "--name"]
    assert "--memory 24g --memory-swap 24g --cpus 6 " in flags
    assert f"--user {os.getuid()}:{os.getgid()} " in flags
    assert f"--label streetzim.mltiles.lock={host}/store/.ml-tiles.lock " in flags
    assert f"-v {pbf}:/in/region.osm.pbf:ro" in flags
    assert f"-v {host}/root/coastline:/w/coastline:ro" in flags
    # tilemaker under timeout(1) in the container: an hour plus half an hour per GB.
    assert f"timeout --kill-after=60 {3600 + int(1800 * 300 / 1e9)} tilemaker " in flags
    assert "sha256:aaa" in run and tag(co.head()) not in run      # runs the id it checked
    assert f"STREETZIM_TILE_LANGUAGES={LANGS}" in run
    assert run[run.index("--bbox") + 1] == BOX
    assert run[run.index("--config") + 1] == "/app/resources/tilemaker/config-openmaptiles.json"
    names = [c[0] for c in _calls(host)]
    assert names.count("pull") == 1 and ["rmi", tag(co.head())] in _calls(host)
    assert _leftovers(host) == []
    with sqlite3.connect(out) as c:
        assert c.execute("select count(*) from tiles").fetchone()[0] == 4
    # Renamed into place (atomic), not copied: the very file tilemaker wrote.
    assert Path(out).stat().st_ino == int((host / "docker" / "part-ino").read_text())


def test_fresh_tiles_need_no_registry_and_no_pull(host):
    m = _load()
    m.make("hispaniola", "hispaniola", BOX)
    n = len(_calls(host))
    m.make("hispaniola", "hispaniola", BOX)
    assert [c[0] for c in _calls(host)[n:]] == ["ps"]          # the orphan check only


def test_any_input_that_decides_the_tiles_remakes_them(host, monkeypatch):
    m = _load()
    co = host.co
    m.make("hispaniola", "hispaniola", BOX)
    monkeypatch.setenv("FAKE_IMAGE_ID", "sha256:bbb")          # a new image, same profile
    co.commit("host: lists", {"rebuild-old.list": "b\n"})
    co.commit("main: docs", {"README": "x\n"})
    co.main()
    publish(host, co.head())
    monkeypatch.setenv("FAKE_REV", co.head())
    m.make("hispaniola", "hispaniola", BOX)
    assert len(_runs(host)) == 1
    monkeypatch.setenv("STREETZIM_TILE_LANGUAGES", "de,fr")    # languages
    m.make("hispaniola", "hispaniola", BOX)
    assert len(_runs(host)) == 2 and Path(str(_out(host)) + ".langs").read_text() == "de,fr\n"
    m.make("hispaniola", "hispaniola", "-74.5,17.5,-68.3,20.2")  # bbox
    assert len(_runs(host)) == 3
    rev = co.commit("main: profile B", {"resources/tilemaker/process-openmaptiles.lua": "-- B\n"})
    co.main()
    publish(host, rev)
    monkeypatch.setenv("FAKE_REV", rev)
    m.make("hispaniola", "hispaniola", "-74.5,17.5,-68.3,20.2")  # the tile profile
    assert len(_runs(host)) == 4
    assert Path(str(_out(host)) + ".profile").read_text() == _inputs(co) + "\n"
    pbf = host / "root" / "world-data" / "regions" / "hispaniola.osm.pbf"
    os.utime(pbf, ns=(time.time_ns(), time.time_ns() + 10**9))   # a new extract
    m.make("hispaniola", "hispaniola", "-74.5,17.5,-68.3,20.2")
    assert len(_runs(host)) == 5
    Path(str(_out(host)) + ".profile").unlink()                  # a sidecar gone
    m.make("hispaniola", "hispaniola", "-74.5,17.5,-68.3,20.2")
    assert len(_runs(host)) == 6


def test_a_symlinked_output_is_not_fresh(host):
    m = _load()
    out = _out(host)
    m.make("hispaniola", "hispaniola", BOX)
    copy = out.with_name("elsewhere.mbtiles")
    out.rename(copy)
    out.symlink_to(copy)
    m.make("hispaniola", "hispaniola", BOX)
    assert len(_runs(host)) == 2 and not out.is_symlink()


def test_a_variant_uses_its_parents_extract_and_file(host):
    out = _load().make("africa-light", "africa", BOX)
    assert out == str(_out(host, "africa"))
    assert f"{host}/root/world-data/regions/africa.osm.pbf:/in/region.osm.pbf:ro" in " ".join(_runs(host)[0])


def test_a_failed_pull_fails_with_no_fallback(host, monkeypatch):
    monkeypatch.setenv("FAKE_PULL_FAIL", "1")
    with pytest.raises(SystemExit, match="cannot pull .*Not falling back"):
        _load().make("hispaniola", "hispaniola", BOX)
    assert _runs(host) == [] and not _out(host).exists()


def test_an_image_of_another_commit_is_refused(host, monkeypatch):
    monkeypatch.setenv("FAKE_REV", "f" * 40)
    with pytest.raises(SystemExit, match="built from f+, not"):
        _load().make("hispaniola", "hispaniola", BOX)
    assert _runs(host) == [] and ["rmi", tag(host.co.head())] in _calls(host)


def test_a_tag_already_there_must_be_the_commits_too(host, monkeypatch):
    (host / "docker" / "present").touch()
    monkeypatch.setenv("FAKE_REV", "f" * 40)
    with pytest.raises(SystemExit, match="built from f+, not"):
        _load().make("hispaniola", "hispaniola", BOX)
    assert _runs(host) == [] and not any(c[0] in ("pull", "rmi") for c in _calls(host))


def test_a_tag_that_was_there_before_is_kept(host):
    (host / "docker" / "present").touch()
    _load().make("hispaniola", "hispaniola", BOX)
    assert not any(c[0] in ("pull", "rmi") for c in _calls(host))


def test_the_tag_is_removed_once_the_container_lets_go(host, monkeypatch):
    m = _load()
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    (host / "docker" / "rmi-conflicts").write_text("2")
    m.make("hispaniola", "hispaniola", BOX)
    assert [c for c in _calls(host) if c[0] == "rmi"] == [["rmi", tag(host.co.head())]] * 3
    assert not (host / "docker" / "present").exists()


@pytest.mark.parametrize("mode, why", [("fail", "tilemaker failed"),
                                       ("sleep", "tilemaker failed"),
                                       ("dir", "a directory"),
                                       ("nolang", "no name:xx"),
                                       ("stray", "name:fi"),
                                       ("bounds", "bounds"),
                                       ("nozoom", "no tile at zoom 14"),
                                       ("format", "format 'png'")])
def test_bad_output_is_never_published(host, monkeypatch, capfd, mode, why):
    m = _load()
    m.make("hispaniola", "hispaniola", BOX)
    old = _out(host).read_bytes()
    monkeypatch.setenv("STREETZIM_TILE_LANGUAGES", "de")
    monkeypatch.setenv("STREETZIM_ML_TIMEOUT", "1")
    monkeypatch.setenv("FAKE_TM", mode)
    capfd.readouterr()
    with pytest.raises(SystemExit, match=why):
        m.make("hispaniola", "hispaniola", BOX)
    if mode == "sleep":
        assert "passed 1 s: stopped in the container" in capfd.readouterr().err
    # The old file is left, but no longer passes for this run's (or any) inputs.
    assert _out(host).read_bytes() == old
    assert not Path(str(_out(host)) + ".langs").exists()
    assert _leftovers(host) == [] and ["rmi", tag(host.co.head())] == _calls(host)[-1]
    monkeypatch.setenv("FAKE_TM", "ok")
    m.make("hispaniola", "hispaniola", BOX)
    assert Path(str(_out(host)) + ".langs").read_text() == "de\n"


def test_the_client_gives_up_on_a_container_that_never_answers(host, monkeypatch, capfd):
    m = _load()
    monkeypatch.setattr(m, "CLIENT_MARGIN", 1)
    monkeypatch.setenv("STREETZIM_ML_TIMEOUT", "1")
    monkeypatch.setenv("FAKE_TM", "hang")
    with pytest.raises(SystemExit, match="tilemaker failed"):
        m.make("hispaniola", "hispaniola", BOX)
    assert "passed 1 s: stopping it" in capfd.readouterr().err
    assert ["rm", "-f", "streetzim-mltiles-hispaniola"] in _calls(host)
    assert _leftovers(host) == [] and not _out(host).exists()


def test_the_time_limit_grows_with_the_extract(host, monkeypatch):
    m = _load()
    pbf = host / "root" / "world-data" / "regions" / "hispaniola.osm.pbf"
    os.truncate(pbf, 2_000_000_000)                 # sparse: 2 GB that take no room
    assert m.timeout_for(str(pbf)) == 3600 + 2 * 1800
    monkeypatch.setenv("STREETZIM_ML_TIMEOUT", "900")
    assert m.timeout_for(str(pbf)) == 900
    monkeypatch.setenv("STREETZIM_ML_TIMEOUT", "an hour")
    with pytest.raises(SystemExit, match="not a number"):
        m.timeout_for(str(pbf))


@pytest.mark.parametrize("setup, why", [
    (lambda r: None, "crosses the antimeridian"),
    (lambda r: (r / "hispaniola.osm.pbf").unlink(), "no extract"),
    (lambda r: ((r / "hispaniola.osm.pbf").unlink(),
                (r / "hispaniola.osm.pbf").symlink_to(r / "africa.osm.pbf")), "symlink"),
    (lambda r: (r / "hispaniola.osm.pbf.bbox").write_text("1,2,3,4\n"), "cut for bbox 1,2,3,4"),
    (lambda r: (r.parents[1] / "coastline" / "water_polygons.shp").unlink(), "water_polygons"),
])
def test_wrong_inputs_stop_before_docker(host, setup, why):
    regions = host / "root" / "world-data" / "regions"
    setup(regions)
    box = "172.0,51.0,-130.0,72.0" if "antimeridian" in why else BOX
    with pytest.raises(SystemExit, match=why):
        _load().make("hispaniola", "hispaniola", box)
    assert _calls(host) == []


@pytest.mark.parametrize("var, value", [("STREETZIM_TILE_LANGUAGES", "en,de"),
                                        ("STREETZIM_TILE_LANGUAGES", "xx"),
                                        ("STREETZIM_ML_IO", "fast"),
                                        ("STREETZIM_ML_TIMEOUT", "an hour")])
def test_bad_settings_stop_before_docker(host, monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    with pytest.raises(SystemExit):
        _load().make("hispaniola", "hispaniola", BOX)
    assert _calls(host) == []


@pytest.mark.parametrize("change", ["edited", "untracked", "dockerfile"])
def test_tile_inputs_not_committed_stop_before_docker(host, change):
    co = host.co
    if change == "edited":
        (co.path / "resources" / "tilemaker" / "process-openmaptiles.lua").write_text("-- local\n")
    elif change == "untracked":
        (co.path / "resources" / "tilemaker" / "extra.lua").write_text("-- new\n")
    else:
        (co.path / "Dockerfile").write_text("FROM x\nARG TILEMAKER_REF=v3.3.0\n")
    with pytest.raises(SystemExit, match="not committed"):
        _load().make("hispaniola", "hispaniola", BOX)
    assert _calls(host) == []


def test_idle_io_is_opt_in(host, monkeypatch):
    m = _load()
    m.make("hispaniola", "hispaniola", BOX)
    run = _runs(host)[-1]
    assert run[run.index(m.NICE_SHIM) + 1] == "normal"
    monkeypatch.setenv("STREETZIM_TILE_LANGUAGES", "de")
    monkeypatch.setenv("STREETZIM_ML_IO", "idle")
    m.make("hispaniola", "hispaniola", BOX)
    run = _runs(host)[-1]
    assert run[run.index(m.NICE_SHIM) + 1] == "idle"


def test_the_nice_shim_execs_its_command():
    m = _load()
    r = subprocess.run([sys.executable, "-c", m.NICE_SHIM, "normal", "sh", "-c", "echo $0-ok", "x"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout == "x-ok\n"


# ---- what a killed run leaves ----------------------------------------------------

def test_containers_and_folders_left_by_dead_runs_are_removed(host):
    regions = host / "root" / "world-data" / "regions"
    store = host / "store"
    store.mkdir()
    dead = 2 ** 22 + 12345                     # above pid_max's default: never alive
    (regions / f".ml-part-x-{dead}").mkdir()
    (store / f"ml-store-x-{dead}").mkdir()
    (store / f"ml-store-x-{os.getpid()}").mkdir()        # alive: kept
    lock = f"{store}/.ml-tiles.lock"
    other = "streetzim.mltiles.lock=/elsewhere/.ml-tiles.lock"      # another lock's run
    (host / "docker" / "containers").write_text(
        f"c-ours\tstreetzim.mltiles.lock={lock}\nc-other\t{other}")
    _load().make("hispaniola", "hispaniola", BOX)
    assert _leftovers(host) == [f"ml-store-x-{os.getpid()}"]
    calls = _calls(host)
    assert ["rm", "-f", "c-ours"] in calls and ["rm", "-f", "c-other"] not in calls
    # Before anything else of this run.
    assert calls.index(["rm", "-f", "c-ours"]) < min(i for i, c in enumerate(calls) if c[0] == "run")
    assert (host / "docker" / "containers").read_text() == f"c-other\t{other}"


def _cli(host, *extra_env):
    env = {**os.environ, **dict(extra_env)}
    return subprocess.Popen([sys.executable, str(SCRIPT), "--", "hispaniola", BOX], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_one_run_at_a_time(host):
    (host / "store").mkdir()
    lock = open(host / "store" / ".ml-tiles.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    p = _cli(host)
    time.sleep(3)
    assert p.poll() is None and _calls(host) == []      # waiting, docker untouched
    fcntl.flock(lock, fcntl.LOCK_UN)
    out, err = p.communicate(timeout=60)
    assert p.returncode == 0 and out == str(_out(host)) + "\n"
    assert "waiting for another multilingual tile run" in err


def test_a_signal_removes_the_container_and_the_store(host):
    p = _cli(host, ("FAKE_TM", "sleep"), ("FAKE_RM_SECS", "1"))
    for _ in range(100):
        if _runs(host):
            break
        time.sleep(0.1)
    time.sleep(0.5)
    done = host / "docker" / "rm-done"
    assert done.read_text() == "x"      # the rm -f before the run
    p.send_signal(signal.SIGTERM)
    time.sleep(0.5)
    # timeout(1) forwards one too; it lands while the container is being
    # removed, and must not cut that short.
    p.send_signal(signal.SIGTERM)
    p.communicate(timeout=60)
    assert p.returncode == 128 + signal.SIGTERM
    assert done.read_text() == "xx"
    assert ["rm", "-f", "streetzim-mltiles-hispaniola"] in _calls(host)[-3:]
    assert _calls(host)[-1] == ["rmi", tag(host.co.head())]
    assert _leftovers(host) == [] and not _out(host).exists()


# ---- which image -----------------------------------------------------------------

@pytest.fixture
def lookup(host):
    return _load(), host.co, host


def test_the_image_is_heads_when_head_is_mains(lookup):
    m, co, _ = lookup
    assert m.image_commit(_inputs(co)) == co.head()


def test_a_host_commit_on_top_uses_the_main_commit_below_it(lookup, capsys):
    m, co, _ = lookup
    main = co.head()
    co.commit("host: torrent", {"rebuild-old.list": "b\n"})
    assert m.image_commit(_inputs(co)) == main
    assert f"image: main's {main[:12]}" in capsys.readouterr().err


def test_an_unpublished_tip_steps_back_to_a_published_commit_with_the_same_inputs(lookup):
    m, co, host = lookup
    b1 = co.commit("main: profile B", {"resources/tilemaker/process-openmaptiles.lua": "-- B\n"})
    publish(host, b1)
    co.commit("main: docs, pushed with the next one", {"README": "x\n"})    # no image
    co.commit("main: tip, CI not done", {"README": "y\n"})                   # no image
    co.main()
    assert m.image_commit(_inputs(co)) == b1


def test_a_published_image_with_another_profile_is_not_taken(lookup):
    m, co, host = lookup
    co.commit("main: profile B", {"resources/tilemaker/process-openmaptiles.lua": "-- B\n"})
    co.main()                       # unpublished; profile A's image below it is published
    with pytest.raises(SystemExit, match="no published image"):
        m.image_commit(_inputs(co))


def test_a_host_commit_merged_into_main_is_not_taken(lookup):
    """The host commits on top of an older main; main merges that as a
    second parent; the host's checkout is then its own commit plus another
    (the review's gitsim: merge-base gave the host commit, which has no
    image)."""
    m, co, host = lookup
    base = co.head()
    co.git("checkout", "-q", "-b", "host")
    h1 = co.commit("host: torrent 1", {"rebuild-old.list": "b\n"})
    co.git("checkout", "-q", "main")
    co.commit("main: work", {"README": "x\n"})
    co.git("merge", "-q", "--no-ff", "-m", "Merge host", "host")
    co.main()
    publish(host, co.head())
    co.git("checkout", "-q", "host")
    co.commit("host: torrent 2", {"rebuild-old.list": "c\n"})
    assert co.git("merge-base", "HEAD", "origin/main") == h1
    assert m.image_commit(_inputs(co)) == base


def test_no_image_with_these_inputs_fails_precisely(lookup):
    m, co, host = lookup
    (host / "docker" / "published").write_text("")
    first = co.head()
    tip = co.commit("main: tip", {"README": "x\n"})
    co.main()
    with pytest.raises(SystemExit, match=f"no published image .*at or below {tip[:12]} "
                                         rf"with these inputs \({tip[:12]}, {first[:12]}\)"):
        m.image_commit(_inputs(co))


def test_no_main_is_refused(lookup):
    m, co, _ = lookup
    co.git("update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(SystemExit, match="cannot read origin/main"):
        m.image_commit(_inputs(co))


def test_the_override_is_checked_like_the_rest(lookup, monkeypatch):
    m, co, host = lookup
    a = co.head()
    b = co.commit("main: profile B", {"resources/tilemaker/process-openmaptiles.lua": "-- B\n"})
    co.main()
    publish(host, b)
    want = _inputs(co)
    monkeypatch.setenv("STREETZIM_ML_COMMIT", a)
    with pytest.raises(SystemExit, match="tile inputs .* are not this checkout's"):
        m.image_commit(want)
    monkeypatch.setenv("STREETZIM_ML_COMMIT", "deadbeef0000")
    with pytest.raises(SystemExit, match="is not a commit"):
        m.image_commit(want)
    c = co.commit("main: docs", {"README": "x\n"})
    monkeypatch.setenv("STREETZIM_ML_COMMIT", c[:12])
    with pytest.raises(SystemExit, match="has no published image"):
        m.image_commit(want)
    monkeypatch.setenv("STREETZIM_ML_COMMIT", b[:12])
    assert m.image_commit(want) == b


# ---- build-region-fast.sh ---------------------------------------------------

WRAPPER = ROOT / "ops" / "build-region-fast.sh"


def _wrapper_block() -> str:
    """From the cleanup and traps through the multilingual tile step."""
    text = WRAPPER.read_text()
    i = text.index('STAGED_MBTILES=""')
    j = text.index("ML_ARGS=()", i)
    return text[i:text.index("\nesac\n", j) + 6]


def _wrapper_body(tmp_path, py, rid="hispaniola"):
    table = tmp_path / "region-languages.tsv"
    table.write_text("# id\tlanguages\nhispaniola\tes,fr,ht\nafrica-light\tar,fr\n")
    block = _wrapper_block().replace("/storage/streetzim/cloud/region-languages.tsv", str(table))
    return (f"set -euo pipefail\nPY={py}\nID={rid}\nSRC_ID=src\nBBOX={BOX}\n"
            f"TMPDIR={tmp_path}\nMBTILES=world.mbtiles\n{block}\n"
            'echo "MBTILES=$MBTILES"\necho "ML_ARGS=${ML_ARGS[*]-}"\n')


def _wrapper(tmp_path, ml, *, py_ok=True, langs=True, rid="hispaniola"):
    tiles = tmp_path / "x.ml.mbtiles"
    py = tmp_path / "py"
    py.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        echo "$*" >> {tmp_path}/py.log
        echo "some log on stderr" >&2
        {'exit 4' if not py_ok else ''}
        echo x > {tiles}
        {f'echo ar,de > {tiles}.langs' if langs else ''}
        echo {tiles}
        """))
    py.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "STREETZIM_ML_TILES"}
    if ml is not None:
        env["STREETZIM_ML_TILES"] = ml
    r = subprocess.run(["bash", "-c", _wrapper_body(tmp_path, py, rid)], env=env,
                       capture_output=True, text=True, timeout=60)
    calls = (tmp_path / "py.log").read_text() if (tmp_path / "py.log").exists() else ""
    return r, calls, tiles


@pytest.mark.parametrize("ml", [None, "0"])
def test_wrapper_without_the_flag_changes_nothing(tmp_path, ml):
    r, calls, _ = _wrapper(tmp_path, ml)
    assert r.returncode == 0 and calls == ""
    assert "MBTILES=world.mbtiles\nML_ARGS=\n" in r.stdout


def test_wrapper_with_the_flag_uses_the_tiles_and_the_languages(tmp_path):
    r, calls, tiles = _wrapper(tmp_path, "1")
    assert r.returncode == 0, r.stderr
    assert calls.split() == ["/storage/streetzim/ops/region-ml-tiles.py", "--src-id", "src",
                             "--", "hispaniola", BOX]
    assert f"MBTILES={tiles}\n" in r.stdout
    assert "ML_ARGS=--tile-languages ar,de --region-languages es,fr,ht\n" in r.stdout
    assert not list(tmp_path.glob("ml-tiles-*"))          # its output file is gone


@pytest.mark.parametrize("ml, kw, why", [
    ("1", {"py_ok": False}, "multilingual tiles for hispaniola failed"),
    ("1", {"langs": False}, "not tiles with a .langs sidecar"),
    ("1", {"rid": "nowhere"}, "no row for nowhere"),
    ("1", {"rid": "africa"}, "no row for africa"),           # africa-light's row is not africa's
    ("yes", {}, "must be 0 or 1"),
])
def test_wrapper_stops_rather_than_build_from_the_world_tiles(tmp_path, ml, kw, why):
    r, calls, _ = _wrapper(tmp_path, ml, **kw)
    assert r.returncode == 1 and why in r.stderr
    assert "MBTILES=" not in r.stdout
    if kw.get("rid") or ml == "yes":
        assert calls == ""          # stopped before the tile step


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_to_the_wrapper_reaches_the_tile_step(tmp_path, sig):
    py = tmp_path / "py"
    py.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import os, signal, sys, time
        def end(n, f):
            open("{tmp_path}/py.signal", "w").write(str(n))
            time.sleep(0.5)                       # its cleanup
            open("{tmp_path}/py.cleaned", "w").write("yes")
            sys.exit(128 + n)
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(s, end)
        open("{tmp_path}/py.pid", "w").write(str(os.getpid()))
        time.sleep(60)
        """))
    py.chmod(0o755)
    env = {**os.environ, "STREETZIM_ML_TILES": "1"}
    p = subprocess.Popen(["bash", "-c", _wrapper_body(tmp_path, py)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        if (tmp_path / "py.pid").exists():
            break
        time.sleep(0.1)
    child = int((tmp_path / "py.pid").read_text())
    p.send_signal(sig)
    out, _ = p.communicate(timeout=30)
    assert p.returncode == -sig                  # the wrapper ends by the same signal
    assert (tmp_path / "py.signal").read_text() == str(int(sig))
    assert (tmp_path / "py.cleaned").read_text() == "yes"     # waited for its cleanup
    assert not Path(f"/proc/{child}").exists()
    assert "MBTILES=" not in out


def test_wrapper_order():
    text = WRAPPER.read_text()
    assert text.index('ARGS+=( "${ML_ARGS[@]}" )') > text.index("ARGS=(")
    # The traps before the tile step; the step after the ALREADY EXISTS skip
    # and before the NVMe staging copies $MBTILES.
    assert (text.index("ALREADY EXISTS") < text.index("trap 'stop_ml TERM;")
            < text.index("ML_ARGS=()") < text.index("NVME_SCRATCH="))


# ---- the rebuild driver's marker -------------------------------------------

def _marker_snippet() -> str:
    text = (ROOT / "ops" / "cloud" / "rebuild_old_regions.sh").read_text()
    i = text.index("# A language-ready round")
    return text[i:text.index("for k, v in checks.items()", i)]


class _Archive:
    def __init__(self, cfg):
        self.cfg = json.dumps(cfg).encode()

    def get_entry_by_path(self, path):
        assert path == "map-config.json"
        cfg = self.cfg
        return type("E", (), {"get_item": lambda s: type("I", (), {"content": cfg})()})()


@pytest.mark.parametrize("env, cfg, want", [
    (None, {}, None),
    ("1", {"tileLanguages": ["de"], "regionLanguages": ["el"]}, True),
    ("1", {"regionLanguages": ["el"]}, False),
    ("1", {"tileLanguages": ["de"]}, False),
    ("1", {}, False),
])
def test_driver_marker(monkeypatch, env, cfg, want):
    if env is None:
        monkeypatch.delenv("STREETZIM_ML_TILES", raising=False)
    else:
        monkeypatch.setenv("STREETZIM_ML_TILES", env)
    checks: dict[str, bool] = {}
    exec(_marker_snippet(), {"a": _Archive(cfg), "checks": checks})
    assert checks.get("ml languages") is want


# ---- cloud/region-languages.tsv beside the ops tables ----------------------

def test_a_variant_has_its_parents_languages():
    def rows(name):
        return [ln.split("\t") for ln in (ROOT / "cloud" / name).read_text().splitlines()
                if ln.strip() and not ln.startswith("#")]
    langs = dict(rows("region-languages.tsv"))
    variants = rows("region-variants.tsv")
    assert variants
    for row in variants:
        assert langs[row[0]] == langs[row[1]], row[0]
