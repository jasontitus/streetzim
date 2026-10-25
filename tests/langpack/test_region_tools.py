"""The production helpers around the pipeline: region_json.py (the gates'
region file from a base), region_meta.py (which languages and packs a base
gets, and their titles), site_index.py (the builder page's region list)
and prefetch.py (the network step before offline pack builds)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("libzim")

from tests.langpack import lpzims  # noqa: E402
from tests.langpack.conftest import tool  # noqa: E402

CENTER = [23.7275, 37.9838]                 # lon, lat (map-config order)
OUTLINE = {"type": "Polygon", "coordinates": [[[19.0, 34.0], [28.0, 34.0], [28.0, 41.5], [19.0, 41.5], [19.0, 34.0]]]}


def _base(path: Path, places: list[dict], geo: dict, cfg: dict, admin: list[dict] | None = None,
          title: str = "OSM - Testland", search: dict[str, list[dict]] | None = None,
          raw: tuple[str, ...] = ()) -> Path:
    """`search`: search-data/<name>.json files, written as the builder does;
    the names in `raw` stored uncompressed."""
    from libzim.writer import Creator
    with Creator(str(path)).config_indexing(False, "eng") as c:
        c.set_mainpath("index.html")
        for k, v in (("Name", "osm_en_testland"), ("Title", title), ("Language", "eng"),
                     ("Description", "d"), ("Creator", "c"), ("Publisher", "p"), ("Date", "2026-10-13")):
            c.add_metadata(k, v)
        c.add_item(lpzims._Item("index.html", "Testland", "text/html", "<html></html>", front=True))
        c.add_item(lpzims._Item("map-config.json", "", "application/json", json.dumps(cfg)))
        c.add_item(lpzims._Item("category-index/place.json", "", "application/json", json.dumps(places)))
        c.add_item(lpzims._Item("category-index/admin.json", "", "application/json", json.dumps(admin or [])))
        c.add_item(lpzims._Item("wiki-geo-index.json", "", "application/json", json.dumps(geo)))
        for name, rows in (search or {}).items():
            c.add_item(lpzims._Item(f"search-data/{name}.json", "", "application/json",
                                    json.dumps(rows, separators=(",", ":"), ensure_ascii=False),
                                    compress=False if name in raw else None))
    return path


PLACES = [
    # the main city has no Q-ID in the geo-index (the base offers no article): not picked
    {"n": "Athens", "nn": "Αθήνα", "s": "city", "a": 37.9756, "o": 23.7348},
    {"n": "Piraeus", "nn": "Πειραιάς", "s": "city", "a": 37.9420, "o": 23.6465, "q": "Q58976"},
    {"n": "Marousi", "nn": "Μαρούσι", "s": "town", "a": 38.0500, "o": 23.8000, "q": "Q852385"},
    {"n": "Keratsini", "s": "suburb", "a": 37.9622, "o": 23.6197},          # 3 km from Piraeus: the route's end
    {"n": "Pasalimani", "s": "quarter", "a": 37.9380, "o": 23.6500},        # 0.5 km: too near to be a route
    {"n": "Edirne", "s": "city", "a": 41.6771, "o": 26.5557, "q": "Q43387"},   # far, and outside the outline
    {"n": "Volos", "nn": "Βόλος", "s": "city", "a": 39.3622, "o": 22.9420, "q": "Q200036"},
]
GEO = {"Piraeus": [37.942, 23.6465, "place", "Q58976", ""], "Marousi": [38.05, 23.8, "place", "Q852385", ""],
       "Edirne": [41.6771, 26.5557, "place", "Q43387", ""], "Volos": [39.36, 22.94, "place", "Q200036", ""]}


def _rj(path: Path) -> dict:
    r = tool("region_json.py", path)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_region_json_picks_a_city_the_base_has_an_article_for(tmp_path: Path) -> None:
    b = _base(tmp_path / "b.zim", PLACES, GEO, {"name": "Testland", "center": CENTER, "clipArea": OUTLINE})
    r = _rj(b)
    assert r["city"]["qid"] == "Q58976" and r["generated"]["city"] == "Piraeus"     # not Athens (no article)
    assert r["search"]["native"] == {"q": "Πειραιάς", "expect": "Piraeus"}
    assert r["search"]["english"] == {"q": "Piraeus", "expect": "Piraeus"}
    assert r["route"]["src"] == "37.94200,23.64650" and r["route"]["dst"] == "37.96220,23.61970"
    assert r["named_qids"].split(",")[0] == "Q58976" and r["base_lang"] == "en"


def test_region_json_near_the_center_beats_bigger_far_away(tmp_path: Path) -> None:
    # without Piraeus: Marousi (a town 10 km from the center) beats Volos (a city 160 km away)
    b = _base(tmp_path / "b.zim", [p for p in PLACES if p["n"] != "Piraeus"], GEO,
              {"name": "Testland", "center": CENTER, "clipArea": OUTLINE})
    assert _rj(b)["generated"]["city"] == "Marousi"


def test_region_json_stays_inside_the_outline(tmp_path: Path) -> None:
    only = [p for p in PLACES if p["n"] in ("Edirne", "Volos", "Athens")]
    b = _base(tmp_path / "b.zim", only, GEO, {"name": "Testland", "center": [26.5, 41.6], "clipArea": OUTLINE})
    assert _rj(b)["generated"]["city"] == "Volos"                # Edirne is nearer, but outside
    b2 = _base(tmp_path / "b2.zim", only, GEO, {"name": "Testland", "center": [26.5, 41.6]})
    assert _rj(b2)["generated"]["city"] == "Edirne"              # no outline: anywhere


def test_region_json_falls_back_to_the_geo_index_and_fails_without_one(tmp_path: Path) -> None:
    b = _base(tmp_path / "b.zim", [], {"Alpha_Town": [10.0, 20.0, "place", "Q1", ""]}, {"name": "T", "center": [20, 10]})
    r = _rj(b)
    assert r["city"]["qid"] == "Q1" and r["search"]["english"]["q"] == "Alpha Town"
    assert r["route"]["dst"] == "10.01800,20.00000"              # no place nearby: 2 km north
    none = _base(tmp_path / "n.zim", PLACES, {}, {"name": "T", "center": CENTER})
    r2 = tool("region_json.py", none)
    assert r2.returncode == 1 and "no place with a Q-ID" in r2.stderr


# osm-balkans-2026-10-23: no category-index/place.json; a neighbourhood's article
# point is nearer map-config's center than the capital's
BALKANS_GEO = {"Belgrade": [44.81781, 20.4569, "place", "Q3711", "capital of Serbia"],
               "Topčidersko_Brdo": [44.78579, 20.44972, "place", "Q655022", "neighborhood"],
               "Novi_Sad": [45.25514, 19.84517, "place", "Q55630", "city in Serbia"],
               "Ripanj": [44.6386, 20.5267, "place", "Q1013458", "village in Serbia"],
               "Zemun": [44.8433, 20.4011, "place", "Q190656", "municipality in Belgrade"]}
BALKANS_SEARCH = {
    "be~l~c": [{"n": "Belgrade", "t": "place", "s": "city", "a": 44.81781, "o": 20.4569, "nn": "Београд", "q": "Q3711"},
               {"n": "Belgrade Fortress", "t": "historic", "s": "castle", "a": 44.823, "o": 20.450, "q": "Q1059"}],
    "br~d~c": [{"n": "Topčidersko brdo", "t": "place", "s": "neighbourhood", "a": 44.78579, "o": 20.44972,
                "nn": "Топчидерско брдо", "q": "Q655022"}],
    "no": [{"n": "Novi Sad", "t": "place", "s": "city", "a": 45.25514, "o": 19.84517, "nn": "Нови Сад", "q": "Q55630"}],
    "ze": [{"n": "Zemun", "t": "place", "s": "suburb", "a": 44.8433, "o": 20.4011, "q": "Q190656"},
           {"n": "Zemun", "t": "place", "s": "suburb", "a": 44.8433, "o": 20.4011, "q": "Q190656"}],
    "ri": [{"n": "Ripanj", "t": "place", "s": "village", "a": 44.6386, "o": 20.5267, "q": "Q1013458"}],
}
BALKANS_CFG = {"name": "Balkans", "center": [20.4489, 44.7866]}


def test_region_json_without_place_records_reads_the_search_records(tmp_path: Path) -> None:
    b = _base(tmp_path / "b.zim", [], BALKANS_GEO, BALKANS_CFG, search=BALKANS_SEARCH)
    r = _rj(b)
    assert r["city"]["qid"] == "Q3711" and r["generated"] == {
        "by": "region_json.py", "city": "Belgrade", "class": "city", "base": "b.zim"}
    assert r["search"]["native"] == {"q": "Београд", "expect": "Belgrade"}     # the record's native name
    assert r["search"]["english"] == {"q": "Belgrade", "expect": "Belgrade"}
    assert r["named_qids"] == "Q3711,Q55630"
    # without the capital's article: the other city, not the nearer neighbourhood, suburb or village
    geo = {k: v for k, v in BALKANS_GEO.items() if k != "Belgrade"}
    assert _rj(_base(tmp_path / "b2.zim", [], geo, BALKANS_CFG, search=BALKANS_SEARCH))["generated"]["city"] == "Novi Sad"
    # only a village drawn: the village
    only = {"ri": BALKANS_SEARCH["ri"], "br~d~c": BALKANS_SEARCH["br~d~c"]}
    assert _rj(_base(tmp_path / "b3.zim", [], BALKANS_GEO, BALKANS_CFG, search=only))["generated"]["city"] == "Ripanj"


def test_region_json_reads_the_search_records_when_no_category_place_qualifies(tmp_path: Path) -> None:
    """A place index whose places have no Q-ID the geo-index lists: the search
    records' city, not the geo-index's nearest row."""
    unlinked = [{k: v for k, v in r.items() if k != "q"} for r in BALKANS_SEARCH["be~l~c"][:1]]
    unlinked[0]["s"] = "city"
    b = _base(tmp_path / "b.zim", unlinked, BALKANS_GEO, BALKANS_CFG, search=BALKANS_SEARCH)
    assert _rj(b)["generated"] == {"by": "region_json.py", "city": "Belgrade", "class": "city", "base": "b.zim"}


def test_region_json_keeps_the_bigger_class_of_records_sharing_a_q_id(tmp_path: Path) -> None:
    """A village and a town linked to one article (the village first in path
    order): the town's record is the one kept."""
    geo = {"Sopot": [44.5194, 20.5753, "place", "Q1013179", "town in Serbia"]}
    search = {"ro": [{"n": "Rogača", "t": "place", "s": "village", "a": 44.42, "o": 20.53, "q": "Q1013179"}],
              "so": [{"n": "Sopot", "t": "place", "s": "town", "a": 44.5194, "o": 20.5753, "q": "Q1013179"}]}
    r = _rj(_base(tmp_path / "b.zim", [], geo, BALKANS_CFG, search=search))
    assert (r["generated"]["city"], r["generated"]["class"]) == ("Sopot", "town")


