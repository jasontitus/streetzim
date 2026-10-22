"""Near-duplicate stops and water features: a region's own tiles carry a
station's every bay and stop position as a POI of the same name metres
apart (Switzerland's "Luzern Bahnhof" 22 times, bus stops 82,846 against
53,039 in the world-tile cache). The search pass merges same-name features
of one transit kind or of water within 100 m (zim_writer.near_duplicate_plan
over search_extract.near_duplicate_clusters), after the Wikipedia and names
matching: a member that links an article is the one kept, members linking
different articles are never merged, and a kept member without names takes
the dropped ones'. Extraction leaves them; places, peaks and parks are never
merged."""
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
from streetzim import zim_writer as W  # noqa: E402

M = 1 / 111_320.0          # a metre of latitude, in degrees


def stop(name, lat, lon, sub="bus", t="poi", **kw):
    return dict({"name": name, "type": t, "subtype": sub, "lat": lat, "lon": lon}, **kw)


def clusters(feats, metres=se.NEAR_DUP_M):
    pts = [(i, g, f["lat"], f["lon"]) for i, f in enumerate(feats)
           if (g := se.near_dup_group(f)) is not None]
    return sorted(sorted(p[0] for p in c) for c in se.near_duplicate_clusters(pts, metres))


# --- the clusters ------------------------------------------------------------

def test_a_stations_bays_are_one_cluster_and_the_central_one_is_its_middle():
    bays = [stop("Luzern Bahnhof", 47.0507, 8.3100 + i * 8 * M / 0.68) for i in range(5)]
    assert clusters(bays) == [[0, 1, 2, 3, 4]]
    pts = [(i, "g", f["lat"], f["lon"]) for i, f in enumerate(bays)]
    assert se.central_member(pts)[0] == 2


def test_far_apart_or_different_is_no_cluster():
    feats = [stop("Bahnhof", 47.0, 8.0), stop("Bahnhof", 47.0 + 150 * M, 8.0),   # 150 m apart
             stop("Post", 47.0, 8.0),                                           # another name
             stop("Bahnhof", 47.0, 8.0, sub="railway"),                         # another kind
             stop("Bahnhof", 47.0 + 5 * M, 8.0, t="place", sub="locality"),     # places
             stop("Bahnhof", 47.0 + 6 * M, 8.0, t="place", sub="locality")]
    assert clusters(feats) == []


def test_places_peaks_parks_and_other_pois_have_no_group():
    for t, s in (("peak", "peak"), ("place", "village"), ("park", "park"), ("poi", "shop"),
                 ("street", "primary"), ("admin", "canton")):
        assert se.near_dup_group(stop("X", 46.5, 9.0, t=t, sub=s)) is None
    assert se.near_dup_group(stop("Rotsee", 47.0, 8.3, t="water", sub="lake")) == ("rotsee", "water", "lake")
    assert se.near_dup_group(stop("Glion", 46.4, 6.9, sub="aerialway")) == ("glion", "poi", "aerialway")


def test_the_case_of_a_name_does_not_part_it():
    assert clusters([stop("Dorfbach", 47.0, 8.0, t="water", sub="stream"),
                     stop("dorfbach", 47.0 + 3 * M, 8.0, t="water", sub="stream")]) == [[0, 1]]


def test_longitude_metres_shrink_with_latitude():
    # 0.0013° of longitude is ~88 m at 47°N (one cluster), ~145 m at the equator (two).
    assert clusters([stop("X", 47.0, 8.0), stop("X", 47.0, 8.0013)]) == [[0, 1]]
    assert clusters([stop("X", 0.0, 8.0), stop("X", 0.0, 8.0013)]) == []


def test_exactly_the_distance_is_in():
    a, b = stop("X", 47.0, 8.0), stop("X", 47.0 + 0.000731, 8.0)
    d = (b["lat"] - a["lat"]) * 111_320.0       # as the clustering computes it
    assert clusters([a, b], metres=d) == [[0, 1]]
    assert clusters([a, b], metres=d * 0.999999) == []


