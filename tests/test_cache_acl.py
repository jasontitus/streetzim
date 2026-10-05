"""Atomic cache updates preserve access rules or leave the old file intact."""
import ctypes
import errno
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

import pytest

from streetzim import cache_permissions as permissions
import wikidata_cache as wc

pytestmark = pytest.mark.skipif(sys.platform not in ("linux", "darwin"),
                                reason="Linux POSIX ACLs or macOS extended ACLs")
ACL_ACCESS = "system.posix_acl_access"
ACL_DEFAULT = "system.posix_acl_default"


def _linux_acl(uid):
    # Linux posix_acl_xattr_header/version2, then tag/permissions/ID entries.
    # Owner rw, named user rw, owning group none, mask rw, everyone else none.
    return struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, mode, ident) for tag, mode, ident in (
            (1, 6, 0xffffffff), (2, 6, uid), (4, 0, 0xffffffff),
            (16, 6, 0xffffffff), (32, 0, 0xffffffff)))


def _set_linux_acl(path, name=ACL_ACCESS, uid=None):
    try:
        os.setxattr(path, name, _linux_acl(uid if uid is not None else os.getuid() + 10000))
    except OSError as error:
        if error.errno == errno.ENOTSUP:
            pytest.skip("fixture filesystem does not support POSIX ACLs")
        raise


def _set_acl(path):
    if sys.platform == "linux":
        _set_linux_acl(path)
    else:
        subprocess.run(["/bin/chmod", "+a", "everyone allow read", str(path)],
                       check=True, capture_output=True)


def _acl_snapshot(path):
    if sys.platform == "linux":
        try:
            return os.getxattr(path, ACL_ACCESS)
        except OSError as error:
            if error.errno == errno.ENODATA:
                return None
            raise
    # ls prints ACL entries independently of the first line's path/size.
    result = subprocess.run(["/bin/ls", "-led", str(path)], check=True,
                            capture_output=True, text=True)
    return tuple(result.stdout.splitlines()[1:])


@pytest.fixture
def integrated(monkeypatch):
    # Test the actual atomic publisher before/after the caller delegates to
    # this helper; failures must retain the original and remove owned stages.
    monkeypatch.setattr(wc, "_preserve_cache_permissions", permissions.preserve_cache_permissions)


def test_atomic_replacement_preserves_acl_and_ownership(tmp_path, integrated):
    path = tmp_path / "11.json"
    path.write_text('{"Q110":{"label":"Original"}}')
    path.chmod(0o640)
    _set_acl(path)
    before = path.stat()
    acl = _acl_snapshot(path)
    assert acl

    wc._write_cache_json(path, {"Q110": {"label": "Updated"}})

    after = path.stat()
    assert _acl_snapshot(path) == acl
    assert (after.st_uid, after.st_gid, after.st_mode & 0o777) == (
        before.st_uid, before.st_gid, before.st_mode & 0o777)
    assert json.loads(path.read_text()) == {"Q110": {"label": "Updated"}}
    assert not list(tmp_path.glob("*.tmp"))


def test_absent_source_acl_removes_inherited_staging_acl(tmp_path, integrated):
    path = tmp_path / "11.json"
    path.write_text('{"Q110":{"label":"Original"}}')
    path.chmod(0o640)
    assert not _acl_snapshot(path)
    if sys.platform == "linux":
        _set_linux_acl(tmp_path, ACL_DEFAULT)
    else:
        subprocess.run(["/bin/chmod", "+a", "everyone allow read,file_inherit", str(tmp_path)],
                       check=True, capture_output=True)
    probe = tmp_path / "inherited"
    probe.write_text("proves the next staging file inherits an ACL")
    assert _acl_snapshot(probe)
    probe.unlink()

    wc._write_cache_json(path, {"Q110": {"label": "Updated"}})

    assert not _acl_snapshot(path)
    assert path.stat().st_mode & 0o777 == 0o640
    assert json.loads(path.read_text()) == {"Q110": {"label": "Updated"}}
    assert not list(tmp_path.glob("*.tmp"))


