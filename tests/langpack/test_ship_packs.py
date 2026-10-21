"""ops/langpack/ship_packs.sh end to end on the small ZIMs, with stand-ins for
the network (prefetch, archive.org's `ia`), the pack builder and the gates:
the right packs are built, uploaded once to streetzim-<region>-lang with the
item's metadata, a listing that lags means exit 6 and a rerun that sends
nothing again, a name archive.org holds with another MD5 is never
overwritten, and the site files pass site_check and publish_site.sh."""
from __future__ import annotations

import fcntl
import hashlib
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

SHIP = ROOT / "ops" / "langpack" / "ship_packs.sh"
PUBLISH = ROOT / "ops" / "langpack" / "publish_site.sh"
for _cmd in ("bash", "flock"):
    if not shutil.which(_cmd):
        pytest.skip(f"ship_packs.sh needs {_cmd}", allow_module_level=True)

# The pack builder: the prebuilt pack of --lang, and a stats file builder_ok accepts.
FAKE_BUILDER = r'''
import argparse, json, os, shutil, sys
ap = argparse.ArgumentParser(); ap.add_argument("base"); ap.add_argument("--lang"); ap.add_argument("-o"); ap.add_argument("--stats")
a, _ = ap.parse_known_args()
with open(os.environ["FAKE_BUILD_LOG"], "a") as fh: fh.write(a.lang + "\n")
src = os.path.join(os.environ["FAKE_PACKS"], f"osm_testland_wiki-{a.lang}_2026-10-13.zim")
shutil.copyfile(src, a.o)
open(a.stats, "w").write(json.dumps({"zim_bytes": os.path.getsize(a.o), "check": {"ok": True, "problems": []}}))
'''

# run_gates.sh: the real menu (recipes), then an all-PASS verdict per entry for exactly that recipe.
FAKE_GATES = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
base, packs, langs, rl, region, rj, port = sys.argv[1:8]
root = os.environ["LP_ROOT"]; sys.path.insert(0, root)
from tools.langpack.gatelib import md5_file, verdict_path, work_dir
tag = os.path.basename(base)[:-4]
out = os.path.join(work_dir(), "gates", tag); os.makedirs(out, exist_ok=True)
with open(os.environ["FAKE_GATES_LOG"], "a") as fh: fh.write(json.dumps({"langs": langs, "rl": rl, "rj": rj, "titles": os.environ.get("TITLES"), "resume": os.environ.get("RESUME")}) + "\n")
menu = subprocess.run([sys.executable, os.path.join(root, "tools/langpack/recipe2.py"), "menu", base, "--packs-dir", packs,
                       "--langs", langs, "--region-langs", rl, "--region", region, "--titles", os.environ.get("TITLES", "{}"),
                       "--language-meta", "map", "--out-dir", os.path.join(work_dir(), "recipes", tag)],
                      capture_output=True, text=True, check=True).stdout
fail = set(filter(None, os.environ.get("FAKE_GATES_FAIL", "").split(",")))
for line in menu.splitlines():
    e = json.loads(line); L = e["L"]
    r = json.load(open(os.path.join(work_dir(), "recipes", tag, f"{L}.json")))
    v = {"lang": L, "recipe_md5": r["md5"], "recipe_uuid": r["uuid"], "recipe_size": r["size"],
         "recipe_file_md5": r.get("file_md5"), "packs": {os.path.basename(f): md5_file(f) for f in e["pack_files"]},
         "checks": {}, "all_pass": L not in fail}
    p = verdict_path(base, L); os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(v, open(p, "w"))
open(os.path.join(out, "summary.txt"), "w").write("fake gates\n")
sys.exit(int(os.environ.get("FAKE_GATES_RC", "0")))
'''

# archive.org's CLI: `metadata ITEM` prints the listing kept in $LANGPACK_WORK/ia-cache/ia-ITEM.json
# (where site_check.py reads it without --ia-fetch); `upload` records the call and lists the files
# at once, or (FAKE_IA_PENDING=1) only into ia-ITEM.pending.json.
FAKE_IA = r'''#!/usr/bin/env python3
import hashlib, json, os, sys
d = os.path.join(os.environ["LANGPACK_WORK"], "ia-cache"); os.makedirs(d, exist_ok=True)
cmd, item = sys.argv[1], sys.argv[2]
path = os.path.join(d, f"ia-{item}.json")
meta = json.load(open(path)) if os.path.exists(path) else {}
if cmd == "metadata":
    print(json.dumps(meta)); sys.exit(0)
