"""Paths in builds capped at zoom 13 (`--max-zoom 13`, the Light variants).

The tile profile (resources/tilemaker/process-openmaptiles.lua) writes
footways, cycleways, bridleways, paths, steps, pedestrian ways and
platforms (transportation class "path") and their names only from zoom 14.
A build that stops at z13 therefore drew no trails at all, while its
routing graph, built from the OSM data rather than the tiles, still sent
walkers and cyclists along them.

`fold` copies a z13 tile's four z14 children's "path" features, their
coordinates halved into the z13 tile's grid, onto the end of its
`transportation` and `transportation_name` layers. Everything already in
the tile stays byte for byte: protobuf repeated fields concatenate, so the
new features, keys and values go after the old ones and every old index
still points where it did. A layer that already has paths at z13 (tiles
from another profile) is left alone. The tile format is Mapbox Vector Tile
2.1 (https://github.com/mapbox/vector-tile-spec).
"""
from __future__ import annotations

import gzip
import math
import sqlite3
import zlib
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

# Where the profile puts "path" features and their names.
LAYERS = ("transportation", "transportation_name")
PATH_VALUE = b"\n\x04path"        # a Value message: string_value "path"
CLASS_KEY = b"class"
FOLD_ZOOM = 13                    # the zoom folded into
SOURCE_ZOOM = FOLD_ZOOM + 1       # where the profile writes paths

_MOVE_TO, _LINE_TO, _CLOSE_PATH = 1, 2, 7
_POINT, _LINESTRING, _POLYGON = 1, 2, 3


# ---------- protobuf ----------

def _varint(buf, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7


def _fields(buf) -> Iterator[tuple[int, int, Any, int, int]]:
    """(field number, wire type, value, start, end) for each field of a
    message; a length-delimited value is a memoryview slice."""
    i, n = 0, len(buf)
    while i < n:
        start = i
        key, i = _varint(buf, i)
        fno, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 2:
            ln, i = _varint(buf, i)
            v = buf[i:i + ln]
            i += ln
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wt}")
        yield fno, wt, v, start, i


def _packed(wt: int, v: Any) -> list[int]:
    """A repeated uint32: packed (wire type 2) or one value (wire type 0)."""
    if wt == 0:
        return [v]
    out, i, n = [], 0, len(v)
    while i < n:
        x, i = _varint(v, i)
        out.append(x)
    return out


def _enc_varint(v: int) -> bytes:
    out = bytearray()
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)


def _enc_len(fno: int, payload: bytes) -> bytes:
    return _enc_varint(fno << 3 | 2) + _enc_varint(len(payload)) + payload


def _enc_packed(fno: int, ints: Iterable[int]) -> bytes:
    return _enc_len(fno, b"".join(_enc_varint(x) for x in ints))


def _zigzag(v: int) -> int:
    return v << 1 if v >= 0 else ((-v) << 1) - 1


def _unzigzag(v: int) -> int:
    return (v >> 1) ^ -(v & 1)


# ---------- layers ----------

class _Layer:
    """The parts of a Layer message this module reads."""
    __slots__ = ("name", "features", "keys", "values", "extent")

    def __init__(self, buf):
        self.name = b""
        self.features: list = []
        self.keys: list[bytes] = []
        self.values: list[bytes] = []
        self.extent = 4096
        for fno, wt, v, _s, _e in _fields(buf):
            if fno == 1 and wt == 2:
                self.name = bytes(v)
            elif fno == 2 and wt == 2:
                self.features.append(v)
            elif fno == 3 and wt == 2:
                self.keys.append(bytes(v))
            elif fno == 4 and wt == 2:
                self.values.append(bytes(v))
            elif fno == 5 and wt == 0:
                self.extent = v

    def has_paths(self) -> bool:
        return CLASS_KEY in self.keys and PATH_VALUE in self.values


def _layer_name(buf) -> bytes:
    for fno, wt, v, _s, _e in _fields(buf):
        if fno == 1 and wt == 2:
            return bytes(v)
    return b""


