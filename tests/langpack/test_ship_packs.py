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
import argparse, json, os, shutil, sys, time
ap = argparse.ArgumentParser(); ap.add_argument("base"); ap.add_argument("--lang"); ap.add_argument("-o"); ap.add_argument("--stats")
a, _ = ap.parse_known_args()
with open(os.environ["FAKE_BUILD_LOG"], "a") as fh: fh.write(a.lang + "\n")
if a.lang in os.environ.get("FAKE_SLOW", "").split(","):    # a build that takes longer than its time
    time.sleep(120)
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
acct = os.environ.get("NVME_SCRATCH") or ""
resv = {n: open(os.path.join(acct, n)).read().split() for n in (os.listdir(acct) if os.path.isdir(acct) else [])
        if n.startswith(".reserve-ml-")}
io = subprocess.run(["ionice", "-p", str(os.getpid())], capture_output=True, text=True).stdout.strip()
with open(os.environ["FAKE_GATES_LOG"], "a") as fh: fh.write(json.dumps({"langs": langs, "rl": rl, "rj": rj, "titles": os.environ.get("TITLES"), "resume": os.environ.get("RESUME"),
    "base": base, "base_md5": md5_file(base), "base_mtime": os.stat(base).st_mtime_ns, "jobs": os.environ.get("GATE_JOBS"),
    "merged": os.environ.get("LANGPACK_MERGED"), "tmp": os.environ.get("LANGPACK_TMP"), "resv": resv, "io": io,
    "search_packs": sorted(f for f in os.listdir(packs) if "_search-" in f)}) + "\n")
menu = subprocess.run([sys.executable, os.path.join(root, "tools/langpack/recipe2.py"), "menu", base, "--packs-dir", packs,
                       "--langs", langs, "--region-langs", rl, "--region", region, "--titles", os.environ.get("TITLES", "{}"),
                       "--language-meta", "map", "--out-dir", os.path.join(work_dir(), "recipes", tag)],
                      capture_output=True, text=True, check=True).stdout
fail = set(filter(None, os.environ.get("FAKE_GATES_FAIL", "").split(",")))
for line in menu.splitlines():
    e = json.loads(line); L = e["L"]
    r = json.load(open(os.path.join(work_dir(), "recipes", tag, f"{L}.json")))
    v = {"lang": L, "recipe_md5": r["md5"], "recipe_uuid": r["uuid"], "recipe_size": r["size"],
         "recipe_file_md5": r.get("file_md5"),
         "packs": {os.path.basename(f): md5_file(f) for f in e["pack_files"] + [e.get("index_file")] if f},
         "checks": {}, "all_pass": L not in fail}
    p = verdict_path(base, L); os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(v, open(p, "w"))
open(os.path.join(out, "summary.txt"), "w").write("fake gates\n")
__import__("time").sleep(float(os.environ.get("FAKE_GATES_SLEEP", "0")))
sys.exit(int(os.environ.get("FAKE_GATES_RC", "0")))
'''

# archive.org's CLI: `metadata ITEM` prints the listing kept in $LANGPACK_WORK/ia-cache/ia-ITEM.json
# (where site_check.py reads it without --ia-fetch); `upload` records the call and lists the files
# at once, or (FAKE_IA_PENDING=1) only into ia-ITEM.pending.json; FAKE_IA_FAIL_ON=NAME: the call
# takes the files before NAME and fails there, as an upload cut off part way.
FAKE_IA = r'''#!/usr/bin/env python3
import hashlib, json, os, sys
d = os.path.join(os.environ["LANGPACK_WORK"], "ia-cache"); os.makedirs(d, exist_ok=True)
cmd, item = sys.argv[1], sys.argv[2]
path = os.path.join(d, f"ia-{item}.json")
meta = json.load(open(path)) if os.path.exists(path) else {}
if cmd == "metadata":
    # FAKE_IA_META_FAIL=COUNTER:N,...: the Nth metadata call for a -lang item fails
    fail = os.environ.get("FAKE_IA_META_FAIL")
    if fail and item.endswith("-lang"):
        counter, calls = fail.split(":")
        n = (int(open(counter).read()) if os.path.exists(counter) else 0) + 1
        open(counter, "w").write(str(n))
        if str(n) in calls.split(","):
            print("ia: metadata: connection reset", file=sys.stderr); sys.exit(1)
    print(json.dumps(meta)); sys.exit(0)
if cmd == "upload":
    files = [a for a in sys.argv[3:] if not a.startswith("--") and not a.isdigit()]
    scr = os.environ.get("LANGPACK_SCRATCH") or ""
    runs = sorted(x for x in os.listdir(scr) if x.startswith("run-")) if os.path.isdir(scr) else None
    with open(os.environ["FAKE_IA_LOG"], "a") as fh: fh.write(json.dumps({"item": item, "args": sys.argv[3:], "scratch_runs": runs}) + "\n")
    bad = os.environ.get("FAKE_IA_FAIL_ON")
    cut = files.index(bad) if bad in files else None
    rows = [{"name": f, "size": str(os.path.getsize(f)), "md5": hashlib.md5(open(f, "rb").read()).hexdigest()}
            for f in files[:cut]]
    if os.environ.get("FAKE_IA_PENDING") == "1":
        pend = path[:-5] + ".pending.json"
        old = json.load(open(pend)) if os.path.exists(pend) else []
        json.dump(old + rows, open(pend, "w")); sys.exit(1 if cut is not None else 0)
    meta.setdefault("files", [])
    meta["files"] = [x for x in meta["files"] if x["name"] not in files] + rows
    json.dump(meta, open(path, "w")); sys.exit(1 if cut is not None else 0)
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
           "UPLOAD_LOCK": str(d / "upload.lock"), "LANGPACK_SCRATCH": "",
           "PYTHONDONTWRITEBYTECODE": "1"}
    return SimpleNamespace(d=d, base=base, work=work, env=env, logs=logs, tag=base.name[:-4])


def _run(ship: SimpleNamespace, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SHIP), "testland", str(ship.base)], capture_output=True, text=True,
                          timeout=900, env={**ship.env, **extra})


def _lines(p: Path) -> list[str]:
    return p.read_text().splitlines() if p.exists() else []


def _sent(ship: SimpleNamespace) -> list[str]:
    """Every pack file handed to `ia upload`, in order, over all calls."""
    return [a for x in _lines(ship.logs["ia"]) for a in json.loads(x)["args"] if a.endswith(".zim")]


def _log(ship: SimpleNamespace) -> str:
    return (ship.work / "logs" / f"ship-testland-{ship.tag}.log").read_text()


def _pk(ship: SimpleNamespace, lang: str) -> str:
    """A pack's file name: the date and the base's UUID (review r3 M-2)."""
    return f"osm_testland_wiki-{lang}_2026-10-21_{lpzims.read_uuid(ship.base)[:8]}.zim"


def _mark_langs(ship: SimpleNamespace, langs: str) -> Path:
    """packs/<base>/prefetched for this very file, with these pack languages."""
    mark = ship.work / "packs" / ship.tag / "prefetched"
    mark.parent.mkdir(parents=True, exist_ok=True)
    st = ship.base.stat()
    mark.write_text(f"{langs}\n{st.st_size} {st.st_mtime_ns // 10**9}.{st.st_mtime_ns % 10**9:09d}\n")
    return mark


def _menu(ship: SimpleNamespace) -> list[str]:
    reg = json.loads((ship.work / "site" / "testland" / "recipes" / "testland" / "region.json").read_text())
    return [m["lang"] for m in reg["menu"]]


