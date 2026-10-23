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
   7.50   43.71
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


def _clip_of(argv):
    """The outline create_osm_zim is given (--clip-poly), as written: not
    cut again, so the CLI's own cut is what is checked. The builder must
    load it with its box, as _activate_clip does. None without one."""
    ns = builder_args(argv)
    if ns.clip_poly is None:
        return None
    from create_osm_zim import parse_bbox
    from streetzim.clip import Clip, parse_poly
    Clip.from_poly_file(ns.clip_poly, bbox=parse_bbox(ns.bbox))
    return parse_poly(Path(ns.clip_poly).read_text())


def test_include_poly_cuts_the_build_to_the_polygon(tmp_path, no_network):
    pytest.importorskip("shapely")
    from shapely.geometry import Point
    from streetzim.clip import parse_poly
    url = "https://download.geofabrik.de/europe/monaco.poly"
    argv, info = plan(["--include-poly", url], tmp_path)
    geom = _clip_of(argv)
    assert info["clip_poly"] == builder_args(argv).clip_poly
    assert geom.symmetric_difference(parse_poly(POLY)).area < 1e-12   # the polygon itself
    assert geom.contains(Point(7.43, 43.75))
    assert not geom.contains(Point(7.415, 43.735))                     # its hole
    assert not geom.contains(Point(7.47, 43.73))                       # in the box, not the polygon
    # --no-clip: the polygon's box, as before.
    argv, info = plan(["--include-poly", url, "--no-clip"], tmp_path / "box")
    assert builder_args(argv).clip_poly is None and info["clip_poly"] is None
    assert info["bbox"] == "7.400000,43.700000,7.510000,43.760000"


def test_area_and_bbox_builds_keep_their_box(tmp_path, no_network):
    for i, argv in enumerate((["--area", "monaco"], ["--area", "monaco", "--clip"],
                              ["--bbox", "7.4,43.72,7.44,43.76", "--pbf-url", "x"])):
        a, info = plan(argv, tmp_path / str(i))
        assert builder_args(a).clip_poly is None and info["clip_poly"] is None


def test_several_polys_make_one_clip_cut_to_the_box_built(tmp_path, monkeypatch, capsys):
    pytest.importorskip("shapely")
    from shapely.geometry import Point, box
    # Switzerland and Liechtenstein, say: two files, one area. The second has
    # a far part, which the box built leaves out, and so does the clip.
    polys = {"a.poly": _poly((5.0, 45.0, 10.0, 48.0)),
             "b.poly": _poly((9.9, 47.0, 10.4, 47.3), (60.0, 10.0, 61.0, 11.0))}
    monkeypatch.setattr(cli, "fetch", lambda url, dest: (
        dest.parent.mkdir(parents=True, exist_ok=True),
        dest.write_text(polys.get(url.rsplit("/", 1)[1], "pbf")), dest)[2])
    argv, info = plan(["--include-poly", "https://example.org/a.poly,https://example.org/b.poly",
                       "--pbf-url", "https://example.org/x.pbf"], tmp_path)
    assert info["bbox"] == "5.000000,45.000000,10.400000,48.000000"
    assert "leaving out 1 part(s)" in capsys.readouterr().out
    geom = _clip_of(argv)
    assert geom.contains(Point(7.0, 46.0)) and geom.contains(Point(10.2, 47.1))
    assert not geom.contains(Point(10.2, 46.0))       # in the box, in neither polygon
    assert not geom.intersects(box(60.0, 10.0, 61.0, 11.0))


def test_a_box_across_the_antimeridian_keeps_its_box(tmp_path, monkeypatch, capsys):
    _fake_poly(monkeypatch, FIJI_LIKE)
    argv, info = plan(["--include-poly", "https://example.org/fiji.poly", "--pbf-url", "x"],
                      tmp_path)
    assert info["bbox"] == "172.840000,-23.120000,183.470000,-11.240000"
    assert builder_args(argv).clip_poly is None and info["clip_poly"] is None
    assert "across the antimeridian" in capsys.readouterr().out
    # Past 180 by less than the box's rounding: still across (the rounded
    # box ends at 180.000000, but the polygon does not).
    _fake_poly(monkeypatch, _poly((170.0, 50.0, 180.0000001, 60.0)))
    argv, info = plan(["--include-poly", "https://example.org/p.poly", "--pbf-url", "x"],
                      tmp_path / "rounded")
    assert info["clip_poly"] is None
    assert "across the antimeridian" in capsys.readouterr().out


