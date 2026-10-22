"""ops/langpack/pack_queue.sh: the language step after each map of the
rebuild round. Stand-ins for ship_packs.sh, publish_site.sh and firebase;
a small git repository as production; a round TSV written by hand."""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("libzim")

from tests.langpack import lpzims  # noqa: E402
from tests.langpack.conftest import ROOT  # noqa: E402

QUEUE = ROOT / "ops" / "langpack" / "pack_queue.sh"
T0 = 1_800_000_000                     # the queue's start in these tests

# ship_packs.sh: logs the call; FAKE_SHIP_RC "region:rc,rc;region2:rc" (one rc per call, the last repeats)
FAKE_SHIP = r'''#!PY
import json, os, sys, time
rid, base = sys.argv[1], sys.argv[2]
log = os.environ["FAKE_LOG"]
calls = [json.loads(x) for x in open(log)] if os.path.exists(log) else []
n = sum(1 for c in calls if c.get("ship") == rid)
plan = dict(x.split(":", 1) for x in os.environ.get("FAKE_SHIP_RC", "").split(";") if x)
rcs = (plan.get(rid) or "0").split(",")
rc = int(rcs[min(n, len(rcs) - 1)])
with open(log, "a") as fh:
    fh.write(json.dumps({"ship": rid, "base": base, "work": os.environ.get("LANGPACK_WORK"),
                         "webseed": os.environ.get("LANGPACK_WEBSEED"), "maxgb": os.environ.get("SHIP_MAX_BASE_GB"),
                         "rc": rc, "t": time.time()}) + "\n")
time.sleep(float(os.environ.get("FAKE_SHIP_SLEEP", "0")))
with open(log, "a") as fh:
    fh.write(json.dumps({"ship_end": rid, "t": time.time()}) + "\n")
if rc == 0:
    d = os.path.join(os.environ["LANGPACK_WORK"], "site", rid, "recipes", rid)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "region.json"), "w").write(json.dumps({"id": rid, "base": os.path.basename(base)}))
sys.exit(rc)
'''
# publish_site.sh REGION WEB: copies the site files over; FAKE_PUBLISH_RC
FAKE_PUBLISH = r'''#!/bin/bash
echo "{\"publish\": \"$1\", \"web\": \"$2\", \"work\": \"$LANGPACK_WORK\"}" >> "$FAKE_LOG"
[ "${FAKE_PUBLISH_RC:-0}" = 0 ] || exit "$FAKE_PUBLISH_RC"
mkdir -p "$2/build/recipes" && rm -rf "$2/build/recipes/$1" && cp -a "$LANGPACK_WORK/site/$1/recipes/$1" "$2/build/recipes/$1"
'''
# firebase: logs where and what; fails while FAKE_FIREBASE_FAILS > the calls so far
# archive.org's CLI: `metadata ITEM` prints FAKE_IA_DIR/ITEM.json (or an empty item)
FAKE_IA = r'''#!PY
import os, sys
p = os.path.join(os.environ["FAKE_IA_DIR"], sys.argv[2] + ".json")
print(open(p).read() if sys.argv[1] == "metadata" and os.path.exists(p) else "{}")
'''
FAKE_FIREBASE = r'''#!PY
import json, os, sys
log = os.environ["FAKE_LOG"]
n = sum(1 for x in open(log) if '"firebase"' in x) if os.path.exists(log) else 0
rc = 1 if n < int(os.environ.get("FAKE_FIREBASE_FAILS", "0")) else 0
open(log, "a").write(json.dumps({"firebase": sys.argv[1:], "cwd": os.getcwd(), "rc": rc}) + "\n")
sys.exit(rc)
'''


def _iso(t: int) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