def test_region_json_reads_search_records_from_any_cluster(tmp_path: Path) -> None:
    """Search files in compressed and uncompressed clusters, among other
    entries: each cluster read whole gives the bytes blob by blob reading
    does, and the place records in either are found."""
    from tools.langpack.zimfmt import ZimReader
    search = {**BALKANS_SEARCH, "a": [{"n": "Ada", "t": "poi", "s": "shop", "a": 44.8, "o": 20.4}]}
    b = _base(tmp_path / "b.zim", [], BALKANS_GEO, BALKANS_CFG, search=search, raw=("be~l~c", "ri"))
    z = ZimReader(str(b))
    blobs: dict[int, set[int]] = {}
    for d in z.iter_dirents():
        if not d.is_redirect and d.cluster is not None and d.blob is not None:
            blobs.setdefault(d.cluster, set()).add(d.blob)
    kinds = {z.cluster_info(c) & 0x0F for c in blobs}
    assert len(kinds) > 1, kinds                              # both kinds of cluster
    for c, bs in blobs.items():
        whole = z.cluster_blobs(c)
        assert len(whole) >= max(bs) + 1
        assert [whole[x] for x in sorted(bs)] == [z.blob_bytes(c, x) for x in sorted(bs)]
    first = z.lower_bound(ord("C"), b"search-data/")
    assert z.dirent_at(first).path == b"search-data/a.json"
    assert z.find(ord("C"), b"search-data/") is None
    assert z.lower_bound(ord("Z"), b"") == z.header.entry_count
    r = _rj(b)
    assert r["generated"]["city"] == "Belgrade" and r["named_qids"] == "Q3711,Q55630"
    geo = {k: v for k, v in BALKANS_GEO.items() if k not in ("Belgrade", "Novi_Sad")}
    assert _rj(_base(tmp_path / "b2.zim", [], geo, BALKANS_CFG, search=search,
                     raw=("be~l~c", "ri")))["generated"]["city"] == "Ripanj"


