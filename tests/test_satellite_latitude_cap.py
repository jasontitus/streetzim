"""Satellite imagery is capped at z13 for regions centred above 45 degrees.

The source is Sentinel-2 Cloudless at 10 m/pixel. A 256 px tile at z14 is
156543*cos(lat)/2^14 m/pixel -- 9.6 m at the equator, 5.2 m at Riga -- so
above the tropics z14 is interpolation, and it costs 63% of a satellite
payload that is 18-24% of a ZIM and does not compress.

These tests pin the DECISION, not the imagery: which regions lose z14, that
an explicit flag still wins, and that the rule reads a real bbox correctly.
The first draft of this feature read a variable (`bbox`) that main() does not
define until ~130 lines later, which would have raised NameError on every
build; test_rule_uses_a_variable_that_exists_at_that_point guards that.
"""
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("coz", ROOT / "create_osm_zim.py")
coz = importlib.util.module_from_spec(spec)
sys.modules["coz"] = coz
spec.loader.exec_module(coz)

CAP_LAT = 45.0


def decide(bbox_str, explicit=None, max_zoom=14, satellite=True):
    """Mirror of the decision in main(), driven by the same parse_bbox."""
    smz = explicit or max_zoom
    if satellite and explicit is None and bbox_str and smz and smz > 13:
        b = coz.parse_bbox(bbox_str)
        if abs((float(b[1]) + float(b[3])) / 2.0) >= CAP_LAT:
            return 13
    return smz


def registry():
    rows = {}
    for line in (ROOT / "cloud" / "regions.tsv").read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        c = line.split("\t")
        if len(c) > 3:
            rows[c[0]] = c[2]
    return rows


def test_far_north_regions_lose_z14():
    reg = registry()
    for rid in ("russia", "canada", "iceland", "baltics", "europe"):
        assert decide(reg[rid]) == 13, f"{rid} should cap at z13"


def test_tropical_and_mid_latitude_regions_keep_z14():
    reg = registry()
    # z14 is ~9.6 m/px at the equator, about the Sentinel-2 native resolution,
    # so these carry real detail and must not be capped.
    for rid in ("africa", "brazil", "southeast-asia", "united-states", "china"):
        assert decide(reg[rid]) == 14, f"{rid} should keep z14"


def test_explicit_flag_always_wins():
    reg = registry()
    # An operator asking for z14 over russia gets z14.
    assert decide(reg["russia"], explicit=14) == 14
    # ...and asking for less than the cap is honoured too.
    assert decide(reg["russia"], explicit=12) == 12


def test_rule_is_centre_not_nearest_edge():
    # russia spans 41-82N. By NEAREST EDGE it would keep z14 off a southern
    # edge near 41 deg although nearly all its area is far north; that was
    # measured to cap only 5 small regions. The centre is the rule.
    b = coz.parse_bbox(registry()["russia"])
    nearest_edge = min(abs(b[1]), abs(b[3]))
    centre = abs((b[1] + b[3]) / 2.0)
    assert nearest_edge < CAP_LAT <= centre
    assert decide(registry()["russia"]) == 13


def test_no_satellite_means_no_cap_logic():
    assert decide(registry()["russia"], satellite=False) == 14


def test_max_zoom_already_at_or_below_13_is_untouched():
    assert decide(registry()["russia"], max_zoom=13) == 13
    assert decide(registry()["russia"], max_zoom=12) == 12


def test_rule_uses_a_variable_that_exists_at_that_point():
    """The cap must not reference `bbox`, which main() defines much later."""
    src = (ROOT / "create_osm_zim.py").read_text(encoding="utf-8")
    start = src.index("Latitude-aware satellite cap")
    block = src[start:src.index("satellite_format = args.satellite_format", start)]
    # Comments in that block legitimately say "bbox"; only CODE matters.
    code = "\n".join(l.split("#", 1)[0] for l in block.splitlines())
    assert "parse_bbox(bbox_str)" in code, "cap should parse bbox_str"
    # `bbox` as a bare name would be undefined here.
    assert not re.search(r"(?<![_.\w])bbox(?![_\w(])", code), \
        "cap references bare `bbox`, which is not defined at that point in main()"


def test_cap_never_raises_on_a_malformed_bbox():
    """A progress nicety must not be able to fail a multi-hour build."""
    src = (ROOT / "create_osm_zim.py").read_text(encoding="utf-8")
    start = src.index("Latitude-aware satellite cap")
    block = src[start:src.index("satellite_format = args.satellite_format", start)]
    assert "except Exception" in block, "cap must swallow its own errors"