@pytest.fixture
def q(tmp_path: Path) -> SimpleNamespace:
    d = tmp_path
    stubs = d / "stubs"
    stubs.mkdir()
    for name, body in (("ship.py", FAKE_SHIP), ("publish.sh", FAKE_PUBLISH), ("firebase", FAKE_FIREBASE), ("ia", FAKE_IA)):
        p = stubs / name
        p.write_text(body.replace("#!PY", f"#!{sys.executable}"))
        p.chmod(0o755)
    (stubs / "ship.sh").write_text(f'#!/bin/bash\nexec {sys.executable} {stubs / "ship.py"} "$@"\n')
    maps = d / "maps"
    maps.mkdir()
    ml = {"tileLanguages": ["de", "fr"]}
    lpzims.write_base(maps / "osm-alpha-2026-10-23.zim", cfg_extra=ml)
    lpzims.write_base(maps / "osm-beta-2026-10-23.zim", cfg_extra=ml)
    lpzims.write_base(maps / "osm-old-2026-10-01.zim", cfg_extra=ml)
    lpzims.write_base(maps / "osm-gamma-2026-10-23.zim", cfg_extra=ml)
    lpzims.write_base(maps / "osm-noml-2026-10-23.zim")                         # no multilingual tiles
    lpzims.write_base(maps / "osm-big-2026-10-23.zim", cfg_extra=ml,            # over the size limit set below
                      extra={"pad.json": "".join(random.choice("0123456789abcdef") for _ in range(600_000))})
    lpzims.write_base(maps / "osm-cont-2026-10-23.zim", cfg_extra=ml)
    lpzims.write_base(maps / "osm-fail-2026-10-23.zim", cfg_extra=ml)
    lpzims.write_base(maps / "osm-delta-2026-10-23.zim", cfg_extra=ml)
    tsv = d / "rebuild-old.tsv"
    rows = [("old", "uploaded", "osm-old-2026-10-01.zim", T0 - 3600),          # shipped before the queue started
            ("beta", "uploaded", "osm-beta-2026-10-23.zim", T0 + 10),
            ("alpha", "uploaded", "osm-alpha-2026-10-23.zim", T0 + 20),
            ("gamma", "uploaded", "osm-gamma-2026-10-23.zim", T0 + 30),
            ("gamma", "upload-failed", "osm-gamma-2026-10-23.zim", T0 + 40),   # its latest row: not uploaded
            ("delta", "upload-pending", "osm-delta-2026-10-23.zim", T0 + 45),
            ("switzerland-light", "uploaded", "osm-switzerland-light-2026-10-23.zim", T0 + 50),
            ("noml", "uploaded", "osm-noml-2026-10-23.zim", T0 + 60),
            ("big", "uploaded", "osm-big-2026-10-23.zim", T0 + 70),
            ("cont", "uploaded", "osm-cont-2026-10-23.zim", T0 + 80),
            ("fail", "uploaded", "osm-fail-2026-10-23.zim", T0 + 90)]
    tsv.write_text("id\tstatus\tzim\tfinished\n" + "".join(f"{a}\t{b}\t{c}\t{_iso(t)}\n" for a, b, c, t in rows))
    prod = d / "prod"
    prod.mkdir()
    (prod / "web").mkdir()
    (prod / "web" / "index.html").write_text("site v1\n")
    (prod / ".gitignore").write_text("/web/build/recipes/\n")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q", str(prod)], check=True)
    subprocess.run([*git, "-C", str(prod), "add", "-A"], check=True)
    subprocess.run([*git, "-C", str(prod), "commit", "-q", "-m", "v1"], check=True)
    work = d / "work"
    qd = work / "pack-queue"
    qd.mkdir(parents=True)
    (qd / "since").write_text(f"{T0}\n")
    log = d / "fake.log"
    (d / "ia").mkdir()
    env = {**os.environ, "LANGPACK_WORK": str(work), "ROUND_TSV": str(tsv), "MAPS_DIR": str(maps), "PROD_REPO": str(prod),
           "PREVIEW_DIR": str(d / "preview" / "streetzim"), "FIREBASE": str(stubs / "firebase"),
           "QUEUE_SHIP": str(stubs / "ship.sh"), "QUEUE_PUBLISH": str(stubs / "publish.sh"), "PYTHON": sys.executable,
           "QUEUE_RETRY_SECS": "0", "QUEUE_REAP_SECS": "0.2", "QUEUE_MIN_MEM_GB": "0", "QUEUE_DISK_MARGIN_GB": "0",
           "QUEUE_MAX_BASE_GB": str((maps / "osm-big-2026-10-23.zim").stat().st_size / 1e9 - 1e-6),
           "FAKE_LOG": str(log), "FAKE_SHIP_RC": "alpha:6,0;cont:4;fail:1", "PYTHONDONTWRITEBYTECODE": "1",
           "IA": str(stubs / "ia"), "FAKE_IA_DIR": str(d / "ia"), "QUEUE_SEED": "",
           "QUEUE_ROUND_PATTERNS": f"no-such-round-{os.getpid()}.sh"}     # not the host's own round
    env.pop("LANGPACK_WEBSEED", None)
    return SimpleNamespace(d=d, tsv=tsv, maps=maps, prod=prod, git=git, work=work, qd=qd, log=log, env=env,
                           preview=d / "preview" / "streetzim")