def test_languages_added_since_the_prefetch_wait_for_a_later_run(ship: SimpleNamespace) -> None:
    """The menu grew since this file's prefetch (th added, as Iberia's Basque
    and Galician): the languages fetched go live now and th waits
    (retry-langs, prefetch.added) instead of holding the run for a Wikidata
    pass of hours; the prefetch ahead fetches it and clears the mark; the
    next run adds it."""
    mark = _mark_langs(ship, "de,fr")
    r = _run(ship)
    assert r.returncode == 0, r.stdout[-3000:]
    assert _lines(ship.logs["prefetch"]) == [] and sorted(_lines(ship.logs["build"])) == ["de", "fr"]
    assert (mark.parent / "prefetch.added").read_text().split() == ["th"]
    assert (mark.parent / "retry-langs").read_text().split() == ["th"]
    assert "th" not in _menu(ship) and {"de", "fr"} <= set(_menu(ship))
    assert "added since this file's prefetch: th" in _log(ship)
    r = _run(ship, SHIP_PREFETCH_ONLY="1")
    assert r.returncode == 0, r.stdout[-3000:]
    assert _lines(ship.logs["prefetch"])[-1].endswith("--langs de,fr,th --cache " + ship.env["WP_CACHE"])
    assert mark.read_text().splitlines()[0] == "de,fr,th" and not (mark.parent / "prefetch.added").exists()
    r = _run(ship)
    assert r.returncode == 0, r.stdout[-3000:]
    assert "th" in _lines(ship.logs["build"]) and "th" in _menu(ship)
    assert not (mark.parent / "retry-langs").exists() and len(_lines(ship.logs["prefetch"])) == 1


def test_a_region_language_added_since_the_prefetch_is_fetched_now(ship: SimpleNamespace) -> None:
    """Every entry carries a region language: one added since the prefetch
    is fetched in this run, not left for a later one."""
    lpzims.write_base(ship.base, cfg_extra={"regionLanguages": ["th"]})
    (ship.work / "ia-cache" / "ia-streetzim-testland.json").write_text(json.dumps(
        {"files": [{"name": ship.base.name, "size": str(ship.base.stat().st_size), "md5": _md5(ship.base)}]}))
    mark = _mark_langs(ship, "de,fr")
    _run(ship)                          # (this base's entries, all carrying th, are over the budget)
    assert _lines(ship.logs["prefetch"])[0].endswith("--langs de,fr,th --cache " + ship.env["WP_CACHE"])
    assert not (mark.parent / "prefetch.added").exists() and "added since" not in _log(ship)
    assert "th" in _lines(ship.logs["build"])


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
    # uploaded once, one pack per call (review M-c), all packs the menu uses, with the new item's metadata
    ups = [json.loads(x) for x in _lines(ship.logs["ia"])]
    assert all(x["item"] == "streetzim-testland-lang" and sum(a.endswith(".zim") for a in x["args"]) == 1 for x in ups)
    sent = sorted(_sent(ship))
    # the packs and each entry's search pack (index_pack.py)
    u8 = lpzims.read_uuid(ship.base)[:8]
    assert sent == sorted([_pk(ship, x) for x in ("de", "fr", "th")]
                          + [f"osm_testland_search-{x}_2026-10-21_{u8}.zim" for x in ("de", "fr", "th")])
    rde_ = json.loads((ship.work / "site" / "testland" / "recipes" / "testland" / "de.json").read_text())
    assert rde_["packs"]["search"]["kind"] == "search"
    assert rde_["packs"]["search"]["url"] == f"{{proxy}}/streetzim-testland-lang/osm_testland_search-de_2026-10-21_{u8}.zim"
    assert "--metadata=collection:opensource" not in ups[0]["args"]        # the bases' collection instead
    for m in ("--metadata=mediatype:data", "--metadata=collection:opensource_media",
              "--metadata=licenseurl:https://creativecommons.org/licenses/by-sa/4.0/", "--metadata=language:deu",
              "--metadata=language:fra", "--metadata=language:tha"):
        assert all(m in x["args"] for x in ups), (m, ups)
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
    assert len(_lines(ship.logs["ia"])) == len(ups)
    assert "packs: all gated for this base already" in _log(ship)


def test_a_prefetch_ahead_then_the_run_skips_it(ship: SimpleNamespace) -> None:
    """SHIP_PREFETCH_ONLY=1 (the queue's prefetch ahead): the languages and
    the network step only, then exit 0 with the base's packs/<base>/prefetched
    naming the pack languages; the run after it skips the network step for
    them, and a run for other pack languages does it again."""
    r = _run(ship, SHIP_PREFETCH_ONLY="1")
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    mark = ship.work / "packs" / ship.tag / "prefetched"
    st = ship.base.stat()
    assert mark.read_text() == f"de,fr,th\n{st.st_size} {st.st_mtime_ns // 10**9}.{st.st_mtime_ns % 10**9:09d}\n"
    assert len(_lines(ship.logs["prefetch"])) == 1
    assert _lines(ship.logs["build"]) == [] and _lines(ship.logs["gates"]) == [] and _lines(ship.logs["ia"]) == []
    assert "prefetch only (SHIP_PREFETCH_ONLY=1): done" in _log(ship)
    r2 = _run(ship)
    assert r2.returncode == 0, r2.stdout[-2000:] + r2.stderr[-2000:]
    assert len(_lines(ship.logs["prefetch"])) == 1 and sorted(_lines(ship.logs["build"])) == ["de", "fr", "th"]
    assert "prefetch: done already for de,fr,th" in _log(ship)
    assert (ship.work / "packs" / ship.tag / "prefetch.ended").exists()      # skipped: its step 2 is over too
    mark.write_text("de\n" + mark.read_text().splitlines()[1] + "\n")     # done for other pack languages
    assert _run(ship, SHIP_PREFETCH_ONLY="1").returncode == 0
    assert len(_lines(ship.logs["prefetch"])) == 2 and mark.read_text().startswith("de,fr,th\n")
    os.utime(ship.base, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))      # the file rewritten
    assert _run(ship, SHIP_PREFETCH_ONLY="1").returncode == 0
    assert len(_lines(ship.logs["prefetch"])) == 3


@pytest.mark.parametrize(("lines", "rc", "marked"), [
    ([{"titles_done": True, "failed": ["de"]}, {"failed": ["de"], "text_incomplete": []}], 1, False),
    ([{"titles_done": True, "failed": []}, {"failed": [], "text_incomplete": ["de"]}], 1, True),
    ([{"titles_done": True, "failed": []}], 124, True),          # the 6 h limit, every title map in
    ([{"lang": "de", "titles": 1}], 124, False)])                 # the 6 h limit before that
