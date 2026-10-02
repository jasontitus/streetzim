"""`streetzim` (streetzim/cli.py): flag translation, .poly parsing, file
naming and the checks that must fail before any download."""
from __future__ import annotations

import datetime
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

cli = pytest.importorskip("streetzim.cli")
pytest.importorskip("regex")

POLY = """monaco
1
   7.40E+00   43.72
   7.44   43.72
   7.44   43.76
   7.40   43.76
END
!2
   7.41   43.73
   7.42   43.73
   7.42   43.74
END
3
   7.50   43.70
   7.51   43.71
END
END
"""
REQ = ["--name", "osm_en_monaco", "--title", "Monaco", "--description", "Offline Monaco"]


def test_parse_poly_ignores_holes_and_unions_rings():
    assert cli.parse_poly(POLY) == (7.40, 43.70, 7.51, 43.76)
    with pytest.raises(ValueError):
        cli.parse_poly("empty\nEND\n")


def test_zim_filename():
    d = datetime.date(2026, 9, 28)
    assert cli.zim_filename("{name}_{period}", "osm_en_monaco", d) == "osm_en_monaco_2026-09.zim"
    assert cli.zim_filename("fixed.zim", "x", d) == "fixed.zim"
    for bad in ("../{name}", "a/b", ".."):
        with pytest.raises(ValueError):
            cli.zim_filename(bad, "x", d)


def test_default_view_is_lat_lon_and_becomes_lon_lat():
    assert cli.parse_default_view("43.73,7.42,13.6") == (43.73, 7.42, 13.6)
    with pytest.raises(ValueError):
        cli.parse_default_view("7.42")
    with pytest.raises(ValueError):
        cli.parse_default_view("143.7,7.4")


@pytest.fixture
def no_network(monkeypatch, tmp_path):
    """fetch() returns fixtures; records every URL it was asked for."""
    asked = []

    def fake_fetch(url, dest):
        asked.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(POLY if url.endswith(".poly") else "pbf")
        return dest
    monkeypatch.setattr(cli, "fetch", fake_fetch)
    return asked


def plan(argv, tmp_path):
    """plan() as main() reaches it (through --profile), with basic, which
    fetches nothing but the extract (tests/test_streetzim_profiles.py
    covers full)."""
    return cli.plan(cli.parse_args(REQ + ["--profile", "basic"] + argv), tmp_path)


def builder_args(argv):
    """What create_osm_zim itself parses from the arguments plan() built."""
    import create_osm_zim
    return create_osm_zim.build_parser().parse_args(argv)


def test_area_uses_geofabrik_extract_and_forwards_metadata(tmp_path, no_network):
    argv, info = plan(["--area", "monaco", "--tags", "a;b", "--default-view", "43.73,7.42,13"],
                      tmp_path)
    assert info["pbf_url"] == "https://download.geofabrik.de/europe/monaco-latest.osm.pbf"
    assert no_network == [info["pbf_url"]]
    ns = builder_args(argv)
    assert ns.bbox == "7.39,43.715,7.46,43.765"
    assert ns.zim_name == "osm_en_monaco" and ns.title == "Monaco"
    assert ns.publisher == "openZIM"
    assert ns.tags == "a;b"
    assert ns.map_center == "7.42,43.73" and ns.map_zoom == 13
    assert ns.routing and not ns.satellite
    assert not ns.kiwix_poi_pages                      # off unless asked for


def test_kiwix_poi_pages_is_forwarded(tmp_path, no_network):
    argv, _ = plan(["--area", "monaco", "--kiwix-poi-pages"], tmp_path)
    assert builder_args(argv).kiwix_poi_pages


def test_cpus_is_forwarded_only_when_given(tmp_path, no_network):
    argv, _ = plan(["--area", "monaco", "--cpus", "3"], tmp_path)
    assert builder_args(argv).cpus == 3
    argv, _ = plan(["--area", "monaco"], tmp_path / "default")
    assert builder_args(argv).cpus is None             # the builder detects it


@pytest.mark.parametrize("flag", ["--cpus", "--zim-workers"])
@pytest.mark.parametrize("value", ["0", "-3", "two"])
def test_counts_below_one_fail_before_any_download(tmp_path, no_network, capsys, flag, value):
    with pytest.raises(SystemExit):
        plan(["--area", "monaco", flag, value], tmp_path)
    assert no_network == []
    assert flag in capsys.readouterr().err


def test_zim_workers_is_forwarded(tmp_path, no_network):
    argv, _ = plan(["--area", "monaco", "--zim-workers", "1"], tmp_path)
    assert builder_args(argv).workers == 1


