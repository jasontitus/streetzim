"""Wikipedia packs (streetzim/wiki_pack.py, docs/language-matrix-prototype.md).

The fixtures are small ZIMs written with python-libzim's Creator: a French
"Kiwix Wikipedia" (articles with links, pictures and coordinates, a redirect,
a disambiguation page, a title with a slash, an article under its old name),
a StreetZim base (map-config, search records with `q`, a wikidata chunk, an
illustration) and frwiki SQL dumps in both of the formats Wikimedia has
written. The pack is built once (with a worker pool) and read back with
python-libzim; zimcheck runs when $ZIMCHECK_BIN or PATH has one.
"""
from __future__ import annotations

import gzip
import io
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("libzim")
from libzim.reader import Archive  # noqa: E402
from libzim.writer import Creator, Hint, Item, StringProvider  # noqa: E402

from cloud import wiki_articles  # noqa: E402
from streetzim import wiki_pack  # noqa: E402

LANG = "fr"
PRE = "~w/fr/"
REGION_NAME = "Testland"


class _Item(Item):
    def __init__(self, path: str, title: str, mime: str, data: bytes | str, front: bool = False):
        super().__init__()
        self._p, self._t, self._m = path, title, mime
        self._d = data.encode() if isinstance(data, str) else data
        self._front = front

    def get_path(self) -> str: return self._p
    def get_title(self) -> str: return self._t
    def get_mimetype(self) -> str: return self._m
    def get_contentprovider(self) -> Any: return StringProvider(self._d)
    def get_hints(self) -> dict[Any, bool]: return {Hint.FRONT_ARTICLE: self._front}


def _png(side: int = 48) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (side, side), (200, 30, 30, 255)).save(buf, "PNG")
    return buf.getvalue()


def _webp(seed: int) -> bytes:
    """A real WebP over 1500 bytes (smaller ones count as icons)."""
    from PIL import Image
    img = Image.effect_noise((96, 96), 60 + seed).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=90)
    data = buf.getvalue()
    assert len(data) > 1500
    return data


def _article(title: str, body: str, geo: tuple[float, float] | None = None) -> str:
    meta = f'<meta name="geo.position" content="{geo[0]};{geo[1]}">' if geo else ""
    return (f"<html><head><meta charset=\"UTF-8\"><title>{title}</title>{meta}</head><body>"
            f'<div class="mw-parser-output">{body}</div></body></html>')


# What the fixture Wikipedia holds (paths as a Kiwix ZIM stores them).
ALPHA = _article("Alpha Town", (
    '<table class="infobox"><tr><td><img src="./_assets_/abc/Alpha_view.webp" width="250">'
    '</td></tr></table>'
    "<p><b>Alpha Town</b> est une ville du Testland, au pied du "
    '<a href="Beta_Peak" title="Beta Peak">Beta</a>, au bord du '
    '<a href="Gamma" class="mw-redirect" title="Gamma">lac Gamma</a>. Elle a une gare.</p>'
    '<p>Voir <a href="AC/DC_Place">la place</a>, <a href="Not_In_Pack">autre chose</a>, '
    '<a href="Disambig_Place">Disambig</a>, <a href="Far_Away#Histoire">loin</a>, '
    '<a href="Old_Name">l\'ancien nom</a>, <a href="Beta_Peak#Climat">le climat</a>, '
    '<a href="#cite_note-1">[1]</a> et <a href="https://example.org/x">un site</a>.</p>'
    '<p>Le <a href="Beta_Peak"><img src="./_assets_/abc/tiny.webp"></a> est haut.</p>'
    '<p>Liens : <a href="./Gamma_Lake">lac</a> ; <a href="//fr.wikipedia.org/wiki/Alpha">web</a>.</p>'
), geo=(10.0, 20.0))
BETA = _article("Beta Peak", (
    '<figure><img src="./_assets_/abc/Beta.webp" width="300"><figcaption>Le sommet</figcaption>'
    "</figure><p>Le <b>Beta Peak</b> est une montagne près d'"
    '<a href="Alpha_Town">Alpha Town</a>. Son altitude est grande.</p>'), geo=(10.5, 20.5))
GAMMA = _article("Gamma Lake", "<p>Le <b>lac Gamma</b> est un lac. Il est profond.</p>",
                 geo=(10.2, 20.2))
ACDC = _article("AC/DC Place", (
    '<p>La <b>place AC/DC</b> est à <a href="../Alpha_Town">Alpha Town</a>. '
    '<img src="../_assets_/abc/Place.webp" width="200"> Elle est grande.</p>'), geo=(10.1, 20.1))
OLD = _article("Old Name", "<p>Le <b>Old Name</b> est un village renommé depuis.</p>",
               geo=(10.3, 20.3))
DISAMBIG = _article("Disambig Place", "<p><b>Disambig Place</b> may refer to:</p><ul><li>x</li></ul>",
                    geo=(10.4, 20.4))
