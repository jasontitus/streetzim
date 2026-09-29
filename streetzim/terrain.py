"""Copernicus DEM -> terrain-RGB tiles (moved verbatim from
create_osm_zim.py, which re-exports these names).

_generate_one_terrain_tile runs in spawn-started worker processes, so it
stays at module level (pickled by module path)."""
import itertools
import os
import subprocess
import time
import urllib.error
import urllib.request

# The builder's flushing, phase-timing print (see streetzim/common.py).
from streetzim.common import (
    print,
    CACHE_DIR,
    COPERNICUS_DEM_URL,
    COPERNICUS_DEM_URL_GLO90,
    parse_bbox,
)


# One open DEM handle per worker process, keyed by path (see _generate_one_terrain_tile).
_DEM_HANDLES = {}


def _generate_one_terrain_tile(args):
    """Generate a single terrain-RGB tile. Module-level for multiprocessing.

    Each process opens its own handle to the VRT/mosaic — GDAL reads only
    the pixels needed from the underlying GeoTIFFs."""
    mosaic_file, tile_x, tile_y, z, dest_dir_local, tb_west, tb_south, tb_east, tb_north = args
    import rasterio
    from rasterio.warp import reproject, Resampling, transform_bounds
    from rasterio.transform import from_bounds
    import numpy as np
    from PIL import Image

    tile_bounds_3857 = transform_bounds(
        "EPSG:4326", "EPSG:3857", tb_west, tb_south, tb_east, tb_north
    )
    # Rasterise with a 2-pixel halo on every side and crop the centre.
    # Cubic resampling looks at a 4-pixel window; at a plain 256×256
    # extent that window was truncated on the tile edge, so neighbouring
    # tiles disagreed along their shared edge (visible seams). This is
    # the buffered generator from cloud/fix_terrain_seams.py folded into
    # the builder, so seams are prevented instead of repaired after.
    west3857, south3857, east3857, north3857 = tile_bounds_3857
    px_w = (east3857 - west3857) / 256.0
    px_h = (north3857 - south3857) / 256.0
    HALO = 2
    BUF = 256 + 2 * HALO
    tile_transform = from_bounds(
        west3857 - HALO * px_w, south3857 - HALO * px_h,
        east3857 + HALO * px_w, north3857 + HALO * px_h, BUF, BUF)

    elevation = np.zeros((1, BUF, BUF), dtype=np.float32)
    # Reopening the VRT per tile costs ~14 ms at 2,000 sources and ~380 ms at
    # 26,000 — which would dominate a multi-million-tile run. Workers are
    # long-lived, so keep one handle per (process, path).
    src = _DEM_HANDLES.get(mosaic_file)
    if src is None:
        src = rasterio.open(mosaic_file)
        _DEM_HANDLES[mosaic_file] = src
    reproject(
        source=rasterio.band(src, 1),
        destination=elevation,
        dst_transform=tile_transform,
        dst_crs="EPSG:3857",
        resampling=Resampling.cubic,
    )

    elev = elevation[0, HALO:HALO + 256, HALO:HALO + 256]
    elev = np.round(elev / 10.0) * 10.0  # quantize to 10m for ~74% compression savings
    encoded = ((elev + 10000.0) / 0.1).astype(np.uint32)
    encoded = np.clip(encoded, 0, 16777215)

    r = ((encoded >> 16) & 0xFF).astype(np.uint8)
    g = ((encoded >> 8) & 0xFF).astype(np.uint8)
    b = (encoded & 0xFF).astype(np.uint8)

    img = Image.fromarray(np.stack([r, g, b], axis=-1))
    tile_dir_path = os.path.join(dest_dir_local, str(z), str(tile_x))
    os.makedirs(tile_dir_path, exist_ok=True)
    tile_path = os.path.join(tile_dir_path, f"{tile_y}.webp")
    # Atomic: a worker killed mid-save must not leave a truncated tile
    # that every later run treats as cached.
    tmp_path = f"{tile_path}.{os.getpid()}.tmp"
    img.save(tmp_path, "WEBP", lossless=True)
    os.replace(tmp_path, tile_path)


def _terrain_vrt_for_zoom(z, mosaic_path, low_zoom_world_vrt=None):
    """Choose the VRT used for terrain generation at a given zoom."""
    if z <= 7 and low_zoom_world_vrt and os.path.isfile(low_zoom_world_vrt):
        return low_zoom_world_vrt
    return mosaic_path


