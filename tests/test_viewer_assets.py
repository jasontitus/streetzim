"""Pinned viewer assets (streetzim/viewer_assets.py, tools/pin_viewer_assets.py):
the repository's vendored MapLibre matches the lock file, a tampered file or
a wrong hash stops the build, and the cache ignores corrupt entries."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streetzim import tiles, viewer_assets as va  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
import pin_viewer_assets as pin  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("STREETZIM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(va, "BAKED", tmp_path / "baked")


def test_repository_lock_file_checks_out():
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "pin_viewer_assets.py"), "--check"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


def test_lock_covers_the_style_fonts_and_every_range():
    lock = va.load_lock()
    style = (ROOT / "resources" / "viewer" / "index.html").read_text(encoding="utf-8")
    for stack, ranges in lock["fonts"]["ranges"].items():
        assert stack in style
        assert list(ranges) == pin.RANGES


def test_build_uses_the_verified_vendored_maplibre():
    js, css = tiles.vendored_maplibre()
    assert Path(js) == va.VENDOR / "maplibre-gl" / "maplibre-gl.js"
    assert Path(css).name == "maplibre-gl.css"


def _vendor_copy(tmp_path):
    dest = tmp_path / "vendor"
    shutil.copytree(va.VENDOR, dest)
    return dest


def test_tampered_vendored_maplibre_fails(tmp_path):
    vendor = _vendor_copy(tmp_path)
    js = vendor / "maplibre-gl" / "maplibre-gl.js"
    js.write_bytes(js.read_bytes() + b"\n// injected\n")
    with pytest.raises(va.IntegrityError, match=r"maplibre-gl\.js: sha256"):
        va.vendored_maplibre(vendor=vendor)


def test_wrong_hash_in_lock_fails(tmp_path):
    lock = va.load_lock()
    lock["maplibre-gl"]["files"]["maplibre-gl.css"] = "0" * 64
    with pytest.raises(va.IntegrityError, match="lock file says 0000"):
        va.vendored_maplibre(lock, vendor=_vendor_copy(tmp_path))


def test_fetch_verified_rejects_other_content_and_caches_good(monkeypatch):
    good = b"the pinned bytes"
    digest = hashlib.sha256(good).hexdigest()
    served = {"body": b"something else"}
    monkeypatch.setattr(va.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(served["body"]))
    with pytest.raises(va.IntegrityError):
        va.fetch_verified("https://cdn.example/x", digest)
    assert va.cached(digest) is None                   # nothing bad was cached
    served["body"] = good
    assert va.fetch_verified("https://cdn.example/x", digest) == good
    assert va.cached(digest) == good


def test_corrupt_cache_entry_is_ignored(monkeypatch):
    good = b"range bytes"
    digest = hashlib.sha256(good).hexdigest()
    path = va.cache_path(va.cache_dirs()[0], digest)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"bit rot")
    monkeypatch.setattr(va.urllib.request, "urlopen", lambda req, timeout=None: _Resp(good))
    assert va.fetch_verified("https://cdn.example/y", digest) == good
    assert path.read_bytes() == good                   # repaired


def test_baked_cache_is_used_offline(tmp_path, monkeypatch):
    good = b"baked range"
    digest = hashlib.sha256(good).hexdigest()
    va.store(digest, good, root=va.BAKED)

    def offline(req, timeout=None):
        raise OSError("offline")
    monkeypatch.setattr(va.urllib.request, "urlopen", offline)
    assert va.fetch_verified("https://cdn.example/z", digest) == good


def test_npm_integrity_check():
    data = b"tarball"
    ok = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    pin.check_npm_integrity(data, ok)
    with pytest.raises(SystemExit):
        pin.check_npm_integrity(data + b"!", ok)
    with pytest.raises(SystemExit):
        pin.check_npm_integrity(data, "sha1-" + ok[7:])


def test_lock_file_is_what_the_tool_writes(tmp_path):
    """Hand edits show up as a diff against the tool's own formatting."""
    lock = va.load_lock()
    out = tmp_path / "lock.json"
    pin.write_lock(json.loads(json.dumps(lock)), out)
    assert out.read_text(encoding="utf-8") == va.LOCK.read_text(encoding="utf-8")
