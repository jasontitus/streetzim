"""upload_validated.sh does not send a ZIM archive.org already holds
(ops/cloud/archive_has_file.py). `ia upload --checksum` skips a file only when
the item has no pending task, and a queued derive can sit for a day, so a
rerun to finish a "listing pending" upload used to re-send the whole file.

The helper's rule, then the real script against stub ia / validator / site
commands: a listed file with the same MD5 is not uploaded and the rest of
the steps run; another MD5 is uploaded.
"""
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
sys.path.insert(0, str(ROOT / "ops" / "cloud"))
import archive_has_file as A  # noqa: E402

MD5 = "0123456789abcdef0123456789abcdef"


def test_the_rule():
    meta = {"files": [{"name": "a.zim", "md5": MD5}, {"name": "b.zim", "md5": "f" * 32}]}
    assert A.has_file(meta, "a.zim", MD5)
    assert A.has_file(meta, "a.zim", MD5.upper())
    assert not A.has_file(meta, "a.zim", "f" * 32)          # another MD5
    assert not A.has_file(meta, "c.zim", MD5)               # not listed
    assert not A.has_file({}, "a.zim", MD5)                 # no such item ({} from ia)
    assert not A.has_file({"files": "x"}, "a.zim", MD5)
    assert not A.has_file(meta, "a.zim", "not-an-md5")
    assert not A.has_file([], "a.zim", MD5)
    # a backup copy archive.org keeps under history/ is not the file
    hist = {"files": [{"name": "history/files/a.zim.~1~", "md5": MD5}]}
    assert not A.has_file(hist, "a.zim", MD5)


@pytest.mark.parametrize("stdin, rc", [
    (json.dumps({"files": [{"name": "a.zim", "md5": MD5}]}), 0),
    (json.dumps({"files": [{"name": "a.zim", "md5": "e" * 32}]}), 1),
    ("not json", 1),
    ("", 1),
])
def test_the_command(stdin, rc):
    r = subprocess.run([sys.executable, str(ROOT / "ops/cloud/archive_has_file.py"), "a.zim", MD5],
                       input=stdin, capture_output=True, text=True)
    assert r.returncode == rc
    assert subprocess.run([sys.executable, str(ROOT / "ops/cloud/archive_has_file.py")],
                          capture_output=True).returncode == 2


STUB_IA = """#!/bin/sh
echo "ia $*" >> "$IA_LOG"
case "$1" in
  metadata)
    case "$*" in *--modify*) exit 0;; esac
    if [ -n "$IA_LISTED" ]; then
      md5="$IA_MD5"
      # archive.org's task for an upload runs a while after it: the new file
      # is listed from the IA_FLIP_AFTER-th metadata read after the upload.
      if [ -n "${IA_MD5_AFTER:-}" ] && [ -f "$IA_LOG.uploaded" ]; then
        n=$(( $(cat "$IA_LOG.after" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$IA_LOG.after"
        [ "$n" -ge "${IA_FLIP_AFTER:-1}" ] && md5="$IA_MD5_AFTER"
      fi
      printf '{"files":[{"name":"%s","md5":"%s","size":"%s"}]}' "$ZIM" "$md5" "$ZIM_SIZE"
    else printf '{}'; fi ;;
  upload) touch "$IA_LOG.uploaded"; exit 0 ;;
esac
"""


def _project(tmp: Path) -> Path:
    """A PROJECT_DIR with the real upload script and helper, stub steps."""
    p = tmp / "proj"
    (p / "ops" / "cloud").mkdir(parents=True)
    (p / "cloud").mkdir()
    (p / "web").mkdir()
    shutil.copy(ROOT / "ops/cloud/upload_validated.sh", p / "ops/cloud/upload_validated.sh")
    shutil.copy(ROOT / "ops/cloud/archive_has_file.py", p / "ops/cloud/archive_has_file.py")
    os.symlink("../ops/cloud/upload_validated.sh", p / "cloud/upload_validated.sh")
    for stub in ("cloud/validate_zim.py", "cloud/stamp_item_metadata.py", "cloud/cleanup_old_zims.py"):
        (p / stub).write_text("import sys; sys.exit(0)\n")
    (p / "web/generate.py").write_text("print('deployed')\n")
    bin_ = tmp / "bin"
    bin_.mkdir()
    (bin_ / "ia").write_text(STUB_IA)
    (bin_ / "ia").chmod(0o755)
    return p