# A terrain-RGB tile holding real elevation compresses to a few hundred
# bytes at minimum; 44-byte files are the signature of a DEM fetch that
# failed. Anything smaller than this is regenerated rather than reused.
_DEM_MIN_TIF_BYTES = 1000
# II*\0 little-endian, MM\0* big-endian, and the BigTIFF variants.
_TIFF_MAGICS = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
_TERRAIN_MIN_REUSE_BYTES = int(os.environ.get("TERRAIN_MIN_REUSE_BYTES", "200"))

# The window in which terrain was rasterised against the wrong DEM: from the
# mtime of the Hispaniola-only mosaic_4326.tif to the moment _build_dem_vrt
# replaced that fallback. Small tiles written inside it are regenerated once.
# Overridable so a future incident can reuse the same machinery.
_TERRAIN_POISON_FROM = float(os.environ.get(
    "TERRAIN_POISON_FROM", "1773960843"))   # 2026-03-19 23:54 CET
# Must sit AFTER the last build that ran the broken fallback (the 2026-09-10
# rebuild pass ended 12:56:44) and BEFORE any tile written by the fixed code,
# or regenerated tiles land back inside the window and the cache never settles.
_TERRAIN_POISON_UNTIL = float(os.environ.get(
    "TERRAIN_POISON_UNTIL", "1789038000"))  # 2026-09-10 13:00 CEST


