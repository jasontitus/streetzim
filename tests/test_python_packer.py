"""Independent format, native readback and failure probes for the Python packer."""

from __future__ import annotations

import hashlib
import io
import json
import lzma
import os
import sqlite3
import struct
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import zstandard

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from streetzim import pack as p  # noqa: E402


def _item(path="body", body="abc", mime="text/plain", **kwargs):
    return {"kind": "item", "path": path, "mime": mime, "content": body, **kwargs}


def _inspect(path):
    """Bounded test-only reader checks tables, raw blobs and native readback."""
    from libzim.reader import Archive

    assert path.stat().st_size <= 4 << 20
    data = path.read_bytes()
    header = struct.unpack("<IHH16sIIQQQQIIQ", data[:80])
    (
        magic,
        major,
        minor,
        _uuid,
        count,
        cluster_count,
        url_pos,
        title_pos,
        cluster_pos,
        mime_pos,
        main,
        layout,
        checksum_pos,
    ) = header
    assert magic == 0x044D495A and (major, minor) == (6, 3)
    assert mime_pos == 80 and layout == 0xFFFFFFFF
    assert count <= 20000 and cluster_count <= 20000
    assert len(data) == checksum_pos + 16
    assert (
        data[checksum_pos:]
        == hashlib.md5(data[:checksum_pos], usedforsecurity=False).digest()
    )
    assert title_pos == url_pos + count * 8
    assert cluster_pos == title_pos + count * 4
    directory_pos = cluster_pos + cluster_count * 8
    assert 80 < url_pos <= title_pos <= cluster_pos <= directory_pos <= checksum_pos
    mime_end = data.index(b"\0\0", mime_pos)
    mimes = [value.decode("ascii") for value in data[mime_pos:mime_end].split(b"\0")]
    cluster_offsets = list(
        struct.unpack_from("<" + "Q" * cluster_count, data, cluster_pos)
    )
    assert len(set(cluster_offsets)) == cluster_count
    sorted_clusters = sorted(cluster_offsets)
    cluster_end = dict(
        zip(sorted_clusters, sorted_clusters[1:] + [url_pos], strict=True)
    )
    clusters = {}
    for start in cluster_offsets:
        assert mime_end < start < url_pos
        end = cluster_end[start]
        info = data[start]
        assert not info & ~0x1F
        code, extended = info & 15, bool(info & 16)
        assert code in (1, 4, 5)
        raw = data[start + 1 : end]
        if code == 4:
            decoder = lzma.LZMADecompressor(memlimit=128 << 20)
            raw = decoder.decompress(raw, max_length=(8 << 20) + 1)
            assert decoder.eof and not decoder.unused_data
        elif code == 5:
            parameters = zstandard.get_frame_parameters(raw)
            assert parameters.content_size <= 8 << 20
            assert parameters.window_size <= 8 << 20
            raw = zstandard.ZstdDecompressor().decompress(raw)
        assert len(raw) <= 8 << 20
        width = 8 if extended else 4
        unpacker = "Q" if extended else "I"
        (first,) = struct.unpack_from("<" + unpacker, raw)
        assert first % width == 0 and width <= first <= len(raw)
        offsets = list(struct.unpack_from("<" + unpacker * (first // width), raw))
        assert offsets == sorted(offsets) and offsets[-1] == len(raw)
        clusters[start] = code, extended, raw, offsets
    native = Archive(path)
    assert native.check() and native.all_entry_count == count
    entries = []
    cursor = directory_pos
    for index in range(count):
        (offset,) = struct.unpack_from("<Q", data, url_pos + index * 8)
        assert offset == cursor
        mime, params, namespace, revision = struct.unpack_from("<HBBI", data, offset)
        assert params == revision == 0 and 33 <= namespace <= 126
        start = offset + (12 if mime == 0xFFFF else 16)
        end = data.index(0, start)
        path_text = data[start:end].decode("utf-8")
        title_end = data.index(0, end + 1)
        title = data[end + 1 : title_end].decode("utf-8") or path_text
        cursor = title_end + 1
        entry = native._get_entry_by_id(index)
        assert entry.path == path_text and entry.title == title
        record = {"namespace": namespace, "path": path_text, "title": title}
        if mime == 0xFFFF:
            assert entry.is_redirect
            (record["target_index"],) = struct.unpack_from("<I", data, offset + 8)
            assert record["target_index"] < count
        else:
            assert not entry.is_redirect and mime < len(mimes)
            cluster, blob = struct.unpack_from("<II", data, offset + 8)
            assert cluster < cluster_count
            code, extended, raw, offsets = clusters[cluster_offsets[cluster]]
            assert blob < len(offsets) - 1
            body = raw[offsets[blob] : offsets[blob + 1]]
            item = entry.get_item()
            assert item.mimetype == mimes[mime] and bytes(item.content) == body
            record.update(
                mime=mimes[mime],
                digest=hashlib.sha256(body).hexdigest(),
                size=len(body),
                compression=code,
                extended=extended,
            )
            if namespace == 77 and path_text == "Counter":
                record["body_hex"] = body.hex()
            if namespace == 88 and path_text == "listing/titleOrdered/v1":
                assert len(body) % 4 == 0
                record["listing_indices"] = list(
                    struct.unpack("<" + "I" * (len(body) // 4), body)
                )
        entries.append(record)
    assert cursor == checksum_pos
    keys = [(entry["namespace"], entry["path"]) for entry in entries]
    assert keys == sorted(keys) and len(set(keys)) == count
    order = list(struct.unpack_from("<" + "I" * count, data, title_pos))
    assert sorted(order) == list(range(count))
    titles = [(entries[index]["namespace"], entries[index]["title"]) for index in order]
    assert titles == sorted(titles)
    for record in entries:
        if "target_index" in record:
            record["target"] = keys[record.pop("target_index")]
        if "listing_indices" in record:
            record["listing"] = [keys[index] for index in record.pop("listing_indices")]
    return {
        "version": [major, minor],
        "main": keys[main] if main != 0xFFFFFFFF else None,
        "entries": entries,
    }


def manifest(tmp_path, records, name="manifest"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(r, ensure_ascii=True) + "\n" for r in records))
    return path


def archive(tmp_path, records):
    path = tmp_path / "out.zim"
    p.pack(manifest(tmp_path, records), path, threads=1)
    return path, _inspect(path)


def raw_clusters(path):
    data = path.read_bytes()
    h = p.HEADER.unpack(data[:80])
    n, up = h[4], h[6]
    result = {}
    for i in range(n):
        off = struct.unpack_from("<Q", data, up + i * 8)[0]
        mime, params, ns, rev = struct.unpack_from("<HBBI", data, off)
        if mime == 65535:
            continue
        cluster, blob = struct.unpack_from("<II", data, off + 8)
        start = off + 16
        end = data.index(0, start)
        url = data[start:end].decode()
        result[(ns, url)] = (cluster, blob)
    return result


@pytest.mark.parametrize("size", [0, 1, 100, 1 << 20])
@pytest.mark.parametrize("level", [3, 19, 22])
def test_zstd_frame_window_and_pledged_size(size, level):
    body = p.Body(size, data=b"x" * size)
    dest = io.BytesIO()
    p._encode(p.Bucket(0, "zstd", [body], size), dest, level, threading.Event())
    params = zstandard.get_frame_parameters(dest.getvalue()[1:])
    payload = size + 8
    ceiling = 1 << max(10, min(27, (payload - 1).bit_length()))
    assert params.window_size <= ceiling
    assert params.content_size == payload
    assert (
        zstandard.ZstdDecompressor().decompress(dest.getvalue()[1:])
        == struct.pack("<II", 8, payload) + b"x" * size
    )


@pytest.mark.parametrize("level", [-100, -22, -1, 0, 1, 19, 22])
def test_zstd_signed_level_retained(level):
    cfg = p.Config.parse({"compression_level": level})
    assert cfg.compression_level() == level


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("-2147483649", 19),
        ("2147483648", 19),
        ("invalid", 19),
        ("-100", -100),
        ("0", 0),
        ("2147483647", 22),
    ],
)
def test_zstd_environment_level_has_signed_32bit_fallback(monkeypatch, value, expected):
    monkeypatch.setenv("ZSTD_CLEVEL", value)
    assert p.Config().compression_level() == expected


@pytest.mark.parametrize("error_type", [p.PackError, OSError, KeyboardInterrupt])
def test_zstd_pledge_close_preserves_original_input_error(monkeypatch, error_type):
    original = error_type("original input failure")

    def broken_chunks(self, stop):
        yield b"x"
        raise original

    monkeypatch.setattr(p.Body, "chunks", broken_chunks)
    body = p.Body(100, data=b"x" * 100)
    with pytest.raises(error_type) as raised:
        p._encode(p.Bucket(0, "zstd", [body], 100), io.BytesIO(), 3, threading.Event())
    assert raised.value is original


def test_zstd_pledge_close_failure_surfaces_without_prior_error():
    # The encoder must still reject a body which emits fewer bytes than promised.
    body = p.Body(100, data=b"x")
    with pytest.raises(zstandard.ZstdError):
        p._encode(p.Bucket(0, "zstd", [body], 100), io.BytesIO(), 3, threading.Event())


@pytest.mark.parametrize("codec", ["none", "xz", "zstd"])
def test_target_preflush(codec, tmp_path):
    dest, _ = archive(
        tmp_path,
        [
            {"kind": "config", "compression": codec, "cluster_size_target": 10},
            _item("a", "123456"),
            _item("b", "ABCDEF"),
        ],
    )
    clusters = raw_clusters(dest)
    assert clusters[(67, "a")][0] != clusters[(67, "b")][0]


@pytest.mark.parametrize("codec", ["none", "xz", "zstd"])
@pytest.mark.parametrize(
    "strategy", ["single", "by_mime", "by_extension", "by_first_path_segment"]
)
def test_cluster_break_separates_all_buckets_and_restores_target(
    tmp_path, codec, strategy
):
    records = [
        {
            "kind": "config",
            "compression": codec,
            "compression_level": 3,
            "cluster_strategy": strategy,
            "cluster_size_target": 64,
            "max_in_flight_bytes": 128,
        },
        _item("a/0.aa", "a" * 8, mime="text/plain"),
        _item("b/0.bb", "b" * 8, mime="application/json"),
        {"kind": "cluster_break", "cluster_size_target": 10},
        _item("a/1.aa", "c" * 6, mime="text/plain"),
        _item("b/1.bb", "d" * 6, mime="application/json"),
        _item("a/2.aa", "e" * 6, mime="text/plain"),
        _item("b/2.bb", "f" * 6, mime="application/json"),
        {"kind": "cluster_break", "cluster_size_target": 64},
        _item("a/3.aa", "g" * 8, mime="text/plain"),
        _item("b/3.bb", "h" * 8, mime="application/json"),
        _item("a/4.aa", "i" * 8, mime="text/plain"),
        _item("b/4.bb", "j" * 8, mime="application/json"),
    ]
    output = tmp_path / "out.zim"
    stats = p.pack(manifest(tmp_path, records), output, threads=2)
    clusters = raw_clusters(output)

    def phase(numbers):
        return {
            clusters[(67, f"{group}/{number}.{group * 2}")][0]
            for group in ("a", "b")
            for number in numbers
        }

    before, smaller, restored = phase([0]), phase([1, 2]), phase([3, 4])
    assert before.isdisjoint(smaller)
    assert before.isdisjoint(restored)
    assert smaller.isdisjoint(restored)
    assert len(smaller) == 4  # Each pair would exceed the new 10-byte target.
    for group in ("a", "b"):
        assert (
            clusters[(67, f"{group}/3.{group * 2}")][0]
            == clusters[(67, f"{group}/4.{group * 2}")][0]
        )
    assert stats["cluster_breaks"] == 2
    assert stats["peak_bucket_bytes"] <= 128
    assert stats["peak_in_flight_bytes"] <= 128
    _inspect(output)


def test_leading_consecutive_cluster_breaks_create_no_empty_clusters(tmp_path):
    config = {"kind": "config", "compression": "none"}
    initial = p.pack(manifest(tmp_path, [config]), tmp_path / "initial.zim", threads=1)
    records = [
        config,
        {"kind": "cluster_break"},
        {"kind": "cluster_break", "cluster_size_target": None},
        {"kind": "cluster_break", "cluster_size_target": 64},
    ]
    output = tmp_path / "out.zim"
    stats = p.pack(manifest(tmp_path, records), output, threads=1)
    assert stats["clusters"] == initial["clusters"]
    assert stats["cluster_breaks"] == 3
    _inspect(output)


@pytest.mark.parametrize("target", [None, 0, (1 << 64) - 1])
def test_cluster_break_target_null_zero_and_unsigned_boundary(tmp_path, target):
    output, _ = archive(
        tmp_path,
        [
            {"kind": "config", "compression": "none", "cluster_size_target": 10},
            {"kind": "cluster_break", "cluster_size_target": target},
            _item("a", "a"),
            _item("b", "b"),
        ],
    )
    clusters = raw_clusters(output)
    assert (clusters[(67, "a")][0] == clusters[(67, "b")][0]) is (target != 0)


@pytest.mark.parametrize("target", [-1, True, False, 1.0, "64", [], {}, 1 << 64])
def test_invalid_cluster_break_target_preserves_destination(tmp_path, target):
    output = tmp_path / "out.zim"
    output.write_bytes(b"previous archive")
    records = [_item(), {"kind": "cluster_break", "cluster_size_target": target}]
    with pytest.raises(p.PackError, match="cluster_size_target"):
        p.pack(manifest(tmp_path, records), output, threads=2)
    assert output.read_bytes() == b"previous archive"
    assert not list(tmp_path.glob(".streetzim-pack-*"))


def test_config_cannot_follow_a_cluster_break(tmp_path):
    output = tmp_path / "out.zim"
    output.write_bytes(b"previous archive")
    with pytest.raises(p.PackError, match="config must appear once before content"):
        p.pack(
            manifest(tmp_path, [{"kind": "cluster_break"}, {"kind": "config"}]),
            output,
            threads=1,
        )
    assert output.read_bytes() == b"previous archive"


def test_adapter_cluster_break_native_roundtrip(tmp_path, monkeypatch):
    from cloud.manifest_writer import ManifestCreator

    monkeypatch.delenv("STREETZIM_PACK_BIN", raising=False)
    output = tmp_path / "out.zim"
    with ManifestCreator(str(output), compression_level=1) as creator:
        creator.add_item(
            SimpleNamespace(
                _path="tiles/12/a.pbf",
                _title="a",
                _mimetype="application/x-protobuf",
                _data=b"first zoom",
                _file_path=None,
                _is_front=False,
            )
        )
        creator.cluster_break(64)
        creator.cluster_break()
        creator.add_item(
            SimpleNamespace(
                _path="tiles/13/a.pbf",
                _title="b",
                _mimetype="application/x-protobuf",
                _data=b"second zoom",
                _file_path=None,
                _is_front=False,
            )
        )
    clusters = raw_clusters(output)
    assert clusters[(67, "tiles/12/a.pbf")][0] != clusters[(67, "tiles/13/a.pbf")][0]
    _inspect(output)


@pytest.mark.parametrize("cut", [1, 2, 3, 4, 7, 8, 10, 15])
def test_truncated_zstd_preserves_output(cut, tmp_path):
    encoded = zstandard.ZstdCompressor(write_checksum=True).compress(
        (json.dumps(_item("article", "body")) + "\n").encode()
    )
    source = tmp_path / "input.any"
    source.write_bytes(encoded[:-cut])
    dest = tmp_path / "out.zim"
    dest.write_bytes(b"OLD")
    with pytest.raises(p.PackError):
        p.pack(source, dest, threads=1)
    assert dest.read_bytes() == b"OLD"
    assert not list(tmp_path.glob(".streetzim-pack-*"))


def test_corrupted_zstd_checksum_preserves_output(tmp_path):
    encoded = bytearray(
        zstandard.ZstdCompressor(write_checksum=True).compress(
            (json.dumps(_item("article", "body")) + "\n").encode()
        )
    )
    encoded[-1] ^= 1
    source = tmp_path / "source"
    source.write_bytes(encoded)
    dest = tmp_path / "out.zim"
    dest.write_bytes(b"OLD")
    with pytest.raises(p.PackError):
        p.pack(source, dest, threads=1)
    assert dest.read_bytes() == b"OLD"


def test_concatenated_zstd_split_inside_record(tmp_path):
    text = (
        json.dumps(_item("first", "one")) + "\n" + json.dumps(_item("second", "two"))
    ).encode()
    split = 17
    source = tmp_path / "input.jsonl"
    source.write_bytes(
        zstandard.ZstdCompressor().compress(text[:split])
        + zstandard.ZstdCompressor().compress(text[split:])
    )
    dest = tmp_path / "out.zim"
    p.pack(source, dest, threads=1)
    a = _inspect(dest)
    assert {e["path"] for e in a["entries"] if e["namespace"] == 67} == {
        "first",
        "second",
    }


def test_magic_beats_extension_and_plain_no_newline(tmp_path):
    source = tmp_path / "wrong.jsonl.zst"
    source.write_text(json.dumps(_item("entry", "plain")))
    p.pack(source, tmp_path / "plain.zim", threads=1)
    _inspect(tmp_path / "plain.zim")
    source = tmp_path / "wrong.txt"
    source.write_bytes(
        zstandard.ZstdCompressor().compress(json.dumps(_item()).encode())
    )
    p.pack(source, tmp_path / "zstd.zim", threads=1)
    _inspect(tmp_path / "zstd.zim")


@pytest.mark.parametrize(
    "size,extended,width",
    [
        ([0], 0, 4),
        ([4294967287], 0, 4),
        ([4294967288], 16, 8),
        ([4294967295, 0], 16, 8),
    ],
)
def test_extended_cluster_offset_boundaries(size, extended, width):
    ext, iterator = p._offsets(size)
    data = b"".join(iterator)
    assert ext == extended
    offsets = struct.unpack("<" + ("Q" if width == 8 else "I") * (len(size) + 1), data)
    assert offsets[0] == width * (len(size) + 1)
    assert offsets[-1] == offsets[0] + sum(size)


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("item", "namespace", True),
        ("item", "namespace", -1),
        ("item", "namespace", 127),
        ("item", "size", 1.5),
        ("item", "size", True),
        ("item", "size", 18446744073709551616),
        ("item", "compress", 0),
        ("item", "front", None),
        ("item", "streaming", 1),
        ("item", "title", None),
        ("item", "content", "\ud800"),
        ("item", "path", "x\x85"),
        ("config", "cluster_size_target", True),
        ("config", "max_in_flight_bytes", -1),
        ("config", "compression_level", 2147483648),
        ("config", "_indexing_requested", None),
        ("illustration", "size", 4294967296),
        ("illustration", "size", -1),
    ],
)
def test_scalar_errors_atomic(kind, field, value, tmp_path):
    rec = {"kind": "config"} if kind == "config" else _item()
    if kind == "illustration":
        rec = {"kind": "illustration", "size": 48, "body_b64": "UE5H"}
    rec[field] = value
    dest = tmp_path / "out.zim"
    dest.write_bytes(b"OLD")
    with pytest.raises(p.PackError):
        p.pack(manifest(tmp_path, [rec]), dest, threads=1)
    assert dest.read_bytes() == b"OLD"
    assert not list(tmp_path.glob(".streetzim-pack-*"))