def test_a_failed_title_map_is_fetched_again_and_throttled_text_is_not(ship: SimpleNamespace, tmp_path: Path,
                                                                     lines: list, rc: int, marked: bool) -> None:
    """A language whose dumps or title map failed, or the 6 h limit before
    every title map was in, leaves no mark (a later run fetches again, and an
    older mark goes); Wikidata text left incomplete by throttling, or cut
    short by the limit after every title map, is recorded as done. The end
    of step 2 is recorded either way (prefetch.ended)."""
    pf = tmp_path / "prefetch-1.py"
    pf.write_text("import json, os, sys\nopen(os.environ['FAKE_PREFETCH_LOG'], 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                  + "".join(f"print(json.dumps({x!r}))\n" for x in lines) + f"sys.exit({rc})\n")
    mark = ship.work / "packs" / ship.tag / "prefetched"
    mark.parent.mkdir(parents=True)
    mark.write_text("de\nan older file\n")
    r = _run(ship, SHIP_PREFETCH_ONLY="1", SHIP_PREFETCH=str(pf))
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert mark.exists() is marked
    assert not (mark.parent / "prefetch.ended").exists()           # a run's, not a prefetch ahead's
    assert _run(ship, SHIP_PREFETCH_ONLY="1", SHIP_PREFETCH=str(pf)).returncode == 0
    assert len(_lines(ship.logs["prefetch"])) == (1 if marked else 2)
    assert _run(ship, SHIP_PREFETCH=str(pf)).returncode in (0, 1)             # a run: its step 2's end marked
    st = ship.base.stat()
    assert (mark.parent / "prefetch.ended").read_text() == f"{st.st_size} {st.st_mtime_ns // 10**9}.{st.st_mtime_ns % 10**9:09d}\n"


def test_listing_pending_then_rerun(ship: SimpleNamespace) -> None:
    r = _run(ship, FAKE_IA_PENDING="1")
    assert r.returncode == 6, r.stdout[-2000:] + r.stderr[-2000:]
    n = len(_lines(ship.logs["ia"]))
    assert "listing pending" in _log(ship)
    assert not (ship.work / "site" / "testland" / "recipes" / "testland" / "index.json").exists()
    # archive.org lists them now
    cache = ship.work / "ia-cache"
    pend = json.loads((cache / "ia-streetzim-testland-lang.pending.json").read_text())
    (cache / "ia-streetzim-testland-lang.json").write_text(json.dumps({"files": pend}))
    r2 = _run(ship)
    assert r2.returncode == 0, r2.stdout[-2000:] + r2.stderr[-2000:]
    assert len(_lines(ship.logs["ia"])) == n            # nothing sent twice


def test_never_overwrites_a_published_name(ship: SimpleNamespace) -> None:
    cache = ship.work / "ia-cache"
    (cache / "ia-streetzim-testland-lang.json").write_text(json.dumps({"files": [
        {"name": _pk(ship, "fr"), "size": "1", "md5": "0" * 32}]}))
    r = _run(ship)
    assert r.returncode == 1
    assert f"already holds {_pk(ship, 'fr')} with another MD5" in _log(ship)
    assert _lines(ship.logs["ia"]) == []


def test_an_unreadable_listing_is_not_an_empty_one(ship: SimpleNamespace, tmp_path: Path) -> None:
    """The first `ia metadata` of the -lang item fails: archive.org may hold a
    pack's name with another MD5 (here it does), so nothing is sent and the
    run stops with exit 8, to run again later."""
    cache = ship.work / "ia-cache"
    (cache / "ia-streetzim-testland-lang.json").write_text(json.dumps({"files": [
        {"name": _pk(ship, "fr"), "size": "1", "md5": "0" * 32}]}))
    counter = tmp_path / "meta-calls"
    r = _run(ship, FAKE_IA_META_FAIL=f"{counter}:1")
    assert counter.read_text() == "1"
    assert r.returncode == 8, _log(ship)[-2000:]
    assert "archive.org metadata unavailable: run again later" in _log(ship)
    assert _lines(ship.logs["ia"]) == []                      # nothing uploaded
    assert json.loads((cache / "ia-streetzim-testland-lang.json").read_text())["files"][0]["md5"] == "0" * 32


def test_a_failed_listing_poll_is_not_listed_yet(ship: SimpleNamespace, tmp_path: Path) -> None:
    """After the upload, a failed `ia metadata` only means "not listed yet":
    the next round sees the packs and the run goes on."""
    counter = tmp_path / "meta-calls"
    r = _run(ship, FAKE_IA_META_FAIL=f"{counter}:2")
    assert r.returncode == 0, _log(ship)[-2000:]
    assert int(counter.read_text()) >= 3 and "archive.org lists every pack" in _log(ship)
    assert len(_sent(ship)) == len(set(_sent(ship))) > 0      # each pack sent once


def test_only_passing_entries_are_published(ship: SimpleNamespace) -> None:
    """fr's gates fail: fr is not on the menu, and its pack is uploaded only
    because another entry (none here) uses it."""
    r = _run(ship, FAKE_GATES_FAIL="fr")
    assert r.returncode == 0, r.stderr[-2000:]
    idx = json.loads((ship.work / "site" / "testland" / "recipes" / "testland" / "index.json").read_text())
    assert [m["lang"] for m in idx["regions"][0]["menu"]] == ["de", "th", "en"]
    assert _pk(ship, "fr") not in _sent(ship)


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


def test_a_preview_on_test_items(ship: SimpleNamespace) -> None:
    """A preview ships to test items (the live ones untouched): the base on
    SHIP_BASE_ITEM, the packs to SHIP_PACK_ITEM with SHIP_ITEM_TITLE, and
    the recipes point there."""
    (ship.work / "ia-cache" / "ia-streetzim-langtest-testland.json").write_text(
        (ship.work / "ia-cache" / "ia-streetzim-testland.json").read_text())
    title = "PREVIEW (test) StreetZim Testland: Wikipedia packs"
    r = _run(ship, SHIP_BASE_ITEM="streetzim-langtest-testland", SHIP_PACK_ITEM="streetzim-langtest-testland-lang",
             SHIP_ITEM_TITLE=title)
    assert r.returncode == 0, _log(ship)[-2000:]
    ups = [json.loads(x) for x in _lines(ship.logs["ia"])]
    assert ups and all(x["item"] == "streetzim-langtest-testland-lang" and f"--metadata=title:{title}" in x["args"]
                       for x in ups), ups
    out = ship.work / "site" / "testland" / "recipes" / "testland"
    reg = json.loads((out / "index.json").read_text())["regions"][0]
    assert reg["base"]["url"] == f"{{proxy}}/streetzim-langtest-testland/{ship.base.name}"
    assert reg["base"]["download"] == f"https://archive.org/download/streetzim-langtest-testland/{ship.base.name}"
    rde = json.loads((out / "de.json").read_text())
    assert rde["packs"]["de"]["url"] == "{proxy}/streetzim-langtest-testland-lang/" + _pk(ship, "de")


def test_a_continent_or_a_big_base_gets_no_packs(ship: SimpleNamespace) -> None:
    """Re-review: v1 gives no packs to a continent (cloud/regions.tsv) or a
    base over SHIP_MAX_BASE_GB: refused in step 1, exit 4, before any
    download, pack build or gate."""
    for region, extra in (("europe", {}), ("testland", {"SHIP_MAX_BASE_GB": "0.000001"})):
        r = subprocess.run(["bash", str(SHIP), region, str(ship.base)], capture_output=True, text=True, timeout=300,
                           env={**ship.env, **extra})
        log = (ship.work / "logs" / f"ship-{region}-{ship.tag}.log").read_text()
        assert r.returncode == 4 and "no language packs in v1" in log, (region, log[-1500:])
        assert _lines(ship.logs["prefetch"]) == [] and _lines(ship.logs["build"]) == [] and _lines(ship.logs["gates"]) == []
    assert "a continent map" in (ship.work / "logs" / f"ship-europe-{ship.tag}.log").read_text()


def test_budget_refusals_are_loud(ship: SimpleNamespace) -> None:
    """Re-review: entries over the per-entry budget are refused before
    anything is sent, each said in the log; when no entry with a Wikipedia
    pack is left the run exits 5 and replaces nothing (it said "ready")."""
    assert _run(ship).returncode == 0
    site = ship.work / "site" / "testland" / "recipes" / "testland"
    menu = {m["lang"]: m["download_bytes"] for m in json.loads((site / "index.json").read_text())["regions"][0]["menu"]}
    small = min(v for v in menu.values() if v)
    big = sorted(L for L, v in menu.items() if v > small)
    assert big, menu
    sent = _sent(ship)
    r = _run(ship, LANGPACK_ENTRY_BUDGET=str(small))       # only the smallest entries fit
    log = _log(ship)
    assert r.returncode == 0, log[-2000:]
    for L in big:
        assert f"REFUSED {L}: over the per-entry budget" in log, log[-2000:]
    idx = json.loads((site / "index.json").read_text())
    assert sorted(m["lang"] for m in idx["regions"][0]["menu"]) == sorted(L for L in menu if L not in big)
    before = sorted(p.name for p in site.iterdir())
    for extra in ({"LANGPACK_ENTRY_BUDGET": "1"}, {"LANGPACK_RECIPE_BUDGET": "1"}):   # nothing with a pack fits
        r = _run(ship, **extra)
        log = _log(ship)
        assert r.returncode == 5, (extra, log[-2000:])
        assert "with a Wikipedia pack is over the per-entry budget: nothing to publish" in log
        assert sorted(p.name for p in site.iterdir()) == before
    assert _sent(ship) == sent                              # nothing sent for refused entries


def test_a_leftover_of_the_old_site_is_not_a_failed_swap(ship: SimpleNamespace) -> None:
    """Review LOW: the region's site files were replaced; only removing the
    previous ones (now in the staging folder) failed. The run says so and
    succeeds (`swap && rm || exit 1` reported "could not replace")."""
    assert _run(ship).returncode == 0
    stuck = ship.work / "site" / "testland" / "stuck"
    stuck.mkdir()
    (stuck / "f").write_text("x")
    stuck.chmod(0o555)                            # its file cannot be removed
    try:
        r = _run(ship)
        log = _log(ship)
        assert r.returncode == 0, log[-2000:]
        assert "could not replace" not in log and "replaced; the previous files" in log, log[-2000:]
        assert not (ship.work / "site" / "testland" / "stuck").exists()     # the new files are live
    finally:
        for d in (stuck, ship.work / "site" / ".testland.tmp" / "stuck"):
            if d.exists():
                d.chmod(0o755)


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
    n = len(_lines(ship.logs["ia"]))
    r2 = _run(ship, FAKE_IA_PENDING="1")
    assert r2.returncode == 6 and "not sent again" in _log(ship)
    assert len(_lines(ship.logs["ia"])) == n
    # still unlisted long after: the run stops and says what to do, and sends nothing
    sent = ship.work / "uploads" / "streetzim-testland-lang.tsv"
    rows = [x.split("\t") for x in sent.read_text().splitlines()]
    sent.write_text("".join(f"{f}\t{m}\t0\n" for f, m, _t in rows))
    r3 = _run(ship, FAKE_IA_PENDING="1")
    assert r3.returncode == 1 and "still not listed: check the item's tasks" in _log(ship)
    assert len(_lines(ship.logs["ia"])) == n


def test_a_failed_upload_sends_nothing_twice(ship: SimpleNamespace) -> None:
    """Review M-c: an upload cut off at the fr pack; the packs sent before it
    are recorded, so the rerun sends only what was not sent (one batch call
    recorded nothing and sent them all again, a second copy each while
    archive.org's tasks were pending). The failed upload exits 8, so the
    queue runs the region again (East Polynesia: an archive.org timeout after
    every gate passed was a failure left until a restart)."""
    r = _run(ship, FAKE_IA_PENDING="1", FAKE_IA_FAIL_ON=_pk(ship, "fr"))
    assert r.returncode == 8, _log(ship)[-2000:]
    log1 = _log(ship)
    r2 = _run(ship, FAKE_IA_PENDING="1")
    assert r2.returncode == 6, _log(ship)[-2000:]
    sent = _sent(ship)
    twice = sorted({f for f in sent if sent.count(f) > 1})
    assert twice == [_pk(ship, "fr")], sent          # only the one cut off is sent again
    assert f"ia upload of {_pk(ship, 'fr')} failed" in log1, log1[-2000:]


def test_uploads_hold_the_host_upload_lock(ship: SimpleNamespace) -> None:
    """Uploads wait for the packs' upload lock (UPLOAD_LOCK)."""
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
    assert len(_lines(ship.logs["ia"])) == len(set(_sent(ship))) > 0


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


@pytest.mark.parametrize("rid,refused", [("switzerland-light", True), ("switzerland-border-de-ultralight", True),
                                         ("switzerland", False), ("switzerland-de", False),
                                         ("switzerland-nosat", False)])
def test_light_maps_get_no_packs(rid: str, refused: bool, ship: SimpleNamespace) -> None:
    """Light and ultralight maps carry no Wikipedia; Switzerland itself, its
    no-satellite builds and the German Swiss maps are never refused here."""
    r = subprocess.run(["bash", str(SHIP), rid, str(ship.base)], capture_output=True, text=True, timeout=900,
                       env={**ship.env, "SHIP_SKIP_UPLOAD": "1"})
    assert ("is a light map" in r.stderr) is refused, r.stderr[-500:]
    if refused:
        assert r.returncode == 1


def _viewer_dir(d: Path, code: str) -> Path:
    """A viewer (the three files, index.html stamped with CODE) for SHIP_VIEWER_DIR."""
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(f"<html><body>map, viewer {code}<script>var SZ_VIEWER_BUILD = '{code}';</script></body></html>")
    (d / "places.html").write_text(f"<html><body>places, viewer {code}</body></html>")
    (d / "routing-worker.js").write_text(f"/* routing, viewer {code} */")
    return d


def test_a_viewer_patch_regenerates_only_the_recipes(ship: SimpleNamespace, tmp_path: Path) -> None:
    """Plan review B2: after cloud/patch_viewer_inplace.py (same file name and
    UUID, new viewer slots), a rerun builds no pack, no search pack and sends
    nothing, and publishes new recipes: the new base MD5, a new merged UUID
    (with the checkout's new viewer), the same masked base hashes. The
    versions carry the checkout's viewer whatever the base's: a base patched
    again with the checkout's viewer unchanged gives the same files."""
    from tests.langpack.test_viewer_slots import V2, patch
    base = tmp_path / "bases-s" / "osm-testland-2026-10-13.zim"
    base.parent.mkdir()
    lpzims.write_base(base, slots=lpzims.VIEWER_V1)
    builder = tmp_path / "builder-s.py"
    builder.write_text(f"""import argparse, json, os, sys
sys.path.insert(0, {str(Path(lpzims.__file__).parent)!r})
import lpzims
ap = argparse.ArgumentParser(); ap.add_argument("base"); ap.add_argument("--lang"); ap.add_argument("-o"); ap.add_argument("--stats")
a, _ = ap.parse_known_args()
open(os.environ["FAKE_BUILD_LOG"], "a").write(a.lang + "\\n")
lpzims.write_pack(__import__("pathlib").Path(a.o), a.lang, lpzims.read_uuid(a.base))
open(a.stats, "w").write(json.dumps({{"zim_bytes": os.path.getsize(a.o), "check": {{"ok": True, "problems": []}}}}))
""")
    def listed(b: Path) -> None:
        (ship.work / "ia-cache" / "ia-streetzim-testland.json").write_text(json.dumps(
            {"files": [{"name": b.name, "size": str(b.stat().st_size), "md5": _md5(b)}]}))
    vd = {c: _viewer_dir(tmp_path / f"viewer-{c}", c) for c in ("aaaa1111", "bbbb2222")}
    run = lambda code: subprocess.run(["bash", str(SHIP), "testland", str(base)], capture_output=True,  # noqa: E731
                                      text=True, timeout=900,
                                      env={**ship.env, "BUILDER": str(builder), "SHIP_VIEWER_DIR": str(vd[code])})
    listed(base)
    assert run("aaaa1111").returncode == 0
    site = ship.work / "site" / "testland" / "recipes" / "testland"
    r1 = json.loads((site / "de.json").read_text())
    reg1 = json.loads((site / "region.json").read_text())
    assert r1["file_name"].endswith(".vaaaa1111")
    builds, sends = _lines(ship.logs["build"]), _lines(ship.logs["ia"])
    patch(base, tmp_path / "patched.zim", V2, tmp_path)
    shutil.copy(tmp_path / "patched.zim", base)
    listed(base)                                   # the round re-uploaded the patched base
    r = run("bbbb2222")
    assert r.returncode == 0, r.stdout[-3000:]
    assert _lines(ship.logs["build"]) == builds and _lines(ship.logs["ia"]) == sends   # nothing rebuilt or sent
    log = (ship.work / "logs" / f"ship-testland-{base.name[:-4]}.log").read_text()
    assert "packs: all gated for this base already" in log
    r2 = json.loads((site / "de.json").read_text())
    reg2 = json.loads((site / "region.json").read_text())
    assert r2["base"]["sha256"] == r1["base"]["sha256"] and r2["fingerprint"] == r1["fingerprint"]
    assert r2["uuid"] != r1["uuid"] and r2["md5"] != r1["md5"] and r2["file_name"] == r1["file_name"][:-8] + "bbbb2222"
    assert reg2["base"]["md5"] == _md5(base) != reg1["base"]["md5"]
    from tests.langpack.test_viewer_slots import patch as patch_again
    patch_again(base, tmp_path / "patched2.zim", lpzims.VIEWER_V1, tmp_path)
    shutil.copy(tmp_path / "patched2.zim", base)
    listed(base)                                   # the base's viewer changed, the checkout's did not
    assert run("bbbb2222").returncode == 0
    r3 = json.loads((site / "de.json").read_text())
    assert (r3["uuid"], r3["md5"], r3["file_name"]) == (r2["uuid"], r2["md5"], r2["file_name"])
    assert _lines(ship.logs["build"]) == builds and _lines(ship.logs["ia"]) == sends


@pytest.mark.parametrize("case", ["swapped", "stale-sidecar", "own-packs", "reswapped"])
def test_a_viewer_swap_reuses_the_parents_packs(ship: SimpleNamespace, tmp_path: Path, case: str) -> None:
    """swap_base_viewer.py gives the base a new file name and UUID: its run
    takes the parent's packs and search packs (hard links; they fit by
    lineage), builds none and sends none, and publishes recipes for the new
    base. Not when the sidecar no longer names the base (patched since: the
    packs are built), nor for languages it has packs of its own for (a base
    swapped and shipped before: two fitting packs per language would let the
    gates and the recipes pick different ones). A swap redone to the same
    file (a new UUID) leaves the packs built for the old one: the parent's
    fit again, and the gates and recipes take them, not the newer ones."""
    from tests.langpack.test_viewer_slots import V2
    base = tmp_path / "bases-s" / "osm-testland-2026-10-13.zim"
    base.parent.mkdir()
    lpzims.write_base(base, slots=lpzims.VIEWER_V1)
    builder = tmp_path / "builder-s.py"
    builder.write_text(f"""import argparse, json, os, sys
sys.path.insert(0, {str(Path(lpzims.__file__).parent)!r})
import lpzims
ap = argparse.ArgumentParser(); ap.add_argument("base"); ap.add_argument("--lang"); ap.add_argument("-o"); ap.add_argument("--stats")
a, _ = ap.parse_known_args()
open(os.environ["FAKE_BUILD_LOG"], "a").write(a.lang + "\\n")
lpzims.write_pack(__import__("pathlib").Path(a.o), a.lang, lpzims.read_uuid(a.base))
open(a.stats, "w").write(json.dumps({{"zim_bytes": os.path.getsize(a.o), "check": {{"ok": True, "problems": []}}}}))
""")
    def listed(b: Path) -> None:
        (ship.work / "ia-cache" / "ia-streetzim-testland.json").write_text(json.dumps(
            {"files": [{"name": b.name, "size": str(b.stat().st_size), "md5": _md5(b)}]}))
    def run(b: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(SHIP), "testland", str(b)], capture_output=True, text=True,
                              timeout=900, env={**ship.env, "BUILDER": str(builder)})
    listed(base)
    assert run(base).returncode == 0
    vexp = tmp_path / "viewer-export"                      # a viewer that fits the test slots
    (vexp / "cloud").mkdir(parents=True)
    (vexp / "cloud" / "patch_viewer_inplace.py").symlink_to(ROOT / "cloud" / "patch_viewer_inplace.py")
    (vexp / "resources" / "viewer").mkdir(parents=True)
    for name, data in V2.items():                         # (a viewer for language packs: WIKI_PACKS)
        (vexp / "resources" / "viewer" / name).write_bytes(data + (b"<!-- WIKI_PACKS -->" if name == "index.html" else b""))
    new = base.with_name("osm-testland-2026-10-26.zim")
    sw = subprocess.run([sys.executable, str(ROOT / "tools" / "langpack" / "swap_base_viewer.py"), str(base), str(new),
                         str(vexp), "test", "--allow-full-copy"], capture_output=True, text=True, timeout=600)
    assert sw.returncode == 0, sw.stdout[-2000:] + sw.stderr[-2000:]
    assert lpzims.read_uuid(new) != lpzims.read_uuid(base)
    listed(new)                                         # the round uploaded the swapped base
    side = new.with_name(new.name + ".parent.json")
    if case == "stale-sidecar":
        d = json.loads(side.read_text())
        side.write_text(json.dumps({**d, "child_md5": "0" * 32}))
    elif case in ("own-packs", "reswapped"):            # shipped once before the sidecar counted
        side.rename(side.with_name("aside.json"))
        assert run(new).returncode == 0
        side.with_name("aside.json").rename(side)
    if case == "reswapped":                             # the swap again, to the same file: a new UUID
        own = sorted(p.name for p in (ship.work / "packs" / new.name[:-4]).glob("osm_testland_wiki-*.zim"))
        sw = subprocess.run([sys.executable, str(ROOT / "tools" / "langpack" / "swap_base_viewer.py"), str(base),
                             str(new), str(vexp), "test2", "--allow-full-copy"], capture_output=True, text=True,
                            timeout=600)
        assert sw.returncode == 0, sw.stdout[-2000:] + sw.stderr[-2000:]
        listed(new)
    old_dir, new_dir = ship.work / "packs" / base.name[:-4], ship.work / "packs" / new.name[:-4]
    parents = sorted(p.name for p in old_dir.glob("osm_testland_*.zim"))
    foreign = old_dir / "osm_testland_wiki-fr_2099-01-01_ffffffff.zim"     # gated, newest, but for another base
    lpzims.write_pack(foreign, "fr", "f" * 32)
    Path(f"{foreign}.gate.json").write_text(json.dumps({"ok": True, "pack_md5": _md5(foreign)}))
    builds, sends, fetches = _lines(ship.logs["build"]), _lines(ship.logs["ia"]), _lines(ship.logs["prefetch"])
    r = run(new)
    assert r.returncode == 0, r.stdout[-3000:]
    log = (ship.work / "logs" / f"ship-testland-{new.name[:-4]}.log").read_text()
    assert not (new_dir / foreign.name).exists()
    assert parents and any("_search-" in p for p in parents), parents
    reg = json.loads((ship.work / "site" / "testland" / "recipes" / "testland" / "region.json").read_text())
    assert reg["base"]["md5"] == _md5(new) and reg["base"]["file"] == new.name
    if case in ("swapped", "reswapped"):
        assert _lines(ship.logs["build"]) == builds and _lines(ship.logs["ia"]) == sends   # nothing rebuilt or sent
        assert _lines(ship.logs["prefetch"]) == fetches and "takes the mark of" in log    # nothing fetched again
        assert f"of {base.name[:-4]} (its viewer-swap parent" in log and "packs: all gated for this base already" in log
        for name in parents:
            assert (new_dir / name).stat().st_ino == (old_dir / name).stat().st_ino     # hard links, not copies
        if case == "reswapped":
            recipes = (ship.work / "site" / "testland" / "recipes").rglob("*.json")
            text = "".join(p.read_text() for p in recipes if p.name != "refused.json")
            assert own and not any(name in text for name in own), own                  # not the old UUID's packs
            assert all(name in text for name in parents if "_wiki-" in name)
    else:
        assert not any((new_dir / name).exists() for name in parents), sorted(p.name for p in new_dir.iterdir())
        assert "viewer-swap parent" not in log
        if case == "stale-sidecar":
            assert f"not reused: the packs of {base.name[:-4]}" in log
            assert len(_lines(ship.logs["build"])) > len(builds)              # built for this base instead
        else:
            assert _lines(ship.logs["build"]) == builds and _lines(ship.logs["ia"]) == sends


