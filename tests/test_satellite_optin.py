"""Satellite imagery in `streetzim`: off by default, one flag to turn on,
each source's licence in the metadata and the viewer, and a non-commercial
source only with an explicit acknowledgement and a restricted label."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

cli = pytest.importorskip("streetzim.cli")
pytest.importorskip("regex")
from streetzim import satellite_sources as ss  # noqa: E402

REQ = ["--name", "osm_en_monaco", "--title", "Monaco", "--description", "Offline Monaco",
       "--area", "monaco"]
FREE, NC = ss.SOURCES["s2cloudless-2016"], ss.SOURCES["s2cloudless-2021"]


def _args(extra):
    a = cli.build_parser().parse_args(REQ + extra)
    cli.apply_satellite(a)
    return a


@pytest.fixture
def no_network(monkeypatch):
    asked = []

    def fake_fetch(url, dest):
        asked.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("pbf")
        return dest
    monkeypatch.setattr(cli, "fetch", fake_fetch)
    return asked


def _builder(argv):
    import create_osm_zim
    return create_osm_zim.build_parser().parse_args(argv)


# ------------------------------------------------------------ the facts


def test_sources_and_their_licences():
    assert not FREE.noncommercial and FREE.license == "CC BY 4.0"
    assert FREE.layer == "s2cloudless_3857" and FREE.year == "2016"
    assert NC.noncommercial and NC.license == "CC BY-NC-SA 4.0"
    assert NC.layer == "s2cloudless-2021_3857"
    for s in (FREE, NC):
        assert s.attribution.startswith(
            "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains "
            "modified Copernicus Sentinel data")
    assert "2016 & 2017" in FREE.attribution and "data 2021)" in NC.attribution
    # The builder's default is unchanged: the 2021 mosaic, its old tile URL.
    from streetzim.common import SATELLITE_TILE_URL
    assert ss.BUILDER_DEFAULT == NC.key and SATELLITE_TILE_URL == NC.tile_url
    assert ss.OPENZIM_DEFAULT == FREE.key


def test_each_source_has_its_own_tile_cache(tmp_path, monkeypatch):
    import streetzim.satellite as sat
    monkeypatch.setattr(sat, "CACHE_DIR", str(tmp_path))
    # 2021 keeps the names existing caches have.
    assert sat.satellite_cache_dirs(NC.key, "avif", 256) == (
        str(tmp_path / "satellite_cache_sources"), str(tmp_path / "satellite_cache_avif_256"))
    src, enc = sat.satellite_cache_dirs(FREE.key, "avif", 256)
    assert src == str(tmp_path / "satellite_s2cloudless-2016" / "sources")
    assert enc == str(tmp_path / "satellite_s2cloudless-2016" / "avif_256")


def test_2016_never_reuses_2021_tiles(tmp_path, monkeypatch):
    """A 2021 tile in the shared caches must not end up in a 2016 build."""
    from PIL import Image
    import streetzim.satellite as sat
    monkeypatch.setattr(sat, "CACHE_DIR", str(tmp_path))
    for d in ("satellite_cache_sources/0/0", "satellite_cache_webp_256/0/0"):
        (tmp_path / d).mkdir(parents=True)
    Image.new("RGB", (256, 256), "red").save(tmp_path / "satellite_cache_sources/0/0/0.jpg")
    Image.new("RGB", (256, 256), "red").save(tmp_path / "satellite_cache_webp_256/0/0/0.webp")
    urls = []

    class Resp:
        def __init__(self, url):
            urls.append(url)
            import io
            buf = io.BytesIO()
            Image.new("RGB", (256, 256), "blue").save(buf, "JPEG")
            self.data = buf.getvalue()

        def read(self):
            return self.data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(sat.urllib.request, "urlopen", lambda req, timeout=0: Resp(req.full_url))
    dest = sat.satellite_cache_dirs(FREE.key, "webp", 256)[1]
    sat.download_satellite_tiles("-1,-1,1,1", dest, max_zoom=0, sat_format="webp",
                                 source=FREE.key)
    assert urls == [FREE.tile_url.format(z=0, x=0, y=0)]
    px = Image.open(Path(dest) / "0/0/0.webp").convert("RGB").getpixel((128, 128))
    assert px[2] > 200 and px[0] < 60          # blue: downloaded, not the red 2021 tile


# ------------------------------------------------------------ the flags


def test_off_by_default(no_network, tmp_path):
    a = _args([])
    assert not a.satellite and a.flavour is None and a.file_name == "{name}_{period}"
    argv, _ = cli.plan(a, tmp_path)
    ns = _builder(argv)
    assert not ns.satellite and ns.flavour is None and "--satellite" not in argv


def test_one_flag_turns_on_the_free_source(no_network, tmp_path):
    a = _args(["--satellite"])
    assert a.flavour == "satellite" and a.tags == "satellite"
    assert a.long_description is None                   # nothing to warn about
    assert a.file_name == "{name}_satellite_{period}"
    ns = _builder(cli.plan(a, tmp_path)[0])
    assert ns.satellite and ns.satellite_source == FREE.key and ns.flavour == "satellite"
    assert ns.satellite_zoom is None


def test_noncommercial_needs_the_acknowledgement(no_network, tmp_path, capsys):
    with pytest.raises(ValueError, match="satellite-accept-noncommercial"):
        _args(["--satellite", "--satellite-source", NC.key])
    rc = cli.main(REQ + ["--satellite-source", NC.key, "--output", str(tmp_path / "o"),
                         "--tmp", str(tmp_path / "t")])
    err = capsys.readouterr().err
    assert rc == 2 and "CC BY-NC-SA 4.0" in err and "non-commercial" in err
    assert no_network == []                              # refused before downloading


def test_noncommercial_build_is_labelled_restricted(no_network, tmp_path):
    # Two flags: the source implies --satellite.
    a = _args(["--satellite-source", NC.key, "--satellite-accept-noncommercial",
               "--tags", "openstreetmap", "--satellite-max-zoom", "12"])
    assert a.flavour == "satellite-nc"
    assert a.tags == "openstreetmap;satellite;non-commercial"
    assert a.long_description.startswith("Offline Monaco\n\nRestricted:")
    assert "CC BY-NC-SA 4.0" in a.long_description and "non-commercial" in a.long_description
    assert a.file_name == "{name}_satellite-nc_{period}"
    from streetzim.zim_metadata import build_overrides
    md = build_overrides(long_description=a.long_description, tags=a.tags, flavour=a.flavour)
    assert md["Flavour"] == "satellite-nc"
    ns = _builder(cli.plan(a, tmp_path)[0])
    assert ns.satellite_source == NC.key and ns.flavour == "satellite-nc"
    assert ns.satellite_zoom == 12 and ns.tags == a.tags


def test_user_long_description_keeps_its_text(no_network):
    a = _args(["--satellite-source", NC.key, "--satellite-accept-noncommercial",
               "--long-description", "Streets of Monaco."])
    assert a.long_description.startswith("Streets of Monaco.\n\nRestricted:")


def test_acknowledging_is_harmless_for_a_free_source():
    a = _args(["--satellite", "--satellite-source", FREE.key,
               "--satellite-accept-noncommercial"])
    assert a.flavour == "satellite" and "non-commercial" not in a.tags


@pytest.mark.parametrize("extra", [["--satellite-accept-noncommercial"],
                                   ["--satellite-max-zoom", "12"]])
def test_satellite_options_without_satellite_are_refused(extra):
    with pytest.raises(ValueError, match="needs --satellite"):
        _args(extra)


def test_file_name_flavour_placeholder():
    assert _args(["--file-name", "{name}_{flavour}_{period}"]).file_name == \
        "{name}_maxi_{period}"
    assert _args(["--satellite", "--file-name", "x"]).file_name == "x"


def test_definition_offers_it_and_zimfarm_argv_parses():
    import json
    d = json.loads((ROOT / "offliner-definition.json").read_text())
    a = cli.build_parser().parse_args(
        REQ + ["--satellite", "--satellite-source=s2cloudless-2021",
               "--satellite-accept-noncommercial", "--satellite-max-zoom=13"])
    assert a.satellite and a.satellite_source == NC.key and a.satellite_max_zoom == 13
    assert not d["flags"]["satellite"]["required"]


# ------------------------------------------------------------ the ZIM


@pytest.mark.parametrize("key", sorted(ss.SOURCES))
def test_metadata_and_map_config_per_source(tmp_path, key):
    src = ss.SOURCES[key]
    sat = tmp_path / "sat"
    sat.mkdir()
    from streetzim import zim_metadata as zm
    import tests.test_zim_metadata as tzm
    md = tzm._build(tmp_path, satellite_dir=str(sat),
                    map_config_extra={"hasSatellite": True, **ss.map_config(src)},
                    metadata=zm.build_overrides(flavour=ss.flavour(src)))
    lic = md["License"].decode()
    assert src.license_metadata in lic
    assert lic.startswith("Non-commercial use only") == src.noncommercial
    assert ("NC" in lic) == src.noncommercial
    assert md["Flavour"].decode() == ss.flavour(src)
    cfg = md["map-config"]
    assert cfg["satelliteSource"] == key and cfg["satelliteAttribution"] == src.attribution
    assert cfg["satelliteNonCommercial"] == src.noncommercial
    # tools/check_openzim_output.py agrees (bar the tags/LongDescription the
    # streetzim command adds).
    from check_openzim_output import satellite_problems
    probs = satellite_problems(md, cfg, key)
    assert all("Tags" in p or "LongDescription" in p for p in probs), probs


def test_legacy_satellite_license_unchanged_in_substance(tmp_path):
    """No source named (older callers): the builder's 2021 mosaic."""
    sat = tmp_path / "sat"
    sat.mkdir()
    import tests.test_zim_metadata as tzm
    md = tzm._build(tmp_path, satellite_dir=str(sat))
    lic = md["License"].decode()
    assert lic.startswith("Non-commercial use only") and NC.license_metadata in lic
    assert md["Flavour"] == b"maxi"