def test_the_clusters_do_not_depend_on_the_order():
    bays = [stop("Bern Bahnhof", 46.9489 + (i % 3) * 7 * M, 7.4391 + (i // 3) * 9 * M / 0.68)
            for i in range(9)]
    pts = [(i, "g", f["lat"], f["lon"]) for i, f in enumerate(bays)]
    a = sorted(sorted(p[0] for p in c) for c in se.near_duplicate_clusters(pts))
    b = sorted(sorted(p[0] for p in c) for c in se.near_duplicate_clusters(list(reversed(pts))))
    assert a == b and len(a) >= 1


def test_the_quick_line_test_lets_every_group_through():
    for f in (stop("A", 1, 2, t="water", sub="lake"), stop("A", 1, 2, sub="bus"),
              stop("A", 1, 2, sub="railway"), stop("A", 1, 2, sub="aerialway"),
              stop("A", 1, 2, sub="ferry_terminal")):
        assert se.near_dup_line(json.dumps(f)) and se.near_dup_line(json.dumps(f, separators=(",", ":")))


# --- the search pass ---------------------------------------------------------

def bucket(tmp_path, feats, wiki=None, names=None, langs=("de",)):
    path = tmp_path / "search.jsonl"
    path.write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in feats), encoding="utf-8")
    (tmp_path / "chunks").mkdir(exist_ok=True)
    b = W._search_bucket(search_features_path=str(path), wikidata_data=None,
                         wiki_cross_refs=wiki or {}, loc_lookup=None, _bundled_set=set(),
                         chunk_tmp=str(tmp_path / "chunks"), ln_languages=list(langs),
                         ln_name_refs=names)
    recs = []
    for p in sorted((tmp_path / "chunks" / "categories").glob("*.jsonl")):
        recs += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    return b, recs


def cell(f):
    return (int(round(f["lat"] * 1e4)), int(round(f["lon"] * 1e4)))


BAYS = [stop("Luzern Bahnhof", 47.0507, 8.3100 + i * 8 * M / 0.68) for i in range(5)]


def test_the_pass_keeps_one_of_a_stations_bays(tmp_path):
    b, recs = bucket(tmp_path, BAYS)
    assert len(recs) == 1 and b.total_features == 1
    assert recs[0]["o"] == round(BAYS[2]["lon"], 5)


def test_the_member_that_links_an_article_is_kept(tmp_path):
    edge = BAYS[0]
    wiki = {("luzern bahnhof",) + cell(edge): {"wikidata": "Q676211", "wikipedia": "en:Lucerne station"}}
    _b, recs = bucket(tmp_path, BAYS, wiki)
    assert [(r["o"], r.get("q")) for r in recs] == [(round(edge["lon"], 5), "Q676211")]


def test_members_linking_different_articles_are_never_merged(tmp_path):
    wiki = {("luzern bahnhof",) + cell(BAYS[0]): {"wikidata": "Q1"},
            ("luzern bahnhof",) + cell(BAYS[4]): {"wikidata": "Q2"}}
    _b, recs = bucket(tmp_path, BAYS, wiki)
    assert sorted(r.get("q") for r in recs) == ["Q1", "Q2"]


def test_the_kept_member_takes_the_dropped_members_names(tmp_path):
    wiki = {("luzern bahnhof",) + cell(BAYS[2]): {"wikidata": "Q676211"}}
    names = {("luzern bahnhof",) + cell(BAYS[0]): {"names": {"de": "Luzern, Bahnhof"}}}
    _b, recs = bucket(tmp_path, BAYS, wiki, names)
    assert [(r.get("q"), r.get("ln")) for r in recs] == [("Q676211", {"de": "Luzern, Bahnhof"})]


def test_disagreeing_names_are_kept_as_their_own_records(tmp_path):
    """Two sets of names the linked member cannot take both of: each stays,
    so no name in another language is lost."""
    wiki = {("luzern bahnhof",) + cell(BAYS[2]): {"wikidata": "Q676211"}}
    names = {("luzern bahnhof",) + cell(BAYS[0]): {"names": {"de": "A"}},
             ("luzern bahnhof",) + cell(BAYS[4]): {"names": {"de": "B"}}}
    _b, recs = bucket(tmp_path, BAYS, wiki, names)
    assert sorted((r.get("q") or "", json.dumps(r.get("ln"))) for r in recs) == [
        ("", '{"de": "A"}'), ("", '{"de": "B"}'), ("Q676211", "null")]


def test_among_one_articles_members_the_one_with_names_is_kept(tmp_path):
    wiki = {("luzern bahnhof",) + cell(f): {"wikidata": "Q676211"} for f in BAYS}
    names = {("luzern bahnhof",) + cell(BAYS[0]): {"names": {"de": "Luzern, Bahnhof"}}}
    _b, recs = bucket(tmp_path, BAYS, wiki, names)
    # The edge member with names, not the central one without: one record, both.
    assert [(r["o"], r.get("q"), r.get("ln")) for r in recs] == [
        (round(BAYS[0]["lon"], 5), "Q676211", {"de": "Luzern, Bahnhof"})]


