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

    added = merge_overture_addresses(str(parquet), str(jsonl))
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

    added = merge_overture_addresses(str(parquet), str(jsonl))
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

    added = merge_overture_addresses(str(parquet), str(jsonl))
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

    added = merge_overture_addresses(str(parquet), str(jsonl))
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

    added = merge_overture_addresses(str(parquet), str(jsonl))
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
    added = merge_overture_addresses(str(parquet), str(jsonl), bbox=bbox)
    assert added == 1
    appended = [json.loads(l) for l in open(jsonl) if l.strip()]
    assert appended[0]["name"].startswith("1 In St")


def test_merge_overture_handles_empty_parquet(duckdb_available, tmp_path):
    parquet = tmp_path / "ov.parquet"
    _write_parquet(str(parquet), [])
    jsonl = tmp_path / "feed.jsonl"
    _write_jsonl(str(jsonl), [])
    added = merge_overture_addresses(str(parquet), str(jsonl))
    assert added == 0


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

    added = merge_overture_addresses(str(parquet), str(jsonl))
    assert added == 1

    after = pathlib.Path(jsonl).read_text(encoding="utf-8")
    # Original lines must still appear verbatim as the file prefix.
    assert after.startswith(before), "merge must not rewrite existing rows"
