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


def test_a_copy_one_viewer_patch_old_still_builds(ship: SimpleNamespace, tmp_path: Path) -> None:  # noqa: F811
    """Plan review B2: recipes made from a base whose viewer was patched in
    place; the visitor's copy has the viewer before the patch. The page's
    check of the copy and the worker's chunk checks skip the viewer slots
    (the recipe carries them), so the copy builds the new file, byte for
    byte; one-click from the patched base on the server too."""
    from tests.langpack import lpzims
    from tests.langpack.test_viewer_slots import V2, patch
    old = tmp_path / "old" / "osm-testland-2026-10-13.zim"
    old.parent.mkdir()
    lpzims.write_base(old, slots=lpzims.VIEWER_V1)
    base = tmp_path / "bases-s" / old.name
    base.parent.mkdir()
    patch(old, base, V2, tmp_path)
    builder = tmp_path / "builder-s.py"
    builder.write_text(f"""import argparse, json, os, sys
sys.path.insert(0, {str(Path(lpzims.__file__).parent)!r})
import lpzims
ap = argparse.ArgumentParser(); ap.add_argument("base"); ap.add_argument("--lang"); ap.add_argument("-o"); ap.add_argument("--stats")
a, _ = ap.parse_known_args()
lpzims.write_pack(__import__("pathlib").Path(a.o), a.lang, lpzims.read_uuid(a.base))
open(a.stats, "w").write(json.dumps({{"zim_bytes": os.path.getsize(a.o), "check": {{"ok": True, "problems": []}}}}))
""")
    import hashlib
    (ship.work / "ia-cache" / "ia-streetzim-testland.json").write_text(json.dumps(
        {"files": [{"name": base.name, "size": str(base.stat().st_size), "md5": hashlib.md5(base.read_bytes()).hexdigest()}]}))
    r = subprocess.run(["bash", str(ROOT / "ops" / "langpack" / "ship_packs.sh"), "testland", str(base)],
                       capture_output=True, text=True, timeout=900, env={**ship.env, "BUILDER": str(builder)})
    assert r.returncode == 0, r.stdout[-3000:]
    rec = tmp_path / "recipes"
    shutil.copytree(ship.work / "site" / "testland" / "recipes" / "testland", rec / "testland")
    assert subprocess.run([sys.executable, str(ROOT / "tools" / "langpack" / "site_index.py"), str(rec)],
                          capture_output=True).returncode == 0
    p = SimpleNamespace(ship=SimpleNamespace(base=old), rec=rec, packs=ship.work / "packs" / base.name[:-4],
                        out=tmp_path / "out")
    rc, rows = _e2e(p, "de", "file")                    # --base: the OLD copy
    assert rc == 0 and rows[0]["pass"], rows
    p.ship = SimpleNamespace(base=base)
    rc, rows = _e2e(p, "th", "net")                     # the server's (patched) base
    assert rc == 0 and rows[0]["pass"], rows


def test_packs_stream_and_are_never_held_whole(ship: SimpleNamespace, tmp_path: Path) -> None:  # noqa: F811
    """Plan review M-3: the page checks only each pack's first and last chunk
    before Build; the worker streams the rest during the build, chunk by
    verified chunk (a small chunk size here, so a pack spans many)."""
    env = {**ship.env, "LANGPACK_RECIPE_CHUNK": "4096"}
    r = subprocess.run(["bash", str(ROOT / "ops" / "langpack" / "ship_packs.sh"), "testland", str(ship.base)],
                       capture_output=True, text=True, timeout=900, env=env)
    assert r.returncode == 0, r.stdout[-3000:]
    rec = tmp_path / "recipes"
    shutil.copytree(ship.work / "site" / "testland" / "recipes" / "testland", rec / "testland")
    assert subprocess.run([sys.executable, str(ROOT / "tools" / "langpack" / "site_index.py"), str(rec)],
                          capture_output=True).returncode == 0
    recipe = json.loads((rec / "testland" / "de.json").read_text())
    chunks = {k: len(p["sha256"]) for k, p in recipe["packs"].items()}
    assert recipe["packs"]["de"]["chunk"] == 4096 and min(chunks.values()) > 4, chunks
    p = SimpleNamespace(ship=ship, rec=rec, packs=ship.work / "packs" / ship.base.name[:-4], out=tmp_path / "out")
    rc, rows = _e2e(p, "de", "file")
    assert rc == 0 and rows[0]["pass"], rows
    before = rows[0]["proxy_before_build"]["files"]
    probe = json.loads((rec / "index.json").read_text())["regions"][0]["probe"]["url"].rsplit("/", 1)[1]
    for k, pk in recipe["packs"].items():
        # its first and last chunk (and, for the speed probe's file, the probe's 4 ranges)
        assert before.get(pk["file"], 0) <= 2 + (4 if pk["file"] == probe else 0), (k, before, probe)
    total = rows[0]["proxy"]["items"].get("streetzim-testland-lang", 0)
    assert total >= sum(chunks.values()), (total, chunks)       # the rest streamed during the build


def test_the_page_offers_each_torrent_and_builds_the_torrents_file(ship: SimpleNamespace, tmp_path: Path) -> None:  # noqa: F811
    """With language torrents (LANGPACK_WEBSEED): the download list links every
    entry's torrent, and the file the page builds hashes piece for piece as
    that torrent says (a visitor who built it can seed it)."""
    import hashlib
    from tools.langpack.lang_torrents import bdecode
    r = _run(ship, LANGPACK_WEBSEED="https://streetzim-build-proxy.example.workers.dev")
    assert r.returncode == 0, r.stdout[-2000:]
    rec = tmp_path / "recipes"
    shutil.copytree(ship.work / "site" / "testland" / "recipes" / "testland", rec / "testland")
    assert subprocess.run([sys.executable, str(ROOT / "tools" / "langpack" / "site_index.py"), str(rec)],
                          capture_output=True).returncode == 0
    p = SimpleNamespace(ship=ship, rec=rec, packs=ship.work / "packs" / ship.base.name[:-4], out=tmp_path / "out")
    rc, rows = _e2e(p, "de")
    assert rc == 0 and rows[0]["pass"], rows
    shown = json.loads((p.out / "e2e-de.json").read_text())["shown"]
    assert shown["torrents"] == [f"recipes/testland/{x}.torrent" for x in ("de", "fr", "th", "en")], shown
    info = bdecode((rec / "testland" / "de.torrent").read_bytes())[b"info"]
    data = (p.out / "testland-de.zim").read_bytes()
    plen = info[b"piece length"]
    assert info[b"length"] == len(data)
    assert info[b"pieces"] == b"".join(hashlib.sha1(data[i:i + plen]).digest() for i in range(0, len(data), plen))