def _path_features(tile, name: bytes, dx: int, dy: int, out: list) -> None:
    """Append (type, [(key, value)], parts, closed, extent, dx, dy) for each
    class=path feature of layer `name` in decoded child tile `tile`."""
    for fno, wt, v, _s, _e in _fields(tile):
        if fno != 3 or wt != 2 or _layer_name(v) != name:
            continue
        # The value table holds "path" only when some feature uses it:
        # most tiles are skipped here without reading a feature.
        if PATH_VALUE not in bytes(v):
            return
        layer = _Layer(v)
        if CLASS_KEY not in layer.keys or PATH_VALUE not in layer.values:
            return
        ck = layer.keys.index(CLASS_KEY)
        pv = {i for i, raw in enumerate(layer.values) if raw == PATH_VALUE}
        for feat in layer.features:
            tags: list[int] = []
            gtype = 0
            geom: list[int] = []
            for ffno, fwt, fv, _fs, _fe in _fields(feat):
                # Repeated fields may come packed, unpacked or split
                # over several fields; each occurrence adds to the list.
                if ffno == 2:
                    tags += _packed(fwt, fv)
                elif ffno == 3 and fwt == 0:
                    gtype = fv
                elif ffno == 4:
                    geom += _packed(fwt, fv)
            pairs = list(zip(tags[0::2], tags[1::2]))
            if not any(k == ck and val in pv for k, val in pairs):
                continue
            parts, closed = _decode_geometry(geom)
            if not parts:
                continue
            out.append((gtype, [(layer.keys[k], layer.values[val]) for k, val in pairs
                                if k < len(layer.keys) and val < len(layer.values)],
                        parts, closed, layer.extent, dx, dy))
        return


# ---------- geometry ----------

def _decode_geometry(geom: list[int]) -> tuple[list[list[tuple[int, int]]], list[bool]]:
    """Absolute coordinates per part (a MoveTo starts one) and, per part,
    whether a ClosePath ended it."""
    parts: list[list[tuple[int, int]]] = []
    closed: list[bool] = []
    x = y = i = 0
    n = len(geom)
    while i < n:
        cmd, count = geom[i] & 7, geom[i] >> 3
        i += 1
        if cmd == _CLOSE_PATH:
            if closed:
                closed[-1] = True
            continue
        for _ in range(count):
            if i + 1 >= n:
                return parts, closed
            x += _unzigzag(geom[i])
            y += _unzigzag(geom[i + 1])
            i += 2
            if cmd == _MOVE_TO:
                parts.append([(x, y)])
                closed.append(False)
            elif cmd == _LINE_TO and parts:
                parts[-1].append((x, y))
    return parts, closed


def _ring_area2(ring: list[tuple[int, int]]) -> int:
    return sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]))


def _encode_geometry(gtype: int, parts, closed, src_extent: int, dst_extent: int,
                     dx: int, dy: int) -> list[int] | None:
    """The geometry in the parent's grid, or None when the halving leaves
    nothing drawable. A polygon keeps the rings that survive: an outer ring
    that collapses takes its holes with it, a hole that collapses goes on
    its own."""
    scale = dst_extent / (2 * src_extent)
    ox, oy = dx * src_extent, dy * src_extent
    out_parts = []
    outer_kept = False
    for pts, is_closed in zip(parts, closed):
        moved = []
        for x, y in pts:
            p = (math.floor((x + ox) * scale + 0.5), math.floor((y + oy) * scale + 0.5))
            if not moved or moved[-1] != p:
                moved.append(p)
        if gtype == _POLYGON:
            if len(moved) > 1 and moved[0] == moved[-1]:
                moved.pop()
            # MVT: an outer ring has a positive area (surveyor's formula,
            # y down), a hole a negative one. Rounding onto the coarser grid
            # can collapse a small ring or turn it inside out.
            was = _ring_area2(pts)
            now = _ring_area2(moved) if len(moved) >= 3 else 0
            if was > 0:
                outer_kept = now > 0
                if not outer_kept:
                    continue
            elif not (outer_kept and was < 0 and now < 0):
                continue
        elif gtype == _LINESTRING:
            if len(moved) < 2:
                continue
        out_parts.append((moved, is_closed))
    if not out_parts:
        return None
    cmds: list[int] = []
    cx = cy = 0
    if gtype == _POINT:
        pts = [pt for moved, _c in out_parts for pt in moved]
        cmds.append(_MOVE_TO | len(pts) << 3)
        for x, y in pts:
            cmds += [_zigzag(x - cx), _zigzag(y - cy)]
            cx, cy = x, y
        return cmds
    for moved, is_closed in out_parts:
        x, y = moved[0]
        cmds += [_MOVE_TO | 1 << 3, _zigzag(x - cx), _zigzag(y - cy)]
        cx, cy = x, y
        cmds.append(_LINE_TO | (len(moved) - 1) << 3)
        for x, y in moved[1:]:
            cmds += [_zigzag(x - cx), _zigzag(y - cy)]
            cx, cy = x, y
        if gtype == _POLYGON or is_closed:
            cmds.append(_CLOSE_PATH | 1 << 3)
    return cmds


