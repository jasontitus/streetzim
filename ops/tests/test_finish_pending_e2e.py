"""cloud/finish_pending_uploads.sh end to end: the real finisher,
upload_validated.sh and archive_has_file.py in a scratch project, a stub `ia`
whose listing is a JSON file, and a stub md5sum that records what it reads.

A pending row is complete once archive.org lists the file with ITS md5. The
row's 4th column holds it unless the file changed after the row was written
(patched again in place), the row predates the column, or a newer row for the
same file supersedes it; a row a later build has superseded is dropped before
its file is read.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAME = "osm-x-2026-10-13.zim"

STUB_IA = """#!/bin/sh
echo "ia $*" >> "$IA_LOG"
case "$1" in
  metadata)
    case "$*" in *--modify*) exit 0;; esac
    # Another job (a build queue) appends a row while the finisher runs.
    if [ -n "${IA_APPEND_ROW:-}" ] && [ ! -f "$IA_LOG.appended" ]; then
      printf '%b\n' "$IA_APPEND_ROW" >> "$PENDING_FILE"; touch "$IA_LOG.appended"
    fi
    cat "$IA_LISTING" ;;
  upload) exit 0 ;;
esac
"""

STUB_MD5SUM = """#!/bin/sh
echo "$*" >> "$MD5_LOG"
exec "$REAL_MD5SUM" "$@"
"""


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).astimezone().isoformat(timespec="seconds")


class Proj:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        p = self.p = tmp / "proj"
        (p / "ops" / "cloud").mkdir(parents=True)
        (p / "cloud").mkdir()
        (p / "web").mkdir()
        (p / "tmp").mkdir()
        for f in ("upload_validated.sh", "archive_has_file.py", "finish_pending_uploads.sh"):
            shutil.copy(ROOT / "ops/cloud" / f, p / "ops/cloud" / f)
        for f in ("upload_validated.sh", "finish_pending_uploads.sh"):
            os.symlink(f"../ops/cloud/{f}", p / "cloud" / f)
        for stub in ("cloud/validate_zim.py", "cloud/stamp_item_metadata.py", "cloud/cleanup_old_zims.py"):
            (p / stub).write_text("import sys; sys.exit(0)\n")
        (p / "web/generate.py").write_text("print('deployed')\n")
        b = tmp / "bin"
        b.mkdir()
        (b / "ia").write_text(STUB_IA)
        (b / "md5sum").write_text(STUB_MD5SUM)
        for f in ("ia", "md5sum"):
            (b / f).chmod(0o755)
        self.zim = p / NAME
        self.ia_log = tmp / "ia.log"
        self.md5_log = tmp / "md5.log"
        self.listing = tmp / "listing.json"
        self.pending = p / "pending-uploads.tsv"

    def write_zim(self, payload: bytes, mtime: float | None = None) -> str:
        self.zim.write_bytes(b"ZIM\x04" + payload)
        if mtime is not None:
            os.utime(self.zim, (mtime, mtime))
        return hashlib.md5(self.zim.read_bytes()).hexdigest()

    def list_files(self, *files: tuple[str, str]):
        size = str(self.zim.stat().st_size)
        self.listing.write_text(json.dumps({"files": [{"name": n, "md5": m, "size": size} for n, m in files]}))

    def rows(self, *rows: tuple):
        self.pending.write_text("".join("\t".join(r) + "\n" for r in rows))

    def run(self, **extra):
        env = dict(os.environ, STREETZIM_ROOT=str(self.p), IA=str(self.tmp / "bin/ia"),
                   PYTHON=sys.executable, IA_LOG=str(self.ia_log), IA_LISTING=str(self.listing),
                   MD5_LOG=str(self.md5_log), REAL_MD5SUM=shutil.which("md5sum"),
                   PATH=f"{self.tmp / 'bin'}:{os.environ['PATH']}", TMPDIR=str(self.p / "tmp"),
                   STREETZIM_NO_TORRENT_COMMIT="1", STREETZIM_FINISH_NO_BUILD_WAIT="1",
                   STREETZIM_LISTING_POLL_SECS="3", STREETZIM_LISTING_POLL_INTERVAL="1")
        env.update(extra, PENDING_FILE=str(self.pending))
        env.pop("PROJECT_DIR", None)
        r = subprocess.run(["bash", "cloud/finish_pending_uploads.sh"], cwd=self.p, env=env,
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def uploads(self) -> int:
        lines = self.ia_log.read_text().splitlines() if self.ia_log.exists() else []
        return sum(c.startswith("ia upload") for c in lines)

    def pending_rows(self) -> list[list[str]]:
        return [ln.split("\t") for ln in self.pending.read_text().splitlines() if ln]

    def finish_log(self) -> str:
        f = self.p / "finish-pending.log"
        return f.read_text() if f.exists() else ""

    def md5_reads(self) -> list[str]:
        return self.md5_log.read_text().splitlines() if self.md5_log.exists() else []


def test_a_row_listed_with_its_md5_completes_without_a_transfer(tmp_path):
    P = Proj(tmp_path)
    now = time.time()
    md5 = P.write_zim(os.urandom(4096), mtime=now - 600)
    P.list_files((NAME, md5))
    P.rows(("x", NAME, _iso(now - 60), md5))
    r = P.run()
    assert P.pending_rows() == [], r.stdout
    assert P.uploads() == 0
    assert "DONE x" in P.finish_log()


def test_while_the_previous_file_is_listed_the_row_stays_with_its_md5(tmp_path):
    P = Proj(tmp_path)
    now = time.time()
    md5 = P.write_zim(os.urandom(4096), mtime=now - 600)
    P.list_files((NAME, "0" * 32))
    when = _iso(now + 60)          # the row is newer than the file: trusted
    P.rows(("x", NAME, when, md5))
    r = P.run()
    assert P.pending_rows() == [["x", NAME, when, md5]], r.stdout
    assert P.uploads() == 0
    assert "DONE x" not in P.finish_log()
    assert P.md5_reads() == []     # the row's md5, not a re-read


def test_a_row_without_the_md5_column_completes_on_the_local_md5(tmp_path):
    P = Proj(tmp_path)
    now = time.time()
    md5 = P.write_zim(os.urandom(4096), mtime=now - 600)
    P.list_files((NAME, md5))
    # Newer than the file: only the missing column can make it read the md5.
    P.rows(("x", NAME, _iso(now + 60)))
    r = P.run()
    assert P.pending_rows() == [], r.stdout
    assert P.uploads() == 0
    assert "DONE x" in P.finish_log()


def test_a_file_patched_again_after_its_row_is_checked_by_its_new_md5(tmp_path):
    # S4: the row holds md5 A; the file was patched (B) after the row was
    # written and archive.org now lists B.
    P = Proj(tmp_path)
    now = time.time()
    a = P.write_zim(b"A" * 4096, mtime=now - 7200)
    b = P.write_zim(b"B" * 4096, mtime=now - 60)
    assert a != b
    P.list_files((NAME, b))
    P.rows(("x", NAME, _iso(now - 3600), a))
    r = P.run()
    assert P.pending_rows() == [], r.stdout
    assert P.uploads() == 0
    assert "DONE x" in P.finish_log()


def test_the_newest_row_for_a_file_decides(tmp_path):
    # S3: pending with A, re-uploaded and pending again with B; archive.org
    # lists B. One completion, both rows gone.
    P = Proj(tmp_path)
    now = time.time()
    a = hashlib.md5(b"ZIM\x04" + b"A" * 4096).hexdigest()
    b = P.write_zim(b"B" * 4096, mtime=now - 3000)
    P.list_files((NAME, b))
    # Both rows newer than the file's ctime: only the row order can tell.
    P.rows(("x", NAME, _iso(now + 60), a), ("x", NAME, _iso(now + 120), b))
    r = P.run()
    assert P.pending_rows() == [], r.stdout
    assert P.uploads() == 0
    assert P.finish_log().count("DONE x") == 1


def test_a_superseded_row_is_dropped_without_reading_its_file(tmp_path):
    P = Proj(tmp_path)
    now = time.time()
    P.write_zim(os.urandom(4096), mtime=now - 600)
    P.list_files((NAME, "0" * 32), ("osm-x-2026-10-15.zim", "1" * 32))
    P.rows(("x", NAME, _iso(now - 60)))           # no md5: would need a read
    r = P.run()
    assert P.pending_rows() == [], r.stdout
    assert "superseded by osm-x-2026-10-15.zim" in r.stdout
    assert P.md5_reads() == []
    assert P.uploads() == 0


def test_a_row_appended_during_the_run_beats_this_runs_leftover(tmp_path):
    # The finisher keeps rows other jobs append while it runs; for the same
    # file the appended (newer) row wins over this run's not-yet-listed one.
    P = Proj(tmp_path)
    now = time.time()
    md5 = P.write_zim(os.urandom(4096), mtime=now - 600)
    P.list_files((NAME, "0" * 32))
    old = ("x", NAME, _iso(now - 3600), md5)
    new = ("x", NAME, _iso(now - 30), "2" * 32)
    P.rows(old)
    r = P.run(IA_APPEND_ROW="\\t".join(new))
    assert P.pending_rows() == [list(new)], r.stdout


def test_a_repatch_that_keeps_the_old_mtime_is_still_seen(tmp_path):
    # S4 with touch -r: patched again after the row, the old mtime restored.
    # The ctime still moved, so the row's md5 is not trusted.
    P = Proj(tmp_path)
    now = time.time()
    a = hashlib.md5(b"ZIM\x04" + b"A" * 4096).hexdigest()
    b = P.write_zim(b"B" * 4096, mtime=now - 7200)   # mtime older than the row
    P.list_files((NAME, b))
    P.rows(("x", NAME, _iso(now - 3600), a))
    r = P.run()
    assert P.pending_rows() == [], r.stdout
    assert P.uploads() == 0
    assert "DONE x" in P.finish_log()