def test_language_torrents_ship_and_publish(ship: SimpleNamespace, tmp_path: Path) -> None:
    """LANGPACK_WEBSEED set: a webseeded torrent per entry beside the recipes,
    checked by site_check.py, sealed and published with them."""
    ws = "https://streetzim-build-proxy.example.workers.dev"
    r = _run(ship, LANGPACK_WEBSEED=ws)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    log = _log(ship)
    assert "language torrents: 4 (webseed " in log and "-> OK" in log
    out = ship.work / "site" / "testland" / "recipes" / "testland"
    reg = json.loads((out / "region.json").read_text())
    for m in reg["menu"]:
        assert m["torrent"] == f"recipes/testland/{m['lang']}.torrent" and len(m["infohash"]) == 40
        ix = json.loads((out / f"{m['lang']}.ws.json").read_text())
        assert ix["webseed"].startswith(f"{ws}/merged/testland/{m['lang']}/")
    seal = json.loads((out / "checked.json").read_text())
    assert any(f.endswith(".torrent") for f in seal["files"]) and any(f.startswith("ws-") for f in seal["files"])
    web = tmp_path / "web"
    (web / "build").mkdir(parents=True)
    p = subprocess.run(["bash", str(PUBLISH), "testland", str(web)], capture_output=True, text=True, timeout=120,
                       env=ship.env)
    assert p.returncode == 0, p.stderr
    pub = web / "build" / "recipes" / "testland"
    assert (pub / "de.torrent").read_bytes() == (out / "de.torrent").read_bytes()
    assert sorted(x.name for x in pub.glob("ws-*.bin")) == sorted(x.name for x in out.glob("ws-*.bin"))