# ---------- folding ----------

def _append(layer_buf, layer: _Layer | None, name: bytes, feats: list) -> tuple[bytes, int]:
    """`layer_buf` (empty for a new layer) with `feats` appended, and how
    many were. A feature that two children both carry in their buffer
    zones is added once."""
    keys = list(layer.keys) if layer else []
    values = list(layer.values) if layer else []
    extent = layer.extent if layer else 4096
    key_ix = {k: i for i, k in reversed(list(enumerate(keys)))}
    val_ix = {v: i for i, v in reversed(list(enumerate(values)))}
    new_keys: list[bytes] = []
    new_values: list[bytes] = []
    out = bytearray()
    seen: set[tuple] = set()
    added = 0
    for gtype, tags, parts, closed, src_extent, dx, dy in feats:
        geom = _encode_geometry(gtype, parts, closed, src_extent, extent, dx, dy)
        if geom is None:
            continue
        key = (gtype, tuple(tags), tuple(geom))
        if key in seen:
            continue
        seen.add(key)
        idx: list[int] = []
        for k, val in tags:
            if k not in key_ix:
                key_ix[k] = len(keys) + len(new_keys)
                new_keys.append(k)
            if val not in val_ix:
                val_ix[val] = len(values) + len(new_values)
                new_values.append(val)
            idx += [key_ix[k], val_ix[val]]
        feature = _enc_packed(2, idx) + _enc_varint(3 << 3) + _enc_varint(gtype) + _enc_packed(4, geom)
        out += _enc_len(2, feature)
        added += 1
    if not out:
        return bytes(layer_buf), 0
    out += b"".join(_enc_len(3, k) for k in new_keys)
    out += b"".join(_enc_len(4, v) for v in new_values)
    if layer is None:
        head = _enc_varint(15 << 3) + _enc_varint(2) + _enc_len(1, name)
        return head + bytes(out) + _enc_varint(5 << 3) + _enc_varint(extent), added
    return bytes(layer_buf) + bytes(out), added


def fold(tile: bytes, children: Iterable[tuple[int, int, bytes]],
         stats: dict | None = None) -> bytes:
    """`tile` (a decoded z13 MVT) with the class=path features of
    `children` ((dx, dy, decoded z14 MVT), dx/dy 0 or 1: the child's
    column/row within it) appended. Returns `tile` itself when there is
    nothing to add."""
    adds: dict[bytes, list] = {n.encode(): [] for n in LAYERS}
    for dx, dy, child in children:
        if child:
            for name, feats in adds.items():
                _path_features(child, name, dx, dy, feats)
    if not any(adds.values()):
        return tile
    out = bytearray()
    seen: set[bytes] = set()
    added = 0
    for fno, wt, v, s, e in _fields(tile):
        if fno == 3 and wt == 2:
            name = _layer_name(v)
            if name in adds:
                seen.add(name)
                layer = _Layer(v)
                if adds[name] and not layer.has_paths():
                    new, n = _append(v, layer, name, adds[name])
                    if n:
                        out += _enc_len(3, new)
                        added += n
                        continue
        out += tile[s:e]
    for name, feats in adds.items():
        if feats and name not in seen:
            new, n = _append(b"", None, name, feats)
            if n:
                out += _enc_len(3, new)
                added += n
    if not added:
        return tile
    if stats is not None:
        stats["tiles"] = stats.get("tiles", 0) + 1
        stats["features"] = stats.get("features", 0) + added
    return bytes(out)


def _decoded(data: bytes | None) -> bytes:
    if data and data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    return data or b""


def _ro_uri(path) -> str:
    """A read-only SQLite URI; `as_uri` escapes '#', '?' and '%' (an
    f-string URI cut the path at '#' and created the truncated file)."""
    return f"{Path(path).resolve().as_uri()}?mode=ro"


_BAD_TILE = (OSError, EOFError, zlib.error, ValueError, IndexError)


