"""The viewer's UI strings (docs/i18n.md).

- the generated files (en.json, part 005, places.html's block) are current;
- every translation table has exactly the keys the code uses, with the same
  {placeholders} and the plural forms its language needs;
- a machine-translated table ("_machine": true) says so on screen, and only
  such a table does;
- user-visible text in the viewer is translatable: static markup carries
  data-i18n* (or data-i18n-code / translate="no"), and no English literal
  goes straight into a text sink in the JavaScript (best effort: the sinks
  below, minus ALLOW).
"""
from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_i18n  # noqa: E402

I18N = ROOT / "resources" / "viewer" / "i18n"
PARTS = ROOT / "resources" / "viewer" / "src" / "index"
PLACES = ROOT / "resources" / "viewer" / "places.html"

# Plural categories each language's table must give (Intl.PluralRules).
PLURAL_FORMS = {"de": {"one", "other"}, "el": {"one", "other"}, "es": {"one", "other"},
                "fr": {"one", "other"}, "ja": {"other"}, "ko": {"other"},
                "th": {"other"}, "zh": {"other"},
                "ar": {"zero", "one", "two", "few", "many", "other"}, "fa": {"one", "other"},
                "he": {"one", "two", "other"}, "hi": {"one", "other"}, "id": {"other"},
                "it": {"one", "other"}, "nl": {"one", "other"},
                "pl": {"one", "few", "many", "other"}, "pt": {"one", "other"},
                "ru": {"one", "few", "many", "other"}, "sv": {"one", "other"},
                "tr": {"one", "other"}, "uk": {"one", "few", "many", "other"}, "vi": {"other"}}
# Tables made by machine translation (their "_machine": true shows the note);
# German is reviewed by a person.
MACHINE = {"el", "es", "fr", "ja", "ko", "th", "zh",
           "ar", "fa", "he", "hi", "id", "it", "nl", "pl", "pt", "ru", "sv", "tr", "uk", "vi"}
# The core languages (docs/language-packs.md): the viewer has a UI table for
# each, so a merged map in any of them shows its interface in that language.
CORE = {"ar", "de", "el", "es", "fa", "fr", "he", "hi", "id", "it", "ja", "ko", "nl", "pl",
        "pt", "ru", "th", "tr", "uk", "vi", "zh"}
# Written right to left: runtime.js SZ_RTL_LANGS gives the page dir="rtl".
RTL = {"ar", "fa", "he"}

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def en_table() -> dict[str, Any]:
    return json.loads((I18N / "en.json").read_text(encoding="utf-8"))


def lang_tables() -> dict[str, dict[str, Any]]:
    return build_i18n.languages()


def test_generated_files_are_current() -> None:
    assert build_i18n.main(["--check"]) == 0, "run: python tools/build_viewer.py"


def test_there_is_a_german_table() -> None:
    assert "de" in lang_tables()


def test_the_language_matrix_menu_has_its_ui_tables() -> None:
    # California's menu entries (es, fr, ja, ko, el) got an English UI and
    # no <html lang> without a table; every plural rule is listed above.
    tables = lang_tables()
    assert {"el", "es", "fr", "ja", "ko"} <= set(tables)
    assert set(tables) <= set(PLURAL_FORMS), sorted(set(tables) - set(PLURAL_FORMS))


def test_every_core_language_has_a_table() -> None:
    assert CORE <= set(lang_tables()), sorted(CORE - set(lang_tables()))