def test_a_polygon_split_at_the_antimeridian_is_cut_inside_it(tmp_path, monkeypatch):
    pytest.importorskip("shapely")
    from shapely.geometry import Point
    # Geofabrik-style rings split at ±180: the main part ends on +180 and a
    # far part starts on -180, too far to share its box. (Geofabrik's Russia
    # and US parts are close enough to share one, which crosses: no clip.)
    # The builder refuses a vertex on ±180, so the clip is cut inside it.
    _fake_poly(monkeypatch, _poly((170.0, 50.0, 180.0, 60.0), (-180.0, 10.0, -178.0, 12.0)))
    argv, info = plan(["--include-poly", "https://example.org/p.poly", "--pbf-url", "x"],
                      tmp_path)
    assert info["bbox"] == "170.000000,50.000000,180.000000,60.000000"
    geom = _clip_of(argv)
    assert geom.contains(Point(175.0, 55.0)) and geom.contains(Point(179.99, 55.0))
    assert geom.bounds[0] >= 170.0 and geom.bounds[2] < 180.0


SQUARE = "p\n1\n7.40 43.72\n7.44 43.72\n7.44 43.76\n7.40 43.76\nEND\nEND\n"
SQUARE_BOX = "7.400000,43.720000,7.440000,43.760000"


def test_builds_writing_one_clip_at_once_do_not_share_a_part_file(tmp_path, monkeypatch):
    pytest.importorskip("shapely")
    from streetzim import clip
    # (In containers both builds are PID 1: a PID in the name would collide.)
    real, started, nested = clip._write_poly, [], []

    def write(geom, path):
        real(geom, path)
        if not started:                                    # a second build, mid-write
            started.append(path)
            nested.append(cli.border_clip([SQUARE], SQUARE_BOX, tmp_path))
    monkeypatch.setattr(clip, "_write_poly", write)
    out = cli.border_clip([SQUARE], SQUARE_BOX, tmp_path)
    assert out == nested[0] and clip.parse_poly(out.read_text()).area > 0


def test_the_clip_is_written_like_any_file_and_leaves_no_part(tmp_path):
    pytest.importorskip("shapely")
    # A umask that tells the umask's mode (0640) from a fixed 0644 or 0600.
    old = os.umask(0o027)
    try:
        out = cli.border_clip([SQUARE], SQUARE_BOX, tmp_path)
    finally:
        os.umask(old)
    assert out.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in out.parent.iterdir()] == [out.name]


@pytest.mark.parametrize("exc", [OSError, KeyboardInterrupt, SystemExit])
def test_a_failed_clip_write_leaves_nothing(tmp_path, monkeypatch, exc):
    pytest.importorskip("shapely")
    from streetzim import clip

    def bad(geom, path):
        Path(path).write_text("partial")
        raise exc()
    monkeypatch.setattr(clip, "_write_poly", bad)
    with pytest.raises(exc):
        cli.border_clip([SQUARE], SQUARE_BOX, tmp_path)
    assert list((tmp_path / "poly").iterdir()) == []


def test_include_poly_with_ready_made_tiles_is_cut_too(tmp_path, no_network):
    from tests.mbtiles_fixture import make_mbtiles
    t = make_mbtiles(tmp_path / "t.mbtiles", [(0, 0, 0)])
    argv, info = plan(["--include-poly", "https://download.geofabrik.de/europe/monaco.poly",
                       "--mbtiles", str(t)], tmp_path)
    assert "--mbtiles" in argv and info["clip_poly"] == builder_args(argv).clip_poly
    assert info["clip_poly"]


