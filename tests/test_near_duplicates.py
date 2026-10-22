"""Near-duplicate stops and water features merged at search extraction
(search_extract.merge_near_duplicates): a region's own tiles carry a
station's every bay and stop position as a POI of the same name metres
apart (Switzerland's "Luzern Bahnhof" 22 times, bus stops 82,846 against
53,039 in the world-tile cache). Within 100 m, features of one transit POI
kind or of water with the same name are one, at the member nearest their
mean; places, peaks and parks are never merged."""
from __future__ import annotations

import gzip
import json
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streetzim import search_extract as se  # noqa: E402

M = 1 / 111_320.0          # a metre of latitude, in degrees


def stop(name, lat, lon, sub="bus", t="poi"):
    return {"name": name, "type": t, "subtype": sub, "lat": lat, "lon": lon}


def kept(feats):
    return se.merge_near_duplicate_features(list(feats))


def test_a_stations_bays_are_one_at_the_most_central():
    bays = [stop("Luzern Bahnhof", 47.0507, 8.3100 + i * 8 * M / 0.68) for i in range(5)]
    [one] = kept(bays)
    assert one is bays[2]                       # the middle one


def test_far_apart_or_different_stays():
    feats = [stop("Bahnhof", 47.0, 8.0), stop("Bahnhof", 47.0 + 150 * M, 8.0),   # 150 m apart
             stop("Post", 47.0, 8.0),                                           # another name
             stop("Bahnhof", 47.0, 8.0, sub="railway"),                         # another kind
             stop("Bahnhof", 47.0 + 5 * M, 8.0, t="place", sub="locality"),     # a place
             stop("Bahnhof", 47.0 + 6 * M, 8.0, t="place", sub="locality")]
    assert kept(feats) == feats


def test_places_peaks_parks_and_other_pois_are_never_merged():
    feats = [stop(n, 46.5 + i * 2 * M, 9.0, t=t, sub=s)
             for n, t, s in (("Piz Buin", "peak", "peak"), ("Dorf", "place", "village"),
                             ("Park", "park", "park"), ("Migros", "poi", "shop"))
             for i in range(2)]
    assert kept(feats) == feats


def test_water_of_one_name_within_100_m_is_one():
    feats = [stop("Rotsee", 47.07, 8.31, t="water", sub="lake"),
             stop("Rotsee", 47.07 + 40 * M, 8.31, t="water", sub="lake"),
             stop("Rotsee", 47.07 + 400 * M, 8.31, t="water", sub="lake")]
    assert len(kept(feats)) == 2


def test_the_case_of_a_name_does_not_part_it():
    feats = [stop("Dorfbach", 47.0, 8.0, t="water", sub="stream"),
             stop("dorfbach", 47.0 + 3 * M, 8.0, t="water", sub="stream")]
    assert len(kept(feats)) == 1


def test_longitude_metres_shrink_with_latitude():
    # 0.0013° of longitude is ~88 m at 47°N (merged), ~145 m at the equator (not).
    assert len(kept([stop("X", 47.0, 8.0), stop("X", 47.0, 8.0013)])) == 1
    assert len(kept([stop("X", 0.0, 8.0), stop("X", 0.0, 8.0013)])) == 2


def test_the_result_does_not_depend_on_the_order():
    bays = [stop("Bern Bahnhof", 46.9489 + (i % 3) * 7 * M, 7.4391 + (i // 3) * 9 * M / 0.68)
            for i in range(9)]
    a = kept(bays)
    b = kept(list(reversed(bays)))
    assert [(f["lat"], f["lon"]) for f in a] == [(f["lat"], f["lon"]) for f in b]


def test_the_file_version_and_the_switch(tmp_path, monkeypatch):
    feats = [stop("Luzern Bahnhof", 47.0507, 8.31 + i * 5 * M) for i in range(3)]
    feats.append(stop("Luzern", 47.05, 8.30, t="place", sub="city"))
    p = tmp_path / "f.jsonl"
    p.write_text("".join(json.dumps(f) + "\n" for f in feats), encoding="utf-8")
    monkeypatch.setenv("STREETZIM_NEAR_DUP", "0")
    assert se.merge_near_duplicates(str(p)) == 0 and len(p.read_text().splitlines()) == 4
    monkeypatch.delenv("STREETZIM_NEAR_DUP")
    assert se.merge_near_duplicates(str(p)) == 2
    lines = [json.loads(x) for x in p.read_text().splitlines()]
    assert [f["name"] for f in lines] == ["Luzern Bahnhof", "Luzern"]
    assert lines[0]["lon"] == feats[1]["lon"]


mvt = pytest.importorskip("mapbox_vector_tile")


def test_extraction_merges_them(tmp_path):
    """Through extract_searchable_features (the streaming path every real
    caller takes): bays metres apart are one; a stop 250 m away stays."""
    def pt(x, y, name, cls, layer="poi"):
        return layer, {"geometry": f"POINT({x} {y})", "properties": {"name": name, "class": cls}}
    feats = [pt(2000 + 12 * i, 2000, "Luzern Bahnhof", "bus") for i in range(4)]
    feats += [pt(2400, 2000, "Luzern Bahnhof", "bus"), pt(1000, 1000, "Dorf", "village", "place"),
              pt(1008, 1000, "Dorf", "village", "place")]
    layers = {}
    for layer, f in feats:
        layers.setdefault(layer, []).append(f)
    tile = mvt.encode([{"name": k, "features": v} for k, v in layers.items()])
    db = tmp_path / "t.mbtiles"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    con.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB)")
    con.executemany("INSERT INTO metadata VALUES (?, ?)", [("format", "pbf"), ("minzoom", "14"), ("maxzoom", "14")])
    con.execute("INSERT INTO tiles VALUES (14, 8529, ?, ?)", ((1 << 14) - 1 - 5973, gzip.compress(tile)))
    con.commit()
    con.close()
    out = tmp_path / "out"
    out.mkdir()
    script = tmp_path / "run.py"
    script.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        from streetzim.search_extract import extract_searchable_features
        if __name__ == "__main__":
            print("PATH=" + extract_searchable_features(mbtiles_path={str(db)!r}, output_dir={str(out)!r}))
    """))
    res = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stderr[-2000:]
    path = [x for x in res.stdout.splitlines() if x.startswith("PATH=")][0][5:]
    recs = [json.loads(x) for x in open(path, encoding="utf-8")]
    assert sorted((r["name"], r["type"]) for r in recs) == [
        ("Dorf", "place"), ("Dorf", "place"), ("Luzern Bahnhof", "poi"), ("Luzern Bahnhof", "poi")]
    assert "near-duplicate stops and water features merged" in res.stdout