def test_acl_copy_failure_preserves_original_and_cleans_stage(tmp_path, integrated, monkeypatch):
    path = tmp_path / "11.json"
    path.write_text('{"Q110":{"label":"Original"}}')
    _set_acl(path)
    before = path.read_bytes(), _acl_snapshot(path)

    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "ACL copy denied")

    if sys.platform == "linux":
        monkeypatch.setattr(permissions.os, "setxattr", denied)
    else:
        original = permissions._darwin_acl

        def denied_copy(source, destination=None):
            if destination is not None:
                denied()
            return original(source)

        monkeypatch.setattr(permissions, "_darwin_acl", denied_copy)
    with pytest.raises(PermissionError, match="ACL copy denied"):
        wc._write_cache_json(path, {"Q110": {"label": "Updated"}})
    assert (path.read_bytes(), _acl_snapshot(path)) == before
    assert not list(tmp_path.glob("*.tmp"))


def test_acl_cannot_use_equal_group_other_chown_fallback(tmp_path, integrated, monkeypatch):
    path = tmp_path / "11.json"
    path.write_text('{"Q110":{"label":"Original"}}')
    _set_acl(path)
    # An ACL's mode-group field can be its mask; equal mode fields do not
    # justify changing the owning group's identity while retaining the ACL.
    path.chmod(0o666)
    before = path.read_bytes(), _acl_snapshot(path)
    original_stat = Path.stat

    def foreign_group(candidate, *args, **kwargs):
        stat = original_stat(candidate, *args, **kwargs)
        if candidate == path:
            fields = list(stat)
            fields[5] = stat.st_gid + 10000
            return os.stat_result(fields)
        return stat

    def denied(*args, **kwargs):
        raise PermissionError(errno.EPERM, "ownership change denied")

    monkeypatch.setattr(Path, "stat", foreign_group)
    monkeypatch.setattr(permissions.os, "chown", denied)
    with pytest.raises(PermissionError, match=r"(?s)it has an ACL.*same owner.*"
                                              r"docs/zimfarm\.md"):
        wc._write_cache_json(path, {"Q110": {"label": "Updated"}})
    assert (path.read_bytes(), _acl_snapshot(path)) == before
    assert not list(tmp_path.glob("*.tmp"))


def test_darwin_acl_error_survives_cleanup(tmp_path, monkeypatch):
    class Library:
        def copyfile(self, *args):
            return 1  # source has an ACL

        def acl_get_file(self, *args):
            return 1  # opaque ACL handle

        def acl_set_file(self, *args):
            ctypes.set_errno(errno.EACCES)
            return -1

        def acl_free(self, *args):
            ctypes.set_errno(0)
            return 0

    monkeypatch.setattr(permissions, "_darwin_functions", Library)
    with pytest.raises(PermissionError) as raised:
        permissions._darwin_acl(tmp_path / "source", tmp_path / "stage")
    assert raised.value.errno == errno.EACCES


def _root_acl_cache(base):
    cache = base / "cache"
    cache.mkdir()
    cache.chmod(0o777)
    for name, value in (("11.json", {"Q110": {"label": "Original"}}),
                         ("manifest.json", {"total_entries": 1, "buckets": 1})):
        path = cache / name
        path.write_text(json.dumps(value))
        os.chown(path, 1000, 2000)
        _set_linux_acl(path, uid=1001)
    return cache


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0,
                    reason="needs Linux root to exercise actual UID/ACL restrictions")