@pytest.mark.parametrize("listed_md5, uploads", [("same", 0), ("other", 1)])
def test_the_script_sends_only_what_archive_org_lacks(tmp_path, listed_md5, uploads):
    # "other": the previous file under this name; archive.org lists the new
    # one from the 4th read after the upload (IA_FLIP_AFTER): the script must
    # keep polling for its md5, not stop at the name (listed at once) and
    # then find the previous file.
    p = _project(tmp_path)
    zim = p / "osm-x-2026-10-13.zim"
    zim.write_bytes(b"ZIM\x04" + os.urandom(4096))
    md5 = hashlib.md5(zim.read_bytes()).hexdigest()
    log = tmp_path / "ia.log"
    env = dict(os.environ, PROJECT_DIR=str(p), IA=str(tmp_path / "bin/ia"), PYTHON=sys.executable,
               IA_LOG=str(log), IA_LISTED="1", ZIM=zim.name, ZIM_SIZE=str(zim.stat().st_size),
               IA_MD5=md5 if listed_md5 == "same" else "0" * 32, IA_MD5_AFTER=md5, IA_FLIP_AFTER="4",
               PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", STREETZIM_NO_TORRENT_COMMIT="1",
               STREETZIM_LISTING_POLL_SECS="5", STREETZIM_LISTING_POLL_INTERVAL="1")
    r = subprocess.run(["bash", "cloud/upload_validated.sh", "x", zim.name], cwd=p, env=env,
                       capture_output=True, text=True, timeout=180)
    calls = log.read_text().splitlines()
    assert sum(c.startswith("ia upload") for c in calls) == uploads, (calls, r.stdout, r.stderr)
    assert r.returncode == 0 and "DONE x" in r.stdout, r.stdout + r.stderr
    assert f"metadata lists {zim.name} with md5 {md5}" in r.stdout, r.stdout
    if uploads == 0:
        assert "not sent again" in r.stdout


def test_a_same_named_reupload_is_pending_while_the_previous_file_is_listed(tmp_path):
    """A re-upload under the same name and size (a viewer patch): archive.org
    keeps listing the previous file (another md5) until its task runs. That
    is "listing pending" (exit 6, no prune, no deploy, a pending row), not a
    shipped upload."""
    p = _project(tmp_path)
    zim = p / "osm-x-2026-10-13.zim"
    zim.write_bytes(b"ZIM\x04" + os.urandom(4096))
    log = tmp_path / "ia.log"
    env = dict(os.environ, PROJECT_DIR=str(p), IA=str(tmp_path / "bin/ia"), PYTHON=sys.executable,
               IA_LOG=str(log), IA_LISTED="1", ZIM=zim.name, ZIM_SIZE=str(zim.stat().st_size),
               IA_MD5="0" * 32,
               PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", STREETZIM_NO_TORRENT_COMMIT="1",
               STREETZIM_LISTING_POLL_SECS="3", STREETZIM_LISTING_POLL_INTERVAL="1")
    r = subprocess.run(["bash", "cloud/upload_validated.sh", "x", zim.name], cwd=p, env=env,
                       capture_output=True, text=True, timeout=180)
    calls = log.read_text().splitlines()
    assert sum(c.startswith("ia upload") for c in calls) == 1, (calls, r.stdout, r.stderr)
    assert r.returncode == 6, r.stdout + r.stderr
    assert "still lists the previous" in r.stdout
    assert "deployed" not in r.stdout and "DONE x" not in r.stdout
    row = (p / "pending-uploads.tsv").read_text().rstrip("\n").split("\t")
    assert row[0] == "x" and row[1] == zim.name
    assert row[3] == hashlib.md5(zim.read_bytes()).hexdigest()   # what the finisher waits for


def test_no_md5_no_upload(tmp_path):
    """Without the local md5 the upload can never be confirmed: stop before
    sending."""
    p = _project(tmp_path)
    zim = p / "osm-x-2026-10-13.zim"
    zim.write_bytes(b"ZIM\x04" + os.urandom(4096))
    log = tmp_path / "ia.log"
    (tmp_path / "bin" / "md5sum").write_text("#!/bin/sh\nexit 1\n")
    (tmp_path / "bin" / "md5sum").chmod(0o755)
    env = dict(os.environ, PROJECT_DIR=str(p), IA=str(tmp_path / "bin/ia"), PYTHON=sys.executable,
               IA_LOG=str(log), ZIM=zim.name, ZIM_SIZE=str(zim.stat().st_size), IA_MD5="0" * 32,
               PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", STREETZIM_NO_TORRENT_COMMIT="1")
    r = subprocess.run(["bash", "cloud/upload_validated.sh", "x", zim.name], cwd=p, env=env,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 3 and "no md5" in r.stderr, r.stdout + r.stderr
    assert not any(c.startswith("ia upload") for c in (log.read_text().splitlines() if log.exists() else []))
