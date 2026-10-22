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
    assert "REFUSED x" in r.stderr and "ship_packs.sh" in r.stderr
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
    and, when lifted, runs ship_packs.sh after a shipped upload (the shell order)."""
    s = (ROOT / "ops/cloud/rollout_viewer_patch.sh").read_text()
    i_freeze = s.index("tools/langpack/published_base.py")
    i_patch = s.index('cloud/patch_viewer_inplace.py "$zim"')
    i_upload = s.index("bash cloud/upload_validated.sh")
    i_ship = s.index("bash ops/langpack/ship_packs.sh")
    assert i_freeze < i_patch < i_upload < i_ship
    assert 'row "$id" frozen-published' in s and "LANGPACK_RESHIP_AFTER_PATCH" in s
