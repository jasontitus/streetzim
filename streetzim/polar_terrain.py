"""Terrain for a polar map (streetzim/polar.py).

A polar map's tiles are in "map" positions, so its terrain cannot be cut
from the DEM the way any other map's is: each pixel of a tile stands for a
real place found through the projection, and its elevation is read there,
from the Copernicus cells the terrain cache already holds for everything
south of 60 S.

    build(dest_dir, max_zoom, dem_dir)

writes terrain-RGB tiles <dest_dir>/<z>/<x>/<y>.webp for zooms 0..max_zoom:
the top zoom from the DEM, each zoom below it from the mean of the four
tiles under it. A tile with no land is not written. create_osm_zim
--projection takes the folder as it is (--terrain-dir).
"""
import hashlib
import math
import os
import re
import shutil
from collections import OrderedDict

import numpy as np

from . import polar
from . import terrain as _terrain

TILE = 256
WORLD = 2 * math.pi * polar.R
# Top-zoom tiles are made in squares of this many a side: a worker keeps the
# cells of one square in memory.
BLOCK = 16
CELL_BUDGET_BYTES = 400 << 20
NORTH_ROW = int(polar.EDGE_LAT) - 1    # the northernmost row of cells: S61
# Raised when build() would write other tiles from the same cells and zoom.
VERSION = 1
_CELL_NAME = re.compile(r"dem_S(\d\d)_[EW]\d{3}\.tif(\.nodata)?$")


class MissingCell(RuntimeError):
    """A DEM cell that is neither cached nor known to be sea."""


def from_polar(x, y):
    """polar.from_polar for arrays: (lon, lat) in degrees."""
    t = np.hypot(x, y) / polar._K
    phi = np.pi / 2 - 2 * np.arctan(t)
    for _ in range(8):
        s = np.sin(phi)
        phi = np.pi / 2 - 2 * np.arctan(t * ((1 - polar.E * s) / (1 + polar.E * s)) ** (polar.E / 2))
    return np.degrees(np.arctan2(x, y)), -np.degrees(phi)


def tile_true(z, x, y, n=TILE):
    """The real (lon, lat) of the n x n pixel centres of map tile z/x/y, as
    two arrays with row 0 at the tile's top."""
    res = WORLD / (n << z)
    mx = -WORLD / 2 + (x * n + np.arange(n) + 0.5) * res
    my = WORLD / 2 - (y * n + np.arange(n) + 0.5) * res
    return from_polar(*np.meshgrid(mx, my))