def test_a_polygon_the_clip_cannot_read_fails_before_the_extract(tmp_path, monkeypatch):
    pytest.importorskip("shapely")
    # Two points: a box for the area, but no outline to cut to.
    asked = []

    def fake_fetch(url, dest):
        asked.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("p\n1\n7.40 43.72\n7.44 43.76\nEND\nEND\n")
        return dest
    monkeypatch.setattr(cli, "fetch", fake_fetch)
    with pytest.raises(ValueError, match="--no-clip"):
        plan(["--include-poly", "https://download.geofabrik.de/europe/monaco.poly"], tmp_path)
    assert asked == ["https://download.geofabrik.de/europe/monaco.poly"]
    argv, info = plan(["--include-poly", "https://download.geofabrik.de/europe/monaco.poly",
                       "--no-clip"], tmp_path / "box")
    assert info["clip_poly"] is None and info["bbox"] == "7.400000,43.720000,7.440000,43.760000"


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

    def cut(src, bbox, out, clip=True):
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


# ------------------------------------------------------------ --multilingual
# What StreetZim's own region builds make (ops/region-ml-tiles.py, the
# driver's --tile-languages/--region-languages), from the openZIM command.

def _matrix_plus(*extra):
    from streetzim.languages import MATRIX_TILE_LANGUAGES
    return sorted(set(MATRIX_TILE_LANGUAGES) | set(extra))


def test_multilingual_labels_the_matrix_plus_a_short_rows_own_languages(tmp_path, no_network):
    argv, _ = plan(["--area", "monaco", "--multilingual",
                    "--region-languages", "de,fr,it,rm"], tmp_path)
    ns = builder_args(argv)
    assert ns.tile_languages.split(",") == _matrix_plus("rm")      # Romansh joins
    assert ns.region_languages.split(",") == ["de", "fr", "it", "rm"]


def test_a_long_row_adds_no_label_languages(tmp_path, no_network):
    row = "de,fr,it,nl,pl,es,pt,ro"                        # 8 > REGION_TILE_ROW_MAX
    argv, _ = plan(["--area", "monaco", "--multilingual", "--region-languages", row],
                   tmp_path)
    ns = builder_args(argv)
    assert ns.tile_languages.split(",") == _matrix_plus()
    assert ns.region_languages.split(",") == row.split(",")


def test_tile_languages_replace_the_default(tmp_path, no_network):
    argv, _ = plan(["--area", "monaco", "--multilingual", "--tile-languages", "fr,de"],
                   tmp_path)
    assert builder_args(argv).tile_languages.split(",") == ["de", "fr"]


def test_a_single_language_build_passes_no_label_languages(tmp_path, no_network):
    argv, _ = plan(["--area", "monaco"], tmp_path)
    ns = builder_args(argv)
    assert ns.tile_languages is None and ns.region_languages is None
    argv, _ = plan(["--area", "monaco", "--region-languages", "fr"], tmp_path / "r")
    ns = builder_args(argv)
    assert ns.tile_languages is None and ns.region_languages == "fr"


@pytest.mark.parametrize("flags, needle", [
    (["--tile-languages", "de"], "--tile-languages needs --multilingual"),
    (["--no-translated-search"], "--no-translated-search needs --multilingual"),
    (["--multilingual", "--region-languages", "de,xx-bogus"], "region"),
    (["--multilingual", "--tile-languages", "de,qqq"], "qqq"),
])
def test_language_flag_mistakes_fail_before_any_download(tmp_path, no_network, capsys,
                                                          flags, needle):
    with pytest.raises(SystemExit):
        plan(["--area", "monaco"] + flags, tmp_path)
    assert no_network == []
    assert needle in capsys.readouterr().err


def _vector_mbtiles(path, names):
    """An MBTiles (plain tiles table) whose zoom-0 tile has one place feature
    with these properties, gzip-encoded like tilemaker's."""
    import gzip
    import sqlite3
    import mapbox_vector_tile
    data = gzip.compress(mapbox_vector_tile.encode(
        [{"name": "place", "features": [{"geometry": "POINT(2048 2048)",
                                         "properties": names}]}]))
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE metadata (name text, value text)")
    conn.executemany("INSERT INTO metadata VALUES (?, ?)",
                     [("format", "pbf"), ("name", "t"), ("bounds", "-180,-85,180,85")])
    conn.execute("CREATE TABLE tiles (zoom_level integer, tile_column integer, "
                 "tile_row integer, tile_data blob)")
    conn.execute("INSERT INTO tiles VALUES (0, 0, 0, ?)", (data,))
    conn.commit()
    conn.close()
    return path