def test_file_identity_mutation_and_replacement_detected(tmp_path):
    file = tmp_path / "body"
    file.write_bytes(b"original")
    original_info = file.stat()
    body = p._body({"file": str(file), "streaming": True, "size": 8})
    file.write_bytes(b"changed!")
    # Detection can only observe stat identity. Rapid same-size writes can
    # retain mtime on Linux; explicitly change it instead of racing the clock.
    os.utime(file, ns=(original_info.st_atime_ns, original_info.st_mtime_ns + 1))
    with pytest.raises(p.PackError):
        list(body.chunks(threading.Event()))
    file.write_bytes(b"original")
    body = p._body({"file": str(file), "streaming": True, "size": 8})
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"original")
    replacement.replace(file)
    with pytest.raises(p.PackError):
        list(body.chunks(threading.Event()))


def test_file_growth_after_first_chunk_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "CHUNK", 2)
    file = tmp_path / "body"
    file.write_bytes(b"abcdef")
    body = p._body({"file": str(file), "streaming": True, "size": 6})
    parts = body.chunks(threading.Event())
    assert bytes(next(parts)) == b"ab"
    with file.open("ab") as out:
        out.write(b"GROWN")
    with pytest.raises(p.PackError):
        list(parts)


def test_line_limit_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "MAX_RECORD", 64)
    dest = tmp_path / "out.zim"
    dest.write_bytes(b"OLD")
    with pytest.raises(p.PackError, match="exceeds"):
        p.pack(manifest(tmp_path, [_item("body", "x" * 100)]), dest, threads=1)
    assert dest.read_bytes() == b"OLD"


