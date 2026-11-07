"""The site in every language of web/i18n/ (web/generate.py LANGUAGES): a home
page per language listing every map in its words, a page per map per
language, the links between the versions (hreflang, the picker, the
sitemap), and the downloads in each language (the build worker's file, the
builder page when it cannot serve it)."""
import importlib.util
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen_lang", ROOT / "web" / "generate.py")
gen = importlib.util.module_from_spec(spec)
sys.modules["gen_lang"] = gen
spec.loader.exec_module(gen)
sys.path.insert(0, str(ROOT / "tools"))
import check_site_i18n  # noqa: E402

LANGS = {"ar", "de", "el", "en", "es", "fa", "fr", "he", "hi", "id", "it", "ja", "ko", "nl", "pl", "pt",
         "ru", "th", "tr", "uk", "vi", "zh"}
ORIGIN = "https://streetzim-build-proxy.tiltastech.workers.dev"
# a map with a version in every language, one with none (on the builder page in no language)
WITH, WITHOUT = "kyiv", "egypt"
UUID = "67fc002e-3ff7-581b-adce-7a931825664f"


def _recipes(root: Path, langs=LANGS) -> None:
    d = root / WITH
    d.mkdir(parents=True)
    menu = []
    for lang in sorted(langs):
        menu.append({"lang": lang, "label": lang, "size": 108694223,
                     "webseed": f"{ORIGIN}/merged/{WITH}/{lang}/{UUID}/osm_{lang}_{WITH}_2026-10-31.v9b2a21ba.zim"})
        (d / f"{lang}.ws.json").write_text("{}")
        (d / f"{lang}.torrent").write_text("")
    (d / "region.json").write_text(json.dumps({
        "id": WITH, "title": "Kyiv", "menu": menu,
        "base": {"file": "osm-kyiv-2026-10-31.zim", "size": 80926812, "md5": "c01fcb2c8aa20411ad68b257816d1411"}}))


def _mock(mp, tmp_path, ids):
    mp.setattr(gen, "SCRIPT_DIR", str(tmp_path))
    (tmp_path / "drive").mkdir(exist_ok=True)
    (tmp_path / "drive" / "preview-config.js").write_text(f"window.STREETZIM_MERGED_ORIGIN = '{ORIGIN}';\n")
    if not (tmp_path / "build" / "recipes").exists():
        _recipes(tmp_path / "build" / "recipes")
    mp.setattr(gen, "fetch_archive_items", lambda: {i: {"item_size": 1} for i in ids})

    def details(item_id, **kw):
        rid = item_id[len("streetzim-"):]
        if rid == WITH:
            f = {"name": "osm-kyiv-2026-10-31.zim", "size": "80926812", "md5": "c01fcb2c8aa20411ad68b257816d1411"}
        else:
            f = {"name": f"osm-{rid}-2026-10-06.zim", "size": "5000000000", "md5": "0" * 32}
        return {"files": [f], "metadata": {k: "yes" for k, _l, _c in gen.FEATURE_BADGES}}
    mp.setattr(gen, "fetch_item_details", details)


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    out = tmp_path_factory.mktemp("site")
    with pytest.MonkeyPatch.context() as mp:
        _mock(mp, out, [r["id"] for r in gen.REGIONS])
        gen.build_page()
    return out


def _page(site, lang, path=""):
    return (site / ("" if lang == "en" else f"{lang}/") / path / "index.html").read_text(encoding="utf-8")


class _Visible(HTMLParser):
    """Text a reader sees or hears: text nodes outside script/style, and
    title / placeholder / aria-label / meta content attributes."""
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        for k, v in attrs:
            if k in ("title", "placeholder", "aria-label", "content") and v:
                self.out.append(v.strip())

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.out.append(" ".join(data.split()))


def _visible(html):
    v = _Visible()
    v.feed(html)
    return v.out


def _chrome(html):
    """A home page without its map list and language picker (names: a map
    and a language may be written alike in English and another language)."""
    html = re.sub(r'<div class="maps">.*?\n    </div>\n', "", html, flags=re.S)
    return re.sub(r'<nav class="lang-picker".*?</nav>', "", html, flags=re.S)