def test_ready_made_tiles_must_carry_some_label_language(tmp_path, no_network, capsys):
    good = _vector_mbtiles(tmp_path / "ml.mbtiles",
                           {"name": "Monaco", "name:de": "Monaco", "name:ru": "Монако",
                            "name:latin": "Monaco"})
    for flag, value, sub in (("--mbtiles", str(good), "a"), ("--mbtiles-url", good.as_uri(), "u")):
        argv, _ = plan(["--area", "monaco", flag, value, "--multilingual"], tmp_path / sub)
        # tileLanguages (and the search records' ln) name only what the tiles carry
        assert builder_args(argv).tile_languages == "de,ru", flag
        out = capsys.readouterr().out
        assert "MBTiles labels: 2 of" in out and "local names there" in out
    plain = _vector_mbtiles(tmp_path / "plain.mbtiles", {"name": "Monaco",
                                                          "name:latin": "Monaco"})
    with pytest.raises(ValueError, match="no name:XX for any label language"):
        plan(["--area", "monaco", "--mbtiles", str(plain), "--multilingual"], tmp_path / "b")


def test_name_languages_reads_every_low_zoom_tile(tmp_path):
    import gzip
    import sqlite3
    import mapbox_vector_tile
    from streetzim import mbtiles
    t = _vector_mbtiles(tmp_path / "t.mbtiles", {"name": "x", "name:fr": "x", "name:latin": "x"})
    conn = sqlite3.connect(str(t))
    for z, key, packed in ((8, "name:it", True), (9, "name:ja", False)):
        tile = mapbox_vector_tile.encode([{"name": "place", "features": [
            {"geometry": "POINT(1 1)", "properties": {key: "x"}}]}])
        conn.execute("INSERT INTO tiles VALUES (?, 0, 0, ?)",
                     (z, gzip.compress(tile) if packed else tile))
    conn.commit()
    conn.close()
    assert mbtiles.name_languages(t) == ({"fr", "it"}, 0)                  # zooms 0-8
    assert mbtiles.name_languages(t, max_zoom=9) == ({"fr", "it", "ja"}, 0)   # an unzipped tile too
    assert mbtiles.name_languages(t, max_zoom=-1) == (set(), 0)


def test_name_languages_reads_zlib_skips_bad_tiles_and_stops_early(tmp_path):
    import sqlite3
    import zlib
    import mapbox_vector_tile
    from streetzim import mbtiles
    t = _vector_mbtiles(tmp_path / "t.mbtiles", {"name": "x", "name:fr": "x"})
    tile = mapbox_vector_tile.encode([{"name": "place", "features": [
        {"geometry": "POINT(1 1)", "properties": {"name:it": "x"}}]}])
    conn = sqlite3.connect(str(t))
    conn.executemany("INSERT INTO tiles VALUES (?, 0, 0, ?)",
                     [(1, zlib.compress(tile)),                 # zlib, as some writers store
                      (2, b"\x1f\x8b\x08\x00broken gzip"),       # bad gzip
                      (3, b"\x0a\xff\xff\xff")])                  # bad protobuf
    conn.commit()
    conn.close()
    assert mbtiles.name_languages(t) == ({"fr", "it"}, 2)
    assert mbtiles.name_languages(t, want=["fr"]) == ({"fr"}, 0)   # stopped at the first tile


def test_name_languages_refuses_raster_tiles(tmp_path):
    import sqlite3
    from streetzim import mbtiles
    t = _vector_mbtiles(tmp_path / "png.mbtiles", {"name:fr": "x"})
    conn = sqlite3.connect(str(t))
    conn.execute("UPDATE metadata SET value = 'png' WHERE name = 'format'")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="raster tiles"):
        mbtiles.name_languages(t)
    t2 = _vector_mbtiles(tmp_path / "nofmt.mbtiles", {"name:fr": "x"})
    conn = sqlite3.connect(str(t2))
    conn.execute("DELETE FROM metadata WHERE name = 'format'")
    conn.execute("UPDATE tiles SET tile_data = ?", (b"\x89PNG\r\n\x1a\n...",))
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="raster tiles"):
        mbtiles.name_languages(t2)