class Folder:
    """Folds z14 paths into the z13 tiles of an MBTiles file (see `fold`).
    One per thread or process: it holds a SQLite connection."""

    def __init__(self, mbtiles_path):
        self.conn = sqlite3.connect(_ro_uri(mbtiles_path), uri=True)
        self.stats: dict[str, int] = {}

    def __call__(self, z: int, x: int, y: int, data: bytes) -> bytes:
        """The tile at XYZ (z, x, y) with its children's paths, in the same
        encoding (gzip or not) when anything was added, else `data`
        itself. A child that will not decode is skipped and counted
        ("bad_children"); a parent that will not decode is returned as
        it came, for the writer's own check to report."""
        if z != FOLD_ZOOM or not data:
            return data
        n = 1 << SOURCE_ZOOM
        cols = (2 * x, 2 * x + 1)
        rows = {n - 1 - 2 * y: 0, n - 1 - (2 * y + 1): 1}       # TMS row -> dy
        cur = self.conn.execute(
            "SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level = ? "
            "AND tile_column IN (?, ?) AND tile_row IN (?, ?) "
            "ORDER BY tile_column, tile_row DESC",
            (SOURCE_ZOOM, cols[0], cols[1], *rows))
        children = []
        for col, row, blob in cur:
            try:
                child = _decoded(blob)
                # Parse it here, so a malformed child is caught alone.
                for _f in _fields(child):
                    pass
            except _BAD_TILE:
                self.stats["bad_children"] = self.stats.get("bad_children", 0) + 1
                continue
            children.append((col - 2 * x, rows[row], child))
        if not children:
            return data
        try:
            tile = _decoded(data)
            folded = fold(tile, children, self.stats)
        except _BAD_TILE:
            return data
        if folded is tile:
            return data
        return gzip.compress(folded, compresslevel=6, mtime=0) if data[:2] == b"\x1f\x8b" else folded

    def close(self) -> None:
        self.conn.close()


def has_source_zoom(mbtiles_path) -> bool:
    """Whether the MBTiles has z14 tiles to fold from."""
    conn = sqlite3.connect(_ro_uri(mbtiles_path), uri=True)
    try:
        return conn.execute("SELECT 1 FROM tiles WHERE zoom_level = ? LIMIT 1",
                            (SOURCE_ZOOM,)).fetchone() is not None
    finally:
        conn.close()


# ---------- whole builds ----------

_WORKER: Folder | None = None


def _init_worker(mbtiles_path) -> None:
    global _WORKER
    _WORKER = Folder(mbtiles_path)


def _fold_with(folder: Folder, x: int, y: int, data: bytes) -> tuple[bytes | None, int, int]:
    """(the folded tile or None when unchanged, paths added, bad children)."""
    f0 = folder.stats.get("features", 0)
    b0 = folder.stats.get("bad_children", 0)
    out = folder(FOLD_ZOOM, x, y, data)
    return ((None if out is data else out), folder.stats.get("features", 0) - f0,
            folder.stats.get("bad_children", 0) - b0)


def _fold_one(item: tuple[int, int, bytes]) -> tuple[bytes | None, int, int]:
    assert _WORKER is not None
    return _fold_with(_WORKER, *item)


def fold_tiles(mbtiles_path, items: Iterable[tuple[int, int, int, bytes]],
               workers: int = 1, stats: dict | None = None,
               batch: int = 4096) -> Iterator[tuple[int, int, int, bytes]]:
    """`items` ((z, x, y, data), XYZ rows) with every z13 tile folded
    (see `fold`), in the same order. `workers` > 1 folds in that many
    processes; `stats` gets "tiles" (tiles that gained paths), "features"
    (paths added) and "bad_children" (z14 tiles that would not decode)."""
    stats = stats if stats is not None else {}
    for k in ("tiles", "features", "bad_children"):
        stats.setdefault(k, 0)
    pool = None
    folder = None
    try:
        if workers > 1:
            import multiprocessing
            pool = multiprocessing.get_context("spawn").Pool(
                workers, initializer=_init_worker, initargs=(str(mbtiles_path),))
        else:
            folder = Folder(mbtiles_path)
        it = iter(items)
        while True:
            chunk = []
            for item in it:
                chunk.append(item)
                if len(chunk) >= batch:
                    break
            if not chunk:
                return
            todo = [i for i, (z, _x, _y, data) in enumerate(chunk) if z == FOLD_ZOOM and data]
            if pool is not None:
                results = pool.map(_fold_one, [chunk[i][1:] for i in todo],
                                   chunksize=max(1, len(todo) // (workers * 4) or 1))
            else:
                assert folder is not None
                results = [_fold_with(folder, *chunk[i][1:]) for i in todo]
            for i, (out, added, bad) in zip(todo, results):
                stats["bad_children"] += bad
                if out is not None:
                    z, x, y, _data = chunk[i]
                    chunk[i] = (z, x, y, out)
                    stats["tiles"] += 1
                    stats["features"] += added
            yield from chunk
    finally:
        if pool is not None:
            pool.terminate()
            pool.join()
        if folder is not None:
            folder.close()
