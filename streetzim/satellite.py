"""Sentinel-2 cloudless satellite tiles: download + stitch (moved verbatim
from create_osm_zim.py, which re-exports these names)."""
import glob
import os
import urllib.request

# The builder's flushing, phase-timing print (see streetzim/common.py).
from streetzim.common import (
    print,
    CACHE_DIR,
    SATELLITE_TILE_URL,
    parse_bbox,
)


def download_satellite_tiles(bbox_str, dest_dir, max_zoom=14, webp_quality=65,
                              sat_format="webp", sat_quality=None, tile_size=256):
    """Download Sentinel-2 Cloudless satellite tiles for a bounding box.

    Downloads JPEG tiles from the EOX Sentinel-2 Cloudless WMTS service,
    converts them to the specified format, and stores them as
    {dest_dir}/{z}/{x}/{y}.{ext}.

    When tile_size=512, four 256px source tiles are stitched into one 512px
    tile, halving the tile count and improving compression.

    Supported formats: "webp", "avif".

    Returns the number of output tiles produced.
    """
    import io
    import math
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from PIL import Image

    if sat_format == "avif":
        # Pillow >= 10.0 has native AVIF support; older versions need pillow-avif-plugin
        from PIL import features
        if not features.check("avif"):
            try:
                import pillow_avif  # noqa: F401 — registers AVIF codec with Pillow
            except ImportError:
                print("    Warning: AVIF not supported (need Pillow >= 10 or pillow-avif-plugin), falling back to webp")
                sat_format = "webp"

    quality = sat_quality if sat_quality is not None else webp_quality
    ext = sat_format  # "webp" or "avif"

    bbox = parse_bbox(bbox_str)
    minlon, minlat, maxlon, maxlat = bbox

    os.makedirs(dest_dir, exist_ok=True)
    # Shared source cache for original JPEG tiles (download once, encode to any format)
    source_cache_dir = os.path.join(CACHE_DIR, "satellite_cache_sources")
    os.makedirs(source_cache_dir, exist_ok=True)
    total_downloaded = 0
    total_skipped = 0
    total_missing = 0
    total_bytes_jpeg = 0
    total_bytes_out = 0

    # Collect existing format caches for transcoding fallback. Only
    # caches holding 256 px tiles qualify: the source tiles stitched
    # below are 256 px, and a 512 px cache tile (satellite_cache_avif_512)
    # pasted at (dx*256, dy*256) overflowed the canvas and overwrote its
    # neighbouring quadrants.
    _format_caches = []
    for d in sorted(glob.glob(os.path.join(CACHE_DIR, "satellite_cache_*_*"))):
        if os.path.isdir(d) and d != dest_dir and d != source_cache_dir:
            # Dir name: satellite_cache_<ext>_<size>
            parts = os.path.basename(d).replace("satellite_cache_", "").split("_")
            if len(parts) >= 2 and parts[1] == "256":
                _format_caches.append((d, parts[0]))
    # Also check the legacy satellite_cache/ (256 px WebP tiles)
    legacy_cache = os.path.join(CACHE_DIR, "satellite_cache")
    if os.path.isdir(legacy_cache) and legacy_cache != dest_dir:
        _format_caches.append((legacy_cache, "webp"))

    def _fetch_source_tile(z, x, y):
        """Get a single 256px tile, using source cache if available.
        Returns (PIL.Image or None, jpeg_bytes_len). Checks: JPEG source
        cache → existing format caches (transcode) → network download.
        A transcoded tile reports 0 bytes (nothing was downloaded); the
        caller must test the image, not the byte count, for presence."""
        # Check JPEG source cache first
        cache_path = os.path.join(source_cache_dir, str(z), str(x), f"{y}.jpg")
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            try:
                return Image.open(cache_path), os.path.getsize(cache_path)
            except Exception:
                pass  # Corrupted cache file, try next

        # Check existing format caches (transcode from WebP/AVIF rather than re-download)
        for cache_dir, cache_ext in _format_caches:
            cached = os.path.join(cache_dir, str(z), str(x), f"{y}.{cache_ext}")
            if os.path.exists(cached) and os.path.getsize(cached) > 0:
                try:
                    im = Image.open(cached)
                    if im.size == (256, 256):
                        return im, 0
                except Exception:
                    pass

        # Download from network
        url = SATELLITE_TILE_URL.format(z=z, x=x, y=y)
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "streetzim/1.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    jpg_data = resp.read()
                # Save to source cache
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                # Atomic: a killed write must not leave a truncated .jpg that
                # every later build reuses (same rule as terrain and DEM).
                import threading as _th
                _tmp = f"{cache_path}.{os.getpid()}-{_th.get_ident()}.tmp"
                try:
                    with open(_tmp, 'wb') as f:
                        f.write(jpg_data)
                    os.replace(_tmp, cache_path)
                finally:
                    if os.path.exists(_tmp):
                        os.unlink(_tmp)
                return Image.open(io.BytesIO(jpg_data)), len(jpg_data)
            except Exception as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                else:
                    print(f"\n    Warning: failed to download z{z}/{x}/{y}: {e}")
        return None, 0

    def _save_image(img, path):
        """Save image in the configured format. Returns output file size.

        Written to a temp file and renamed into place. Pillow opens the target
        path before encoding and only removes it if the encoder raises, so a
        build killed mid-encode left a 0-byte tile at the final path. The
        downloader retries 0-byte files only up to --satellite-download-zoom, so
        anything above that stayed broken for good — australia-nz's two empty
        z14 tiles from 2026-04-13 failed the validator five months later.
        """
        import threading as _th
        tmp = f"{path}.{os.getpid()}-{_th.get_ident()}.tmp"
        try:
            if sat_format == "avif":
                img.save(tmp, "AVIF", quality=quality, speed=6)
            else:
                img.save(tmp, "WEBP", quality=quality)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return os.path.getsize(path)

    def _process_tile_256(z, x, y):
        """Download and convert a single 256px tile. Returns (downloaded, jpeg_bytes, out_bytes)."""
        tile_dir = os.path.join(dest_dir, str(z), str(x))
        tile_path = os.path.join(tile_dir, f"{y}.{ext}")

        if os.path.exists(tile_path) and os.path.getsize(tile_path) > 0:
            return (False, 0, 0)

        os.makedirs(tile_dir, exist_ok=True)
        img, jpeg_size = _fetch_source_tile(z, x, y)
        if img is None:
            return (False, 0, 0)
        out_size = _save_image(img, tile_path)
        return (True, jpeg_size, out_size)

    def _process_tile_512(z, x0, y0):
        """Download four 256px source tiles at z+1 and stitch into one 512px tile.

        The output tile is stored at coordinates (z, x0, y0) but contains the
        pixel data of source tiles (z+1, x0*2..x0*2+1, y0*2..y0*2+1).

        Returns (downloaded, jpeg_bytes, out_bytes).
        """
        tile_dir = os.path.join(dest_dir, str(z), str(x0))
        tile_path = os.path.join(tile_dir, f"{y0}.{ext}")

        if os.path.exists(tile_path) and os.path.getsize(tile_path) > 0:
            return (False, 0, 0)

        os.makedirs(tile_dir, exist_ok=True)

        # Fetch 4 source tiles from one zoom level deeper
        sz = z + 1
        sx0, sy0 = x0 * 2, y0 * 2
        stitched = Image.new("RGB", (512, 512))
        total_jpeg = 0
        found = 0
        for dy in range(2):
            for dx in range(2):
                img, jpeg_size = _fetch_source_tile(sz, sx0 + dx, sy0 + dy)
                total_jpeg += jpeg_size
                if img is not None:
                    stitched.paste(img, (dx * 256, dy * 256))
                    found += 1

        # Presence, not byte count: four transcoded quadrants report 0
        # bytes and used to make this tile "not written". And a tile
        # with any missing quadrant must not be written either — it was
        # cached permanently with black squares.
        if found < 4:
            return (None, 0, 0)   # None = incomplete (not cached, not written)

        out_size = _save_image(stitched, tile_path)
        return (True, total_jpeg, out_size)

    max_workers = min(32, (os.cpu_count() or 4) * 4)

    if tile_size == 512:
        print(f"    Mode: 512px tiles ({sat_format} q{quality})")
        print("    Stitching 4x source 256px tiles per output tile")
    else:
        print(f"    Mode: 256px tiles ({sat_format} q{quality})")

    for z in range(0, max_zoom + 1):
        # Calculate tile range at this zoom level
        if tile_size == 512:
            # For 512px tiles, we need source tiles at z+1 but store at z.
            # The output tile grid at zoom z covers the same area as the
            # 256px grid at zoom z, but each tile has 4x the source pixels.
            src_z = z + 1
            n = 2 ** src_z
        else:
            n = 2 ** z

        x_min = int(n * (minlon + 180) / 360)
        x_max = int(n * (maxlon + 180) / 360)
        lat_rad_min = math.radians(minlat)
        lat_rad_max = math.radians(maxlat)
        y_max = int(n * (1 - math.log(math.tan(lat_rad_min) + 1 / math.cos(lat_rad_min)) / math.pi) / 2)
        y_min = int(n * (1 - math.log(math.tan(lat_rad_max) + 1 / math.cos(lat_rad_max)) / math.pi) / 2)

        x_min = max(0, x_min)
        x_max = min(n - 1, x_max)
        y_min = max(0, y_min)
        y_max = min(n - 1, y_max)

        if tile_size == 512:
            # Convert source tile range to output tile range (halve coordinates)
            out_x_min = x_min // 2
            out_x_max = x_max // 2
            out_y_min = y_min // 2
            out_y_max = y_max // 2
            tile_count = (out_x_max - out_x_min + 1) * (out_y_max - out_y_min + 1)
            print(f"    z{z}: {tile_count} tiles ({out_x_max - out_x_min + 1}x{out_y_max - out_y_min + 1}) [512px, src z{src_z}]")
            process_fn = _process_tile_512
            tile_coords = [(z, x, y) for x in range(out_x_min, out_x_max + 1)
                           for y in range(out_y_min, out_y_max + 1)]
        else:
            tile_count = (x_max - x_min + 1) * (y_max - y_min + 1)
            print(f"    z{z}: {tile_count} tiles ({x_max - x_min + 1}x{y_max - y_min + 1})")
            process_fn = _process_tile_256
            tile_coords = [(z, x, y) for x in range(x_min, x_max + 1)
                           for y in range(y_min, y_max + 1)]

        # Small zoom levels: process sequentially
        if tile_count <= 10:
            for coords in tile_coords:
                downloaded, jpeg_bytes, out_bytes = process_fn(*coords)
                if downloaded:
                    total_downloaded += 1
                    total_bytes_jpeg += jpeg_bytes
                    total_bytes_out += out_bytes
                else:
                    total_skipped += 1
            continue

        # Larger zoom levels: process in parallel
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(process_fn, *t): t for t in tile_coords}
            for future in as_completed(futures):
                downloaded, jpeg_bytes, out_bytes = future.result()
                if downloaded:
                    total_downloaded += 1
                    total_bytes_jpeg += jpeg_bytes
                    total_bytes_out += out_bytes
                elif downloaded is None:
                    total_missing += 1
                else:
                    total_skipped += 1
                completed += 1
                if completed % 500 == 0:
                    print(f"\r    Processed {total_downloaded} tiles ({total_skipped} cached)...", end="", flush=True)

    print(f"\r    Produced {total_downloaded} satellite tiles ({total_skipped} cached)")
    if total_missing:
        # A tile with a missing quadrant is neither written nor cached, so
        # it is a hole in the imagery (re-fetched next run). Say so instead
        # of folding it into "cached".
        print(f"    WARNING: {total_missing} satellite tiles skipped — a source "
              f"quadrant failed to download (holes in imagery)", flush=True)
    if total_bytes_jpeg > 0:
        saved_mb = (total_bytes_jpeg - total_bytes_out) / (1024 * 1024)
        ratio = (1 - total_bytes_out / total_bytes_jpeg) * 100
        print(f"    {sat_format.upper()} compression saved {saved_mb:.1f} MB ({ratio:.0f}% vs JPEG source)")
    return total_downloaded + total_skipped


