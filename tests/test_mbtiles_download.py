"""--mbtiles-url's download (streetzim/download.py): resume, reuse,
checksums, eviction, locking, and what plan() does with the URL."""
from __future__ import annotations

import hashlib
import json
import random
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streetzim import cli, download, mbtiles  # noqa: E402
from tests.mbtiles_fixture import make_mbtiles  # noqa: E402

A = mbtiles.SQLITE_MAGIC + random.Random(1).randbytes(3_000_000)
B = mbtiles.SQLITE_MAGIC + random.Random(2).randbytes(3_000_000)


class Server:
    """A local HTTP server whose behaviour each test sets: the file and its
    validators, Range support (honoured, ignored, or answered with the wrong
    range), a first GET cut short, HEAD refused, a SHA256SUMS file."""

    def __init__(self, data: bytes, path: str = "/t.mbtiles"):
        self.files = {path: data}
        self.etag, self.lm = '"v1"', "Sun, 27 Sep 2026 20:00:00 GMT"
        self.accept_ranges = True       # advertise ranges
        self.ranges = "honour"          # or "ignore" (200) or "wrong" (206 elsewhere)
        self.drop_after = None          # the next GET stops after this many bytes
        self.fail_after_drop = 0        # this many GETs after that one answered 503
        self.head_ok = True
        self.gets: list[tuple[str | None, str | None]] = []
        o = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _hdrs(self, n):
                self.send_header("Content-Length", str(n))
                if o.etag:
                    self.send_header("ETag", o.etag)
                if o.lm:
                    self.send_header("Last-Modified", o.lm)
                if o.accept_ranges:
                    self.send_header("Accept-Ranges", "bytes")

            def do_HEAD(self):
                if not o.head_ok:
                    self.send_response(405)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self._hdrs(len(o.files[self.path]))
                self.end_headers()

            def do_GET(self):
                data = o.files[self.path]
                rng = self.headers.get("Range")
                if o.fail_after_drop and o.drop_after is None:
                    o.fail_after_drop -= 1
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.endswith(".mbtiles"):
                    o.gets.append((rng, self.headers.get("If-Range")))
                if rng and o.ranges != "ignore":
                    lo, _, hi = rng.split("=")[1].partition("-")
                    start = int(lo)
                    end = int(hi) if hi else len(data) - 1
                    body = data[start:end + 1]
                    if o.ranges == "wrong":        # the right length, from elsewhere
                        start += 7
                        body = data[start:end + 1] + b"\0" * 7
                    self.send_response(206)
                    self._hdrs(len(body))
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self._hdrs(len(data))
                self.end_headers()
                if o.drop_after is not None:
                    n, o.drop_after = o.drop_after, None
                    self.wfile.write(data[:n])
                    self.wfile.flush()
                    self.connection.shutdown(2)
                    return
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.url = self.base + path

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def srv(monkeypatch):
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    servers = []

    def make(data=A, path="/t.mbtiles"):
        servers.append(Server(data, path))
        return servers[-1]
    yield make
    for s in servers:
        s.close()


def fetch(s, dl):
    return cli.fetch_resumable(s.url, dl / "mbtiles" / cli._name_of_url(s.url),
                               check_head=cli._check_mbtiles_head)


def paths(s, dl):
    dest = dl / "mbtiles" / cli._name_of_url(s.url)
    part = dest.with_name(dest.name + ".part")
    return dest, part, part.with_name(part.name + ".source.json")


def interrupted(s, dl, at=2_500_000):
    """A first download cut short after `at` bytes; returns the .part size.
    (Where the cut reads as a short body, the immediate retry fails too.)"""
    s.drop_after, s.fail_after_drop = at, 1
    with pytest.raises(Exception):  # noqa: B017 (requests or http.client, by install)
        fetch(s, dl)
    s.fail_after_drop = 0
    return paths(s, dl)[1].stat().st_size


# ------------------------------------------------ resume


