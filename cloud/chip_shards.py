"""Geographic sharding of Find-chip files.

A chip (``category-index/chip-{id}.json``, see ``cloud/chip_rules.py``)
used to be split, once over 10 MB, into FNV-1a *name*-hash buckets. Name
hashing has no locality, so both viewers fetched every bucket and
concatenated them: a Shops tap on east-coast-us parsed 147 MB of JSON,
brazil 217 MB, continents several times that — past iOS's WebView heap.

Here a chip over ``target_bytes`` is cut into spatially compact shards
with a byte-weighted k-d split. The manifest lists each shard's bbox so a
viewer fetches only the shards around the viewport / origin.

Manifest entry for a sharded chip — a superset of the old shape, so a
reader that only knows ``sub_chunks`` still fetches every file and gets
the whole chip::

    {"label", "count", "bytes",            # whole chip, as before
     "sub_chunks": ["g000", ...],          # every file, as before
     "layout": "geo",
     "shards": [[s, w, n, e, count, bytes], ...]}   # aligned with sub_chunks

``bytes`` in a shard row is the size of that shard's file. A bbox with
``w > e`` crosses the antimeridian. Records without usable coordinates go
to trailing shards whose bbox fields are all ``null``. ``n_sub_buckets`` is
deliberately absent: it means "route by name hash", which no longer holds.

Imported by create_osm_zim.py (build) and cloud/repackage_zim.py
(``--split-find-chips`` retrofit). The viewer side is the
``chip-shards`` block in resources/viewer/{index,places}.html.
"""
from __future__ import annotations

import json
import math
from typing import Callable, Iterator

import numpy as np

CHIP_SHARD_TARGET_BYTES = 2 * 1024 * 1024
LAYOUT_GEO = "geo"

# Bboxes are rounded outward to this grid to keep the manifest short.
_GRID = 1e5


def _usable_coords(rec: dict) -> tuple[float, float] | None:
    a = rec.get("a")
    o = rec.get("o")
    for v in (a, o):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        if not math.isfinite(v):
            return None
    if not (-90.0 <= a <= 90.0 and -180.0 <= o <= 180.0):
        return None
    return float(a), float(o)


def _floor_grid(x: float) -> float:
    v = math.floor(x * _GRID) / _GRID
    return v - 1.0 / _GRID if v > x else v


def _ceil_grid(x: float) -> float:
    v = math.ceil(x * _GRID) / _GRID
    return v + 1.0 / _GRID if v < x else v


def _lon_interval(lons: np.ndarray) -> tuple[float, float]:
    """Smallest circular longitude interval [w, e] covering ``lons``.
    ``w > e`` means it wraps through ±180."""
    u = np.unique(lons)
    if len(u) == 1:
        w = e = float(u[0])
    else:
        gaps = np.diff(u)
        i = int(np.argmax(gaps))
        wrap_gap = float(u[0]) + 360.0 - float(u[-1])
        if float(gaps[i]) > wrap_gap:
            w, e = float(u[i + 1]), float(u[i])
        else:
            w, e = float(u[0]), float(u[-1])
    return max(-180.0, _floor_grid(w)), min(180.0, _ceil_grid(e))


def _kd_leaves(lat: np.ndarray, lon: np.ndarray, size: np.ndarray,
               target_bytes: int) -> list[np.ndarray]:
    """Index arrays of the k-d leaves, in depth-first (spatially
    coherent) order; each index array sorted ascending so a shard keeps
    the chip's original record order."""
    leaves: list[np.ndarray] = []
    stack = [np.arange(len(lat))]
    while stack:
        ix = stack.pop()
        # +len(ix)+1 accounts for the commas and brackets of the array.
        total = int(size[ix].sum()) + len(ix) + 1
        if total <= target_bytes or len(ix) <= 1:
            leaves.append(np.sort(ix))
            continue
        la = lat[ix]
        lo = lon[ix]
        lat_lo, lat_hi = float(la.min()), float(la.max())
        lat_span = lat_hi - lat_lo
        lon_span = (float(lo.max()) - float(lo.min())) * max(
            math.cos(math.radians((lat_lo + lat_hi) / 2.0)), 0.01)
        key = la if lat_span >= lon_span else lo
        ix = ix[np.argsort(key, kind="stable")]
        cum = np.cumsum(size[ix])
        cut = int(np.searchsorted(cum, cum[-1] / 2.0)) + 1
        cut = min(max(cut, 1), len(ix) - 1)
        stack.append(ix[cut:])
        stack.append(ix[:cut])
    return leaves