def _build_dem_vrt(tif_paths, out_path, res=1.0/3600.0, want_bbox=None):
    """Write a mosaic VRT for DEM tiles without needing the gdalbuildvrt CLI.

    This host has no GDAL command-line tools (rasterio ships libgdal, not the
    binaries), so the old code silently fell through to a merge() fallback
    that reused ONE shared `mosaic_4326.tif` for every region. That file was
    built from Hispaniola on 2026-03-19 and covers -75..-68E / 17..21N, so
    every terrain tile generated anywhere else since then rasterised against
    nodata and came out a 44-byte blank. Hence the blank-terrain Carolinas
    (2026-07) and this round's mexico/argentina/alaska/himalayas failures.

    Copernicus GLO-30 tiles are 1 degree square but longitudinally subsampled
    above 50 degrees (3600/1800/1200/720/360 columns), so the sources have
    mixed x-resolution. A ComplexSource maps each source's pixel rect onto the
    common output grid, and GDAL rescales on read -- which is exactly what
    gdalbuildvrt itself emits.
    """
    import rasterio
    from xml.sax.saxutils import escape as _xesc

    srcs = []
    for path in tif_paths:
        try:
            with rasterio.open(path) as ds:
                srcs.append((path, ds.bounds, ds.width, ds.height,
                             ds.block_shapes[0], str(ds.dtypes[0]), ds.nodata))
        except Exception as e:
            print(f"    Warning: skipping unreadable DEM {os.path.basename(path)}: {e}")
    if not srcs:
        return None

    west = min(b.left for _, b, *_ in srcs)
    east = max(b.right for _, b, *_ in srcs)
    south = min(b.bottom for _, b, *_ in srcs)
    north = max(b.top for _, b, *_ in srcs)
    xsize = int(round((east - west) / res))
    ysize = int(round((north - south) / res))
    dtype = srcs[0][5]
    gdal_dtype = {"float32": "Float32", "float64": "Float64",
                  "int16": "Int16", "int32": "Int32",
                  "uint16": "UInt16"}.get(dtype, "Float32")

    parts = [
        f'<VRTDataset rasterXSize="{xsize}" rasterYSize="{ysize}">',
        '  <SRS>EPSG:4326</SRS>',
        f'  <GeoTransform>{west:.12f}, {res:.12f}, 0.0, {north:.12f}, 0.0, {-res:.12f}</GeoTransform>',
        f'  <VRTRasterBand dataType="{gdal_dtype}" band="1">',
        '    <ColorInterp>Gray</ColorInterp>',
    ]
    for path, b, w, h, block, _dt, nodata in srcs:
        # Destination rect on the common grid. Note GLO-30 tiles are
        # pixel-edge registered at a half-pixel offset that differs per
        # resolution band (3600 cols below 50 deg, then 2400/1800/1200/720/360),
        # so this rounds rather than landing exactly: the residual is
        # 0.5*(res_grid - res_src)/res_grid, i.e. 0.25 px for a 2400-col source
        # and at worst ~4.5 px above 85 deg, where cos(lat) shrinks it to ~12 m
        # on the ground. Sub-pixel at every terrain zoom, and gdalbuildvrt
        # rounds the same way. Neighbouring same-band tiles share an offset, so
        # rows still tile without gaps or overlaps.
        dx = int(round((b.left - west) / res))
        dy = int(round((north - b.top) / res))
        dw = int(round((b.right - b.left) / res))
        dh = int(round((b.top - b.bottom) / res))
        parts.append('    <ComplexSource>')
        parts.append(f'      <SourceFilename relativeToVRT="0">{_xesc(path)}</SourceFilename>')
        parts.append('      <SourceBand>1</SourceBand>')
        parts.append(f'      <SourceProperties RasterXSize="{w}" RasterYSize="{h}" '
                     f'DataType="{gdal_dtype}" BlockXSize="{block[1]}" BlockYSize="{block[0]}"/>')
        parts.append(f'      <SrcRect xOff="0" yOff="0" xSize="{w}" ySize="{h}"/>')
        parts.append(f'      <DstRect xOff="{dx}" yOff="{dy}" xSize="{dw}" ySize="{dh}"/>')
        if nodata is not None:
            parts.append(f'      <NODATA>{nodata}</NODATA>')
        parts.append('    </ComplexSource>')
    parts.append('  </VRTRasterBand>')
    parts.append('</VRTDataset>')

    # pid-suffixed so two builds whose bboxes round to the same key cannot
    # clobber each other's staging file.
    tmp = f"{out_path}.{os.getpid()}.part"
    try:
        with open(tmp, "w") as f:
            f.write("\n".join(parts) + "\n")
        with rasterio.open(tmp) as ds:
            got = ds.bounds

        # The bug this function exists to prevent was a DEM mosaic that did not
        # cover the region being built, so check coverage before publishing the
        # VRT rather than merely that the file opens.
        #
        # A shortfall of a degree or less is normal and not an error: DEM cells
        # over open ocean legitimately 404, so a bbox whose corner sits in the
        # sea has no source there and the union rectangle falls short. Warn on
        # those. Only a gross shortfall means the wrong DEM — the Hispaniola
        # mosaic missed its regions by tens of degrees.
        if want_bbox:
            wlon, wlat, elon, nlat = want_bbox
            short = {
                "west": got.left - wlon, "east": elon - got.right,
                "south": got.bottom - wlat, "north": nlat - got.top,
            }
            missing = {k: v for k, v in short.items() if v > res}
            if missing:
                desc = ", ".join(f"{k} short by {v:.2f} deg"
                                 for k, v in sorted(missing.items()))
                if max(missing.values()) > 1.0:
                    raise RuntimeError(
                        f"DEM VRT {out_path} does not cover the requested bbox "
                        f"({wlon},{wlat},{elon},{nlat}): {desc}. Refusing to "
                        f"rasterise terrain against the wrong DEM.")
                print(f"    Note: DEM stops short of the bbox ({desc}) — "
                      f"expected where the edge is open ocean.")
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(f"    VRT built: {len(srcs)} DEM tiles, {xsize}x{ysize} px, "
          f"bbox {west:.2f},{south:.2f},{east:.2f},{north:.2f}")
    return out_path


def _dem_tif_is_usable(path, deep=False):
    """True if a DEM GeoTIFF is present, big enough, and has TIFF magic.

    One predicate for the download gate, the marker-clear and the verification
    VRT, so they cannot disagree about what "a real DEM" means (they used
    <1000, >=1000 and >1000). deep=True additionally reads the last row to
    catch truncation; it costs a read, so only the rare marker-clear uses it.
    Also swallows the stat race: a concurrent build replacing the file must not
    kill this one with an uncaught OSError.
    """
    try:
        if os.path.getsize(path) < _DEM_MIN_TIF_BYTES:
            return False
        with open(path, "rb") as fh:
            magic = fh.read(4)
    except OSError:
        return False
    if magic not in _TIFF_MAGICS:
        return False
    if not deep:
        return True
    # Magic only rules out non-TIFF content (HTML error pages, zero-filled
    # files). A TRUNCATED GeoTIFF keeps its header and passes it — review
    # showed a 3 KB head of a real COG opens as 1200x1200 and fails only on
    # read. So the deep form reads the last row, where truncation lands.
    try:
        import rasterio
        from rasterio.windows import Window
        with rasterio.open(path) as ds:
            ds.read(1, window=Window(0, ds.height - 1, ds.width, 1))
        return True
    except Exception:
        return False


