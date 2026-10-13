"""Multilingual region tiles (ops/region-ml-tiles.py) and how the build
wrappers use them (build-region-fast.sh's STREETZIM_ML_TILES block, the
rebuild driver's marker). docker is a stub here that records its calls and
plays tilemaker: it writes an MBTiles with name:xx for the languages it is
given, or fails in the ways the step must catch.

What must hold: the tiles come from the image CI published for the commit,
pulled (and its tag removed afterwards only if this run pulled it), run as
a guest (24g, no swap, 6 cpus, our uid, the extract read-only); the output
appears only whole and checked, with sidecars, and is reused only while its
bbox, languages, image and extract are the same; a failed pull, run or check
leaves no output and no fallback; the store and part folder never outlive a
run; and the wrapper switches to the tiles only with STREETZIM_ML_TILES=1.
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
REV = "0123456789abcdef0123456789abcdef01234567"
TAG = f"ghcr.io/jasontitus/streetzim:sha-{REV[:12]}"
BOX = "-74.5,17.5,-68.3,20.1"
LANGS = "ar,de,el,es,fa,fr,he,hi,id,it,ja,ko,nl,pl,pt,ru,th,tr,uk,vi,zh"
pytest.importorskip("mapbox_vector_tile")

FAKE_DOCKER = r'''
import json, os, sqlite3, sys, time
st = os.environ["FAKE_STATE"]
argv = sys.argv[1:]
with open(os.path.join(st, "calls"), "a") as f:
    f.write(json.dumps(argv) + "\n")
present = os.path.join(st, "present")
if argv[:2] == ["image", "inspect"]:
    if not os.path.exists(present):
        sys.exit(1)
    print(os.environ.get("FAKE_IMAGE_ID", "sha256:aaa"), os.environ.get("FAKE_REV", ""))
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
mode = os.environ.get("FAKE_TM", "ok")
if mode == "fail":
    sys.exit(3)
if mode == "sleep":
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
tile = mvt.encode([{"name": "place", "features": [
    {"geometry": "POINT(10 10)", "properties": props}]}])
c = sqlite3.connect(out)
c.execute("create table metadata (name text, value text)")
c.execute("create table tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob)")
bounds = opt("--bbox") if mode != "bounds" else "0,0,1,1"
for k, v in (("format", "pbf"), ("bounds", bounds), ("maxzoom", "14")):
    c.execute("insert into metadata values (?, ?)", (k, v))
for z in (0, 6, 10, 14):
    c.execute("insert into tiles values (?, 0, 0, ?)", (z, tile))
c.commit()
c.close()
open(os.path.join(st, "part-ino"), "w").write(str(os.stat(out).st_ino))
'''


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A data root with an extract and the shapefiles, a stub docker, and
    the environment the step reads."""
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
    env = {"STREETZIM_ROOT": str(root), "DOCKER": str(docker), "FAKE_STATE": str(state),
           "FAKE_REV": REV, "STREETZIM_ML_COMMIT": REV,
           "STREETZIM_ML_STORE_DIR": str(tmp_path / "store")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in ("STREETZIM_TILE_LANGUAGES", "STREETZIM_ML_OUT_DIR", "STREETZIM_ML_IO",
              "STREETZIM_ML_TIMEOUT", "FAKE_TM", "FAKE_PULL_FAIL", "FAKE_IMAGE_ID"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


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


def test_builds_from_the_commits_image_as_a_guest(host):
    out = _load().make("hispaniola", "hispaniola", BOX)
    assert out == str(_out(host))
    assert Path(out + ".bbox").read_text() == BOX + "\n"
    assert Path(out + ".langs").read_text() == LANGS + "\n"
    assert Path(out + ".image").read_text() == "sha256:aaa\n"
    pbf = host / "root" / "world-data" / "regions" / "hispaniola.osm.pbf"
    st = pbf.stat()
    assert Path(out + ".pbf").read_text() == f"{st.st_size} {st.st_mtime_ns}\n"
    assert json.loads(Path(out + ".run.json").read_text())["commit"] == REV
    run, = _runs(host)
    flags = " ".join(run)
    assert "--memory 24g --memory-swap 24g --cpus 6 " in flags
    assert f"--user {os.getuid()}:{os.getgid()} " in flags
    assert f"-v {pbf}:/in/region.osm.pbf:ro" in flags
    assert f"-v {host}/root/coastline:/w/coastline:ro" in flags
    assert "sha256:aaa" in run and TAG not in run      # runs the id it checked
    assert f"STREETZIM_TILE_LANGUAGES={LANGS}" in run
    assert run[run.index("--bbox") + 1] == BOX
    assert run[run.index("--config") + 1] == "/app/resources/tilemaker/config-openmaptiles.json"
    names = [c[0] for c in _calls(host)]
    assert names.count("pull") == 1 and ["rmi", TAG] in _calls(host)
    assert _leftovers(host) == []
    with sqlite3.connect(out) as c:
        assert c.execute("select count(*) from tiles").fetchone()[0] == 4
    # Renamed into place (atomic), not copied: the very file tilemaker wrote.
    assert Path(out).stat().st_ino == int((host / "docker" / "part-ino").read_text())


def test_fresh_tiles_are_reused_and_any_changed_input_remakes_them(host, monkeypatch):
    m = _load()
    m.make("hispaniola", "hispaniola", BOX)
    m.make("hispaniola", "hispaniola", BOX)
    assert len(_runs(host)) == 1
    monkeypatch.setenv("STREETZIM_TILE_LANGUAGES", "de,fr")
    m.make("hispaniola", "hispaniola", BOX)
    assert len(_runs(host)) == 2 and _out(host).with_suffix(".mbtiles.langs").read_text() == "de,fr\n"
    m.make("hispaniola", "hispaniola", BOX)
    assert len(_runs(host)) == 2
    m.make("hispaniola", "hispaniola", "-74.5,17.5,-68.3,20.2")              # bbox
    assert len(_runs(host)) == 3
    monkeypatch.setenv("FAKE_IMAGE_ID", "sha256:bbb")                        # image
    m.make("hispaniola", "hispaniola", "-74.5,17.5,-68.3,20.2")
    assert len(_runs(host)) == 4
    pbf = host / "root" / "world-data" / "regions" / "hispaniola.osm.pbf"
    os.utime(pbf, ns=(time.time_ns(), time.time_ns() + 10**9))               # a new extract
    m.make("hispaniola", "hispaniola", "-74.5,17.5,-68.3,20.2")
    assert len(_runs(host)) == 5
    Path(str(_out(host)) + ".image").unlink()                                # a sidecar gone
    m.make("hispaniola", "hispaniola", "-74.5,17.5,-68.3,20.2")
    assert len(_runs(host)) == 6


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
    assert _runs(host) == [] and ["rmi", TAG] in _calls(host)


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


@pytest.mark.parametrize("mode, why", [("fail", "tilemaker failed"),
                                       ("dir", "a directory"),
                                       ("nolang", "no name:xx"),
                                       ("stray", "name:fi"),
                                       ("bounds", "bounds")])
def test_bad_output_is_never_published(host, monkeypatch, mode, why):
    m = _load()
    m.make("hispaniola", "hispaniola", BOX)
    old = _out(host).read_bytes()
    monkeypatch.setenv("STREETZIM_TILE_LANGUAGES", "de")
    monkeypatch.setenv("FAKE_TM", mode)
    with pytest.raises(SystemExit, match=why):
        m.make("hispaniola", "hispaniola", BOX)
    # The old file is left, but no longer passes for this run's (or any) inputs.
    assert _out(host).read_bytes() == old
    assert not Path(str(_out(host)) + ".langs").exists()
    assert _leftovers(host) == [] and ["rmi", TAG] == _calls(host)[-1]
    monkeypatch.setenv("FAKE_TM", "ok")
    m.make("hispaniola", "hispaniola", BOX)
    assert Path(str(_out(host)) + ".langs").read_text() == "de\n"


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
                                        ("STREETZIM_ML_COMMIT", "HEAD")])