def test_resume_sends_if_range_and_appends(srv, tmp_path):
    s = srv()
    have = interrupted(s, tmp_path)
    assert 0 < have < len(A)
    assert fetch(s, tmp_path).read_bytes() == A
    assert s.gets == [(None, None), (f"bytes={have}-", '"v1"')]
    dest, part, part_meta = paths(s, tmp_path)
    assert not part.exists() and not part_meta.exists()


def test_no_resume_when_upstream_changed(srv, tmp_path):
    s = srv()
    interrupted(s, tmp_path)
    s.files["/t.mbtiles"], s.etag = B, '"v2"'          # same size, new version
    assert fetch(s, tmp_path).read_bytes() == B
    assert s.gets[-1] == (None, None)


def test_no_resume_without_validators(srv, tmp_path):
    s = srv()
    s.etag = s.lm = ""
    interrupted(s, tmp_path)
    s.files["/t.mbtiles"] = B                           # changed, and nothing says so
    assert fetch(s, tmp_path).read_bytes() == B
    assert s.gets[-1] == (None, None)


def test_last_modified_alone_is_a_validator(srv, tmp_path):
    s = srv()
    s.etag = ""
    have = interrupted(s, tmp_path)
    assert fetch(s, tmp_path).read_bytes() == A
    assert s.gets[-1] == (f"bytes={have}-", s.lm)


def test_no_resume_without_accept_ranges(srv, tmp_path):
    s = srv()
    s.accept_ranges = False
    interrupted(s, tmp_path, at=100)
    assert fetch(s, tmp_path).read_bytes() == A
    assert s.gets == [(None, None), (None, None)]


def test_a_200_to_a_range_is_the_whole_file_once(srv, tmp_path):
    s = srv()
    interrupted(s, tmp_path)
    s.ranges = "ignore"
    assert fetch(s, tmp_path).read_bytes() == A
    assert len(s.gets) == 2                             # no second full GET


def test_a_range_elsewhere_restarts(srv, tmp_path):
    s = srv()
    have = interrupted(s, tmp_path)
    s.ranges = "wrong"
    assert fetch(s, tmp_path).read_bytes() == A
    assert s.gets[1:] == [(f"bytes={have}-", '"v1"'), (None, None)]


def test_part_longer_than_upstream_starts_over(srv, tmp_path):
    s = srv()
    interrupted(s, tmp_path)
    dest, part, _ = paths(s, tmp_path)
    part.write_bytes(A + b"extra")
    assert fetch(s, tmp_path).read_bytes() == A
    assert s.gets == [(None, None), (None, None)]


def test_complete_part_is_renamed(srv, tmp_path):
    s = srv()
    dest, part, part_meta = paths(s, tmp_path)
    part.parent.mkdir(parents=True)
    part.write_bytes(A)
    part_meta.write_text(json.dumps(cli._source_stamp(s.url)))
    assert fetch(s, tmp_path).read_bytes() == A
    assert s.gets == []


def test_part_from_another_version_is_not_resumed(srv, tmp_path):
    s = srv()
    dest, part, part_meta = paths(s, tmp_path)
    part.parent.mkdir(parents=True)
    part.write_bytes(B[:1000])
    part_meta.write_text(json.dumps({**cli._source_stamp(s.url), "ETag": '"v0"'}))
    assert fetch(s, tmp_path).read_bytes() == A
    assert s.gets == [(None, None)]


def test_not_an_mbtiles_stops_and_leaves_no_part(srv, tmp_path):
    s = srv(b"<html>" + b"x" * 5000)
    with pytest.raises(ValueError, match="not an MBTiles"):
        fetch(s, tmp_path)
    dest, part, part_meta = paths(s, tmp_path)
    assert not part.exists() and not part_meta.exists() and not dest.exists()


def test_head_refused_falls_back_to_a_one_byte_get(srv, tmp_path):
    s = srv()
    s.head_ok = False
    h = download.head(s.url, "t")
    assert h is not None and h["Content-Length"] == str(len(A))
    assert fetch(s, tmp_path).read_bytes() == A


# ------------------------------------------------ reuse