def test_html_listing_exact_and_raw_x_namespace(tmp_path):
    rows = [
        {"kind": "config", "compression": "zstd", "main_path": "redirect"},
        _item("z", "Z", mime='TEXT/HTML; charset="utf-8"', title="é"),
        _item("a", "A", mime="text/html", title="same", front=False),
        _item("b", "B", mime="text/plain", title="same", front=True),
        {"kind": "redirect", "path": "redirect", "target": "z", "title": "same"},
        _item("not.html", "R", mime="text/html", namespace=88, compress=True),
    ]
    _, a = archive(tmp_path, rows)
    listing = next(
        e
        for e in a["entries"]
        if e["namespace"] == 88 and e["path"] == "listing/titleOrdered/v1"
    )
    assert listing["listing"] == [(67, "a"), (67, "redirect"), (67, "z")]
    assert all(e["compression"] == 1 for e in a["entries"] if e["namespace"] == 88)
    assert a["main"] == (87, "mainPage")


def test_control_validation_allows_format_unicode(tmp_path):
    _, a = archive(
        tmp_path, [_item("zero\u200dwidth", "text", title="soft\u00adhyphen")]
    )
    e = next(e for e in a["entries"] if e["namespace"] == 67)
    assert e["path"] == "zero\u200dwidth" and e["title"] == "soft\u00adhyphen"


