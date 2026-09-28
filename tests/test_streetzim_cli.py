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


def test_area_uses_geofabrik_extract_and_forwards_metadata(tmp_path, no_network):
    argv, info = plan(["--area", "monaco", "--tags", "a;b", "--default-view", "43.73,7.42,13"],
                      tmp_path)
    assert info["pbf_url"] == "https://download.geofabrik.de/europe/monaco-latest.osm.pbf"
    assert no_network == [info["pbf_url"]]
    get = lambda f: argv[argv.index(f) + 1]  # noqa: E731
    assert get("--bbox") == "7.40,43.72,7.44,43.76"
    assert get("--zim-name") == "osm_en_monaco" and get("--title") == "Monaco"
    assert get("--publisher") == "openZIM"
    assert get("--tags") == "a;b"
    assert get("--map-center") == "7.42,43.73" and get("--map-zoom") == "13"
    assert "--routing" in argv and "--satellite" not in argv


def test_geofabrik_poly_selects_its_extract(tmp_path, no_network):
    argv, info = plan(["--include-poly", "https://download.geofabrik.de/europe/monaco.poly"],
                      tmp_path)
    assert info["pbf_url"].endswith("/europe/monaco-latest.osm.pbf")
    assert info["bbox"] == "7.400000,43.700000,7.510000,43.760000"


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


def test_polys_and_bboxes_reaching_the_antimeridian_are_refused(tmp_path, monkeypatch):
    def fake_fetch(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(RUSSIA_LIKE)
        return dest
    monkeypatch.setattr(cli, "fetch", fake_fetch)
    with pytest.raises(ValueError, match="antimeridian"):
        plan(["--include-poly", "https://example.org/russia.poly", "--pbf-url", "x"],
             tmp_path)
    for bad in ("170,10,180,20", "10,20,5,30", "1,2,3", "0,-91,1,1"):
        with pytest.raises(ValueError):
            cli.parse_bbox_arg(bad)
    assert cli.parse_bbox_arg("7.40,43.72,7.44,43.76") == (7.40, 43.72, 7.44, 43.76)


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
