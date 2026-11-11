"""The home page's clips and screenshots (web/media/, web/media/media.json,
tools/site_media.py): every file a page asks for is there under a name that
carries its hash, each picture of someone else's work says whose it is, the
credits page and the README agree with the manifest, and the host serves the
files as never changing."""
import fnmatch
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen_media", ROOT / "web" / "generate.py")
gen = importlib.util.module_from_spec(spec)
sys.modules["gen_media"] = gen
spec.loader.exec_module(gen)
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import check_site_i18n  # noqa: E402
import test_site_languages as tsl  # noqa: E402

MEDIA = ROOT / "web" / "media"
MANIFEST = json.loads((MEDIA / "media.json").read_text(encoding="utf-8"))
CREDITS_DOC = (ROOT / "docs" / "media-credits.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
TEMPLATE = (ROOT / "web" / "template.html").read_text(encoding="utf-8")
EN = json.loads((ROOT / "web" / "i18n" / "site.en.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    out = tmp_path_factory.mktemp("site-media")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tsl, "gen", gen)
        tsl._mock(mp, out, [r["id"] for r in gen.REGIONS])
        gen.build_page()
    return out


def _page(site, lang):
    return (site / ("" if lang == "en" else f"{lang}/") / "index.html").read_text(encoding="utf-8")


def _place(lang, region_id):
    """A map's name on a language's page, as the page writes it."""
    region = next(r for r in gen.REGIONS if r["id"] == region_id)
    return gen.escape(gen.plain_text(gen.region_text(lang, region)[0]))


def _own_name(code):
    """A language's name for itself, as the page writes it."""
    return gen.escape(gen.site_text(code)["lang_name"])


def _url(logical):
    return "/media/" + MANIFEST["files"][logical]["file"]


def _tool():
    spec = importlib.util.spec_from_file_location("site_media_tool", ROOT / "tools" / "site_media.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


def _logical_names():
    """Every name the manifest's own entries point at."""
    names = set()
    for g in MANIFEST["gallery"]:
        names |= {g["small"], g["large"]}
    for entry in MANIFEST["langs"]:
        for shot in entry["shots"]:
            names |= set(shot.values())
    for s in MANIFEST["signs"]:
        names |= {s["video"], s["poster"]}
    return names | set(MANIFEST["video_credits"])


# ---- the files ---------------------------------------------------------

def test_the_manifest_and_the_folder_hold_the_same_files():
    listed = {f["file"] for f in MANIFEST["files"].values()}
    on_disk = {p.name for p in MEDIA.iterdir()} - {"media.json"}
    assert listed == on_disk
    assert len(listed) == len(MANIFEST["files"])                 # no two names share a file


@pytest.mark.parametrize("logical", sorted(MANIFEST["files"]))
def test_each_file_is_named_with_its_own_hash(logical):
    f = MANIFEST["files"][logical]
    stem, _, ext = logical.rpartition(".")
    digest = hashlib.sha256((MEDIA / f["file"]).read_bytes()).hexdigest()[:8]
    assert f["file"] == f"{stem}.{digest}.{ext}"
    assert f["w"] > 0 and f["h"] > 0
    if ext == "webp":                                            # the size a page reserves is the picture's own
        with Image.open(MEDIA / f["file"]) as im:
            assert im.size == (f["w"], f["h"])


def test_every_entry_of_the_manifest_names_a_file_it_has():
    assert _logical_names() <= set(MANIFEST["files"])
    in_template = set(re.findall(r"\{\{mc?:([a-z0-9.-]+)\}\}", TEMPLATE))
    assert in_template and in_template <= set(MANIFEST["files"])
    unused = set(MANIFEST["files"]) - _logical_names() - in_template
    assert not unused, f"files no page shows: {sorted(unused)}"


def test_the_site_stays_small_enough_to_load_on_a_phone():
    size = {f["file"]: (MEDIA / f["file"]).stat().st_size for f in MANIFEST["files"].values()}
    assert sum(size.values()) < 45e6
    stills = {n: s for n, s in size.items() if not n.endswith(".mp4")}
    assert max(stills.values()) < 450e3, max(stills, key=stills.get)
    hero = gen.media_file("hero-loop.mp4")[0].rsplit("/", 1)[1]
    assert size[hero] < 2.5e6                                    # plays by itself at the top of the page


# ---- the page ----------------------------------------------------------

def test_a_file_the_manifest_lacks_stops_the_page():
    with pytest.raises(KeyError):
        gen.media_file("no-such-picture.webp")
    with pytest.raises(KeyError):
        gen.fill_media('<img src="{{m:no-such-picture.webp}}">', "en")
    with pytest.raises(KeyError):
        gen.fill_media('<a data-credit="{{mc:no-such-clip.mp4}}">', "en")
    url, w, h = gen.media_file("poster-hero.webp")
    assert gen.fill_media('<img src="{{m:poster-hero.webp}}">', "en") == f'<img src="{url}">'
    assert url == "/media/" + MANIFEST["files"]["poster-hero.webp"]["file"] and (w, h) > (0, 0)


def test_a_caption_the_site_has_no_words_for_stops_the_page():
    entry = dict(MANIFEST["gallery"][0], caption="gal_nothing")
    with pytest.raises(ValueError, match="unknown caption"):
        gen.media_caption("en", entry)
    assert set(gen.GALLERY_CAPTIONS) <= set(EN)
    assert {g["caption"] for g in MANIFEST["gallery"]} <= set(gen.GALLERY_CAPTIONS)


def test_a_manifest_the_page_cannot_show_stops_it(monkeypatch):
    plain = next(g for g in MANIFEST["gallery"] if not g["lang"])
    version = next(g for g in MANIFEST["gallery"] if g["lang"])
    with pytest.raises(ValueError, match="no map 'atlantis'"):
        gen.media_caption("en", dict(plain, region="atlantis"))
    with pytest.raises(ValueError, match="'xx' is not a language of the site"):
        gen.media_caption("en", dict(version, lang="xx"))
    with pytest.raises(ValueError, match="names a language and the entry has none"):
        gen.media_caption("en", dict(version, lang=None))
    assert "{" not in gen.media_caption("en", version) and "{" not in gen.media_caption("en", plain)
    # a photo credited under a picture has its line in the list at the foot of the page
    gen.render_credits("en")
    changed = json.loads(json.dumps(MANIFEST))
    changed["credits"]["tower"] = dict(MANIFEST["credits"]["gate"], author="Someone Else")
    changed["gallery"][0]["credits"] = ["tower"]
    monkeypatch.setattr(gen, "media", lambda: changed)
    with pytest.raises(ValueError, match=r"\['tower'\] are credited"):
        gen.render_credits("en")


def test_a_credit_with_marks_in_it_is_written_safely(monkeypatch):
    monkeypatch.setattr(gen, "media_credit", lambda lang, ids: 'Photo: A & "B" <c>' if ids else "")
    safe = "Photo: A &amp; &quot;B&quot; &lt;c&gt;"
    gallery = gen.render_gallery("en")
    credited = sum(1 for g in MANIFEST["gallery"] if g["credits"])
    assert gallery.count(f'data-credit="{safe}"') == gallery.count(f"<small>{safe}</small>") == credited > 0
    assert '"B"' not in gallery and "<c>" not in gallery
    signs = gen.render_signs("en")
    assert signs.count(f'data-credit="{safe}"') == len(MANIFEST["signs"]) and "<c>" not in signs
    assert gen.fill_media('<p>{{mc:tour-photo.mp4}}</p>', "en") == f"<p>{safe}</p>"


@pytest.mark.parametrize("lang", sorted(tsl.LANGS))
def test_each_page_shows_every_clip_and_screenshot_and_asks_only_for_files_there(site, lang):
    page = _page(site, lang)
    assert len(re.findall(r'<figure class="shot shot-(?:wide|phone)">', page)) == len(MANIFEST["gallery"])
    assert len(re.findall(r'<a class="sign" ', page)) == len(MANIFEST["signs"]) == 8
    assert len(re.findall(r'<figure class="lang-shot">', page)) == len(MANIFEST["langs"]) == 6
    assert len(re.findall(r'<img [^>]*data-name=', re.search(
        r'<div class="frame" id="hero-phone">.*?</div>', page, re.S).group(0))) == len(MANIFEST["langs"])
    asked = set(re.findall(r'/media/([A-Za-z0-9._-]+)', page))
    on_disk = {p.name for p in MEDIA.iterdir()}
    assert asked <= on_disk, asked - on_disk
    assert "media.json" not in asked
    # and no file is carried that the page never shows
    assert asked == {f["file"] for f in MANIFEST["files"].values()}
    # each screenshot says what it shows and of which map, in the page's words
    figures = re.findall(r'<figure class="shot shot-(?:wide|phone)">.*?</figure>', page, re.S)
    for g, fig in zip(MANIFEST["gallery"], figures, strict=True):
        caption = re.search(r"<figcaption>([^<]*)", fig).group(1)
        what = gen.site_text(lang)[g["caption"]]
        if g["lang"]:                                 # a language version is named by that language's own name
            assert "{language}" in what
            what = what.replace("{language}", gen.site_text(g["lang"])["lang_name"])
        assert caption == gen.escape(gen.plain_text(what)) + " · " + _place(lang, g["region"]), caption
        assert f'alt="{caption}"' in fig and f'data-caption="{caption}"' in fig
        # the small file on the page, the large one behind it; neither loads before it is scrolled to
        small, large = MANIFEST["files"][g["small"]], MANIFEST["files"][g["large"]]
        img = re.search(r"<img [^>]*>", fig).group(0)
        assert f'<a href="{_url(g["large"])}" data-shot ' in fig
        assert img.startswith(f'<img src="{_url(g["small"])}" ') and ' loading="lazy"' in img
        if g["kind"] == "wide":
            assert small["w"] < large["w"]
            assert f' srcset="{_url(g["small"])} {small["w"]}w, {_url(g["large"])} {large["w"]}w" ' in img
        else:
            assert "srcset" not in img
    assert {g["lang"] for g in MANIFEST["gallery"] if g["lang"]} - {lang}       # a caption cannot pass by naming the page's own
    # each sign: its own clip and poster, the language named by itself, the city in the page's words
    signs = re.findall(r'<a class="sign" .*?</a>', page, re.S)
    for s, a in zip(MANIFEST["signs"], signs, strict=True):
        caption = _own_name(s["lang"]) + " · " + _place(lang, s["region"])
        assert a.startswith(f'<a class="sign" href="{_url(s["video"])}" data-video '
                            f'data-poster="{_url(s["poster"])}" data-caption="{caption}" data-credit="')
        assert f'<img src="{_url(s["poster"])}" ' in a and f' alt="{caption}" loading="lazy">' in a
        assert f'<span class="sign-label" lang="{s["lang"]}">{_own_name(s["lang"])}</span>' in a
    for key in ("lang", "region", "video", "poster"):
        assert len({s[key] for s in MANIFEST["signs"]}) == len(MANIFEST["signs"]), key
    # one map in six languages: the first screen on the page, the second behind it
    shots = re.findall(r'<figure class="lang-shot">.*?</figure>', page, re.S)
    for entry, fig in zip(MANIFEST["langs"], shots, strict=True):
        caption = _own_name(entry["lang"]) + " · " + _place(lang, "kyiv")
        links = re.findall(r'<a href="([^"]*)" data-shot data-caption="([^"]*)"( hidden)?>', fig)
        assert links == [(_url(shot["large"]), caption, " hidden" if i else "")
                         for i, shot in enumerate(entry["shots"])] and len(links) == 2
        assert fig.count("<img ") == 1 and f'<img src="{_url(entry["shots"][0]["small"])}" ' in fig
        assert ' loading="lazy">' in fig
        assert f'<figcaption lang="{entry["lang"]}">{_own_name(entry["lang"])}</figcaption>' in fig
        assert entry["shots"][0]["large"].startswith(f'kyiv-{entry["lang"]}-')
    assert len({e["lang"] for e in MANIFEST["langs"]}) == len(MANIFEST["langs"])
    # the phone at the top: the first screen loads with the page and is the one named; the rest wait their turn
    phone = re.search(r'<div class="hero-phone">.*?</span></div>', page, re.S).group(0)
    faces = re.findall(r"<img [^>]*>", phone)
    for i, (entry, face) in enumerate(zip(MANIFEST["langs"], faces, strict=True)):
        url = _url(entry["shots"][0]["small"])
        assert face.startswith(f'<img class="on" src="{url}" ' if i == 0 else f'<img data-src="{url}" ')
        assert (" src=" in face) == (i == 0) and "loading=" not in face
        assert f' lang="{entry["lang"]}" data-name="{_own_name(entry["lang"])}">' in face
    first = MANIFEST["langs"][0]["lang"]
    assert f'<span class="phone-tag" lang="{first}">{_own_name(first)}</span>' in phone
    # a picture is never taller or wider than its file says
    for src, w, h in re.findall(r'<img (?:class="on" )?(?:data-)?src="/media/([^"]+)" width="(\d+)" height="(\d+)"', page):
        f = next(f for f in MANIFEST["files"].values() if f["file"] == src)
        assert (int(w), int(h)) == (f["w"], f["h"])


@pytest.mark.parametrize("lang", sorted(tsl.LANGS))
def test_the_sections_and_their_links(site, lang):
    page = _page(site, lang)
    for anchor in ("videos", "gallery", "languages", "maps", "credits"):
        assert len(re.findall(rf'\bid="{anchor}"', page)) == 1, anchor
        assert f'href="#{anchor}"' in page, anchor
    stats = re.search(r'<div class="stats">(.*?)</div>', page).group(1)
    assert re.findall(r'<a href="#([a-z]+)"><b>', stats) == ["maps", "languages"]
    counts = [int(n) for n in re.findall(r"<b>(\d+)</b>", stats)]
    cards = len(re.findall(r'<div class="map-card">', page))
    assert len(counts) == 2 and counts[0] == cards > 0 and counts[1] == len(tsl.LANGS)
    grid = re.search(r'<div class="lang-grid">.*?</nav>', page, re.S).group(0)
    assert len(re.findall(r"<a ", grid)) == len(tsl.LANGS)
    # each tile leads to its own language's page
    assert re.findall(r'<a href="([^"]*)" hreflang="([a-z]+)" lang="([a-z]+)"', grid) == [
        ("/" if code == "en" else f"/{code}/", code, code) for code, _name, _sub in gen.ALL_LANGUAGES if code in tsl.LANGS]
    assert f'hreflang="{lang}" lang="{lang}" aria-current="page"' in grid
    chips = re.findall(r'<button type="button" class="chip" data-tier="([^"]+)" aria-pressed="(true|false)">', page)
    headers = len(re.findall(r'class="maps-tier-header', page))
    assert chips[0] == ("all", "true") and [c[0] for c in chips[1:]] == [str(i) for i in range(headers)]
    assert headers >= 2 and all(c[1] == "false" for c in chips[1:])
    # a chip is named as the section it shows
    names = re.findall(r'<button type="button" class="chip" data-tier="\d+" aria-pressed="false">([^<]*)</button>', page)
    assert names == re.findall(r'<h3 class="maps-tier-header">([^<]*)</h3>', page) and len(set(names)) == headers


def test_the_count_beside_a_language_is_the_maps_ready_in_it(site):
    page = _page(site, "en")
    grid = re.search(r'<div class="lang-grid">.*?</nav>', page, re.S).group(0)
    count = {code: int(n) for code, n in re.findall(r'hreflang="([a-z]+)"[^>]*><span>[^<]*</span><b>(\d+)</b>', grid)}
    assert set(count) == tsl.LANGS
    live = int(re.search(r'<div class="stats"><a href="#maps"><b>(\d+)</b>', page).group(1))
    made_in_german = sum(1 for r in gen.REGIONS if r.get("language") == "de")
    assert count["en"] == live                                   # every English map, as it is
    assert count["ja"] == 1                                      # the one map with a version (the fixture's Kyiv)
    assert count["de"] == 1 + made_in_german                     # that, and the maps made in German


def _recipe(root, rid, lang, size):
    """A map's recipes with one language version, made from the map the fixture serves."""
    d = root / rid
    d.mkdir(parents=True)
    (d / "region.json").write_text(json.dumps({
        "id": rid, "title": rid, "menu": [{"lang": lang, "label": lang, **({"size": size} if size else {})}],
        "base": {"file": f"osm-{rid}-2026-10-06.zim", "size": 5000000000, "md5": "0" * 32}}))


def test_the_counts_are_of_maps_a_visitor_can_get(tmp_path):
    """A language with no map ready shows 0 and is not counted. Not counted
    either: a map still being built, an English map its page lists as the
    map made in the language of the same name, a version without its size."""
    english = [r for r in gen.REGIONS if r.get("language", "en") == "en"]
    german = [r for r in gen.REGIONS if r.get("language") == "de"]
    made = {gen.region_text("de", r)[0]: r["id"] for r in german}
    twin = next(r for r in english if gen.region_text("de", r)[0] in made)
    others = [r["id"] for r in english if r["id"] not in (tsl.WITH, twin["id"])]
    building, sizeless = others[0], others[1]
    building_de = next(r["id"] for r in german if r["id"] != made[gen.region_text("de", twin)[0]])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tsl, "gen", gen)
        recipes = tmp_path / "build" / "recipes"
        tsl._recipes(recipes, langs=tsl.LANGS - {"th"})
        _recipe(recipes, twin["id"], "de", 1000)
        _recipe(recipes, sizeless, "de", None)
        tsl._mock(mp, tmp_path, [r["id"] for r in gen.REGIONS if r["id"] not in (building, building_de)])
        gen.build_page()
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    stats = re.search(r'<div class="stats">(.*?)</div>', page).group(1)
    live, langs = (int(n) for n in re.findall(r"<b>(\d+)</b>", stats))
    assert langs == len(tsl.LANGS) - 1
    assert live == len(re.findall(r'<div class="map-card">', page)) == len(english) - 1
    assert len(re.findall(r'<div class="map-card upcoming">', page)) == 1
    grid = re.search(r'<div class="lang-grid">.*?</nav>', page, re.S).group(0)
    count = {code: int(n) for code, n in re.findall(r'hreflang="([a-z]+)"[^>]*><span>[^<]*</span><b>(\d+)</b>', grid)}
    assert count["th"] == 0 and "th" in tsl.LANGS                # still a tile: its page exists
    assert count["vi"] == 1
    assert count["en"] == live
    assert count["de"] == 1 + len(german) - 1                    # Kyiv, and the German maps that are ready
    # the German page says the same: its number is its cards, and it offers what was counted
    de = (tmp_path / "de" / "index.html").read_text(encoding="utf-8")
    de_live = int(re.search(r'<div class="stats"><a href="#maps"><b>(\d+)</b>', de).group(1))
    assert de_live == len(re.findall(r'<div class="map-card">', de)) == len(english) - 2 + len(german) - 1
    assert len(re.findall(r'data-track="download_lang"', de)) == 1
    assert f'data-track="download_lang" data-region="{tsl.WITH}"' in de
    assert f'href="/de/maps/{twin["id"]}/"' not in re.search(r'<div class="maps".*', de, re.S).group(0)
    assert f"/build/?region={sizeless}&amp;lang=de" not in de
    assert f"/build/?region={sizeless}&amp;lang=de" not in (
        tmp_path / "de" / "maps" / sizeless / "index.html").read_text(encoding="utf-8")


# ---- whose work a picture shows ------------------------------------------

def test_each_picture_with_someone_elses_work_says_whose(site):
    page = _page(site, "en")
    figures = re.findall(r'<figure class="shot shot-(?:wide|phone)">.*?</figure>', page, re.S)
    for g, fig in zip(MANIFEST["gallery"], figures, strict=True):
        small = re.search(r"<small>(.*?)</small>", fig)
        assert bool(small) == bool(g["credits"]), g["small"]
        for cid in g["credits"]:
            c = MANIFEST["credits"][cid]
            if c["kind"] == "imagery":
                assert "EOxCloudless" in small.group(1) and "CC BY-NC-SA 4.0" in small.group(1)
            else:
                assert gen.escape(c["author"]) in small.group(1), (g["small"], cid)
                assert (c["licence"][0] if c.get("licence") else "public domain") in small.group(1)
            assert f'data-credit="{small.group(1)}"' in fig       # the same line under the large picture
    signs = re.findall(r'<a class="sign" [^>]*>', page)
    for s, a in zip(MANIFEST["signs"], signs, strict=True):
        assert s["credits"] == [s["id"]] and gen.escape(MANIFEST["credits"][s["id"]]["author"]) in a
    for clip, cids in MANIFEST["video_credits"].items():
        url = re.escape(gen.media_file(clip)[0])
        if clip == "tour-photo.mp4":                             # plays in the page: the line under it
            credit = re.search(rf'<source src="{url}"[^>]*>\s*</video>\s*</div>\s*'
                               r'<p class="note clip-credit">([^<]*)</p>', page).group(1)
        else:
            a = re.search(rf'<a class="clip-media" href="{url}"[^>]*>', page).group(0)
            credit = re.search(r'data-credit="([^"]*)"', a).group(1)
        assert credit == gen.escape(gen.media_credit("en", cids)) and credit
        for cid in cids:
            c = MANIFEST["credits"][cid]
            assert ("EOxCloudless, CC BY-NC-SA 4.0" if c["kind"] == "imagery" else gen.escape(c["author"])) in credit
    # a clip that shows only the map says nothing
    zoom = re.search(rf'<a class="clip-media" href="{re.escape(gen.media_file("tour-zoom.mp4")[0])}"[^>]*>', page).group(0)
    assert "data-credit" not in zoom and "tour-zoom.mp4" not in MANIFEST["video_credits"]
    assert [g["credits"] for g in MANIFEST["gallery"] if g["caption"] == "gal_place"] == [[]]
    assert [g["credits"] for g in MANIFEST["gallery"] if g["caption"] == "gal_article"] == [["gate"]]
    # a picture that shows two people's work names both
    assert gen.media_credit("en", ["gate", "sat"]) == (
        "Photo: George Chernilevsky, public domain · Satellite imagery: EOxCloudless, CC BY-NC-SA 4.0")


def test_a_satellite_picture_is_never_shown_without_its_licence():
    sat = [g for g in MANIFEST["gallery"] if "satellite" in g["small"]]
    assert len(sat) >= 3 and all("sat" in g["credits"] for g in sat)
    assert MANIFEST["video_credits"]["tour-terrain.mp4"] == ["sat"]
    assert check_site_i18n.VERBATIM["credit_sat"] == check_site_i18n.VERBATIM["src_sat"]
    eox = check_site_i18n.VERBATIM["credit_sat"][0]
    assert eox in EN["credit_sat"] and "CC BY-NC-SA 4.0" in EN["credit_sat"]
    assert "CC BY-NC-SA 4.0" in EN["credit_sat_short"] and "EOxCloudless" in EN["credit_sat_short"]
    changed = dict(EN, credit_sat=EN["credit_sat"].replace("EOX IT Services GmbH", "EOX"))
    assert any("credit_sat" in p for p in check_site_i18n.check_site(EN, changed, "site.xx.json"))


@pytest.mark.parametrize("lang", sorted(tsl.LANGS))
def test_the_credits_list_names_every_photo_and_the_imagery(site, lang):
    page = _page(site, lang)
    block = re.search(r'<div class="licenses" id="credits">.*?</ul>', page, re.S).group(0)
    lines = re.findall(r"<li>(.*?)</li>", block, re.S)
    assert len(lines) == len(MANIFEST["signs"]) + 2
    for s, line in zip(MANIFEST["signs"], lines, strict=False):
        c = MANIFEST["credits"][s["id"]]
        assert _place(lang, s["region"]) in line
        assert f'<a href="{gen.escape(c["source"])}" target="_blank" rel="noopener">{gen.escape(c["author"])}</a>' in line
        if c.get("licence"):
            assert f'<a href="{c["licence"][1]}" target="_blank" rel="noopener">{c["licence"][0]}</a>' in line
        else:
            assert "creativecommons.org" not in line
    gate = MANIFEST["credits"]["gate"]
    assert gen.escape(gate["source"]) in lines[-2] and gate["author"] in lines[-2]
    assert check_site_i18n.VERBATIM["credit_sat"][0] in lines[-1]
    assert "https://creativecommons.org/licenses/by-nc-sa/4.0/" in lines[-1]


# ---- docs/media-credits.md and the README --------------------------------

def _doc_rows():
    """{source URL: (files cell, author, licence cell)} of the credits page's table."""
    rows = {}
    for line in CREDITS_DOC.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("| `") and len(cells) == 4:
            rows[re.search(r"\]\((\S+)\)", cells[1]).group(1)] = (cells[0], cells[2], cells[3])
    return rows


def _shown_in(cid):
    """The files of the site that show a credit's photo, without hash and extension."""
    names = {s["video"] for s in MANIFEST["signs"] if cid in s["credits"]}
    names |= {clip for clip, cids in MANIFEST["video_credits"].items() if cid in cids}
    names |= {g["large"] for g in MANIFEST["gallery"] if cid in g["credits"]}
    return {n.rsplit(".", 1)[0] for n in names}


def test_the_credits_page_names_every_photo_as_the_manifest_does():
    rows = _doc_rows()
    photos = {cid: c for cid, c in MANIFEST["credits"].items() if c["kind"] != "imagery"}
    assert set(rows) == {c["source"] for c in photos.values()}
    for cid, c in photos.items():
        files, author, licence = rows[c["source"]]
        assert author == c["author"], cid
        if c.get("licence"):
            assert licence == f"[{c['licence'][0]}]({c['licence'][1]})", cid
        else:
            assert licence == "public domain", cid
        patterns = [p for p in re.findall(r"`([^`]+)`", files) if not p.startswith("docs/")]
        for name in _shown_in(cid):
            assert any(fnmatch.fnmatch(name, p) for p in patterns), f"{cid}: {name} is not in its row"
        for p in patterns:
            assert any(fnmatch.fnmatch(name, p) for name in _shown_in(cid)), f"{cid}: nothing is called {p}"


def test_the_credits_page_lists_every_satellite_picture():
    section = CREDITS_DOC.split("## Satellite imagery", 1)[1].split("\n## ", 1)[0]
    assert check_site_i18n.VERBATIM["credit_sat"][0] in " ".join(section.split())
    assert "[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)" in section
    patterns = re.findall(r"`([^`/]+)`", section.split("- `web/media/`:", 1)[1])
    shown = {re.sub(r"-\d+\.webp$", "-*", g["large"]) for g in MANIFEST["gallery"] if "sat" in g["credits"]}
    shown |= {clip.replace(".mp4", ".*.mp4") for clip, cids in MANIFEST["video_credits"].items() if "sat" in cids}
    shown |= {"poster-terrain.*"}
    assert set(patterns) == shown
    for p in patterns:
        assert any(fnmatch.fnmatch(f["file"], p) for f in MANIFEST["files"].values()), p


def test_the_readme_shows_pictures_that_are_there_and_links_sections_that_exist():
    used = set(re.findall(r'src="(docs/readme/[^"]+)"', README))
    on_disk = {f"docs/readme/{p.name}" for p in (ROOT / "docs" / "readme").iterdir()}
    assert used == on_disk and len(used) == 7
    assert sum((ROOT / p).stat().st_size for p in used) < 6e6     # the repository carries them for good
    for p in used:
        assert (ROOT / p).stat().st_size < 2.2e6, p
    for img in re.findall(r"<img [^>]*>", README):
        assert len(re.search(r'alt="([^"]*)"', img).group(1)) > 20, img
    anchors = set(re.findall(r'href="https://streetzim\.web\.app/#([a-z]+)"', README))
    assert anchors == {"videos", "gallery", "languages"}
    for a in anchors:
        assert f'id="{a}"' in TEMPLATE
    assert "docs/media-credits.md" in README and "non-commercial" in README
    for name in re.findall(r"`(docs/readme/[^`]+)`", CREDITS_DOC):
        assert name in on_disk, name


# ---- the tool that makes them -----------------------------------------------

def test_the_manifest_is_what_the_tool_writes():
    tool = _tool()
    assert MANIFEST["credits"] == json.loads(json.dumps(tool.CREDITS))
    assert MANIFEST["video_credits"] == tool.VIDEO_CREDITS
    assert [(s["id"], s["lang"], s["region"]) for s in MANIFEST["signs"]] == [tuple(s) for s in tool.SIGNS]
    assert [(g["large"].rsplit("-", 1)[0], g["kind"], g["caption"], g["region"], g["lang"], g["credits"])
            for g in MANIFEST["gallery"]] == [(src.rpartition(":")[2], *rest) for src, *rest in tool.GALLERY]
    assert [(e["lang"], [shot["large"].rsplit("-", 1)[0] for shot in e["shots"]])
            for e in MANIFEST["langs"]] == [(lang, shots) for lang, shots in tool.LANG_SHOTS]
    large = tool.SIZES["phone"][0]
    pictures = {f"{src.rpartition(':')[2]}-{w}.webp" for src, kind, *_ in tool.GALLERY for w in tool.SIZES[kind]}
    for _lang, shots in tool.LANG_SHOTS:
        pictures |= {f"{shots[0]}-{w}.webp" for w in tool.SIZES["phone"]} | {f"{name}-{large}.webp" for name in shots[1:]}
    assert set(MANIFEST["files"]) == set(tool.VIDEOS) | set(tool.POSTERS) | pictures
    assert set(MANIFEST["made_from"]) == set(MANIFEST["files"])
    assert all(tool.HASHED.search(f["file"]) for f in MANIFEST["files"].values())
    assert not tool.HASHED.search("media.json") and not tool.HASHED.search("poster-hero.webp")
    # the README's pictures: the ones the tool makes and no other
    assert {p.name for p in (ROOT / "docs" / "readme").iterdir()} == set(tool.README_CLIPS) | set(tool.README_STILLS)


def _captures(tool, root):
    """A capture folder with a file for every name the tool reads, and a
    stand-in for ffmpeg that writes different bytes every time it is asked."""
    for kind in ("shots", "lang", "videos"):
        (root / kind).mkdir(parents=True)
    for src, _ff in tool.VIDEOS.values():
        (root / "videos" / src).write_bytes(src.encode())
    for spec, _at, _width in tool.POSTERS.values():
        kind, _, name = spec.partition(":")
        (root / kind / name).write_bytes(name.encode())
    for src, *_ in tool.GALLERY:
        d, _, name = src.rpartition(":")
        (root / (d or "shots") / f"{name}.png").write_bytes(name.encode())
    for _lang, shots in tool.LANG_SHOTS:
        for name in shots:
            (root / "lang" / f"{name}.png").write_bytes(name.encode())
    made = []

    def run(cmd):
        if cmd[0] == "ffmpeg":
            made.append(Path(cmd[-1]).name)
            Path(cmd[-1]).write_bytes(f"{cmd[-1]} {len(made)}".encode())
        return "4,3"
    return made, run, ["site_media.py", "--shots", str(root / "shots"), "--lang-shots", str(root / "lang"),
                       "--videos", str(root / "videos")]


def test_the_tool_replaces_its_own_files_and_leaves_the_rest(tmp_path, monkeypatch):
    tool = _tool()
    made, run, argv = _captures(tool, tmp_path / "in")
    monkeypatch.setattr(tool, "run", run)
    out = tmp_path / "media"
    (out / "notes").mkdir(parents=True)
    others = ["README.txt", "plain.webp", "media.txt"]
    for name in [*others, "old.0123abcd.webp", "clip.89abcdef.mp4"]:
        (out / name).write_text("before")
    monkeypatch.setattr(sys, "argv", [*argv, "--out", str(out)])
    tool.main()
    first = json.loads((out / "media.json").read_text(encoding="utf-8"))
    assert sorted(made) == sorted(first["files"]) == sorted(MANIFEST["files"])
    assert {k: v for k, v in first.items() if k not in ("files", "made_from")} == {
        k: v for k, v in MANIFEST.items() if k not in ("files", "made_from")}
    names = {f["file"] for f in first["files"].values()}
    # its own old files are gone; what it did not make is as it was
    assert {p.name for p in out.iterdir()} == names | {*others, "notes", "media.json"}
    assert all((out / n).read_text() == "before" for n in others)
    for logical, f in first["files"].items():
        stem, _, ext = logical.rpartition(".")
        assert f == {"file": f"{stem}.{hashlib.sha256((out / f['file']).read_bytes()).hexdigest()[:8]}.{ext}", "w": 4, "h": 3}
    # the same captures again: nothing is encoded, no name changes
    made.clear()
    tool.main()
    assert made == [] and json.loads((out / "media.json").read_text(encoding="utf-8")) == first
    # a capture that changed, a file that is gone and one that is damaged are made again; the rest stay
    (tmp_path / "in" / "videos" / tool.VIDEOS["tour-drive.mp4"][0]).write_bytes(b"recorded again")
    (out / first["files"]["poster-zoom.webp"]["file"]).unlink()
    (out / first["files"]["sign-greek.mp4"]["file"]).write_bytes(b"cut short")
    tool.main()
    again = json.loads((out / "media.json").read_text(encoding="utf-8"))
    assert sorted(made) == ["poster-drive.webp", "poster-zoom.webp", "sign-greek.mp4", "tour-drive.mp4"]
    changed = {k for k in first["files"] if first["files"][k] != again["files"][k]}
    assert changed == set(made)
    assert {k for k in first["made_from"] if first["made_from"][k] != again["made_from"][k]} == {
        "poster-drive.webp", "tour-drive.mp4"}
    assert {p.name for p in out.iterdir()} == {f["file"] for f in again["files"].values()} | {*others, "notes", "media.json"}
    # other settings for a clip: that clip again
    made.clear()
    monkeypatch.setitem(tool.VIDEOS, "tour-zoom.mp4", (tool.VIDEOS["tour-zoom.mp4"][0], ["-vf", "scale=640:360", "-crf", "30"]))
    tool.main()
    assert made == ["tour-zoom.mp4"]


@pytest.mark.parametrize("readme", [False, True])
def test_a_run_that_fails_leaves_the_folder_as_it_was(tmp_path, monkeypatch, readme):
    tool = _tool()
    made, run, argv = _captures(tool, tmp_path / "in")
    out, work = tmp_path / "out", tmp_path / "work"
    out.mkdir()
    for name in ("kept.0123abcd.webp", "media.json", "zoom.webp"):
        (out / name).write_text("before")
    monkeypatch.setattr(tool.tempfile, "mkdtemp", lambda prefix="": (work.mkdir(), str(work))[1])

    def failing(cmd):
        if cmd[0] == "ffmpeg" and len(made) == 2:
            sys.exit("ffmpeg: no")
        return run(cmd)
    monkeypatch.setattr(tool, "run", failing)
    monkeypatch.setattr(sys, "argv", [*argv, "--out", str(out), *(["--readme"] if readme else [])])
    with pytest.raises(SystemExit, match="ffmpeg: no"):
        tool.main()
    assert len(made) == 2 and not work.exists()
    assert sorted(p.name for p in out.iterdir()) == ["kept.0123abcd.webp", "media.json", "zoom.webp"]
    assert all((out / n).read_text() == "before" for n in ("kept.0123abcd.webp", "media.json", "zoom.webp"))
    # and one that works clears its workroom too
    monkeypatch.setattr(tool, "run", run)
    tool.main()
    assert not work.exists() and ((out / "zoom.webp").read_text() != "before") == readme
    assert (out / "kept.0123abcd.webp").exists() == readme


# ---- the host ------------------------------------------------------------

def test_the_host_serves_the_media_as_never_changing_and_keeps_the_manifest_to_itself():
    hosting = json.loads((ROOT / "firebase.json").read_text(encoding="utf-8"))["hosting"]
    rule = next(h for h in hosting["headers"] if h["source"] == "/media/**")
    assert rule["headers"] == [{"key": "Cache-Control", "value": "public, max-age=31536000, immutable"}]
    assert "media/media.json" in hosting["ignore"]
    # nothing is served for a year under a name that does not change with its contents
    assert all(re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)?\.[0-9a-f]{8}\.(webp|mp4)", f["file"])
               for f in MANIFEST["files"].values())
