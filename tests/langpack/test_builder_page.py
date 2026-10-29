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
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("libzim")

from tests.langpack.conftest import ROOT  # noqa: E402
from tests.langpack.test_ship_packs import _run, ship  # noqa: E402,F401

NEED = ("NODE", "CHROME_PATH", "PLAYWRIGHT_CORE")
if not all(os.environ.get(k) and os.path.exists(os.environ[k]) for k in NEED):
    pytest.skip(f"the browser stack: set {', '.join(NEED)} (the build host's settings: docs/language-pack-pipeline.md)", allow_module_level=True)


@pytest.fixture
def page(ship: SimpleNamespace, tmp_path: Path) -> SimpleNamespace:  # noqa: F811
    r = _run(ship)
    assert r.returncode == 0, r.stdout[-2000:]
    rec = tmp_path / "recipes"
    shutil.copytree(ship.work / "site" / "testland" / "recipes" / "testland", rec / "testland")
    assert subprocess.run([sys.executable, str(ROOT / "tools" / "langpack" / "site_index.py"), str(rec)],
                          capture_output=True).returncode == 0
    return SimpleNamespace(ship=ship, rec=rec, packs=ship.work / "packs" / ship.base.name[:-4], out=tmp_path / "out")


def _e2e(p: SimpleNamespace, langs: str, mode: str = "net", *extra: str, env: dict | None = None,
         timeout: int = 1800) -> tuple[int, list[dict]]:
    cmd = [os.environ["NODE"], str(ROOT / "tools" / "langpack" / "builder_e2e.mjs"), "--web", str(ROOT / "web"),
           "--recipes", str(p.rec), "--item", f"streetzim-testland={p.ship.base.parent}",
           "--item", f"streetzim-testland-lang={p.packs}", "--region", "testland", "--langs", langs,
           "--out", str(p.out), "--mode", mode, "--base", str(p.ship.base), *extra]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})})
    rows = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
    return r.returncode, rows


def test_one_click_and_from_my_copy(page: SimpleNamespace) -> None:
    rc, rows = _e2e(page, "de,th,en")
    assert rc == 0 and [x["lang"] for x in rows] == ["de", "th", "en"] and all(x["pass"] for x in rows), rows
    rc, rows = _e2e(page, "de", "file")
    assert rc == 0 and rows[0]["pass"] and rows[0]["proxy"]["items"].get("streetzim-testland", 0) == 0, rows


def _monotonic(xs: list) -> bool:
    from itertools import pairwise
    return all(b >= a for a, b in pairwise(xs))


PHASES = ["prepare", "plan", "check", "map", "copy", "packs", "finishing", "verify", "saving"]


def test_a_corrupt_pack_saves_nothing(page: SimpleNamespace) -> None:
    pack = next(p.name for p in page.packs.glob("osm_testland_wiki-de_*.zim"))
    rc, rows = _e2e(page, "de", "net", "--corrupt", f"streetzim-testland-lang/{pack}@2000", "--expect", "fail")
    assert rc == 0 and rows[0]["pass"], rows
    assert rows[0]["status"].startswith("Not started: Wikipedia could not be downloaded"), rows
    ui = rows[0]["ui"]
    # the job ended: the button is itself again (review LOW: a mutant of jobEnd survived)
    b = ui["oneclick"]
    assert b["text"].startswith("Download") and "busy" not in b["cls"] and not b["disabled"] and b["ariaDisabled"] is None, b
    assert ui["jobHidden"] is True and ui["langDisabled"] is False, ui
    # the damaged check chunk was downloaded again from 0: the bar did not go back
    assert ui["bar"] and _monotonic(ui["bar"]), ui["bar"]


