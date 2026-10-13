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
                "th": {"other"}, "zh": {"other"}}
# Tables made by machine translation (their "_machine": true shows the note);
# German is reviewed by a person.
MACHINE = {"el", "es", "fr", "ja", "ko", "th", "zh"}

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
        tables = json.loads(m.group(1))
        assert set(tables) == set(lang_tables()), page
        assert "szT" in src and "function szUseLanguage" in src, page
    # The Find page carries only its own keys, not the map viewer's.
    m = re.search(r'id="sz-i18n">(.*?)</script>', plc, re.S)
    assert m
    assert "drive.roundabout" not in json.loads(m.group(1))["de"]


def _page_tables(page: str) -> dict[str, dict[str, Any]]:
    src = (ROOT / "resources" / "viewer" / page).read_text(encoding="utf-8")
    m = re.search(r'<script type="application/json" id="sz-i18n">(.*?)</script>', src, re.S)
    assert m, page
    return json.loads(m.group(1))


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
