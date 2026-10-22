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