@pytest.mark.parametrize("outside, flags, want", [
    (None, ["--multilingual"], "1"),
    ("0", ["--multilingual"], "1"),                        # the flag decides, not the shell
    (None, ["--multilingual", "--no-translated-search"], "0"),
    ("1", ["--multilingual", "--no-translated-search"], "0"),
    (None, [], None),                                      # single language: left alone
    ("0", [], "0"),
])
def test_translated_search_switch_reaches_the_builder_and_is_restored(
        tmp_path, monkeypatch, outside, flags, want):
    import create_osm_zim
    seen = {}
    if outside is None:
        monkeypatch.delenv("STREETZIM_SEARCH_LN", raising=False)
    else:
        monkeypatch.setenv("STREETZIM_SEARCH_LN", outside)
    monkeypatch.setattr(cli, "plan", lambda *a, **k: (["--bbox=0,0,1,1"], {}))
    monkeypatch.setattr(cli, "ensure_shapefiles", lambda folder: tmp_path)
    monkeypatch.setattr(cli, "publish", lambda building, final, overwrite: None)
    monkeypatch.setattr(create_osm_zim, "main",
                        lambda argv: seen.setdefault("ln", os.environ.get("STREETZIM_SEARCH_LN")))
    args = cli.parse_args(REQ + ["--profile", "basic", "--area", "monaco"] + flags)
    assert cli._build(args, tmp_path, None, tmp_path, tmp_path / "b.zim", tmp_path / "f.zim") == 0
    assert seen["ln"] == want
    assert os.environ.get("STREETZIM_SEARCH_LN") == outside


def test_build_restores_the_language_environment_when_the_builder_fails(tmp_path, monkeypatch):
    import create_osm_zim
    monkeypatch.delenv("STREETZIM_SEARCH_LN", raising=False)
    monkeypatch.setenv("STREETZIM_TILE_LANGUAGES", "xx")
    monkeypatch.setattr(cli, "plan", lambda *a, **k: (["--bbox=0,0,1,1"], {}))
    monkeypatch.setattr(cli, "ensure_shapefiles", lambda folder: tmp_path)

    def failing_builder(argv):
        os.environ["STREETZIM_TILE_LANGUAGES"] = "de,fr"   # as create_osm_zim's own setup does
        raise RuntimeError("tilemaker died")
    monkeypatch.setattr(create_osm_zim, "main", failing_builder)
    args = cli.parse_args(REQ + ["--profile", "basic", "--area", "monaco", "--multilingual",
                                 "--no-translated-search"])
    with pytest.raises(RuntimeError):
        cli._build(args, tmp_path, None, tmp_path, tmp_path / "b.zim", tmp_path / "f.zim")
    assert "STREETZIM_SEARCH_LN" not in os.environ
    assert os.environ["STREETZIM_TILE_LANGUAGES"] == "xx"


def test_region_languages_without_label_support_are_recorded(tmp_path, no_network, capsys):
    # Georgia's languages: az has label support, ka and hy do not (no tiles carry them)
    argv, _ = plan(["--area", "monaco", "--multilingual", "--region-languages", "ka,ru,hy,az"],
                   tmp_path)
    ns = builder_args(argv)
    assert ns.tile_languages.split(",") == _matrix_plus("az")
    assert ns.region_languages.split(",") == ["ka", "ru", "hy", "az"]
    out = capsys.readouterr().out
    assert "ka: recorded, no label support" in out and "hy: recorded, no label support" in out
    assert "plus az of the region's 4" in out


def test_every_streetzim_region_row_labels_as_before():
    # streetzim and StreetZim's own builds (ops/region-ml-tiles.py) agree on
    # every row of cloud/region-languages.tsv
    from streetzim import languages as L
    rows = [line.rstrip("\n").split("\t") for line in
            (ROOT / "cloud" / "region-languages.tsv").read_text().splitlines()
            if "\t" in line and not line.startswith("#")]
    assert len(rows) >= 71
    for rid, row in rows:
        langs = L.region_languages(row)
        assert langs == row.split(","), rid
        assert L.region_label_languages(langs) == (L.default_tile_languages(langs), []), rid


