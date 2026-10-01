"""Independent behavioral fault probes; no production/container dependencies."""

import gc
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import zstandard
from libzim.reader import Archive
import streetzim.pack as packer


OLD = b"prior complete archive must survive"


def item(path="index.html", content="ok", **kwargs):
    return dict(kind="item", path=path, mime="text/html", content=content, **kwargs)


def manifest(tmp_path, records):
    path = tmp_path / "manifest.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def failed_pack(tmp_path, path, error=Exception):
    output = tmp_path / "archive.zim"
    output.write_bytes(OLD)
    source = path.read_bytes()
    with pytest.raises(error):
        packer.pack(path, output, threads=2)
    assert output.read_bytes() == OLD
    assert path.read_bytes() == source
    assert not list(tmp_path.glob(".streetzim-pack-*"))
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("zim-compress")
    ]


@pytest.mark.parametrize("cut", [1, 4, 10])
def test_truncated_final_zstd_frame_preserves_output(tmp_path, cut):
    first = (json.dumps(item("first")) + "\n").encode()
    second = (json.dumps(item("second")) + "\n").encode()
    codec = zstandard.ZstdCompressor(write_checksum=True)
    source = tmp_path / "concat.jsonl.zst"
    source.write_bytes(codec.compress(first) + codec.compress(second)[:-cut])
    failed_pack(tmp_path, source, packer.PackError)


def test_concatenated_zstd_frames_split_record(tmp_path):
    raw = (json.dumps(item(content="many bytes" * 500)) + "\n").encode()
    source = tmp_path / "concat.jsonl.zst"
    codec = zstandard.ZstdCompressor(write_checksum=True)
    source.write_bytes(codec.compress(raw[:35]) + codec.compress(raw[35:]))
    output = tmp_path / "archive.zim"
    packer.pack(source, output, threads=2)
    archive = Archive(str(output))
    assert archive.check()
    assert (
        bytes(archive.get_entry_by_path("index.html").get_item().content)
        == b"many bytes" * 500
    )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"kind":"item","path":"a","path":"b","mime":"text/html","content":"x"}\n',
        b'{"kind":"config","cluster_size_target":NaN}\n',
        b'{"kind":"item","path":"a","mime":"text/html","content":"\\ud800"}\n',
        b'{"kind":"item","path":"a","mime":"text/html","body_b64":"Zh=="}\n',
        b'{"kind":"item","path":"a","mime":"text/html","body_b64":"Zg==junk"}\n',
        b'{"kind":"item","path":"a\\u0000b","mime":"text/html","content":"x"}\n',
        b'{"kind":"item","path":"a","mime":"text/html","content":"x","namespace":32}\n',
        b'{"kind":"item","path":"a","mime":"text/html","content":"x","namespace":true}\n',
    ],
)
def test_invalid_manifest_preserves_output(tmp_path, raw):
    source = tmp_path / "bad.jsonl"
    source.write_bytes(raw)
    failed_pack(tmp_path, source, packer.PackError)


@pytest.mark.parametrize(
    "records",
    [
        [item("a"), item("a")],
        [item("a"), {"kind": "redirect", "path": "a", "target": "a"}],
        [{"kind": "redirect", "path": "a", "target": "a"}],
        [
            {"kind": "redirect", "path": "a", "target": "b"},
            {"kind": "redirect", "path": "b", "target": "a"},
        ],
        [item(), {"kind": "redirect", "path": "missing", "target": "unknown"}],
        [{"kind": "config", "main_path": "missing"}, item()],
        [
            {"kind": "config", "main_path": "index.html"},
            item(),
            item("mainPage", namespace=ord("W")),
        ],
        [item(), {"kind": "config"}],
    ],
)
def test_finalize_and_duplicate_failures_preserve_output(tmp_path, records):
    failed_pack(tmp_path, manifest(tmp_path, records), packer.PackError)