def _bbox_tile_total(minlon, minlat, maxlon, maxlat, max_zoom):
    """Tile count for a bbox over z0..max_zoom, without touching the disk."""
    import math
    import mercantile
    total = 0
    for z in range(0, max_zoom + 1):
        if z <= 8:
            total += sum(1 for _ in mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=z))
        else:
            n = 2 ** z
            x_min = int((minlon + 180) / 360 * n)
            x_max = int((maxlon + 180) / 360 * n)
            lat_hi, lat_lo = maxlat, max(minlat, -85)
            y_min = int((1 - math.log(math.tan(math.radians(lat_hi)) +
                                      1 / math.cos(math.radians(lat_hi))) / math.pi) / 2 * n)
            y_max = int((1 - math.log(math.tan(math.radians(lat_lo)) +
                                      1 / math.cos(math.radians(lat_lo))) / math.pi) / 2 * n)
            total += (x_max - x_min + 1) * (y_max - y_min + 1)
    return total


def _terrain_tile_usable(path):
    """True if a cached terrain tile can be trusted and reused.

    Size alone cannot answer this. A terrain-RGB tile of constant elevation
    compresses to exactly 44 bytes, so an ocean tile is byte-identical in size
    to one whose DEM read failed — Hawaii's cache is 99% legitimate 44-byte
    ocean. Treating every small tile as broken would regenerate them on every
    build forever and permanently defeat the COMPLETED-marker fast path.

    What actually separates them here is *when* the tile was written. From
    2026-03-19 until the VRT fix below, `gdalbuildvrt` was missing on this host
    and terrain fell back to one shared `mosaic_4326.tif` covering only
    Hispaniola, so every small tile written in that window is suspect while
    every small tile written outside it is genuine flat ground. Sampling the
    cache bears this out: sub-200-byte tiles exist only in 2026-04/05/09, never
    in 2026-03.

    Each suspect tile is regenerated exactly once; the rewrite moves its mtime
    past the window, so genuinely-flat tiles settle as cached and the cache
    converges instead of churning. TERRAIN_MIN_REUSE_BYTES=0 trusts everything.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    # A real tile is never smaller than the 44-byte constant-elevation floor,
    # whatever its mtime says. Truncated leftovers (pre-atomic-write builds, an
    # interrupted rsync) must not be trusted just for being old.
    if st.st_size < 40:
        return False
    if _TERRAIN_MIN_REUSE_BYTES == 0:
        return True
    if st.st_size >= _TERRAIN_MIN_REUSE_BYTES:
        return True
    return not (_TERRAIN_POISON_FROM <= st.st_mtime < _TERRAIN_POISON_UNTIL)


def generate_terrain_tiles(bbox_str, dest_dir, max_zoom=12,
                           low_zoom_world_vrt=None):
    """Download Copernicus GLO-30 DEM and generate terrain-RGB tiles.

    Downloads 1-degree GeoTIFF tiles from AWS, mosaics them, then generates
    Mapbox terrain-RGB tiles as lossless WebP using rasterio + mercantile.
    Tiles are stored as {dest_dir}/{z}/{x}/{y}.webp.

    ``low_zoom_world_vrt`` (optional): if provided, z=0-7 tiles are
    generated from that DEM instead of the region-bbox mosaic. Prevents
    the bbox-edge stripe bug at low zooms where a tile's footprint
    extends past the region and zero-fills outside. z=8+ still use the
    regional mosaic (fine-grained detail, no stripe risk since each
    tile is small).

    An area across the antimeridian is generated one side at a time.
    """
    import math
    from streetzim import area

    if area.crosses(parse_bbox(bbox_str)):
        return sum(generate_terrain_tiles(area.to_str(part), dest_dir, max_zoom=max_zoom,
                                          low_zoom_world_vrt=low_zoom_world_vrt) or 0
                   for part in area.split(parse_bbox(bbox_str)))

    bbox = parse_bbox(bbox_str)
    minlon, minlat, maxlon, maxlat = bbox

    os.makedirs(dest_dir, exist_ok=True)
    # Always use the shared DEM sources directory (large raw files, ~547 GB total)
    dem_dir = os.path.join(CACHE_DIR, "terrain_cache", "dem_sources")
    os.makedirs(dem_dir, exist_ok=True)

    # Check if terrain generation is already complete for THIS SPECIFIC bbox.
    # The marker encodes the bbox so a Europe build can't fool a US build.
    import mercantile
    bbox_key = f"{minlon:.1f}_{minlat:.1f}_{maxlon:.1f}_{maxlat:.1f}"
    completed_marker = os.path.join(dest_dir, f"COMPLETED_z{max_zoom}_{bbox_key}")

    def _blank_sample():
        """First cached tile in this bbox too small to hold elevation.

        Both fast paths below skip terrain generation wholesale, so a
        stale 44-byte blank (a DEM fetch that failed months ago) was never
        revisited no matter what the per-tile check does — himalayas came
        back with 498 blank land tiles in the 2026-09 round because the
        COMPLETED marker meant the generator never ran.

        This scans EVERY tile in the bbox, not a sample: a 400-tile sample
        missed the single blank among midwest-us's 74,842 land tiles, and
        one blank tile is still a hole in the map and still fails the
        release gate. It is one getsize per tile with an early exit on the
        first hit, which is seconds against a build measured in hours.
        """
        for z in range(max_zoom, -1, -1):
            for t in mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=z):
                fp = os.path.join(dest_dir, str(z), str(t.x), f"{t.y}.webp")
                if os.path.exists(fp):
                    # Must use the SAME rule as the generator. A bare size test
                    # here condemned every legitimate 44-byte ocean tile, so for
                    # any coastal region this returned a hit on the first tile
                    # it touched, forever: the marker fast path was reported
                    # stale on every build, the generator then correctly
                    # regenerated nothing, and the marker was rewritten — a
                    # permanent full rescan that never converged.
                    if not _terrain_tile_usable(fp):
                        return f"z{z}/{t.x}/{t.y} (blank)"
                else:
                    # ABSENT counts too. These fast paths skip the generator
                    # wholesale, so "absent is the generator's business" was
                    # wrong: the generator never runs. alaska came back with
                    # 4,030 missing-land tiles that way, after a regeneration
                    # sweep was interrupted and left holes the COMPLETED
                    # marker then hid.
                    return f"z{z}/{t.x}/{t.y} (absent)"
        return None

    if os.path.isfile(completed_marker):
        blank = _blank_sample()
        if blank is None:
            # Count this bbox's tiles arithmetically. This used to os.walk the
            # whole of terrain_cache — 22 million files shared by every region
            # — which made the "everything is cached" path by far the SLOWEST
            # one (minutes of disk sleep), and reported a global count as if it
            # were the region's. The value is only printed.
            total = _bbox_tile_total(minlon, minlat, maxlon, maxlat, max_zoom)
            print(f"    Using ~{total} cached terrain tiles "
                  f"(generation complete for {bbox_key})", flush=True)
            return total
        print(f"    Cached terrain for {bbox_key} is marked complete but {blank} "
              f"— regenerating missing/undersized tiles", flush=True)

    # Fallback: sample z-max tiles at the CORNERS AND CENTER of this bbox
    # to check if they're cached. More robust than just first/last.
    z_max_tiles = list(mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=max_zoom))
    if z_max_tiles:
        # Sample corners + center of the bbox tile range
        n_tiles = len(z_max_tiles)
        sample_indices = [0, n_tiles//4, n_tiles//2, 3*n_tiles//4, n_tiles-1]
        sample = [z_max_tiles[i] for i in sample_indices if i < n_tiles]
        all_cached = all(
            _terrain_tile_usable(
                os.path.join(dest_dir, str(max_zoom), str(t.x), f"{t.y}.webp"))
            for t in sample
        )
        if all_cached and _blank_sample() is None:
            total = sum(
                len([f for f in files if f.endswith(".webp")])
                for _, _, files in os.walk(dest_dir)
                if "dem_sources" not in _
            )
            print(f"    Using {total} cached terrain tiles")
            return total

    # Determine which 1-degree Copernicus tiles we need.
    # Include a 1-degree BUFFER around the bbox so that tiles at degree
    # boundaries get correct data from neighboring DEM cells.
    tif_paths = []
    transient_dem_failures = []
    for lat in range(math.floor(minlat) - 1, math.floor(maxlat) + 2):
        for lon in range(math.floor(minlon) - 1, math.floor(maxlon) + 2):
            # The 1-cell halo can step off the edge of the world: alaska's
            # minlon=-180 asks for dem_N**_W181, which exists nowhere and
            # 404s on every source, so each build wrote a bogus `.nodata`
            # marker for it. GLO-30 cells are named for their SW corner, so
            # the valid range is lat -90..89, lon -180..179.
            if not (-90 <= lat <= 89 and -180 <= lon <= 179):
                continue
            ns = "N" if lat >= 0 else "S"
            ew = "E" if lon >= 0 else "W"
            abs_lat = abs(lat)
            abs_lon = abs(lon)
            url = COPERNICUS_DEM_URL.format(ns=ns, lat=abs_lat, ew=ew, lon=abs_lon)
            fname = f"dem_{ns}{abs_lat:02d}_{ew}{abs_lon:03d}.tif"
            fpath = os.path.join(dem_dir, fname)

            # Check for a "no data" marker (empty file left by a previous 404).
            #
            # A marker must NEVER shadow a real DEM. The GLO-90 fallback below
            # was added after some of these markers were written, so five cells
            # (Georgia/Armenia/Azerbaijan and the Caspian — dem_N39_E048,
            # N40_E049, N40_E050, N41_E043, N41_E046, carrying terrain up to
            # 4,113 m) ended up with BOTH a usable .tif and a stale marker, and
            # were silently dropped from every VRT covering them. That is why
            # central-asia's 2026-09-11 build aborted: 263 Caspian tiles
            # rasterised as 0 m against a DEM that reads -28 m. Clear the
            # marker when the file is usable instead of skipping the cell.
            nodata_marker = fpath + ".nodata"
            if os.path.exists(nodata_marker):
                # Only clear the marker for a DEM that actually reads. Pre-
                # atomic-write builds left truncated .tif files; a truncated
                # GeoTIFF keeps its header, so size and magic both pass — hence
                # the deep read here. If it fails, the cell is skipped with its
                # marker left in place (treated as absent, not re-downloaded);
                # clearing the marker then would admit a corrupt DEM to the VRT.
                if _dem_tif_is_usable(fpath, deep=True):
                    print(f"    Clearing stale .nodata marker shadowing {fname}",
                          flush=True)
                    try:
                        os.unlink(nodata_marker)
                    except OSError:
                        pass
                else:
                    continue

            if not _dem_tif_is_usable(fpath):
                # Try GLO-30 first, fall back to GLO-90 for restricted regions
                # (Georgia, Armenia, Azerbaijan etc. that 404 on GLO-30).
                glo90_url = COPERNICUS_DEM_URL_GLO90.format(ns=ns, lat=abs_lat, ew=ew, lon=abs_lon)
                downloaded = False
                all_404 = True   # only a 404 from EVERY source means "ocean"
                for try_url, label in [(url, "GLO-30"), (glo90_url, "GLO-90 fallback")]:
                    print(f"    Downloading {ns}{abs_lat:02d} {ew}{abs_lon:03d} ({label})...")
                    req = urllib.request.Request(try_url, headers={"User-Agent": "streetzim/1.0"})
                    got = False
                    for attempt in range(1, 4):
                        # Download to a temp file and rename only when the
                        # body is complete + looks like a TIFF. Writing in
                        # place left a truncated .tif (> 1000 bytes passes
                        # every size check) that gdalbuildvrt then used.
                        tmp_path = fpath + ".part"
                        try:
                            with urllib.request.urlopen(req, timeout=120) as resp:
                                with open(tmp_path, "wb") as f:
                                    while True:
                                        chunk = resp.read(1024 * 1024)
                                        if not chunk:
                                            break
                                        f.write(chunk)
                            with open(tmp_path, "rb") as f:
                                magic = f.read(4)
                            # Classic TIFF or BigTIFF (download_dem.py accepts both).
                            if magic not in _TIFF_MAGICS:
                                raise OSError("response is not a TIFF (truncated or HTML error page)")
                            os.replace(tmp_path, fpath)
                            size_mb = os.path.getsize(fpath) / (1024 * 1024)
                            print(f"      {size_mb:.1f} MB ({label})")
                            got = True
                            break
                        except urllib.error.HTTPError as e:
                            if e.code == 404:
                                print(f"      404 on {label}, trying next source...")
                                break
                            all_404 = False
                            print(f"      Warning: HTTP {e.code} from {label} (attempt {attempt}/3)")
                        except Exception as e:
                            all_404 = False
                            print(f"      Warning: failed to download from {label} (attempt {attempt}/3): {e}")
                        finally:
                            if os.path.exists(tmp_path):
                                try: os.remove(tmp_path)
                                except OSError: pass
                        if attempt < 3:
                            time.sleep(2 * attempt)
                    if got:
                        downloaded = True
                        break
                if not downloaded:
                    if all_404:
                        # 404 from both GLO-30 and GLO-90 — genuinely no
                        # data (ocean). Persist that so we don't re-ask.
                        open(nodata_marker, "w").close()
                    else:
                        # Transient failure: do NOT write the marker — it
                        # used to brand a land cell as ocean forever, and
                        # every later build zero-filled real terrain there.
                        # Remember it: continuing would rasterise the cell
                        # as 0 m AND write the COMPLETED marker, so the
                        # "retry" would never happen. We abort the terrain
                        # step below instead.
                        transient_dem_failures.append(f"{ns}{abs_lat:02d}{ew}{abs_lon:03d}")
                    continue
            else:
                size_mb = os.path.getsize(fpath) / (1024 * 1024)
                print(f"    Cached: {ns}{abs_lat:02d} {ew}{abs_lon:03d} ({size_mb:.1f} MB)")
            tif_paths.append(fpath)

    if transient_dem_failures:
        raise RuntimeError(
            f"{len(transient_dem_failures)} DEM cell(s) could not be downloaded "
            f"this run ({', '.join(transient_dem_failures[:8])}"
            f"{'…' if len(transient_dem_failures) > 8 else ''}); refusing to "
            f"rasterise them as 0 m and cache the result. Re-run the build.")
    if not tif_paths:
        print("    No DEM tiles downloaded, skipping terrain")
        return 0

    # Build a VRT (Virtual Raster) instead of loading all DEMs into memory.
    # A VRT is a lightweight XML file that references source tiles on disk.
    # rasterio reads only the pixels needed for each terrain tile on demand.
    print("    Building VRT from DEM tiles...")
    import rasterio  # noqa: F401 -- fail before building the VRT if missing
    import mercantile

    # Use a UNIQUE VRT path per bbox to avoid race conditions when two
    # builds run in parallel and overwrite each other's VRT.
    mosaic_path = os.path.join(dem_dir, f"mosaic_{bbox_key}.vrt")
    try:
        # Use -input_file_list to avoid "Argument list too long" with 24K+ files
        import tempfile as _tmpfile
        with _tmpfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as flist:
            flist.write('\n'.join(tif_paths))
            flist_path = flist.name
        try:
            subprocess.run(
                ["gdalbuildvrt", "-overwrite", "-input_file_list", flist_path, mosaic_path],
                check=True, capture_output=True, text=True,
            )
        finally:
            # Must be in a finally: gdalbuildvrt is absent on this host, so the
            # raise below skipped the unlink and every build leaked a /tmp file.
            os.unlink(flist_path)
    except FileNotFoundError:
        # gdalbuildvrt not on PATH — build the same VRT XML ourselves.
        #
        # NEVER fall back to a shared single-file mosaic here. The previous
        # code did, keyed on a fixed `mosaic_4326.tif` with no bbox check, and
        # silently rasterised every region against a Hispaniola-only DEM for
        # six months. A per-bbox VRT is cheap (XML over the tiles already on
        # disk) and cannot be reused for the wrong region.
        print("    gdalbuildvrt not found; building VRT directly")
        if _build_dem_vrt(tif_paths, mosaic_path,
                          want_bbox=(minlon, minlat, maxlon, maxlat)) is None:
            print("    No readable DEM tiles, skipping terrain")
            return 0

    # Generate terrain-RGB tiles using multiprocessing.
    # Each process opens its own handle to the VRT file — GDAL reads only the
    # pixels needed per tile from the underlying GeoTIFFs. No shared state.
    # Uses a streaming generator so workers start immediately without building
    # a multi-million element list in memory (world z12 = 16.7M tiles).
    print(f"    Generating terrain-RGB tiles (z0-{max_zoom})...")
    count = 0
    cached = 0
    import multiprocessing

    num_workers = min(os.cpu_count() or 4, 16)  # cap at 16 to limit I/O contention

    for z in range(0, max_zoom + 1):
        # For z=0-7, prefer the world-coverage VRT if supplied — those
        # tiles span regions past the bbox, so a regional mosaic would
        # zero-fill outside and produce the bbox-edge stripe bug
        # (Iran 33°N, Butte MT, east-Iran 65°E). z=8+ stays on the
        # regional mosaic (small tiles, full DEM resolution, no stripe).
        vrt_for_z = _terrain_vrt_for_zoom(z, mosaic_path, low_zoom_world_vrt)

        # Streaming generator — yields args one at a time, skipping cached tiles
        def tile_arg_gen(zoom, _vrt=vrt_for_z):
            for tile in mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=zoom):
                # Reuse a cached tile only if it is big enough to hold real
                # elevation. A DEM download that failed at generation time
                # leaves a 44-byte blank, and `isfile()` happily reused it
                # for every later build — that is how the blank-terrain
                # Carolinas shipped in 2026-07, and why alaska (228),
                # argentina (406) and mexico (4,491) failed the terrain gate
                # in this round with the same stale entries.
                #
                # Below the threshold, regenerate. Genuinely-blank ocean
                # tiles simply come back blank (a DEM sample each, ~450
                # tiles/s), which is a small price for never shipping a
                # blank mountain range again. TERRAIN_MIN_REUSE_BYTES=0
                # restores the old reuse-anything behaviour.
                tile_path = os.path.join(dest_dir, str(zoom), str(tile.x), f"{tile.y}.webp")
                if _terrain_tile_usable(tile_path):
                    continue
                b = mercantile.bounds(tile)
                yield (_vrt, tile.x, tile.y, zoom, dest_dir,
                       b.west, b.south, b.east, b.north)

        # Count total and cached for this zoom (estimate for large zooms).
        #
        # Only z<=8 is pre-counted. Above that the region's tile count is an
        # estimate while the cache directory is shared by every region ever
        # built, so any directory-wide count is meaningless here: comparing a
        # global `cached` against a regional `total` made `need` negative and
        # skipped z9-z12 outright for every region with a populated cache.
        # For z>8 we stream instead and let the per-tile check decide, which
        # is one stat pass rather than two.
        cached_at_z = None
        if z <= 8:
            all_tiles = list(mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=z))
            total_at_z = len(all_tiles)
            cached_at_z = sum(1 for t in all_tiles
                              if _terrain_tile_usable(
                                  os.path.join(dest_dir, str(z), str(t.x), f"{t.y}.webp")))
        else:
            # For large zoom levels, estimate count from 4x previous zoom
            import math
            n = 2 ** z
            x_min = int((minlon + 180) / 360 * n)
            x_max = int((maxlon + 180) / 360 * n)
            y_min = int((1 - math.log(math.tan(math.radians(maxlat)) + 1/math.cos(math.radians(maxlat))) / math.pi) / 2 * n)
            y_max = int((1 - math.log(math.tan(math.radians(max(minlat, -85))) + 1/math.cos(math.radians(max(minlat, -85)))) / math.pi) / 2 * n)
            total_at_z = (x_max - x_min + 1) * (y_max - y_min + 1)

        if cached_at_z is None:
            need = None
            print(f"      z{z}: ~{total_at_z} tiles (streaming; generating missing/blank)...",
                  flush=True)
        else:
            need = total_at_z - cached_at_z
            if need <= 0:
                cached += cached_at_z
                print(f"      z{z}: {total_at_z} tiles (all cached)")
                continue
            print(f"      z{z}: {total_at_z} tiles ({cached_at_z} cached, {need} to generate)")
        z_count = 0

        if total_at_z <= 10:
            for args in tile_arg_gen(z):
                _generate_one_terrain_tile(args)
                z_count += 1
                count += 1
        else:
            # Peek before spawning: with nothing to generate this would still
            # start (and tear down) a 16-process spawn Pool for every zoom.
            gen = tile_arg_gen(z)
            first = next(gen, None)
            if first is None:
                z_cached = cached_at_z if cached_at_z is not None else total_at_z
                cached += z_cached
                print(f"      z{z}: 0 generated, {z_cached} cached          ", flush=True)
                continue
            gen = itertools.chain([first], gen)
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(num_workers) as pool:
                for _ in pool.imap_unordered(_generate_one_terrain_tile,
                                              gen, chunksize=256):
                    z_count += 1
                    count += 1
                    if z_count % 5000 == 0:
                        print(f"\r      z{z}: {z_count}/{need or total_at_z} generated...",
                              end="", flush=True)

        z_cached = cached_at_z if cached_at_z is not None else max(total_at_z - z_count, 0)
        cached += z_cached
        print(f"\r      z{z}: {z_count} generated, {z_cached} cached          ", flush=True)

    print(f"    Terrain complete: {count} generated, {cached} cached")
    # Write completion marker so future builds skip terrain entirely
    with open(completed_marker, "w") as f:
        f.write(f"{count + cached}\n")
    return count + cached
