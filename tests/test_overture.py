#!/usr/bin/env python3
"""
Tests for the Overture Maps conflation path in `create_osm_zim.py`.

Covers three layers:

  1. `_normalize_street` — the string-folding helper every pass-2
     attribute key runs through. Idempotent, case-insensitive,
     Unicode-tolerant, applies the US street-suffix abbreviation
     table.
  2. `_STREET_ABBREV` — canary assertions on the abbreviation table
     itself so a typo doesn't silently break all pass-2 matches.
  3. `merge_overture_addresses` — end-to-end: build a minimal
     Overture parquet + a minimal OSM-feed JSONL, run the merge,
     assert on the pass-1 / pass-2 / appended outcomes.

Run with:
    pytest tests/test_overture.py
or directly:
    python3 -m pytest tests/test_overture.py -q

Requires duckdb (already in requirements.txt) and nothing else —
no real Overture parquet, no S3 access, no network.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile

import pytest

# Load `create_osm_zim.py` as a module so we can import private
# helpers (`_normalize_street`, `_STREET_ABBREV`) without having to
# turn the file into a package. Uses importlib so the filename's
# `.py` suffix is irrelevant.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "create_osm_zim", _REPO_ROOT / "create_osm_zim.py"
)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["create_osm_zim"] = _MOD
_SPEC.loader.exec_module(_MOD)

_normalize_street = _MOD._normalize_street
_STREET_ABBREV = _MOD._STREET_ABBREV
merge_overture_addresses = _MOD.merge_overture_addresses
merge_overture_places = _MOD.merge_overture_places


# ---------------------------------------------------------------------------
# _normalize_street
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("inp,expected", [
    # Empty / falsy input.
    ("", ""),
    (None, ""),

    # Basic suffix expansion.
    ("Ramona St", "ramona street"),
    ("Ramona Street", "ramona street"),
    ("Ramona St.", "ramona street"),
    ("ramona st", "ramona street"),

    # Alternate abbreviations for the same canonical.
    ("1600 Amphitheatre Pkwy", "1600 amphitheatre parkway"),
    ("1st Ave", "1st avenue"),
    ("1st Av", "1st avenue"),
    ("Main Blvd", "main boulevard"),
    ("Main Bl", "main boulevard"),

    # Directional prefixes.
    ("N Main St", "north main street"),
    ("SW 1st Ct", "southwest 1st court"),

    # Pre-normalized input is idempotent.
    ("north main street", "north main street"),
    ("ramona street", "ramona street"),

    # Punctuation variants collapse to spaces — `[a-z0-9]+` splits on
    # apostrophes and hyphens, which means "O'Reilly" becomes two
    # tokens. That's fine for pass-2 matching as long as BOTH sides
    # (Overture + OSM) get normalized the same way.
    ("Jean-Paul Ct.", "jean paul court"),
    ("O'Reilly Ave", "o reilly avenue"),

    # Unicode diacritics fold to ASCII.
    ("Café Lane", "cafe lane"),
    ("Niño Cir", "nino circle"),

    # ALL CAPS from OpenAddresses lowers correctly.
    ("RAMONA ST", "ramona street"),
])
def test_normalize_street(inp, expected):
    assert _normalize_street(inp) == expected


def test_normalize_street_is_idempotent():
    # Real-world identity: if we normalize, then normalize again, the
    # result shouldn't drift. This is what makes the pass-2 attr key
    # stable across repeated merges.
    samples = ["Ramona St", "1600 Amphitheatre Pkwy",
               "NE Martin Luther King Jr Blvd", "Café Lane",
               "1st Av"]
    for s in samples:
        once = _normalize_street(s)
        twice = _normalize_street(once)
        assert once == twice, f"drift on {s!r}: {once!r} → {twice!r}"


# ---------------------------------------------------------------------------
# _STREET_ABBREV — canary table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("abbrev,canonical", [
    ("st",   "street"),
    ("str",  "street"),
    ("ave",  "avenue"),
    ("av",   "avenue"),
    ("blvd", "boulevard"),
    ("bl",   "boulevard"),
    ("rd",   "road"),
    ("dr",   "drive"),
    ("ln",   "lane"),
    ("ct",   "court"),
    ("pl",   "place"),
    ("hwy",  "highway"),
    ("pkwy", "parkway"),
    ("cir",  "circle"),
    ("ter",  "terrace"),
    ("ctr",  "center"),
    ("sq",   "square"),
    ("mt",   "mount"),
    ("ft",   "fort"),
    # Directional abbreviations.
    ("n",    "north"),
    ("s",    "south"),
    ("e",    "east"),
    ("w",    "west"),
    ("ne",   "northeast"),
    ("nw",   "northwest"),
    ("se",   "southeast"),
    ("sw",   "southwest"),
])
def test_street_abbrev_table_covers_known_shortforms(abbrev, canonical):
    assert _STREET_ABBREV[abbrev] == canonical


def test_street_abbrev_table_has_no_shadowed_canonicals():
    # Guard against a typo that makes an abbreviation expand to
    # another abbreviation instead of the canonical long form.
    canonicals = set(_STREET_ABBREV.values())
    shortforms = set(_STREET_ABBREV.keys())
    collisions = canonicals & shortforms
    assert not collisions, (
        f"_STREET_ABBREV maps to a key that's also a shortform: "
        + f"{collisions}")


# ---------------------------------------------------------------------------
# merge_overture_addresses — end-to-end with a synthetic parquet.
# ---------------------------------------------------------------------------

@pytest.fixture
def duckdb_available():
    try:
        import duckdb  # noqa: F401
    except ImportError:
        pytest.skip("duckdb not installed; skipping end-to-end merge tests")
    return True


def _write_parquet(path: str, rows: list[dict]) -> None:
    """
    Build an Overture-shaped parquet from a list of plain dicts. We
    use DuckDB so the schema matches what `merge_overture_addresses`
    expects down to the nested `sources` list and the WKT point
    geometry; pyarrow schemas would drift from that shape too easily.
    """
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("""
        CREATE TABLE addr (
            number VARCHAR,
            street VARCHAR,
            postcode VARCHAR,
            address_levels STRUCT(value VARCHAR)[],
            sources STRUCT(dataset VARCHAR, record_id VARCHAR)[],
            wkt VARCHAR
        )
    """)
    for r in rows:
        levels_literal = (
            "[" + ", ".join(
                f"{{'value': '{lvl}'}}" for lvl in r.get("levels", [])
            ) + "]"
        )
        sources_literal = (
            "[" + ", ".join(
                f"{{'dataset': '{s[0]}', 'record_id': '{s[1]}'}}"
                for s in r.get("sources", [])
            ) + "]"
        )
        wkt = f"POINT ({r['lon']} {r['lat']})"
        con.execute(
            f"INSERT INTO addr VALUES ("
            f"  '{r['number']}', '{r['street']}', '{r.get('postcode', '')}', "
            f"  {levels_literal}, {sources_literal}, '{wkt}'"
            f")"
        )
    con.execute(f"COPY addr TO '{path}' (FORMAT PARQUET)")
    con.close()


def _write_jsonl(path: str, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n")


def test_merge_overture_adds_new_address_when_osm_has_none(
    duckdb_available, tmp_path
):
    parquet = tmp_path / "ov.parquet"
    _write_parquet(str(parquet), [{
        "number": "1600", "street": "Amphitheatre Pkwy",
        "lat": 37.422, "lon": -122.084,
        "levels": ["CA", "Mountain View"],
        "sources": [("Esri", "c-42")],     # NOT an OSM source
    }])
    jsonl = tmp_path / "feed.jsonl"
    _write_jsonl(str(jsonl), [])           # empty OSM side

    result = merge_overture_addresses(str(parquet), str(jsonl))
    added = result["added"] if isinstance(result, dict) else result
    assert added == 1

    # Last line is the appended Overture record.
    appended = [json.loads(l) for l in open(jsonl) if l.strip()]
    assert len(appended) == 1
    rec = appended[0]
    assert rec["type"] == "addr"
    assert rec["subtype"] == "overture"     # provenance marker
    assert rec["name"].startswith("1600 Amphitheatre Pkwy")
    assert "Mountain View" in rec["name"]   # city suffix
    assert rec["lat"] == pytest.approx(37.422, abs=1e-5)
    assert rec["lon"] == pytest.approx(-122.084, abs=1e-5)


def test_merge_overture_skips_when_sources_link_to_osm(
    duckdb_available, tmp_path
):
    # Pass-1 dedupe: any row with an OSM source entry is dropped
    # regardless of coord/attr match state.
    parquet = tmp_path / "ov.parquet"
    _write_parquet(str(parquet), [{
        "number": "1600", "street": "Amphitheatre Pkwy",
        "lat": 37.422, "lon": -122.084,
        "levels": [],
        "sources": [("OpenStreetMap", "n12345")],
    }])
    jsonl = tmp_path / "feed.jsonl"
    _write_jsonl(str(jsonl), [])

    result = merge_overture_addresses(str(parquet), str(jsonl))
    added = result["added"] if isinstance(result, dict) else result
    assert added == 0, "OSM-sourced rows must be skipped in pass 1"


def test_merge_overture_skips_when_coord_matches_existing_osm(
    duckdb_available, tmp_path
):
    # Pass-2 dedupe: same ~1 m coord as an OSM addr record → skip.
    parquet = tmp_path / "ov.parquet"
    _write_parquet(str(parquet), [{
        "number": "1600", "street": "Amphitheatre Pkwy",
        "lat": 37.42200, "lon": -122.08400,  # 5-decimal == same grid cell
        "levels": [], "sources": [("Esri", "c-42")],
    }])
    jsonl = tmp_path / "feed.jsonl"
    _write_jsonl(str(jsonl), [{
        "name": "1600 Amphitheatre Pkwy, Mountain View",
        "type": "addr",
        "lat": 37.42200, "lon": -122.08400,
    }])

    result = merge_overture_addresses(str(parquet), str(jsonl))
    added = result["added"] if isinstance(result, dict) else result
    assert added == 0


def test_merge_overture_skips_when_attr_matches_existing_osm(
    duckdb_available, tmp_path
):
    # Pass-2 dedupe: matching (number, normalized_street) even at a
    # DIFFERENT coordinate (OpenAddresses points off by 30 m from the
    # OSM geocode, common on rural roads).
    parquet = tmp_path / "ov.parquet"
    _write_parquet(str(parquet), [{
        "number": "1029", "street": "RAMONA ST",           # uppercase
        "lat": 37.44200, "lon": -122.16100,                # displaced 30 m
        "levels": [], "sources": [("Esri", "c-99")],
    }])
    jsonl = tmp_path / "feed.jsonl"
    _write_jsonl(str(jsonl), [{
        "name": "1029 Ramona Street, Palo Alto",           # expanded suffix
        "type": "addr",
        "lat": 37.44170, "lon": -122.16080,
    }])

    result = merge_overture_addresses(str(parquet), str(jsonl))
    added = result["added"] if isinstance(result, dict) else result
    assert added == 0, (
        "normalized-street attr key must collapse 'RAMONA ST' and "
        + "'Ramona Street' so pass-2 catches the dup")


def test_merge_overture_rejects_orphan_rows_missing_number_or_street(
    duckdb_available, tmp_path
):
    parquet = tmp_path / "ov.parquet"
    _write_parquet(str(parquet), [
        {"number": "",    "street": "A St", "lat": 1.0, "lon": 2.0,
         "levels": [], "sources": []},
        {"number": "100", "street": "",     "lat": 1.0, "lon": 2.0,
         "levels": [], "sources": []},
    ])
    jsonl = tmp_path / "feed.jsonl"
    _write_jsonl(str(jsonl), [])

    result = merge_overture_addresses(str(parquet), str(jsonl))
    added = result["added"] if isinstance(result, dict) else result
    assert added == 0


def test_merge_overture_respects_bbox_filter(duckdb_available, tmp_path):
    parquet = tmp_path / "ov.parquet"
    _write_parquet(str(parquet), [
        {"number": "1", "street": "In St",
         "lat": 37.42, "lon": -122.08,                     # inside
         "levels": [], "sources": []},
        {"number": "2", "street": "Out Ave",
         "lat": 40.00, "lon": -74.00,                      # far outside
         "levels": [], "sources": []},
    ])
    jsonl = tmp_path / "feed.jsonl"
    _write_jsonl(str(jsonl), [])

    # bbox order matches the rest of the codebase: (minlon, minlat, maxlon, maxlat)
    bbox = (-122.5, 37.3, -121.9, 37.5)
    result = merge_overture_addresses(str(parquet), str(jsonl), bbox=bbox)
    added = result["added"] if isinstance(result, dict) else result
    assert added == 1
    appended = [json.loads(l) for l in open(jsonl) if l.strip()]
    assert appended[0]["name"].startswith("1 In St")


def test_merge_overture_handles_empty_parquet(duckdb_available, tmp_path):
    parquet = tmp_path / "ov.parquet"
    _write_parquet(str(parquet), [])
    jsonl = tmp_path / "feed.jsonl"
    _write_jsonl(str(jsonl), [])
    result = merge_overture_addresses(str(parquet), str(jsonl))
    added = result["added"] if isinstance(result, dict) else result
    assert added == 0


def _write_places_parquet(path: str, rows: list[dict]) -> None:
    """Build an Overture-places-shaped parquet from plain dicts.

    Matches the columns `merge_overture_places` selects via DuckDB:
    `names.primary`, `categories.primary`, `phones[]`, `websites[]`,
    `socials[]`, `brand.names.primary` + `brand.wikidata`, `sources[]`,
    and a WKT point for the geometry.
    """
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    # `primary` is a DuckDB reserved keyword — quote the field name
    # both at CREATE TABLE time and in INSERT struct literals below
    # so the schema matches what Overture's real parquet exposes
    # (names.primary, categories.primary, brand.names.primary).
    con.execute("""
        CREATE TABLE places (
            names STRUCT("primary" VARCHAR),
            categories STRUCT("primary" VARCHAR),
            phones VARCHAR[],
            websites VARCHAR[],
            socials VARCHAR[],
            brand STRUCT(
                names STRUCT("primary" VARCHAR),
                wikidata VARCHAR
            ),
            sources STRUCT(dataset VARCHAR, record_id VARCHAR)[],
            wkt VARCHAR
        )
    """)
    def _lit_list(values: list[str]) -> str:
        return "[" + ", ".join(
            "'" + v.replace("'", "''") + "'" for v in values
        ) + "]"
    for r in rows:
        name = (r.get("name") or "").replace("'", "''")
        cat = (r.get("category") or "").replace("'", "''")
        brand_name = (r.get("brand") or "").replace("'", "''")
        brand_wd = (r.get("brand_wd") or "").replace("'", "''")
        sources_lit = (
            "[" + ", ".join(
                f"{{'dataset': '{s[0]}', 'record_id': '{s[1]}'}}"
                for s in r.get("sources", [])
            ) + "]"
        )
        wkt = f"POINT ({r['lon']} {r['lat']})"
        con.execute(
            "INSERT INTO places VALUES ("
            f"  {{\"primary\": '{name}'}}, "
            f"  {{\"primary\": '{cat}'}}, "
            f"  {_lit_list(r.get('phones', []))}, "
            f"  {_lit_list(r.get('websites', []))}, "
            f"  {_lit_list(r.get('socials', []))}, "
            f"  {{'names': {{\"primary\": '{brand_name}'}}, "
            f"   'wikidata': '{brand_wd}'}}, "
            f"  {sources_lit}, '{wkt}'"
            f")"
        )
    con.execute(f"COPY places TO '{path}' (FORMAT PARQUET)")
    con.close()


# ---------------------------------------------------------------------------
# merge_overture_places — POI enrichment + add-new.
# ---------------------------------------------------------------------------

def test_merge_overture_places_enriches_existing_poi(
    duckdb_available, tmp_path
):
    # OSM has the HP Garage as a POI but with OMT's noisy `tourism`
    # subtype. Overture knows the same place at the same coord with
    # `museum` category + website + phone. Pass-1 enrichment:
    # rewrite the subtype to the clean category, add the fields, do
    # NOT duplicate the record.
    parquet = tmp_path / "ov.parquet"
    _write_places_parquet(str(parquet), [{
        "name": "HP Garage",
        "category": "museum",
        "lat": 37.44453, "lon": -122.15269,
        "websites": ["https://www.hpgarage.com"],
        "phones": ["+16508574400"],
        "socials": [
            "https://www.facebook.com/hpgarage",
            "https://www.instagram.com/hpgarage",
        ],
        "brand": "HP", "brand_wd": "Q82525",
        "sources": [("meta", "a-1")],
    }])
    jsonl = tmp_path / "feed.jsonl"
    _write_jsonl(str(jsonl), [{
        "name": "HP Garage",
        "type": "poi",
        "subtype": "tourism",   # OMT's noisy bucket
        "lat": 37.44453,
        "lon": -122.15269,
    }])

    out = merge_overture_places(str(parquet), str(jsonl))
    assert out["enriched"] == 1
    assert out["added"] == 0

    rows = [json.loads(l) for l in open(jsonl) if l.strip()]
    assert len(rows) == 1, "enrich must not duplicate the record"
    rec = rows[0]
    assert rec["subtype"] == "museum", (
        "generic OMT bucket must be replaced by Overture's clean category")
    assert rec["cat"] == "museum"
    assert rec["w"] == "https://www.hpgarage.com"
    assert rec["p"] == "+16508574400"
    assert rec["soc"] == [
        "https://www.facebook.com/hpgarage",
        "https://www.instagram.com/hpgarage",
    ]
    assert rec["brand"] == "HP"
    assert rec["wd"] == "Q82525"


def test_merge_overture_places_keeps_specific_subtype_over_category(
    duckdb_available, tmp_path
):
    # When OSM already has a SPECIFIC subtype (not a generic bucket),
    # Overture's category enrichment should NOT overwrite it. Only the
    # six noisy buckets (tourism/amenity/shop/attraction/leisure/etc.)
    # get rewritten.
    parquet = tmp_path / "ov.parquet"
    _write_places_parquet(str(parquet), [{
        "name": "Joe's Pizzeria",
        "category": "pizza_restaurant",
        "lat": 37.50, "lon": -122.20,
        "websites": ["https://joes.example"],
    }])
    jsonl = tmp_path / "feed.jsonl"
    _write_jsonl(str(jsonl), [{
        "name": "Joe's Pizzeria",
        "type": "poi",
        "subtype": "restaurant",        # already specific; don't replace
        "lat": 37.50, "lon": -122.20,
    }])
    merge_overture_places(str(parquet), str(jsonl))
    rec = [json.loads(l) for l in open(jsonl) if l.strip()][0]
    assert rec["subtype"] == "restaurant", (
        "specific OSM subtype must survive Overture enrichment")
    assert rec["cat"] == "pizza_restaurant", (
        "Overture's category still lands in `cat` for display/filtering")


def test_merge_overture_places_adds_new_poi(duckdb_available, tmp_path):
    # Overture knows about a place OSM doesn't — e.g. a new restaurant
    # in the middle of nowhere. Pass-2 creates a fresh POI record
    # tagged `source="overture"` so downstream consumers can see
    # provenance.
    parquet = tmp_path / "ov.parquet"
    _write_places_parquet(str(parquet), [{
        "name": "Mystery Ramen House",
        "category": "ramen_restaurant",
        "lat": 37.77, "lon": -122.42,
        "websites": ["https://mystery.example"],
    }])
    jsonl = tmp_path / "feed.jsonl"
    _write_jsonl(str(jsonl), [])
    out = merge_overture_places(str(parquet), str(jsonl))
    assert out["added"] == 1
    assert out["enriched"] == 0
    rec = [json.loads(l) for l in open(jsonl) if l.strip()][0]
    assert rec["source"] == "overture"
    assert rec["subtype"] == "ramen_restaurant"
    assert rec["cat"] == "ramen_restaurant"
    assert rec["w"] == "https://mystery.example"


def test_merge_overture_places_skips_unnamed(duckdb_available, tmp_path):
    # Overture rows without a primary name are useless for our
    # chip-driven search UI — drop them silently.
    parquet = tmp_path / "ov.parquet"
    _write_places_parquet(str(parquet), [{
        "name": "",
        "category": "restaurant",
        "lat": 37.5, "lon": -122.5,
    }])
    jsonl = tmp_path / "feed.jsonl"
    _write_jsonl(str(jsonl), [])
    out = merge_overture_places(str(parquet), str(jsonl))
    assert out["added"] == 0
    assert out["enriched"] == 0


def test_merge_overture_places_skips_uncategorized_new_poi(
    duckdb_available, tmp_path
):
    # A new Overture row with no `category.primary` is noise — we
    # wouldn't know which chip to put it under. Skip.
    parquet = tmp_path / "ov.parquet"
    _write_places_parquet(str(parquet), [{
        "name": "Unknown Thing",
        "category": "",
        "lat": 37.5, "lon": -122.5,
    }])
    jsonl = tmp_path / "feed.jsonl"
    _write_jsonl(str(jsonl), [])
    out = merge_overture_places(str(parquet), str(jsonl))
    assert out["added"] == 0


def test_merge_overture_places_drops_empty_enrichment_fields(
    duckdb_available, tmp_path
):
    # When Overture doesn't know a website / phone / socials for a
    # row, those keys MUST NOT land on the record (empty strings or
    # empty arrays would bloat every search-data chunk).
    parquet = tmp_path / "ov.parquet"
    _write_places_parquet(str(parquet), [{
        "name": "Plain POI",
        "category": "cafe",
        "lat": 37.5, "lon": -122.5,
        # all enrichment fields intentionally absent
    }])
    jsonl = tmp_path / "feed.jsonl"
    _write_jsonl(str(jsonl), [])
    merge_overture_places(str(parquet), str(jsonl))
    rec = [json.loads(l) for l in open(jsonl) if l.strip()][0]
    # `cat` is always present when category is known.
    assert rec.get("cat") == "cafe"
    for empty_key in ("w", "p", "soc", "brand", "wd"):
        assert empty_key not in rec, (
            f"empty enrichment field '{empty_key}' leaked onto record")


def test_merge_overture_places_preserves_non_poi_rows(
    duckdb_available, tmp_path
):
    # Regression guard: addresses + non-POI records (cities,
    # airports, streets) must survive the merge byte-identical —
    # they're filtered by `type != 'poi'` and passed through.
    parquet = tmp_path / "ov.parquet"
    _write_places_parquet(str(parquet), [{
        "name": "New Cafe",
        "category": "cafe",
        "lat": 37.5, "lon": -122.5,
    }])
    jsonl = tmp_path / "feed.jsonl"
    survivors = [
        {"name": "123 Castro St", "type": "addr", "lat": 37.4, "lon": -122.1},
        {"name": "Palo Alto", "type": "place", "subtype": "city",
         "lat": 37.44, "lon": -122.15},
        {"name": "Castro St", "type": "street", "lat": 37.4, "lon": -122.1},
    ]
    _write_jsonl(str(jsonl), survivors)
    merge_overture_places(str(parquet), str(jsonl))
    rows = [json.loads(l) for l in open(jsonl) if l.strip()]
    # Survivors come first, added POI last.
    for i, expected in enumerate(survivors):
        actual = rows[i]
        assert actual["name"] == expected["name"]
        assert actual["type"] == expected["type"]
    assert rows[-1]["source"] == "overture"


def test_merge_overture_places_does_not_overwrite_existing_enrichment(
    duckdb_available, tmp_path
):
    # If OSM already carried `wd` (wikidata Q-ID) from the wiki
    # cross-ref pass, Overture's brand.wikidata must NOT clobber it —
    # OSM's wiki is entity-level, Overture's is brand-level (often
    # different Q-IDs).
    parquet = tmp_path / "ov.parquet"
    _write_places_parquet(str(parquet), [{
        "name": "Stanford Museum",
        "category": "museum",
        "lat": 37.43, "lon": -122.17,
        "brand_wd": "Q99999",  # Overture's brand Q-ID (random)
    }])
    jsonl = tmp_path / "feed.jsonl"
    _write_jsonl(str(jsonl), [{
        "name": "Stanford Museum",
        "type": "poi",
        "subtype": "museum",
        "wd": "Q5035378",                   # OSM's entity Q-ID
        "lat": 37.43, "lon": -122.17,
    }])
    merge_overture_places(str(parquet), str(jsonl))
    rec = [json.loads(l) for l in open(jsonl) if l.strip()][0]
    assert rec["wd"] == "Q5035378", (
        "OSM entity Q-ID must win over Overture brand Q-ID")


def test_merge_overture_preserves_existing_feed_rows(duckdb_available, tmp_path):
    # Regression guard: we append to the JSONL — never rewrite. OSM
    # records must survive every merge pass byte-for-byte.
    parquet = tmp_path / "ov.parquet"
    _write_parquet(str(parquet), [{
        "number": "1600", "street": "Amphitheatre Pkwy",
        "lat": 37.422, "lon": -122.084,
        "levels": ["CA", "Mountain View"], "sources": [],
    }])
    jsonl = tmp_path / "feed.jsonl"
    original = [
        {"name": "500 Castro St, Mountain View", "type": "addr",
         "lat": 37.396, "lon": -122.079},
        {"name": "Computer History Museum", "type": "poi", "subtype": "museum",
         "lat": 37.414, "lon": -122.077},
    ]
    _write_jsonl(str(jsonl), original)
    before = pathlib.Path(jsonl).read_text(encoding="utf-8")

    result = merge_overture_addresses(str(parquet), str(jsonl))
    added = result["added"] if isinstance(result, dict) else result
    assert added == 1

    after = pathlib.Path(jsonl).read_text(encoding="utf-8")
    # Original lines must still appear verbatim as the file prefix.
    assert after.startswith(before), "merge must not rewrite existing rows"