@pytest.mark.parametrize("flags, want, source", [
    (["--area", "monaco"], "fr",
     "the official languages of the Geofabrik extract europe/monaco; "),
    (["--area", "california"], "en,es", "StreetZim region california; "),
    (["--area", "us"], "en,es,zh", "StreetZim region united-states, the outline north-america/us; "),
    (["--area", "dc"], "en,es", "StreetZim region washington-dc, the box of --area dc; "),
    (["--include-poly", "https://download.geofabrik.de/europe/switzerland.poly"],
     "de,fr,it,rm", "StreetZim region switzerland, the extract europe/switzerland; "),
    (["--include-poly", "https://download.geofabrik.de/north-america/us/alaska.poly"],
     "en,esu", "StreetZim region alaska, the extract north-america/us/alaska; "),
    (["--include-poly", ",".join(f"https://download.geofabrik.de/europe/{c}.poly"
                                 for c in ("lithuania", "latvia", "estonia")),
      "--pbf-url", "https://example.org/baltics.pbf"],
     "lt,lv,et", "StreetZim region baltics, the outline "),
    (["--include-poly", "https://download.geofabrik.de/europe/ukraine.poly"],
     "uk", "the official languages of the Geofabrik extract europe/ukraine; "),
    (["--include-poly", "https://download.geofabrik.de/europe/ukraine.poly,"
                        "https://download.geofabrik.de/europe/moldova.poly",
      "--pbf-url", "https://example.org/um.pbf"],
     "uk,ro", "the official languages of the Geofabrik extracts europe/ukraine, "),
    (["--include-poly", "https://download.geofabrik.de/europe/ukraine.poly,"
                        "https://download.geofabrik.de/europe/moldova.poly,"
                        "https://download.geofabrik.de/russia.poly",
      "--pbf-url", "https://example.org/umr.pbf"],
     "ru,uk,ro", "StreetZim region ukraine, the outline "),
    (["--include-poly", "https://download.geofabrik.de/europe/monaco.poly,"
                        "https://download.geofabrik.de/europe/liechtenstein.poly",
      "--pbf-url", "https://example.org/two.pbf"],
     "fr,de", "the official languages of the Geofabrik extracts europe/monaco, "
              "europe/liechtenstein; "),
])
def test_multilingual_derives_the_region_languages(tmp_path, no_network, capsys,
                                                   flags, want, source):
    from streetzim import area_languages
    argv, _ = plan(flags + ["--multilingual"], tmp_path)
    ns = builder_args(argv)
    assert ns.region_languages == want
    out = capsys.readouterr().out
    assert f"Region languages: {want} ({source}" in out
    assert "Label languages: the 21 label languages" in out
    # Only the Geofabrik table (CLDR and GeoNames data) is credited
    assert ns.region_languages_source == (area_languages.GEOFABRIK_CREDIT
                                          if "Geofabrik extract" in source
                                          and "StreetZim" not in source else None)
    if want in ("de,fr,it,rm", "en,esu"):                  # Romansh and Yup'ik label the map
        assert ns.tile_languages.split(",") == _matrix_plus(want.split(",")[-1])



@pytest.mark.parametrize("polys, hint", [
    (["https://download.geofabrik.de/europe.poly"], False),          # a continent alone
    (["https://download.geofabrik.de/europe/monaco.poly", "https://example.org/x.poly"], True),
    (["https://example.org/x.poly"], True),
])
def test_no_region_languages_for_continents_and_foreign_polygons(tmp_path, no_network, capsys,
                                                                 polys, hint):
    argv, _ = plan(["--include-poly", ",".join(polys), "--pbf-url", "https://example.org/x.pbf",
                    "--multilingual"], tmp_path)
    ns = builder_args(argv)
    assert ns.region_languages is None and ns.region_languages_source is None
    assert ns.tile_languages.split(",") == _matrix_plus()
    out = capsys.readouterr().out
    assert "WARNING: --multilingual without region languages" in out
    assert ("Hint: pass --region-languages" in out) == hint