def test_a_preview_on_test_items_gets_torrents_from_them(ship: SimpleNamespace) -> None:
    """SHIP_BASE_ITEM / SHIP_PACK_ITEM with LANGPACK_WEBSEED: the worker's
    segment index reads the base from the test item, with that item's ETag."""
    meta = json.loads((ship.work / "ia-cache" / "ia-streetzim-testland.json").read_text())
    meta["files"][0]["mtime"] = "1760000000"
    (ship.work / "ia-cache" / "ia-streetzim-langtest-testland.json").write_text(json.dumps(meta))
    r = _run(ship, SHIP_BASE_ITEM="streetzim-langtest-testland", SHIP_PACK_ITEM="streetzim-langtest-testland-lang",
             LANGPACK_WEBSEED="https://streetzim-build-proxy.example.workers.dev")
    assert r.returncode == 0, _log(ship)[-3000:]
    out = ship.work / "site" / "testland" / "recipes" / "testland"
    menu = json.loads((out / "region.json").read_text())["menu"]
    assert menu and all("torrent" in m for m in menu), menu
    etag = '"%x-%x"' % (1760000000, ship.base.stat().st_size)
    for m in menu:
        srcs = json.loads((out / f"{m['lang']}.ws.json").read_text())["sources"].values()
        bases = [x for x in srcs if x["file"] == ship.base.name]
        assert bases and all(x["item"] == "streetzim-langtest-testland" and x.get("etag") == etag for x in bases), srcs
        assert all(x["item"] in ("streetzim-langtest-testland", "streetzim-langtest-testland-lang") for x in srcs)


