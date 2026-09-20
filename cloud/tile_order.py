"""Tile insertion order shared by the builder and the derive tool.

Zoom-major, Hilbert-within-zoom is the layout docs/zim-variants.md measured:
a viewport's tiles are neighbouring blobs, a zoom level never shares a
cluster with another, and a light derive can drop a zoom by dropping whole
clusters. ``hilbert_index`` is the standard d2xy inverse on a 2^z grid.
"""
from __future__ import annotations

from typing import Iterable, Iterator

ORDERS = ("source", "zoom-hilbert", "zoom-xy")


def hilbert_index(z: int, x: int, y: int) -> int:
    """Distance along the Hilbert curve filling the 2^z x 2^z tile grid."""
    n = 1 << z
    d = 0
    s = n >> 1
    while s > 0:
        rx = 1 if (x & s) else 0
        ry = 1 if (y & s) else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = s - 1 - x
                y = s - 1 - y
            x, y = y, x
        s >>= 1
    return d


def tile_sort_key(order: str, z: int, x: int, y: int) -> int:
    """Sort key WITHIN one zoom for the given order name."""
    if order in ("hilbert", "zoom-hilbert"):
        return hilbert_index(z, x, y)
    if order in ("xy", "zoom-xy"):
        return (x << z) | y
    if order == "yx":
        return (y << z) | x
    raise ValueError(f"unknown tile order {order!r}")


def order_tiles(coords: Iterable[tuple[int, int, int]], order: str) -> Iterator[tuple[int, int, int]]:
    """Yield (z, x, y) zoom-major, each zoom in ``order``. ``source`` keeps
    the input order. Holds one zoom's coordinate list at a time."""
    if order == "source":
        yield from coords
        return
    by_zoom: dict[int, list[tuple[int, int, int]]] = {}
    for z, x, y in coords:
        by_zoom.setdefault(z, []).append((z, x, y))
    for z in sorted(by_zoom):
        lst = by_zoom.pop(z)
        lst.sort(key=lambda t: tile_sort_key(order, z, t[1], t[2]))
        yield from lst