@pytest.mark.parametrize("throttle", ["20000:5000:20", "750:250:100"])     # 20 Mbit/s; 3G
def test_every_click_shows_progress_at_once(page: SimpleNamespace, throttle: str, tmp_path: Path) -> None:
    """User report on the Swiss preview: nothing visible for 20 s after a
    click (the recipe and the pack checks loaded silently). The button turns
    busy and the job panel appears within the click; the panel changes at
    least every 1.5 s until the file is done, at phone width, the network
    slowed by Chromium's emulation."""
    shots = tmp_path / "shots"
    rc, rows = _e2e(page, "de", "net", "--throttle", throttle, "--watch-secs", "120", "--viewport", "390x844",
                    "--shots", str(shots))
    assert rc == 0 and rows[0]["pass"], rows
    fb = rows[0]["feedback"]
    assert fb["first_change_ms"] is not None and fb["first_change_ms"] < 200, fb
    assert fb["max_gap_ms"] < 1500 and fb["max_progress_gap_ms"] < 1500, fb
    assert rows[0]["phases"] and rows[0]["phases"][-1] in ("saving", "verify", "packs", "map"), rows[0]["phases"]
    assert list(shots.glob("de-390x844-*.png")), list(shots.iterdir())


def test_a_damaged_chunk_counts_once_and_nothing_goes_back(ship: SimpleNamespace, tmp_path: Path) -> None:  # noqa: F811
    """Review LOWs: a pack chunk the worker gets damaged once is downloaded
    again and its bytes count once (the packs' count never passes their
    total); the phase never goes back (the tail's reads of the base are
    "finishing", not "map" again); the bar never goes back; the busy button
    keeps the focus (aria-disabled, not disabled)."""
    env = {**ship.env, "LANGPACK_RECIPE_CHUNK": "4096"}
    r = subprocess.run(["bash", str(ROOT / "ops" / "langpack" / "ship_packs.sh"), "testland", str(ship.base)],
                       capture_output=True, text=True, timeout=900, env=env)
    assert r.returncode == 0, r.stdout[-3000:]
    rec = tmp_path / "recipes"
    shutil.copytree(ship.work / "site" / "testland" / "recipes" / "testland", rec / "testland")
    assert subprocess.run([sys.executable, str(ROOT / "tools" / "langpack" / "site_index.py"), str(rec)],
                          capture_output=True).returncode == 0
    recipe = json.loads((rec / "testland" / "de.json").read_text())
    pk = recipe["packs"]["de"]
    first = min(s["off"] // pk["chunk"] for s in recipe["segments"] if s["src"] == "pack" and s["pack"] == "de")
    p = SimpleNamespace(ship=ship, rec=rec, packs=ship.work / "packs" / ship.base.name[:-4], out=tmp_path / "out")
    rc, rows = _e2e(p, "de", "net", "--corrupt", f"streetzim-testland-lang/{pk['file']}@{(first + 2) * pk['chunk'] + 100}",
                    "--corrupt-times", "1", "--throttle", "2000:1000:20")      # long enough to look at the focus during it
    assert rc == 0 and rows[0]["pass"], rows
    ui = rows[0]["ui"]
    assert ui["activeDuring"] == "oneclick" and ui["activeEnd"] == "oneclick", ui
    assert all(x in PHASES for x in ui["phases"]) and _monotonic([PHASES.index(x) for x in ui["phases"]]), ui["phases"]
    assert ui["bar"] and _monotonic(ui["bar"]), ui["bar"]
    lp = ui["lastProgress"]
    assert lp["packs"]["got"] <= lp["packs"]["size"] and lp["base"]["got"] <= lp["base"]["size"], lp


def test_from_my_copy_says_less_and_more(page: SimpleNamespace) -> None:
    """Review LOWs, "build from my copy": the live region #baseCheck says only
    the step and the verdict (the recipe's byte count went to it, 1768
    updates in 1.56 s); a retry of a damaged pack check is said; the busy
    button says "Building…", not "Downloading…"."""
    pack = next(p.name for p in page.packs.glob("osm_testland_wiki-de_*.zim"))
    rc, rows = _e2e(page, "de", "file", "--corrupt", f"streetzim-testland-lang/{pack}@2000", "--corrupt-times", "1")
    assert rc == 0 and rows[0]["pass"], rows
    ui = rows[0]["ui"]
    assert len(ui["baseCheckTexts"]) <= 3 and not any("build plan" in t for t in ui["baseCheckTexts"]), ui["baseCheckTexts"]
    assert any("arrived damaged" in t for t in ui["packStatusTexts"]), ui["packStatusTexts"]
    assert any(t.startswith("Building") for t in ui["buttonTexts"]), ui["buttonTexts"]
    assert not any(t.startswith("Downloading") for t in ui["buttonTexts"]), ui["buttonTexts"]


def test_an_error_in_the_page_ends_the_job(page: SimpleNamespace, tmp_path: Path) -> None:
    """Review LOW: whatever throws after the click, the job ends (status
    said, button itself again); a recipe without its packs list made
    ensurePacks throw and left the page "Preparing…" for good."""
    rec = tmp_path / "recipes-nopacks"
    shutil.copytree(page.rec, rec)
    rp = rec / "testland" / "de.json"
    r = json.loads(rp.read_text())
    del r["packs"]
    rp.write_text(json.dumps(r))
    p = SimpleNamespace(ship=page.ship, rec=rec, packs=page.packs, out=tmp_path / "out")
    rc, rows = _e2e(p, "de", "net", "--expect", "fail", "--status-timeout-secs", "90")
    assert rows and rows[0]["pass"] and rows[0]["status"].startswith(("Stopped", "Not started")), rows
    b = rows[0]["ui"]["oneclick"]
    assert "busy" not in b["cls"] and not b["disabled"] and b["text"].startswith("Download"), b


def test_a_dead_browser_fails_fast(page: SimpleNamespace, tmp_path: Path) -> None:
    """A browser that cannot start: the driver says so for every language
    and exits (its two local servers kept it waiting until the caller's
    1800 s timeout)."""
    dead = tmp_path / "chrome"
    dead.write_text("#!/bin/sh\necho 'no browser here' >&2\nexit 1\n")
    dead.chmod(0o755)
    import time
    t0 = time.time()
    rc, rows = _e2e(page, "de,th", "net", env={"CHROME_PATH": str(dead)}, timeout=300)
    assert rc == 1 and [x["lang"] for x in rows] == ["de", "th"], rows
    assert all(not x["pass"] and x["error"].startswith("browser: ") for x in rows), rows
    assert time.time() - t0 < 200, time.time() - t0


def test_a_browser_that_never_closes_does_not_hang(page: SimpleNamespace, tmp_path: Path) -> None:
    """A browser whose close never answers (one that died under the driver):
    the driver reports and exits within its 15 s grace instead of waiting
    for the caller's timeout."""
    hang = tmp_path / "hang.mjs"
    hang.write_text(
        "const pw = await import(process.env.PLAYWRIGHT_CORE);\n"
        "const launch = pw.chromium.launch.bind(pw.chromium);\n"
        "pw.chromium.launch = async (...a) => { const b = await launch(...a); b.close = () => new Promise(() => {}); return b; };\n")
    import time
    t0 = time.time()
    rc, rows = _e2e(page, "de", "net", env={"NODE_OPTIONS": f"--import={hang.as_uri()}"}, timeout=240)
    assert rc == 0 and rows[0]["pass"], rows
    assert time.time() - t0 < 180, time.time() - t0


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
    # a recipe that reads a viewer slot from the base (whose bytes the chunk hashes do not
    # cover) is refused by the worker before anything is saved (re-review LOW)
    rp = rec / "testland" / "de.json"
    r = json.loads(rp.read_text())
    slot = r["base"]["slots"][0]
    seg = next(x for x in r["segments"] if x["src"] == "base")
    seg["off"] = slot["off"]
    rp.write_text(json.dumps(r))
    rc, rows = _e2e(p, "de", "net", "--expect", "fail")
    assert rc == 0 and rows[0]["pass"] and "masked viewer slot" in rows[0]["status"], rows


def test_packs_stream_and_are_never_held_whole(ship: SimpleNamespace, tmp_path: Path) -> None:  # noqa: F811
    """Plan review M-3: the page checks only the first chunk the build needs
    from each pack before Build and hands it to the worker; the worker
    streams the rest during the build, chunk by verified chunk (a small chunk
    size here, so a pack spans many), and nothing is downloaded twice."""
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
    files = rows[0]["proxy"]["files"]
    for k, pk in recipe["packs"].items():
        extra = 4 if pk["file"] == probe else 0                   # the speed probe's 4 ranges
        touched = {c for s in recipe["segments"] if s["src"] == "pack" and s["pack"] == k
                   for c in range(s["off"] // pk["chunk"], (s["off"] + s["len"] - 1) // pk["chunk"] + 1)}
        # before Build: one chunk; in all: every chunk the recipe reads, once (one range each)
        assert before.get(pk["file"], 0) == 1 + extra, (k, before, probe)
        assert files.get(pk["file"], 0) == len(touched) + extra, (k, files.get(pk["file"]), len(touched), extra)


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
    ws = "https://streetzim-build-proxy.example.workers.dev"
    # each entry's webseed, previewed in /drive/ (which accepts exactly that origin and path): th's on
    # another origin gets none, en's (published before entries carried "webseed") comes from en.ws.json
    idx = json.loads((rec / "index.json").read_text())
    menu = idx["regions"][0]["menu"]
    hooks = {m["lang"]: m["webseed"] for m in menu}
    assert all(h.startswith(ws + "/merged/testland/") for h in hooks.values()), hooks
    menu[2]["webseed"] = hooks["th"].replace(ws, "https://elsewhere.example")
    del menu[3]["webseed"]
    (rec / "index.json").write_text(json.dumps(idx))
    rc, rows = _e2e(p, "de", "net", "--merged-origin", ws, "--wait-previews", "3")
    assert rc == 0 and rows[0]["pass"], rows
    shown = json.loads((p.out / "e2e-de.json").read_text())["shown"]
    assert shown["torrents"] == [f"recipes/testland/{x}.torrent" for x in ("de", "fr", "th", "en")], shown
    assert shown["previews"] == ["/drive/?zim=" + urllib.parse.quote(hooks[x], safe="-_.!~*'()") for x in ("de", "fr", "en")], shown
    info = bdecode((rec / "testland" / "de.torrent").read_bytes())[b"info"]
    data = (p.out / "testland-de.zim").read_bytes()
    plen = info[b"piece length"]
    assert info[b"length"] == len(data)
    assert info[b"pieces"] == b"".join(hashlib.sha1(data[i:i + plen]).digest() for i in range(0, len(data), plen))


FIREFOX_ANDROID = "Mozilla/5.0 (Android 14; Mobile; rv:131.0) Gecko/131.0 Firefox/131.0"


def test_a_phone_downloads_the_file_from_the_build_worker(ship: SimpleNamespace, tmp_path: Path) -> None:  # noqa: F811
    """On a phone the page builds nothing: Download, and every link of the
    download list, is the browser's own download of the entry's webseed URL
    (it goes on when the phone pauses the page, which stops a build there).
    Through the build worker's own code, the file must be the gated merge."""
    ws = "https://streetzim-build-proxy.example.workers.dev"
    r = _run(ship, LANGPACK_WEBSEED=ws)
    assert r.returncode == 0, r.stdout[-2000:]
    rec = tmp_path / "recipes"
    shutil.copytree(ship.work / "site" / "testland" / "recipes" / "testland", rec / "testland")
    assert subprocess.run([sys.executable, str(ROOT / "tools" / "langpack" / "site_index.py"), str(rec)],
                          capture_output=True).returncode == 0
    p = SimpleNamespace(ship=ship, rec=rec, packs=ship.work / "packs" / ship.base.name[:-4], out=tmp_path / "out")
    rc, rows = _e2e(p, "de", "direct", "--merged-local", ws, "--user-agent", FIREFOX_ANDROID)
    assert rc == 0 and rows[0]["pass"], rows
    res = json.loads((p.out / "e2e-de.json").read_text())
    assert res["status"].startswith("Your browser is downloading"), res["status"]
    assert all("/merged/testland/" in h for h in res["shown"]["links"]), res["shown"]["links"]
    # the same page on a desktop builds in the page, as before
    rc, rows = _e2e(p, "fr", "net", "--merged-local", ws)
    assert rc == 0 and rows[0]["pass"] and rows[0]["ui"]["workerPhases"], rows