@pytest.mark.parametrize(
    "strategy", ["single", "by_mime", "by_extension", "by_first_path_segment"]
)
def test_main_redirect_namespace_compression_bytes(tmp_path, strategy):
    records = [
        {
            "kind": "config",
            "main_path": "alias",
            "compression": "zstd",
            "compression_level": 22,
            "cluster_strategy": strategy,
            "cluster_size_target": 5,
        },
        item(content="INDEX", front=True),
        item("second.html", "SECOND", front=True),
        {"kind": "redirect", "path": "alias", "title": "Alias", "target": "index.html"},
        item("index.html", "META", namespace=ord("M")),
        item("fulltext/xapian", "XINDEX", namespace=ord("X"), compress=True),
        item("raw.json", "RAW", compress=False),
    ]
    output = tmp_path / "archive.zim"
    packer.pack(manifest(tmp_path, records), output, threads=2)
    archive = Archive(str(output))
    assert archive.check()
    assert bytes(archive.main_entry.get_item().content) == b"INDEX"
    for path, data in [
        ("index.html", b"INDEX"),
        ("second.html", b"SECOND"),
        ("raw.json", b"RAW"),
    ]:
        assert bytes(archive.get_entry_by_path(path).get_item().content) == data


@pytest.mark.parametrize("change", ["grow", "shrink", "rewrite"])
def test_file_mutation_mid_read_preserves_output(tmp_path, monkeypatch, change):
    body = tmp_path / "body.bin"
    body.write_bytes(b"a" * (packer.CHUNK + 100))
    source = manifest(
        tmp_path,
        [
            {"kind": "config", "compression": "none"},
            {
                "kind": "item",
                "path": "body.bin",
                "mime": "application/octet-stream",
                "file": str(body),
                "streaming": True,
                "size": body.stat().st_size,
            },
        ],
    )
    original = packer.Body.chunks

    def changed_chunks(self, stop):
        for index, part in enumerate(original(self, stop)):
            yield part
            if index == 0 and self.path == body:
                if change == "grow":
                    with body.open("ab") as stream:
                        stream.write(b"more")
                elif change == "shrink":
                    with body.open("r+b") as stream:
                        stream.truncate(10)
                else:
                    with body.open("r+b") as stream:
                        stream.write(b"changed")

    monkeypatch.setattr(packer.Body, "chunks", changed_chunks)
    failed_pack(tmp_path, source, packer.PackError)


def test_codec_error_reaps_queued_workers(tmp_path, monkeypatch):
    source = manifest(
        tmp_path,
        [
            {"kind": "config", "cluster_size_target": 1},
            *[item(str(index), "x" * 1000) for index in range(50)],
        ],
    )

    def broken_encode(*args):
        raise zstandard.ZstdError("injected encoder failure")

    monkeypatch.setattr(packer, "_encode", broken_encode)
    failed_pack(tmp_path, source, zstandard.ZstdError)


def test_replace_failure_preserves_output(tmp_path, monkeypatch):
    source = manifest(tmp_path, [item()])

    def broken_replace(*args):
        raise PermissionError("injected rename failure")

    monkeypatch.setattr(packer.os, "replace", broken_replace)
    failed_pack(tmp_path, source, PermissionError)


def test_bucket_and_queue_metrics_bounded_for_distinct_keys(tmp_path):
    records = [
        {
            "kind": "config",
            "compression": "zstd",
            "cluster_strategy": "by_first_path_segment",
            "cluster_size_target": 1 << 20,
            "max_in_flight_bytes": 16384,
        }
    ]
    records += [item(f"key{index}/file", "x" * 4096) for index in range(512)]
    stats = packer.pack(
        manifest(tmp_path, records), tmp_path / "archive.zim", threads=2
    )
    assert stats["peak_bucket_bytes"] <= 16384
    assert stats["peak_in_flight_bytes"] <= 16384
    assert stats["peak_open_buckets"] <= packer.MAX_BUCKETS