def test_region_json_names_the_search_record_not_the_article_title(tmp_path: Path) -> None:
    geo = {"New_York_City": [40.7127, -74.0059, "place", "Q60", "most populous city in the United States"],
           "Trenton,_New_Jersey": [40.2170, -74.7429, "place", "Q25330", "capital city of New Jersey"]}
    search = {"ne": [{"n": "New York", "t": "place", "s": "city", "a": 40.7127, "o": -74.0059, "q": "Q60"}],
              "tr": [{"n": "Trenton", "t": "place", "s": "city", "a": 40.2170, "o": -74.7429, "q": "Q25330"}]}
    r = _rj(_base(tmp_path / "b.zim", [], geo, {"name": "East Coast", "center": [-74.0, 40.71]}, search=search))
    assert r["generated"]["city"] == "New York"
    assert r["search"]["native"] == {"q": "New York", "expect": "New York"}


def _wiki(tmp_path: Path, langs: list[str]) -> Path:
    w = tmp_path / "wiki-src"
    w.mkdir()
    for lg in langs:
        (w / f"wikipedia_{lg}_all_maxi_2026-01.zim").write_bytes(b"")
    return w


def _meta(base: Path, wiki: Path, core: str, cache: Path | None = None, *extra: str) -> dict:
    args = [base, "--wiki-src", wiki, "--core", core] + (["--cache", cache] if cache else []) + list(extra)
    r = tool("region_meta.py", *args)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