UNPLACED = _article("Unplaced Thing", "<p>Une chose sans coordonnées. Rien de plus ici.</p>")
FAR = _article("Far Away", "<p>Un endroit lointain hors de la carte. Vraiment loin.</p>",
               geo=(-40.0, 150.0))
DELTA = _article("Delta Old", "<p>Le <b>Delta</b> est un hameau du Testland. Il est petit.</p>",
                 geo=(10.7, 20.7))
NOTIN = _article("Not In Pack", "<p>Un article sans Q-ID de la carte. Aucun lien ici.</p>",
                 geo=(10.0, 20.0))


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    d = tmp_path_factory.mktemp("wikipack")
    images = {"Alpha_view.webp": _webp(1), "Beta.webp": _webp(2), "Place.webp": _webp(3),
              "tiny.webp": b"RIFF\x10\x00\x00\x00WEBPVP8 tiny"}
    src = d / "wikipedia_fr_test_2026-01.zim"
    with Creator(str(src)).config_indexing(False, "fra") as c:
        c.set_mainpath("Alpha_Town")
        for k, v in (("Language", "fra"), ("Title", "Wikipédia"), ("Name", "wikipedia_fr_test")):
            c.add_metadata(k, v)
        for path, title, html in (("Alpha_Town", "Alpha Town", ALPHA), ("Beta_Peak", "Beta Peak", BETA),
                                  ("Gamma_Lake", "Gamma Lake", GAMMA), ("AC/DC_Place", "AC/DC Place", ACDC),
                                  ("Old_Name", "Old Name", OLD), ("Disambig_Place", "Disambig Place", DISAMBIG),
                                  ("Unplaced_Thing", "Unplaced Thing", UNPLACED), ("Far_Away", "Far Away", FAR),
                                  ("Not_In_Pack", "Not In Pack", NOTIN)):
            c.add_item(_Item(path, title, "text/html", html, front=True))
        for name, data in images.items():
            c.add_item(_Item(f"_assets_/abc/{name}", "", "image/webp", data))
        c.add_redirection("Gamma", "Gamma", "Gamma_Lake", {Hint.FRONT_ARTICLE: False})
        # Q30's article is newer than this ZIM, which redirects its title to
        # another subject's article (Q3's): not Q30's article.
        c.add_redirection("Gamma_(lac)", "Gamma (lac)", "Gamma_Lake", {Hint.FRONT_ARTICLE: False})
        # Q15's article was renamed Delta_Old -> Delta_New over a redirect.
        c.add_item(_Item("Delta_Old", "Delta Old", "text/html", DELTA, front=True))
        c.add_redirection("Delta_New", "Delta New", "Delta_Old", {Hint.FRONT_ARTICLE: False})

    base = d / "osm-testland-2026-10-01.zim"
    illustration = _png()
    records = [
        {"n": "Alpha Town", "t": "place", "s": "town", "a": 10.000001, "o": 20.000001, "l": "", "q": "Q1"},
        {"n": "Alpha Town", "t": "poi", "s": "", "a": 11.0, "o": 21.0, "l": "", "q": "Q1"},
        {"n": "Gamma Lake", "t": "water", "s": "lake", "a": 10.2, "o": 20.2, "l": "", "q": "Q3"},
        {"n": "Gamma", "t": "poi", "s": "", "a": 10.25, "o": 20.25, "l": "", "q": "Q30"},
        {"n": "AC/DC Place", "t": "poi", "s": "square", "a": 10.1, "o": 20.1, "l": "", "q": "Q9"},
        {"n": "Missing", "t": "poi", "s": "", "a": 10.6, "o": 20.6, "l": "", "q": "Q10"},
        {"n": "Bad", "t": "poi", "s": "", "a": 10.6, "o": 20.6, "l": "", "q": "Q01"},
        {"n": "No q", "t": "poi", "s": "", "a": 10.6, "o": 20.6, "l": ""},
        {"n": "Search Only", "t": "poi", "s": "", "a": 10.8, "o": 20.8, "l": "", "q": "Q78"},
        {"n": "Testland County", "t": "admin", "s": "county", "a": 10.9, "o": 20.9, "l": "",
         "q": "Q20", "al": 6},
    ]
    # The base's own geo-index ([lat, lon, type, qid, desc]; an older row
    # has no Q-ID): it places the POIs the category files do not hold.
    base_geo = {"AC/DC_Place": [10.1, 20.1, "poi", "Q9", "a square"],
                "Gamma": [10.25, 20.25, "poi", "Q30", ""],
                "Missing": [10.6, 20.6, "poi", "Q10", ""], "Old_Row": [10.6, 20.6, "poi"]}
    wikidata = {"Q2": {"l": "Beta Peak", "i": "mountain", "e": 3000},
                "Q5": {"l": "New Name", "i": "village", "p": 300},
                "Q6": {"l": "Disambig"}, "Q7": {"l": "Unplaced", "i": "monument"},
                "Q8": {"l": "Far", "i": "city", "p": 5}, "Q11": {"l": "Nothing", "i": "bridge"},
                "Q15": {"l": "Delta", "i": "hamlet"}}
    with Creator(str(base)).config_indexing(False, "eng") as c:
        c.set_mainpath("index.html")
        c.add_metadata("Title", "OSM - Testland")
        c.add_metadata("Language", "eng")
        c.add_illustration(48, illustration)
        c.add_item(_Item("index.html", "Map", "text/html", "<html><body>map</body></html>"))
        c.add_item(_Item("map-config.json", "", "application/json",
                         json.dumps({"name": REGION_NAME, "bounds": [19.0, 9.0, 22.0, 12.0]})))
        c.add_item(_Item("search-data/al.json", "", "application/json", json.dumps(records[:2])))
        c.add_item(_Item("search-data/ga.json", "", "application/json", json.dumps(records[2:])))
        c.add_item(_Item("search-data/manifest.json", "", "application/json",
                         json.dumps({"total": 8, "chunks": {"al": 2, "ga": 6}})))
        c.add_item(_Item("wikidata/10.json", "", "application/json", json.dumps(wikidata)))
        c.add_item(_Item("wiki-geo-index.json", "", "application/json", json.dumps(base_geo)))
        c.add_item(_Item("category-index/place.json", "", "application/json",
                         json.dumps([records[0]])))
        c.add_item(_Item("category-index/water-g000.json", "", "application/json",
                         json.dumps([records[2]])))
        c.add_item(_Item("category-index/admin.json", "", "application/json",
                         json.dumps([records[-1]])))
        c.add_item(_Item("category-index/chip-food-g000.json", "", "application/json",
                         json.dumps([{**records[4], "q": "Q77"}])))   # a chip: not read
        c.add_item(_Item("wikidata/manifest.json", "", "application/json", json.dumps({"total": 6})))

    cache = d / "cache"
    dumps = cache / "dumps"
    dumps.mkdir(parents=True)
    head = "-- MariaDB dump\n/*!40101 SET NAMES binary*/;\nCREATE TABLE `x` (\n  `a` int\n);\n"
    props = (head + "INSERT INTO `page_props` VALUES\n"
             "(101,'wikibase_item','Q1',NULL),\n(101,'page_image_free','Alpha.jpg',NULL),\n"
             "(102,'wikibase_item','Q2',NULL),\n(103,'wikibase_item','Q3',NULL),\n"
             "(105,'wikibase_item','Q5',NULL),\n(106,'wikibase_item','Q6',NULL),\n"
             "(107,'wikibase_item','Q7',NULL),\n(108,'wikibase_item','Q8',NULL),\n"
             "(109,'wikibase_item','Q9',NULL),\n(110,'wikibase_item','Q10',NULL),\n"
             "(130,'wikibase_item','Q30',NULL),\n(140,'wikibase_item','Q40',NULL),\n"
             "(170,'wikibase_item','Q70',NULL),\n(115,'wikibase_item','Q15',NULL),\n"
             "(150,'wikibase_item','Q12',NULL),\n(200,'wikibase_item','Q1000',NULL);\n")
    # The page dump in the older format: every row on one INSERT line.
    rows = [(101, 0, "Alpha_Town", 0), (102, 0, "Beta_Peak", 0), (103, 0, "Gamma_Lake", 0),
            (105, 0, "New_Name", 0), (106, 0, "Disambig_Place", 0), (107, 0, "Unplaced_Thing", 0),
            (108, 0, "Far_Away", 0), (109, 0, "AC/DC_Place", 0), (110, 0, "Missing_Article", 0),
            (130, 0, "Gamma_(lac)", 0), (140, 0, "L\\'Île_aux_\\\"Quotes\\\"", 0),
            (170, 0, "Redirected_Page", 1), (162, 2, "Bob", 1),
            (115, 0, "Delta_New", 0), (116, 0, "Delta_Old", 1),
            (150, 4, "Projet:Q12", 0), (160, 0, "Old_Name", 1), (200, 0, "Elsewhere", 0)]
    page = head + "INSERT INTO `page` VALUES " + ",".join(
        f"({pid},{ns},'{t}',{red},0,0.5,'20261001000000','20261001000000',1,100,'wikitext',NULL)"
        for pid, ns, t, red in rows) + ";\n"
    redirect = (head + "INSERT INTO `redirect` VALUES\n(170,0,'Gamma_Lake','',''),\n"
                "(160,0,'New_Name','',''),\n(161,4,'New_Name','',''),\n"
                "(162,0,'New_Name','','');\n")     # from User:Bob: not an old title
    for table, text in (("page_props", props), ("page", page), ("redirect", redirect)):
        with gzip.open(dumps / f"frwiki-latest-{table}.sql.gz", "wt", encoding="utf-8") as f:
            f.write(text)

    asked: list[list[str]] = []

    def fetch(ids: list[str], codes: list[str]) -> dict[str, Any]:
        asked.append(list(ids))
        assert "fr" in codes and "zh-hans" in codes
        ents: dict[str, Any] = {}
        for q in ids:
            if q == "Q11":
                continue                                  # Wikidata: missing
            if q == "Q30":                                # merged into Q3
                ents["Q30"] = {"id": "Q3", "redirects": {"from": "Q30", "to": "Q3"},
                              "labels": {"fr": {"value": "Lac Gamma"}}}
                continue
            labels = {"fr": {"language": "fr", "value": f"Lieu {q}"},
                      "zh-hans": {"value": f"地方 {q}"}}
            descs = {"fr": {"value": f"lieu numéro {q[1:]}"}} if q != "Q2" else {}
            ents[q] = {"id": q, "labels": labels, "descriptions": descs}
        return ents

    out = d / "packs" / "osm_testland_wiki-fr_2026-10-13.zim"
    logs: list[str] = []
    res = wiki_pack.build_pack(base, lang=LANG, source=src, region="testland", output=out,
                               cache_dir=cache, images="all", fetch=fetch, jobs=2,
                               date="2026-10-13", log=logs.append)
    return {"dir": d, "src": src, "base": base, "cache": cache, "pack": out, "stats": res.stats,
            "illustration": illustration, "asked": asked, "fetch": fetch, "logs": logs}