if cmd == "upload":
    files = [a for a in sys.argv[3:] if not a.startswith("--") and not a.isdigit()]
    with open(os.environ["FAKE_IA_LOG"], "a") as fh: fh.write(json.dumps({"item": item, "args": sys.argv[3:]}) + "\n")
    rows = [{"name": f, "size": str(os.path.getsize(f)), "md5": hashlib.md5(open(f, "rb").read()).hexdigest()} for f in files]
    if os.environ.get("FAKE_IA_PENDING") == "1":
        pend = path[:-5] + ".pending.json"
        old = json.load(open(pend)) if os.path.exists(pend) else []
        json.dump(old + rows, open(pend, "w")); sys.exit(0)
    meta.setdefault("files", [])
    meta["files"] = [x for x in meta["files"] if x["name"] not in files] + rows
    json.dump(meta, open(path, "w")); sys.exit(0)
sys.exit(2)
'''

PREFETCH_OK = "import sys\nopen(__import__('os').environ['FAKE_PREFETCH_LOG'], 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


@pytest.fixture
def ship(lp: SimpleNamespace, tmp_path: Path) -> SimpleNamespace:
    d = tmp_path
    for name, body, mode in (("builder.py", FAKE_BUILDER, 0o644), ("gates.py", FAKE_GATES, 0o755),
                             ("ia", FAKE_IA, 0o755), ("prefetch.py", PREFETCH_OK, 0o644)):
        p = d / name
        p.write_text(body.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
        p.chmod(mode)
    base = d / "bases" / lp.base.name
    base.parent.mkdir()
    shutil.copy(lp.base, base)
    fake_packs = d / "fakepacks"
    fake_packs.mkdir()
    for p in lp.packs.values():
        shutil.copy(p, fake_packs / p.name)
    wiki = d / "wiki-src"       # de, fr, th and en have a Wikipedia ZIM; nothing else does
    wiki.mkdir()
    for lg in ("de", "fr", "th", "en"):
        (wiki / f"wikipedia_{lg}_all_maxi_2026-01.zim").write_bytes(b"")
    work = d / "work"
    (work / "ia-cache").mkdir(parents=True)
    # the base as published: streetzim-testland lists it with its MD5
    (work / "ia-cache" / "ia-streetzim-testland.json").write_text(json.dumps(
        {"files": [{"name": base.name, "size": str(base.stat().st_size), "md5": _md5(base)}]}))
    zc = lpzims.fake_zimcheck(d / "zc", "clean", None)
    logs = {k: d / f"{k}.log" for k in ("build", "gates", "ia", "prefetch")}
    env = {**os.environ, "LANGPACK_WORK": str(work), "WP_CACHE": str(d / "wpcache"), "WIKI_SRC": str(wiki),
           "PYTHON": sys.executable, "PACK_GATE_ZIMCHECK": zc, "IA": str(d / "ia"),
           "BUILDER": str(d / "builder.py"), "SHIP_PREFETCH": str(d / "prefetch.py"),
           "SHIP_RUN_GATES": str(d / "gates.py"), "SHIP_SKIP_ENV_CHECK": "1", "SHIP_IA_FETCH": "",
           "SHIP_PROBE_URLS": "", "SHIP_LISTING_POLLS": "2", "SHIP_LISTING_POLL_SECS": "0",
           "CORE": "de,fr,th,ja,en", "PACK_DATE": "2026-10-21", "FAKE_PACKS": str(fake_packs),
           "FAKE_BUILD_LOG": str(logs["build"]), "FAKE_GATES_LOG": str(logs["gates"]),
           "FAKE_IA_LOG": str(logs["ia"]), "FAKE_PREFETCH_LOG": str(logs["prefetch"]),
           "UPLOAD_LOCK": str(d / "upload.lock"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    return SimpleNamespace(d=d, base=base, work=work, env=env, logs=logs, tag=base.name[:-4])


def _run(ship: SimpleNamespace, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SHIP), "testland", str(ship.base)], capture_output=True, text=True,
                          timeout=900, env={**ship.env, **extra})


def _lines(p: Path) -> list[str]:
    return p.read_text().splitlines() if p.exists() else []


def _log(ship: SimpleNamespace) -> str:
    return (ship.work / "logs" / f"ship-testland-{ship.tag}.log").read_text()


def _pk(ship: SimpleNamespace, lang: str) -> str:
    """A pack's file name: the date and the base's UUID (review r3 M-2)."""
    return f"osm_testland_wiki-{lang}_2026-10-21_{lpzims.read_uuid(ship.base)[:8]}.zim"


def test_ships_the_core_languages(ship: SimpleNamespace, tmp_path: Path) -> None:
    r = _run(ship)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    log = _log(ship)
    # ja has no Wikipedia ZIM: skipped and said; the rest are offered
    assert "skipped ja: no Wikipedia ZIM in WIKI_SRC" in log
    assert "menu: de,fr,th,en;" in log
    # the network step, then one build per pack language (the base's own Wikipedia is English)
    assert _lines(ship.logs["prefetch"])[0].endswith("--langs de,fr,th --cache " + ship.env["WP_CACHE"])
    assert sorted(_lines(ship.logs["build"])) == ["de", "fr", "th"]
    g = json.loads(_lines(ship.logs["gates"])[0])
    assert g["langs"] == "de,fr,th,en" and g["resume"] == "1"
    assert g["rj"].endswith(f"regions/{ship.tag}.json")         # no hand-written region file: region_json.py's
    # uploaded once, all packs the menu uses, with the new item's metadata
    ups = [json.loads(x) for x in _lines(ship.logs["ia"])]
    assert len(ups) == 1 and ups[0]["item"] == "streetzim-testland-lang"
    sent = sorted(a for a in ups[0]["args"] if a.endswith(".zim"))
    assert sent == [_pk(ship, x) for x in ("de", "fr", "th")]
    for m in ("--metadata=mediatype:data", "--metadata=collection:opensource", "--metadata=language:deu",
              "--metadata=language:fra", "--metadata=language:tha"):
        assert m in ups[0]["args"], ups[0]["args"]
    # the site files: proxy-relative URLs, the base on its own item, recipes under recipes/testland/
    out = ship.work / "site" / "testland" / "recipes" / "testland"
    idx = json.loads((out / "index.json").read_text())
    reg = idx["regions"][0]
    assert [m["lang"] for m in reg["menu"]] == ["de", "fr", "th", "en"]
    assert reg["base"]["url"] == f"{{proxy}}/streetzim-testland/{ship.base.name}"
    assert reg["base"]["download"] == f"https://archive.org/download/streetzim-testland/{ship.base.name}"
    assert all(m["recipe"] == f"recipes/testland/{m['lang']}.json" for m in reg["menu"])
    rde = json.loads((out / "de.json").read_text())
    assert rde["packs"]["de"]["url"] == "{proxy}/streetzim-testland-lang/" + _pk(ship, "de")
    assert "site_check.py FAILED" not in log and "SUMMARY" in log and "-> OK" in log
    # publish: into a web folder's build/recipes/, with the index the page reads
    web = tmp_path / "web"
    (web / "build").mkdir(parents=True)
    p = subprocess.run(["bash", str(PUBLISH), "testland", str(web)], capture_output=True, text=True, timeout=120,
                       env=ship.env)
    assert p.returncode == 0, p.stderr
    site_idx = json.loads((web / "build" / "recipes" / "index.json").read_text())
    assert [x["id"] for x in site_idx["regions"]] == ["testland"]
    assert (web / "build" / "recipes" / "testland" / "de.json").read_bytes() == (out / "de.json").read_bytes()
    # a second run redoes nothing: packs gated, packs listed
    r2 = _run(ship)
    assert r2.returncode == 0, r2.stderr
    assert sorted(_lines(ship.logs["build"])) == ["de", "fr", "th"]
    assert len(_lines(ship.logs["ia"])) == 1
    assert "packs: all gated for this base already" in _log(ship)


def test_listing_pending_then_rerun(ship: SimpleNamespace) -> None:
    r = _run(ship, FAKE_IA_PENDING="1")
    assert r.returncode == 6, r.stdout[-2000:] + r.stderr[-2000:]
    assert "listing pending" in _log(ship)
    assert not (ship.work / "site" / "testland" / "recipes" / "testland" / "index.json").exists()
    # archive.org lists them now
    cache = ship.work / "ia-cache"
    pend = json.loads((cache / "ia-streetzim-testland-lang.pending.json").read_text())
    (cache / "ia-streetzim-testland-lang.json").write_text(json.dumps({"files": pend}))
    r2 = _run(ship)
    assert r2.returncode == 0, r2.stdout[-2000:] + r2.stderr[-2000:]
    assert len(_lines(ship.logs["ia"])) == 1            # nothing sent twice


def test_never_overwrites_a_published_name(ship: SimpleNamespace) -> None:
    cache = ship.work / "ia-cache"
    (cache / "ia-streetzim-testland-lang.json").write_text(json.dumps({"files": [
        {"name": _pk(ship, "fr"), "size": "1", "md5": "0" * 32}]}))
    r = _run(ship)
    assert r.returncode == 1
    assert f"already holds {_pk(ship, 'fr')} with another MD5" in _log(ship)
    assert _lines(ship.logs["ia"]) == []


def test_only_passing_entries_are_published(ship: SimpleNamespace) -> None:
    """fr's gates fail: fr is not on the menu, and its pack is uploaded only
    because another entry (none here) uses it."""
    r = _run(ship, FAKE_GATES_FAIL="fr")
    assert r.returncode == 0, r.stderr[-2000:]
    idx = json.loads((ship.work / "site" / "testland" / "recipes" / "testland" / "index.json").read_text())
    assert [m["lang"] for m in idx["regions"][0]["menu"]] == ["de", "th", "en"]
    sent = [a for a in json.loads(_lines(ship.logs["ia"])[0])["args"] if a.endswith(".zim")]
    assert _pk(ship, "fr") not in sent


def test_one_run_per_region(ship: SimpleNamespace) -> None:
    lock = ship.work / "locks" / "ship-testland.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = _run(ship)
    assert r.returncode == 2 and "another run is shipping testland" in r.stderr
    assert _lines(ship.logs["build"]) == [] and _lines(ship.logs["prefetch"]) == []


def test_dry_run_stops_before_archive_org(ship: SimpleNamespace) -> None:
    r = _run(ship, SHIP_SKIP_UPLOAD="1")
    assert r.returncode == 3 and "SHIP_SKIP_UPLOAD=1" in _log(ship)       # not 0: nothing is ready (r3 L-6)
    assert _lines(ship.logs["ia"]) == []


def test_failing_gates_stop_the_run(ship: SimpleNamespace) -> None:
    """run_gates.sh's exit code counts (review r3 M-5)."""
    r = _run(ship, FAKE_GATES_RC="1")
    assert r.returncode == 1 and "run_gates.sh rc=1" in _log(ship)
    assert _lines(ship.logs["ia"]) == []


def test_a_held_pack_build_lock_stops_the_run(ship: SimpleNamespace) -> None:
    """build_packs.sh's exit 2 (a language's build lock is held) stops the run
    with exit 2 (review r3 L-6)."""
    import fcntl
    lock = ship.work / "locks" / "build-testland-de.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = _run(ship)
    assert r.returncode == 2 and "another run is building some of these packs" in _log(ship)
    assert _lines(ship.logs["ia"]) == []


def test_titles_come_after_the_network_step(ship: SimpleNamespace, tmp_path: Path) -> None:
    """The merged files' titles are the region's name in each language, which
    the Wikidata cache holds only once prefetch has run."""
    base = tmp_path / "bases2" / ship.base.name
    base.parent.mkdir()
    lpzims.write_base(base, extra={"category-index/admin.json": [{"n": "Testland", "s": "country", "q": "Q77"}]})
    # packs for THIS base: the stand-in builder writes them
    builder = tmp_path / "builder2.py"
    builder.write_text(f"""import argparse, json, os, sys
sys.path.insert(0, {str(Path(lpzims.__file__).parent)!r})
import lpzims
ap = argparse.ArgumentParser(); ap.add_argument("base"); ap.add_argument("--lang"); ap.add_argument("-o"); ap.add_argument("--stats")
a, _ = ap.parse_known_args()
lpzims.write_pack(__import__("pathlib").Path(a.o), a.lang, lpzims.read_uuid(a.base))
open(a.stats, "w").write(json.dumps({{"zim_bytes": os.path.getsize(a.o), "check": {{"ok": True, "problems": []}}}}))
""")
    prefetch = tmp_path / "prefetch2.py"      # what the real one leaves in the cache
    prefetch.write_text("""import os, json
d = os.path.join(os.environ["WP_CACHE"], "wikidata-text"); os.makedirs(d, exist_ok=True)
open(os.path.join(d, "de.jsonl"), "w").write(json.dumps(["Q77", "Testlandien", ""]) + "\\n")
""")
    (ship.work / "ia-cache" / "ia-streetzim-testland.json").write_text(json.dumps(
        {"files": [{"name": base.name, "size": str(base.stat().st_size), "md5": _md5(base)}]}))
    r = subprocess.run(["bash", str(SHIP), "testland", str(base)], capture_output=True, text=True, timeout=900,
                       env={**ship.env, "BUILDER": str(builder), "SHIP_PREFETCH": str(prefetch), "CORE": "de,en"})
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    titles = json.loads(json.loads(_lines(ship.logs["gates"])[-1])["titles"])
    assert titles == {"de": "Testlandien", "en": "OSM - Testland"}
    de = json.loads((ship.work / "site" / "testland" / "recipes" / "testland" / "de.json").read_text())
    assert de["title"] == "Testlandien"


def test_bad_region_id(ship: SimpleNamespace) -> None:
    r = subprocess.run(["bash", str(SHIP), "Test Land", str(ship.base)], capture_output=True, text=True,
                       timeout=60, env=ship.env)
    assert r.returncode == 1 and "a-z 0-9 - only" in r.stderr


def _publish(ship: SimpleNamespace, web: Path) -> subprocess.CompletedProcess[str]:
    (web / "build").mkdir(parents=True, exist_ok=True)
    return subprocess.run(["bash", str(PUBLISH), "testland", str(web)], capture_output=True, text=True, timeout=120,
                          env=ship.env)


def test_a_failed_site_check_replaces_and_publishes_nothing(ship: SimpleNamespace, tmp_path: Path) -> None:
    """Review r3 H-2: site files are checked where they are built and replace
    the region's previous ones only when site_check passed; publish_site.sh
    takes only sealed files."""
    assert _run(ship).returncode == 0
    site = ship.work / "site" / "testland" / "recipes" / "testland"
    good = (site / "de.json").read_bytes()
    assert json.loads((site / "checked.json").read_text())["site_check"].endswith("-> OK")
    # archive.org now lists another file under the base's name: the check fails
    (ship.work / "ia-cache" / "ia-streetzim-testland.json").write_text(json.dumps(
        {"files": [{"name": ship.base.name, "size": str(ship.base.stat().st_size), "md5": "0" * 32}]}))
    r = _run(ship, TITLES_UNUSED="1")
    assert r.returncode == 1 and "site_check.py FAILED" in _log(ship), r.stdout[-2000:]
    assert (site / "de.json").read_bytes() == good                 # the previous good files stay
    # the first run's files publish; a tampered copy does not
    web = tmp_path / "web"
    assert _publish(ship, web).returncode == 0
    (site / "de.json").write_text("{}")
    p = _publish(ship, tmp_path / "web2")
    assert p.returncode == 1 and "not what ship_packs.sh checked" in p.stderr
    assert not (tmp_path / "web2" / "build" / "recipes" / "testland").exists()


def test_publish_needs_a_seal(ship: SimpleNamespace, tmp_path: Path) -> None:
    src = ship.work / "site" / "testland" / "recipes" / "testland"
    src.mkdir(parents=True)
    (src / "index.json").write_text("{}")
    (src / "region.json").write_text("{}")
    p = _publish(ship, tmp_path / "web")
    assert p.returncode == 1 and "not what ship_packs.sh checked" in p.stderr


def test_publish_replaces_in_one_step_and_rolls_back(ship: SimpleNamespace, tmp_path: Path) -> None:
    assert _run(ship).returncode == 0
    web = tmp_path / "web"
    assert _publish(ship, web).returncode == 0
    dst = web / "build" / "recipes" / "testland"
    before = (dst / "de.json").read_bytes()
    # a broken index step (another region's folder with an unreadable region.json is skipped, so break
    # site_index itself): the previous folder comes back
    bad = tmp_path / "py-fail"
    bad.write_text(f"#!/bin/bash\ncase \"$1\" in *site_index.py) exit 1;; esac\nexec {sys.executable} \"$@\"\n")
    bad.chmod(0o755)
    src = ship.work / "site" / "testland" / "recipes" / "testland"
    (src / "de.json").write_text(json.dumps({"changed": True}))        # a newer, sealed version
    assert subprocess.run([sys.executable, str(ROOT / "tools" / "langpack" / "checked.py"), "write", str(src),
                           "--summary", "x"]).returncode == 0
    p = subprocess.run(["bash", str(PUBLISH), "testland", str(web)], capture_output=True, text=True, timeout=120,
                       env={**ship.env, "PYTHON": str(bad)})
    assert p.returncode == 1 and "put back" in p.stderr
    assert (dst / "de.json").read_bytes() == before and not (web / "build" / "recipes" / "testland.new").exists()
    # and with a working index step it goes live
    assert _publish(ship, web).returncode == 0 and json.loads((dst / "de.json").read_text()) == {"changed": True}


def test_no_second_send_while_the_listing_is_pending(ship: SimpleNamespace) -> None:
    """Review r3 M-1: a rerun while archive.org has not listed the packs yet
    waits for them; it never sends them again (ia upload --checksum does not
    skip a file while the item has a pending task)."""
    assert _run(ship, FAKE_IA_PENDING="1").returncode == 6
    r2 = _run(ship, FAKE_IA_PENDING="1")
    assert r2.returncode == 6 and "not sent again" in _log(ship)
    assert len(_lines(ship.logs["ia"])) == 1
    # still unlisted long after: the run stops and says what to do, and sends nothing
    sent = ship.work / "uploads" / "streetzim-testland-lang.tsv"
    rows = [x.split("\t") for x in sent.read_text().splitlines()]
    sent.write_text("".join(f"{f}\t{m}\t0\n" for f, m, _t in rows))
    r3 = _run(ship, FAKE_IA_PENDING="1")
    assert r3.returncode == 1 and "still not listed: check the item's tasks" in _log(ship)
    assert len(_lines(ship.logs["ia"])) == 1


def test_uploads_hold_the_host_upload_lock(ship: SimpleNamespace) -> None:
    """Review r3 L-1: uploads wait for the host's upload lock."""
    import fcntl
    with open(ship.env["UPLOAD_LOCK"], "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        p = subprocess.Popen(["bash", str(SHIP), "testland", str(ship.base)], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=ship.env)
        import time
        t0 = time.time()
        while "upload to streetzim-testland-lang" not in (_log(ship) if (ship.work / "logs").exists()
                                                           and list((ship.work / "logs").glob("*.log")) else ""):
            assert time.time() - t0 < 300 and p.poll() is None
            time.sleep(0.2)
        time.sleep(2)
        assert _lines(ship.logs["ia"]) == []           # waiting for the lock
    assert p.wait(timeout=300) == 0
    assert len(_lines(ship.logs["ia"])) == 1


def test_a_second_base_the_same_day_ships_too(ship: SimpleNamespace, tmp_path: Path) -> None:
    """Review r3 M-2: packs for another base of the region (a rebuild shipped
    the same day) get their own names, so they neither clash with the first
    base's on archive.org nor stop the run."""
    assert _run(ship).returncode == 0
    base_b = tmp_path / "bases-b" / "osm-testland-2026-10-22.zim"
    base_b.parent.mkdir()
    lpzims.write_base(base_b, description="Offline map of Testland, rebuilt")
    assert lpzims.read_uuid(base_b) != lpzims.read_uuid(ship.base)
    builder = tmp_path / "builder-b.py"
    builder.write_text(f"""import argparse, json, os, sys
sys.path.insert(0, {str(Path(lpzims.__file__).parent)!r})
import lpzims
ap = argparse.ArgumentParser(); ap.add_argument("base"); ap.add_argument("--lang"); ap.add_argument("-o"); ap.add_argument("--stats")
a, _ = ap.parse_known_args()
lpzims.write_pack(__import__("pathlib").Path(a.o), a.lang, lpzims.read_uuid(a.base))
open(a.stats, "w").write(json.dumps({{"zim_bytes": os.path.getsize(a.o), "check": {{"ok": True, "problems": []}}}}))
""")
    listing = json.loads((ship.work / "ia-cache" / "ia-streetzim-testland.json").read_text())
    listing["files"].append({"name": base_b.name, "size": str(base_b.stat().st_size), "md5": _md5(base_b)})
    (ship.work / "ia-cache" / "ia-streetzim-testland.json").write_text(json.dumps(listing))
    rb = subprocess.run(["bash", str(SHIP), "testland", str(base_b)], capture_output=True, text=True, timeout=900,
                        env={**ship.env, "BUILDER": str(builder)})
    log_b = (ship.work / "logs" / f"ship-testland-{base_b.name[:-4]}.log").read_text()
    assert rb.returncode == 0, log_b[-3000:]
    lang_item = json.loads((ship.work / "ia-cache" / "ia-streetzim-testland-lang.json").read_text())
    names = {f["name"] for f in lang_item["files"]}
    u8 = lpzims.read_uuid(base_b)[:8]
    assert {f"osm_testland_wiki-{x}_2026-10-21_{u8}.zim" for x in ("de", "fr", "th")} <= names
    assert {_pk(ship, x) for x in ("de", "fr", "th")} <= names          # the first base's are still there