def test_a_failing_torrent_step_publishes_without_torrents(ship: SimpleNamespace) -> None:
    r = _run(ship, LANGPACK_WEBSEED="https://example.invalid/not-an-origin")     # refused: the tool exits 1
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert "publishing WITHOUT torrents" in _log(ship)
    out = ship.work / "site" / "testland" / "recipes" / "testland"
    assert not list(out.glob("*.torrent")) and not list(out.glob("ws-*.bin"))
    assert all("torrent" not in m for m in json.loads((out / "region.json").read_text())["menu"])


def test_a_viewer_patch_regenerates_the_torrents(ship: SimpleNamespace, tmp_path: Path) -> None:
    """Follow-up review H-2: the patched-base rerun regenerates the language
    torrents too — a new recipe UUID in the webseed URL, a new infohash, and
    the base's new archive.org ETag (its new mtime) in the index."""
    import time
    from tests.langpack.test_viewer_slots import V2, patch
    base = tmp_path / "bases-t" / "osm-testland-2026-10-13.zim"
    base.parent.mkdir()
    lpzims.write_base(base, slots=lpzims.VIEWER_V1)
    builder = tmp_path / "builder-t.py"
    builder.write_text(f"""import argparse, json, os, sys
sys.path.insert(0, {str(Path(lpzims.__file__).parent)!r})
import lpzims
ap = argparse.ArgumentParser(); ap.add_argument("base"); ap.add_argument("--lang"); ap.add_argument("-o"); ap.add_argument("--stats")
a, _ = ap.parse_known_args()
open(os.environ["FAKE_BUILD_LOG"], "a").write(a.lang + "\\n")
lpzims.write_pack(__import__("pathlib").Path(a.o), a.lang, lpzims.read_uuid(a.base))
open(a.stats, "w").write(json.dumps({{"zim_bytes": os.path.getsize(a.o), "check": {{"ok": True, "problems": []}}}}))
""")
    def listed(b: Path, mtime: int) -> None:
        (ship.work / "ia-cache" / "ia-streetzim-testland.json").write_text(json.dumps(
            {"files": [{"name": b.name, "size": str(b.stat().st_size), "md5": _md5(b), "mtime": str(mtime)}]}))
    ws = "https://streetzim-build-proxy.example.workers.dev"
    vd = {c: _viewer_dir(tmp_path / f"viewer-{c}", c) for c in ("aaaa1111", "bbbb2222")}
    run = lambda code: subprocess.run(["bash", str(SHIP), "testland", str(base)], capture_output=True,  # noqa: E731
                                      text=True, timeout=900, env={**ship.env, "BUILDER": str(builder),
                                                                   "LANGPACK_WEBSEED": ws, "SHIP_VIEWER_DIR": str(vd[code])})
    listed(base, 1792287864)
    r = run("aaaa1111")
    assert r.returncode == 0, r.stdout[-3000:]
    site = ship.work / "site" / "testland" / "recipes" / "testland"
    ix1 = json.loads((site / "de.ws.json").read_text())
    t1 = (site / "de.torrent").read_bytes()
    assert ix1["sources"]["base"]["etag"] == '"6ad42478-%x"' % base.stat().st_size
    patch(base, tmp_path / "patched.zim", V2, tmp_path)
    shutil.copy(tmp_path / "patched.zim", base)
    listed(base, 1792287864 + 3600)                # re-uploaded: archive.org's mtime moves on
    time.sleep(0.01)
    r = run("bbbb2222")
    assert r.returncode == 0, r.stdout[-3000:]
    ix2 = json.loads((site / "de.ws.json").read_text())
    reg2 = json.loads((site / "region.json").read_text())
    assert ix2["uuid"] != ix1["uuid"] and ix2["uuid"] in ix2["webseed"]
    assert ix2["sources"]["base"]["etag"] == '"6ad43288-%x"' % base.stat().st_size
    assert (site / "de.torrent").read_bytes() != t1
    assert next(m for m in reg2["menu"] if m["lang"] == "de")["infohash"] == ix2["infohash"] != ix1["infohash"]


