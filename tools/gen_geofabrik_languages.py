#!/usr/bin/env python3
"""Generate resources/geofabrik-languages.tsv: the languages of each
Geofabrik extract, which `streetzim --multilingual` records as the region's
languages when a recipe gives none and the area is no StreetZim region
(streetzim/area_languages.py).

    python tools/gen_geofabrik_languages.py           # download the sources, rewrite the table
    python tools/gen_geofabrik_languages.py --index I --territories T --countries C   # local copies

Sources:
- Geofabrik's extract index (INDEX_URL): each extract's path and ISO 3166
  codes. An extract with its own iso3166-1:alpha2 codes is a country (or
  several); otherwise it is part of a country: the one its iso3166-2 code
  names, else its nearest parent's. OVERRIDES gives the codes of extracts
  the index gets wrong or leaves without any. Continents (extracts without
  a parent) are left out.
- Unicode CLDR's territory data (TERRITORIES_URL, Unicode License v3,
  resources/LICENSES/Unicode-3.0.txt): each country's official and de facto
  official languages (cloud/region-languages.tsv's rule a), the most spoken
  first; script subtags dropped, codes as StreetZim's tables write them
  (CLDR_CODES). COUNTRY_LANGUAGES gives the languages of countries where
  that rule misses one of the country's own or names a neighbour's.
- GeoNames' country table (COUNTRIES_URL, CC BY 4.0): a country's most
  spoken language, for a country CLDR gives no official language.

A country extract lists its countries' languages: one country's in its
order, the countries of an OVERRIDES entry by speakers summed over them
(population times share), others' in the index's order of countries; a
part of a country
lists only its country's first, since a part's own languages differ from
its country's (Andalucía, Quebec). Codes as
streetzim.languages.region_languages gives them (three-letter codes with a
two-letter one become it).

The generator stops when two extracts carry the same alpha-2 code in the
index and neither has an override, and when an extract other than a
continent ends without a row and has no override saying so.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TARGET = ROOT / "resources" / "geofabrik-languages.tsv"
INDEX_URL = "https://download.geofabrik.de/index-v1-nogeom.json"
CLDR_VERSION = "47.0.0"
TERRITORIES_URL = ("https://raw.githubusercontent.com/unicode-org/cldr-json/"
                   f"{CLDR_VERSION}/cldr-json/cldr-core/supplemental/territoryInfo.json")
COUNTRIES_URL = "https://download.geonames.org/export/dump/countryInfo.txt"
CLDR_LICENSE = "https://www.unicode.org/license.txt"
GEONAMES_LICENSE = "https://creativecommons.org/licenses/by/4.0/"
PBF = re.compile(r"^https://download\.geofabrik\.de/(.+)-latest\.osm\.pbf$")
LICENCE_FILE = "resources/LICENSES/Unicode-3.0.txt"
CLDR_STATUSES = ("official", "de_facto_official")
# Countries whose languages are not their official and de facto official
# ones in CLDR, in order: South Africa's most spoken official languages
# (CLDR lists them as regional), Iraq's Kurdish, North Macedonia's
# Albanian; Ukraine without Russian (CLDR: de facto official).
COUNTRY_LANGUAGES: dict[str, list[str]] = {
    "ZA": ["en", "zu", "xh", "af"],
    "IQ": ["ar", "ku"],
    "MK": ["mk", "sq"],
    "UA": ["uk"],
}

# Extracts whose codes in the index are wrong or missing, by Geofabrik id:
# (kind, countries), or None for one that spans parts of several countries
# and gets no row.
OVERRIDES: dict[str, tuple[str, list[str]] | None] = {
    "polynesie-francaise": ("country", ["PF"]),
    "american-oceania": ("country", ["AS", "GU", "MP"]),
    "tokelau": ("country", ["TK"]),
    "wallis-et-futuna": ("country", ["WF"]),
    "ile-de-clipperton": ("part", ["FR"]),
    "pitcairn-islands": ("country", ["PN"]),
    "malaysia-singapore-brunei": ("country", ["MY", "SG", "BN"]),
    "kosovo": ("country", ["XK"]),
    "great-britain": ("country", ["GB"]),
    "comores": ("country", ["KM"]),
    "canary-islands": ("part", ["ES"]),
    "azores": ("part", ["PT"]),
    "isle-of-man": ("country", ["IM"]),
    "guernsey-jersey": ("country", ["GG", "JE"]),
    "south-africa-and-lesotho": ("country", ["ZA", "LS"]),
    "dach": ("country", ["DE", "AT", "CH"]),
    "britain-and-ireland": ("country", ["GB", "IE", "IM"]),
    "us-midwest": ("part", ["US"]),
    "us-northeast": ("part", ["US"]),
    "us-pacific": ("part", ["US"]),
    "us-south": ("part", ["US"]),
    "us-west": ("part", ["US"]),
    "alps": None,
    "sea": None,
}

# CLDR codes StreetZim's tables write otherwise (cloud/region-languages.tsv:
# a macrolanguage counts as one, Morocco's Tamazight as zgh, Swiss German
# as the German it is classed under).
CLDR_CODES = {"nb": "no", "nn": "no", "fil": "tl", "tzm": "zgh", "shi": "zgh", "rif": "zgh",
              "gsw": "de", "yue": "zh", "hak": "zh", "nan": "zh", "cmn": "zh", "wuu": "zh",
              "ckb": "ku", "kmr": "ku", "zsm": "ms", "swh": "sw"}

from streetzim.languages import region_languages  # noqa: E402

Row = tuple[str, str, list[str], list[str]]


def _codes(raw: list[str]) -> list[str]:
    """Language tags as region_languages gives them: the language subtag,
    lowercase, as StreetZim writes it (CLDR_CODES), two or three letters,
    once each, in order."""
    out = [t.replace("-", "_").split("_")[0].strip().lower() for t in raw]
    out = [CLDR_CODES.get(c, c) for c in out if re.fullmatch(r"[a-z]{2,3}", c)]
    return region_languages(",".join(out)) if out else []


Spoken = dict[str, tuple[float, list[tuple[str, float]]]]


def spoken_languages(territory_info: dict,
                     country_languages: dict[str, list[str]] | None = None) -> Spoken:
    """CLDR territoryInfo.json: ISO 3166-1 alpha-2 -> (population, [(language,
    percent of its people)]) for its official and de facto official
    languages, the most spoken first, or COUNTRY_LANGUAGES' in their order;
    codes as _codes gives them (two tags that become one code keep the
    larger share)."""
    fixed = COUNTRY_LANGUAGES if country_languages is None else country_languages
    out: Spoken = {}
    for code, info in territory_info["supplemental"]["territoryInfo"].items():
        shares: dict[str, float] = {}
        for tag, v in (info.get("languagePopulation") or {}).items():
            share = float(v.get("_populationPercent") or 0)
            if code in fixed or v.get("_officialStatus") in CLDR_STATUSES:
                for lang in _codes([tag]):
                    shares[lang] = max(shares.get(lang, 0.0), share)
        population = float(info.get("_population") or 0)
        if code in fixed:
            out[code] = (population, [(lang, shares.get(lang, 0.0)) for lang in fixed[code]])
        else:
            out[code] = (population, sorted(shares.items(), key=lambda x: -x[1]))
    return out


def first_languages(country_info: str) -> dict[str, str]:
    """GeoNames countryInfo.txt: ISO 3166-1 alpha-2 -> its most spoken
    language; countries with none left out."""
    out: dict[str, str] = {}
    for line in country_info.splitlines():
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 16:
            continue
        codes = _codes(cols[15].split(","))
        if codes:
            out[cols[0]] = codes[0]
    return out


def rows(index: dict, spoken: Spoken, first: dict[str, str],
         overrides: dict[str, tuple[str, list[str]] | None] | None = None) -> list[Row]:
    """(extract path, "country" or "part", its countries, its languages)
    for every extract with languages, by path. ValueError for two extracts
    with the same alpha-2 code in the index and no override, and for an
    extract other than a continent left without a row and no override."""
    overrides = OVERRIDES if overrides is None else overrides
    props = {f["properties"]["id"]: f["properties"] for f in index["features"]}
    holders: dict[str, list[str]] = {}
    for p in props.values():
        if p["id"] not in overrides:
            for c in p.get("iso3166-1:alpha2") or []:
                holders.setdefault(c.upper(), []).append(p["id"])
    shared = {c: ids for c, ids in holders.items() if len(ids) > 1}
    if shared:
        raise ValueError("extracts sharing an alpha-2 code (give them OVERRIDES): "
                         + "; ".join(f"{c}: {', '.join(sorted(ids))}"
                                     for c, ids in sorted(shared.items())))

    def speakers(code: str) -> list[tuple[str, float]]:
        """A country's languages and their speakers, the most spoken first:
        CLDR's, else GeoNames' most spoken language."""
        population, shares = spoken.get(code, (0.0, []))
        if shares:
            return [(lang, population * share / 100) for lang, share in shares]
        return [(first[code], population)] if code in first else []

    def own_countries(p: dict) -> list[str]:
        return list(dict.fromkeys(c.upper() for c in p.get("iso3166-1:alpha2") or []))

    def part_of(p: dict | None) -> str | None:
        """The country of an extract without its own alpha-2 codes: its
        iso3166-2 code's, else its nearest parent's."""
        seen: set[str] = set()
        while p is not None and p["id"] not in seen:
            seen.add(p["id"])
            if p.get("iso3166-2"):
                return p["iso3166-2"][0].split("-")[0].upper()
            if own_countries(p):
                return own_countries(p)[0]
            p = props.get(p.get("parent") or "")
        return None

    out: list[Row] = []
    missing: list[str] = []
    for p in props.values():
        m = PBF.match((p.get("urls") or {}).get("pbf") or "")
        if not m:
            continue
        if p["id"] in overrides:
            fixed = overrides[p["id"]]
            if fixed is None:
                continue
            kind, countries = fixed
        elif own_countries(p):
            kind, countries = "country", own_countries(p)
        else:
            country = part_of(p)
            kind, countries = "part", [country] if country else []
        if kind == "country":
            total: dict[str, float] = {}
            for c in countries:
                for lang, n in speakers(c):
                    total[lang] = total.get(lang, 0.0) + n
            langs = list(total)
            if len(countries) > 1 and p["id"] in overrides:
                langs = [lang for lang, _ in sorted(total.items(), key=lambda x: -x[1])]
        else:
            langs = [lang for lang, _ in speakers(countries[0])[:1]] if countries else []
        if langs:
            out.append((m.group(1), kind, countries, langs))
        elif p.get("parent"):
            missing.append(p["id"])
    if missing:
        raise ValueError("extracts without languages (give them OVERRIDES, None for no row): "
                         + ", ".join(sorted(missing)))
    return sorted(out)


def render(table: list[Row], today: str) -> str:
    head = [
        "# Region languages of each Geofabrik extract, for `streetzim --multilingual`",
        "# without --region-languages, when the area is no StreetZim region",
        "# (streetzim/area_languages.py).",
        f"# Generated by tools/gen_geofabrik_languages.py on {today} from:",
        f"#   {INDEX_URL} (Geofabrik's extract index: the extracts' ISO 3166 codes)",
        f"#   {TERRITORIES_URL}",
        f"#     (Unicode CLDR {CLDR_VERSION}: official and de facto official languages;",
        f"#     Unicode License v3, {CLDR_LICENSE}, its notice in {LICENCE_FILE})",
        f"#   {COUNTRIES_URL}",
        f"#     (GeoNames, CC BY 4.0, {GEONAMES_LICENSE}: a country's most spoken language)",
        "# Modified: those languages only (else the most spoken one), some",
        "# countries' languages and some extracts' countries corrected, subtags",
        "# dropped, codes as StreetZim writes them, a corrected group of countries'",
        "# languages ordered by speakers, a part of a country given only its",
        "# country's first language.",
        "# Columns (tab-separated): extract path (download.geofabrik.de/<path>.poly),",
        "# kind (country | part), ISO 3166-1 countries, languages (ISO 639).",
    ]
    return "\n".join(head + [f"{p}\t{k}\t{','.join(c)}\t{','.join(ls)}"
                             for p, k, c, ls in table]) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--index", help="a local copy of index-v1-nogeom.json")
    ap.add_argument("--territories", help="a local copy of CLDR's territoryInfo.json")
    ap.add_argument("--countries", help="a local copy of GeoNames' countryInfo.txt")
    ap.add_argument("-o", "--output", default=str(TARGET))
    a = ap.parse_args(argv)

    def fetch(local: str | None, url: str) -> str:
        if local:
            return Path(local).read_text(encoding="utf-8")
        with urllib.request.urlopen(url, timeout=120) as r:
            return r.read().decode("utf-8")

    table = rows(json.loads(fetch(a.index, INDEX_URL)),
                 spoken_languages(json.loads(fetch(a.territories, TERRITORIES_URL))),
                 first_languages(fetch(a.countries, COUNTRIES_URL)))
    Path(a.output).write_text(render(table, datetime.date.today().isoformat()), encoding="utf-8")
    print(f"wrote {a.output}: {len(table)} extracts", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
