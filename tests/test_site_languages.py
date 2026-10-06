"""The site in more than one language (web/generate.py LANGUAGES): every
page has the language picker, each lists only the regions in its language,
and the German page is the English template translated — a template edit
that drops an English string the German table replaces fails the build
instead of leaving English on the German page."""
import importlib.util
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen_lang", ROOT / "web" / "generate.py")
gen = importlib.util.module_from_spec(spec)
sys.modules["gen_lang"] = gen
spec.loader.exec_module(gen)


def test_every_german_string_is_in_the_template_once():
    template = (ROOT / "web" / "template.html").read_text(encoding="utf-8")
    for en, _de in gen.PAGE_TEXT["de"]:
        assert template.count(en) == 1, en[:80]


def _mock(monkeypatch, tmp_path, ids):
    monkeypatch.setattr(gen, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(gen, "fetch_archive_items", lambda: {i: {"item_size": 1} for i in ids})
    monkeypatch.setattr(gen, "fetch_item_details", lambda item_id, **kw: {
        "files": [{"name": f"osm-{item_id[len('streetzim-'):]}-2026-10-06.zim", "size": "5000000000"}],
        "metadata": {k: "yes" for k, _l, _c in gen.FEATURE_BADGES}})


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


# The same in both languages: names, licences, and EOX's required attribution.
SAME_IN_GERMAN = {
    "(CC-BY 4.0).", "Android", "CC BY-NC-SA 4.0", "Deutsch", "EOxCloudless", "English",
    "GitHub", "Info", "Internet Archive", "Kiwix", "Kiwix Desktop", "Kiwix PWA",
    "Language / Sprache", "Linux", "Open Database License (ODbL)", "OpenMapTiles", "Street",
    "StreetZim", "Wikidata", "Wikipedia", "Wikipedia & Wikidata", "Windows", "Zim",
    "github.com/jasontitus/streetzim", "github.com/kiwix/kiwix-android",
    "github.com/kiwix/kiwix-apple", "github.com/kiwix/kiwix-desktop",
    "github.com/kiwix/kiwix-js-pwa", "iOS", "kiwix.org", "macOS",
    "https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus "
    "Sentinel data 2021) —",
    "width=device-width, initial-scale=1",
}


def test_each_language_lists_only_its_regions_and_has_the_picker(tmp_path, monkeypatch):
    _mock(monkeypatch, tmp_path, [r["id"] for r in gen.REGIONS])
    gen.build_page()
    en = (tmp_path / "index.html").read_text(encoding="utf-8")
    de = (tmp_path / "de" / "index.html").read_text(encoding="utf-8")
    german_ids = {r["id"] for r in gen.REGIONS if r.get("language") == "de"}
    assert german_ids == {"switzerland-de", "switzerland-de-ultralight"}
    assert 'data-region="switzerland-de"' in de and 'data-region="switzerland-de"' not in en
    assert 'data-region="switzerland"' in en and 'data-region="switzerland"' not in de
    for page, cur in ((en, "en"), (de, "de")):
        nav = re.search(r'<nav class="lang-picker".*?</nav>', page).group(0)
        assert 'href="/"' in nav and 'href="/de/"' in nav
        assert f'hreflang="{cur}" lang="{cur}" aria-current="page"' in nav
        assert 'hreflang="de" href="https://streetzim.web.app/de/"' in page
        assert "{{" not in page
    assert '<html lang="de">' in de and '<html lang="en">' in en
    card = re.search(r'<div class="map-card">.*?</div>\s*</div>\s*</div>', de, re.S).group(0)
    assert ">Herunterladen</a>" in card
    if 'data-track="preview"' in card:
        assert ">Vorschau</a>" in card
    assert 'Stand <time datetime="2026-10-06">6. Okt. 2026</time>' in card
    assert "4,7 GB" in card
    assert 'badge-nav">Routen &amp; Navigation</span>' in card
    assert "Einzelne Länder" in de
    assert "(shown + ' von ' + total)" in de


def test_nothing_on_the_german_page_is_left_in_english(tmp_path, monkeypatch):
    """Every visible string the German page shares with the English one is a
    name, licence or attribution (SAME_IN_GERMAN): new English text in the
    template, a dropped or untranslated PAGE_TEXT / CARD_TEXT / badge
    entry all show up here."""
    _mock(monkeypatch, tmp_path, [r["id"] for r in gen.REGIONS])
    gen.build_page()
    en = set(_visible((tmp_path / "index.html").read_text(encoding="utf-8")))
    de = _visible((tmp_path / "de" / "index.html").read_text(encoding="utf-8"))
    left = sorted({t for t in de if t in en and re.search(r"[A-Za-z]{2}", t)} - SAME_IN_GERMAN)
    assert not left, left


def test_a_language_page_with_no_live_map_is_not_written(tmp_path, monkeypatch):
    """archive.org not listing the German item yet (or an index hiccup)
    must not publish a German page without a single download link."""
    _mock(monkeypatch, tmp_path, [r["id"] for r in gen.REGIONS if r.get("language", "en") == "en"])
    with pytest.raises(SystemExit):
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


def test_a_template_change_that_drops_a_translated_string_stops_the_build(tmp_path, monkeypatch):
    bad = (ROOT / "web" / "template.html").read_text(encoding="utf-8").replace(
        "<h2>How to Use</h2>", "<h2>How to use it</h2>")
    t = tmp_path / "template.html"
    t.write_text(bad, encoding="utf-8")
    monkeypatch.setattr(gen, "TEMPLATE_PATH", str(t))
    monkeypatch.setattr(gen, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(gen, "fetch_archive_items", lambda: {"switzerland-de": {"item_size": 1},
                                                              "switzerland": {"item_size": 1}})
    monkeypatch.setattr(gen, "fetch_item_details", lambda item_id, **kw: {
        "files": [{"name": "osm-x-2026-10-06.zim", "size": "5"}], "metadata": {}})
    with pytest.raises(ValueError, match="PAGE_TEXT"):
        gen.build_page()
    # twice is as wrong as never: one copy would stay English
    t.write_text((ROOT / "web" / "template.html").read_text(encoding="utf-8").replace(
        "</footer>", "<h2>How to Use</h2></footer>"), encoding="utf-8")
    with pytest.raises(ValueError, match="PAGE_TEXT"):
        gen.build_page()