# The same in every language: names, licences, EOX's required attribution.
NAMES = {
    "(CC-BY 4.0).", "Android", "CC BY-NC-SA 4.0", "EOxCloudless", "GitHub", "Internet Archive", "Kiwix",
    "Kiwix Desktop", "Kiwix PWA", "Linux", "Open Database License (ODbL)", "OpenMapTiles", "Street",
    "StreetZim", "Wikidata", "Wikipedia", "Wikipedia & Wikidata", "Windows", "Zim", "iOS", "kiwix.org",
    "macOS", "Mac", "Torrent", "Info", "website", "width=device-width, initial-scale=1",
    "github.com/jasontitus/streetzim", "github.com/kiwix/kiwix-android", "github.com/kiwix/kiwix-apple",
    "github.com/kiwix/kiwix-desktop", "github.com/kiwix/kiwix-js-pwa",
    "https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus Sentinel data 2021) —",
    "(CC0)", "(CC BY-SA 3.0).", "Copernicus GLO-30 DEM — © DLR / Airbus,", "Open Source", "MB", "GB",
}
# Words a language writes as English does.
SAME_AS_ENGLISH = {"fr": {"Satellite"}, "it": {"Satellite"}, "id": {"Kiwix PWA (Windows & Linux)"},
                   "nl": {"nightly builds"}}


# Languages whose site lists only the maps that have a version in them
# (site.<lang>.json "_maps_only"; test_site_maps_only.py).
MAPS_ONLY = {"as", "bn", "gu", "kn", "ml", "mr", "ne", "or", "pa", "ta", "te", "ur"}


def test_every_language_is_translated_in_full():
    assert {code for code, _n, _s in gen.ALL_LANGUAGES} == LANGS | MAPS_ONLY
    assert gen.MAPS_ONLY == MAPS_ONLY
    assert check_site_i18n.main([]) == 0


def test_every_key_is_used_and_every_used_key_exists():
    en = {k for k in json.loads((ROOT / "web" / "i18n" / "site.en.json").read_text(encoding="utf-8"))
          if not k.startswith("_")}
    template = (ROOT / "web" / "template.html").read_text(encoding="utf-8")
    code = (ROOT / "web" / "generate.py").read_text(encoding="utf-8")
    in_template = set(re.findall(r"\{\{[ta]:([a-z0-9_]+)\}\}", template))
    assert in_template <= en, in_template - en
    families = ({f"badge_{c}" for _k, _l, c in gen.FEATURE_BADGES} | {f"mp_feat_{c}" for _k, _l, c in gen.FEATURE_BADGES}
                | {"tier_" + tid.replace("-", "_") for tid, _l in gen.TIERS} | {"card_" + k for k in gen.card_text("en")}
                | set(gen.LANG_VARIANTS.values()))
    unused = {k for k in en - in_template - families if f'"{k}"' not in code}
    assert not unused, f"keys nothing uses (translated for nothing): {sorted(unused)}"


