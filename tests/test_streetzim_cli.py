"""`streetzim` (streetzim/cli.py): flag translation, .poly parsing, file
naming and the checks that must fail before any download."""
from __future__ import annotations

import datetime
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
    return cli.plan(cli.build_parser().parse_args(REQ + argv), tmp_path)


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


def test_terrain_is_on_by_default_and_no_terrain_turns_it_off(tmp_path, no_network):
    argv, _ = plan(["--area", "monaco"], tmp_path)
    assert builder_args(argv).terrain
    argv, _ = plan(["--area", "monaco", "--no-terrain"], tmp_path)
    assert not builder_args(argv).terrain
    argv, _ = plan(["--area", "monaco", "--terrain"], tmp_path)   # still accepted
    assert builder_args(argv).terrain


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
    argv, _ = plan(["--bbox", "7.4,43.72,7.44,43.76", "--mbtiles", "t.mbtiles",
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
    argv, _ = cli.plan(cli.build_parser().parse_args(REQ + ["--area", "monaco"]), tmp_path,
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
