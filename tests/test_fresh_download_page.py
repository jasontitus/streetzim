"""The main page's Download of a map with today's viewer, in headless
Chromium (tests/fresh_download_page.mjs, the template's own script): the
build worker's file when it answers the HEAD, else archive.org's
(data-fallback); one check for a double click.

Needs the browser stack (NODE, CHROME_PATH, PLAYWRIGHT_CORE; BROWSER_LIBS on
the build host); skips without it."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NEED = ("NODE", "CHROME_PATH", "PLAYWRIGHT_CORE")
if not all(os.environ.get(k) and os.path.exists(os.environ[k]) for k in NEED):
    pytest.skip(f"the browser stack: set {', '.join(NEED)}", allow_module_level=True)


def test_the_worker_s_file_else_archive_org():
    r = subprocess.run([os.environ["NODE"], str(ROOT / "tests" / "fresh_download_page.mjs"), str(ROOT / "web" / "template.html")],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["errors"] == []
    assert "/merged/taipei-viewer/en/" in out["taipei"], out
    assert out["hawaii"].endswith("/fallback/osm-hawaii.zim"), out
    heads = [x for x in out["requests"] if x.startswith("HEAD /merged/taipei-viewer/")]
    assert len(heads) == 1, out["requests"]                    # the double click asked once