ADMIN = [{"n": "Testland", "s": "city", "q": "Q5"},        # a city of the same name is not the region
         {"n": "Testland", "s": "country", "q": "Q41"}, {"n": "Elsewhere", "s": "region", "q": "Q9"}]


def test_region_meta_languages_and_packs(tmp_path: Path) -> None:
    cfg = {"name": "Testland", "center": CENTER, "hasWikiArticles": True, "regionLanguages": ["el", "tr", "xx"]}
    b = _base(tmp_path / "b.zim", PLACES, GEO, cfg, ADMIN)
    m = _meta(b, _wiki(tmp_path, ["de", "el", "en", "ja", "tr"]), "de,ja,fa,en")
    assert m["base_lang"] == "en" and m["base_wiki"] == "en"
    assert m["region_langs"] == ["el", "tr"]                     # xx: no Wikipedia ZIM
    assert m["langs"] == ["de", "ja", "en", "el", "tr"]          # fa: none either
    assert {d["lang"] for d in m["dropped"]} == {"xx", "fa"}
    # every entry needs its own pack plus the region's; English is the base's own
    assert m["pack_langs"] == ["de", "el", "ja", "tr"]


def test_region_meta_keeps_a_continents_most_spoken_languages(tmp_path: Path) -> None:
    """Every entry carries the region's packs: Europe's 38 languages are cut to
    the first few with a Wikipedia ZIM (the base's own English not counted)."""
    cfg = {"name": "Europe", "hasWikiArticles": True, "regionLanguages": ["en", "de", "xx", "ru", "fr", "tr", "it"]}
    b = _base(tmp_path / "b.zim", PLACES, GEO, cfg)
    w = _wiki(tmp_path, ["de", "en", "fr", "it", "ru", "tr"])
    m = _meta(b, w, "de,fr,it,en", None, "--tier", "continent")
    assert m["region_langs"] == ["en", "de", "ru", "fr"]
    why = {d["lang"]: d["why"] for d in m["dropped"]}
    assert set(why) == {"xx", "tr", "it"} and why["xx"].startswith("region language with no Wikipedia ZIM")
    assert why["tr"].startswith("region language beyond the 3 most spoken") and why["it"] == why["tr"]
    assert "it" in m["langs"]                                    # still on the menu: a core language
    r = tool("region_meta.py", b, "--wiki-src", w, "--core", "de", "--region-packs-max", "1", "--region", "europe")
    assert json.loads(r.stdout)["region_langs"] == ["en", "de"] and json.loads(r.stdout)["tier"] == "continent"