@pytest.mark.parametrize("space", ["\u00a0", "\u2007", "\u202f", "\u3000"])
def test_unicode_manifest_whitespace_and_comments(space, tmp_path):
    source = tmp_path / "manifest"
    source.write_text(
        space + "# comment" + space + "\n" + space + json.dumps(_item()) + space
    )
    p.pack(source, tmp_path / "out.zim", threads=1)
    _inspect(tmp_path / "out.zim")


def test_counter_order_follows_content_mime_first_use(tmp_path):
    _, a = archive(
        tmp_path,
        [
            {
                "kind": "metadata",
                "name": "Other",
                "mimetype": "application/json",
                "value": "{}",
            },
            _item("a", "x", mime="text/plain"),
            _item("b", "{}", mime="application/json"),
        ],
    )
    counter = next(
        e for e in a["entries"] if e["namespace"] == 77 and e["path"] == "Counter"
    )
    assert bytes.fromhex(counter["body_hex"]) == b"text/plain=1;application/json=1"


def test_many_distinct_buckets_do_not_retain_all_bodies(tmp_path):
    records = [
        {
            "kind": "config",
            "compression": "none",
            "cluster_strategy": "by_first_path_segment",
            "cluster_size_target": 1 << 20,
            "max_in_flight_bytes": 4096,
        }
    ]
    records.extend(_item(f"segment{i}/body", "x" * 128) for i in range(200))
    stats = p.pack(manifest(tmp_path, records), tmp_path / "out.zim", threads=2)
    assert stats["peak_open_buckets"] <= p.MAX_BUCKETS
    assert stats["peak_bucket_bytes"] <= 4096
    assert stats["peak_in_flight_bytes"] <= 4096
    _inspect(tmp_path / "out.zim")