def tile_range(z):
    """(x0, y0, x1, y1), inclusive: the tiles of zoom z that reach the map's
    edge circle."""
    rho = polar._K * polar._t(math.radians(-polar.EDGE_LAT))
    size = WORLD / (1 << z)
    lo = int((WORLD / 2 - rho) // size)
    hi = int((WORLD / 2 + rho) // size)
    return lo, lo, hi, hi


class Cells:
    """The Copernicus cells of a DEM folder, read whole and kept while they
    fit the budget.

    A cell named for its south-west corner (lat0, lon0) holds the samples at
    lat0 + 1 down to just above lat0, and lon0 up to just below lon0 + 1:
    the sample at lat0 is the first row of the cell to the south, the one at
    lon0 + 1 the first column of the cell to the east. get() returns the
    cell with that row and column added, so that every place in the cell
    lies between four samples."""

    def __init__(self, dem_dir, budget=CELL_BUDGET_BYTES):
        self.dem_dir = dem_dir
        self.budget = budget
        self._kept = OrderedDict()
        self._bytes = 0
        self._land = {}

    def is_land(self, lat0, lon0):
        key = (lat0, lon0)
        if key not in self._land:
            path = _terrain._glo30_path(self.dem_dir, lat0, lon0)
            if os.path.isfile(path):
                self._land[key] = True
            elif os.path.isfile(path + ".nodata"):
                self._land[key] = False
            else:
                raise MissingCell(f"{path}: neither the cell nor its .nodata mark is there")
        return self._land[key]

    def _read(self, lat0, lon0, window=None):
        import rasterio
        with rasterio.open(_terrain._glo30_path(self.dem_dir, lat0, lon0)) as src:
            # A window is ((first row, row after the last), (the same for columns)).
            if window == "row":
                return src.read(1, window=((0, 1), (0, src.width)))[0].astype(np.float32)
            if window == "col":
                return src.read(1, window=((0, src.height), (0, 1)))[:, 0].astype(np.float32)
            a = src.read(1).astype(np.float32)
            if src.nodata is not None:
                a[a == src.nodata] = 0.0
            return a

    def _first(self, lat0, lon0, window, n):
        """The first row or column of a cell; zeros (n of them) for sea."""
        if not self.is_land(lat0, lon0):
            return np.zeros(n, dtype=np.float32)
        return self._read(lat0, lon0, window)

    def _padded(self, lat0, lon0):
        a = self._read(lat0, lon0)
        rows, cols = a.shape
        out = np.empty((rows + 1, cols + 1), dtype=np.float32)
        out[:rows, :cols] = a
        east = (lon0 + 180 + 1) % 360 - 180
        out[:rows, cols] = self._first(lat0, east, "col", rows)
        if lat0 <= -90:
            out[rows] = out[rows - 1]
            return out
        # The row to the south, up to the first sample of the cell beyond
        # it; a band nearer the pole has fewer columns.
        south = np.append(self._first(lat0 - 1, lon0, "row", cols),
                          self._first(lat0 - 1, east, "row", 1)[0])
        n = len(south) - 1
        out[rows] = np.interp(np.arange(cols + 1) / cols, np.arange(n + 1) / n, south)
        return out

    def get(self, lat0, lon0):
        """The cell with its south row and east column added, or None for sea."""
        if not self.is_land(lat0, lon0):
            return None
        key = (lat0, lon0)
        a = self._kept.get(key)
        if a is not None:
            self._kept.move_to_end(key)
            return a
        a = self._padded(lat0, lon0)
        self._kept[key] = a
        self._bytes += a.nbytes
        while self._bytes > self.budget and len(self._kept) > 1:
            _, old = self._kept.popitem(last=False)
            self._bytes -= old.nbytes
        return a


def _cell_keys(lon, lat):
    """For arrays of real positions: the longitude wrapped to [-180, 180),
    and each position's cell as (lat0 + 90) * 360 + (lon0 + 180), or -1
    north of the map's edge."""
    lon = np.where(lon >= 180.0, lon - 360.0, lon)
    lat0 = np.clip(np.floor(lat), -90, NORTH_ROW).astype(np.int64)
    lon0 = np.clip(np.floor(lon), -180, 179).astype(np.int64)
    key = (lat0 + 90) * 360 + (lon0 + 180)
    key[lat > polar.EDGE_LAT] = -1
    return lon, key


def elevation(lon, lat, cells):
    """Elevation (m) at arrays of real positions: between the four DEM
    samples around each, 0 at sea and north of the map's edge."""
    out = np.zeros(lon.shape, dtype=np.float32)
    lon, key = _cell_keys(lon, lat)
    for k in np.unique(key):
        if k < 0:
            continue
        lat0, lon0 = int(k) // 360 - 90, int(k) % 360 - 180
        a = cells.get(lat0, lon0)
        if a is None:
            continue
        m = key == k
        rows, cols = a.shape[0] - 1, a.shape[1] - 1
        rf = (lat0 + 1 - lat[m]) * rows
        cf = (lon[m] - lon0) * cols
        r0 = np.clip(np.floor(rf), 0, rows - 1).astype(np.int64)
        c0 = np.clip(np.floor(cf), 0, cols - 1).astype(np.int64)
        fr = np.clip(rf - r0, 0.0, 1.0)
        fc = np.clip(cf - c0, 0.0, 1.0)
        out[m] = ((a[r0, c0] * (1 - fc) + a[r0, c0 + 1] * fc) * (1 - fr)
                  + (a[r0 + 1, c0] * (1 - fc) + a[r0 + 1, c0 + 1] * fc) * fr)
    return out


def has_land(z, x, y, cells):
    """Whether tile z/x/y reaches a cell with land. 16 x 16 points of it
    are looked at: only around the pole, where every cell is land, are
    cells narrower than those points are apart."""
    _, key = _cell_keys(*tile_true(z, x, y, n=16))
    return any(cells.is_land(int(k) // 360 - 90, int(k) % 360 - 180)
               for k in np.unique(key) if k >= 0)


def tile_elevation(z, x, y, cells):
    """The 256 x 256 elevations of map tile z/x/y."""
    return elevation(*tile_true(z, x, y), cells)


def _tile_path(dest_dir, z, x, y):
    return os.path.join(dest_dir, str(z), str(x), f"{y}.webp")


def _block(args):
    """The top-zoom tiles of one square of BLOCK x BLOCK; how many were written."""
    dest_dir, dem_dir, z, bx, by, (x0, y0, x1, y1) = args
    cells = _WORKER.get(dem_dir)
    if cells is None:
        cells = _WORKER[dem_dir] = Cells(dem_dir)
    written = 0
    for x in range(max(x0, bx * BLOCK), min(x1, bx * BLOCK + BLOCK - 1) + 1):
        for y in range(max(y0, by * BLOCK), min(y1, by * BLOCK + BLOCK - 1) + 1):
            if not has_land(z, x, y, cells):
                continue
            elev = tile_elevation(z, x, y, cells)
            if not _terrain.quantize_terrain(elev).any():
                continue
            _terrain.save_terrain_tile(elev, dest_dir, z, x, y)
            written += 1
    return bx, by, written


_WORKER = {}


def _parent(args):
    """Tile z/x/y from the mean of the four under it; whether it was written."""
    dest_dir, z, x, y = args
    big = np.zeros((2 * TILE, 2 * TILE), dtype=np.float32)
    found = False
    for dy in (0, 1):
        for dx in (0, 1):
            path = _tile_path(dest_dir, z + 1, 2 * x + dx, 2 * y + dy)
            if os.path.isfile(path):
                big[dy * TILE:(dy + 1) * TILE, dx * TILE:(dx + 1) * TILE] = _terrain._decode_terrain(path)
                found = True
    if not found:
        return False
    elev = big.reshape(TILE, 2, TILE, 2).mean(axis=(1, 3))
    if not _terrain.quantize_terrain(elev).any():
        return False
    _terrain.save_terrain_tile(elev, dest_dir, z, x, y)
    return True


def _tiles_of(dest_dir, z):
    """(x, y) of the tiles of zoom z that are there."""
    out = []
    z_dir = os.path.join(dest_dir, str(z))
    if not os.path.isdir(z_dir):
        return out
    for x_name in os.listdir(z_dir):
        if not x_name.isdigit():
            continue
        for name in os.listdir(os.path.join(z_dir, x_name)):
            if name.endswith(".webp") and name[:-5].isdigit():
                out.append((int(x_name), int(name[:-5])))
    return out


def made_from(dem_dir, max_zoom):
    """What build() makes its tiles from: the zoom and every cell south of
    60 S, land or sea, by name, size and time."""
    h = hashlib.sha256(f"{VERSION}\0{max_zoom}\n".encode())
    for name in sorted(os.listdir(dem_dir)):
        m = _CELL_NAME.match(name)
        if m and -int(m.group(1)) <= NORTH_ROW:
            st = os.stat(os.path.join(dem_dir, name))
            h.update(f"{name}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def _start(dest_dir, want):
    """Empty dest_dir of tiles made from anything but `want`."""
    stamp = os.path.join(dest_dir, ".made-from")
    try:
        with open(stamp, encoding="utf-8") as f:
            if f.read().strip() == want:
                return
    except OSError:
        pass
    for name in os.listdir(dest_dir):
        if name.isdigit() or name == ".squares":
            shutil.rmtree(os.path.join(dest_dir, name))
    with open(stamp, "w", encoding="utf-8") as f:
        f.write(want + "\n")


def build(dest_dir, max_zoom, dem_dir, workers=1, say=print):
    """Terrain tiles for zooms 0..max_zoom under dest_dir; tiles per zoom.

    A run that was stopped goes on where it was: every finished square of
    the top zoom leaves a mark under <dest_dir>/.squares. Tiles made from
    other cells or for another zoom are removed first."""
    os.makedirs(dest_dir, exist_ok=True)
    _start(dest_dir, made_from(dem_dir, max_zoom))
    _WORKER.pop(dem_dir, None)      # cells read by an earlier run in this process
    marks = os.path.join(dest_dir, ".squares")
    os.makedirs(marks, exist_ok=True)
    box = tile_range(max_zoom)
    todo = []
    for bx in range(box[0] // BLOCK, box[2] // BLOCK + 1):
        for by in range(box[1] // BLOCK, box[3] // BLOCK + 1):
            if not os.path.exists(os.path.join(marks, f"{max_zoom}-{bx}-{by}")):
                todo.append((dest_dir, dem_dir, max_zoom, bx, by, box))
    say(f"  terrain z{max_zoom}: {len(todo)} squares of {BLOCK} x {BLOCK} tiles to make")
    pool = None
    if workers > 1 and todo:
        import multiprocessing
        pool = multiprocessing.get_context("spawn").Pool(workers)
    try:
        done = 0
        for bx, by, _ in (pool.imap_unordered(_block, todo) if pool else map(_block, todo)):
            with open(os.path.join(marks, f"{max_zoom}-{bx}-{by}"), "w", encoding="utf-8"):
                pass
            done += 1
            if done % 100 == 0:
                say(f"    {done}/{len(todo)} squares")
        counts = {max_zoom: len(_tiles_of(dest_dir, max_zoom))}
        for z in range(max_zoom - 1, -1, -1):
            parents = sorted({(x // 2, y // 2) for x, y in _tiles_of(dest_dir, z + 1)})
            jobs = [(dest_dir, z, x, y) for x, y in parents]
            made = pool.imap_unordered(_parent, jobs, chunksize=64) if pool else map(_parent, jobs)
            counts[z] = sum(1 for ok in made if ok)
            say(f"  terrain z{z}: {counts[z]} tiles")
    finally:
        if pool:
            pool.close()
            pool.join()
    return counts