def test_a_country_map_carries_all_its_languages(tmp_path: Path) -> None:
    """The cap is for continents and multi-country maps: Switzerland (a
    country in cloud/regions.tsv) carries de, fr, it and rm in every entry."""
    cfg = {"name": "Switzerland", "hasWikiArticles": True, "regionLanguages": ["de", "fr", "it", "rm"]}
    b = _base(tmp_path / "b.zim", PLACES, GEO, cfg)
    w = _wiki(tmp_path, ["de", "fr", "it", "rm", "en", "ja"])
    for extra in (("--region", "switzerland"), ("--tier", "country"), ("--tier", "local"), ("--tier", "us-region")):
        m = _meta(b, w, "ja,en", None, *extra)
        assert m["region_langs"] == ["de", "fr", "it", "rm"] and not m["dropped"], (extra, m)
        assert m["pack_langs"] == ["de", "fr", "it", "ja", "rm"]
    assert _meta(b, w, "ja,en", None, "--tier", "multi-country")["region_langs"] == ["de", "fr", "it"]


def test_the_cap_fails_closed(tmp_path: Path) -> None:
    """Review S-4: a region cloud/regions.tsv does not list (or no region at
    all) is capped like a continent, not given every language."""
    cfg = {"name": "Somewhere", "hasWikiArticles": True, "regionLanguages": ["de", "fr", "it", "rm"]}
    b = _base(tmp_path / "b.zim", PLACES, GEO, cfg)
    w = _wiki(tmp_path, ["de", "fr", "it", "rm", "en", "ja"])
    for extra in (("--region", "not-in-the-registry"), ()):
        m = _meta(b, w, "ja,en", None, *extra)
        assert m["region_langs"] == ["de", "fr", "it"] and m["tier"] is None, (extra, m)
        assert [d["lang"] for d in m["dropped"]] == ["rm"], m
    from tools.langpack.region_meta import registry_tier
    assert (registry_tier("switzerland"), registry_tier("europe"), registry_tier("balkans"),
            registry_tier("switzerland-de"), registry_tier(None)) == ("country", "continent", "multi-country", "country", None)


def test_a_variant_takes_its_parents_tier(tmp_path: Path) -> None:
    """Re-review: the German Swiss maps (switzerland-de*) are not in
    cloud/regions.tsv; with the cap failing closed they lost Romansh. A
    variant takes its parent region's tier: switzerland's, a country."""
    from tools.langpack.region_meta import registry_tier
    assert [registry_tier(x) for x in ("switzerland-de", "switzerland-de-ost", "europe-north-x", "nowhere-de")] == \
        ["country", "country", "continent", None]
    cfg = {"name": "Deutschschweiz", "hasWikiArticles": True, "regionLanguages": ["de", "fr", "it", "rm"]}
    b = _base(tmp_path / "b.zim", PLACES, GEO, cfg)
    m = _meta(b, _wiki(tmp_path, ["de", "fr", "it", "rm", "en"]), "en", None, "--region", "switzerland-de")
    assert m["tier"] == "country" and m["region_langs"] == ["de", "fr", "it", "rm"] and not m["dropped"], m