@pytest.mark.parametrize("lang", sorted(LANGS))
def test_each_home_page_is_in_its_language_and_links_the_others(site, lang):
    page = _page(site, lang)
    sub = "" if lang == "en" else f"{lang}/"
    rtl = ' dir="rtl"' if lang in ("ar", "fa", "he") else ""
    assert f'<html lang="{lang}"{rtl}>' in page
    nav = re.search(r'<nav class="lang-picker".*?</nav>', page).group(0)
    assert len(re.findall(r"<a ", nav)) == len(LANGS)
    assert f'hreflang="{lang}" lang="{lang}" aria-current="page"' in nav
    assert f'<link rel="canonical" href="{gen.SITE_URL}{sub}">' in page
    for code in LANGS:
        assert f'hreflang="{code}" href="{gen.SITE_URL}{"" if code == "en" else code + "/"}"' in page
    assert f'hreflang="x-default" href="{gen.SITE_URL}"' in page
    assert "{{" not in page and gen.t(lang, "html_title") in page
    if lang == "en":
        return
    # every map, in the language's words, its title linking to its page there
    cards = re.findall(r'<div class="map-card">.*?\n      </div>', page, re.S)
    titles = re.findall(r'<div class="map-card-title">(?:<a [^>]*>)?([^<]*)', page)
    assert len(titles) == len(set(titles)) == len(cards)          # each map once, a name once
    native = {gen.region_text(lang, r)[0] for r in gen.REGIONS if r.get("language") == lang}
    assert len(cards) == len([r for r in gen.REGIONS if gen._on_page(r, lang) and (
        r.get("language") == lang or gen.region_text(lang, r)[0] not in native)])
    kyiv = next(c for c in cards if f'href="/{lang}/maps/{WITH}/"' in c)
    tr = json.loads((ROOT / "web" / "i18n" / f"regions.{lang}.json").read_text(encoding="utf-8"))[WITH]
    assert f'<a href="/{lang}/maps/{WITH}/">{gen.escape(tr["title"])}</a>' in kyiv
    assert gen.escape(tr["description"], quote=False) in kyiv
    dl = re.search(r'<a class="btn btn-primary"[^>]*>', kyiv).group(0)
    assert f'href="{ORIGIN}/merged/{WITH}/{lang}/{UUID}/' in dl
    assert f'data-fallback="/build/?region={WITH}&amp;lang={lang}&amp;go=1"' in dl
    assert f">{gen.t(lang, 'card_download_lang')}</a>" in kyiv and gen.t(lang, "card_no_lang") not in kyiv
    egypt = next(c for c in cards if f'href="/{lang}/maps/{WITHOUT}/"' in c)
    assert gen.t(lang, "card_no_lang") in egypt and "archive.org/download/streetzim-egypt/" in egypt
    assert gen.t(lang, "maps_intro_lang") in page and gen.t(lang, "step2_p_lang") in page


@pytest.mark.parametrize("lang", sorted(LANGS - {"en"}))
def test_nothing_on_a_home_page_is_left_in_english(site, lang):
    """Every visible string a page shares with the English one, outside the
    map list and the language picker, is a name or a licence."""
    en = set(_visible(_chrome(_page(site, "en"))))
    left = sorted({s for s in _visible(_chrome(_page(site, lang))) if s in en and re.search(r"[A-Za-z]{2}", s)}
                  - NAMES - SAME_AS_ENGLISH.get(lang, set()))
    assert not left, left


@pytest.mark.parametrize("lang", sorted(LANGS))
def test_each_map_has_a_page_in_each_language(site, lang):
    live = [r for r in gen.REGIONS if r.get("language", "en") == "en"]
    sub = "" if lang == "en" else f"{lang}/"
    for r in live:
        assert (site / sub / "maps" / r["id"] / "index.html").is_file(), r["id"]
    page = _page(site, lang, f"maps/{WITH}")
    name = gen.escape(json.loads((ROOT / "web" / "i18n" / f"regions.{lang}.json").read_text(encoding="utf-8"))[WITH]["title"]
                      if lang != "en" else "Kyiv")
    assert f"<title>{gen.t_attr(lang, 'mp_html_title', name=name)}</title>" in page
    assert f'<link rel="canonical" href="{gen.SITE_URL}{sub}maps/{WITH}/">' in page
    alts = re.findall(r'<link rel="alternate" hreflang="([a-z-]+)" href="([^"]+)">', page)
    assert {h for h, _u in alts} == LANGS | {"x-default"}
    assert all(u.endswith(f"/maps/{WITH}/") for _h, u in alts)
    dl = re.search(r'<a class="btn btn-primary"[^>]*>', page).group(0)
    if lang == "en":
        # English: the map's own file first (as the home page), the builder page for other languages
        assert "archive.org/download/streetzim-kyiv/" in dl and "/merged/" not in page
        assert f'href="/build/?region={WITH}"' in page
    else:
        assert f'href="{ORIGIN}/merged/{WITH}/{lang}/{UUID}/' in dl
        assert f'data-fallback="/build/?region={WITH}&amp;lang={lang}&amp;go=1"' in dl
        assert f'href="/build/recipes/{WITH}/{lang}.torrent"' in page
        assert '<time datetime="2026-10-31">' in page                  # the data date of the version's base
        assert f'content="{gen.t_attr(lang, "mp_meta_description", name=name)}"' in page
    assert gen.t(lang, "kg_h2") in page and gen.t(lang, "kg_android_1") in page and gen.t(lang, "kg_ios_3") in page
    others = re.search(r'<ul class="other-langs">(.*?)</ul>', page).group(1)
    assert len(re.findall(r"<li>", others)) == len(LANGS) - 1 and f'hreflang="{lang}"' not in others
    assert "a[data-fallback]" in page and 'href="/site.css"' in page
    egypt = _page(site, lang, f"maps/{WITHOUT}")
    assert "/build/?region=egypt" not in egypt and "archive.org/download/streetzim-egypt/" in egypt
    if lang != "en":
        assert gen.t(lang, "card_no_lang") in egypt
        # no version in the language: the description does not promise one
        ename = gen.escape(gen.region_text(lang, next(r for r in gen.REGIONS if r["id"] == WITHOUT))[0])
        assert f'<meta name="description" content="{gen.t_attr(lang, "mp_lead", name=ename)}">' in egypt
    assert (site / "site.css").read_text(encoding="utf-8") == gen.template_parts()["css"]
    assert ".map-dl" in gen.template_parts()["css"]