@pytest.mark.parametrize(
    "strategy", ["by_mime", "by_extension", "by_first_path_segment"]
)
def test_cyclic_keys_pack_compactly_with_bounded_buckets(tmp_path, strategy):
    records = [
        {
            "kind": "config",
            "compression": "zstd",
            "compression_level": 1,
            "cluster_strategy": strategy,
            "cluster_size_target": 64 << 10,
            "max_in_flight_bytes": 128 << 10,
        }
    ]
    for index in range(1008):
        key = index % 72
        records.append(
            _item(
                f"key{key}/body-{index}.extension{key}",
                "x" * 128,
                mime=f"application/x-key-{key}",
            )
        )
    output = tmp_path / "out.zim"
    stats = p.pack(manifest(tmp_path, records), output, threads=2)
    assert stats["clusters"] < 200
    assert stats["peak_open_buckets"] <= p.MAX_BUCKETS
    assert stats["peak_bucket_bytes"] <= 128 << 10
    assert stats["peak_in_flight_bytes"] <= 128 << 10
    result = _inspect(output)
    content = [entry for entry in result["entries"] if entry["namespace"] == 67]
    assert len(content) == 1008
    assert all(
        entry["digest"] == hashlib.sha256(b"x" * 128).hexdigest() for entry in content
    )