@pytest.mark.parametrize("lang", sorted(build_i18n.languages()))
def test_plural_forms_are_what_intl_gives(lang: str) -> None:
    # PLURAL_FORMS (what each table must give) against the categories the
    # runtime's Intl.PluralRules returns for the language: a table missing one
    # would show the English for that count. Node's ICU stands in for the
    # browsers'; "many" in es/fr/it/pt (millions, compact forms) may fall back
    # to "other", as runtime.js szTn does.
    import shutil
    import subprocess
    node = shutil.which("node") or "/storage/streetzim/.browser-libs/node-v20.18.1-linux-x64/bin/node"
    if not Path(node).exists():
        pytest.skip("no node")
    out = subprocess.run([node, "-e", "console.log(JSON.stringify(new Intl.PluralRules(process.argv[1])"
                          ".resolvedOptions().pluralCategories))", lang],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    intl = set(json.loads(out)) - ({"many"} if lang in ("es", "fr", "it", "pt") else set())
    assert PLURAL_FORMS[lang] == intl, (lang, sorted(intl))


def test_rtl_tables_mirror_the_back_arrows() -> None:
    # "← Map" / "← Back to list" point at the start of the line; in a
    # right-to-left UI the start is on the right, so the arrow is "→".
    for lang, table in lang_tables().items():
        for key in ("places.back_to_map", "wiki.back_to_list"):
            arrow = "→" if lang in RTL else "←"
            assert table[key].startswith(arrow), (lang, key, table[key])


def test_the_pages_carry_the_right_to_left_rules() -> None:
    # What runtime.js's dir="rtl" relies on (docs/i18n.md#right-to-left-languages):
    # the clear buttons move to the start of their inputs' text, the scale bar
    # stays "3000 ft", and the runtime has the RTL set, szLtr and isolates.
    idx = (ROOT / "resources" / "viewer" / "index.html").read_text(encoding="utf-8")
    plc = PLACES.read_text(encoding="utf-8")
    for rule in ('html[dir="rtl"] #search-clear { right: auto; left: 10px; }',
                 'html[dir="rtl"] #search-input { padding: 10px 14px 10px 36px; }',
                 'html[dir="rtl"] .search-result-native { margin-left: 0; margin-right: 4px; }',
                 'html[dir="rtl"] .search-result-type { margin-left: 0; margin-right: 6px; }',
                 # each part of a result row its own run ("Acropolisarts and entertainment")
                 'html[dir="rtl"] .search-result-name, html[dir="rtl"] .search-result-native,\n'
                 'html[dir="rtl"] .search-result-type { unicode-bidi: isolate; }',
                 'html[dir="rtl"] #controls button { text-align: right; }',
                 'html[dir="rtl"] .wiki-item { border-left: none; border-right: 3px solid transparent; }',
                 'html[dir="rtl"] .wiki-item:hover { border-right-color: var(--accent); }',
                 'html[dir="rtl"] #wiki-panel-close { margin-left: 0; margin-right: 8px; }',
                 'border-left: none; padding-left: 0; border-right: 3px solid var(--accent); padding-right: 8px;',
                 'html[dir="rtl"] #attr-close { right: auto; left: 14px; }',
                 'html[dir="rtl"] #routing-panel.minimized #routing-expand-hint { margin-left: 0; margin-right: 8px; }',
                 'html[dir="rtl"] .routing-results { left: 0; right: 18px; }',
                 'html[dir="rtl"] .maplibregl-ctrl-scale { direction: ltr; }',
                 'html[dir="rtl"] #controls button + button { border-left: none; border-right: 1px solid var(--border); }',
                 'html[dir="rtl"] .sz-home-group button + button { border-left: none; border-right: 1px solid #ddd; }',
                 'html[dir="rtl"] #routing-close { margin-right: 0 !important; margin-left: -8px !important; }',
                 'html[dir="rtl"] #wiki-panel-close { margin: -14px 0 -12px -12px; }',
                 'html[dir="rtl"] #search-clear { right: auto; left: 0; }',
                 'html[dir="rtl"] #attr-dialog #about-mt { border-left: none; padding-left: 0; border-right: 3px solid var(--border); padding-right: 8px; }',
                 'html[dir="rtl"] #sz-fatal ul { padding-left: 0; padding-right: 20px; }',
                 # not RTL: About fits a 390 px phone; #info keeps clear of the About button
                 'max-width: 536px; width: calc(100% - 40px); box-sizing: border-box;',
                 'max-width: min(250px, calc(100vw - 82px - var(--sz-attr-w, 71px)));',
                 # the HUD reroute button leaves the status beside it half the row, a
                 # longer label cut with an ellipsis
                 'flex: 0 0 auto; max-width: 50%;\n'
                 'overflow: hidden; text-overflow: ellipsis; white-space: nowrap;',
                 # a field emptied by code keeps its placeholder on the right
                 'html[dir="rtl"] input[dir="auto"]:placeholder-shown { direction: rtl; unicode-bidi: isolate; }'):
        assert rule in idx, rule
    # every html[dir="rtl"] selector in the parts is in the list above
    css = "".join(p.read_text(encoding="utf-8") for p in sorted(PARTS.glob("0*.html"))
                  if p.name != build_i18n.PART_OUT.name)
    assert css.count('html[dir="rtl"]') == 24, css.count('html[dir="rtl"]')
    for rule in ('html[dir="rtl"] #near-clear { right:auto; left:6px; }',
                 'html[dir="rtl"] #near-input { padding:8px 12px 8px 28px; }',
                 'html[dir="rtl"] input[dir="auto"]:placeholder-shown { direction:rtl; unicode-bidi:isolate; }'):
        assert rule in plc, rule
    for page, src in (("index.html", idx), ("places.html", plc)):
        for token in ("SZ_RTL_LANGS", "function szLtr", "\\u2068", "root.dir = 'rtl'"):
            assert token in src, (page, token)


def test_numbers_reach_a_sentence_as_one_isolated_value() -> None:
    # A unit or a second number outside the placeholder ("({m} m)",
    # "({lat}, {lon})") is laid out right to left with the sentence:
    # "(m 120)", the pair swapped. The code passes one szLtr value instead,
    # so no table can get it wrong (review of 6e12a1a).
    drive = (PARTS / "600-driving-mode.js").read_text(encoding="utf-8")
    # in the reader's units, through the HUD's formatter, which returns szLtr runs
    assert "{ dist: formatDistanceLocalized(proj.offMeters, unit) }" in drive
    fmt = drive.split("function formatDistanceLocalized(", 1)[1].split("\n  }", 1)[0]
    assert fmt.count("return szLtr(") == 6, fmt
    plc = build_i18n._strip_generated(PLACES.read_text(encoding="utf-8"))
    assert "{ coords: szLtr(lat.toFixed(3) + ', ' + lon.toFixed(3)) }" in plc
    banner = (PARTS / "020-body-markup.html").read_text(encoding="utf-8")
    assert "szLtr(szFixed(mb / 1024, 1) + ' GB')" in banner and "szLtr(Math.round(mb) + ' MB')" in banner
    for lang, table in lang_tables().items():
        for key in ("drive.off_route", "places.near_coords"):
            assert not re.search(r"\{(m|lat|lon)\}", table[key]), (lang, key)


def test_an_empty_find_list_says_so_instead_of_counting_zero() -> None:
    # CLDR puts 0 in "one" for Portuguese ("0 resultado"): both count
    # paths of the Find page go through _szMatchCount, which has no count
    # for 0.
    plc = build_i18n._strip_generated(PLACES.read_text(encoding="utf-8"))
    assert plc.count("_szMatchCount(") == 3, "the function and both count paths"
    assert "szTn('places.n_matches'" in plc.split("function _szMatchCount", 1)[1].split("\n}", 1)[0]
    assert plc.count("szTn('places.n_matches'") == 1


@pytest.mark.parametrize("part, code", [
    # the arrival clock's digits follow the table's _nu (Latin in ar / fa)
    ("600-driving-mode.js", "new Intl.DateTimeFormat(SZ_I18N ? SZ_NUM_LANG : undefined,"),
    # an edited coordinate label is searched without its bidi isolates
    ("550-routing-pickers.js", "var q = inputEl.value.replace(/[\\u200e\\u200f\\u2066-\\u2069]/g, '').trim();"),
    # a picked point's coordinates keep their order
    ("540-routing-worker-bridge.js", "return szLtr(lat.toFixed(4) + ', ' + lon.toFixed(4));"),
    # the place sheet's button icon sits before the label on either side
    ("210-place-detail-sheet.js", "leadIcon.style.cssText = SZ_UI_RTL ? 'margin-left:6px;' : 'margin-right:6px;';"),
    # nearby rows align with the text
    ("240-on-map-find-ui.js", "'text-align:' + (SZ_UI_RTL ? 'right' : 'left') + '; font:inherit;'"),
    # an exit says "take exit", an on-ramp "take ramp onto" (szLinkKind)
    ("600-driving-mode.js", "if (kind === 'exit') {\n"
     "            streetEl.textContent = name ? szT('drive.exit_ramp_onto', 'take exit toward {road}', { road: name })\n"
     "                                        : szT('drive.exit_ramp', 'take exit');\n"
     "          } else if (kind === 'on') {\n"
     "            streetEl.textContent = name ? szT('drive.on_ramp_onto', 'take ramp onto {road}', { road: name })\n"
     "                                        : szT('drive.on_ramp', 'take ramp');"),
    # a translated UI writes the "Map data" month as About does (Persian calendar)
    ("120-map-init-and-style.js", "{ date: SZ_I18N ? _szMonth(config.buildDate) : config.buildDate });"),
    # the preview banner ends its sentence as the language does (hi "।")
    ("020-body-markup.html", "szT('places.status_sentence', '{text}.', { text: '' });"),
    # the About button's width, published for #info's max-width
    ("120-map-init-and-style.js", "document.documentElement.style.setProperty('--sz-attr-w', about.offsetWidth + 'px');"),
])
def test_right_to_left_code_paths(part: str, code: str) -> None:
    assert code in (PARTS / part).read_text(encoding="utf-8"), part


def test_the_hindi_sentence_end_is_the_danda() -> None:
    # The preview banner takes its full stop from places.status_sentence.
    tables = lang_tables()
    assert tables["hi"]["places.status_sentence"] == "{text}।"
    assert tables["th"]["places.status_sentence"] == "{text}"


def test_the_find_page_searches_an_edited_label_without_isolates() -> None:
    plc = build_i18n._strip_generated(PLACES.read_text(encoding="utf-8"))
    assert "const q = els.near.value.replace(/[\\u200e\\u200f\\u2066-\\u2069]/g, '').trim();" in plc


def test_there_are_thai_and_chinese_tables() -> None:
    # The language-matrix prototype's travellers (docs/language-matrix.md).
    tables = lang_tables()
    assert {"th", "zh"} <= set(tables)
    assert tables["zh"].get("_lang") == "zh-Hans"   # Simplified Chinese


@pytest.mark.parametrize("lang", sorted(build_i18n.languages()))
def test_table_has_every_key_and_no_stale_one(lang: str) -> None:
    en, table = en_table(), lang_tables()[lang]
    keys = {k for k in table if not build_i18n.is_dynamic(k) and not build_i18n.is_meta(k)}
    missing = sorted(set(en) - keys)
    stale = sorted(keys - set(en))
    assert not missing, f"{lang}.json lacks {missing}"
    assert not stale, f"{lang}.json has keys no code uses: {stale}"


@pytest.mark.parametrize("lang", sorted(build_i18n.languages()))
def test_placeholders_and_plural_forms_match(lang: str) -> None:
    en, table = en_table(), lang_tables()[lang]
    bad = []
    for key, text in table.items():
        if build_i18n.is_meta(key):
            continue   # table settings: build_i18n.languages() checks them
        if build_i18n.is_dynamic(key):
            if not re.fullmatch(r"type\.[a-z0-9_]+", key) or not isinstance(text, str) or not text.strip():
                bad.append(f"{key}: a place type needs an OSM-style key and a text")
            continue
        want = en.get(key)
        if isinstance(want, dict):
            if not isinstance(text, dict):
                bad.append(f"{key}: a count needs plural forms, not one text")
                continue
            need = PLURAL_FORMS.get(lang, {"other"})
            if not need <= set(text) or not set(text) <= set(build_i18n.PLURAL_CATEGORIES):
                bad.append(f"{key}: plural forms {sorted(text)}, {lang} needs {sorted(need)}")
            names = set(PLACEHOLDER.findall(want["other"]))
            for cat, form in text.items():
                got = set(PLACEHOLDER.findall(form))
                # "one" may spell the number out ("ein Link"); never add one.
                if (got != names and cat == "other") or not got <= names:
                    bad.append(f"{key}.{cat}: placeholders {sorted(got)} vs English {sorted(names)}")
            continue
        if not isinstance(text, str) or not text.strip():
            bad.append(f"{key}: empty or not text")
            continue
        got, names = sorted(set(PLACEHOLDER.findall(text))), sorted(set(PLACEHOLDER.findall(want or "")))
        if got != names:
            bad.append(f"{key}: placeholders {got} vs English {names}")
    assert not bad, "\n".join(bad)


def test_inlined_tables_hold_the_page_keys() -> None:
    idx = (ROOT / "resources" / "viewer" / "index.html").read_text(encoding="utf-8")
    plc = PLACES.read_text(encoding="utf-8")
    for page, src in (("index.html", idx), ("places.html", plc)):
        m = re.search(r'<script type="application/json" id="sz-i18n">(.*?)</script>', src, re.S)
        assert m, page
        tables = build_i18n.decode_tables(json.loads(m.group(1)))
        assert set(tables) == set(lang_tables()), page
        assert "szT" in src and "function szUseLanguage" in src, page
    # The Find page carries only its own keys, not the map viewer's.
    assert "drive.roundabout" not in _page_tables("places.html")["de"]
    assert "drive.roundabout" in _page_tables("index.html")["de"]


def _page_tables(page: str) -> dict[str, dict[str, Any]]:
    src = (ROOT / "resources" / "viewer" / page).read_text(encoding="utf-8")
    m = re.search(r'<script type="application/json" id="sz-i18n">(.*?)</script>', src, re.S)
    assert m, page
    return build_i18n.decode_tables(json.loads(m.group(1)))


@pytest.mark.parametrize("page", ["index.html", "places.html"])
def test_inlined_tables_decode_to_the_sources(page: str) -> None:
    # Compressed (build_i18n.encode_tables) but the same texts: every key
    # the page uses, every place type, every setting, in every language.
    src = (ROOT / "resources" / "viewer" / page).read_text(encoding="utf-8")
    keys = set(build_i18n.collect_page(build_i18n.index_sources() if page == "index.html"
                                       else build_i18n.places_source()))
    want = build_i18n.tables_for(keys, lang_tables())
    assert _page_tables(page) == want
    # Stored compressed: no table text appears in the page as it is.
    assert lang_tables()["de"]["routing.no_route"] not in src
    block = json.loads(re.search(r'id="sz-i18n">(.*?)</script>', src, re.S).group(1))
    assert block["v"] == build_i18n.FORMAT and set(block["t"]) == set(block["m"]) == set(lang_tables())


def test_check_ignores_how_zlib_compressed_the_same_tables() -> None:
    # Another zlib (version, level) packs the same tables into other bytes:
    # --check compares the decoded tables, not the bytes, so CI and the
    # build host agree; a changed text is still stale.
    src = (ROOT / "resources" / "viewer" / "places.html").read_text(encoding="utf-8")
    m = re.search(r'id="sz-i18n">(.*?)</script>', src, re.S)
    assert m
    block = json.loads(m.group(1))
    import base64
    import zlib
    def repack(s: str, level: int) -> str:
        raw = zlib.decompress(base64.b64decode(s), -15)
        c = zlib.compressobj(level, zlib.DEFLATED, -15)
        return base64.b64encode(c.compress(raw) + c.flush()).decode()
    other = dict(block, t={k: repack(v, 1) for k, v in block["t"].items()})
    assert other["t"]["de"] != block["t"]["de"]
    alt = src[:m.start(1)] + json.dumps(other) + src[m.end(1):]
    assert build_i18n._canonical(alt) == build_i18n._canonical(src)
    texts = build_i18n.unpack(block["t"]["de"])
    texts[texts.index(lang_tables()["de"]["places.title"])] = "Orte finden!"
    changed = dict(block, t=dict(block["t"], de=build_i18n._pack(texts)))
    alt = src[:m.start(1)] + json.dumps(changed) + src[m.end(1):]
    assert build_i18n._canonical(alt) != build_i18n._canonical(src)


def test_type_keys_cover_the_same_types_in_every_table() -> None:
    # Place types are the one family the key test cannot pin to en.json;
    # a new table covers the German one's.
    tables = lang_tables()
    want = {k for k in tables["de"] if build_i18n.is_dynamic(k)}
    for lang, table in tables.items():
        got = {k for k in table if build_i18n.is_dynamic(k)}
        assert got >= want, f"{lang}.json lacks {sorted(want - got)}"


# ------------------------------------------------- machine-translation note

def test_machine_tables_are_marked_and_reviewed_ones_are_not() -> None:
    tables = lang_tables()
    marked = {lang for lang, t in tables.items() if t.get("_machine") is True}
    assert marked == MACHINE
    # Both pages carry the flag (and _lang) in exactly those tables.
    for page in ("index.html", "places.html"):
        inlined = _page_tables(page)
        assert {lang for lang, t in inlined.items() if t.get("_machine") is True} == MACHINE, page
        for lang, t in tables.items():
            assert inlined[lang].get("_lang") == t.get("_lang"), (page, lang)


def test_machine_note_is_in_both_pages_hidden_until_the_runtime_shows_it() -> None:
    # About in the map viewer, the footer of the Find page: data-i18n-machine
    # (runtime.js szApplyI18n shows it only for a "_machine" table) and
    # display:none in the markup, so English and German never show it.
    idx = "".join(p.read_text(encoding="utf-8") for p in sorted(PARTS.glob("*.html"))
                  if p.name != build_i18n.PART_OUT.name)
    plc = build_i18n._strip_generated(PLACES.read_text(encoding="utf-8"))
    for page, src in (("index", idx), ("places", plc)):
        notes = re.findall(r"<[a-z]+\b[^>]*\bdata-i18n-machine\b[^>]*>", src)
        assert len(notes) == 1, (page, notes)
        assert 'data-i18n="common.machine_translated"' in notes[0], page
        assert re.search(r'style="display:\s*none;?"', notes[0]), page
    assert 'id="about-mt"' in idx
    en = en_table()["common.machine_translated"]
    assert "machine" in en
    for lang, table in lang_tables().items():
        assert table["common.machine_translated"].strip(), lang


@pytest.mark.parametrize("bad, why", [
    ({"_machine": "yes"}, "must be a bool"),
    ({"_machine": 1}, "must be a bool"),
    ({"_lang": "de-Hans"}, "must be zh-<subtag>"),
    ({"_lang": "zh"}, "must be zh-<subtag>"),
    ({"_reviewed": True}, "unknown table setting"),
    ({"_compact": True}, "must be a str"),
    ({"_compact": "myriad"}, 'must be "intl"'),
    ({"_compact": "intl-short"}, 'must be "intl" or "intl-long"'),
])
def test_table_settings_are_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                    bad: dict[str, Any], why: str) -> None:
    (tmp_path / "zh.json").write_text(json.dumps({"common.close": "关闭", **bad}), encoding="utf-8")
    monkeypatch.setattr(build_i18n, "I18N", tmp_path)
    with pytest.raises(build_i18n.I18nError, match=re.escape(why)):
        build_i18n.languages()
    (tmp_path / "zh.json").write_text(json.dumps({"_machine": True, "_lang": "zh-Hans"}), encoding="utf-8")
    assert build_i18n.languages() == {"zh": {"_machine": True, "_lang": "zh-Hans"}}


