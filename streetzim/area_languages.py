"""The region's own languages of a `streetzim --multilingual` build that
gives no --region-languages (docs/zimfarm.md, "Multilingual maps").

1. A StreetZim region: an --area preset of that name, an --area preset
   whose box is a region's box in cloud/regions.tsv, Geofabrik extracts
   that are all of a region's outline in cloud/region-outlines.tsv (every
   part of it, nothing else), or, for a region without an outline row
   there, a single extract named like it (europe/switzerland,
   north-america/us/alaska). Its row of cloud/region-languages.tsv, the
   table StreetZim's own builds use.
2. Otherwise the Geofabrik extracts behind the area, when every one has a
   row of resources/geofabrik-languages.tsv
   (tools/gen_geofabrik_languages.py): a country's official and de facto
   official languages; a part of a country only its country's first
   language, and then none of them become label languages (a part's own
   languages differ from its country's; the log asks for
   --region-languages).
3. Otherwise none: a --bbox, an --include-poly with any polygon that is not
   a Geofabrik extract (the log asks for --region-languages), a continent.

Stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from streetzim import languages
from streetzim.paths import REPO_ROOT, RESOURCES_DIR

REGION_TABLE = REPO_ROOT / "cloud" / "region-languages.tsv"
OUTLINE_TABLE = REPO_ROOT / "cloud" / "region-outlines.tsv"
REGISTRY_TABLE = REPO_ROOT / "cloud" / "regions.tsv"
GEOFABRIK_TABLE = RESOURCES_DIR / "geofabrik-languages.tsv"
# The License metadata's credit when resources/geofabrik-languages.tsv
# decided the languages.
GEOFABRIK_CREDIT = ("GeoNames (geonames.org, CC BY 4.0) and Unicode CLDR "
                    "(unicode.org/license.txt), modified")
ASK = "pass --region-languages with the area's own languages"


@dataclass
class Derived:
    languages: list[str]
    where: str = ""                 # for the log; "" when nothing knew them
    labels: bool = True             # the region's own may become label languages
    credit: str | None = None       # for the License metadata
    hint: str | None = None         # logged with the languages


def _rows(path: Path) -> list[list[str]]:
    """A tab-separated table's rows; [] when the file is missing (a wheel
    without cloud/)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [line.split("\t") for line in text.splitlines()
            if line and not line.startswith("#") and "\t" in line]


def _box(text: str) -> tuple[float, ...] | None:
    try:
        box = tuple(round(float(v), 6) for v in text.split(","))
    except ValueError:
        return None
    return box if len(box) == 4 else None


def streetzim_region(geofabrik: list[str], *, region_table: Path | None = None,
                     outline_table: Path | None = None) -> str | None:
    """The StreetZim region whose whole outline is exactly these Geofabrik
    extracts; else, for a single extract, the region without an outline
    row that is named like it."""
    regions = {r[0] for r in _rows(region_table or REGION_TABLE)}
    want = set(geofabrik)
    if not want:
        return None
    outlines = _rows(outline_table or OUTLINE_TABLE)
    for row in outlines:
        if row[0] in regions and set(row[1].strip().split("+")) == want:
            return row[0]
    if len(geofabrik) == 1:
        leaf = geofabrik[0].rsplit("/", 1)[-1]
        if leaf in regions and leaf not in {row[0] for row in outlines}:
            return leaf
    return None


def region_of_box(bbox: str | None, *, region_table: Path | None = None,
                  registry_table: Path | None = None) -> str | None:
    """The StreetZim region whose registry box is `bbox`."""
    box = _box(bbox) if bbox else None
    if box is None:
        return None
    regions = {r[0] for r in _rows(region_table or REGION_TABLE)}
    for row in _rows(registry_table or REGISTRY_TABLE):
        if len(row) > 2 and row[0] in regions and _box(row[2]) == box:
            return row[0]
    return None


def derive(area: str | None, geofabrik: list[str] | None, *, bbox: str | None = None,
           region_table: Path | None = None, outline_table: Path | None = None,
           registry_table: Path | None = None,
           geofabrik_table: Path | None = None) -> Derived:
    """The languages for an --area preset (its name and box) and the
    Geofabrik extract paths behind the area ("europe/switzerland"); None
    for an area with a polygon from elsewhere."""
    table = {r[0]: [x for x in r[-1].split(",") if x]
             for r in _rows(region_table or REGION_TABLE)}

    def region(rid: str, how: str) -> Derived:
        return Derived(languages.region_languages(",".join(table[rid])),
                       f"StreetZim region {rid}{how}; cloud/region-languages.tsv")

    if area and table.get(area):
        return region(area, "")
    rid = region_of_box(bbox, region_table=region_table, registry_table=registry_table)
    if area and rid:
        return region(rid, f", the box of --area {area}")
    if geofabrik is None:
        return Derived([], "--include-poly has a polygon that is not a Geofabrik extract",
                       hint=ASK)
    rid = streetzim_region(geofabrik, region_table=region_table, outline_table=outline_table)
    if rid:
        how = "the outline" if len(geofabrik) > 1 or rid != geofabrik[0].rsplit("/", 1)[-1] \
            else "the extract"
        return region(rid, f", {how} {'+'.join(geofabrik)}")
    extracts = {r[0]: r for r in _rows(geofabrik_table or GEOFABRIK_TABLE) if len(r) >= 4}
    if not geofabrik or any(p not in extracts for p in geofabrik):
        unknown = [p for p in geofabrik if p not in extracts]
        return Derived([], f"no languages known for {', '.join(unknown)}" if unknown else "")
    found = [extracts[p] for p in geofabrik]
    merged: list[str] = []
    for row in found:
        merged += [x for x in row[3].split(",") if x and x not in merged]
    parts = [row[0] for row in found if row[1] == "part"]
    what = ("the official languages of" if not parts else
            "the first language of the country of" if len(parts) == len(found) else
            "the languages of")
    return Derived(languages.region_languages(",".join(merged)),
                   f"{what} the Geofabrik extract{'s' if len(found) > 1 else ''} "
                   f"{', '.join(row[0] for row in found)}; resources/geofabrik-languages.tsv",
                   labels=not parts, credit=GEOFABRIK_CREDIT,
                   hint=(f"{', '.join(parts)} "
                         + ("are parts of countries" if len(parts) > 1 else "is part of a country")
                         + f": only the country's first language is recorded and none labels "
                         f"the map; {ASK}") if parts else None)