def test_a_member_with_names_wins_over_one_without(tmp_path):
    names = {("luzern bahnhof",) + cell(BAYS[4]): {"names": {"de": "Luzern, Bahnhof"}}}
    _b, recs = bucket(tmp_path, BAYS, None, names)
    assert [(r["o"], r.get("ln")) for r in recs] == [(round(BAYS[4]["lon"], 5), {"de": "Luzern, Bahnhof"})]
    own = [dict(f) for f in BAYS]
    own[1]["names"] = {"de": "Luzern Bf"}
    (tmp_path / "chunks").rename(tmp_path / "chunks-1")
    _b, recs = bucket(tmp_path, own, None, None)
    assert [r.get("ln") for r in recs] == [{"de": "Luzern Bf"}]


def test_other_features_pass_untouched(tmp_path):
    feats = BAYS + [stop("Luzern", 47.05, 8.30, t="place", sub="city"),
                    stop("Luzern", 47.05 + 3 * M, 8.30, t="place", sub="city"),
                    stop("Luzern Bahnhof", 47.0507, 8.3100, sub="railway")]
    b, recs = bucket(tmp_path, feats)
    assert sorted((r["t"], r["s"]) for r in recs) == [
        ("place", "city"), ("place", "city"), ("poi", "bus"), ("poi", "railway")]
    assert b.type_counts == {"poi": 2, "place": 2}


def test_the_switch_leaves_them(tmp_path, monkeypatch):
    monkeypatch.setenv("STREETZIM_NEAR_DUP", "0")
    _b, recs = bucket(tmp_path, BAYS)
    assert len(recs) == 5


mvt = pytest.importorskip("mapbox_vector_tile")


def test_extraction_leaves_them(tmp_path):
    """extract_searchable_features keeps every bay (the search pass merges
    them, once it knows which one links an article)."""
    feats = [{"geometry": f"POINT({2000 + 40 * i} 2000)",
              "properties": {"name": "Luzern Bahnhof", "class": "bus"}} for i in range(4)]
    tile = mvt.encode([{"name": "poi", "features": feats}])
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
    assert len(open(path, encoding="utf-8").read().splitlines()) == 4


def test_two_members_of_one_article_with_other_names_both_stay(tmp_path):
    """Same article, different names: the second set of names is not lost."""
    wiki = {("luzern bahnhof",) + cell(BAYS[1]): {"wikidata": "Q676211"},
            ("luzern bahnhof",) + cell(BAYS[3]): {"wikidata": "Q676211"}}
    names = {("luzern bahnhof",) + cell(BAYS[1]): {"names": {"de": "A"}},
             ("luzern bahnhof",) + cell(BAYS[3]): {"names": {"de": "B"}}}
    _b, recs = bucket(tmp_path, BAYS, wiki, names)
    assert sorted(json.dumps(r.get("ln")) for r in recs) == ['{"de": "A"}', '{"de": "B"}']
    assert {r.get("q") for r in recs} == {"Q676211"}


# --- with neighbour matching (feature_wiki_tier) ---------------------------

def test_the_tiers():
    from streetzim.addresses import OsmRef
    feat = {"name": "Zurich", "name_native": "Zürich", "type": "place", "subtype": "city",
            "lat": 47.37445, "lon": 8.54104}
    own_cell = (473745, 85410)
    next_cell = (473744, 85410)
    q72 = OsmRef("place", wikidata="Q72")
    assert W.feature_wiki_tier(feat, {("zurich",) + own_cell: q72}) == (q72, W.TIER_OWN)
    assert W.feature_wiki_tier(feat, {("zürich",) + own_cell: q72}) == (q72, W.TIER_OWN)
    assert W.feature_wiki_tier(feat, {("zürich",) + next_cell: q72}) == (q72, W.TIER_NEAR)
    assert W.feature_wiki_tier(feat, {}) == (None, W.TIER_NONE)
    two = {("zürich",) + own_cell: OsmRef("place", wikidata="Q72"),
           ("zürich",) + (own_cell[0], own_cell[1]): OsmRef("place", wikidata="Q72")}
    assert W.feature_wiki_tier(feat, two)[1] == W.TIER_OWN
    # Its own-script name at its cell linking two articles (two objects
    # under name and name_native... ): ambiguous, no entry.
    feat2 = dict(feat, name="Zuerich", name_native="Zürich")
    amb = {("zürich",) + own_cell: OsmRef("place", wikidata="Q72")}
    pieces = dict(feat2, _pts=[[47.37455, 8.54104]])
    amb[("zürich", 473746, 85410)] = OsmRef("place", wikidata="Q99")
    assert W.feature_wiki_tier(pieces, amb) == (None, W.TIER_AMBIGUOUS)
    # Next door, two articles: ambiguous.
    nd = {("zurich",) + next_cell: OsmRef("place", wikidata="Q72"),
          ("zurich", 473746, 85410): OsmRef("place", wikidata="Q99")}
    assert W.feature_wiki_tier(feat, nd) == (None, W.TIER_AMBIGUOUS)
    # A folded key next door that two objects share: ambiguous.
    fz = {("zürich",) + next_cell: OsmRef("place", wikidata="Q72"),
          ("zurich",) + next_cell: OsmRef("place", wikidata="Q99")}
    plain = {"name": "Zurich", "type": "place", "subtype": "city", "lat": 47.37445, "lon": 8.54104}
    assert W.feature_wiki_tier(plain, fz, W.wiki_folded_index(fz)) == (None, W.TIER_AMBIGUOUS)
    # feature_wiki_match keeps its contract.
    assert W.feature_wiki_match(feat, {("zürich",) + next_cell: q72}) == (q72, True)
    assert W.feature_wiki_match(feat, nd) == (None, False)