# --------------------------------------------------------------- markup lint

LETTERS = re.compile(r"[A-Za-z]{2}")


class _Visible(HTMLParser):
    """Text and title/placeholder/aria-label attributes outside a
    data-i18n / data-i18n-code / translate="no" element."""

    VOID = build_i18n._Markup.VOID

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.skip = 0
        self.found: list[str] = []

    def covered(self) -> bool:
        return any(c for _, c in self.stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
            return
        cover = ("data-i18n" in a or "data-i18n-code" in a or a.get("translate") == "no")
        for name in ("title", "placeholder", "aria-label", "alt"):
            v = a.get(name)
            if v and LETTERS.search(v) and not (f"data-i18n-{name}" in a or a.get("translate") == "no"
                                                or self.covered()):
                self.found.append(f"<{tag}> {name}={v!r}")
        if tag in self.VOID:
            return
        self.stack.append((tag, cover))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.skip -= 1
            return
        while self.stack:
            t, _ = self.stack.pop()
            if t == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.skip or not data.strip() or not LETTERS.search(data):
            return
        if not self.covered():
            self.found.append(" ".join(data.split())[:80])


def _markup_misses(src: str) -> list[str]:
    p = _Visible()
    p.feed(src)
    return p.found


def test_markup_text_is_translatable() -> None:
    misses = []
    for part in sorted(PARTS.glob("*.html")):
        if part.name == build_i18n.PART_OUT.name:
            continue
        misses += [f"{part.name}: {m}" for m in _markup_misses(part.read_text(encoding="utf-8"))]
    misses += [f"places.html: {m}" for m in _markup_misses(build_i18n._strip_generated(
        PLACES.read_text(encoding="utf-8")))]
    assert not misses, "add data-i18n (or translate=\"no\"):\n" + "\n".join(misses)


def test_markup_lint_catches_a_bare_text() -> None:
    assert _markup_misses('<div><button title="Close it">x</button><p>Hello there</p></div>') == [
        "<button> title='Close it'", "Hello there"]
    assert _markup_misses('<p data-i18n="a.b">Hello there</p><p translate="no">GPL v2</p>') == []


# ------------------------------------------------------------ JavaScript lint

def _strip_calls(src: str) -> str:
    """src with every szT(…)/szTn(…) call blanked out, so the English inside
    them is not reported."""
    out, i = [], 0
    for m in re.finditer(r"\bszTn?\(", src):
        if m.start() < i:
            continue
        depth, j = 0, m.end() - 1
        q = None
        while j < len(src):
            c = src[j]
            if q:
                if c == "\\":
                    j += 2
                    continue
                if c == q:
                    q = None
            elif c in "'\"`":
                q = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(src[i:m.start()])
        out.append("szT()")
        i = j + 1
    out.append(src[i:])
    return "".join(out)


_LIT = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`")

# A sink, then the extent of what flows into it: an assignment's right-hand
# side (to the end of the statement) or a call's arguments.
_SINK_PROPS = r"(?:textContent|innerText|title|placeholder|innerHTML|value)"
_ASSIGN = re.compile(r"\." + _SINK_PROPS + r"\s*\+?=(?!=)")
_SINK_CALLS = (r"\b(?:setStatus|setRoutingStatus|_showFindToast|showFatalError|createEl|_detailActionBtn"
               r"|_detailFactRow|makeChip|szFatalPage|createTextNode)\(|\bsetAttribute\(\s*'(?:aria-label|title"
               r"|placeholder|alt)'|\bfacts\.push\(|\badd\('(?:h1|p|li|button|summary)'")
_CALLS = re.compile(_SINK_CALLS)
# Text parked in a variable or a lookup table first, then sunk:
#   var label = 'Satellite'; … btn.textContent = label;
#   var LABELS = { drive: 'Drive' }; … btn.textContent = LABELS[m];
_VAR_LIT = re.compile(r"\b(?:var|let|const)\s+(\w+)\s*=\s*('(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\")\s*[;,\n]")
_VAR_OBJ = re.compile(r"\b(?:var|let|const)\s+(\w+)\s*=\s*\{([^{}]*)\}")

# Deliberately English (debug output, technical detail, glyphs).
ALLOW = (
    "'build:?'",
    "'\\nUser agent: '", "'Missing: '", "'\\nPage: '", "'\\nURL: '",
    "'UA: '", "'URL: '", "'Base: '",
)


# Words a person reads: two words, or a capitalised one ("Satellite").
# Lower-case single tokens are identifiers ('div', 'loading', 'walk').
TEXTY = re.compile(r"[A-Za-z]{2,}[^\S\n]+[A-Za-z]|^[^A-Za-z]*[A-Z][a-z]")


def _text_of(lit: str) -> str:
    body = lit[1:-1]
    if lit[0] == "`":
        body = re.sub(r"\$\{[^}]*\}", "", body)
    # Markup in innerHTML: only the text between tags counts.
    body = re.sub(r"<[^>]*>", " ", body)
    body = re.sub(r"&\w+;", " ", body)
    return body


def _extent(src: str, start: int, call: bool) -> str:
    depth, j, q = (1 if call else 0), start, None
    while j < len(src):
        c = src[j]
        if q:
            if c == "\\":
                j += 2
                continue
            if c == q:
                q = None
        elif c in "'\"`":
            q = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth < (1 if call else 0):
                break
        elif c == ";" and depth <= (1 if call else 0):
            break
        j += 1
    return src[start:j]


def _strip_comments(src: str) -> str:
    lines = []
    for line in src.split("\n"):
        t = line.lstrip()
        if t.startswith(("//", "*", "/*")):
            lines.append("")
            continue
        lines.append(re.sub(r"(^|\s)//.*$", "", line))
    return "\n".join(lines)


def _sunk(src: str, name: str) -> int | None:
    """Offset of a sink that takes variable `name` (or `name[…]`) directly."""
    nm = re.escape(name)
    m = (re.search(r"\." + _SINK_PROPS + r"\s*\+?=(?!=)\s*(?:[^;\n]*[?:]\s*)?" + nm + r"\b", src)
         or re.search(r"(?:" + _SINK_CALLS + r")\s*(?:[^;)\n]*,\s*)?" + nm + r"\b", src))
    return m.start() if m else None


def js_misses(src: str) -> list[str]:
    src = _strip_calls(_strip_comments(src))
    found = []
    for rx in (_VAR_LIT, _VAR_OBJ):
        for m in rx.finditer(src):
            lits = [m.group(2)] if rx is _VAR_LIT else _LIT.findall(m.group(2))
            texty = [lit for lit in lits if lit not in ALLOW and TEXTY.search(_text_of(lit))
                     and not re.search(r"[:;]\s*[\w#(-]|^.(?:https?:|tel:|#|\.|/)", lit)]
            if texty and _sunk(src, m.group(1)) is not None:
                line = src.count("\n", 0, m.start()) + 1
                found.append(f"line {line}: {m.group(1)} = {texty[0][:60]} (sunk later)")
    for rx, call in ((_ASSIGN, False), (_CALLS, True)):
        for m in rx.finditer(src):
            rhs = _extent(src, m.end(), call)
            for lit in _LIT.findall(rhs):
                if lit in ALLOW or not TEXTY.search(_text_of(lit)):
                    continue
                # CSS and URLs are not text.
                if re.search(r"[:;]\s*[\w#(-]|^.(?:https?:|tel:|#|\.|/)", lit) and "<" not in lit:
                    continue
                line = src.count("\n", 0, m.start()) + 1
                found.append(f"line {line}: {lit[:70]}")
    return found


def _js_sources() -> list[tuple[str, str]]:
    out = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(PARTS.iterdir())
           if p.suffix in (".js", ".html") and p.name != build_i18n.PART_OUT.name]
    out.append(("places.html", build_i18n._strip_generated(PLACES.read_text(encoding="utf-8"))))
    return out