def test_constructor_failure_closes_descriptors_even_with_retained_traceback(
    tmp_path, monkeypatch
):
    source = manifest(tmp_path, [item()])
    connect = sqlite3.connect

    class BrokenConnection(sqlite3.Connection):
        def executescript(self, script):
            raise OSError("injected directory initialization failure")

    monkeypatch.setattr(
        packer.sqlite3,
        "connect",
        lambda *args, **kwargs: connect(*args, **kwargs, factory=BrokenConnection),
    )
    gc.collect()
    before = len(os.listdir("/dev/fd"))
    with pytest.raises(OSError) as retained:
        packer.pack(source, tmp_path / "archive.zim", threads=2)
    gc.collect()
    after = len(os.listdir("/dev/fd"))
    assert retained.value is not None
    assert after <= before, f"retained exception leaked {after - before} descriptors"


def test_file_reader_initialization_error_closes_descriptor(tmp_path, monkeypatch):
    body = tmp_path / "body.bin"
    body.write_bytes(b"test body")
    source = manifest(
        tmp_path,
        [
            {"kind": "config", "compression": "none"},
            {
                "kind": "item",
                "path": "body.bin",
                "mime": "application/octet-stream",
                "file": str(body),
            },
        ],
    )

    def failing_fdopen(*args, **kwargs):
        raise OSError("injected reader setup failure")

    monkeypatch.setattr(packer.os, "fdopen", failing_fdopen)
    gc.collect()
    before = len(os.listdir("/dev/fd"))
    failed_pack(tmp_path, source, OSError)
    gc.collect()
    after = len(os.listdir("/dev/fd"))
    assert after <= before, f"file reader setup leaked {after - before} descriptors"


def test_manifest_truncation_at_valid_record_boundary_cannot_publish_subset(
    tmp_path, monkeypatch
):
    source = tmp_path / "manifest.jsonl"
    lines = []
    for index in range(128):
        record = item(f"item{index:06d}", "")
        overhead = len(json.dumps(record).encode()) + 1
        record["content"] = "a" * (1024 - overhead)
        line = (json.dumps(record) + "\n").encode()
        assert len(line) == 1024
        lines.append(line)
    source.write_bytes(b"".join(lines))
    output = tmp_path / "archive.zim"
    output.write_bytes(OLD)
    original = packer.Writer.record
    changed = False

    def truncate_after_first_record(self, record):
        nonlocal changed
        result = original(self, record)
        if not changed:
            with source.open("r+b") as stream:
                stream.truncate(64 << 10)
            changed = True
        return result

    monkeypatch.setattr(packer.Writer, "record", truncate_after_first_record)
    with pytest.raises(packer.PackError, match=r"changed|truncat"):
        packer.pack(source, output, threads=2)
    assert output.read_bytes() == OLD
    assert not list(tmp_path.glob(".streetzim-pack-*"))


@pytest.mark.parametrize(
    "damage",
    [
        "url_pointer",
        "title_pointer",
        "cluster_pointer",
        "namespace",
        "redirect_index",
        "raw_offset",
        "main_index",
        "mime_terminator",
        "mime_ascii",
        "mime_token",
        "compression_info",
    ],
)
def test_structural_gate_rejects_corrupt_serialized_tables(
    tmp_path, monkeypatch, damage
):
    source = manifest(
        tmp_path,
        [
            {
                "kind": "config",
                "compression": "none",
                "cluster_size_target": 10,
                "main_path": "alias",
            },
            item("index.html", "index" * 5),
            item("other", "other" * 5),
            {"kind": "redirect", "path": "alias", "target": "index.html"},
        ],
    )
    verifier = packer._verify_structure

    def corrupt_then_verify(stream, database):
        stream.seek(0)
        header = packer.HEADER.unpack(stream.read(packer.HEADER.size))
        urls, titles, clusters, checksum = header[6], header[7], header[8], header[12]
        stream.seek(urls)
        directory = packer.U64.unpack(stream.read(8))[0]
        stream.seek(clusters)
        cluster = packer.U64.unpack(stream.read(8))[0]
        if damage == "url_pointer":
            stream.seek(urls)
            stream.write(packer.U64.pack(checksum))
        elif damage == "title_pointer":
            stream.seek(titles)
            stream.write(packer.U32.pack(packer.MAX_U32))
        elif damage == "cluster_pointer":
            stream.seek(clusters)
            stream.write(packer.U64.pack(urls))
        elif damage == "namespace":
            stream.seek(directory + 3)
            stream.write(b"\0")
        elif damage == "redirect_index":
            stream.seek(directory + 8)  # alias is the first content path
            stream.write(packer.U32.pack(packer.MAX_U32))
        elif damage == "raw_offset":
            stream.seek(cluster + 1)
            stream.write(packer.U32.pack(3))
        elif damage == "main_index":
            stream.seek(64)
            stream.write(packer.U32.pack(0))
        elif damage == "mime_terminator":
            stream.seek(packer.HEADER.size)
            stream.write(b"x" * packer.MIME_RESERVE)
        elif damage in ("mime_ascii", "mime_token"):
            stream.seek(packer.HEADER.size)
            stream.write(b"\xff" if damage == "mime_ascii" else b"@")
        else:
            stream.seek(cluster)
            stream.write(b"\x07")
        stream.flush()
        return verifier(stream, database)

    monkeypatch.setattr(packer, "_verify_structure", corrupt_then_verify)
    failed_pack(tmp_path, source, ValueError)


