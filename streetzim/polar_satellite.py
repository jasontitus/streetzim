"""Satellite imagery for a polar map (streetzim/polar.py).

The Web Mercator mosaics every other map uses (streetzim/satellite.py) end
at 85 S and are plain white over most of the ice sheet. NASA's Blue Marble
covers the continent to the pole, and GIBS serves it in EPSG:3031: the very
positions a polar map draws in. A map tile is a piece of that picture, cut
out and scaled; nothing is reprojected.

The picture is 512 m a pixel (level 4 of GIBS's "500m" set, 32 x 32 tiles of
512 pixels around the pole). Zoom 9, 306 m a pixel, keeps all of it. Every
zoom is cut from the picture itself, and every tile of the map's box is
written: the sea is part of the picture.
"""
import hashlib
import io
import os
import shutil
import time
import urllib.request

from streetzim import polar, polar_terrain, satellite_sources

SOURCE = satellite_sources.POLAR
LEVEL = 4
SOURCE_TILE = 512
SOURCE_TILES = 2 << LEVEL                 # along each side of the picture
HALF = 4194304.0                          # the picture reaches this far from the pole, metres
SOURCE_PIXELS = SOURCE_TILES * SOURCE_TILE
METRES_PER_PIXEL = 2 * HALF / SOURCE_PIXELS
TOP_ZOOM = 9
TILE = polar_terrain.TILE
# A zoom whose tiles reach past the picture is cut from a small copy of it
# set in the whole map square, this many metres a pixel.
WORLD_METRES_PER_PIXEL = 16 * METRES_PER_PIXEL
# Raised when build() would write other tiles from the same picture.
VERSION = 1


def source_path(cache_dir, row, col):
    return os.path.join(cache_dir, str(LEVEL), str(row), f"{col}.jpeg")


def _decoded(data):
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.size != (SOURCE_TILE, SOURCE_TILE):
        raise ValueError(f"a {im.size[0]} x {im.size[1]} picture, not a source tile")
    return im.convert("RGB")


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "streetzim/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def fetch(cache_dir, get=_get, workers=4, say=print, wait=time.sleep):
    """Every source tile into cache_dir; how many were fetched. A tile that
    cannot be had stops the build: a hole would be a black square on the map."""
    from concurrent.futures import ThreadPoolExecutor
    missing = [(r, c) for r in range(SOURCE_TILES) for c in range(SOURCE_TILES)
               if not os.path.isfile(source_path(cache_dir, r, c))]

    def one(rc):
        row, col = rc
        url = SOURCE.tile_url.format(z=LEVEL, y=row, x=col)
        for attempt in range(4):
            try:
                data = get(url)
                _decoded(data)
                break
            except Exception as e:
                if attempt == 3:
                    raise RuntimeError(f"{url}: {e}") from e
                wait(2 ** attempt)
        path = source_path(cache_dir, row, col)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        part = f"{path}.{os.getpid()}.part"
        with open(part, "wb") as f:
            f.write(data)
        os.replace(part, path)

    if missing:
        say(f"  satellite: fetching {len(missing)} source tiles ({SOURCE.layer})")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, missing))
    return len(missing)


def made_from(cache_dir, bbox, max_zoom=TOP_ZOOM, quality=40):
    """What build() makes its tiles from: the box, the zoom, the quality and
    every source tile by name and size."""
    h = hashlib.sha256(f"{VERSION}\0{tuple(bbox)}\0{max_zoom}\0{quality}\n".encode())
    for r in range(SOURCE_TILES):
        for c in range(SOURCE_TILES):
            try:
                size = os.path.getsize(source_path(cache_dir, r, c))
            except OSError:
                size = -1
            h.update(f"{r}\0{c}\0{size}\n".encode())
    return h.hexdigest()


def picture(cache_dir):
    """The whole source picture, north of 0 E at the top."""
    from PIL import Image
    full = Image.new("RGB", (SOURCE_PIXELS, SOURCE_PIXELS))
    for r in range(SOURCE_TILES):
        for c in range(SOURCE_TILES):
            with open(source_path(cache_dir, r, c), "rb") as f:
                full.paste(_decoded(f.read()), (c * SOURCE_TILE, r * SOURCE_TILE))
    return full