def ref(kind, **kw):
    from streetzim.addresses import OsmRef
    return OsmRef(kind, **kw)


def test_the_member_matched_in_its_own_cell_is_kept_over_one_from_next_door(tmp_path):
    """BAYS[2] (central) finds the station only next door; BAYS[0] in its
    own cell: BAYS[0] is kept, and its geo-index row is its own cell's."""
    st = ref("station", wikidata="Q676211", wikipedia="en:Lucerne_station")
    c0, c2 = cell(BAYS[0]), cell(BAYS[2])
    rail = [dict(f, subtype="railway") for f in BAYS]
    wiki = {("luzern bahnhof",) + c0: st, ("luzern bahnhof", c2[0] + 1, c2[1]): st}
    path = tmp_path / "search.jsonl"
    path.write_text("".join(json.dumps(f) + "\n" for f in rail), encoding="utf-8")
    (tmp_path / "chunks").mkdir()
    b = W._search_bucket(search_features_path=str(path), wikidata_data=None, wiki_cross_refs=wiki,
                         loc_lookup=None, _bundled_set={"Lucerne_station"},
                         chunk_tmp=str(tmp_path / "chunks"), ln_languages=None)
    assert b.wiki_geo["Lucerne_station"][:2] == [round(rail[0]["lat"], 5), round(rail[0]["lon"], 5)]
    assert b.wiki_fields_added == 1 and b.total_features == 1


def test_an_ambiguous_member_vetoes_nothing(tmp_path):
    """Member A finds two stations next door (ambiguous: none); member B,
    50 m on, finds one: the cluster keeps B with it."""
    a = stop("Luzern Bahnhof", 47.0507, 8.3100, sub="railway")
    b = stop("Luzern Bahnhof", 47.0507, 8.3100 + 50 * M / 0.68, sub="railway")
    ca, cb = cell(a), cell(b)
    wiki = {("luzern bahnhof", ca[0] + 1, ca[1]): ref("station", wikidata="Q1"),
            ("luzern bahnhof", ca[0] - 1, ca[1]): ref("station", wikidata="Q2"),
            ("luzern bahnhof", cb[0] + 1, cb[1]): ref("station", wikidata="Q676211")}
    assert W.feature_wiki_tier(a, wiki) == (None, W.TIER_AMBIGUOUS)
    assert W.feature_wiki_tier(b, wiki)[1] == W.TIER_NEAR
    _b, recs = bucket(tmp_path, [a, b], wiki)
    assert [(r["o"], r.get("q")) for r in recs] == [(round(b["lon"], 5), "Q676211")]


def test_a_dropped_duplicate_places_no_row_and_counts_nothing(tmp_path):
    wiki = {("luzern bahnhof",) + cell(f): ref("stop", wikidata="Q676211", wikipedia="en:Lucerne_station")
            for f in BAYS}
    path = tmp_path / "search.jsonl"
    path.write_text("".join(json.dumps(f) + "\n" for f in BAYS), encoding="utf-8")
    (tmp_path / "chunks").mkdir()
    b = W._search_bucket(search_features_path=str(path), wikidata_data=None, wiki_cross_refs=wiki,
                         loc_lookup=None, _bundled_set={"Lucerne_station"},
                         chunk_tmp=str(tmp_path / "chunks"), ln_languages=None)
    assert b.wiki_fields_added == 1 and b.total_features == 1
    assert b.wiki_geo["Lucerne_station"][:2] == [round(BAYS[2]["lat"], 5), round(BAYS[2]["lon"], 5)]


def test_names_a_kept_member_already_has_add_no_record(tmp_path):
    wiki = {("luzern bahnhof",) + cell(BAYS[2]): {"wikidata": "Q676211"}}
    same = {"names": {"de": "Luzern, Bahnhof"}}
    names = {("luzern bahnhof",) + cell(BAYS[2]): same, ("luzern bahnhof",) + cell(BAYS[0]): dict(same)}
    _b, recs = bucket(tmp_path, BAYS, wiki, names)
    assert [(r.get("q"), r.get("ln")) for r in recs] == [("Q676211", {"de": "Luzern, Bahnhof"})]