def test_structural_verifier_io_error_preserves_output_and_cleans_workers(
    tmp_path, monkeypatch
):
    source = manifest(tmp_path, [item()])

    def broken_verifier(*args):
        raise OSError("injected verification read failure")

    monkeypatch.setattr(packer, "_verify_structure", broken_verifier)
    failed_pack(tmp_path, source, OSError)


def test_structural_gate_rejects_impossible_compressed_blob_index(
    tmp_path, monkeypatch
):
    source = manifest(
        tmp_path,
        [
            {"kind": "config", "compression": "zstd", "compression_level": 1},
            item(),
        ],
    )
    verifier = packer._verify_structure

    def corrupt_then_verify(stream, database):
        stream.seek(0)
        header = packer.HEADER.unpack(stream.read(packer.HEADER.size))
        stream.seek(header[6])
        directory = packer.U64.unpack(stream.read(8))[0]
        stream.seek(directory + 12)
        stream.write(packer.U32.pack(packer.MAX_U32))
        stream.flush()
        return verifier(stream, database)

    monkeypatch.setattr(packer, "_verify_structure", corrupt_then_verify)
    failed_pack(tmp_path, source, packer.PackError)


@pytest.mark.parametrize("cycle", ["self", "two-node"])
def test_structural_gate_rejects_serialized_cycles_outside_main_page(
    tmp_path, monkeypatch, cycle
):
    source = manifest(
        tmp_path,
        [
            {"kind": "config", "main_path": "index.html"},
            item(),
            {"kind": "redirect", "path": "a", "target": "index.html"},
            {"kind": "redirect", "path": "b", "target": "index.html"},
        ],
    )
    verifier = packer._verify_structure

    def corrupt_then_verify(stream, database):
        stream.seek(0)
        header = packer.HEADER.unpack(stream.read(packer.HEADER.size))
        changes = [(0, 0)] if cycle == "self" else [(0, 1), (1, 0)]
        for index, target in changes:
            stream.seek(header[6] + index * 8)
            offset = packer.U64.unpack(stream.read(8))[0]
            stream.seek(offset + 8)
            stream.write(packer.U32.pack(target))
        stream.seek(0)
        digest = hashlib.md5(usedforsecurity=False)
        remaining = header[12]
        while remaining:
            data = stream.read(min(packer.CHUNK, remaining))
            digest.update(data)
            remaining -= len(data)
        stream.write(digest.digest())
        stream.flush()
        return verifier(stream, database)

    monkeypatch.setattr(packer, "_verify_structure", corrupt_then_verify)
    failed_pack(tmp_path, source, packer.PackError)