@pytest.mark.parametrize("existing_lock", [False, True])
def test_acl_only_nonowner_fails_before_publication(existing_lock):
    with tempfile.TemporaryDirectory(prefix="wikidata-acl-denied-", dir="/tmp") as td:
        base = Path(td)
        base.chmod(0o755)
        cache = _root_acl_cache(base)
        lock = cache / "manifest.json.lock"
        if existing_lock:
            lock.touch()
            lock.chmod(0o666)
        before = {p.name: (p.read_bytes(), _acl_snapshot(p)) for p in cache.glob("*.json")}
        code = """
import json
import os
from pathlib import Path
import sys
import wikidata_cache as wc
from streetzim.cache_permissions import preserve_cache_permissions
cache = Path(sys.argv[1])
assert all(os.access(cache / name, os.R_OK | os.W_OK)
           for name in ('11.json', 'manifest.json'))
wc._preserve_cache_permissions = preserve_cache_permissions
try:
    wc.save_cache(cache, {'Q110': {'label': 'Updated'}})
except PermissionError as error:
    print(json.dumps({'error': str(error)}))
else:
    raise AssertionError('non-owner update unexpectedly succeeded')
"""
        child = subprocess.run(
            [sys.executable, "-c", code, str(cache)], cwd=Path(wc.__file__).resolve().parent,
            user=1001, group=1001, extra_groups=[], umask=0o022,
            capture_output=True, text=True, timeout=15)
        assert child.returncode == 0, child.stdout + child.stderr
        assert "ACL-protected cache" in json.loads(child.stdout.splitlines()[-1])["error"]
        assert {p.name: (p.read_bytes(), _acl_snapshot(p))
                for p in cache.glob("*.json")} == before
        assert lock.exists() == existing_lock
        assert not list(cache.glob("*.tmp"))


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0,
                    reason="needs Linux root to retain another UID's ownership")
def test_privileged_update_preserves_acl_owner_group_and_first_lock(integrated):
    with tempfile.TemporaryDirectory(prefix="wikidata-acl-root-", dir="/tmp") as td:
        cache = _root_acl_cache(Path(td))
        before = {p.name: _acl_snapshot(p) for p in cache.glob("*.json")}
        wc.save_cache(cache, {"Q110": {"label": "Updated"}})
        for name in ("11.json", "manifest.json", "manifest.json.lock"):
            path = cache / name
            assert (path.stat().st_uid, path.stat().st_gid) == (1000, 2000)
            assert _acl_snapshot(path) == before.get(name, before["manifest.json"])
        assert json.loads((cache / "11.json").read_text()) == {"Q110": {"label": "Updated"}}
        assert not list(cache.glob("*.tmp"))


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0,
                    reason="needs Linux root followed by the original unprivileged UID")
@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_privileged_plain_update_preserves_original_owners_next_save(mode, integrated):
    with tempfile.TemporaryDirectory(prefix="wikidata-owner-", dir="/tmp") as td:
        base = Path(td)
        base.chmod(0o755)
        cache = base / "cache"
        cache.mkdir()
        os.chown(cache, 1000, 1000)
        cache.chmod(0o700)
        for name, value in (("11.json", {"Q110": {"label": "Original"}}),
                             ("manifest.json", {"total_entries": 1, "buckets": 1})):
            path = cache / name
            path.write_text(json.dumps(value))
            os.chown(path, 1000, 1000)
            path.chmod(mode)
        wc.save_cache(cache, {"Q110": {"label": "Updated by root"}})
        for name in ("11.json", "manifest.json", "manifest.json.lock"):
            stat = (cache / name).stat()
            assert (stat.st_uid, stat.st_gid, stat.st_mode & 0o777) == (1000, 1000, mode)
        code = """
import sys
import wikidata_cache as wc
from streetzim.cache_permissions import preserve_cache_permissions
wc._preserve_cache_permissions = preserve_cache_permissions
wc.save_cache(sys.argv[1], {'Q111': {'label': 'Updated by original owner'}})
"""
        child = subprocess.run(
            [sys.executable, "-c", code, str(cache)], cwd=Path(wc.__file__).resolve().parent,
            user=1000, group=1000, extra_groups=[], capture_output=True, text=True, timeout=15)
        assert child.returncode == 0, child.stdout + child.stderr
        assert wc.load_cache(cache) == {
            "Q110": {"label": "Updated by root"},
            "Q111": {"label": "Updated by original owner"}}