def _read(world: dict[str, Any], path: str) -> bytes:
    return bytes(Archive(world["pack"]).get_entry_by_path(path).get_item().content)


def _page(world: dict[str, Any], title: str) -> str:
    return _read(world, PRE + "wiki-article/" + title).decode()


def test_every_path_is_under_the_prefix_and_the_pack_opens(world: dict[str, Any]) -> None:
    a = Archive(world["pack"])
    assert a.check() is True
    content = [a._get_entry_by_id(i).path for i in range(a.entry_count)]
    assert content and all(p.startswith(PRE) for p in content), content
    assert {PRE + "wiki-geo-index.json", PRE + "pack.json", PRE + "wikidata-text.json"} <= set(content)
    assert not [p for p in content if p.endswith("map-config.json")]   # the merger writes it
    others = {a._get_entry_by_id(i).path for i in range(a.entry_count, a.all_entry_count)}
    assert "listing/titleOrdered/v1" in others
    assert not ({"title/xapian", "fulltext/xapian"} & others)
    assert not a.has_title_index and not a.has_fulltext_index
    articles = sorted(p[len(PRE + "wiki-article/"):] for p in content
                      if p.startswith(PRE + "wiki-article/"))
    assert articles == ["AC/DC_Place", "Alpha_Town", "Beta_Peak", "Delta_Old", "Gamma_Lake",
                        "Old_Name"]
    # Front articles: the v1 listing holds exactly the pages.
    assert a.article_count == len(articles)
    main = a.main_entry
    assert (main.get_redirect_entry() if main.is_redirect else main).path == \
        PRE + "wiki-article/AC/DC_Place"
    assert a.get_entry_by_path(PRE + "wiki-article/Alpha_Town").title == "Alpha Town"


