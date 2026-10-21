"""The builder page (web/build/) in headless Chromium on the small ZIMs,
through tools/langpack/builder_e2e.mjs: ship_packs.sh's site files, a local
stand-in for the site and one for the range proxy. One-click and "from my
copy" must save files byte-identical to the gated merges; a pack or a base
chunk the proxy corrupts must stop the build with nothing saved (the page's
pack check, the worker's chunk SHA-256: review r3 M-5).

Needs the browser stack (ops/langpack/host-env.sh): NODE, CHROME_PATH,
PLAYWRIGHT_CORE (and BROWSER_LIBS on the build host); skips without it."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("libzim")

from tests.langpack import lpzims  # noqa: E402
from tests.langpack.conftest import ROOT  # noqa: E402
from tests.langpack.test_ship_packs import _run, ship  # noqa: E402,F401

NEED = ("NODE", "CHROME_PATH", "PLAYWRIGHT_CORE")
if not all(os.environ.get(k) and os.path.exists(os.environ[k]) for k in NEED):
    pytest.skip(f"the browser stack: set {', '.join(NEED)} (ops/langpack/host-env.sh)", allow_module_level=True)


@pytest.fixture
def page(ship: SimpleNamespace, tmp_path: Path) -> SimpleNamespace:  # noqa: F811
    r = _run(ship)
    assert r.returncode == 0, r.stdout[-2000:]
    rec = tmp_path / "recipes"
    shutil.copytree(ship.work / "site" / "testland" / "recipes" / "testland", rec / "testland")
    assert subprocess.run([sys.executable, str(ROOT / "tools" / "langpack" / "site_index.py"), str(rec)],
                          capture_output=True).returncode == 0
    return SimpleNamespace(ship=ship, rec=rec, packs=ship.work / "packs" / ship.base.name[:-4], out=tmp_path / "out")


def _e2e(p: SimpleNamespace, langs: str, mode: str = "net", *extra: str) -> tuple[int, list[dict]]:
    cmd = [os.environ["NODE"], str(ROOT / "tools" / "langpack" / "builder_e2e.mjs"), "--web", str(ROOT / "web"),
           "--recipes", str(p.rec), "--item", f"streetzim-testland={p.ship.base.parent}",
           "--item", f"streetzim-testland-lang={p.packs}", "--region", "testland", "--langs", langs,
           "--out", str(p.out), "--mode", mode, "--base", str(p.ship.base), *extra]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    rows = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
    return r.returncode, rows


def test_one_click_and_from_my_copy(page: SimpleNamespace) -> None:
    rc, rows = _e2e(page, "de,th,en")
    assert rc == 0 and [x["lang"] for x in rows] == ["de", "th", "en"] and all(x["pass"] for x in rows), rows
    rc, rows = _e2e(page, "de", "file")
    assert rc == 0 and rows[0]["pass"] and rows[0]["proxy"]["items"].get("streetzim-testland", 0) == 0, rows


def test_a_corrupt_pack_saves_nothing(page: SimpleNamespace) -> None:
    pack = next(p.name for p in page.packs.glob("osm_testland_wiki-de_*.zim"))
    rc, rows = _e2e(page, "de", "net", "--corrupt", f"streetzim-testland-lang/{pack}@2000", "--expect", "fail")
    assert rc == 0 and rows[0]["pass"], rows
    assert rows[0]["status"].startswith("Not started: Wikipedia could not be downloaded"), rows


def test_a_corrupt_base_chunk_stops_the_build(page: SimpleNamespace) -> None:
    at = page.ship.base.stat().st_size // 2
    rc, rows = _e2e(page, "th", "net", "--corrupt", f"streetzim-testland/{page.ship.base.name}@{at}", "--expect", "fail")
    assert rc == 0 and rows[0]["pass"], rows
    # the worker's chunk check stops it at the chunk, before the final MD5
    assert rows[0]["status"].startswith("Stopped") and "does not match the recipe" in rows[0]["status"], rows