def test_the_sitemap_lists_every_page_with_its_versions(site):
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9", "x": "http://www.w3.org/1999/xhtml"}
    root = ET.parse(site / "sitemap.xml").getroot()
    urls = root.findall("s:url", ns)
    maps = [r for r in gen.REGIONS if r.get("language", "en") == "en"]
    assert len(urls) == (1 + len(maps)) * len(LANGS)
    locs = [u.find("s:loc", ns).text for u in urls]
    assert len(set(locs)) == len(locs)
    for u in urls:
        alts = {a.get("hreflang"): a.get("href") for a in u.findall("x:link", ns)}
        assert set(alts) == LANGS | {"x-default"}
        assert u.find("s:loc", ns).text in alts.values()
    for loc in locs:
        path = loc[len(gen.SITE_URL):]
        assert (site / path / "index.html").is_file(), loc


def test_a_language_version_only_from_the_site_worker_with_its_index(tmp_path):
    d = tmp_path / WITH
    d.mkdir()
    good = f"{ORIGIN}/merged/{WITH}/de/{UUID}/a.zim"
    menu = [{"lang": "de", "size": 5, "webseed": good},
            {"lang": "fr", "size": 5, "webseed": f"https://elsewhere.example/merged/{WITH}/fr/{UUID}/a.zim"},
            {"lang": "it", "size": 5, "webseed": f"{ORIGIN}/merged/egypt/it/{UUID}/a.zim"},
            {"lang": "es", "size": 5, "webseed": f"{ORIGIN}/merged/{WITH}/es/{UUID}/a.zim"},
            {"lang": "../x", "size": 5, "webseed": good}]
    for lang in ("de", "fr", "it"):
        (d / f"{lang}.ws.json").write_text("{}")
    (d / "region.json").write_text(json.dumps({"id": WITH, "menu": menu}))
    v = gen.lang_versions(WITH, recipes_dir=str(tmp_path), origin=ORIGIN)
    assert set(v) == {"de", "fr", "it", "es"}
    assert v["de"]["url"] == good
    assert v["fr"]["url"] == v["it"]["url"] == v["es"]["url"] == ""     # another site, map, or no index
    assert v["fr"]["builder"] == f"/build/?region={WITH}&lang=fr&go=1" and v["fr"]["torrent"] is None


def test_a_removed_map_loses_its_pages_and_a_map_not_live_keeps_them(tmp_path, monkeypatch):
    for d in ("maps/gone-region", "maps/kyiv", "fr/maps/gone-region"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "index.html").write_text("old")
    _mock(monkeypatch, tmp_path, [r["id"] for r in gen.REGIONS if r["id"] != WITH])
    gen.build_page()
    assert not (tmp_path / "maps" / "gone-region").exists() and not (tmp_path / "fr" / "maps" / "gone-region").exists()
    assert (tmp_path / "maps" / "kyiv" / "index.html").read_text() == "old"     # not live just now: kept
    assert 'href="/maps/kyiv/"' not in (tmp_path / "index.html").read_text(encoding="utf-8")


