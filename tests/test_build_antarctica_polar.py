"""ops/build-antarctica-polar.py: the steps it runs and what it hands the
ZIM build."""
import importlib.util
import os
import sys
import types

import pytest

pytest.importorskip("numpy")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def script(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location(
        "build_antarctica_polar", os.path.join(ROOT, "ops", "build-antarctica-polar.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(script, monkeypatch, tmp_path, dem, top_tiles=7, fetch=None, cut=None):
    """main() with every step's work replaced; (exit code, terrain builds,
    ZIM commands, the work folder). `cut` gathers the imagery builds."""
    work = tmp_path / "work"
    extract = tmp_path / "real.osm.pbf"
    extract.write_bytes(b"pbf")
    profile = tmp_path / "profile.json"
    profile.write_text("{}")
    ml = types.SimpleNamespace(
        region_row=lambda _id: ["en", "es"], languages=lambda _id: ["en", "es", "ru"],
        PROFILE=("a/b/" + os.path.relpath(profile, script.CODE), "process.lua"),
        checkout_inputs=lambda: "inputs")
    monkeypatch.setattr(script, "ml_tiles", lambda: ml)
    monkeypatch.setattr(script, "XAPIANBUILDER", str(tmp_path / "none"))

    def tile_config(_base, out, _ocean):
        with open(out, "w", encoding="utf-8") as f:
            f.write("{}")
    monkeypatch.setattr(script.polar_data, "write_tile_config", tile_config)
    monkeypatch.setattr(script.polar_data, "move", lambda _s, dst: (open(dst, "w").close(), (5, 0))[1])
    monkeypatch.setattr(script.polar_data, "ocean", lambda _s, dst: (open(dst, "w").write("{}"), (3, True))[1])
    monkeypatch.setattr(script, "tiles", lambda _ml, _m, _o, _c, out, *_a: open(out, "w").write("tiles"))
    built = []

    def build(dest, zoom, dem_dir, workers=1, say=print):
        built.append((dest, zoom, dem_dir, workers))
        os.makedirs(dest, exist_ok=True)
        return {zoom: top_tiles, 0: 1}
    monkeypatch.setattr(script.polar_terrain, "build", build)
    imagery = str(tmp_path / "imagery")

    def fetched(cache, say=print):
        assert cache == imagery
        return 0
    monkeypatch.setattr(script.polar_satellite, "fetch", fetch or fetched)

    def satellite(dest, cache, box, workers=4, say=print):
        if cut is not None:
            cut.append((dest, cache, box, workers))
        os.makedirs(dest, exist_ok=True)
        return {0: 1}
    monkeypatch.setattr(script.polar_satellite, "build", satellite)
    ran = []
    monkeypatch.setattr(script.subprocess, "run",
                        lambda cmd, **_k: (ran.append(cmd), types.SimpleNamespace(returncode=0))[1])
    with pytest.raises(SystemExit) as e:
        script.main(["--extract", str(extract), "--work", str(work), "--output", str(tmp_path / "o.zim"),
                     "--dem", str(dem), "--imagery", imagery, "--cpus", "3",
                     "--name-suffix-for-the-zim-build"])
    return e.value.code, built, ran, work


def test_the_terrain_is_made_once_and_handed_to_the_zim_build(script, monkeypatch, tmp_path):
    dem = tmp_path / "dem"
    dem.mkdir()
    (dem / "dem_S90_E000.tif").write_bytes(b"cell")
    code, built, ran, work = _run(script, monkeypatch, tmp_path, dem)
    assert code == 0
    terrain = str(work / "terrain")
    assert built == [(terrain, script.TERRAIN_ZOOM, str(dem), 3)]
    cmd = ran[0]
    i = cmd.index("--terrain")
    assert cmd[i:i + 5] == ["--terrain", "--terrain-zoom", str(script.TERRAIN_ZOOM),
                            "--terrain-dir", terrain]
    assert cmd[cmd.index("--projection") + 1] == "antarctic-polar"
    assert "--name-suffix-for-the-zim-build" in cmd
    # Unchanged cells: the tiles are reused. Other cells: made again.
    code, built, ran, _ = _run(script, monkeypatch, tmp_path, dem)
    assert code == 0 and built == [] and "--terrain-dir" in ran[0]
    (dem / "dem_S89_E000.tif").write_bytes(b"cell")
    assert len(_run(script, monkeypatch, tmp_path, dem)[1]) == 1


def test_no_zim_without_the_dem_or_without_a_tile(script, monkeypatch, tmp_path):
    code, built, ran, _ = _run(script, monkeypatch, tmp_path, tmp_path / "no-such-dem")
    assert "no DEM cells" in str(code) and not built and not ran
    dem = tmp_path / "dem"
    dem.mkdir()
    code, built, ran, work = _run(script, monkeypatch, tmp_path, dem, top_tiles=0)
    assert "no terrain tile" in str(code) and built and not ran
    assert not os.path.exists(str(work / "terrain") + ".from")


def test_the_imagery_is_cut_once_and_handed_to_the_zim_build(script, monkeypatch, tmp_path):
    dem = tmp_path / "dem"
    dem.mkdir()
    cut = []
    code, _built, ran, work = _run(script, monkeypatch, tmp_path, dem, cut=cut)
    assert code == 0
    satellite, cache = str(work / "satellite"), str(tmp_path / "imagery")
    assert cut == [(satellite, cache, (-31.0, -30.0, 31.0, 30.0), 3)]
    cmd = ran[0]
    i = cmd.index("--satellite")
    assert cmd[i:i + 5] == ["--satellite", "--satellite-zoom", str(script.polar_satellite.TOP_ZOOM),
                            "--satellite-dir", satellite]
    assert f"--bbox={script.BBOX}" in cmd
    # The same source tiles: the cut is reused. Another one: cut again.
    assert _run(script, monkeypatch, tmp_path, dem, cut=cut)[0] == 0 and len(cut) == 1
    path = script.polar_satellite.source_path(cache, 0, 0)
    os.makedirs(os.path.dirname(path))
    with open(path, "wb") as f:
        f.write(b"tile")
    assert _run(script, monkeypatch, tmp_path, dem, cut=cut)[0] == 0 and len(cut) == 2


def test_no_zim_when_the_imagery_cannot_be_fetched(script, monkeypatch, tmp_path):
    dem = tmp_path / "dem"
    dem.mkdir()
    cut = []

    def fails(_cache, say=print):
        raise RuntimeError("https://example.invalid/4/0/0.jpeg: timed out")

    code, built, ran, work = _run(script, monkeypatch, tmp_path, dem, fetch=fails, cut=cut)
    assert str(code) == ("FATAL: the imagery could not be fetched: "
                         "https://example.invalid/4/0/0.jpeg: timed out")
    assert built and not cut and not ran
    assert not os.path.exists(str(work / "satellite") + ".from")

