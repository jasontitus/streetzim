"""--mbtiles-url: the Zimfarm flag, the resumable download and its checks,
and the cut of a larger MBTiles to the area (streetzim/mbtiles.py)."""
from __future__ import annotations

import json
import math
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streetzim import cli, mbtiles  # noqa: E402
from tests.mbtiles_fixture import make_mbtiles, read_tiles  # noqa: E402

DEF = json.loads((ROOT / "offliner-definition.json").read_text())
MONACO = (7.40, 43.72, 7.44, 43.76)
FIJI = (172.8, -23.2, -176.5, -11.2)          # across the antimeridian


# ------------------------------------------------ maps2zim's rule, verbatim


def m2z_tile_to_bbox(z, x, y):
    n = 2.0**z
    lon_min = (x / n) * 360.0 - 180.0
    lon_max = ((x + 1) / n) * 360.0 - 180.0
    lat_max = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_min = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return (lon_min, lat_min, lon_max, lat_max)


def m2z_intersects(bbox, z, x, y):
    """maps2zim TileFilter.tile_intersects; its box across the antimeridian
    has min_lon > max_lon."""
    west, south, east, north = m2z_tile_to_bbox(z, x, y)
    min_lon, min_lat, max_lon, max_lat = bbox
    if north < min_lat or south > max_lat:
        return False
    if min_lon > max_lon:
        return east >= min_lon or west <= max_lon
    return not (east < min_lon or west > max_lon)


def m2z_box(bbox):
    w, s, e, n = mbtiles.area.normalize(bbox)
    return (w, s, e - 360.0 if e > 180 else e, n)


def selected(bbox, z):
    return {(x, y) for c0, c1, r0, r1 in mbtiles.tile_ranges(bbox, z)
            for x in range(c0, c1 + 1) for y in range(r0, r1 + 1)}


BOXES = [MONACO, FIJI, (0.0, 0.0, 90.0, 45.0),          # on tile edges
         (-180.0, -90.0, 180.0, 90.0), (179.9, 60.0, -179.9, 70.0),
         (-10.0, 85.2, 10.0, 89.0), (-73.7, 40.5, -73.6, 40.6)]