def test_named_pipe_manifest_fails_promptly_without_publishing(tmp_path):
    source = tmp_path / "pipe.jsonl"
    os.mkfifo(source)
    output = tmp_path / "archive.zim"
    output.write_bytes(OLD)
    env = dict(
        os.environ, PYTHONPATH=str(Path(packer.__file__).resolve().parent.parent)
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "streetzim.pack",
            str(source),
            str(output),
            "--threads",
            "1",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 1
    assert "regular file" in completed.stderr
    assert output.read_bytes() == OLD
    assert not list(tmp_path.glob(".streetzim-pack-*"))


def test_final_fsync_failure_preserves_previous_archive(tmp_path, monkeypatch):
    source = manifest(tmp_path, [item()])

    def broken_fsync(*args):
        raise OSError("injected final fsync failure")

    monkeypatch.setattr(packer.os, "fsync", broken_fsync)
    failed_pack(tmp_path, source, OSError)


@pytest.mark.parametrize("compression", ["none", "zstd"])
def test_real_cli_sigterm_preserves_output_and_cleans_stage(tmp_path, compression):
    body = tmp_path / "body.bin"
    with body.open("wb") as stream:
        stream.truncate(128 << 20)
    source = manifest(
        tmp_path,
        [
            {"kind": "config", "compression": compression, "compression_level": 1},
            {
                "kind": "item",
                "path": "body.bin",
                "mime": "application/octet-stream",
                "file": str(body),
                "streaming": True,
                "size": body.stat().st_size,
            },
        ],
    )
    output = tmp_path / "archive.zim"
    output.write_bytes(OLD)
    ready = tmp_path / "ready"
    helper = tmp_path / "slow_pack.py"
    helper.write_text(
        """import sys,time\nfrom pathlib import Path\nimport streetzim.pack as p\noriginal=p.Body.chunks\ndef slow(self,stop):\n for chunk in original(self,stop):\n  Path(sys.argv[3]).touch()\n  time.sleep(.1)\n  yield chunk\np.Body.chunks=slow\nraise SystemExit(p.main([sys.argv[1],sys.argv[2],"--threads","2"]))\n"""
    )
    env = dict(
        os.environ, PYTHONPATH=str(Path(packer.__file__).resolve().parent.parent)
    )
    process = subprocess.Popen(
        [sys.executable, str(helper), str(source), str(output), str(ready)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while (
            not ready.exists()
            and time.monotonic() < deadline
            and process.poll() is None
        ):
            time.sleep(0.01)
        assert ready.exists(), "packer did not begin reading"
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 130, (stdout, stderr)
        assert output.read_bytes() == OLD
        assert not list(tmp_path.glob(".streetzim-pack-*"))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize("change", ["grow", "shrink", "rewrite"])
def test_compressed_mutation_preserves_original_path_specific_error(
    tmp_path, monkeypatch, change
):
    body = tmp_path / "mutating-source.bin"
    body.write_bytes(b"a" * (packer.CHUNK + 100))
    source = tmp_path / "manifest.jsonl"
    records = [
        {"kind": "config", "compression": "zstd", "compression_level": 1},
        {
            "kind": "item",
            "path": "body.bin",
            "mime": "application/octet-stream",
            "file": str(body),
            "streaming": True,
            "size": body.stat().st_size,
        },
    ]
    source.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    output = tmp_path / "archive.zim"
    output.write_bytes(b"prior")
    original = packer.Body.chunks

    def changed(self, stop):
        for index, part in enumerate(original(self, stop)):
            yield part
            if index == 0 and self.path == body:
                if change == "grow":
                    with body.open("ab") as stream:
                        stream.write(b"more")
                elif change == "shrink":
                    with body.open("r+b") as stream:
                        stream.truncate(10)
                else:
                    with body.open("r+b") as stream:
                        stream.write(b"changed")

    monkeypatch.setattr(packer.Body, "chunks", changed)
    with pytest.raises(packer.PackError, match=r"grew|shrank|changed") as captured:
        packer.pack(source, output, threads=2)
    assert str(body) in str(captured.value)
    assert output.read_bytes() == b"prior"
    assert not list(tmp_path.glob(".streetzim-pack-*"))
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("zim-compress")
    ]