def test_a_string_not_translated_yet_stays_english(tmp_path, monkeypatch):
    i18n = tmp_path / "i18n"
    shutil.copytree(ROOT / "web" / "i18n", i18n)
    fr = json.loads((i18n / "site.fr.json").read_text(encoding="utf-8"))
    del fr["kg_offline"]
    (i18n / "site.fr.json").write_text(json.dumps(fr, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(gen, "I18N_DIR", str(i18n))
    monkeypatch.setattr(gen, "_I18N", {})
    assert gen.t("fr", "kg_offline") == gen.t("en", "kg_offline")
    assert gen.t("fr", "kg_h2") != gen.t("en", "kg_h2")


def test_a_translation_with_other_markup_stops_the_build(tmp_path, monkeypatch):
    """A translation's HTML goes into the pages as it is: one that adds an
    attribute (an event handler), a tag or a comment stops the build."""
    for bad in ('Choisir <a href="https://kiwix.org" target="_blank" rel="noopener" onfocus=alert(1)>Kiwix</a>.',
                'Choisir <a href="https://kiwix.org" target="_blank" rel="noopener">Kiwix</a> <!--',
                'Choisir <a href="https://kiwix.org" target="_blank" rel="noopener">Kiwix</a> <img src=x onerror=alert(1)'):
        i18n = tmp_path / "i18n"
        shutil.rmtree(i18n, ignore_errors=True)
        shutil.copytree(ROOT / "web" / "i18n", i18n)
        fr = json.loads((i18n / "site.fr.json").read_text(encoding="utf-8"))
        fr["maps_intro_lang"] = bad
        (i18n / "site.fr.json").write_text(json.dumps(fr, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(gen, "I18N_DIR", str(i18n))
        monkeypatch.setattr(gen, "_I18N", {})
        with pytest.raises(ValueError, match=r"site\.fr\.json"):
            gen.site_languages()


def test_without_the_builder_no_map_says_it_lacks_a_version(site):
    e = {"region": next(r for r in gen.REGIONS if r["id"] == WITHOUT), "live": True, "size": 5, "meta": {},
         "torrent_ok": False, "built": None, "fresh": None, "build_ok": False,
         "archive_url": "https://archive.org/download/streetzim-egypt/osm-egypt-2026-10-06.zim"}
    assert gen.t("fr", "card_no_lang") in gen.render_lang_card(e, "fr", builder_on=True)
    assert gen.t("fr", "card_no_lang") not in gen.render_lang_card(e, "fr", builder_on=False)


def test_a_dropped_language_loses_its_pages(tmp_path, monkeypatch):
    for d in ("xx/maps/kyiv", "yy"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "xx" / "index.html").write_text("old")
    (tmp_path / "yy" / "index.html").write_text("old")
    (tmp_path / "yy" / "notes.txt").write_text("someone else's")
    monkeypatch.setattr(gen, "SCRIPT_DIR", str(tmp_path))
    gen.remove_dropped_languages()
    assert not (tmp_path / "xx").exists()
    assert (tmp_path / "yy" / "notes.txt").exists()               # not only what the script writes: kept


def test_each_download_says_the_date_of_its_own_data(tmp_path, monkeypatch):
    """The live file newer than the one the language version was made from:
    each download names its own data's date, its torrent beside it."""
    monkeypatch.setattr(gen, "SCRIPT_DIR", str(tmp_path))
    (tmp_path / "drive").mkdir()
    (tmp_path / "drive" / "preview-config.js").write_text(f"window.STREETZIM_MERGED_ORIGIN = '{ORIGIN}';\n")
    _recipes(tmp_path / "build" / "recipes")
    e = {"region": {**next(r for r in gen.REGIONS if r["id"] == WITH), "zim_file": "osm-kyiv-2026-11-02.zim"},
         "live": True, "size": 90000000, "meta": {}, "torrent_ok": True, "built": ("2026-11-02", "2 Nov 2026"),
         "fresh": None, "build_ok": True, "archive_url": "https://archive.org/download/streetzim-kyiv/osm-kyiv-2026-11-02.zim"}
    with open(gen.MAP_TEMPLATE_PATH, encoding="utf-8") as f:
        page = gen.render_map_page(e, "fr", f.read(), gen.template_parts())
    dl = page[page.index('<div class="map-dl">'):]
    lang_part = dl[:dl.index("archive.org/download/streetzim-kyiv/")]
    own_part = dl[dl.index("archive.org/download/streetzim-kyiv/"):]
    assert '<time datetime="2026-10-31">' in lang_part and '<time datetime="2026-11-02">' not in lang_part
    assert '<time datetime="2026-11-02">' in own_part
    assert f'href="/build/recipes/{WITH}/fr.torrent"' in lang_part and f'href="/torrents/{WITH}.torrent"' in own_part


def test_german_numbers_and_dates(site):
    page = _page(site, "de")
    card = next(c for c in re.findall(r'<div class="map-card">.*?\n      </div>', page, re.S)
                if 'href="/de/maps/egypt/"' in c)
    assert 'Stand <time datetime="2026-10-06">6. Okt. 2026</time>' in card
    assert "4,7 GB" in card
    assert 'badge-nav">Routen &amp; Navigation</span>' in card


def test_a_page_with_no_live_map_is_not_written(tmp_path, monkeypatch):
    """archive.org listing nothing (an index hiccup) must not publish pages
    without a single download link."""
    _mock(monkeypatch, tmp_path, [])
    with pytest.raises(SystemExit):
        gen.build_page()
    assert not (tmp_path / "index.html").exists()


def test_an_unknown_or_unfilled_placeholder_stops_the_build(tmp_path, monkeypatch):
    base = (ROOT / "web" / "template.html").read_text(encoding="utf-8")
    t = tmp_path / "template.html"
    monkeypatch.setattr(gen, "TEMPLATE_PATH", str(t))
    _mock(monkeypatch, tmp_path, [r["id"] for r in gen.REGIONS])
    t.write_text(base.replace("{{t:howto_h2}}", "{{t:no_such_key}}"), encoding="utf-8")
    with pytest.raises(KeyError):
        gen.build_page()
    t.write_text(base.replace("</footer>", "{{NOPE}}</footer>"), encoding="utf-8")
    with pytest.raises(ValueError, match="unfilled placeholder"):
        gen.build_page()


def test_a_german_region_is_not_in_the_build_registry():
    """cloud/regions.tsv drives the batch refresh (build-region-fast.sh),
    which builds in English: a German region there would be rebuilt in
    English and uploaded over the German map."""
    tsv = (ROOT / "cloud" / "regions.tsv").read_text(encoding="utf-8")
    ids = {line.split("\t")[0] for line in tsv.splitlines() if line and not line.startswith("#")}
    for r in gen.REGIONS:
        if r.get("language", "en") != "en":
            assert r["id"] not in ids, r["id"]


def test_the_german_maps_stay_on_the_german_page_only(site):
    de, en, fr = _page(site, "de"), _page(site, "en"), _page(site, "fr")
    assert 'data-region="switzerland-de"' in de
    assert 'data-region="switzerland-de"' not in en and 'data-region="switzerland-de"' not in fr
    assert not (site / "de" / "maps" / "switzerland-de").exists()


@pytest.mark.parametrize("found, listed, ok", [(78, 78, True), (101, 100, False), (None, 3, True)])
def test_a_listing_cut_short_by_archive_org_stops_the_page(monkeypatch, found, listed, ok):
    """archive.org lists at most `rows` items; a region past the cap would
    render as not live, with no error. fetch_archive_items stops instead."""
    import io
    docs = [{"identifier": f"streetzim-r{i}", "item_size": 1} for i in range(listed)]
    resp = {"response": {"docs": docs}}
    if found is not None:
        resp["response"]["numFound"] = found
    seen = {}

    def urlopen(req, timeout=0):
        seen["url"] = req.full_url
        return io.BytesIO(json.dumps(resp).encode())
    monkeypatch.setattr(gen.urllib.request, "urlopen", urlopen)
    if ok:
        assert len(gen.fetch_archive_items()) == listed
    else:
        with pytest.raises(SystemExit, match=f"listed {listed} of {found}"):
            gen.fetch_archive_items()
    assert "&rows=1000&" in seen["url"]