def test_monaco_preset_frames_all_of_monaco_with_sea_around_it():
    """--area monaco is what CI and the Zimfarm recipe build. Its box used to
    stop on Monaco's eastern border (7.44) and 500 m out to sea, so the sea
    ended in a straight line and a desktop window could not fit the whole
    country. Monaco: 7.409-7.440 E, 43.725-43.752 N."""
    from create_osm_zim import KNOWN_AREAS
    w, s, e, n = (float(v) for v in KNOWN_AREAS["monaco"]["bbox"].split(","))
    assert w <= 7.409 - 0.01 and e >= 7.440 + 0.015      # ~1 km of margin east
    assert s <= 43.725 - 0.009 and n >= 43.752 + 0.009   # ~1 km of sea south
    # Wide enough for a 1280x800 window at the zoom that shows it all
    # (MapLibre keeps the viewport inside the box): 3 km tall needs >= 0.06 deg.
    assert e - w >= 0.06
    assert e - w <= 0.1 and n - s <= 0.1                 # still a small CI build


def test_terrain_follows_the_profile_and_its_flags(tmp_path, no_network, monkeypatch):
    # This checks terrain flag translation; Overture downloads have their
    # own profile tests and must not require live network access here.
    monkeypatch.setattr(cli, "fetch_overture", lambda *args: {})
    def terrain(extra):
        args = cli.parse_args(REQ + ["--area", "monaco"] + extra)
        return builder_args(cli.plan(args, tmp_path)[0]).terrain
    assert terrain([]) and terrain(["--profile", "full"])      # full is the default
    assert not terrain(["--profile", "basic"])
    assert not terrain(["--no-terrain"]) and not terrain(["--terrain=off"])
    assert terrain(["--profile", "basic", "--terrain"])


def test_geofabrik_poly_selects_its_extract(tmp_path, no_network):
    argv, info = plan(["--include-poly", "https://download.geofabrik.de/europe/monaco.poly"],
                      tmp_path)
    assert info["pbf_url"].endswith("/europe/monaco-latest.osm.pbf")
    assert info["bbox"] == "7.400000,43.700000,7.510000,43.760000"


def _ring(minlon, minlat, maxlon, maxlat):
    return (f"{minlon} {minlat}\n{maxlon} {minlat}\n{maxlon} {maxlat}\n"
            f"{minlon} {maxlat}\n")


def _poly(*boxes):
    return "p\n" + "".join(f"{i}\n{_ring(*b)}END\n" for i, b in enumerate(boxes, 1)) + "END\n"


def test_poly_parts_far_apart_build_the_largest(tmp_path, monkeypatch, capsys):
    # openstreetmap.fr's netherlands.poly: the mainland and, 70 degrees west,
    # the Caribbean islands. One box around both is mostly Atlantic.
    nl = _poly((3.2, 50.7, 7.3, 53.8), (-68.7, 11.8, -68.1, 12.4),
               (-70.1, 12.4, -69.8, 12.7), (-63.3, 17.4, -62.9, 17.7))
    monkeypatch.setattr(cli, "fetch", lambda url, dest: (
        dest.parent.mkdir(parents=True, exist_ok=True), dest.write_text(nl), dest)[2])
    argv, info = plan(["--include-poly", "https://example.org/nl.poly", "--pbf-url",
                       "https://example.org/nl.pbf"], tmp_path)
    assert info["bbox"] == "3.200000,50.700000,7.300000,53.800000"
    out = capsys.readouterr().out
    assert "leaving out 3 part(s)" in out and "-68.70,11.80,-68.10,12.40" in out
    # Spain and the Balearics share a box; the Canaries do not.
    # Spain keeps the Balearics; Portugal is built from the mainland, not the
    # Azores, whose scattered islands have the bigger box but less land.
    spain = [((-9.3, 36.0, 3.3, 43.8), 50.0), ((1.2, 38.6, 4.3, 40.1), 0.5)]
    assert cli.area_bbox(spain) == ((-9.3, 36.0, 4.3, 43.8), [])
    portugal = [((-9.5, 36.9, -6.2, 42.2), 9.0), ((-31.3, 36.9, -25.0, 39.8), 0.2),
                ((-17.3, 32.6, -16.3, 33.1), 0.08)]
    box, left = cli.area_bbox(portugal)
    assert box == (-9.5, 36.9, -6.2, 42.2) and len(left) == 2