def test_torrent_only_check_failures_publish_without_torrents(ship: SimpleNamespace) -> None:
    """Follow-up review SH1: when site_check.py fails only on the torrent files
    (here: the index lacks the base's archive.org ETag the metadata cache now
    has), ship_packs.sh strips them, checks again and publishes the recipes,
    saying so loudly."""
    (ship.work / "ia-cache" / "ia-streetzim-testland.json").write_text(json.dumps(
        {"files": [{"name": ship.base.name, "size": str(ship.base.stat().st_size), "md5": _md5(ship.base),
                    "mtime": "1792287864"}]}))
    r = _run(ship, LANGPACK_WEBSEED="https://streetzim-build-proxy.example.workers.dev",
             SHIP_LANG_TORRENTS_ARGS=f"--ia-cache {ship.d / 'empty-ia'}")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    log = _log(ship)
    assert "WARNING: site_check.py: only torrent checks failed" in log
    assert "FAIL TORRENT de:" in log and "WARNING: no language torrents this time" in log
    out = ship.work / "site" / "testland" / "recipes" / "testland"
    assert not list(out.glob("*.torrent")) and not list(out.glob("ws-*.bin"))
    assert all("torrent" not in m for m in json.loads((out / "region.json").read_text())["menu"])
    assert (out / "checked.json").exists()                          # sealed: publishable


def test_a_torrent_timeout_is_loud_and_publishes_without(ship: SimpleNamespace) -> None:
    r = _run(ship, LANGPACK_WEBSEED="https://streetzim-build-proxy.example.workers.dev", SHIP_TORRENT_TIMEOUT="0.01")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    log = _log(ship)
    assert "WARNING: lang_torrents.py ran out of its 0.01 s" in log
    assert "WARNING: no language torrents this time: lang_torrents.py ran out" in log
    out = ship.work / "site" / "testland" / "recipes" / "testland"
    assert not list(out.glob("*.torrent"))


SLOW_PREFETCH = """import os, sys, time
log = os.environ["SLOW_PREFETCH_LOG"]
open(log, "a").write(f"start {time.time()}\\n")
time.sleep(2)
open(log, "a").write(f"end {time.time()}\\n")
"""