@pytest.mark.parametrize("bbox", BOXES)
def test_tile_ranges_are_maps2zims_rule(bbox):
    box = m2z_box(bbox)
    for z in range(0, 8):                     # every tile of the world
        n = 1 << z
        want = {(x, y) for x in range(n) for y in range(n) if m2z_intersects(box, z, x, y)}
        assert selected(bbox, z) == want, z
    for z in range(8, 15):                    # the edges: a margin round each block
        n = 1 << z
        ranges = mbtiles.tile_ranges(bbox, z)
        cand_x = {(x + d) % n for c0, c1, _, _ in ranges for x in (c0, c1)
                  for d in range(-3, 4)}
        cand_y = {y + d for _, _, r0, r1 in ranges for y in (r0, r1)
                  for d in range(-3, 4) if 0 <= y + d < n}
        if not ranges:                         # nothing selected: check the poles
            cand_x, cand_y = {0, n // 2, n - 1}, {0, 1, n - 2, n - 1}
        for x in cand_x:
            for y in cand_y:
                assert mbtiles.tile_touches(bbox, z, x, y) == m2z_intersects(box, z, x, y), \
                    (z, x, y)


def test_tile_ranges_by_hand():
    assert mbtiles.tile_ranges(MONACO, 0) == [(0, 0, 0, 0)]
    # Fiji at z1: both western... and eastern columns, the southern row.
    assert mbtiles.tile_ranges(FIJI, 1) == [(0, 1, 1, 1)]
    # z3: columns 7 (to 180) and 0 (from -180), not joined.
    assert mbtiles.tile_ranges(FIJI, 3) == [(0, 0, 4, 4), (7, 7, 4, 4)]
    assert mbtiles.tile_ranges((-10.0, 85.2, 10.0, 89.0), 3) == []   # north of Mercator


# ------------------------------------------------ the cut, on a fixture file


def _window(bbox, z, margin):
    """The tiles around bbox's selection at z, `margin` tiles each way."""
    n = 1 << z
    out = set()
    for c0, c1, r0, r1 in mbtiles.tile_ranges(bbox, z):
        for x in range(c0 - margin, c1 + margin + 1):
            for y in range(max(r0 - margin, 0), min(r1 + margin, n - 1) + 1):
                out.add((z, x % n, y))
    return out


@pytest.mark.parametrize("ofm", [True, False])
@pytest.mark.parametrize("bbox", [MONACO, FIJI])
def test_cut_keeps_exactly_the_touching_tiles(tmp_path, bbox, ofm):
    world = set()
    for z in range(0, 15):
        world |= _window(bbox, z, 3)
    src = make_mbtiles(tmp_path / "big.mbtiles", world, ofm=ofm)
    counts = mbtiles.cut(src, tmp_path / "cut.mbtiles", bbox)
    got = read_tiles(tmp_path / "cut.mbtiles")
    box = m2z_box(bbox)
    for z in range(0, 15):
        want = {t for t in world if t[0] == z and m2z_intersects(box, *t)}
        assert {t for t in got if t[0] == z} == want, z
        assert counts[z] == len(want)
    assert len(got) < len(world)
    if bbox == FIJI:                          # both sides of the antimeridian
        assert {x for z, x, _ in got if z == 14} >= {0, (1 << 14) - 1}
    # Tile data and metadata come across unchanged.
    conn = sqlite3.connect(str(tmp_path / "cut.mbtiles"))
    for z, x, r, data in conn.execute("SELECT * FROM tiles"):
        assert data == f"{z}/{x}/{(1 << z) - 1 - r}".encode()
    assert dict(conn.execute("SELECT * FROM metadata"))["name"] == "OpenFreeMap"
    conn.close()


def test_cut_is_an_index_search(tmp_path):
    for ofm in (True, False):
        src = make_mbtiles(tmp_path / f"{ofm}.mbtiles", [(0, 0, 0)], ofm=ofm)
        plan = mbtiles.query_plan(src)
        assert all(p.startswith("SEARCH") for p in plan), plan
        assert "tile_column=? AND tile_row>? AND tile_row<?" in plan[0], plan


def test_covers_more_reads_bounds():
    meta = {"bounds": "7.40858,43.48382,7.59567,43.75293"}
    assert mbtiles.covers_more(meta, MONACO)
    assert not mbtiles.covers_more(meta, (7.0, 43.0, 8.0, 44.0))
    assert mbtiles.covers_more({}, (7.0, 43.0, 8.0, 44.0))
    assert not mbtiles.covers_more({"bounds": "178,-20,179,-18"}, FIJI)
    assert not mbtiles.covers_more({"bounds": "-179,-20,-178,-18"}, FIJI)
    assert mbtiles.covers_more({"bounds": "-180,-85,180,85"}, FIJI)


def test_no_area_keeps_every_tile(tmp_path):
    tiles = {(z, x, y) for z in range(0, 4) for x in range(1 << z) for y in range(1 << z)}
    src = make_mbtiles(tmp_path / "all.mbtiles", tiles)
    path, meta = cli.prepare_mbtiles(src, None, tmp_path / "work", None)
    assert path == src and meta["name"] == "OpenFreeMap"
    assert not (tmp_path / "work").exists()
    # and the builder, given no area, loads all of them, as before
    from streetzim.tiles import extract_tiles_from_mbtiles
    loaded, _ = extract_tiles_from_mbtiles(src)
    assert set(loaded) == tiles


def test_prepare_cuts_only_a_larger_file(tmp_path, capsys):
    world = set()
    for z in range(0, 15):
        world |= _window(MONACO, z, 2)
    src = make_mbtiles(tmp_path / "m.mbtiles", world,
                       meta={"name": "OpenFreeMap", "bounds": "7.3,43.6,7.6,43.9"})
    path, _ = cli.prepare_mbtiles(src, MONACO, tmp_path / "work", 12)
    assert path == tmp_path / "work" / "area.mbtiles"
    assert max(z for z, _, _ in read_tiles(path)) == 12
    out = capsys.readouterr().out
    assert "MBTiles: OpenFreeMap" in out and "Cut to the area" in out
    path, _ = cli.prepare_mbtiles(src, (7.0, 43.0, 8.0, 44.0), tmp_path / "w2", None)
    assert path == src


# ------------------------------------------------ the flag and the download


def test_flag_offered_to_zimfarm_as_a_url():
    f = DEF["flags"]["mbtiles_url"]
    assert f["type"] == "url" and f["required"] is False
    assert "mbtiles" not in DEF["flags"]            # the local path stays CLI-only


class _Server:
    """A local HTTP server for `files`, with Range requests (unless
    ranges=False) and, optionally, a first GET that stops after N bytes."""

    def __init__(self, files, *, ranges=True, drop_after=None):
        self.files, self.ranges, self.drop_after = files, ranges, drop_after
        self.gets: list[str | None] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _head(self, body, status=200, extra=None):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("ETag", '"v1"')
                self.send_header("Last-Modified", "Sun, 27 Sep 2026 20:00:00 GMT")
                if outer.ranges:
                    self.send_header("Accept-Ranges", "bytes")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()

            def do_HEAD(self):
                self._head(outer.files[self.path])

            def do_GET(self):
                data = outer.files[self.path]
                rng = self.headers.get("Range")
                outer.gets.append(rng)
                if rng and outer.ranges:
                    start = int(rng.split("=")[1].rstrip("-"))
                    body = data[start:]
                    self.send_response(206)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Content-Range",
                                     f"bytes {start}-{len(data) - 1}/{len(data)}")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._head(data)
                if outer.drop_after is not None and len(outer.gets) == 1:
                    self.wfile.write(data[:outer.drop_after])
                    self.wfile.flush()
                    self.connection.shutdown(2)
                    return
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def local_only(monkeypatch):
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _args(**kw):
    ns = cli.build_parser().parse_args(["--name", "n", "--title", "t", "--description", "d",
                                        "--bbox", "7.4,43.72,7.44,43.76"])
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


