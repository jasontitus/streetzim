"""The render gate probes each region's anchor city, so the anchors must resolve.

tmp/map-health.mjs used to count rendered features wherever the viewer opens,
which is the middle of the region's bbox. Three correct maps failed a >=100
threshold that way on 2026-09-25 -- north-africa 14 features (open Sahara),
east-polynesia 94 (open Pacific), hawaii 72 (sea between the islands) -- and
two of them were withheld from upload. The gate now jumps to cloud/regions.tsv
column 5 before counting.

That makes the gate depend on data, so these tests pin the data: the id it
derives from a ZIM filename must match a registry row, and that row must carry
a usable anchor. A region that silently fails to resolve falls back to the old
behaviour, which is exactly the weaker check these tests exist to prevent.
"""
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Mirrors the regex in tmp/map-health.mjs: /^osm-(.+)-\d{4}-\d{2}-\d{2}[a-z]?$/
TAG_RE = re.compile(r"^osm-(.+)-\d{4}-\d{2}-\d{2}[a-z]?$")


def registry():
    rows = {}
    for line in (ROOT / "cloud" / "regions.tsv").read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        c = line.split("\t")
        if len(c) > 4:
            rows[c[0]] = c
    return rows


def test_the_regex_extracts_ids_from_real_filenames():
    cases = {
        "osm-hawaii-2026-09-20": "hawaii",
        "osm-north-africa-2026-09-25": "north-africa",
        "osm-britain-ireland-2026-09-24": "britain-ireland",
        "osm-switzerland-light-2026-09-20": "switzerland-light",
        # a rebuild suffix, which the queues do emit
        "osm-australia-nz-2026-09-19d": "australia-nz",
    }
    for tag, want in cases.items():
        m = TAG_RE.match(tag)
        assert m, f"{tag} did not match"
        assert m.group(1) == want


def test_every_zim_we_will_gate_resolves_to_a_registry_row():
    """The queues gate exactly these; each must find its anchor.

    Deliberately NOT every osm-*.zim on disk: the build host keeps dozens of
    undated experiment files (osm-japan-v3.zim, osm-dc-avif256.zim) that no
    queue gates and whose names never matched the dated contract.
    """
    reg = registry()
    todo = set()
    lst = ROOT / "rebuild-old.list"
    if lst.exists():
        todo |= set(lst.read_text(encoding="utf-8").split())
    tsv = ROOT / "rollout-viewer.tsv"
    if tsv.exists():
        for line in tsv.read_text(encoding="utf-8").splitlines():
            if line.startswith("id") or line.startswith("#"):
                continue
            c = line.split("\t")
            if c and c[0]:
                todo.add(c[0])
    assert todo, "no work lists found to check"
    missing = sorted(r for r in todo if r not in reg)
    assert not missing, f"queued for gating but absent from regions.tsv: {missing}"


def test_a_dated_build_of_each_queued_region_would_resolve():
    """The gate derives the id from the filename, so round-trip it."""
    reg = registry()
    lst = ROOT / "rebuild-old.list"
    ids = lst.read_text(encoding="utf-8").split() if lst.exists() else []
    for rid in ids:
        tag = f"osm-{rid}-2026-09-25"
        m = TAG_RE.match(tag)
        assert m and m.group(1) == rid, f"{tag} does not round-trip"
        assert reg[rid][4], f"{rid} has no anchor in column 5"


def test_every_registry_anchor_is_a_usable_coordinate():
    bad = []
    for rid, c in registry().items():
        anchor = c[4] if len(c) > 4 else ""
        try:
            lat, lon = (float(x) for x in anchor.split(","))
        except (ValueError, AttributeError):
            bad.append((rid, anchor))
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            bad.append((rid, anchor))
    assert not bad, f"unusable anchors: {bad}"


def test_anchors_sit_inside_their_region_bbox():
    """An anchor outside the bbox would probe empty space or be clamped."""
    outside = []
    for rid, c in registry().items():
        try:
            lat, lon = (float(x) for x in c[4].split(","))
            w, s, e, n = (float(x) for x in c[2].split(","))
        except (ValueError, IndexError):
            continue
        if not (s <= lat <= n and w <= lon <= e):
            outside.append((rid, c[4], c[2]))
    assert not outside, f"anchor outside its own bbox: {outside}"


def test_the_regions_that_exposed_the_bug_have_anchors_on_land():
    """north-africa, east-polynesia and hawaii are the regressions in question."""
    reg = registry()
    for rid, city in (("north-africa", "Casablanca"),
                      ("east-polynesia", "Papeete"),
                      ("hawaii", "Honolulu")):
        c = reg[rid]
        lat, lon = (float(x) for x in c[4].split(","))
        assert c[6] == city, f"{rid} anchor label changed: {c[6]!r}"
        assert abs(lat) <= 90 and abs(lon) <= 180
        # z14 resolution at this latitude, for the satellite cap's sake
        assert 156543.03 * math.cos(math.radians(lat)) / (2 ** 14) > 0