def test_values_starting_with_a_dash_reach_the_builder(tmp_path, no_network):
    # A western bbox (every --area preset in the Americas) and a title
    # starting with "-" used to be read by argparse as flags.
    # (Zimfarm passes --key=value, as here; so must a person.)
    argv, _ = plan(["--bbox=-122.52,37.70,-122.35,37.83", "--pbf-url",
                    "https://example.org/sf.pbf", "--default-view", "37.77,-122.42,12",
                    "--long-description=-- offline --", "--tags=-x"], tmp_path)
    ns = builder_args(argv)
    assert ns.bbox == "-122.52,37.70,-122.35,37.83"
    assert ns.map_center == "-122.42,37.77"
    assert ns.long_description == "-- offline --" and ns.tags == "-x"


def test_errors_are_raised_before_downloading_the_extract(tmp_path, no_network):
    with pytest.raises(ValueError, match="exactly one"):
        plan(["--area", "monaco", "--bbox", "1,2,3,4"], tmp_path)
    with pytest.raises(ValueError, match="unknown --area"):
        plan(["--area", "atlantis"], tmp_path)
    with pytest.raises(ValueError, match="--pbf-url"):
        plan(["--include-poly", "https://example.org/x.poly"], tmp_path)
    with pytest.raises(ValueError, match="needs an OSM extract"):
        plan(["--bbox", "7.4,43.72,7.44,43.76", "--mbtiles", "t.mbtiles"], tmp_path)
    assert all(u.endswith(".poly") for u in no_network)
    from tests.mbtiles_fixture import make_mbtiles
    t = make_mbtiles(tmp_path / "t.mbtiles", [(0, 0, 0)])
    argv, _ = plan(["--bbox", "7.4,43.72,7.44,43.76", "--mbtiles", str(t),
                    "--no-routing"], tmp_path)
    assert "--routing" not in argv and "--pbf" not in argv


def test_main_rejects_bad_metadata_and_existing_output_first(tmp_path, no_network, capsys):
    out = tmp_path / "out"
    rc = cli.main(["--name", "n", "--title", "x" * 31, "--description", "d",
                   "--area", "monaco", "--output", str(out), "--tmp", str(tmp_path / "t")])
    assert rc == 2 and "Title is too long" in capsys.readouterr().err
    out.mkdir()
    (out / "fixed.zim").write_bytes(b"old")
    rc = cli.main(REQ + ["--area", "monaco", "--output", str(out), "--file-name", "fixed",
                         "--tmp", str(tmp_path / "t")])
    assert rc == 2 and "exists" in capsys.readouterr().err
    assert (out / "fixed.zim").read_bytes() == b"old"
    assert no_network == []


def test_shapefiles_fetched_only_when_missing(tmp_path, monkeypatch):
    from streetzim.tiles import required_shapefiles
    runs = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, check: runs.append(cmd))
    cli.ensure_shapefiles(tmp_path)
    assert len(runs) == 1 and runs[0][-1] == str(tmp_path)
    for rel in required_shapefiles():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"")
    cli.ensure_shapefiles(tmp_path)
    assert len(runs) == 1


RUSSIA_LIKE = """russia
1
   19.6   54.3
   180.0  64.0
   -180.0 66.0
   -169.0 65.9
END
END
"""


# Fiji as Geofabrik-style rings split at the antimeridian.
FIJI_LIKE = """fiji
1
   172.84 -17.76
   176.01 -23.12
   180.0  -20.48
   180.0  -12.65
   176.51 -11.24
   172.84 -17.76
END
2
   -180.0   -14.72
   -176.53  -19.27
   -180.0   -20.94
   -180.0   -14.72
END
END
"""


