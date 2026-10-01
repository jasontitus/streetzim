"""Manifest transport, lifecycle, and real Python/native round trips."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud import manifest_writer as mw  # noqa: E402


def item(data=None, file=None, mime="application/octet-stream", front=False):
    return SimpleNamespace(_path="body", _title="Body", _mimetype=mime,
                           _data=data, _file_path=file, _is_front=front)


@pytest.fixture
def staging(monkeypatch):
    monkeypatch.setenv("STREETZIM_MANIFEST_ZSTD", "0")
    monkeypatch.setattr(mw.ManifestCreator, "_run_packer", lambda self: None)


def test_large_in_memory_bodies_stage_without_base64(tmp_path, monkeypatch, staging):
    monkeypatch.setattr(mw, "_STREAMING_THRESHOLD", 1024)
    creator = mw.ManifestCreator(str(tmp_path / "out.zim"), keep_stage=True)
    data = bytes(range(256)) * 4
    with creator:
        creator.add_item(item(data))
        creator.add_item(item(data[::-1]))
    records = list(mw.iter_records(str(creator._manifest_path)))
    for record, payload in zip(records[1:], [data, data[::-1]], strict=True):
        assert record["streaming"] and record["size"] == len(payload)
        assert Path(record["file"]).is_absolute()
        assert Path(record["file"]).read_bytes() == payload
        assert "body_b64" not in record and "content" not in record
    assert records[1]["file"] != records[2]["file"]


def test_threshold_keeps_small_binary_inline(tmp_path, monkeypatch, staging):
    monkeypatch.setattr(mw, "_STREAMING_THRESHOLD", 1024)
    creator = mw.ManifestCreator(str(tmp_path / "out.zim"), keep_stage=True)
    with creator:
        creator.add_item(item(b"x" * 1023))
        creator.add_item(item("東京".encode(), mime="text/plain"))
        creator.add_item(item(b"\xff", mime="text/plain"))
    _, small, text, invalid = mw.iter_records(str(creator._manifest_path))
    assert base64.b64decode(small["body_b64"]) == b"x" * 1023
    assert text["content"] == "東京"
    assert base64.b64decode(invalid["body_b64"]) == b"\xff"


def test_stages_are_unique_and_cleanup_after_success(tmp_path, staging):
    first = mw.ManifestCreator(str(tmp_path / "out.zim"))
    second = mw.ManifestCreator(str(tmp_path / "out.zim"))
    assert first._stage_dir != second._stage_dir
    with first:
        first.add_item(item(b"one"))
    assert not first._stage_dir.exists() and second._stage_dir.exists()
    with second:
        second.add_item(item(b"two"))
    assert not second._stage_dir.exists()


def test_failure_preserves_manifest_and_large_body(tmp_path, monkeypatch, staging):
    monkeypatch.setattr(mw, "_STREAMING_THRESHOLD", 4)
    creator = mw.ManifestCreator(str(tmp_path / "out.zim"))
    with pytest.raises(ValueError, match="interrupted producer"), creator:
        creator.add_item(item(b"large"))
        raise ValueError("interrupted producer")
    record = list(mw.iter_records(str(creator._manifest_path)))[1]
    assert Path(record["file"]).read_bytes() == b"large"


def fake_packer(tmp_path, monkeypatch, exit_code=0):
    path = tmp_path / "pack"
    path.write_text(
        f"#!{sys.executable}\nimport json,sys\n"
        + (f"sys.exit({exit_code})\n" if exit_code else
           "open(sys.argv[2], 'w').write(json.dumps(sys.argv[3:]))\n"))
    path.chmod(0o755)
    monkeypatch.setenv("STREETZIM_PACK_BIN", str(path))
    monkeypatch.setenv("STREETZIM_MANIFEST_ZSTD", "0")


@pytest.mark.parametrize("workers", [None, 1, 3])
def test_only_requested_workers_are_passed(tmp_path, monkeypatch, workers):
    fake_packer(tmp_path, monkeypatch)
    monkeypatch.setenv("RAYON_NUM_THREADS", "7")
    out = tmp_path / "out.zim"
    creator = mw.ManifestCreator(str(out))
    if workers is not None:
        creator.config_nbworkers(workers)
    with creator:
        creator.add_item(item(b"payload"))
    assert json.loads(out.read_text()) == ([] if workers is None else
                                          ["--threads", str(workers)])
    assert os.environ["RAYON_NUM_THREADS"] == "7"


def test_packer_failure_does_not_remove_existing_output(tmp_path, monkeypatch):
    fake_packer(tmp_path, monkeypatch, exit_code=2)
    out = tmp_path / "out.zim"
    out.write_bytes(b"previous archive")
    creator = mw.ManifestCreator(str(out))
    with pytest.raises(RuntimeError, match="exit 2"), creator:
        creator.add_item(item(b"payload"))
    assert out.read_bytes() == b"previous archive"
    assert creator._manifest_path.exists()


def test_worker_count_must_be_positive(tmp_path, staging):
    creator = mw.ManifestCreator(str(tmp_path / "out.zim"))
    with creator:
        for n in (0, -1):
            with pytest.raises(ValueError, match="positive"):
                creator.config_nbworkers(n)


def test_requested_libzim_indexing_is_rejected(tmp_path, staging):
    creator = mw.ManifestCreator(str(tmp_path / "out.zim"))
    with pytest.raises(ValueError, match="--xapian=builder"):
        creator.config_indexing(True, "en")
    creator.config_indexing(False, "en")
    with creator:
        pass


@pytest.mark.parametrize("exists", [False, True])
def test_explicit_packer_must_be_an_executable(tmp_path, monkeypatch, exists):
    path = tmp_path / "packer"
    if exists:
        path.write_text("not executable")
    monkeypatch.setenv("STREETZIM_PACK_BIN", str(path))
    with pytest.raises(RuntimeError, match="not an executable file"):
        mw.resolve_pack_binary()


def test_explicit_executable_is_resolved_to_absolute_path(tmp_path, monkeypatch):
    path = tmp_path / "packer"
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STREETZIM_PACK_BIN", "packer")
    assert mw.resolve_pack_binary() == str(path)
    assert mw._resolve_pack_binary() == str(path)


@pytest.fixture
def real_packer(monkeypatch):
    binary = os.environ.get("STREETZIM_TEST_PACK_BIN")
    if binary:
        monkeypatch.setenv("STREETZIM_PACK_BIN", binary)
        return [binary]
    monkeypatch.delenv("STREETZIM_PACK_BIN", raising=False)
    return [sys.executable, "-m", "streetzim.pack"]


@pytest.mark.parametrize("manifest_zstd", ["0", "1"])
@pytest.mark.parametrize("compress", [False, True])
def test_real_streamed_body_roundtrips_with_stock_libzim(
        tmp_path, monkeypatch, real_packer, manifest_zstd, compress):
    Archive = pytest.importorskip("libzim.reader").Archive
    if manifest_zstd == "1":
        pytest.importorskip("zstandard")
    monkeypatch.setenv("STREETZIM_MANIFEST_ZSTD", manifest_zstd)
    source = tmp_path / "large.bin"
    block = bytes(range(256)) * 4096
    digest = hashlib.sha256()
    with source.open("wb") as body:
        for _ in range(64):
            body.write(block)
            digest.update(block)
    out = tmp_path / "out.zim"
    creator = mw.ManifestCreator(str(out), compression_level=1, keep_stage=True)
    creator.config_nbworkers(2)
    creator.set_mainpath("index.html")
    with creator:
        home = item(b"<html>viewer</html>", mime="text/html", front=True)
        home._path = "index.html"
        creator.add_item(home)
        graph = item(file=str(source))
        graph._compress = compress
        creator.add_item(graph)
        search = item(b"<html>search</html>", mime="text/html", front=True)
        search._path = "search/last.html"
        creator.add_item(search)
    archive = Archive(str(out))
    assert archive.main_entry.get_item().path == "index.html"
    got = archive.get_entry_by_path("body").get_item()
    assert got.size == source.stat().st_size
    assert hashlib.sha256(got.content).digest() == digest.digest()
    assert archive.check()


@pytest.mark.parametrize("suffix", ["invalid json", "duplicate"])
def test_real_packer_error_preserves_archive(tmp_path, real_packer, suffix):
    out = tmp_path / "out.zim"
    out.write_bytes(b"previous archive")
    manifest = tmp_path / "input.jsonl"
    record = {"kind": "item", "path": "index.html", "mime": "text/html",
              "content": "viewer"}
    manifest.write_text(json.dumps(record) + "\n" +
                        (json.dumps(record) if suffix == "duplicate" else suffix))
    completed = subprocess.run(real_packer + [str(manifest), str(out)],
                               capture_output=True, text=True)
    assert completed.returncode != 0
    assert out.read_bytes() == b"previous archive"
    assert not list(tmp_path.glob(".streetzim-pack-*.zim"))