def test_existing_key_append_respects_global_byte_budget(tmp_path):
    records = [
        {
            "kind": "config",
            "compression": "zstd",
            "compression_level": 1,
            "cluster_strategy": "by_first_path_segment",
            "cluster_size_target": 64 << 10,
            "max_in_flight_bytes": 1024,
        },
        _item("a/one", "a" * 640),
        _item("b/one", "b" * 128),
        _item("b/two", "c" * 400),
    ]
    output = tmp_path / "out.zim"
    stats = p.pack(manifest(tmp_path, records), output, threads=2)
    assert stats["peak_bucket_bytes"] <= 1024
    assert stats["peak_in_flight_bytes"] <= 1024
    result = _inspect(output)
    content = {
        entry["path"]: entry["digest"]
        for entry in result["entries"]
        if entry["namespace"] == 67
    }
    assert content == {
        "a/one": hashlib.sha256(b"a" * 640).hexdigest(),
        "b/one": hashlib.sha256(b"b" * 128).hexdigest(),
        "b/two": hashlib.sha256(b"c" * 400).hexdigest(),
    }


def test_many_zero_bodies_limited_by_blob_count(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "MAX_BLOBS", 8)
    _, a = archive(tmp_path, [_item(str(i), "") for i in range(25)])
    assert sum(e["namespace"] == 67 for e in a["entries"]) == 25
    clusters = raw_clusters(tmp_path / "out.zim")
    grouped = {}
    for (ns, _name), (cluster, blob) in clusters.items():
        if ns == 67:
            grouped.setdefault(cluster, []).append(blob)
    assert max(map(len, grouped.values())) <= 8