@pytest.fixture
def mbt_bytes(tmp_path):
    world = set()
    for z in range(0, 15):
        world |= _window(MONACO, z, 2)
    return make_mbtiles(tmp_path / "src.mbtiles", world).read_bytes()


def test_download_checks_and_reuses(tmp_path, local_only, mbt_bytes):
    srv = _Server({"/t.mbtiles": mbt_bytes, "/x.mbtiles": b"<html>not found</html>" * 10})
    try:
        dl = tmp_path / "dl"
        path, url = cli.mbtiles_source(_args(mbtiles_url=srv.base + "/t.mbtiles"), dl)
        assert url == srv.base + "/t.mbtiles" and path.parent == dl / "mbtiles"
        assert path.read_bytes() == mbt_bytes and srv.gets == [None]
        assert mbtiles.check(path)["name"] == "OpenFreeMap"
        cli.mbtiles_source(_args(mbtiles_url=srv.base + "/t.mbtiles"), dl)
        assert srv.gets == [None]                        # unchanged upstream: reused
        # A pre-seeded file of the upstream size is used without downloading.
        seeded = tmp_path / "seed"
        target = seeded / "mbtiles" / cli._name_of_url(srv.base + "/t.mbtiles")
        target.parent.mkdir(parents=True)
        target.write_bytes(mbt_bytes)
        cli.mbtiles_source(_args(mbtiles_url=srv.base + "/t.mbtiles"), seeded)
        assert srv.gets == [None]
        # Not an MBTiles: refused from the first bytes.
        with pytest.raises(ValueError, match="not an MBTiles"):
            cli.mbtiles_source(_args(mbtiles_url=srv.base + "/x.mbtiles"), dl)
    finally:
        srv.close()