def test_geo_index_rows_carry_the_qid(world: dict[str, Any]) -> None:
    geo = json.loads(_read(world, PRE + "wiki-geo-index.json"))
    assert set(geo) == {"AC/DC_Place", "Alpha_Town", "Beta_Peak", "Delta_Old", "Gamma_Lake",
                        "Old_Name"}
    # The base's search record places it (the place record, not the POI one).
    # The base's own row order: [lat, lon, type, qid, desc].
    assert geo["Alpha_Town"][:4] == [10.0, 20.0, "place", "Q1"]
    assert geo["Alpha_Town"][4].startswith("Alpha Town est une ville du Testland")
    # No record: the article's coordinates, the type from the Wikidata entry.
    assert geo["Beta_Peak"] == [10.5, 20.5, "peak", "Q2",
                                "Le Beta Peak est une montagne près d'Alpha Town."]
    # Q30's title leads to Q3's article: Q3 holds it, Q30 has none.
    assert geo["Gamma_Lake"][3] == "Q3" and geo["Gamma_Lake"][2] == "water"
    # Q15's title is a redirect to its own article under the old name.
    assert geo["Delta_Old"][3] == "Q15" and geo["Delta_Old"][2] == "place"
    # Q5's current title is New_Name; the older ZIM has it as Old_Name.
    assert geo["Old_Name"][3] == "Q5" and geo["Old_Name"][2] == "place"
    assert geo["AC/DC_Place"][3] == "Q9"
    for row in geo.values():
        assert len(row) == 5 and isinstance(row[0], float) and isinstance(row[1], float)