def stitch_satellite_image(satellite_dir, max_zoom, bbox_str, webp_quality=80):
    """Stitch max-zoom satellite tiles into a single image.

    Returns (image_path, coordinates) where coordinates is the MapLibre
    image source format: [[west,north],[east,north],[east,south],[west,south]].
    """
    import math

    from PIL import Image

    bbox = parse_bbox(bbox_str)
    minlon, minlat, maxlon, maxlat = bbox
    n = 2 ** max_zoom

    x_min = int(n * (minlon + 180) / 360)
    x_max = int(n * (maxlon + 180) / 360)
    lat_rad_min = math.radians(minlat)
    lat_rad_max = math.radians(maxlat)
    y_max = int(n * (1 - math.log(math.tan(lat_rad_min) + 1 / math.cos(lat_rad_min)) / math.pi) / 2)
    y_min = int(n * (1 - math.log(math.tan(lat_rad_max) + 1 / math.cos(lat_rad_max)) / math.pi) / 2)

    x_min = max(0, x_min)
    x_max = min(n - 1, x_max)
    y_min = max(0, y_min)
    y_max = min(n - 1, y_max)

    cols = x_max - x_min + 1
    rows = y_max - y_min + 1
    width = cols * 256
    height = rows * 256
    print(f"    Stitching {cols}x{rows} tiles ({width}x{height} px) from z{max_zoom}...")

    stitched = Image.new("RGB", (width, height))
    for x in range(x_min, x_max + 1):
        for y in range(y_min, y_max + 1):
            tile_path = os.path.join(satellite_dir, str(max_zoom), str(x), f"{y}.webp")
            if os.path.exists(tile_path):
                tile_img = Image.open(tile_path)
                px = (x - x_min) * 256
                py = (y - y_min) * 256
                stitched.paste(tile_img, (px, py))

    output_path = os.path.join(satellite_dir, "stitched.webp")
    stitched.save(output_path, "WEBP", quality=webp_quality)
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"    Stitched image: {size_mb:.1f} MB")

    # Geographic bounds of the stitched image (tile edges, not bbox)
    west = x_min / n * 360 - 180
    east = (x_max + 1) / n * 360 - 180
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y_min / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y_max + 1) / n))))

    # MapLibre image source coordinates: [lng, lat] for each corner
    coordinates = [
        [west, north],   # top-left
        [east, north],   # top-right
        [east, south],   # bottom-right
        [west, south],   # bottom-left
    ]

    return output_path, coordinates