def test_deep_redirect_chain_without_recursion(tmp_path):
    # Listing eligibility exercises 10000 links without native get_item walks.
    records = [_item("end", "html", mime="text/html")]
    records.extend(
        {
            "kind": "redirect",
            "path": f"r{i:05d}",
            "target": ("end" if i == 9999 else f"r{i + 1:05d}"),
        }
        for i in range(10000)
    )
    dest = tmp_path / "out.zim"
    p.pack(manifest(tmp_path, records), dest, threads=1)
    result = _inspect(dest)
    listing = next(
        e
        for e in result["entries"]
        if e["namespace"] == 88 and e["path"] == "listing/titleOrdered/v1"
    )
    assert len(listing["listing"]) == 10001


@pytest.mark.parametrize("codec", ["none", "xz", "zstd"])
@pytest.mark.parametrize(
    "strategy", ["single", "by_mime", "by_extension", "by_first_path_segment"]
)
def test_codecs_strategies_unicode_and_namespace_readback(tmp_path, codec, strategy):
    records = [
        {
            "kind": "config",
            "compression": codec,
            "compression_level": 3,
            "cluster_strategy": strategy,
            "cluster_size_target": 32,
            "max_in_flight_bytes": 1,
            "main_path": "home",
        },
        _item("index.html", "<html>map</html>", mime="text/html", title="Same"),
        _item("s.a/b", "dot before slash"),
        _item(".a", "extension"),
        _item("𐀀", "astral", title="𐀀"),
        _item("\ue000", "BMP", title="\ue000"),
        _item("e\u0301", "decomposed", title="e\u0301"),
        _item("é", "composed", title="é"),
        _item("日本\u200d🌎", "Unicode", title="\u200d"),
        _item("same", "content"),
        _item("same", "metadata", namespace=77),
        _item("same", "index", namespace=88, compress=True),
        _item("same", "custom", namespace=89),
        _item("raw.bin", "RAW", compress=False),
        _item("empty", ""),
        {"kind": "redirect", "path": "home", "target": "alias", "title": "Same"},
        {"kind": "redirect", "path": "alias", "target": "index.html"},
        {"kind": "metadata", "name": "Binary", "body_b64": "AP8="},
        {"kind": "illustration", "size": 48, "body_b64": "UE5H"},
    ]
    _, result = archive(tmp_path, records)
    entries = {
        (entry["namespace"], entry["path"]): entry for entry in result["entries"]
    }
    expected_code = {"none": 1, "xz": 4, "zstd": 5}[codec]
    for record in records:
        if record["kind"] == "item":
            key = record.get("namespace", 67), record["path"]
            entry = entries[key]
            assert (
                entry["digest"]
                == hashlib.sha256(record["content"].encode()).hexdigest()
            )
            assert entry["title"] == (record.get("title") or record["path"])
            expected = (
                1 if key[0] == 88 or record.get("compress") is False else expected_code
            )
            assert entry["compression"] == expected
    assert entries[(67, "home")]["target"] == (67, "alias")
    assert entries[(67, "alias")]["target"] == (67, "index.html")
    assert entries[(77, "Binary")]["digest"] == hashlib.sha256(b"\x00\xff").hexdigest()
    assert entries[(77, "Illustration_48x48@1")]["mime"] == "image/png"
    assert result["main"] == (87, "mainPage")