def _world(full):
    """A small copy of the picture set in the whole map square, and the
    metres its first pixel lies from the centre. What the picture does not
    reach has the mean colour of its rim, which is deep sea."""
    import numpy as np
    from PIL import Image
    small_px = round(2 * HALF / WORLD_METRES_PER_PIXEL)
    small = full.resize((small_px, small_px), Image.Resampling.BOX)
    a = np.asarray(small)
    rim = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    fill = tuple(int(v) for v in rim.mean(axis=0).round())
    side = int(polar_terrain.WORLD / WORLD_METRES_PER_PIXEL) + 2
    side += (side - small_px) % 2            # the picture's centre on the square's
    world = Image.new("RGB", (side, side), fill)
    at = (side - small_px) // 2
    world.paste(small, (at, at))
    return world, side * WORLD_METRES_PER_PIXEL / 2


def tile_range(z, bbox):
    """(x0, y0, x1, y1), inclusive: the tiles of zoom z that the box (west,
    south, east, north, in map positions) reaches."""
    n = 1 << z
    size = polar_terrain.WORLD / n
    west, south = polar.lonlat_to_merc(bbox[0], bbox[1])
    east, north = polar.lonlat_to_merc(bbox[2], bbox[3])

    def index(metres):
        return min(n - 1, max(0, int(metres // size)))

    return (index(west + polar_terrain.WORLD / 2), index(polar_terrain.WORLD / 2 - north),
            index(east + polar_terrain.WORLD / 2), index(polar_terrain.WORLD / 2 - south))


def tile_box(z, x, y, half, metres_per_pixel):
    """The pixels (left, top, right, bottom; fractions too) that tile z/x/y
    covers in a picture centred on the pole whose first pixel lies `half`
    metres from the centre."""
    size = polar_terrain.WORLD / (1 << z)
    left = -polar_terrain.WORLD / 2 + x * size
    top = polar_terrain.WORLD / 2 - y * size
    return ((left + half) / metres_per_pixel, (half - top) / metres_per_pixel,
            (left + size + half) / metres_per_pixel, (half - top + size) / metres_per_pixel)


def _tile_path(dest_dir, z, x, y):
    return os.path.join(dest_dir, str(z), str(x), f"{y}.avif")


def build(dest_dir, cache_dir, bbox, max_zoom=TOP_ZOOM, quality=40, workers=4, say=print):
    """AVIF tiles of the box for zooms 0..max_zoom under dest_dir, from the
    source tiles in cache_dir (fetch() them first); tiles per zoom. dest_dir
    is replaced whole, and only when every tile is written."""
    from concurrent.futures import ThreadPoolExecutor
    from PIL import Image, features
    if not features.check("avif"):
        import pillow_avif  # noqa: F401  (registers the codec)
    full = picture(cache_dir)
    world, world_half = _world(full)
    part = dest_dir.rstrip("/") + ".part"
    if os.path.isdir(part):
        shutil.rmtree(part)
    counts = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for z in range(max_zoom, -1, -1):
            x0, y0, x1, y1 = tile_range(z, bbox)
            first = tile_box(z, x0, y0, HALF, METRES_PER_PIXEL)
            last = tile_box(z, x1, y1, HALF, METRES_PER_PIXEL)
            if min(first[:2]) >= 0 and max(last[2:]) <= SOURCE_PIXELS:
                src, half, mpp = full, HALF, METRES_PER_PIXEL
            else:
                src, half, mpp = world, world_half, WORLD_METRES_PER_PIXEL
            zoom_in = polar_terrain.WORLD / (1 << z) / TILE < mpp
            how = Image.Resampling.BICUBIC if zoom_in else Image.Resampling.BOX

            def one(xy, z=z, src=src, half=half, mpp=mpp, how=how):
                x, y = xy
                box = tile_box(z, x, y, half, mpp)
                path = _tile_path(part, z, x, y)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                src.resize((TILE, TILE), how, box=box).save(
                    path, "AVIF", quality=quality, speed=6)

            tiles = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
            list(pool.map(one, tiles))
            counts[z] = len(tiles)
            say(f"  satellite z{z}: {counts[z]} tiles")
    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir)
    os.replace(part, dest_dir)
    return counts