def test_download_resumes(tmp_path, local_only):
    # Bigger than zimscraperlib's 1 MB blocks, so some arrive before the drop.
    # (mbtiles_source only downloads; the tables are checked afterwards.)
    import random
    mbt_bytes = mbtiles.SQLITE_MAGIC + random.Random(1).randbytes(3_000_000)
    srv = _Server({"/t.mbtiles": mbt_bytes}, drop_after=2_500_000)
    try:
        dl = tmp_path / "dl"
        with pytest.raises(ValueError):          # the dropped connection
            cli.mbtiles_source(_args(mbtiles_url=srv.base + "/t.mbtiles"), dl)
        part = dl / "mbtiles" / (cli._name_of_url(srv.base + "/t.mbtiles") + ".part")
        assert 0 < part.stat().st_size < len(mbt_bytes)
        have = part.stat().st_size
        path, _ = cli.mbtiles_source(_args(mbtiles_url=srv.base + "/t.mbtiles"), dl)
        assert srv.gets == [None, f"bytes={have}-"]
        assert path.read_bytes() == mbt_bytes and not part.exists()
    finally:
        srv.close()


def test_download_restarts_without_range_support(tmp_path, local_only, mbt_bytes):
    srv = _Server({"/t.mbtiles": mbt_bytes}, ranges=False, drop_after=100)
    try:
        dl = tmp_path / "dl"
        with pytest.raises(ValueError):          # the dropped connection
            cli.mbtiles_source(_args(mbtiles_url=srv.base + "/t.mbtiles"), dl)
        path, _ = cli.mbtiles_source(_args(mbtiles_url=srv.base + "/t.mbtiles"), dl)
        assert srv.gets == [None, None] and path.read_bytes() == mbt_bytes
    finally:
        srv.close()


def test_file_url_is_used_in_place_and_recorded(tmp_path, mbt_bytes, monkeypatch):
    src = tmp_path / "planet.mbtiles"
    src.write_bytes(mbt_bytes)
    monkeypatch.setattr(cli, "fetch", lambda url, dest: pytest.fail("no download"))
    ns = _args(mbtiles_url=src.as_uri(), routing=False)
    argv, info = cli.plan(ns, tmp_path / "dl", work=tmp_path / "work")
    assert info["mbtiles_cut"] == str(tmp_path / "work" / "area.mbtiles")
    import create_osm_zim
    b = create_osm_zim.build_parser().parse_args(argv)
    assert b.mbtiles == info["mbtiles_cut"] and b.record_tile_source
    assert b.tile_source_url == src.as_uri()
    assert not (tmp_path / "dl").exists()
    with pytest.raises(ValueError, match="not both"):
        cli.plan(_args(mbtiles_url=src.as_uri(), mbtiles=str(src), routing=False),
                 tmp_path / "dl")
    bad = tmp_path / "bad.mbtiles"
    bad.write_bytes(b"x" * 100)
    with pytest.raises(ValueError, match="not an MBTiles"):
        cli.plan(_args(mbtiles_url=bad.as_uri(), routing=False), tmp_path / "dl")
    with pytest.raises(ValueError, match="http"):
        cli.plan(_args(mbtiles_url="ftp://x/y.mbtiles", routing=False), tmp_path / "dl")


def test_source_record_and_credit():
    meta = {"name": "OpenFreeMap", "description": "https://openfreemap.org",
            "version": "3.16.0", "planetiler:version": "0.10.3",
            "planetiler:osm:osmosisreplicationtime": "2026-09-27T20:23:36Z"}
    assert mbtiles.source_record(meta, "https://x/t.mbtiles") == {
        "name": "OpenFreeMap", "version": "3.16.0", "osmDate": "2026-09-27",
        "generator": "planetiler 0.10.3", "homepage": "https://openfreemap.org",
        "url": "https://x/t.mbtiles"}
    assert "url" not in mbtiles.source_record(meta)
    assert mbtiles.license_text(meta) == "Vector tiles: OpenFreeMap (https://openfreemap.org)"
    assert "OSM data 2026-09-27" in mbtiles.describe(meta)


def test_check_rejects_other_sqlite(tmp_path):
    p = tmp_path / "other.sqlite"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE t (a)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="no tiles and metadata"):
        mbtiles.check(p)