class ChipPlan:
    """How one chip's records become ZIM entries. Build with
    :func:`plan_chip`; blobs are produced lazily by :meth:`files` so a
    continent chip never exists twice in memory."""

    def __init__(self, parts: list[bytes], leaves: list[np.ndarray] | None,
                 leaf_bboxes: list[list | None]):
        self._parts = parts
        self._leaves = leaves
        self._bboxes = leaf_bboxes
        self.count = len(parts)
        self.bytes = sum(len(p) for p in parts) + max(len(parts) - 1, 0) + 2

    @property
    def sharded(self) -> bool:
        return self._leaves is not None

    def _suffixes(self) -> list[str]:
        n = len(self._leaves or ())
        width = max(3, len(format(max(n - 1, 0), "x")))
        return [f"g{i:0{width}x}" for i in range(n)]

    def _leaf_bytes(self, ix: np.ndarray) -> int:
        return sum(len(self._parts[i]) for i in ix) + max(len(ix) - 1, 0) + 2

    def manifest_entry(self, label: str) -> dict:
        entry: dict = {"label": label, "count": self.count, "bytes": self.bytes}
        if self.sharded:
            rows = []
            for ix, bbox in zip(self._leaves, self._bboxes):
                box = bbox if bbox is not None else [None, None, None, None]
                rows.append([*box, int(len(ix)), self._leaf_bytes(ix)])
            entry["sub_chunks"] = self._suffixes()
            entry["layout"] = LAYOUT_GEO
            entry["shards"] = rows
        return entry

    def files(self, chip_id: str, label: str, name_prefix: str = "chip-",
              title_kind: str = "Find chip") -> Iterator[tuple[str, str, bytes]]:
        """Yield ``(path, title, json_bytes)`` for every entry to add.

        ``name_prefix``/``title_kind`` let the same planner shard something
        that is not a Find chip: the ``place`` category is sharded for the
        viewer's reverse geocoder and is named ``place-g000.json``, not
        ``chip-place-g000.json``, so it can't be mistaken for a chip.
        """
        if not self.sharded:
            yield (f"category-index/{name_prefix}{chip_id}.json",
                   f"{title_kind} {label}",
                   b"[" + b",".join(self._parts) + b"]")
            return
        for suffix, ix in zip(self._suffixes(), self._leaves):
            yield (f"category-index/{name_prefix}{chip_id}-{suffix}.json",
                   f"{title_kind} {label} (area {suffix})",
                   b"[" + b",".join(self._parts[i] for i in ix) + b"]")


def plan_chip(records: list[dict],
              target_bytes: int = CHIP_SHARD_TARGET_BYTES) -> ChipPlan:
    """Serialize ``records`` once and decide the file layout. A chip whose
    JSON array fits in ``target_bytes`` (or ``target_bytes <= 0``) stays a
    single file; otherwise it's cut into geographic shards."""
    parts = [json.dumps(r, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
             for r in records]
    plan = ChipPlan(parts, None, [])
    if target_bytes <= 0 or plan.bytes <= target_bytes:
        return plan

    geo_ix, lats, lons, nogeo_ix = [], [], [], []
    for i, r in enumerate(records):
        c = _usable_coords(r)
        if c is None:
            nogeo_ix.append(i)
        else:
            geo_ix.append(i)
            lats.append(c[0])
            lons.append(c[1])

    leaves: list[np.ndarray] = []
    bboxes: list[list | None] = []
    if geo_ix:
        g = np.asarray(geo_ix, dtype=np.int64)
        lat = np.asarray(lats, dtype=np.float64)
        lon = np.asarray(lons, dtype=np.float64)
        size = np.fromiter((len(parts[i]) for i in geo_ix), dtype=np.int64,
                           count=len(geo_ix))
        for leaf in _kd_leaves(lat, lon, size, target_bytes):
            w, e = _lon_interval(lon[leaf])
            bboxes.append([max(-90.0, _floor_grid(float(lat[leaf].min()))), w,
                           min(90.0, _ceil_grid(float(lat[leaf].max()))), e])
            leaves.append(g[leaf])
    if nogeo_ix:
        run: list[int] = []
        run_bytes = 2
        for i in nogeo_ix:
            if run and run_bytes + len(parts[i]) + 1 > target_bytes:
                leaves.append(np.asarray(run, dtype=np.int64))
                bboxes.append(None)
                run, run_bytes = [], 2
            run.append(i)
            run_bytes += len(parts[i]) + 1
        leaves.append(np.asarray(run, dtype=np.int64))
        bboxes.append(None)
    plan._leaves = leaves
    plan._bboxes = bboxes
    return plan


def read_chip_records(get_bytes: Callable[[str], bytes], chip_id: str,
                      meta: dict) -> list[dict]:
    """Reassemble one chip's full record list from an existing ZIM, whatever
    its layout (single file, name-hash buckets, or geo shards).
    ``get_bytes(path)`` returns an entry's content or raises."""
    subs = meta.get("sub_chunks")
    if isinstance(subs, list) and subs:
        out: list[dict] = []
        for suffix in subs:
            out.extend(json.loads(get_bytes(f"category-index/chip-{chip_id}-{suffix}.json")))
        return out
    return json.loads(get_bytes(f"category-index/chip-{chip_id}.json"))