def test_two_runs_take_turns_on_the_wikipedia_cache(ship: SimpleNamespace, tmp_path: Path) -> None:
    """pack_queue.sh runs two ship_packs.sh side by side: their network steps
    (prefetch.py: downloads into, and appends to, the shared cache) take
    turns under a lock on the cache."""
    slow = tmp_path / "slow_prefetch.py"
    slow.write_text(SLOW_PREFETCH)
    log = tmp_path / "prefetch-times.log"
    b2 = tmp_path / "bases2" / "osm-testland2-2026-10-13.zim"
    b2.parent.mkdir()
    shutil.copy(ship.base, b2)
    env = {**ship.env, "SHIP_PREFETCH": str(slow), "SLOW_PREFETCH_LOG": str(log), "SHIP_SKIP_UPLOAD": "1"}
    ps = [subprocess.Popen(["bash", str(SHIP), rid, str(b)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
          for rid, b in (("testland", ship.base), ("testland2", b2))]
    assert [p.wait(timeout=900) for p in ps] == [3, 3]
    ev = [x.split() for x in log.read_text().splitlines()]
    assert [e[0] for e in ev] == ["start", "end", "start", "end"], ev       # one after the other, never together


GB = 1 << 30


def _nvme(ship: SimpleNamespace, free_gb: int = 500, min_gb: int = 100) -> dict[str, str]:
    """An NVMe of its own for the run: its scratch, the shared reservations'
    folder and the parallel runner's state, all here."""
    for x in ("nvme/packs", "nvme/acct", "par/status", "parscratch"):
        (ship.d / x).mkdir(parents=True, exist_ok=True)
    return {"LANGPACK_SCRATCH": str(ship.d / "nvme" / "packs"), "NVME_SCRATCH": str(ship.d / "nvme" / "acct"),
            "PAR_STATE": str(ship.d / "par"), "PAR_SCRATCH": str(ship.d / "parscratch"),
            "LANGPACK_NVME_FREE": str(free_gb * GB), "LANGPACK_MEM_AVAILABLE": str(64 * GB),
            "LANGPACK_NVME_MIN_GB": str(min_gb)}


def test_the_search_packs_and_gates_work_on_the_nvme(ship: SimpleNamespace) -> None:
    """With room on the NVMe: the search packs and gates read a checked copy
    of the base there (same name, MD5 and mtime: the same recipes and
    verdicts), merge into the run's own folder, run four at a time, and the
    run is reserved where the map builds count it; the folder and the
    reservation are gone once the gates are done."""
    r = _run(ship, **_nvme(ship))
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    g = json.loads(_lines(ship.logs["gates"])[0])
    run = ship.d / "nvme" / "packs" / f"run-testland-{g['base'].split('run-testland-')[1].split('/')[0]}"
    assert g["base"] == str(run / "base" / ship.base.name), g
    assert g["base_md5"] == _md5(ship.base) and g["base_mtime"] == ship.base.stat().st_mtime_ns
    assert g["jobs"] == "4" and g["merged"] == str(run / "merged") and g["tmp"] == str(run / "tmp"), g
    (name, (b, st)), = g["resv"].items()
    assert name.startswith(".reserve-ml-langpack-") and int(b) > 0 and st.isdigit(), g["resv"]
    zims = [x for x in g["search_packs"] if x.endswith(".zim")]       # built on the NVMe, moved beside the packs
    assert len(zims) == 3 and sorted(g["search_packs"]) == sorted(z + e for z in zims for e in ("", ".json", ".gate.json"))
    assert not run.exists() and not list((ship.d / "nvme" / "acct").iterdir())
    log = _log(ship)
    assert "NVMe scratch: " + str(run) in log and "4 gate and 4 search jobs" in log and "base staged" in log
    # the recipes and the site name the published base, not the copy
    rde = json.loads((ship.work / "site" / "testland" / "recipes" / "testland" / "de.json").read_text())
    assert rde["base"]["file"] == ship.base.name


def test_the_nvme_holds_the_copy_with_todays_viewer(ship: SimpleNamespace, tmp_path: Path) -> None:
    """A base with viewer slots: the search packs and gates on the NVMe read a
    staged copy of tools/langpack/viewer_base.py's copy (today's viewer), not
    of the published base, so the gated recipes are the ones make_site.py
    publishes; the site still names the published base."""
    base = tmp_path / "bases-n" / "osm-testland-2026-10-13.zim"
    base.parent.mkdir()
    lpzims.write_base(base, slots=lpzims.VIEWER_V1)
    builder = tmp_path / "builder-n.py"
    builder.write_text(f"""import argparse, json, os, sys
sys.path.insert(0, {str(Path(lpzims.__file__).parent)!r})
import lpzims
ap = argparse.ArgumentParser(); ap.add_argument("base"); ap.add_argument("--lang"); ap.add_argument("-o"); ap.add_argument("--stats")
a, _ = ap.parse_known_args()
open(os.environ["FAKE_BUILD_LOG"], "a").write(a.lang + "\\n")
lpzims.write_pack(__import__("pathlib").Path(a.o), a.lang, lpzims.read_uuid(a.base))
open(a.stats, "w").write(json.dumps({{"zim_bytes": os.path.getsize(a.o), "check": {{"ok": True, "problems": []}}}}))
""")
    (ship.work / "ia-cache" / "ia-streetzim-testland.json").write_text(json.dumps(
        {"files": [{"name": base.name, "size": str(base.stat().st_size), "md5": _md5(base)}]}))
    vd = _viewer_dir(tmp_path / "viewer-n", "cccc3333")
    r = subprocess.run(["bash", str(SHIP), "testland", str(base)], capture_output=True, text=True, timeout=900,
                       env={**ship.env, **_nvme(ship), "BUILDER": str(builder), "SHIP_VIEWER_DIR": str(vd)})
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    copy = ship.work / "viewer-base" / base.stem / base.name
    g = json.loads(_lines(ship.logs["gates"])[-1])
    assert "/run-testland-" in g["base"] and g["base"].endswith("/base/" + base.name), g
    assert g["base_md5"] == _md5(copy) != _md5(base)
    rde = json.loads((ship.work / "site" / "testland" / "recipes" / "testland" / "de.json").read_text())
    assert rde["base"]["file"] == base.name and rde["file_name"].endswith(".vcccc3333")
    assert (ship.work / "packs" / base.stem / "viewer-code").read_text() == "cccc3333\n"


@pytest.mark.parametrize("why", ["kept-free", "runner", "reservation"])
def test_without_room_on_the_nvme_everything_stays_on_storage(ship: SimpleNamespace, why: str) -> None:
    """The NVMe's room is counted as the parallel runner counts it: what it
    keeps free, what its running builds may still write, and the live
    reservations; short of room for the base and one job, the run works on
    LANGPACK_WORK, one job at a time, as before."""
    env = _nvme(ship, free_gb=500, min_gb=600 if why == "kept-free" else 100)
    if why == "runner":
        (ship.d / "par" / "status" / "rome").write_text("running w0 1 2 3 ports=1,2 cpus=0-11 proj=450G\n")
    if why == "reservation":                                 # a live tile store's, as region-ml-tiles.py writes it
        me = os.getpid()
        mine = Path(f"/proc/{me}/stat").read_text()
        (ship.d / "nvme" / "acct" / f".reserve-ml-{me}").write_text(f"{450 * GB} {mine[mine.rindex(')') + 2:].split()[19]}\n")
    r = _run(ship, **env)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    g = json.loads(_lines(ship.logs["gates"])[0])
    assert g["base"] == str(ship.base) and g["jobs"] == "1" and g["merged"] is None, g
    assert "NVMe scratch: not used" in _log(ship)
    assert not list((ship.d / "nvme" / "packs").glob("run-*"))


def test_a_pack_that_ran_out_of_time_is_listed_for_a_later_run(ship: SimpleNamespace) -> None:
    """A pack whose build ran out of its time: the run goes on without it and
    lists it in retry-langs for the queue; the next run builds that one alone
    and removes the list."""
    r = _run(ship, FAKE_SLOW="fr", PACK_BUILD_TIMEOUT="3")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    packs = ship.work / "packs" / ship.tag
    assert (packs / "retry-langs").read_text().split() == ["fr"]
    assert "ran out of time: fr" in _log(ship)
    n = len(_lines(ship.logs["build"]))
    r = _run(ship)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert _lines(ship.logs["build"])[n:] == ["fr"] and not (packs / "retry-langs").exists()


def test_the_io_class_is_best_effort_even_when_started_at_idle(ship: SimpleNamespace) -> None:
    """mq-deadline serves the idle class only when the disk is idle, which a
    round's builds never leave it: ship_packs.sh works at best effort, lowest
    level, even when the queue (or anyone) starts it at the idle class."""
    r = subprocess.run(["ionice", "-c3", "bash", str(SHIP), "testland", str(ship.base)], capture_output=True, text=True,
                       timeout=900, env=ship.env)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert json.loads(_lines(ship.logs["gates"])[0])["io"] == "best-effort: prio 7"


def test_the_scratch_is_released_before_the_uploads_and_on_a_signal(ship: SimpleNamespace) -> None:
    """The NVMe folder is gone before the first upload (the uploads may wait
    hours for archive.org); a run stopped by a signal (the queue's kill, a
    timeout) removes its folder and its reservation too; the search packs
    were built there, three at a time."""
    env = _nvme(ship)
    r = _run(ship, **env)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    ups = [json.loads(x) for x in _lines(ship.logs["ia"])]
    assert ups and all(x["scratch_runs"] == [] for x in ups), ups
    assert "search packs: 3 to build, 3 at a time, in " + str(ship.d / "nvme" / "packs" / "run-testland-") in _log(ship)
    # a stale list of languages that ran out of time goes once every pack is gated
    (ship.work / "packs" / ship.tag / "retry-langs").write_text("xx\n")
    assert _run(ship, **env).returncode == 0 and not (ship.work / "packs" / ship.tag / "retry-langs").exists()
    # stopped by a signal while the gates run
    import signal
    import time
    shutil.rmtree(ship.work / "verdicts")
    p = subprocess.Popen(["bash", str(SHIP), "testland", str(ship.base)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env={**ship.env, **env, "FAKE_GATES_SLEEP": "60"}, start_new_session=True)
    try:
        t0 = time.time()
        while len(_lines(ship.logs["gates"])) < 3 and time.time() - t0 < 300:
            time.sleep(0.2)
        assert list((ship.d / "nvme" / "packs").glob("run-*")) and list((ship.d / "nvme" / "acct").iterdir())
        os.killpg(p.pid, signal.SIGTERM)
        assert p.wait(timeout=60) != 0
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
    t0 = time.time()
    while list((ship.d / "nvme" / "acct").iterdir()) and time.time() - t0 < 30:
        time.sleep(0.2)
    assert not list((ship.d / "nvme" / "packs").glob("run-*")) and not list((ship.d / "nvme" / "acct").iterdir())


@pytest.mark.parametrize("slow", ["fr", "de,fr,th"])
def test_a_region_language_out_of_time_means_run_again_not_refused(ship: SimpleNamespace, slow: str) -> None:
    """A region language's pack (fr here) is in every entry: out of time, it
    leaves nothing publishable. That is no refusal (exit 5, never retried)
    but exit 7, run again later, with retry-langs saying which; so too when
    every pack ran out of time (no Wikipedia pack at all)."""
    uuid = lpzims.write_base(ship.base, cfg_extra={"regionLanguages": ["fr"]})
    fake = ship.d / "fakepacks"
    for p in fake.iterdir():                                  # the stand-in builder's packs, for this base
        lpzims.write_pack(p, p.name.split("wiki-")[1][:2], uuid)
    (ship.work / "ia-cache" / "ia-streetzim-testland.json").write_text(json.dumps(
        {"files": [{"name": ship.base.name, "size": str(ship.base.stat().st_size), "md5": _md5(ship.base)}]}))
    r = _run(ship, FAKE_SLOW=slow, PACK_BUILD_TIMEOUT="3")
    assert r.returncode == 7, (r.returncode, r.stdout[-3000:] + r.stderr[-3000:])
    packs = ship.work / "packs" / ship.tag
    assert (packs / "retry-langs").read_text().split() == slow.split(","), (packs / "retry-langs").read_text()
    assert "ran out of time: run again later (exit 7)" in _log(ship)
