"""The freeze on in-place patches of a base whose language recipes are
published on /build/ (user decision 2026-10-21; plan review B2/H-2):
tools/langpack/published_base.py and upload_validated.sh's step 1b. A
re-upload of such a base with other bytes is refused (exit 7, nothing sent)
unless LANGPACK_RESHIP_AFTER_PATCH=1, which queues the region for
ship_packs.sh in langpack-reship.tsv."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ops.tests.test_archive_has_file import _project  # noqa: E402
from tools.langpack import published_base as pb  # noqa: E402


def _publish(web: Path, region: str, file: str, md5: str):
    d = web / "build" / "recipes" / region
    d.mkdir(parents=True, exist_ok=True)
    (d / "region.json").write_text(json.dumps({"id": region, "base": {"file": file, "size": 1, "md5": md5}, "menu": []}))


def test_published_base(tmp_path):
    web = tmp_path / "web"
    assert pb.published(str(tmp_path / "osm-x-2026-10-13.zim"), str(web)) == []          # nothing published
    _publish(web, "x", "osm-x-2026-10-13.zim", "a" * 32)
    _publish(web, "x-de", "osm-x-2026-10-13.zim", "b" * 32)
    _publish(web, "y", "osm-y-2026-10-13.zim", "a" * 32)
    (web / "build" / "recipes" / "broken").mkdir()
    assert pb.published("/somewhere/osm-x-2026-10-13.zim", str(web)) == ["x", "x-de"]
    assert pb.published("osm-x-2026-10-13.zim", str(web), "a" * 32) == ["x-de"]          # x: unchanged bytes
    assert pb.main(["osm-x-2026-10-13.zim", "--web", str(web)]) == 3
    assert pb.main(["osm-z-2026-10-13.zim", "--web", str(web)]) == 0
    assert pb.main(["osm-y-2026-10-13.zim", "--web", str(web), "--md5", "a" * 32]) == 0
    assert pb.main(["osm-y-2026-10-13.zim", "--web", str(web), "--md5", "nope"]) == 2
    # nothing it cannot positively match blocks, and nothing raises
    for i, doc in enumerate(['{"base": null}', '"x"', "[1]", '{"base": {"file": 5}}', "{bad", '{"base": "osm-x-2026-10-13.zim"}']):
        d = web / "build" / "recipes" / f"junk{i}"
        d.mkdir()
        (d / "region.json").write_text(doc)
    assert pb.published("osm-x-2026-10-13.zim", str(web)) == ["x", "x-de"]
    _publish(web, "nomd5", "osm-n-2026-10-13.zim", "a" * 32)
    (web / "build" / "recipes" / "nomd5" / "region.json").write_text(json.dumps({"base": {"file": "osm-n-2026-10-13.zim"}}))
    assert pb.published("osm-n-2026-10-13.zim", str(web)) == ["nomd5"]                     # published: frozen for patching
    assert pb.published("osm-n-2026-10-13.zim", str(web), "c" * 32) == []                  # md5 unknown: no match
    rd = web / "build" / "recipes"
    rd.chmod(0o000)
    try:
        assert pb.published("osm-x-2026-10-13.zim", str(web)) == []
        assert pb.main(["osm-x-2026-10-13.zim", "--web", str(web)]) == 0
    finally:
        rd.chmod(0o755)


def _run(tmp_path, publish_md5: str | None, extra: dict | None = None):
    p = _project(tmp_path)
    (p / "tools" / "langpack").mkdir(parents=True)
    shutil.copy(ROOT / "tools/langpack/published_base.py", p / "tools/langpack/published_base.py")
    zim = p / "osm-x-2026-10-13.zim"
    zim.write_bytes(b"ZIM\x04" + os.urandom(4096))
    md5 = hashlib.md5(zim.read_bytes()).hexdigest()
    if publish_md5 is not None:
        _publish(p / "web", "x", zim.name, md5 if publish_md5 == "same" else publish_md5)
    log = tmp_path / "ia.log"
    env = dict(os.environ, PROJECT_DIR=str(p), IA=str(tmp_path / "bin/ia"), PYTHON=sys.executable,
               IA_LOG=str(log), IA_LISTED="1", ZIM=zim.name, ZIM_SIZE=str(zim.stat().st_size),
               IA_MD5="0" * 32, IA_MD5_AFTER=md5, IA_FLIP_AFTER="1",
               PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", STREETZIM_NO_TORRENT_COMMIT="1",
               STREETZIM_LISTING_POLL_SECS="5", STREETZIM_LISTING_POLL_INTERVAL="1", **(extra or {}))
    if not extra:
        env.pop("LANGPACK_RESHIP_AFTER_PATCH", None)
    r = subprocess.run(["bash", "cloud/upload_validated.sh", "x", zim.name], cwd=p, env=env,
                       capture_output=True, text=True, timeout=180)
    calls = log.read_text().splitlines() if log.exists() else []
    return r, calls, p


def test_a_changed_published_base_is_refused(tmp_path):
    r, calls, p = _run(tmp_path, "f" * 32)
    assert r.returncode == 7, r.stdout + r.stderr
    assert "REFUSED x: frozen: published base" in r.stderr and "reship_queue.sh" in r.stderr
    assert not any(c.startswith("ia upload") for c in calls)
    assert not (p / "langpack-reship.tsv").exists()


@pytest.mark.parametrize("publish_md5", [None, "same"])
def test_an_unpublished_or_unchanged_base_uploads(tmp_path, publish_md5):
    r, calls, p = _run(tmp_path, publish_md5)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sum(c.startswith("ia upload") for c in calls) == 1
    assert not (p / "langpack-reship.tsv").exists()


def test_reship_after_patch_lifts_the_freeze_and_queues_the_region(tmp_path):
    r, calls, p = _run(tmp_path, "f" * 32, {"LANGPACK_RESHIP_AFTER_PATCH": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert sum(c.startswith("ia upload") for c in calls) == 1
    row = (p / "langpack-reship.tsv").read_text().split("\t")
    assert row[0] == "x" and row[1] == "osm-x-2026-10-13.zim"
    assert "queued in langpack-reship.tsv" in r.stdout


def test_the_rollout_freezes_published_bases_before_patching():
    """rollout_viewer_patch.sh asks published_base.py before patch_viewer_inplace.py
    (a file it then could not upload is never patched), skips a region whose
    check fails without calling it frozen, and queues the re-ship instead of
    running ship_packs.sh inline (hours per region)."""
    s = (ROOT / "ops/cloud/rollout_viewer_patch.sh").read_text()
    i_freeze = s.index("tools/langpack/published_base.py")
    i_patch = s.index('cloud/patch_viewer_inplace.py "$zim"')
    i_upload = s.index("bash cloud/upload_validated.sh")
    assert i_freeze < i_patch < i_upload
    assert 'row "$id" frozen-published' in s and 'row "$id" freeze-check-failed' in s
    assert "bash ops/langpack/ship_packs.sh" not in s and "reship_queue.sh" in s[i_upload:]


# ---- the review's adversarial cases (adv3/test_upload_freeze_adv.py): block only on a positive match ----

def _setup(tmp_path):
    p = _project(tmp_path)
    (p / "tools" / "langpack").mkdir(parents=True)
    shutil.copy(ROOT / "tools/langpack/published_base.py", p / "tools/langpack/published_base.py")
    zim = p / "osm-x-2026-10-22.zim"
    zim.write_bytes(b"ZIM\x04" + os.urandom(4096))
    return p, zim, hashlib.md5(zim.read_bytes()).hexdigest()


def _pub(p, region, doc):
    d = p / "web" / "build" / "recipes" / region
    d.mkdir(parents=True, exist_ok=True)
    (d / "region.json").write_text(doc if isinstance(doc, str) else json.dumps(doc))


def _up(tmp_path, p, zim, md5, listed_md5=None):
    log = tmp_path / "ia.log"
    env = dict(os.environ, PROJECT_DIR=str(p), IA=str(tmp_path / "bin/ia"), PYTHON=sys.executable,
               IA_LOG=str(log), IA_LISTED="1", ZIM=zim.name, ZIM_SIZE=str(zim.stat().st_size),
               IA_MD5=listed_md5 or "0" * 32, IA_MD5_AFTER=md5, IA_FLIP_AFTER="1",
               PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", STREETZIM_NO_TORRENT_COMMIT="1",
               STREETZIM_LISTING_POLL_SECS="5", STREETZIM_LISTING_POLL_INTERVAL="1")
    env.pop("LANGPACK_RESHIP_AFTER_PATCH", None)
    r = subprocess.run(["bash", "cloud/upload_validated.sh", "x", zim.name], cwd=p, env=env,
                       capture_output=True, text=True, timeout=180)
    return r, (log.read_text().splitlines() if log.exists() else [])


def test_adv_no_recipes_folder(tmp_path):
    p, zim, md5 = _setup(tmp_path)
    shutil.rmtree(p / "web")
    (p / "web").mkdir()
    (p / "web/generate.py").write_text("print('deployed')\n")
    assert _up(tmp_path, p, zim, md5)[0].returncode == 0


def test_adv_recipes_of_another_file(tmp_path):
    p, zim, md5 = _setup(tmp_path)
    _pub(p, "x", {"id": "x", "base": {"file": "osm-x-2026-10-15.zim", "size": 1, "md5": "a" * 32}, "menu": []})
    (p / "web/build/recipes/index.json").write_text("{}")
    assert _up(tmp_path, p, zim, md5)[0].returncode == 0


@pytest.mark.parametrize("doc", ['{"id": "y", "base": null}', '{"id": "y", "base": "osm-y.zim"}', "[1, 2]",
                                 '{"base": {"file": 5}}', "{not json"])
def test_adv_a_malformed_region_json_does_not_block(tmp_path, doc):
    p, zim, md5 = _setup(tmp_path)
    _pub(p, "y", doc)
    r, _ = _up(tmp_path, p, zim, md5)
    assert r.returncode == 0, r.stderr[-400:]


def test_adv_an_unreadable_recipes_folder_does_not_block(tmp_path):
    p, zim, md5 = _setup(tmp_path)
    _pub(p, "y", {"id": "y", "base": {"file": "osm-y.zim", "md5": "a" * 32}, "menu": []})
    rd = p / "web/build/recipes"
    rd.chmod(0o000)
    try:
        r, _ = _up(tmp_path, p, zim, md5)
    finally:
        rd.chmod(0o755)
    assert r.returncode == 0, r.stderr[-400:]


def test_adv_a_finisher_for_bytes_already_on_archive_org(tmp_path):
    """The re-upload already happened: archive.org lists the local md5, nothing
    would be sent, so nothing is refused (the finisher completes torrent/prune/deploy)."""
    p, zim, md5 = _setup(tmp_path)
    _pub(p, "x", {"id": "x", "base": {"file": zim.name, "size": 1, "md5": "f" * 32}, "menu": []})
    r, calls = _up(tmp_path, p, zim, md5, listed_md5=md5)
    assert not any(c.startswith("ia upload") for c in calls)
    assert r.returncode == 0, r.stderr[-400:]


def test_adv_a_region_json_without_the_base_md5(tmp_path):
    p, zim, md5 = _setup(tmp_path)
    _pub(p, "x", {"id": "x", "base": {"file": zim.name, "size": 1}, "menu": []})
    r, calls = _up(tmp_path, p, zim, md5)                                # bytes are sent: unknown md5 is no match
    assert r.returncode == 0, r.stderr[-400:]
    assert sum(c.startswith("ia upload") for c in calls) == 1


def test_a_checker_crash_warns_and_uploads(tmp_path):
    p, zim, md5 = _setup(tmp_path)
    (p / "tools/langpack/published_base.py").write_text("raise SystemExit(1)\n")
    r, calls = _up(tmp_path, p, zim, md5)
    assert r.returncode == 0, r.stderr[-400:]
    assert "WARNING x: published_base.py failed (rc 1)" in r.stderr
    assert sum(c.startswith("ia upload") for c in calls) == 1


# ---- ops/langpack/reship_queue.sh ----

def test_the_reship_queue(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "osm-x-2026-10-22.zim").write_bytes(b"x")
    (root / "osm-y-2026-10-22.zim").write_bytes(b"y")
    q = root / "langpack-reship.tsv"
    q.write_text("x\tosm-x-2026-10-21.zim\tt1\ny\tosm-y-2026-10-22.zim\tt2\nx\tosm-x-2026-10-22.zim\tt3\n"
                 "bad id\tz.zim\tt4\n")
    fake = tmp_path / "ship.sh"
    log = tmp_path / "ship.log"
    fake.write_text(f'echo "$1 $(basename "$2")" >> {log}\n[ "$1" = y ] && exit 6\nexit 0\n')
    env = dict(os.environ, SHIP_PACKS=str(fake))
    r = subprocess.run(["bash", str(ROOT / "ops/langpack/reship_queue.sh"), str(root)], env=env,
                       capture_output=True, text=True, timeout=60)
    assert log.read_text().splitlines() == ["x osm-x-2026-10-22.zim", "y osm-y-2026-10-22.zim"]   # x once, its latest base
    assert r.returncode == 1 and "still queued:" in r.stdout and "skipping a bad region id" in r.stdout
    rows = [ln.split("\t")[0] for ln in q.read_text().splitlines()]
    assert rows == ["y", "bad id"], rows                                # x done; y kept (6); the bad row stays visible
    # a row queued while the runner works stays for the next run, even for a region it just did
    fake.write_text(f'echo "$1" >> {log}\nprintf "y\\tosm-y-2026-10-22.zim\\tlater\\n" >> {q}\nexit 0\n')
    q.write_text("y\tosm-y-2026-10-22.zim\tt2\n")
    r = subprocess.run(["bash", str(ROOT / "ops/langpack/reship_queue.sh"), str(root)], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and q.read_text().splitlines() == ["y\tosm-y-2026-10-22.zim\tlater"]
    fake.write_text(f'echo "$1" >> {log}\nexit 0\n')
    q.write_text("y\tosm-y-2026-10-22.zim\tt2\n")
    r = subprocess.run(["bash", str(ROOT / "ops/langpack/reship_queue.sh"), str(root)], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "queue empty" in r.stdout and q.read_text() == ""