def test_region_meta_non_english_base_needs_english(tmp_path: Path) -> None:
    cfg = {"name": "Testland", "language": "de", "hasWikiArticles": True}
    b = _base(tmp_path / "b.zim", PLACES, GEO, cfg)
    w = _wiki(tmp_path, ["de", "en", "fr"])
    m = _meta(b, w, "fr,en")
    assert m["base_wiki"] == "de" and m["langs"] == ["fr", "en", "de"]
    assert m["pack_langs"] == ["en", "fr"]
    # English is needed even when it is not on the menu: every entry carries it
    m2 = _meta(b, w, "fr")
    assert m2["langs"] == ["fr", "de"] and m2["pack_langs"] == ["en", "fr"]


def test_region_meta_titles_from_the_wikidata_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    (cache / "wikidata-text").mkdir(parents=True)
    (cache / "wikidata-text" / "de.jsonl").write_text(
        json.dumps(["Q9", "Anderswo", ""]) + "\n" + json.dumps(["Q5", "Teststadt", ""]) + "\n"
        + json.dumps(["Q41", "Testlandia", "Staat"]) + "\n")
    (cache / "wikidata-text" / "ja.jsonl").write_text(json.dumps(["Q41", "テ" * 31, ""]) + "\n")   # too long
    b = _base(tmp_path / "b.zim", PLACES, GEO, {"name": "Testland", "hasWikiArticles": True}, ADMIN)
    m = _meta(b, _wiki(tmp_path, ["de", "ja", "en"]), "de,ja,en", cache)
    assert m["region_qid"] == "Q41"
    assert m["titles"] == {"de": "Testlandia", "ja": "OSM - Testland", "en": "OSM - Testland"}


def test_the_region_is_found_by_another_name_or_its_id(tmp_path: Path) -> None:
    """Switzerland: the map is "Switzerland and the Alps", the country record
    "Schweiz/Suisse/Svizzera/Svizra" (also called Switzerland): found through
    the region id, so every entry's title is the country's name in its
    language (the dry run titled all 23 "OSM - Switzerland and the Alps")."""
    cache = tmp_path / "cache"
    (cache / "wikidata-text").mkdir(parents=True)
    for lg, label in (("de", "Schweiz"), ("rm", "Svizra"), ("ja", "スイス")):
        (cache / "wikidata-text" / f"{lg}.jsonl").write_text(json.dumps(["Q39", label, ""]) + "\n")
    admin = [{"n": "Liechtenstein", "s": "country", "q": "Q347", "alt": ["Principality of Liechtenstein"]},
             {"n": "Schweiz/Suisse/Svizzera/Svizra", "s": "country", "q": "Q39", "alt": ["Switzerland", "Swiss Confederation"]}]
    cfg = {"name": "Switzerland and the Alps", "hasWikiArticles": True, "regionLanguages": ["de", "rm"]}
    b = _base(tmp_path / "b.zim", PLACES, GEO, cfg, admin)
    w = _wiki(tmp_path, ["de", "rm", "ja", "en"])
    m = _meta(b, w, "de,ja,en", cache, "--region", "switzerland")
    assert m["region_qid"] == "Q39", m
    assert (m["titles"]["de"], m["titles"]["rm"], m["titles"]["ja"]) == ("Schweiz", "Svizra", "スイス"), m["titles"]
    # an alt name equal to the map's name counts too; no region id, no other match: none
    cfg2 = dict(cfg, name="Swiss Confederation")
    assert _meta(_base(tmp_path / "b2.zim", PLACES, GEO, cfg2, admin), w, "de", cache)["region_qid"] == "Q39"
    assert _meta(b, w, "de", cache)["region_qid"] is None


def _region_dir(root: Path, rid: str, title: str, menu: bool = True) -> None:
    d = root / rid
    d.mkdir(parents=True)
    (d / "region.json").write_text(json.dumps({"id": rid, "title": title, "menu": [{"lang": "de"}] if menu else []}))


def test_site_index_writes_under_a_temporary_name_of_its_own(tmp_path: Path) -> None:
    """Another writer of the index (web/generate.py) may be writing at the
    same time: never a fixed temporary name."""
    rd = tmp_path / "recipes"
    _region_dir(rd, "greece", "Greece")
    (rd / "index.json.tmp").mkdir()                       # a fixed name taken
    r = tool("site_index.py", rd)
    assert r.returncode == 0, r.stderr
    assert [x["id"] for x in json.loads((rd / "index.json").read_text())["regions"]] == ["greece"]
    assert sorted(p.name for p in rd.iterdir() if p.name.startswith("index.json.")) == ["index.json.tmp"]
    assert (rd / "index.json").stat().st_mode & 0o777 == 0o644               # served: readable by all