def test_default_manifest_command_works_outside_checkout(tmp_path, monkeypatch):
    from cloud import manifest_writer

    monkeypatch.delenv("STREETZIM_PACK_BIN", raising=False)
    monkeypatch.setenv("STREETZIM_MANIFEST_ZSTD", "1")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "irrelevant-package-root"))
    monkeypatch.chdir(tmp_path)
    creator = manifest_writer.ManifestCreator("out.zim", compression_level=3)
    creator.config_nbworkers(1)
    creator.set_mainpath("index.html")
    with creator:
        creator.add_item(
            SimpleNamespace(
                _path="index.html", _title="Map", _mimetype="text/html", _data=b"MAP"
            )
        )
    assert _inspect(tmp_path / "out.zim")["main"] == (87, "mainPage")
    assert not creator._stage_dir.exists()


def test_partial_failing_override_preserves_output_and_recovery(tmp_path, monkeypatch):
    from cloud import manifest_writer

    executable = tmp_path / "override"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import pathlib,sys\n"
        "pathlib.Path(sys.argv[2]).write_bytes(b'PARTIAL')\n"
        "sys.exit(2)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("STREETZIM_PACK_BIN", str(executable))
    output = tmp_path / "out.zim"
    output.write_bytes(b"OLD")
    creator = manifest_writer.ManifestCreator(str(output))
    with pytest.raises(RuntimeError, match="exit 2"), creator:
        creator.add_metadata("Title", "Map")
    assert output.read_bytes() == b"OLD"
    assert creator._manifest_path.exists()
    assert (creator._stage_dir / "packed.zim").read_bytes() == b"PARTIAL"


def test_adapter_publication_failure_preserves_candidate(tmp_path, monkeypatch):
    from cloud import manifest_writer

    monkeypatch.delenv("STREETZIM_PACK_BIN", raising=False)
    output = tmp_path / "out.zim"
    output.write_bytes(b"OLD")
    creator = manifest_writer.ManifestCreator(str(output), compression_level=3)
    creator.config_nbworkers(1)

    def failed_replace(source, destination):
        raise PermissionError("forced publication failure")

    monkeypatch.setattr(manifest_writer.os, "replace", failed_replace)
    with pytest.raises(PermissionError, match="forced publication failure"), creator:
        creator.add_metadata("Title", "Map")
    assert output.read_bytes() == b"OLD"
    assert creator._manifest_path.exists()
    _inspect(creator._stage_dir / "packed.zim")


@pytest.mark.parametrize(
    "damage",
    [
        "magic",
        "main",
        "url",
        "title",
        "duplicate-cluster",
        "cluster-info",
        "raw-table",
        "raw-blob",
        "namespace",
        "redirect",
        "mime-ascii",
        "mime-syntax",
    ],
)
def test_structural_gate_rejects_serialized_damage(tmp_path, damage):
    path, _ = archive(
        tmp_path,
        [
            {"kind": "config", "compression": "none", "cluster_size_target": 1},
            _item("a", "BODY", mime="text/html"),
            _item("b", "SECOND"),
            {"kind": "redirect", "path": "alias", "target": "a"},
        ],
    )
    data = bytearray(path.read_bytes())
    header = struct.unpack("<IHH16sIIQQQQIIQ", data[:80])
    count, urls, titles, pointers, mimes = (
        header[4],
        header[6],
        header[7],
        header[8],
        header[9],
    )
    (article,) = struct.unpack_from("<Q", data, urls)
    (redirect,) = struct.unpack_from("<Q", data, urls + 8)
    (cluster,) = struct.unpack_from("<Q", data, pointers)
    if damage == "magic":
        struct.pack_into("<I", data, 0, 0)
    elif damage == "main":
        struct.pack_into("<I", data, 64, 0)
    elif damage == "url":
        struct.pack_into("<Q", data, urls, article + 1)
    elif damage == "title":
        data[titles + 4 : titles + 8] = data[titles : titles + 4]
    elif damage == "duplicate-cluster":
        data[pointers + 8 : pointers + 16] = data[pointers : pointers + 8]
    elif damage == "cluster-info":
        data[cluster] = 255
    elif damage == "raw-table":
        struct.pack_into("<I", data, cluster + 1, 4)
    elif damage == "raw-blob":
        struct.pack_into("<I", data, article + 12, 0xFFFFFFFF)
    elif damage == "namespace":
        data[article + 3] = 32
    elif damage == "redirect":
        struct.pack_into("<I", data, redirect + 8, count)
    elif damage == "mime-ascii":
        data[mimes] = 255
    else:
        data[data.index(b"/", mimes)] = ord("!")
    with (
        sqlite3.connect(":memory:") as db,
        pytest.raises((p.PackError, UnicodeError, sqlite3.IntegrityError)),
    ):
        p._verify_structure(io.BytesIO(data), db)
