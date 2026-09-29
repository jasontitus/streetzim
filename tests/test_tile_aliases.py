"""Identical tiles are stored once: the first copy is an item, each later copy
a ZIM alias (a second dirent on the same cluster/blob; streetzim/tile_alias.py).

Checked on a real create_zim build from a synthetic MBTiles with duplicates,
plus a satellite cache with duplicate files:
  * every tile position still resolves, to exactly the bytes it had;
  * the duplicates share one blob (read from the dirents themselves, since
    python-libzim does not expose cluster/blob numbers);
  * STREETZIM_TILE_ALIASES=0 writes every tile as its own blob;
  * the choice of first copy is deterministic;
  * cloud/repackage_zim.py keeps the aliases;
  * the /drive PWA's own reader (web/drive/zim-reader.js) reads aliases.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("libzim.writer")
from libzim.reader import Archive  # noqa: E402
from libzim.writer import Creator  # noqa: E402

if not hasattr(Creator, "add_alias"):
    pytest.skip("python-libzim without Creator.add_alias", allow_module_level=True)

BBOX = (7.40, 43.72, 7.44, 43.76)          # Monaco
SEA = b"\x1a\x35sea-tile" + bytes(40)     # the 55-byte ocean tile's shape
LAND = b"\x1a\x20forest-tile" + bytes(30)


def _mbtiles(path: Path) -> dict[str, bytes]:
    """z13/z14 over Monaco; SEA and LAND each repeat. Stored gzipped, as
    tilemaker writes them. Returns {zim path: decompressed bytes}."""
    tiles = {}
    for z, xs, ys in ((13, range(4264, 4266), range(2986, 2988)),
                      (14, range(8528, 8531), range(5972, 5976))):
        for x in xs:
            for y in ys:
                if (x + y) % 3 == 0:
                    data = SEA
                elif (x + y) % 3 == 1:
                    data = LAND
                else:
                    data = f"unique {z}/{x}/{y}".encode() * 3
                tiles[(z, x, y)] = data
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                 "tile_row INTEGER, tile_data BLOB)")
    conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    conn.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", [
        (z, x, (1 << z) - 1 - y, gzip.compress(d, mtime=0))
        for (z, x, y), d in tiles.items()])
    conn.commit()
    conn.close()
    return {f"tiles/{z}/{x}/{y}.pbf": d for (z, x, y), d in tiles.items()}


def _satellite(root: Path) -> dict[str, bytes]:
    """A raster cache: raster tiles go in uncompressed clusters, so a repeat
    costs its full size unless aliased."""
    blue = b"RIFF" + bytes(range(256)) * 8          # 2 KiB, identical
    out = {}
    for x in (8528, 8529, 8530):
        for y in (5973, 5974):
            d = blue if (x, y) != (8529, 5973) else b"RIFFdifferent" * 20
            p = root / "14" / str(x)
            p.mkdir(parents=True, exist_ok=True)
            (p / f"{y}.webp").write_bytes(d)
            out[f"satellite/14/{x}/{y}.webp"] = d
    return out


def _build(tmp_path: Path, name: str, mbtiles: Path, sat: Path) -> Path:
    from streetzim.zim_writer import create_zim
    (tmp_path / "ml.js").write_text("//")
    (tmp_path / "ml.css").write_text("/**/")
    out = tmp_path / name
    create_zim(
        out, tiles=None, tile_metadata={}, mbtiles_path=str(mbtiles),
        fonts={("OpenSansRegular", "0-255"): b"g"},
        maplibre_js_path=str(tmp_path / "ml.js"),
        maplibre_css_path=str(tmp_path / "ml.css"),
        viewer_html_path=str(ROOT / "resources/viewer/index.html"),
        map_config={"name": "Monaco"}, name="OSM - Monaco", bbox=BBOX,
        search_features=[{"name": "Casino", "type": "poi", "subtype": "casino",
                          "lat": 43.739, "lon": 7.428}],
        satellite_dir=str(sat), satellite_max_zoom=14,
        xapian_mode="none", max_zoom=14)
    return out


def dirent_blobs(zim: Path) -> dict[str, tuple[int, int]]:
    """{path: (cluster, blob)} for every content entry, from the dirents."""
    with open(zim, "rb") as fh:
        buf = fh.read()
    entry_count, = struct.unpack_from("<I", buf, 24)
    url_ptr_pos, = struct.unpack_from("<Q", buf, 32)
    out = {}
    for i in range(entry_count):
        off, = struct.unpack_from("<Q", buf, url_ptr_pos + 8 * i)
        mime, _plen, ns = struct.unpack_from("<HBc", buf, off)
        if mime >= 0xFFFE or ns != b"C":          # redirect / deleted / not content
            continue
        cluster, blob = struct.unpack_from("<II", buf, off + 8)
        path = buf[off + 16:buf.index(b"\0", off + 16)].decode()
        out[path] = (cluster, blob)
    return out


def _check_bytes(zim: Path, expected: dict[str, bytes]) -> None:
    arc = Archive(str(zim))
    for path, data in expected.items():
        entry = arc.get_entry_by_path(path)
        assert not entry.is_redirect, path
        assert bytes(entry.get_item().content) == data, path


def _groups(blobs: dict[str, tuple[int, int]], expected: dict[str, bytes]):
    """Paths grouped by the blob they point at."""
    by_blob: dict[tuple[int, int], list[str]] = {}
    for p in expected:
        by_blob.setdefault(blobs[p], []).append(p)
    return by_blob


@pytest.fixture
def inputs(tmp_path):
    pytest.importorskip("mercantile")
    mb = tmp_path / "t.mbtiles"
    expected = _mbtiles(mb)
    expected |= _satellite(tmp_path / "sat")
    return mb, tmp_path / "sat", expected


def test_duplicates_share_one_blob(tmp_path, inputs, monkeypatch):
    monkeypatch.delenv("STREETZIM_TILE_ALIASES", raising=False)
    mb, sat, expected = inputs
    zim = _build(tmp_path, "a.zim", mb, sat)
    _check_bytes(zim, expected)
    blobs = dirent_blobs(zim)
    distinct = {d for d in expected.values()}
    # One blob per distinct content, per mimetype (SEA/LAND are vector only).
    assert len(_groups(blobs, expected)) == len(distinct)
    n_sea = sum(d == SEA for d in expected.values())
    assert n_sea > 1 and len({blobs[p] for p, d in expected.items() if d == SEA}) == 1


def test_env_var_turns_aliases_off(tmp_path, inputs, monkeypatch):
    mb, sat, expected = inputs
    monkeypatch.setenv("STREETZIM_TILE_ALIASES", "0")
    off = _build(tmp_path, "off.zim", mb, sat)
    _check_bytes(off, expected)
    assert len(_groups(dirent_blobs(off), expected)) == len(expected)
    monkeypatch.delenv("STREETZIM_TILE_ALIASES")
    on = _build(tmp_path, "on.zim", mb, sat)
    # 4 duplicate 2 KiB satellite tiles in uncompressed clusters.
    assert off.stat().st_size - on.stat().st_size > 4 * 2000


def test_first_copy_is_deterministic(tmp_path, inputs, monkeypatch):
    monkeypatch.delenv("STREETZIM_TILE_ALIASES", raising=False)
    mb, sat, expected = inputs
    a = dirent_blobs(_build(tmp_path, "1.zim", mb, sat))
    b = dirent_blobs(_build(tmp_path, "2.zim", mb, sat))
    assert {p: a[p] for p in expected} == {p: b[p] for p in expected}


def test_repackage_keeps_aliases(tmp_path, inputs, monkeypatch):
    monkeypatch.delenv("STREETZIM_TILE_ALIASES", raising=False)
    from cloud.repackage_zim import repackage
    mb, sat, expected = inputs
    src = _build(tmp_path, "src.zim", mb, sat)
    dst = tmp_path / "dst.zim"
    repackage(str(src), str(dst), swap_viewer=False)
    _check_bytes(dst, expected)
    assert len(_groups(dirent_blobs(dst), expected)) == len(set(expected.values()))


def test_drive_reader_reads_aliases(tmp_path, inputs, monkeypatch):
    """web/drive/zim-reader.js parses dirents itself; an alias must read
    back the target's bytes there too."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    monkeypatch.delenv("STREETZIM_TILE_ALIASES", raising=False)
    mb, sat, expected = inputs
    zim = _build(tmp_path, "a.zim", mb, sat)
    script = tmp_path / "read.mjs"
    script.write_text(f"""
import fs from 'node:fs';
const self = {{}};
new Function('self', fs.readFileSync({json.dumps(str(ROOT / 'web/drive/fzstd.js'))}, 'utf8'))(self);
const R = new Function('self', fs.readFileSync({json.dumps(str(ROOT / 'web/drive/zim-reader.js'))}, 'utf8')
  + '; return self.StreetZimReader;')(self);
const r = new R(await fs.openAsBlob({json.dumps(str(zim))}));
await r.open();
const out = {{}};
for (const p of {json.dumps(sorted(expected))}) {{
  const e = await r.read(p);
  out[p] = e ? Buffer.from(e.data).toString('hex') : null;
}}
console.log(JSON.stringify(out));
""")
    res = subprocess.run([node, str(script)], capture_output=True, text=True,
                         timeout=120, env=dict(os.environ))
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    assert got == {p: d.hex() for p, d in expected.items()}