def test_site_index(tmp_path: Path) -> None:
    rd = tmp_path / "recipes"
    _region_dir(rd, "greece", "Greece")
    _region_dir(rd, "austria-czech", "Austria & Czechia")
    _region_dir(rd, "aaa-zone", "Zeta Islands")             # sorted by title, not folder
    _region_dir(rd, "empty", "Nothing", menu=False)
    (rd / "broken").mkdir()
    (rd / "wrongid").mkdir()
    (rd / "wrongid" / "region.json").write_text(json.dumps({"id": "other", "title": "x", "menu": [1]}))
    r = tool("site_index.py", rd)
    assert r.returncode == 0, r.stderr
    idx = json.loads((rd / "index.json").read_text())
    assert idx["format"] == "streetzim-build-index/1"
    assert [x["id"] for x in idx["regions"]] == ["austria-czech", "greece", "aaa-zone"]
    for bad in ("empty", "broken", "wrongid"):
        assert bad in r.stderr
    empty = tmp_path / "none"
    empty.mkdir()
    assert tool("site_index.py", empty).returncode == 1 and not (empty / "index.json").exists()


def test_prefetch_retries_text_and_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    from streetzim import wiki_pack
    from tools.langpack import prefetch
    calls: dict[str, list] = {"titles": [], "dump": [], "text": []}

    class B:
        uuid, qids = "u", ["Q1", "Q2"]
    monkeypatch.setattr(wiki_pack, "read_base", lambda *a, **k: B())
    monkeypatch.setattr(wiki_pack, "build_title_map", lambda lg, c, **k: calls["titles"].append(lg))

    def dump(lg, table, c, **k):
        calls["dump"].append((lg, table))
        if lg == "zz":
            raise FileNotFoundError("no dump")
    monkeypatch.setattr(wiki_pack, "fetch_dump", dump)
    monkeypatch.setattr(wiki_pack, "load_titles", lambda lg, q, c, **k: {"Q1": "T"})
    monkeypatch.setattr(wiki_pack, "api_fetcher", lambda log: "fetcher")

    def text(qids, lg, c, fetch=None, langs=(), log=None):
        calls["text"].append(lg)
        done = lg == "de" or calls["text"].count(lg) >= 2       # el completes on its second round
        return {}, {"qids": 2, "asked": 2, "requests": 1, "missing": 0 if done else 1, "complete": done}
    monkeypatch.setattr(wiki_pack, "wikidata_text", text)
    monkeypatch.setattr(prefetch.time, "sleep", lambda s: None)
    monkeypatch.setattr(prefetch, "pack_iso639_3", lambda lg: lg)
    rc = prefetch.main(["base.zim", "--langs", "de,el", "--cache", str(tmp_path), "--text-rounds", "3"])
    assert rc == 0 and calls["text"] == ["de", "el", "el"]
    assert ("de", "redirect") in calls["dump"]
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    # every title map first, then a line saying so, then the text: a run cut short lacks only text
    assert out[0] == {"titles_done": True, "failed": []} and [r["lang"] for r in out[1:3]] == ["de", "el"]
    assert {k: out[-1][k] for k in ("failed", "text_incomplete")} == {"failed": [], "text_incomplete": []}
    order: list[str] = []
    monkeypatch.setattr(wiki_pack, "build_title_map", lambda lg, c, **k: order.append(f"titles {lg}"))
    monkeypatch.setattr(wiki_pack, "wikidata_text", lambda q, lg, c, fetch=None, langs=(), log=None:
                        (order.append(f"text {lg}"), {"qids": 2, "asked": 0, "requests": 0, "missing": 0, "complete": True})[1:] and
                        ({}, {"qids": 2, "asked": 0, "requests": 0, "missing": 0, "complete": True}))
    assert prefetch.main(["base.zim", "--langs", "de,el", "--cache", str(tmp_path), "--text-rounds", "1"]) == 0
    assert order == ["titles de", "titles el", "text de", "text el"], order
    capsys.readouterr()
    monkeypatch.setattr(wiki_pack, "wikidata_text", text)
    # a language whose dumps fail: exit 1 (its text complete)
    assert prefetch.main(["base.zim", "--langs", "zz", "--cache", str(tmp_path), "--text-rounds", "1"]) == 1
    assert _summary(capsys) == {"failed": ["zz"], "text_incomplete": ["zz"]}
    # text that never completes: exit 1 (its dumps fine)
    monkeypatch.setattr(wiki_pack, "wikidata_text", lambda q, lg, c, fetch=None, langs=(), log=None:
                        ({}, {"qids": 2, "asked": 2, "requests": 1, "missing": 1, "complete": False}))
    assert prefetch.main(["base.zim", "--langs", "de", "--cache", str(tmp_path), "--text-rounds", "2"]) == 1
    assert _summary(capsys) == {"failed": [], "text_incomplete": ["de"]}


