"""Shared-cache writers must retain group access after atomic publication."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

import wikidata_cache as wc


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0,
                    reason="needs Linux root to exercise two real writer UIDs")
@pytest.mark.parametrize("setgid", [False, True])
def test_shared_cache_remains_writable_by_second_uid(setgid):
    # Use our own top-level /tmp directory: pytest's private parent directory
    # intentionally cannot be traversed by the other UIDs.
    with tempfile.TemporaryDirectory(prefix="wikidata-shared-permissions-", dir="/tmp") as root:
        base = Path(root)
        base.chmod(0o755)
        cache = base / "cache"
        cache.mkdir()
        os.chown(cache, 0, 2000)
        cache.chmod(0o2770 if setgid else 0o777)
        for name, data in (("11.json", {"Q110": {"label": "Original"}}),
                           ("manifest.json", {"total_entries": 1, "buckets": 1})):
            path = cache / name
            path.write_text(json.dumps(data))
            os.chown(path, 0, 2000)
            path.chmod(0o660)
        inode = None
        for uid in (1000, 1001):
            code = """
import sys
import wikidata_cache as wc
wc.save_cache(sys.argv[1], {sys.argv[2]: {'label': 'Shared fact'}})
"""
            result = subprocess.run(
                [sys.executable, "-c", code, str(cache), f"Q{uid + 100}"],
                cwd=Path(wc.__file__).resolve().parent,
                user=uid, group=uid, extra_groups=[2000], umask=0o022,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15)
            assert result.returncode == 0, result.stdout
            for name in ("11.json", "manifest.json", "manifest.json.lock"):
                stat = (cache / name).stat()
                assert stat.st_gid == 2000, name
                assert stat.st_mode & 0o777 == 0o660, name
            current = (cache / "manifest.json.lock").stat().st_ino
            assert inode is None or current == inode  # no lock-inode replacement
            inode = current
        assert set(wc.load_cache(cache)) == {"Q110", "Q1100", "Q1101"}


def test_lock_is_accessible_before_publication_and_keeps_winner(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    manifest.chmod(0o660)
    lock = tmp_path / "manifest.json.lock"
    winner = None

    def concurrent_publish(source, destination):
        nonlocal winner
        assert source.stat().st_mode & 0o777 == 0o660
        assert source.stat().st_gid == manifest.stat().st_gid
        assert destination == lock
        lock.write_text("another writer won")
        winner = lock.stat().st_ino
        raise FileExistsError("concurrent creator")

    monkeypatch.setattr(wc.os, "link", concurrent_publish)
    wc._prepare_cache_lock(tmp_path)
    wc._prepare_cache_lock(tmp_path)
    assert lock.stat().st_ino == winner
    assert lock.read_text() == "another writer won"
    assert not list(tmp_path.glob(".*.tmp"))


def test_group_preservation_failure_keeps_published_json(tmp_path, monkeypatch):
    path = tmp_path / "11.json"
    path.write_text('{"Q110":{"label":"original"}}')
    path.chmod(0o660)
    before = path.read_bytes()
    original_stat = Path.stat

    def stat_with_different_group(self, *args, **kwargs):
        result = original_stat(self, *args, **kwargs)
        if self == path:
            fields = list(result)
            fields[5] += 1  # st_gid
            return os.stat_result(fields)
        return result

    def cannot_change_group(*args, **kwargs):
        raise PermissionError("cannot preserve shared group")

    monkeypatch.setattr(Path, "stat", stat_with_different_group)
    monkeypatch.setattr(wc.os, "chown", cannot_change_group)
    # The error says why, and how to fix it.
    with pytest.raises(PermissionError, match=r"(?s)is not in that group.*--group-add.*"
                                              r"--wikidata-cache.*docs/zimfarm\.md"):
        wc._write_cache_json(path, {"Q111": {"label": "new"}})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0,
                    reason="needs Linux root to exercise an unprivileged writer")
@pytest.mark.parametrize("existing_lock", [False, True])
def test_world_writable_cache_does_not_require_group_membership(existing_lock):
    with tempfile.TemporaryDirectory(prefix="wikidata-world-permissions-", dir="/tmp") as root:
        cache = Path(root)
        cache.chmod(0o777)
        for name, data in (("11.json", {"Q110": {"label": "Original"}}),
                           ("manifest.json", {"total_entries": 1, "buckets": 1})):
            path = cache / name
            path.write_text(json.dumps(data))
            path.chmod(0o666)
        if existing_lock:
            lock = cache / "manifest.json.lock"
            lock.touch()
            lock.chmod(0o666)
        result = subprocess.run(
            [sys.executable, "-c", "import sys, wikidata_cache as w; "
             "w.save_cache(sys.argv[1], {'Q111': {'label': 'New'}})", str(cache)],
            cwd=Path(wc.__file__).resolve().parent,
            user=1000, group=1000, extra_groups=[], umask=0o022,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15)
        assert result.returncode == 0, result.stdout
        assert set(wc.load_cache(cache)) == {"Q110", "Q111"}


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0,
                    reason="needs Linux root to check an unrelated reader's access")
def test_private_bucket_staging_is_private_before_first_write(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="wikidata-private-stage-", dir="/tmp") as root:
        cache = Path(root)
        cache.chmod(0o755)
        path = cache / "11.json"
        path.write_text("{}")
        path.chmod(0o600)
        dump = wc.json.dump
        permissions = wc._preserve_cache_permissions

        def check_reader(stage):
            result = subprocess.run(
                [sys.executable, "-c", "from pathlib import Path; import sys; "
                 "Path(sys.argv[1]).read_bytes()", str(stage)],
                user=1001, group=1001, extra_groups=[],
                capture_output=True, text=True, timeout=15)
            assert result.returncode != 0 and "PermissionError" in result.stderr

        def check_at_creation(staging, previous):
            # Even opening an EMPTY stage before chmod is unsafe: that FD
            # could read data after chmod. Check immediately after open.
            check_reader(staging)
            return permissions(staging, previous)

        def check_reader_then_dump(value, stream, **kwargs):
            check_reader(stream.name)
            return dump(value, stream, **kwargs)

        monkeypatch.setattr(wc, "_preserve_cache_permissions", check_at_creation)
        monkeypatch.setattr(wc.json, "dump", check_reader_then_dump)
        wc._write_cache_json(path, {"Q110": {"label": "Private cache fixture"}})
        assert json.loads(path.read_text())["Q110"]["label"] == "Private cache fixture"