def test_pack_json_and_counts(world: dict[str, Any]) -> None:
    meta = json.loads(_read(world, PRE + "pack.json"))
    assert meta == {"lang": "fr", "region": "testland", "base_uuid": str(Archive(world["base"]).uuid),
                    "articles": 6, "images": 3, "source": world["src"].name, "built": "2026-10-13"}
    s = world["stats"]
    assert (s["base_qids"], s["sitelinks"], s["not_in_zim"], s["found_under_old_title"]) == \
        (13, 11, 1, 1)
    assert (s["through_redirect"], s["redirect_to_other_article"]) == (1, 1)
    assert s["redirect_to_other_article_list"] == [["Q30", "Gamma_(lac)", "Gamma_Lake"]]
    assert s["not_in_zim_list"] == [["Q10", "Missing_Article"]]
    assert (s["disambiguation"], s["unplaced"], s["outside_bounds"]) == (1, 1, 1)
    assert s["shared_article"] == 0 and s["articles"] == 6


def test_internal_links_are_rewritten(world: dict[str, Any]) -> None:
    alpha = _page(world, "Alpha_Town")
    links = re.findall(r'<a href="([^"]*)">([^<]*)</a>', alpha)
    internal = [(h, t) for h, t in links if not h.startswith("http")]
    assert internal == [("./Beta_Peak", "Beta"), ("./Gamma_Lake", "lac Gamma"),
                        ("./AC/DC_Place", "la place"), ("./Old_Name", "l'ancien nom"),
                        ("./Beta_Peak", "le climat"), ("./Gamma_Lake", "lac")]
    # Not in the pack, left out of it, external, a note, an image-only link: text.
    for text in ("autre chose", "Disambig", "loin", "un site", "web"):
        assert text in alpha and f">{text}</a>" not in alpha
    assert "example.org" not in alpha and "cite_note" not in alpha
    # From a title with a slash, one level deeper.
    acdc = _page(world, "AC/DC_Place")
    assert '<a href="../Alpha_Town">Alpha Town</a>' in acdc
    # Alpha -> Disambig_Place and Far_Away: candidates, left out after reading.
    assert world["stats"]["links_dropped"] == 2


def test_back_bar_climbs_out_of_the_prefix(world: dict[str, Any]) -> None:
    alpha = _page(world, "Alpha_Town")
    assert '<nav class="sz-back"><a href="../../../index.html"' in alpha
    assert '<nav class="sz-back"><a href="../../../../index.html"' in _page(world, "AC/DC_Place")
    # Pictures sit beside the articles, as in the base.
    imgs = re.findall(r'<img src="([^"]+)"', alpha)
    assert len(imgs) == 1 and re.fullmatch(r"\.\./wiki-image/[0-9a-f]{20}\.webp", imgs[0])
    Archive(world["pack"]).get_entry_by_path(PRE + "wiki-image/" + imgs[0].split("/")[-1])
    acdc_img = re.findall(r'<img src="([^"]+)"', _page(world, "AC/DC_Place"))
    assert len(acdc_img) == 1 and acdc_img[0].startswith("../../wiki-image/")


def test_pages_are_in_the_pack_language(world: dict[str, Any]) -> None:
    alpha = _page(world, "Alpha_Town")
    assert alpha.startswith('<!DOCTYPE html><html lang="fr">')
    assert 'href="https://fr.wikipedia.org/wiki/Alpha_Town"' in alpha
    assert wiki_articles._LANG == "en"                 # the base build's global untouched


def test_metadata_limits_and_illustration(world: dict[str, Any]) -> None:
    a = Archive(world["pack"])

    def m(k: str) -> str:
        return bytes(a.get_metadata(k)).decode()
    assert m("Name") == "osm_testland_wiki-fr"
    assert m("Language") == "fra"
    assert m("Title") == "Testland Wikipedia (fr)" and wiki_pack.utf16_len(m("Title")) <= 30
    assert len(m("Description")) <= 80
    assert m("Date") == "2026-10-13" and m("Creator") and m("Publisher")
    assert bytes(a.get_metadata("Illustration_48x48@1")) == world["illustration"]


def test_title_fits_in_utf16_units() -> None:
    # Astral characters are two UTF-16 units each: the limit is on units.
    t = wiki_pack.fit("{name} Wikipedia (th)", "𝔗𝔥𝔢 𝔙𝔢𝔯𝔶 𝔏𝔬𝔫𝔤 𝔑𝔞𝔪𝔢", 30, wiki_pack.utf16_len)
    assert wiki_pack.utf16_len(t) <= 30 and t.endswith("… Wikipedia (th)")
    assert wiki_pack.fit("{name} Wikipedia (de)", "Greece", 30, wiki_pack.utf16_len) == \
        "Greece Wikipedia (de)"