def test_no_english_literal_reaches_a_text_sink() -> None:
    misses = [f"{name} {m}" for name, src in _js_sources() for m in js_misses(src)]
    assert not misses, "wrap in szT('key', 'English'):\n" + "\n".join(misses)


def test_js_lint_catches_a_bare_literal() -> None:
    assert js_misses("el.textContent = 'Hello world';") == ["line 1: 'Hello world'"]
    assert js_misses("b.title = on ? 'Exit now' : szT('a.b', 'Enter');") == ["line 1: 'Exit now'"]
    assert js_misses("setStatus(`Loading ${x}…`, 'loading');") == ["line 1: `Loading ${x}…`"]
    assert js_misses("el.textContent = szT('a.b', 'Hello world', { n: 'two words' });") == []
    assert js_misses("el.style.cssText = 'color:red; font-size:12px';") == []
    # The reviewer's mutations (2026-10-05): each must be caught.
    assert js_misses("p.appendChild(document.createTextNode('Change map'));") == ["line 1: 'Change map'"]
    assert js_misses("originInput.value = 'Current location';") == ["line 1: 'Current location'"]
    assert js_misses("var lbl = 'Hello world';\nel.textContent = lbl;") == [
        "line 1: lbl = 'Hello world' (sunk later)"]
    assert js_misses("var L = { drive: 'Drive', walk: 'Walk' };\nb.textContent = L[m];") == [
        "line 1: L = 'Drive' (sunk later)"]
    assert js_misses("var key = 'streetzim.units';\nel.value = key;") == []
    assert js_misses("input.value = '';") == []
    assert js_misses("el.innerHTML += '<b>Hello world</b>';") == ["line 1: '<b>Hello world</b>'"]
    assert js_misses("el.textContent += ' more text';") == ["line 1: ' more text'"]