def test_part_of_a_country_records_its_first_language_only(tmp_path, no_network, capsys):
    argv, _ = plan(["--include-poly", "https://download.geofabrik.de/europe/spain/andalucia.poly",
                    "--multilingual"], tmp_path)
    ns = builder_args(argv)
    assert ns.region_languages == "es" and ns.tile_languages.split(",") == _matrix_plus()
    out = capsys.readouterr().out
    assert "Region languages: es (the first language of the country of the Geofabrik " in out
    assert "Hint: europe/spain/andalucia is part of a country" in out
    assert "--region-languages" in out.split("Hint:")[1]


def test_part_of_a_country_adds_no_label_language(tmp_path, no_network, capsys, monkeypatch):
    from streetzim import area_languages
    table = tmp_path / "geofabrik-languages.tsv"
    table.write_text("# test\neurope/sweden/x\tpart\tSE\tsv\n")
    monkeypatch.setattr(area_languages, "GEOFABRIK_TABLE", table)
    argv, _ = plan(["--include-poly", "https://download.geofabrik.de/europe/sweden/x.poly",
                    "--multilingual"], tmp_path)
    ns = builder_args(argv)
    assert ns.region_languages == "sv"
    assert ns.tile_languages.split(",") == _matrix_plus()       # sv has label support, still none
    assert "none of the region's (part of a country)" in capsys.readouterr().out


def test_single_language_builds_pass_no_region_language_source(tmp_path, no_network):
    argv, _ = plan(["--area", "monaco", "--region-languages", "fr"], tmp_path)
    assert builder_args(argv).region_languages_source is None
    assert not any(a.startswith("--region-languages-source") for a in argv)


def test_derived_region_languages_are_credited_in_the_license(tmp_path):
    import tests.test_zim_metadata as tzm
    from streetzim import area_languages
    md = tzm._build(tmp_path, map_config_extra={
        "regionLanguages": ["fr"], "regionLanguagesSource": area_languages.GEOFABRIK_CREDIT})
    lic = md["License"].decode()
    assert f"Region languages: {area_languages.GEOFABRIK_CREDIT}" in lic
    assert "GeoNames" in lic and "CC BY 4.0" in lic
    (tmp_path / "p").mkdir()
    plain = tzm._build(tmp_path / "p")
    assert "Region languages" not in plain["License"].decode()


def test_region_languages_given_none_or_unknown_warn(tmp_path, no_network, capsys):
    argv, _ = plan(["--area", "monaco", "--multilingual", "--region-languages", "none"], tmp_path)
    ns = builder_args(argv)
    assert ns.region_languages is None and ns.tile_languages.split(",") == _matrix_plus()
    assert ("WARNING: --multilingual without region languages (--region-languages=none)"
            in capsys.readouterr().out)
    argv, _ = plan(["--bbox", "7.40,43.72,7.44,43.76", "--pbf-url", "https://example.org/x.pbf",
                    "--multilingual"], tmp_path / "b")
    assert builder_args(argv).region_languages is None
    assert "(none known for this area)" in capsys.readouterr().out


def test_single_language_builds_derive_no_region_languages(tmp_path, no_network):
    argv, _ = plan(["--area", "monaco"], tmp_path)
    assert builder_args(argv).region_languages is None
    argv, _ = plan(["--area", "monaco", "--region-languages", "none"], tmp_path / "n")
    assert builder_args(argv).region_languages is None


def test_three_letter_region_codes_become_two_letter(tmp_path, no_network):
    argv, _ = plan(["--area", "monaco", "--multilingual", "--region-languages", "deu,fra,haw"],
                   tmp_path)
    ns = builder_args(argv)
    assert ns.region_languages == "de,fr,haw"
    assert ns.tile_languages.split(",") == _matrix_plus("haw")


def test_english_counts_toward_the_region_row_limit():
    from streetzim import languages as L
    matrix = sorted(L.MATRIX_TILE_LANGUAGES)
    seven = ["en", "de", "fr", "it", "nl", "pl", "rm"]
    assert L.region_label_languages(seven) == (sorted(matrix + ["rm"]), [])
    assert L.region_label_languages(seven + ["es"]) == (matrix, [])     # eight with English


def test_bibliographic_and_terminology_codes_become_two_letter():
    from streetzim import languages as L
    assert L.region_languages("ger,deu,fre,FRA,haw,gsw") == ["de", "fr", "haw", "gsw"]