def _run(q: SimpleNamespace, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(QUEUE), "--once"], capture_output=True, text=True, timeout=600, env={**q.env, **extra})


def _calls(q: SimpleNamespace, kind: str) -> list[dict]:
    return [c for c in (json.loads(x) for x in q.log.read_text().splitlines()) if kind in c] if q.log.exists() else []


def log_text(q: SimpleNamespace) -> str:
    return (q.qd / "queue.log").read_text()


def _status(q: SimpleNamespace) -> dict[str, list[str]]:
    return {f[0]: f for f in (x.split("\t") for x in (q.qd / "status.tsv").read_text().splitlines())}


def test_ships_in_order_publishes_to_the_preview_and_resumes(q: SimpleNamespace) -> None:
    tsv_before = q.tsv.read_bytes()
    r = _run(q)
    assert r.returncode == 1 and "FAILED: fail: ship_packs.sh rc=1" in r.stderr, r.stderr   # a loud failure
    ships = _calls(q, "ship")
    # what shipped since the start and is eligible, in the order it shipped; with production's settings
    assert [c["ship"] for c in ships] == ["beta", "alpha", "cont", "fail"], ships
    assert all(c["work"] == str(q.work) and c["webseed"] == "https://streetzim-build-proxy.tiltastech.workers.dev"
               and c["base"] == str(q.maps / f"osm-{c['ship']}-2026-10-23.zim") for c in ships), ships
    st = _status(q)
    assert st["beta"][1] == "published" and st["alpha"][1] == "pending" and st["cont"][1] == "refused-4", st
    assert st["fail"][1] == "failed" and st["noml"][1] == "ineligible-no-ml-tiles" and st["big"][1] == "ineligible-size", st
    assert st["switzerland-light"][1] == "ineligible-light" and "gamma" not in st and "old" not in st
    assert st["delta"][1] == "waiting-listing", st           # upload-pending, archive.org does not list it yet
    assert "big: osm-big-2026-10-23.zim is" in log_text(q) and "GB limit: no language packs in v1" in log_text(q)
    # published into the preview clone at production's commit, then the preview channel deployed from it
    assert (q.preview / "web" / "build" / "recipes" / "beta" / "region.json").exists()
    head = subprocess.run(["git", "-C", str(q.prod), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert subprocess.run(["git", "-C", str(q.preview), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() == head
    fb = _calls(q, "firebase")
    assert [c["firebase"] for c in fb] == [["hosting:channel:deploy", "swiss-languages", "--expires", "30d", "--project", "streetzim"]]
    assert fb[0]["cwd"] == str(q.preview)
    log = (q.qd / "queue.log").read_text()
    assert " PDT " in log or " PST " in log
    # the second pass: alpha (listing pending before) ships and deploys; beta, cont, the ineligible ones not again; fail is tried again
    r2 = _run(q)
    ships2 = [c["ship"] for c in _calls(q, "ship")][len(ships):]
    assert ships2 == ["alpha", "fail"], ships2
    assert _status(q)["alpha"][1] == "published" and len(_calls(q, "firebase")) == 2
    assert r2.returncode == 1
    published = [x.split("\t")[0] for x in (q.qd / "published.tsv").read_text().splitlines()]
    assert published == ["beta", "alpha"]
    assert q.tsv.read_bytes() == tsv_before                       # the round's file only read


def test_a_new_file_ships_again_and_the_preview_follows_production(q: SimpleNamespace) -> None:
    assert _run(q, FAKE_SHIP_RC="").returncode == 0
    n = len(_calls(q, "ship"))
    # beta rebuilt: a new file, a new UUID; production moved on meanwhile
    lpzims.write_base(q.maps / "osm-beta-2026-10-24.zim", cfg_extra={"tileLanguages": ["de"]})
    with open(q.tsv, "a") as fh:
        fh.write(f"beta\tuploaded\tosm-beta-2026-10-24.zim\t{_iso(T0 + 500)}\n")
    (q.prod / "web" / "index.html").write_text("site v2\n")
    subprocess.run([*q.git, "-C", str(q.prod), "commit", "-q", "-am", "v2"], check=True)
    assert _run(q, FAKE_SHIP_RC="").returncode == 0
    again = _calls(q, "ship")[n:]
    assert [(c["ship"], Path(c["base"]).name) for c in again] == [("beta", "osm-beta-2026-10-24.zim")], again
    assert (q.preview / "web" / "index.html").read_text() == "site v2\n"
    assert (q.preview / "web" / "build" / "recipes" / "alpha").exists()   # what was published stays


def test_a_failed_deploy_is_tried_again_without_shipping_again(q: SimpleNamespace) -> None:
    r = _run(q, FAKE_SHIP_RC="", FAKE_FIREBASE_FAILS="100")
    assert "FAILED: the preview deploy failed" in r.stderr, r.stderr
    st = _status(q)
    assert st["beta"][1] == st["alpha"][1] == "published-not-deployed" and (q.qd / "deploy-pending").exists(), st
    n = len(_calls(q, "ship"))
    _run(q, FAKE_SHIP_RC="", FAKE_FIREBASE_FAILS="0")
    assert len(_calls(q, "ship")) == n and not (q.qd / "deploy-pending").exists()
    assert [c["rc"] for c in _calls(q, "firebase")][-1] == 0
    st = _status(q)
    assert st["beta"][1] == st["alpha"][1] == "published", st


def test_one_queue_at_a_time(q: SimpleNamespace) -> None:
    with open(q.qd / "lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        r = _run(q)
    assert r.returncode == 2 and "another queue holds" in r.stderr
    assert _calls(q, "ship") == []


def test_a_failed_publish_is_loud_and_not_marked_published(q: SimpleNamespace) -> None:
    r = _run(q, FAKE_SHIP_RC="", FAKE_PUBLISH_RC="3")
    assert r.returncode == 1 and "publish_site.sh into" in r.stderr, r.stderr
    assert _status(q)["beta"][1] == "failed-publish"
    assert (q.qd / "published.tsv").read_text() == "" and _calls(q, "firebase") == []


@pytest.mark.parametrize("jobs,mem_gb,together", [("2", "0", True), ("1", "0", False), ("2", "100000", False)])
def test_two_at_a_time_when_the_host_has_room(q: SimpleNamespace, jobs: str, mem_gb: str, together: bool) -> None:
    """The parallel round ships maps close together: up to QUEUE_JOBS
    ship_packs.sh runs at a time, a second one only while memory allows
    (QUEUE_MIN_MEM_GB) and the disk has room; publishing and deploying stay
    one at a time; the order is the order they shipped."""
    r = _run(q, FAKE_SHIP_RC="", FAKE_SHIP_SLEEP="2", QUEUE_JOBS=jobs, QUEUE_MIN_MEM_GB=mem_gb)
    assert r.returncode == 0, r.stderr
    log = [json.loads(x) for x in q.log.read_text().splitlines()]
    t = {(k, c[k]): c["t"] for c in log for k in ("ship", "ship_end") if k in c}
    starts = [c["ship"] for c in log if "ship" in c]
    assert starts[:2] == ["beta", "alpha"], starts
    overlap = t[("ship", "alpha")] < t[("ship_end", "beta")]
    assert overlap is together, (jobs, mem_gb, t)
    assert _status(q)["alpha"][1] == _status(q)["beta"][1] == "published"
    assert len(_calls(q, "firebase")) == len(_calls(q, "publish")) == 4    # beta, alpha, cont, fail: one deploy each


def test_queue_since_counts_what_shipped_before_the_first_start(q: SimpleNamespace) -> None:
    """Started after the round began (Switzerland shipped already): QUEUE_SINCE
    on the first run counts maps shipped since then; a later run keeps it."""
    (q.qd / "since").unlink()
    r = _run(q, FAKE_SHIP_RC="", QUEUE_SINCE=_iso(T0 - 7200))
    assert r.returncode == 0, r.stderr
    assert "old" in [c["ship"] for c in _calls(q, "ship")]
    assert int((q.qd / "since").read_text()) == T0 - 7200


def test_a_refusal_is_no_failure(q: SimpleNamespace) -> None:
    """ship_packs.sh exit 4 or 5 (no packs in v1 for this map; nothing
    publishable): logged and skipped, not a failure."""
    r = _run(q, FAKE_SHIP_RC="cont:4;fail:5")
    assert r.returncode == 0 and "FAILED" not in r.stderr, r.stderr
    st = _status(q)
    assert st["cont"][1] == "refused-4" and st["fail"][1] == "refused-5", st
    log = (q.qd / "queue.log").read_text()
    assert "cont: ship_packs rc=4 (v1 gives this map no packs): skipped" in log
    assert "fail: ship_packs rc=5 (nothing publishable): skipped" in log


def test_an_upload_pending_map_ships_once_archive_org_lists_it(q: SimpleNamespace) -> None:
    """The round's last European uploads ended upload-pending (exit 6) with
    nothing appending `uploaded` later: such a map ships once archive.org
    lists the local file with its MD5; a later `uploaded` row works too."""
    assert _run(q, FAKE_SHIP_RC="").returncode == 0
    assert "delta" not in [c["ship"] for c in _calls(q, "ship")]
    import hashlib
    z = q.maps / "osm-delta-2026-10-23.zim"
    (q.d / "ia" / "streetzim-delta.json").write_text(json.dumps({"files": [
        {"name": z.name, "md5": hashlib.md5(z.read_bytes()).hexdigest(), "size": str(z.stat().st_size)}]}))
    assert _run(q, FAKE_SHIP_RC="").returncode == 0
    assert [c["ship"] for c in _calls(q, "ship")].count("delta") == 1 and _status(q)["delta"][1] == "published"


def test_the_preview_starts_with_what_the_channel_shows(q: SimpleNamespace, tmp_path: Path) -> None:
    """A new preview clone starts with the Swiss test recipes the channel shows
    now (QUEUE_SEED); production's Switzerland replaces them when it ships."""
    site = tmp_path / "swiss-site2"
    (site / "recipes" / "switzerland").mkdir(parents=True)
    (site / "recipes" / "switzerland" / "test-item.json").write_text("{}")
    assert _run(q, FAKE_SHIP_RC="", QUEUE_SEED=f"switzerland={site}").returncode == 0
    sw = q.preview / "web" / "build" / "recipes" / "switzerland"
    assert (sw / "test-item.json").exists() and (q.preview / "web" / "build" / "recipes" / "beta").exists()
    lpzims.write_base(q.maps / "osm-switzerland-2026-10-24.zim", cfg_extra={"tileLanguages": ["de", "rm"]})
    with open(q.tsv, "a") as fh:
        fh.write(f"switzerland\tuploaded\tosm-switzerland-2026-10-24.zim\t{_iso(T0 + 900)}\n")
    assert _run(q, FAKE_SHIP_RC="", QUEUE_SEED=f"switzerland={site}").returncode == 0
    assert (sw / "region.json").exists() and not (sw / "test-item.json").exists()


def test_one_run_at_a_time_while_the_round_builds(q: SimpleNamespace, tmp_path: Path) -> None:
    """While a round driver runs (the disk is saturated) at most one
    ship_packs.sh at a time, whatever QUEUE_JOBS says."""
    rnd = tmp_path / "fake_parallel_rebuild.sh"
    rnd.write_text("sleep 120\n")
    p = subprocess.Popen(["bash", str(rnd)])
    try:
        r = _run(q, FAKE_SHIP_RC="", FAKE_SHIP_SLEEP="2", QUEUE_JOBS="2", QUEUE_ROUND_PATTERNS="fake_parallel_rebuild.sh")
    finally:
        p.kill()
    assert r.returncode == 0, r.stderr
    log = [json.loads(x) for x in q.log.read_text().splitlines()]
    t = {(k, c[k]): c["t"] for c in log for k in ("ship", "ship_end") if k in c}
    assert t[("ship", "alpha")] >= t[("ship_end", "beta")], t