def test_bad_settings_stop_before_docker(host, monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    with pytest.raises(SystemExit):
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


def test_runs_left_by_dead_processes_are_reaped(host):
    regions = host / "root" / "world-data" / "regions"
    store = host / "store"
    store.mkdir()
    dead = 2 ** 22 + 12345                     # above pid_max's default: never alive
    (regions / f".ml-part-x-{dead}").mkdir()
    (store / f"ml-store-x-{dead}").mkdir()
    (store / f"ml-store-x-{os.getpid()}").mkdir()        # alive: kept
    _load().make("hispaniola", "hispaniola", BOX)
    assert _leftovers(host) == [f"ml-store-x-{os.getpid()}"]


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


def test_a_timeout_or_a_signal_removes_the_container_and_the_store(host):
    p = _cli(host, ("FAKE_TM", "sleep"), ("STREETZIM_ML_TIMEOUT", "2"))
    _, err = p.communicate(timeout=60)
    assert p.returncode != 0 and "passed 2 s" in err and "tilemaker failed" in err
    assert ["rm", "-f", "streetzim-mltiles-hispaniola"] == _calls(host)[-2]
    assert _leftovers(host) == [] and not _out(host).exists()
    done = host / "docker" / "rm-done"
    done.unlink()
    p = _cli(host, ("FAKE_TM", "sleep"), ("FAKE_RM_SECS", "1"))
    for _ in range(100):
        if _runs(host)[1:]:
            break
        time.sleep(0.1)
    time.sleep(0.5)
    assert done.read_text() == "x"      # the rm -f before the run
    p.send_signal(signal.SIGTERM)
    time.sleep(0.5)
    # timeout(1) forwards one too; it lands while the container is being
    # removed, and must not cut that short.
    p.send_signal(signal.SIGTERM)
    p.communicate(timeout=60)
    assert done.read_text() == "xx"
    assert p.returncode == 128 + signal.SIGTERM
    assert ["rm", "-f", "streetzim-mltiles-hispaniola"] in _calls(host)[-3:]
    assert _calls(host)[-1] == ["rmi", TAG]
    assert _leftovers(host) == [] and not _out(host).exists()


# ---- build-region-fast.sh ---------------------------------------------------

WRAPPER = ROOT / "ops" / "build-region-fast.sh"


def _wrapper_block() -> str:
    text = WRAPPER.read_text()
    i = text.index("ML_ARGS=()")
    return text[i:text.index("\nesac\n", i) + 6]


def _wrapper(tmp_path, ml, *, py_ok=True, langs=True, rid="hispaniola"):
    table = tmp_path / "region-languages.tsv"
    table.write_text("# id\tlanguages\nhispaniola\tes,fr,ht\nafrica-light\tar,fr\n")
    tiles = tmp_path / "x.ml.mbtiles"
    py = tmp_path / "py"
    py.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        echo "$*" >> {tmp_path}/py.log
        {'exit 4' if not py_ok else ''}
        echo x > {tiles}
        {f'echo ar,de > {tiles}.langs' if langs else ''}
        echo {tiles}
        """))
    py.chmod(0o755)
    block = _wrapper_block().replace("/storage/streetzim/cloud/region-languages.tsv", str(table))
    body = (f"set -euo pipefail\nPY={py}\nID={rid}\nSRC_ID=src\nBBOX={BOX}\n"
            f"MBTILES=world.mbtiles\n{block}\n"
            'echo "MBTILES=$MBTILES"\necho "ML_ARGS=${ML_ARGS[*]-}"\n')
    env = {k: v for k, v in os.environ.items() if k != "STREETZIM_ML_TILES"}
    if ml is not None:
        env["STREETZIM_ML_TILES"] = ml
    r = subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True, timeout=60)
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


def test_wrapper_passes_the_arguments_to_the_builder():
    text = WRAPPER.read_text()
    assert text.index('ARGS+=( "${ML_ARGS[@]}" )') > text.index("ARGS=(")
    # Decided after the ALREADY EXISTS skip and before the NVMe staging
    # copies $MBTILES.
    assert text.index("ALREADY EXISTS") < text.index("ML_ARGS=()") < text.index("STAGED_MBTILES=\"\"")


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


# ---- which commit's image ----------------------------------------------------

@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A git checkout whose origin/main is its first commit."""
    repo = tmp_path / "co"
    (repo / "resources" / "tilemaker").mkdir(parents=True)
    (repo / "resources" / "tilemaker" / "process.lua").write_text("-- profile\n")
    (repo / "Dockerfile").write_text("FROM x\n")
    (repo / "rebuild-old.list").write_text("a\n")

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                              text=True, env={**os.environ, "GIT_AUTHOR_NAME": "t",
                                              "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                                              "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()
    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "main")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    m = _load()
    monkeypatch.setattr(m, "CODE", str(repo))
    monkeypatch.delenv("STREETZIM_ML_COMMIT", raising=False)
    return m, repo, git


def test_the_commit_is_head_on_main(checkout):
    m, _, git = checkout
    assert m.commit() == git("rev-parse", "HEAD")


def test_a_host_commit_on_top_uses_the_main_commit_below_it(checkout, capsys):
    m, repo, git = checkout
    main = git("rev-parse", "HEAD")
    (repo / "rebuild-old.list").write_text("b\n")
    git("commit", "-qam", "host: lists")
    assert m.commit() == main
    assert f"using main's {main[:12]}" in capsys.readouterr().err


@pytest.mark.parametrize("path", ["resources/tilemaker/process.lua", "Dockerfile"])
@pytest.mark.parametrize("committed", [True, False])
def test_a_changed_profile_or_dockerfile_is_refused(checkout, path, committed):
    m, repo, git = checkout
    (repo / path).write_text("changed\n")
    if committed:
        git("commit", "-qam", "host: profile")
    with pytest.raises(SystemExit, match=f"differ from main's.*{path}"):
        m.commit()


def test_no_main_is_refused(checkout):
    m, _, git = checkout
    git("update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(SystemExit, match="cannot find the main commit"):
        m.commit()


def test_the_tag_is_removed_once_the_container_lets_go(host, monkeypatch):
    m = _load()
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    (host / "docker" / "rmi-conflicts").write_text("2")
    m.make("hispaniola", "hispaniola", BOX)
    assert [c for c in _calls(host) if c[0] == "rmi"] == [["rmi", TAG]] * 3
    assert not (host / "docker" / "present").exists()