def _summary(capsys: pytest.CaptureFixture[str]) -> dict:
    """prefetch.py's summary line: which title maps failed, which texts are incomplete."""
    last = json.loads([x for x in capsys.readouterr().out.splitlines() if x.startswith("{")][-1])
    return {k: last[k] for k in ("failed", "text_incomplete")}


def test_prefetch_asks_wikidata_once_for_every_pack_language(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A region language outside the default label languages (Romansh) is
    in the first pass: 120 Q-IDs are 3 requests, not 3 more on rm's turn
    (Switzerland asked Wikidata twice for 67,954 Q-IDs, ~22 min each)."""
    from streetzim import wiki_pack
    from tools.langpack import prefetch

    class B:
        uuid, qids = "u", [f"Q{i}" for i in range(1, 121)]
    monkeypatch.setattr(wiki_pack, "read_base", lambda *a, **k: B())
    monkeypatch.setattr(wiki_pack, "build_title_map", lambda lg, c, **k: None)
    monkeypatch.setattr(wiki_pack, "fetch_dump", lambda lg, table, c, **k: None)
    monkeypatch.setattr(wiki_pack, "load_titles", lambda lg, q, c, **k: {})
    asked: list[int] = []

    def fetch(batch, codes):
        asked.append(len(batch))
        return {q: {"labels": {c: {"value": f"{q}-{c}"} for c in codes}} for q in batch}
    monkeypatch.setattr(wiki_pack, "api_fetcher", lambda log: fetch)
    monkeypatch.setattr(prefetch, "pack_iso639_3", lambda lg: lg)
    assert prefetch.main(["base.zim", "--langs", "de,rm", "--cache", str(tmp_path), "--text-rounds", "1"]) == 0
    assert asked == [50, 50, 20], asked
    rm = [json.loads(x) for x in (tmp_path / "wikidata-text" / "rm.jsonl").read_text().splitlines()]
    assert len(rm) == 120 and rm[0][1] == "Q1-rm", rm[:2]


def test_prefetch_refuses_an_unknown_language(tmp_path: Path) -> None:
    r = tool("prefetch.py", tmp_path / "missing.zim", "--langs", "de,qq", "--cache", tmp_path)
    assert r.returncode == 2 and "qq" in r.stderr



def test_titles_are_cut_in_utf16_units(tmp_path: Path) -> None:
    """Review r3 L-7: a fallback Title is cut to 30 UTF-16 units, not code
    points: merge_zim.py refuses anything longer."""
    from tools.langpack.region_meta import cut16, utf16
    t = "OSM - " + "\U0001F5FA" * 20                    # astral characters count 2 units each
    assert utf16(cut16(t, 30)) <= 30 and cut16(t, 30) == "OSM - " + "\U0001F5FA" * 12
    b = _base(tmp_path / "b.zim", PLACES, GEO, {"name": "Testland", "hasWikiArticles": True}, title=t)
    m = _meta(b, _wiki(tmp_path, ["de", "en"]), "de,en")
    assert all(utf16(v) <= 30 for v in m["titles"].values()), m["titles"]
