"""The UI gate's html_lang verdict (tools/langpack/ui_verdict.mjs, run by
node): a map language needs its UI table, unless the viewer declares a
fallback (SZ_UI_FALLBACK: Romansh -> German) and shows that one."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests.langpack.conftest import ROOT

NODE = os.environ.get("NODE") or shutil.which("node")
if not NODE or not os.path.exists(NODE):
    pytest.skip("node", allow_module_level=True)

CASES = [
    # (facts, pass, ui) — facts: lang, uiLang, hasTable, htmlLang, dir, rtl, shownCount, fallback
    ({"lang": "rm", "uiLang": "de", "hasTable": True, "htmlLang": "de", "shownCount": 3, "fallback": "de"}, True, "de"),
    ({"lang": "rm", "uiLang": "de", "hasTable": True, "htmlLang": "de", "shownCount": 3, "fallback": None}, False, "rm"),
    ({"lang": "rm", "uiLang": "en", "hasTable": False, "htmlLang": "en", "shownCount": 0, "fallback": "de"}, False, "rm"),
    ({"lang": "rm", "uiLang": "de", "hasTable": True, "htmlLang": "rm", "shownCount": 3, "fallback": "de"}, False, "de"),
    ({"lang": "th", "uiLang": "th", "hasTable": True, "htmlLang": "th", "shownCount": 2, "fallback": None}, True, "th"),
    ({"lang": "th", "uiLang": "en", "hasTable": False, "htmlLang": "en", "shownCount": 0, "fallback": None}, False, "th"),
    ({"lang": "ar", "uiLang": "ar", "hasTable": True, "htmlLang": "ar", "dir": "", "rtl": True, "shownCount": 2,
      "fallback": None}, False, "ar"),
    ({"lang": "ar", "uiLang": "ar", "hasTable": True, "htmlLang": "ar", "dir": "rtl", "rtl": True, "shownCount": 2,
      "fallback": None}, True, "ar"),
    ({"lang": "en", "uiLang": "en", "hasTable": False, "htmlLang": "en", "shownCount": 0, "fallback": None}, True, "en"),
    ({"lang": "en", "uiLang": "de", "hasTable": True, "htmlLang": "de", "shownCount": 1, "fallback": None}, False, "en"),
]


def test_the_ui_verdict_honours_the_viewers_fallback() -> None:
    """Romansh shows the German UI the viewer declares for it (SZ_UI_FALLBACK):
    PASS, saying so; without a declared fallback, or with another UI, FAIL
    (the inline check FAILED every rm entry: 'no rm UI table: the UI is in de')."""
    script = ("import { uiVerdict } from " + json.dumps(str(ROOT / "tools" / "langpack" / "ui_verdict.mjs")) + ";\n"
              "const cases = JSON.parse(process.argv[1]);\n"
              "console.log(JSON.stringify(cases.map((c) => uiVerdict(c))));\n")
    r = subprocess.run([NODE, "--input-type=module", "-e", script, json.dumps([c for c, _, _ in CASES])],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    for (facts, ok, ui), v in zip(CASES, got):
        assert (v["pass"], v["ui"]) == (ok, ui), (facts, v)
    assert got[0]["fallbackUsed"] == "de" and got[4]["fallbackUsed"] is None
    assert "the viewer's fallback for rm is de" in got[2]["why"][0], got[2]


LABEL_CASES = [
    # Vietnamese on Switzerland: Zurich is "Zürich" in both, no rendered place names itself
    # differently in vi, the tiles carry vi: nothing to show bilingually, nothing wrong (the gate failed it)
    ({"second": True, "usesKey": True, "lang2": "vi", "lang": "vi", "cityRendered": True, "cityShown": "Zürich",
      "cityWant": "Zürich", "bilingual": 0, "bilingualWrong": 0, "ownLang": False, "tileLanguages": ["de", "vi"]},
     True, True),
    # the same, but the tiles do not carry the language: FAIL
    ({"second": True, "usesKey": True, "lang2": "vi", "lang": "vi", "cityRendered": True, "cityShown": "Zürich",
      "cityWant": "Zürich", "bilingual": 0, "bilingualWrong": 0, "ownLang": False, "tileLanguages": ["de"]},
     False, False),
    # a region language (rm on Switzerland): its names are the native ones
    ({"second": True, "usesKey": True, "lang2": "rm", "lang": "rm", "cityRendered": True, "cityShown": "Turitg",
      "cityWant": "Turitg", "bilingual": 0, "bilingualWrong": 0, "ownLang": True, "tileLanguages": None}, True, False),
    # bilingual labels shown, one against the rule: FAIL
    ({"second": True, "usesKey": True, "lang2": "el", "lang": "el", "cityRendered": True, "cityShown": "Αθήνα",
      "cityWant": "Αθήνα", "bilingual": 2, "bilingualWrong": 1, "ownLang": False, "tileLanguages": ["el"]}, False, False),
    # the city's label not what the rule says: FAIL
    ({"second": True, "usesKey": True, "lang2": "th", "lang": "th", "cityRendered": True, "cityShown": "Zürich",
      "cityWant": "ซูริก · Zürich", "bilingual": 1, "bilingualWrong": 0, "ownLang": False, "tileLanguages": ["th"]},
     False, False),
    # the base: one name, no second language
    ({"second": False, "lang2": "", "lang": "en", "cityRendered": True, "cityShown": "Zürich", "cityLocal": "Zürich"},
     True, False),
    ({"second": False, "lang2": "vi", "lang": "en", "cityRendered": True, "cityShown": "Zürich", "cityLocal": "Zürich"},
     False, False),
]


def test_the_labels_verdict_has_a_vacuous_case() -> None:
    """Re-gate of Switzerland's vi entry: some rendered label must show two
    names, unless none can (a region language, or a map language whose names
    in view agree with the local ones while the tiles carry it)."""
    script = ("import { labelsVerdict } from " + json.dumps(str(ROOT / "tools" / "langpack" / "ui_verdict.mjs")) + ";\n"
              "console.log(JSON.stringify(JSON.parse(process.argv[1]).map((c) => labelsVerdict(c))));\n")
    r = subprocess.run([NODE, "--input-type=module", "-e", script, json.dumps([c for c, _, _ in LABEL_CASES])],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    for (facts, ok, vacuous), v in zip(LABEL_CASES, json.loads(r.stdout)):
        assert (v["pass"], v["vacuous"]) == (ok, vacuous), (facts, v)


SEARCH_CASES = [
    # (rows: [shown name, data-name], the region file's expect, found)
    ([["Warschau", "Warsaw"], ["Warszawa", "Warszawa"]], "Warsaw", True),         # German on Poland: the city's data-name
    ([["Warszawa", "Warszawa"], ["Warszawa Centralna", "Warszawa Centralna"]], "Warsaw", False),   # other records only
    ([["Warsaw", "Warsaw"]], "Warsaw", True),                                     # English
    ([["ลอสแอนเจลิส", "Los Angeles"]], "ลอสแอนเจลิส", True),                       # a hand-written expect: the shown name
    ([["Warsaw Chopin", "Warsaw Chopin Airport"]], "Warsaw", True),               # the name, then more
    ([["Warsawski", "Warsawski"]], "Warsaw", False),                              # a longer word is not the name
    ([["Warschau", "Warsaw"]], ["Varsovie", "Warsaw"], True),                     # a list
    ([], "Warsaw", False),
]


def test_the_search_check_finds_the_place_by_its_shown_name_or_its_records_name() -> None:
    """Poland's 21 non-English entries failed search_native: a German map
    shows Warsaw as Warschau; the row's data-name is still the record's name."""
    script = ("import { searchFound } from " + json.dumps(str(ROOT / "tools" / "langpack" / "ui_verdict.mjs")) + ";\n"
              "console.log(JSON.stringify(JSON.parse(process.argv[1]).map(([rows, e]) =>\n"
              "  searchFound(rows.map(([shown, name]) => ({ shown, name })), e))));\n")
    r = subprocess.run([NODE, "--input-type=module", "-e", script, json.dumps([[n, e] for n, e, _ in SEARCH_CASES])],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [ok for _, _, ok in SEARCH_CASES]