def test_wikidata_text(world: dict[str, Any]) -> None:
    text = json.loads(_read(world, PRE + "wikidata-text.json"))
    # Every Q-ID of the base with fr text, article or not (Q11: Wikidata has none).
    assert set(text) == {"Q1", "Q2", "Q3", "Q5", "Q6", "Q7", "Q8", "Q9", "Q10", "Q15", "Q20",
                         "Q30"}
    assert text["Q1"] == ["Lieu Q1", "lieu numéro 1"]
    assert text["Q2"] == ["Lieu Q2", ""]
    assert text["Q30"] == ["Lac Gamma", ""]            # answered under the item it was merged into
    s = world["stats"]["wikidata_text"]
    assert (s["labels"], s["descriptions"], s["requests"], s["complete"]) == (12, 10, 1, True)
    # Every language was cached at once: zh needs no request.
    n = len(world["asked"])
    got, st = wiki_pack.wikidata_text(["Q1", "Q2", "Q11"], "zh", world["cache"],
                                      fetch=world["fetch"])
    assert len(world["asked"]) == n and st["requests"] == 0
    assert got == {"Q1": ["地方 Q1", ""], "Q2": ["地方 Q2", ""]}


def test_wikidata_text_halves_a_refused_batch(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fetch(ids: list[str], codes: list[str]) -> dict[str, Any]:
        calls.append(ids)
        if "Q13" in ids:
            raise ValueError("wbgetentities: no-such-entity")
        return {q: {"id": q, "labels": {"de": {"value": q}}} for q in ids}
    got, st = wiki_pack.wikidata_text([f"Q{i}" for i in range(1, 21)], "de", tmp_path,
                                      fetch=fetch, langs=("de",))
    assert set(got) == {f"Q{i}" for i in range(1, 21)} - {"Q13"}
    assert st["complete"] and len(calls) > 1 and st["refused"] == ["Q13"]
    # A refused id is not cached as "no text": the next run asks again.
    calls.clear()
    wiki_pack.wikidata_text([f"Q{i}" for i in range(1, 21)], "de", tmp_path,
                            fetch=fetch, langs=("de",))
    assert calls == [["Q13"]]


def test_wikidata_text_stops_when_refused(tmp_path: Path) -> None:
    import urllib.error
    calls: list[list[str]] = []

    def refuse(ids: list[str], codes: list[str]) -> dict[str, Any]:
        calls.append(ids)
        raise urllib.error.HTTPError("https://www.wikidata.org/w/api.php", 403, "Forbidden",
                                     None, None)  # type: ignore[arg-type]
    got, st = wiki_pack.wikidata_text([f"Q{i}" for i in range(1, 101)], "de", tmp_path,
                                      fetch=refuse, langs=("de",))
    assert got == {} and st["complete"] is False and len(calls) == 1
    # Nothing was cached as "no text": a working API is asked for all of them.
    got, st = wiki_pack.wikidata_text(
        [f"Q{i}" for i in range(1, 101)], "de", tmp_path, langs=("de",),
        fetch=lambda ids, codes: {q: {"labels": {"de": {"value": q}}} for q in ids})
    assert len(got) == 100 and st["requests"] == 2


def test_wikidata_text_follows_an_item_merged_away(tmp_path: Path) -> None:
    # The answer for a merged id may come keyed by the item it now is.
    def fetch(ids: list[str], codes: list[str]) -> dict[str, Any]:
        return {"Q99": {"id": "Q99", "redirects": {"from": "Q21", "to": "Q99"},
                        "labels": {"de": {"value": "Ziel"}}},
                "Q22": {"id": "Q22", "labels": {"de": {"value": "Zwei"}}}}
    got, _ = wiki_pack.wikidata_text(["Q21", "Q22"], "de", tmp_path, fetch=fetch, langs=("de",))
    assert got == {"Q21": ["Ziel", ""], "Q22": ["Zwei", ""]}


def test_offline_text_is_cache_only(tmp_path: Path) -> None:
    got, st = wiki_pack.wikidata_text(["Q1"], "de", tmp_path, fetch=None)
    assert got == {} and st["complete"] is False and st["missing"] == 1


def test_title_map_from_both_dump_formats(world: dict[str, Any]) -> None:
    tsv = wiki_pack.build_title_map("fr", world["cache"])
    rows = dict(line.split("\t") for line in tsv.read_text(encoding="utf-8").splitlines())
    assert rows["Q1"] == "Alpha_Town" and rows["Q9"] == "AC/DC_Place"
    assert rows["Q40"] == "L'Île_aux_\"Quotes\""        # SQL escapes undone
    assert rows["Q30"] == "Gamma_(lac)"
    assert "Q70" not in rows                            # a redirect is not an article
    assert "Q12" not in rows                            # namespace 4
    renamed = wiki_pack.renamed_titles("fr", ["New_Name", "Beta_Peak"], world["cache"])
    assert renamed == {"New_Name": ["Old_Name"], "Beta_Peak": []}


def test_the_gate(world: dict[str, Any]) -> None:
    res = wiki_pack.check_pack(world["pack"], LANG, wiki_pack.find_zimcheck())
    assert res["ok"], res["problems"]
    if "zimcheck" in res:
        # Only the back-to-map links may dangle: index.html is the base's.
        assert res["zimcheck"].startswith("pass"), res


def test_the_gate_catches_a_stray_path(tmp_path: Path, world: dict[str, Any]) -> None:
    bad = tmp_path / "bad.zim"
    with Creator(str(bad)).config_indexing(False, "fra") as c:
        c.set_mainpath(PRE + "wiki-article/A")
        for k, v in (("Name", "osm_x_wiki-fr"), ("Title", "x" * 31), ("Language", "fra"),
                     ("Description", "d"), ("Date", "2026-10-13"), ("Creator", "c"),
                     ("Publisher", "p")):
            c.add_metadata(k, v)
        c.add_item(_Item(PRE + "wiki-article/A", "A", "text/html", "<html><body>a</body></html>",
                         front=True))
        c.add_item(_Item("index.html", "x", "text/html", "<html><body>x</body></html>"))
        c.add_item(_Item(PRE + "map-config.json", "", "application/json", "{}"))
    res = wiki_pack.check_pack(bad, LANG)
    text = " ".join(res["problems"])
    assert not res["ok"]
    for want in ("outside ~w/fr/", "Title", "Xapian", "Illustration", "pack.json", "map-config"):
        assert want in text, (want, text)


@pytest.mark.skipif(not wiki_pack.find_zimcheck(), reason="no zimcheck here")
@pytest.mark.parametrize("link", ["./Nowhere", "../../index.html"])
def test_the_gate_fails_a_dangling_link(tmp_path: Path, link: str) -> None:
    """Only the back-to-map links at the right depth may dangle: a link to a
    missing article, or a bar link one level short (~w/index.html), fails."""
    bad = tmp_path / "dangling.zim"
    page = ('<html><head><title>A</title></head><body><nav class="sz-back">'
            '<a href="../../../index.html">map</a></nav><p>x <a href="' + link + '">y</a></p>'
            "</body></html>")
    with Creator(str(bad)).config_indexing(False, "fra") as c:
        c.set_mainpath(PRE + "wiki-article/A")
        c.add_metadata("Title", "x")
        c.add_item(_Item(PRE + "wiki-article/A", "A", "text/html", page, front=True))
    res = wiki_pack.check_pack(bad, LANG, wiki_pack.find_zimcheck())
    assert res["zimcheck"].startswith("FAIL"), res
    assert res["zimcheck_findings"].get("url_internal") == 1, res


def test_cli_rejects_an_unknown_language(capsys: pytest.CaptureFixture[str]) -> None:
    assert wiki_pack.main(["b.zim", "--lang", "xx", "--source", "s.zim", "--region", "r"]) == 2
    assert "unsupported language" in capsys.readouterr().err


def test_source_language_must_match(world: dict[str, Any], tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fra Wikipedia, not de"):
        wiki_pack.build_pack(world["base"], lang="de", source=world["src"], region="testland",
                             output=tmp_path / "x.zim", cache_dir=world["cache"], text=False)


def test_pick_source_takes_the_newest(tmp_path: Path) -> None:
    for name in ("wikipedia_de_all_maxi_2026-01.zim", "wikipedia_de_all_maxi_2025-11.zim",
                 "wikipedia_de_all_maxi_2026-09.zim.part", "wikipedia_fr_all_maxi_2026-10.zim"):
        (tmp_path / name).write_bytes(b"")
    assert wiki_pack.pick_source(str(tmp_path), "de").endswith("de_all_maxi_2026-01.zim")


# ---- the pieces ---------------------------------------------------------------

def test_clean_article_html_without_link_href_is_unchanged() -> None:
    page = wiki_articles.clean_article_html(
        '<p>See <a href="Foo">Foo</a> and <a href="Bar">Bar</a>.</p>', "T", "https://x/T")
    assert "<a href=\"Foo\"" not in page and "See Foo and Bar." in page
    assert '<a href="../index.html"' in page and 'lang="en"' in page


def test_kept_links_survive_the_whitelist() -> None:
    def keep(h: str) -> str | None:
        return {"Foo": "./Foo", "B&amp;C": None, "Q": "./Q?x=1&y=2"}.get(h)
    page = wiki_articles.clean_article_html(
        '<p><b><a href="Foo" title="t">Foo</a></b> <a href="Bar">Bar</a> '
        '<a href="Q">q&amp;a</a> <a href="Foo"><img src="x"></a> '
        '<a href="Foo">open</p>', "T", "u", link_href=keep, lang="de", prefix_depth=2)
    body = page.split("</h1>", 1)[1]
    assert "<b><a href=\"./Foo\">Foo</a></b> Bar" in body
    assert '<a href="./Q?x=1&amp;y=2">q&amp;a</a>' in body
    assert body.count("<a ") == body.count("</a>")    # the unclosed one is closed
    assert '<a href="../../../index.html"' in page and 'lang="de"' in page
    assert "" not in page and "" not in page


def test_relative_links_and_excerpts() -> None:
    assert wiki_pack.relative_link("A", "B") == "./B"
    assert wiki_pack.relative_link("A/B", "C") == "../C"
    assert wiki_pack.relative_link("A", "X/Y") == "./X/Y"
    # To an ancestor title: up to wiki-article/ and down, never "./." (a folder).
    assert wiki_pack.relative_link("Foo/Bar", "Foo") == "../Foo"
    assert wiki_pack.relative_link("Foo/Bar/Baz", "Foo") == "../../Foo"
    assert wiki_pack.relative_link("A/B", "A/C") == "../A/C"
    assert wiki_pack.relative_link("A", "Gr%C3%A8ce?") == "./Gr%25C3%25A8ce%3F"
    assert wiki_pack.relative_link("A", "Grèce_(pays)") == "./Gr%C3%A8ce_(pays)"
    assert wiki_pack.excerpt("<h1>x</h1><p>Die Moschee bzw. Kirche ist alt. Sie steht in "
                             "Tirana und wurde 1600 gebaut. Mehr.</p>") == \
        "Die Moschee bzw. Kirche ist alt. Sie steht in Tirana und wurde 1600 gebaut."
    assert wiki_pack.excerpt("<h1>x</h1><p>アテネはギリシャの首都である。人口は多い。</p>") == \
        "アテネはギリシャの首都である。"
    long = wiki_pack.excerpt("<h1>x</h1><p>" + "mot " * 80 + "</p>")
    assert len(long) <= 160 and long.endswith("…")
    assert wiki_pack.geo_position('<meta name="geo.position" content="40.63;22.95">') == \
        (40.63, 22.95)
    assert wiki_pack.geo_position('<meta name="geo.position" content="0;0">') is None
    assert wiki_pack.guess_type({"i": "human settlement", "p": 10}) == "place"
    assert wiki_pack.guess_type({"i": "mountain"}) == "peak"
    assert wiki_pack.guess_type({"i": "church building"}) == "poi"
    assert wiki_pack.guess_type({"i": "Natura 2000 site", "d": "mountain in Elassona municipality",
                                 "e": 2918}) == "peak"
    assert wiki_pack.guess_type({"d": "beach in Crete, Greece"}) == "poi"
    assert wiki_pack.excerpt("<h1>x</h1><p>Thessalonique (en grec moderne : Θεσσαλονίκη "
                             "(Thessaloníki), /θe.sa.lo.ˈni.ci/) est la deuxième ville de Grèce. "
                             "Elle est grande.</p>") == "Thessalonique est la deuxième ville de Grèce."
    assert wiki_pack.excerpt("<h1>x</h1><p>东京（日语：東京）是日本的首都，人口众多。</p>") == \
        "东京是日本的首都，人口众多。"


def test_read_base_light_and_full(world: dict[str, Any], tmp_path: Path) -> None:
    light = wiki_pack.read_base(world["base"], tmp_path, log=lambda m: None)
    assert light.placed_from == "category files + geo-index"
    assert light.places["Q1"] == (10.0, 20.0, "place")       # place.json
    assert light.places["Q3"] == (10.2, 20.2, "water")       # a water shard
    assert light.places["Q9"] == (10.1, 20.1, "poi")         # the base's geo-index
    assert light.places["Q20"] == (10.9, 20.9, "admin")      # admin.json
    assert "Q77" not in light.qids and "Q78" not in light.qids
    full = wiki_pack.read_base(world["base"], tmp_path, log=lambda m: None, full_scan=True)
    assert full.placed_from == "search records" and full.places["Q78"] == (10.8, 20.8, "poi")
    assert "Q77" not in full.qids
    # A cached full scan serves later light reads too.
    again = wiki_pack.read_base(world["base"], tmp_path, log=lambda m: None)
    assert again.placed_from == "search records" and "Q78" in again.places
    # A cache from an older guess_type keeps its places and guesses again.
    import gzip
    cached = next((tmp_path / "bases").glob("*[0-9a-f].json.gz"))
    with gzip.open(cached, "rt") as f:
        old = json.load(f)
    old["kinds"] = dict.fromkeys(old["kinds"], "street")
    old.pop("kinds_version")
    with gzip.open(cached, "wt") as f:
        json.dump(old, f)
    redo = wiki_pack.read_base(world["base"], tmp_path, log=lambda m: None)
    assert redo.kinds == full.kinds and redo.places == full.places


REAL_BASE = Path("/storage/streetzim/sz-tests/cli-clip-e2e")


@pytest.mark.skipif(not list(REAL_BASE.glob("**/*.zim")), reason="no real Liechtenstein ZIM here")
def test_read_base_on_a_real_streetzim(tmp_path: Path) -> None:
    zim = sorted(REAL_BASE.glob("**/*.zim"), key=lambda p: p.stat().st_size)[-1]
    base = wiki_pack.read_base(zim, tmp_path, log=lambda m: None)
    assert base.places and base.bounds and base.illustration
    assert all(wiki_pack._QID_RE.fullmatch(q) for q in base.qids)
    again = wiki_pack.read_base(zim, tmp_path, log=lambda m: None)   # from the cache
    assert again.places == base.places and again.kinds == base.kinds