def test_reuse_while_unchanged_and_refresh_when_changed(srv, tmp_path):
    s = srv()
    fetch(s, tmp_path)
    fetch(s, tmp_path)
    assert len(s.gets) == 1
    s.files["/t.mbtiles"], s.etag = B, '"v2"'
    assert fetch(s, tmp_path).read_bytes() == B
    assert len(s.gets) == 2


def test_preseeded_file_of_the_upstream_size_is_used(srv, tmp_path):
    s = srv()
    dest, _, _ = paths(s, tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(A)
    fetch(s, tmp_path)
    assert s.gets == []
    dest.write_bytes(A[:-1])                           # another size: downloaded
    dest.with_name(dest.name + ".source.json").unlink()
    fetch(s, tmp_path)
    assert len(s.gets) == 1 and dest.read_bytes() == A


def test_offline_reuses_what_is_there(srv, tmp_path):
    s = srv()
    fetch(s, tmp_path)
    s.close()
    assert fetch(s, tmp_path).read_bytes() == A


# ------------------------------------------------ OpenFreeMap: checksums, eviction


def test_ofm_urls():
    u = "https://btrfs.openfreemap.com/areas/planet/20260927_001001_pt/tiles.mbtiles"
    assert download.ofm_sums_url(u) == (
        "https://btrfs.openfreemap.com/areas/planet/20260927_001001_pt/SHA256SUMS")
    for other in ("https://example.org/areas/planet/v/tiles.mbtiles",
                  "http://btrfs.openfreemap.com/areas/planet/v/tiles.mbtiles",
                  "https://evil-openfreemap.com/areas/planet/v/tiles.mbtiles",
                  "https://btrfs.openfreemap.com/areas/planet/v/tiles.pmtiles"):
        assert download.ofm_sums_url(other) is None, other


@pytest.fixture
def ofm(srv, monkeypatch):
    """A server laid out like OpenFreeMap, with its SHA256SUMS honoured."""
    s = srv(A, "/areas/monaco/v1/tiles.mbtiles")
    s.files["/areas/monaco/v1/SHA256SUMS"] = (
        f"{hashlib.sha256(A).hexdigest()}  tiles.mbtiles\n").encode()
    monkeypatch.setattr(download, "ofm_sums_url",
                        lambda u: u.rsplit("/", 1)[0] + "/SHA256SUMS"
                        if u.startswith(s.base + "/areas/") else None)
    return s


def test_checksum_rejects_a_corrupt_download(ofm, tmp_path):
    ofm.files["/areas/monaco/v1/SHA256SUMS"] = b"0" * 64 + b"  tiles.mbtiles\n"
    with pytest.raises(OSError, match="SHA-256"):
        fetch(ofm, tmp_path)
    dest, part, _ = paths(ofm, tmp_path)
    assert not dest.exists() and not part.exists()


def test_checksum_decides_a_preseeded_file(ofm, tmp_path):
    dest, _, _ = paths(ofm, tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(B)                                # right size, wrong file
    assert fetch(ofm, tmp_path).read_bytes() == A
    assert len(ofm.gets) == 1


def test_new_version_evicts_the_old_one(ofm, tmp_path, capsys):
    old = fetch(ofm, tmp_path)
    ofm.files["/areas/monaco/v2/tiles.mbtiles"] = B
    ofm.files["/areas/monaco/v2/SHA256SUMS"] = (
        f"{hashlib.sha256(B).hexdigest()}  tiles.mbtiles\n").encode()
    other = tmp_path / "mbtiles" / "unrelated.mbtiles"
    other.write_bytes(b"x")
    ofm.url = ofm.base + "/areas/monaco/v2/tiles.mbtiles"
    new = fetch(ofm, tmp_path)
    assert new.read_bytes() == B and not old.exists() and other.exists()
    assert not old.with_name(old.name + ".source.json").exists()
    assert "Removed older download" in capsys.readouterr().out


# ------------------------------------------------ locking


def test_a_held_lock_is_waited_for(srv, tmp_path):
    s = srv()
    dest, _, _ = paths(s, tmp_path)
    done = threading.Event()
    with download.file_lock(dest):
        with download.file_lock(dest, block=False) as got:
            assert got is False
        t = threading.Thread(target=lambda: (fetch(s, tmp_path), done.set()))
        t.start()
        assert not done.wait(0.5)                      # blocked behind us
    t.join(10)
    assert done.is_set() and dest.read_bytes() == A


# ------------------------------------------------ what plan() makes of the URL


def _args(**kw):
    ns = cli.build_parser().parse_args(["--name", "n", "--title", "t", "--description", "d",
                                        "--bbox", "7.4,43.72,7.44,43.76", "--no-routing"])
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_record_url_drops_secrets_and_paths():
    assert cli.record_url("https://user:pw@host.org:8443/a/t.mbtiles?token=x#f") == \
        "https://host.org:8443/a/t.mbtiles"
    assert cli.record_url("file:///srv/tiles/planet%20v2.mbtiles") == "planet v2.mbtiles"


def test_file_url_is_used_in_place_and_recorded(tmp_path, monkeypatch):
    src = make_mbtiles(tmp_path / "planet.mbtiles", [(0, 0, 0), (14, 8529, 16383 - 5973)])
    monkeypatch.setattr(cli, "fetch", lambda url, dest: pytest.fail("no download"))
    argv, info = cli.plan(_args(mbtiles_url=src.as_uri()), tmp_path / "dl",
                          work=tmp_path / "work")
    assert info["mbtiles_cut"] == str(tmp_path / "work" / "area.mbtiles")
    import create_osm_zim
    b = create_osm_zim.build_parser().parse_args(argv)
    assert b.mbtiles == info["mbtiles_cut"] and b.record_tile_source
    assert b.tile_source_url == "planet.mbtiles"
    assert not (tmp_path / "dl").exists()


@pytest.mark.parametrize("kw, match", [
    ({"mbtiles": "missing.mbtiles"}, "no such file"),
    ({"mbtiles_url": "file:///nonexistent/p.mbtiles"}, "no such file"),
    ({"mbtiles_url": "ftp://x/y.mbtiles"}, "http"),
])
def test_bad_sources_are_clean_errors(tmp_path, kw, match):
    with pytest.raises(ValueError, match=match):
        cli.plan(_args(**kw), tmp_path / "dl")


def test_both_flags_refused(tmp_path):
    src = make_mbtiles(tmp_path / "p.mbtiles", [(0, 0, 0)])
    with pytest.raises(ValueError, match="not both"):
        cli.plan(_args(mbtiles_url=src.as_uri(), mbtiles=str(src)), tmp_path / "dl")


def test_not_an_mbtiles_file_url(tmp_path):
    bad = tmp_path / "bad.mbtiles"
    bad.write_bytes(b"x" * 100)
    with pytest.raises(ValueError, match="not an MBTiles"):
        cli.plan(_args(mbtiles_url=bad.as_uri()), tmp_path / "dl")


def test_cut_removed_when_the_extract_download_fails(tmp_path, monkeypatch):
    src = make_mbtiles(tmp_path / "p.mbtiles", [(0, 0, 0)])

    def fail(url, dest):
        raise OSError("no network")
    monkeypatch.setattr(cli, "fetch", fail)
    ns = _args(mbtiles_url=src.as_uri(), pbf_url="https://example.org/x.pbf")
    with pytest.raises(OSError):
        cli.plan(ns, tmp_path / "dl", work=tmp_path / "work")
    assert not (tmp_path / "work").exists()


def test_main_cleans_owned_cut_on_failure_and_preserves_other_workspace(tmp_path, monkeypatch):
    src = make_mbtiles(tmp_path / "p.mbtiles", [(0, 0, 0)])
    stale = tmp_path / "tmp" / "mbtiles-cut" / "old-run.mbtiles.part"   # may still be active
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    import create_osm_zim
    seen = []

    def builder_fails(argv):
        cut = Path(argv[argv.index("--mbtiles") + 1])
        seen.append((stale.exists(), cut))
        assert cut.exists()
        raise SystemExit(143)                          # e.g. SIGTERM mid-build
    monkeypatch.setattr(create_osm_zim, "main", builder_fails)
    with pytest.raises(SystemExit):
        cli.main(["--name", "n", "--title", "t", "--description", "d",
                  "--bbox", "7.4,43.72,7.44,43.76", "--no-routing", "--profile=basic",
                  "--mbtiles-url", src.as_uri(), "--tmp", str(tmp_path / "tmp"),
                  "--output", str(tmp_path / "out")])
    assert len(seen) == 1 and seen[0][0]
    assert not seen[0][1].exists()
    assert not seen[0][1].parent.exists()
    assert stale.read_bytes() == b"old"


def test_generic_fetch_never_promotes_a_short_body(srv, tmp_path):
    s = srv()
    dest = tmp_path / 'extract.pbf'
    s.drop_after, s.fail_after_drop = 2048, 1
    with pytest.raises(OSError):
        cli.fetch(s.url, dest)
    s.fail_after_drop = 0
    assert not dest.exists()
    assert cli.fetch(s.url, dest).read_bytes() == A


def test_generic_fetch_serializes_parallel_transfers(srv, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    s = srv()
    dest = tmp_path / 'extract.pbf'
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(cli.fetch, s.url, dest) for _ in range(2)]
        assert [f.result().read_bytes() for f in futures] == [A, A]
    assert s.gets == [(None, None)]
    assert not dest.with_name(dest.name + '.part').exists()


def test_matching_stamp_does_not_hide_truncated_cached_file(srv, tmp_path):
    s = srv()
    dest = fetch(s, tmp_path)
    dest.write_bytes(A[:2048])
    assert fetch(s, tmp_path).read_bytes() == A
    assert len(s.gets) == 2


def test_http_headers_are_case_insensitive(monkeypatch, tmp_path):
    from contextlib import contextmanager
    headers = {'etag': '"a"', 'last-modified': 'today', 'content-length': str(len(A)),
               'accept-ranges': 'bytes'}
    monkeypatch.setattr(download, 'head', lambda url, agent: headers)
    assert download.stamp_of(headers) == {'ETag': '"a"', 'Last-Modified': 'today',
                                          'Content-Length': str(len(A))}
    dest = tmp_path / 'tiles.mbtiles'
    part = dest.with_name(dest.name + '.part')
    part.write_bytes(A[:2000])
    part.with_name(part.name + '.source.json').write_text(json.dumps(download.stamp_of(headers)))
    seen = []

    @contextmanager
    def stream(url, request_headers):
        seen.append(request_headers)
        yield 206, {'content-range': f'bytes 2000-{len(A) - 1}/{len(A)}'}, iter([A[2000:]])

    monkeypatch.setattr(download, '_stream', stream)
    assert cli.fetch_resumable('https://example.org/tiles', dest).read_bytes() == A
    assert seen[0]['Range'] == 'bytes=2000-'
    assert seen[0]['If-Range'] == '"a"'


def test_short_non_sqlite_response_is_rejected_before_promotion(monkeypatch, tmp_path):
    from contextlib import contextmanager
    monkeypatch.setattr(download, 'head', lambda url, agent: {'Content-Length': '4'})

    @contextmanager
    def stream(url, request_headers):
        yield 200, {}, iter([b'html'])

    monkeypatch.setattr(download, '_stream', stream)
    dest = tmp_path / 'tiles.mbtiles'
    with pytest.raises(ValueError, match='not SQLite'):
        cli.fetch_resumable('https://example.org/tiles', dest, check_head=cli._check_mbtiles_head)
    assert not dest.exists()
    assert not list(tmp_path.glob('*.part*'))


def test_generic_fetch_refreshes_unstamped_same_size_input(srv, tmp_path):
    s = srv(data=B)
    dest = tmp_path / 'extract.pbf'
    dest.write_bytes(A)
    assert cli.fetch(s.url, dest).read_bytes() == B
    assert s.gets == [(None, None)]


def test_get_length_detects_short_body_when_head_has_no_length(monkeypatch, tmp_path):
    from contextlib import contextmanager
    monkeypatch.setattr(download, 'head', lambda url, agent: {'ETag': '"a"'})

    @contextmanager
    def stream(url, request_headers):
        yield 200, {'content-length': '10'}, iter([b'short'])

    monkeypatch.setattr(download, '_stream', stream)
    dest = tmp_path / 'extract.pbf'
    with pytest.raises(OSError, match='GET body has 5 bytes'):
        cli.fetch('https://example.org/extract', dest)
    assert not dest.exists()


def test_eviction_preserves_lock_inode_for_existing_waiters(ofm, tmp_path):
    import fcntl
    old = fetch(ofm, tmp_path)
    lock = old.with_name(old.name + '.lock')
    # An existing waiter can hold this inode open before eviction releases
    # its own lock. Every later caller must contend on the same inode.
    with open(lock, 'a') as waiter:
        download._evict_other_versions(ofm.base + '/areas/monaco/v2/tiles.mbtiles',
                                      old.with_name(old.name.replace('v1', 'v2')))
        fcntl.flock(waiter, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with download.file_lock(old, block=False) as acquired:
                assert not acquired
        finally:
            fcntl.flock(waiter, fcntl.LOCK_UN)


def test_short_body_with_content_length_is_resumed(monkeypatch, tmp_path):
    # A body that ends cleanly before its Content-Length is an interrupted
    # transfer like any other: the second attempt resumes from what arrived.
    from contextlib import contextmanager
    headers = {'ETag': '"a"', 'Content-Length': str(len(A)), 'Accept-Ranges': 'bytes'}
    monkeypatch.setattr(download, 'head', lambda url, agent: headers)
    seen = []

    @contextmanager
    def stream(url, request_headers):
        seen.append(request_headers.get('Range'))
        if len(seen) == 1:
            yield 200, {'Content-Length': str(len(A))}, iter([A[:5000]])
        else:
            yield 206, {'Content-Range': f'bytes 5000-{len(A) - 1}/{len(A)}',
                        'Content-Length': str(len(A) - 5000)}, iter([A[5000:]])

    monkeypatch.setattr(download, '_stream', stream)
    dest = tmp_path / 'tiles.mbtiles'
    assert cli.fetch_resumable('https://example.org/tiles', dest,
                               check_head=cli._check_mbtiles_head).read_bytes() == A
    assert seen == [None, 'bytes=5000-']
    assert not list(tmp_path.glob('*.part*'))


def test_short_body_without_ranges_is_fetched_again(monkeypatch, tmp_path):
    from contextlib import contextmanager
    monkeypatch.setattr(download, 'head',
                        lambda url, agent: {'Content-Length': str(len(A))})
    seen = []

    @contextmanager
    def stream(url, request_headers):
        seen.append(request_headers.get('Range'))
        body = A[:5000] if len(seen) == 1 else A
        yield 200, {'Content-Length': str(len(A))}, iter([body])

    monkeypatch.setattr(download, '_stream', stream)
    dest = tmp_path / 'extract.pbf'
    assert cli.fetch_resumable('https://example.org/x', dest).read_bytes() == A
    assert seen == [None, None]


def test_bad_cached_file_is_replaced_once(srv, tmp_path):
    # A cached download that fails the format check (e.g. a pre-seeded
    # HTML error page of the upstream size) used to fail every later build.
    s = srv()
    dest, part, _ = paths(s, tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"<html>" + b"x" * (len(A) - 6))
    meta = dest.with_name(dest.name + ".source.json")
    meta.write_text(json.dumps(cli._source_stamp(s.url)))
    assert fetch(s, tmp_path).read_bytes() == A
    assert s.gets == [(None, None)]
    assert json.loads(meta.read_text()) == cli._source_stamp(s.url)
    # When upstream is bad too, the build fails clearly and nothing is kept.
    bad = srv(b"<html>" + b"x" * 5000, path="/u.mbtiles")
    bad_dest = paths(bad, tmp_path)[0]
    bad_dest.write_bytes(b"<html>" + b"y" * 5000)
    with pytest.raises(ValueError, match="not an MBTiles"):
        fetch(bad, tmp_path)
    assert not bad_dest.exists() and len(bad.gets) == 1