def _fake_poly(monkeypatch, text):
    def fake_fetch(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        return dest
    monkeypatch.setattr(cli, "fetch", fake_fetch)


def test_polys_and_bboxes_across_the_antimeridian_are_one_unwrapped_box(tmp_path, monkeypatch):
    # A ring drawn across ±180 (Russia's) is followed the short way.
    _fake_poly(monkeypatch, RUSSIA_LIKE)
    _, info = plan(["--include-poly", "https://example.org/russia.poly", "--pbf-url", "x"],
                   tmp_path)
    assert info["bbox"] == "19.600000,54.300000,191.000000,66.000000"
    # Rings split at ±180 (Fiji's) join into one box, not a world band.
    _fake_poly(monkeypatch, FIJI_LIKE)
    _, info = plan(["--include-poly", "https://example.org/fiji.poly", "--pbf-url", "x"],
                   tmp_path)
    assert info["bbox"] == "172.840000,-23.120000,183.470000,-11.240000"
    # Either spelling of a box across the antimeridian.
    assert cli.parse_bbox_arg("172.8,-23,-176.5,-11") == (172.8, -23.0, 183.5, -11.0)
    assert cli.parse_bbox_arg("172.8,-23,183.5,-11") == (172.8, -23.0, 183.5, -11.0)
    assert cli.parse_bbox_arg("170,10,180,20") == (170.0, 10.0, 180.0, 20.0)
    for bad in ("10,20,5,30", "1,2,3", "0,-91,1,1", "170,10,550,20", "5,1,5,2"):
        with pytest.raises(ValueError):
            cli.parse_bbox_arg(bad)
    assert cli.parse_bbox_arg("7.40,43.72,7.44,43.76") == (7.40, 43.72, 7.44, 43.76)


def test_far_apart_parts_still_split_the_short_way_round():
    # New Caledonia (165E) and French Polynesia (150W) are 45° apart
    # across the antimeridian, not 315° the other way; France is still
    # the part built.
    france = ((-5.0, 42.0, 8.0, 51.0), 60.0)
    nc = ((163.5, -22.7, 168.2, -19.5), 2.0)
    pf = ((-154.0, -28.0, -134.0, -7.0), 1.0)
    box, left_out = cli.area_bbox([france, nc, pf])
    assert box == france[0] and len(left_out) == 2
    assert cli._gap(nc[0], (-179.5, -21.0, -178.0, -20.0)) == pytest.approx(12.3)
    assert cli._union([(177.0, -20.0, 180.0, -16.0), (-180.0, -18.0, -179.0, -15.0)]) \
        == (177.0, -20.0, 181.0, -15.0)


def test_placeholders_are_filled_like_maps2zim():
    d = datetime.date(2026, 9, 28)
    assert cli.fill("Monaco {period} ({name})", "osm_en_monaco", d) == \
        "Monaco 2026-09 (osm_en_monaco)"


def test_fetch_refreshes_when_the_source_changes(tmp_path):
    import os
    import time
    src = tmp_path / "src.pbf"
    src.write_bytes(b"v1")
    dest = tmp_path / "dl" / "x.pbf"
    url = f"file://{src}"
    assert cli.fetch(url, dest).read_bytes() == b"v1"
    assert cli.fetch(url, dest).read_bytes() == b"v1"          # reused
    src.write_bytes(b"version 2")
    os.utime(src, (time.time() + 5, time.time() + 5))
    assert cli.fetch(url, dest).read_bytes() == b"version 2"   # refreshed


def test_illustration_checked_before_downloads_and_resolved(tmp_path, no_network, capsys):
    out = tmp_path / "out"
    rc = cli.main(REQ + ["--area", "monaco", "--output", str(out), "--tmp", str(tmp_path / "t"),
                         "--illustration-url", str(tmp_path / "missing.png")])
    assert rc == 2 and "missing.png" in capsys.readouterr().err
    assert no_network == []
    from PIL import Image
    Image.new("RGB", (64, 64)).save(tmp_path / "icon.png")
    argv, _ = cli.plan(cli.parse_args(REQ + ["--area", "monaco", "--profile=basic"]), tmp_path,
                       illustration=(tmp_path / "icon.png"))
    ill = argv[argv.index("--illustration") + 1]
    assert Path(ill).is_absolute()


@pytest.mark.parametrize("module", ["create_osm_zim", "streetzim.cli"])
def test_help_renders(module):
    # argparse %-formats help strings: a bare "%" once made --help print an
    # internal dict (create_osm_zim's --bundle-wiki-articles text).
    import importlib
    text = importlib.import_module(module).build_parser().format_help()
    assert "option_strings" not in text and "_ArgumentGroup" not in text


def test_bands_round_the_world_are_still_refused(tmp_path, monkeypatch):
    # The old guard: an area is a box, and one wider than 180° is a band
    # round the world, crossing or not.
    for band in ("-170,0,170,10", "-180,-85,180,85", "-180,-90,180,90", "10,0,-10,10"):
        with pytest.raises(ValueError, match="at most 180"):
            cli.parse_bbox_arg(band)
    assert cli.parse_bbox_arg("-90,0,90,10") == (-90.0, 0.0, 90.0, 10.0)   # exactly 180
    # A whole-world ring has no box: a clear error, not "min < max".
    _fake_poly(monkeypatch, "world\n1\n  -180 -90\n  180 -90\n  180 90\n"
                            "  -180 90\n  -180 -90\nEND\nEND\n")
    with pytest.raises(ValueError, match="all the way round"):
        plan(["--include-poly", "https://example.org/world.poly", "--pbf-url", "x"], tmp_path)
    ring = "ring\n1\n" + "".join(f"  {x} -70\n" for x in (-180, -90, 0, 90, 180)) + "END\nEND\n"
    _fake_poly(monkeypatch, ring)
    with pytest.raises(ValueError, match="all the way round"):
        plan(["--include-poly", "https://example.org/ring.poly", "--pbf-url", "x"], tmp_path)


def test_concurrent_builds_have_independent_cut_workspaces(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    scratch = tmp_path / 'scratch'
    legacy = scratch / 'mbtiles-cut'
    legacy.mkdir(parents=True)
    (legacy / 'area.mbtiles').write_bytes(b'another build')
    ready = threading.Barrier(2, timeout=10)
    workspaces = []

    def build(args, dl, illustration, work, building, final):
        workspaces.append(work)
        cut = work / 'area.mbtiles'
        contents = str(work).encode()
        cut.write_bytes(contents)
        ready.wait()
        # Both invocations are active. Neither startup may clear the other.
        assert cut.read_bytes() == contents
        return 0

    monkeypatch.setattr(cli, '_build', build)
    argv = REQ + ['--area', 'monaco', '--profile', 'basic',
                  '--output', str(tmp_path / 'out'), '--tmp', str(scratch)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(cli.main, argv) for _ in range(2)]
        assert [f.result() for f in futures] == [0, 0]
    assert len(set(workspaces)) == 2
    assert all(p.parent == scratch for p in workspaces)
    assert all(not p.exists() for p in workspaces)
    assert (legacy / 'area.mbtiles').read_bytes() == b'another build'


@pytest.mark.parametrize('failure', [RuntimeError('build failed'), SystemExit(143)])
@pytest.mark.parametrize('keep_flag', [None, '--debug', '--keep-temp'])
def test_cut_workspace_cleanup_on_failure(tmp_path, monkeypatch, failure, keep_flag):
    workspaces = []

    def build(args, dl, illustration, work, building, final):
        workspaces.append(work)
        (work / 'area.mbtiles').write_bytes(b'partial')
        raise failure

    monkeypatch.setattr(cli, '_build', build)
    argv = REQ + ['--area', 'monaco', '--profile', 'basic',
                  '--output', str(tmp_path / 'out'), '--tmp', str(tmp_path / 'scratch')]
    if keep_flag:
        argv.append(keep_flag)
    with pytest.raises(type(failure)):
        cli.main(argv)
    assert len(workspaces) == 1
    if keep_flag:
        assert (workspaces[0] / 'area.mbtiles').read_bytes() == b'partial'
    else:
        assert not workspaces[0].exists()


def test_local_fetch_handles_escaped_urls_and_same_second_updates(tmp_path):
    import os
    src = tmp_path / 'source with spaces.pbf'
    src.write_bytes(b'old')
    dest = tmp_path / 'download.pbf'
    assert cli.fetch(src.as_uri(), dest).read_bytes() == b'old'
    before = src.stat()
    src.write_bytes(b'new')
    os.utime(src, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    assert cli.fetch(src.as_uri(), dest).read_bytes() == b'new'
    # Interrupted metadata writes from an old run are recovered.
    dest.with_name(dest.name + '.source.json').write_text('{broken')
    assert cli.fetch(src.as_uri(), dest).read_bytes() == b'new'


@pytest.mark.parametrize('overwrite', [False, True])
def test_concurrent_same_output_uses_private_staging_and_respects_overwrite(
        tmp_path, monkeypatch, overwrite):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import create_osm_zim

    ready = threading.Barrier(2, timeout=10)
    stages = []
    illustrations = []
    monkeypatch.setattr(cli, 'plan', lambda *args, **kwargs: ([], {}))
    from streetzim import zim_metadata
    monkeypatch.setattr(zim_metadata, 'load_illustration', lambda url: url.encode())

    def builder(argv):
        path = Path(argv[argv.index('-o') + 1])
        stages.append(path)
        path.write_bytes(str(path).encode())
        ready.wait()
        # Each invocation's archive remains its own while both are active.
        assert path.read_bytes() == str(path).encode()

    original_build = cli._build

    def build(args, dl, illustration, work, building, final):
        illustrations.append(illustration)
        assert illustration.parent == work
        assert illustration.read_bytes() == args.illustration_url.encode()
        return original_build(args, dl, illustration, work, building, final)

    monkeypatch.setattr(cli, '_build', build)
    monkeypatch.setattr(create_osm_zim, 'main', builder)
    argv = REQ + ['--bbox=7.4,43.72,7.44,43.76', '--mbtiles', 'unused', '--no-routing',
                  '--profile', 'basic', '--file-name', 'same',
                  '--output', str(tmp_path / 'out'), '--tmp', str(tmp_path / 'scratch')]
    if overwrite:
        argv.append('--overwrite')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(cli.main, argv + ['--illustration-url', str(i)]) for i in range(2)]
        results = [f.result() for f in futures]
    assert sorted(results) == ([0, 0] if overwrite else [0, 2])
    assert len(set(stages)) == 2
    assert len(set(illustrations)) == 2
    final = tmp_path / 'out' / 'same.zim'
    assert final.read_bytes() in {str(stage).encode() for stage in stages}
    assert list(final.parent.iterdir()) == [final]


def test_failed_overwrite_preserves_published_output_and_cleans_staging(tmp_path, monkeypatch):
    import create_osm_zim
    out = tmp_path / 'out'
    out.mkdir()
    final = out / 'same.zim'
    final.write_bytes(b'published')
    monkeypatch.setattr(cli, 'plan', lambda *args, **kwargs: ([], {}))

    def fail(argv):
        Path(argv[argv.index('-o') + 1]).write_bytes(b'partial')
        raise RuntimeError('writer failed')

    monkeypatch.setattr(create_osm_zim, 'main', fail)
    with pytest.raises(RuntimeError, match='writer failed'):
        cli.main(REQ + ['--bbox=7.4,43.72,7.44,43.76', '--mbtiles', 'unused', '--no-routing',
                        '--profile', 'basic', '--file-name', 'same', '--overwrite',
                        '--output', str(out), '--tmp', str(tmp_path / 'scratch')])
    assert final.read_bytes() == b'published'
    assert list(out.iterdir()) == [final]


@pytest.mark.parametrize('workers', ['0', '-1'])
def test_invalid_compression_workers_fail_before_download(tmp_path, no_network, capsys, workers):
    with pytest.raises(SystemExit) as error:
        cli.main(REQ + ['--area', 'monaco', '--zim-workers', workers,
                        '--output', str(tmp_path / 'out'), '--tmp', str(tmp_path / 'scratch')])
    assert error.value.code == 2
    error_text = capsys.readouterr().err
    assert '--zim-workers' in error_text and 'must be at least 1' in error_text
    assert no_network == []


def test_http_protocol_failure_is_reported_without_publishing_output(tmp_path, monkeypatch, capsys):
    import http.client

    def interrupted(url, dest):
        raise http.client.IncompleteRead(b'partial', 10)

    monkeypatch.setattr(cli, 'fetch', interrupted)
    assert cli.main(REQ + ['--area', 'monaco', '--profile', 'basic',
                          '--output', str(tmp_path / 'out'), '--tmp', str(tmp_path / 'scratch')]) == 2
    assert 'IncompleteRead' in capsys.readouterr().err
    assert list((tmp_path / 'out').iterdir()) == []


# ------------------------------------------------- --tilemaker-store
GIB = 1 << 30
tms = pytest.importorskip("streetzim.tilemaker_store")


@pytest.mark.parametrize("mode", ["auto", "disk", "memory"])
def test_tilemaker_store_reaches_the_builder_in_the_workspace(tmp_path, no_network, mode):
    work = tmp_path / "work"
    argv, _ = cli.plan(cli.parse_args(REQ + ["--profile", "basic", "--area", "monaco",
                                             "--tilemaker-store", mode]),
                       tmp_path / "dl", work=work)
    ns = builder_args(argv)
    assert ns.tilemaker_store == mode                  # the builder decides, on the cut
    assert ns.store == str(work / cli.TILEMAKER_STORE_DIR)
    assert not (work / cli.TILEMAKER_STORE_DIR).exists()     # tilemaker's step makes it
    argv, _ = cli.plan(cli.parse_args(REQ + ["--profile", "basic", "--area", "monaco"]),
                       tmp_path / "dl")
    ns = builder_args(argv)                            # no workspace: the builder's tmpdir
    assert ns.tilemaker_store == "auto" and ns.store is None


def test_tilemaker_store_is_not_passed_without_tilemaker(tmp_path, no_network, monkeypatch):
    monkeypatch.setattr(cli, "mbtiles_source", lambda args, dl: (tmp_path / "t.mbtiles", None))
    monkeypatch.setattr(cli, "prepare_mbtiles", lambda path, box, work: (path, None))
    argv, _ = cli.plan(cli.parse_args(REQ + ["--profile", "basic", "--area", "monaco",
                                             "--mbtiles", "x", "--tilemaker-store", "disk"]),
                       tmp_path / "dl", work=tmp_path / "work")
    ns = builder_args(argv)
    assert ns.store is None and ns.tilemaker_store is None


def test_tilemaker_store_auto_rules():
    choose = tms.choose
    # Explicit modes win whatever the size and limit.
    assert choose("memory", 10 * GIB, 8, 4 * GIB)[0] is False
    assert choose("disk", 1, 1, None)[0] is True
    # auto: over 1 GiB to read goes to disk even without a memory limit...
    assert choose("auto", GIB + 1, 4, None)[0] is True
    assert choose("auto", GIB, 4, None)[0] is False
    # ...less only when the estimate is over half the limit.
    est = tms.memory_estimate(500_000_000, 4)
    assert 2.5e9 < est < 2.7e9                        # 0.5 + 4 x 0.25 + 2.2 x 0.5 GB
    assert choose("auto", 500_000_000, 4, int(2 * est) + 1)[0] is False
    assert choose("auto", 500_000_000, 4, int(2 * est) - 1)[0] is True
    assert choose("auto", 500_000_000, 4, 16 * GIB)[0] is False
    assert choose("auto", 500_000_000, 4, 4 * GIB)[0] is True
    # The measured regions at 4 threads: within 15% of the estimate.
    for size, measured in ((47_583_019, 1.48e9), (547_273_092, 2.74e9),
                           (1_403_823_266, 4.02e9)):
        assert abs(tms.memory_estimate(size, 4) / measured - 1) < 0.15


def _sparse(path, size):
    with open(path, "wb") as f:
        f.truncate(size)                               # no real bytes written
    return str(path)


def test_tilemaker_store_decide_checks_free_disk(tmp_path, monkeypatch, capsys):
    pbf = _sparse(tmp_path / "area.osm.pbf", GIB + 1)
    store = str(tmp_path / "work" / "tilemaker-store")     # parent missing too
    monkeypatch.setattr(tms, "free_bytes", lambda p: 4 * GIB)
    assert tms.decide("auto", pbf, store, 4, None) == store
    assert "tilemaker store: disk" in capsys.readouterr().out
    # Not 3 times the cut free: auto falls back to memory, disk fails.
    monkeypatch.setattr(tms, "free_bytes", lambda p: 3 * GIB)
    assert tms.decide("auto", pbf, store, 4, None) is None
    assert "WARNING: tilemaker store: memory, not disk" in capsys.readouterr().out
    with pytest.raises(tms.StoreError, match=r"--tilemaker-store disk: .* GB free"):
        tms.decide("disk", pbf, store, 4, None)
    # Memory needs no disk.
    assert tms.decide("memory", pbf, store, 4, None) is None
    # free_bytes reads the nearest folder that exists.
    monkeypatch.undo()
    assert tms.free_bytes(store) == shutil.disk_usage(tmp_path).free


@pytest.fixture
def builder_until_tiles(tmp_path, monkeypatch):
    """create_osm_zim's tile step with a cut of a chosen size, recording the
    store generate_tiles gets."""
    import create_osm_zim as c
    got = {}

    def cut(src, bbox, out):
        _sparse(out, got["cut_size"])
    monkeypatch.setattr(c, "extract_bbox_from_pbf", cut)

    def tiles(pbf, mbtiles, bbox=None, fast=False, store=None):
        got["store"] = store
        if store:
            os.makedirs(store)
    monkeypatch.setattr(c, "generate_tiles", tiles)
    monkeypatch.setattr(c._cpus, "memory_limit", lambda: None)
    monkeypatch.setattr(c, "build_cpus", lambda: 4)

    def run(cut_size, *flags):
        got["cut_size"] = cut_size
        args = c.build_parser().parse_args(["--bbox", "7.4,43.72,7.44,43.76", *flags])
        tmpdir = tmp_path / "osm_zim"
        tmpdir.mkdir(exist_ok=True)
        c._acquire_tiles(args=args, bbox_str="7.4,43.72,7.44,43.76", geofabrik_path=None,
                         pbf_path=_sparse(tmp_path / "extract.osm.pbf", 5 * GIB),
                         tmpdir=str(tmpdir), total_steps=9)
        return got["store"], tmpdir
    return run



def test_create_osm_zim_decides_on_the_cut(builder_until_tiles, tmp_path, capsys):
    # A 5 GiB extract cut to 10 MB: memory (the full size would say disk).
    store, _ = builder_until_tiles(10_000_000, "--tilemaker-store", "auto")
    assert store is None
    assert "tilemaker reads 0.01 GB" in capsys.readouterr().out
    # A cut over 1 GiB: disk, in the builder's tmpdir, removed after the tiles.
    store, tmpdir = builder_until_tiles(GIB + 1, "--tilemaker-store", "auto")
    assert store == str(tmpdir / "tilemaker-store")
    assert not os.path.exists(store)
    # A --store of the caller's is used and left to the caller.
    mine = tmp_path / "mine"
    store, _ = builder_until_tiles(GIB + 1, "--tilemaker-store", "disk", "--store", str(mine))
    assert store == str(mine) and mine.is_dir()
    # --store alone: always, as before --tilemaker-store.
    alone = tmp_path / "alone"
    store, _ = builder_until_tiles(10, "--store", str(alone))
    assert store == str(alone)


def test_create_osm_zim_forced_disk_without_space_fails(builder_until_tiles, monkeypatch):
    monkeypatch.setattr(tms, "free_bytes", lambda p: GIB)
    with pytest.raises(SystemExit, match=r"error: --tilemaker-store disk: 1\.1 GB free"):
        builder_until_tiles(GIB + 1, "--tilemaker-store", "disk")


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("keep_flag", [None, "--keep-temp"])
def test_tilemaker_store_is_under_tmp_and_removed(tmp_path, no_network, monkeypatch,
                                                  fails, keep_flag):
    """The store sits in this build's workspace under --tmp, and is removed
    after the build even when tilemaker left its files (killed) and even
    with --keep-temp."""
    import create_osm_zim
    from streetzim.tiles import required_shapefiles
    shp = tmp_path / "shp"
    for rel in required_shapefiles():
        (shp / rel).parent.mkdir(parents=True, exist_ok=True)
        (shp / rel).write_bytes(b"")
    seen = []

    def builder(argv):
        store = Path(argv[argv.index("--store") + 1])
        seen.append(store)
        store.mkdir(parents=True)
        (store / "mmap_0.dat").write_bytes(b"left by a killed tilemaker")
        if fails:
            raise SystemExit(1)
        Path(argv[argv.index("-o") + 1]).write_bytes(b"zim")
    monkeypatch.setattr(create_osm_zim, "main", builder)
    scratch = tmp_path / "scratch"
    argv = REQ + ["--area", "monaco", "--profile", "basic", "--tilemaker-store", "disk",
                  "--shapefiles", str(shp), "--file-name", "m",
                  "--output", str(tmp_path / "out"), "--tmp", str(scratch),
                  "--dl", str(tmp_path / "dl")]
    if keep_flag:
        argv.append(keep_flag)
    if fails:
        with pytest.raises(SystemExit):
            cli.main(argv)
    else:
        assert cli.main(argv) == 0
        assert (tmp_path / "out" / "m.zim").read_bytes() == b"zim"
    assert len(seen) == 1
    store = seen[0]
    assert store.name == cli.TILEMAKER_STORE_DIR
    assert store.parent.parent == scratch.resolve()
    assert store.parent.name.startswith("streetzim-build-")
    assert not store.exists()
    assert store.parent.exists() == bool(keep_flag)


def _tiles_with(monkeypatch, run):
    from streetzim import tiles
    monkeypatch.setattr(tiles.subprocess, "run", run)
    monkeypatch.setattr(tiles.os.path, "getsize", lambda p: 0)
    monkeypatch.setattr(tiles, "required_shapefiles", lambda: [])
    return tiles


def test_tiles_step_creates_the_store_and_passes_it(tmp_path, monkeypatch):
    runs = []

    def run(cmd, check):
        store = Path(cmd[cmd.index("--store") + 1])
        # tilemaker makes the leaf itself, but a missing parent aborts it.
        assert store.is_dir()
        runs.append(cmd)
    tiles = _tiles_with(monkeypatch, run)
    store = tmp_path / "work" / cli.TILEMAKER_STORE_DIR
    tiles.generate_tiles("in.pbf", str(tmp_path / "t.mbtiles"),
                         bbox="7.40,43.72,7.44,43.76", store=str(store))
    assert len(runs) == 1


@pytest.mark.parametrize("returncode, store, says", [
    (-7, True, True), (-9, True, False), (1, True, False), (-7, False, False)])
def test_sigbus_in_tilemaker_is_reported_as_a_full_disk(tmp_path, monkeypatch, capsys,
                                                         returncode, store, says):
    import subprocess

    def run(cmd, check):
        raise subprocess.CalledProcessError(returncode, cmd)
    tiles = _tiles_with(monkeypatch, run)
    where = str(tmp_path / "s") if store else None
    with pytest.raises(subprocess.CalledProcessError):
        tiles.generate_tiles("in.pbf", str(tmp_path / "t.mbtiles"),
                             bbox="7.40,43.72,7.44,43.76", store=where)
    out = capsys.readouterr().out
    assert ("on-disk store ran out of disk in " + str(where) in out) == says
