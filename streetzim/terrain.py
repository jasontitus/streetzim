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


def _bbox_tile_total(minlon, minlat, maxlon, maxlat, max_zoom, min_zoom=0):
    """Tile count for a bbox over min_zoom..max_zoom, without touching the disk."""
    import math
    import mercantile
    total = 0
    for z in range(min_zoom, max_zoom + 1):
        if z <= 8:
            total += sum(1 for _ in mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=z))
        else:
            n = 2 ** z
            x_min = int((minlon + 180) / 360 * n)
            x_max = int((maxlon + 180) / 360 * n)
            lat_hi, lat_lo = min(maxlat, 85.0511), max(minlat, -85)
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


# ---------------------------------------------------------------- the plan
#
# Which DEM cells a region needs, and which zooms it gets, when no world DEM
# is given (--low-zoom-world-vrt): every `streetzim` build, and every fresh
# Zimfarm task.
#
# A terrain tile covers its whole square, not just the region, and low-zoom
# squares are far bigger than a small region: Luxembourg's z7 tile spans
# 2.8 degrees, its z5 tile 11. The old per-region DEM (the bbox plus one
# degree) left the rest of such a tile at 0 m. On screen that is a cliff
# where the DEM stops, visible whenever the view reaches it (3D tilt looks
# far past the region), and a tile that is wrong for any other region that
# reuses it from the cache. So:
#
#  - no tile is made below the zoom the viewer can use. The viewer pins the
#    view to the region's box (maxBounds, no margin), so the lowest zoom it
#    can reach follows from the bbox (viewer_min_map_zoom); terrain starts
#    two levels below that (terrain_min_zoom). Tiles below it (z0 for
#    Monaco is the whole world) are never displayed, and filling them would
#    take a world DEM;
#  - every tile that is made is filled over its whole square: z >= 10 from
#    GLO-30 (30 m), z <= 9 from a 3-arc-second (90 m) mosaic of GLO-30
#    where it was fetched anyway and GLO-90 around it. A 256-px tile at
#    z9 has 10-arc-second pixels, so GLO-90 loses nothing there, and a
#    GLO-90 cell is about an eighth of the size of a GLO-30 one;
#  - GLO-30 is fetched for the cells under the region's z10 tiles only,
#    not bbox + 1 degree: Monaco needs 1 cell instead of 9, Luxembourg 4
#    instead of 16.
#
# The low-zoom mosaic is capped: for a very large area the full square of
# its lowest tiles would be most of a continent. It may span at most
# max(LOWZOOM_MIN_CELLS, LOWZOOM_CELL_BUDGET x the cells under the area's
# z10 tiles) 1-degree cells, counted as the squares cover them, sea cells
# (a cheap 404) and cells shared with GLO-30 included. Past the cap it
# covers the full squares of the lowest zoom that fits (TerrainPlan.
# low_zoom); the zooms below that are filled over that part and are 0 m
# beyond it, far outside the area. The health check holds them to what the
# plan fetched (audit_terrain).

# Zooms at or below this read the 3-arc-second mosaic; above it, GLO-30.
LOWRES_MAX_ZOOM = 9
# Grid of the low-zoom mosaic, in degrees (3 arc-seconds, GLO-90's grid).
LOWRES_RES = 1.0 / 1200.0
# The smallest viewport (CSS px, both sides) the lowest zoom is sized for.
# Phones are at least 320 px wide; a larger viewport only raises the zoom.
VIEWER_MIN_VIEWPORT_PX = 320
# Cells the low-zoom mosaic may span: this many times the cells under the
# region's z10 tiles, but never fewer than LOWZOOM_MIN_CELLS (see above).
LOWZOOM_CELL_BUDGET = 3
LOWZOOM_MIN_CELLS = 64


def _merc_y(lat):
    """Web-Mercator y of a latitude, 0 (north edge) to 1 (south edge)."""
    import math
    lat = max(min(lat, 85.0511287798), -85.0511287798)
    return (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0


def viewer_min_map_zoom(bbox, viewport_px=VIEWER_MIN_VIEWPORT_PX):
    """The lowest MapLibre zoom at which the viewer can show `bbox`.

    The viewer sets maxBounds to the bbox, and MapLibre then zooms in until
    the viewport fits inside it; its world is 512 px wide at zoom 0."""
    import math
    minlon, minlat, maxlon, maxlat = bbox
    fx = (maxlon - minlon) / 360.0
    fy = _merc_y(minlat) - _merc_y(maxlat)
    frac = max(min(fx, fy), 1e-9)
    return math.log2(viewport_px / (512.0 * frac))


def terrain_min_zoom(bbox, max_zoom, low_zoom_world_vrt=None):
    """Lowest terrain zoom to make for `bbox` (0 with a world DEM).

    At map zoom m (MapLibre's 512-px zoom) the hillshade reads the 256-px
    DEM tiles at about m + 1. 3D terrain reads coarser ones: MapLibre's
    terrain uses a deltaZoom of 1, so it asks for tiles one level below the
    tiles it draws, and the far side of a tilted view is drawn from coarser
    tiles again. floor(m) - 2 covers both; measured, a 320x440 view of
    Monaco fully zoomed out and tilted still reads real elevation."""
    import math
    if low_zoom_world_vrt:
        _require_world_dem(low_zoom_world_vrt)
        return 0
    z = math.floor(viewer_min_map_zoom(bbox)) - 2
    return max(0, min(max_zoom, z))


def _require_world_dem(path):
    """A world DEM that was asked for must exist: falling back to the fresh
    layout without a word would change a production build's tiles."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"--low-zoom-world-vrt {path} is not a file")


_MERC_MAX_LAT = 85.0511


def _tiles_footprint(bbox, z):
    """(west, south, east, north) of the union of the zoom-z tiles over
    `bbox`, widened by the rasteriser's 2-px halo and a few source pixels."""
    import mercantile
    minlon, minlat, maxlon, maxlat = bbox
    # Web Mercator ends at +-85.0511: a box reaching a pole would otherwise
    # ask mercantile for a tile at infinity.
    minlat = min(max(minlat, -_MERC_MAX_LAT), _MERC_MAX_LAT)
    maxlat = min(max(maxlat, -_MERC_MAX_LAT), _MERC_MAX_LAT)
    ul = mercantile.tile(minlon, maxlat, z)
    lr = mercantile.tile(min(maxlon, 180.0 - 1e-9), minlat, z)
    west, north = mercantile.bounds(ul).west, mercantile.bounds(ul).north
    east, south = mercantile.bounds(lr).east, mercantile.bounds(lr).south
    pad = 3.0 * (360.0 / (1 << z)) / 256.0 + 0.001
    return (max(west - pad, -180.0), max(south - pad, -_MERC_MAX_LAT),
            min(east + pad, 180.0), min(north + pad, _MERC_MAX_LAT))


def _cells(box):
    """The 1-degree DEM cells (lat, lon of their SW corner) that `box` touches."""
    import math
    west, south, east, north = box
    return [(lat, lon)
            for lat in range(math.floor(south), math.floor(north - 1e-9) + 1)
            for lon in range(math.floor(west), math.floor(east - 1e-9) + 1)
            if -90 <= lat <= 89 and -180 <= lon <= 179]


class TerrainPlan:
    """Which DEM cells a bbox needs and which mosaic each zoom reads.

    `world_vrt` set: the production layout, unchanged (bbox + 1 degree of
    GLO-30 for every zoom from 0, the world DEM for z <= 7). Otherwise the
    layout described above, for a bbox inside [-180, 180]."""

    def __init__(self, bbox, max_zoom, min_zoom=None, low_zoom_world_vrt=None):
        import math
        self.bbox = tuple(float(v) for v in bbox)
        self.max_zoom = max_zoom
        if low_zoom_world_vrt:
            _require_world_dem(low_zoom_world_vrt)
        self.world_vrt = low_zoom_world_vrt or None
        if min_zoom is None:
            min_zoom = terrain_min_zoom(self.bbox, max_zoom, self.world_vrt)
        self.min_zoom = min_zoom
        minlon, minlat, maxlon, maxlat = self.bbox
        if self.world_vrt:
            self.glo30_cells = [
                (lat, lon)
                for lat in range(math.floor(minlat) - 1, math.floor(maxlat) + 2)
                for lon in range(math.floor(minlon) - 1, math.floor(maxlon) + 2)
                # The 1-cell halo can step off the edge of the world: alaska's
                # minlon=-180 asks for dem_N**_W181, which exists nowhere and
                # 404s on every source. GLO-30 cells are named for their SW
                # corner, so the valid range is lat -90..89, lon -180..179.
                if -90 <= lat <= 89 and -180 <= lon <= 179]
            self.low_cells = []
            self.low_zoom = None
            return
        core = _cells(_tiles_footprint(self.bbox, LOWRES_MAX_ZOOM + 1))
        self.glo30_cells = (
            _cells(_tiles_footprint(self.bbox, max(min_zoom, LOWRES_MAX_ZOOM + 1)))
            if max_zoom > LOWRES_MAX_ZOOM else [])
        self.low_cells = []
        # The zoom whose full squares the low-zoom mosaic covers: min_zoom
        # when the budget allows (every tile complete), else the lowest zoom
        # that fits it.
        self.low_zoom = None
        if min_zoom <= LOWRES_MAX_ZOOM:
            budget = max(LOWZOOM_MIN_CELLS, LOWZOOM_CELL_BUDGET * len(core))
            for z in range(min_zoom, LOWRES_MAX_ZOOM + 1):
                cells = _cells(_tiles_footprint(self.bbox, z))
                if len(cells) <= budget or z == LOWRES_MAX_ZOOM:
                    self.low_zoom, self.low_cells = z, cells
                    break

    @property
    def fresh(self):
        return self.world_vrt is None

    @property
    def key(self):
        minlon, minlat, maxlon, maxlat = self.bbox
        return f"{minlon:.1f}_{minlat:.1f}_{maxlon:.1f}_{maxlat:.1f}"

    @property
    def marker_name(self):
        # Fresh layouts get their own marker: a production marker for the
        # same bbox vouches for tiles made the old way.
        suffix = "" if self.world_vrt else f"_from{self.min_zoom}_full"
        return f"COMPLETED_z{self.max_zoom}_{self.key}{suffix}"

    def zooms(self):
        return range(self.min_zoom, self.max_zoom + 1)

    def vrt_for_zoom(self, z, glo30_vrt, low_vrt):
        if self.world_vrt:
            return _terrain_vrt_for_zoom(z, glo30_vrt, self.world_vrt)
        if (z <= LOWRES_MAX_ZOOM and low_vrt) or not glo30_vrt:
            return low_vrt
        return glo30_vrt


def dem_sources_dir():
    """Where DEM cells are cached: <cache>/terrain_cache/dem_sources, shared
    by every region (and by --terrain-dir builds)."""
    d = os.path.join(CACHE_DIR, "terrain_cache", "dem_sources")
    os.makedirs(d, exist_ok=True)
    return d


def _cell_name(lat, lon):
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return ns, abs(lat), ew, abs(lon)


# Seconds a DEM request may wait to connect or between two reads (it was
# 120: a dead route then took 12 minutes per cell before failing).
DEM_HTTP_TIMEOUT_S = 30.0
# Wall-clock budget for all of a fresh build's DEM downloads, in seconds
# (TERRAIN_DOWNLOAD_BUDGET_S overrides it; 0 = none). Switzerland's 815 MB
# is 14 minutes at 1 MB/s. Builds with a world DEM have no budget by
# default: the production host fetches whole continents.
DEM_DOWNLOAD_BUDGET_S = 1800.0


class DemDownloadError(RuntimeError):
    """The DEM could not be fetched; the message says how to build without."""


def _dem_failure(what):
    return DemDownloadError(
        f"Terrain: {what}. Refusing to rasterise the missing DEM as 0 m. Re-run "
        "the build when the Copernicus buckets on S3 are reachable, or build "
        "without terrain: --no-terrain (on Zimfarm, set \"terrain\" to "
        "\"off\").")


class _DemStats:
    """What the DEM step fetched, for the build log (and docs/zimfarm.md)."""

    def __init__(self):
        self.downloaded = {"GLO-30": 0, "GLO-90": 0}
        self.bytes = {"GLO-30": 0, "GLO-90": 0}
        self.cached = 0
        self.sea = 0
        self.deadline: float | None = None   # time.monotonic() past which to give up

    def out_of_time(self):
        return self.deadline is not None and time.monotonic() > self.deadline

    def summary(self):
        mb = sum(self.bytes.values()) / 1e6
        return (f"DEM cells: {self.downloaded['GLO-30']} GLO-30 + "
                f"{self.downloaded['GLO-90']} GLO-90 downloaded ({mb:.1f} MB), "
                f"{self.cached} cached, {self.sea} sea")


def _download_dem(sources, fpath, stats):
    """Fetch the first source that has the cell into `fpath`.

    Returns "ok", "sea" (404 from every source) or "failed" (anything else:
    the caller must not treat the cell as sea)."""
    all_404 = True   # only a 404 from EVERY source means "ocean"
    for try_url, label in sources:
        req = urllib.request.Request(try_url, headers={"User-Agent": "streetzim/1.0"})
        print(f"    Downloading {os.path.basename(fpath)} ({label})...")
        for attempt in range(1, 4):
            if stats.out_of_time():
                return "failed"
            # Download to a temp file and rename only when the body is
            # complete + looks like a TIFF. Writing in place left a
            # truncated .tif (> 1000 bytes passes every size check) that
            # gdalbuildvrt then used.
            tmp_path = fpath + ".part"
            try:
                with urllib.request.urlopen(req, timeout=float(os.environ.get(
                        "TERRAIN_HTTP_TIMEOUT_S", DEM_HTTP_TIMEOUT_S))) as resp:
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
                size = os.path.getsize(fpath)
                kind = "GLO-90" if "GLO-90" in label else "GLO-30"
                stats.downloaded[kind] += 1
                stats.bytes[kind] += size
                print(f"      {size / (1024 * 1024):.1f} MB ({label})")
                return "ok"
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    print(f"      404 on {label}")
                    break
                all_404 = False
                print(f"      Warning: HTTP {e.code} from {label} (attempt {attempt}/3)")
            except Exception as e:
                all_404 = False
                print(f"      Warning: failed to download from {label} (attempt {attempt}/3): {e}")
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
            if attempt < 3:
                time.sleep(2 * attempt)
    return "sea" if all_404 else "failed"


def _resolve_cell(fpath, sources, stats):
    """The usable GeoTIFF for one cell (downloading it if needed), None for
    sea, or "failed" for a transient failure."""
    fname = os.path.basename(fpath)
    # Check for a "no data" marker (empty file left by a previous 404).
    #
    # A marker must NEVER shadow a real DEM. The GLO-90 fallback was added
    # after some of these markers were written, so five cells
    # (Georgia/Armenia/Azerbaijan and the Caspian — dem_N39_E048, N40_E049,
    # N40_E050, N41_E043, N41_E046, carrying terrain up to 4,113 m) ended up
    # with BOTH a usable .tif and a stale marker, and were silently dropped
    # from every VRT covering them. That is why central-asia's 2026-09-11
    # build aborted: 263 Caspian tiles rasterised as 0 m against a DEM that
    # reads -28 m. Clear the marker when the file is usable instead of
    # skipping the cell.
    nodata_marker = fpath + ".nodata"
    if os.path.exists(nodata_marker):
        # Only clear the marker for a DEM that actually reads. Pre-atomic-
        # write builds left truncated .tif files; a truncated GeoTIFF keeps
        # its header, so size and magic both pass — hence the deep read
        # here. If it fails, the cell is skipped with its marker left in
        # place (treated as absent, not re-downloaded); clearing the marker
        # then would admit a corrupt DEM to the VRT.
        if _dem_tif_is_usable(fpath, deep=True):
            print(f"    Clearing stale .nodata marker shadowing {fname}", flush=True)
            try:
                os.unlink(nodata_marker)
            except OSError:
                pass
        else:
            stats.sea += 1
            return None
    if _dem_tif_is_usable(fpath):
        stats.cached += 1
        return fpath
    got = _download_dem(sources, fpath, stats)
    if got == "ok":
        return fpath
    if got == "sea":
        # 404 from every source — genuinely no data (ocean). Persist that so
        # we don't re-ask.
        open(nodata_marker, "w").close()
        stats.sea += 1
        return None
    # Transient failure: do NOT write the marker — it used to brand a land
    # cell as ocean forever, and every later build zero-filled real terrain
    # there.
    return "failed"


def _glo30_sources(lat, lon):
    ns, alat, ew, alon = _cell_name(lat, lon)
    # GLO-30 first, GLO-90 for the regions GLO-30 leaves out (Georgia,
    # Armenia, Azerbaijan etc. 404 on GLO-30).
    return [(COPERNICUS_DEM_URL.format(ns=ns, lat=alat, ew=ew, lon=alon), "GLO-30"),
            (COPERNICUS_DEM_URL_GLO90.format(ns=ns, lat=alat, ew=ew, lon=alon),
             "GLO-90 fallback")]


def _glo30_path(dem_dir, lat, lon):
    ns, alat, ew, alon = _cell_name(lat, lon)
    return os.path.join(dem_dir, f"dem_{ns}{alat:02d}_{ew}{alon:03d}.tif")


def _glo90_path(dem_dir, lat, lon):
    # Its own name: a GLO-90 cell must never pass for the GLO-30 one that a
    # later build of a neighbouring region needs at z12.
    ns, alat, ew, alon = _cell_name(lat, lon)
    return os.path.join(dem_dir, f"dem90_{ns}{alat:02d}_{ew}{alon:03d}.tif")


def fetch_plan_dems(plan, dem_dir, stats=None):
    """Download (or find cached) every DEM cell of `plan`.

    Returns (glo30 paths, low-zoom paths). Raises DemDownloadError at the
    first cell that every source failed for a reason other than a 404
    (rasterising it as 0 m and writing the COMPLETED marker would make the
    "retry" never happen), and when the download budget runs out: a task
    whose route to S3 is broken fails in minutes, not hours."""
    stats = stats or _DemStats()
    budget = float(os.environ.get("TERRAIN_DOWNLOAD_BUDGET_S",
                                  DEM_DOWNLOAD_BUDGET_S if plan.fresh else 0) or 0)
    if budget > 0:
        stats.deadline = time.monotonic() + budget

    def check(got, path):
        if got == "failed":
            raise _dem_failure(
                f"{os.path.basename(path)} could not be downloaded"
                + (f" within the {budget:.0f} s download budget "
                   "(TERRAIN_DOWNLOAD_BUDGET_S)" if stats.out_of_time() else ""))
        if stats.out_of_time():
            raise _dem_failure(f"the DEM download took longer than its {budget:.0f} s "
                               "budget (TERRAIN_DOWNLOAD_BUDGET_S)")
        return got

    glo30 = {}
    for lat, lon in plan.glo30_cells:
        path = _glo30_path(dem_dir, lat, lon)
        got = check(_resolve_cell(path, _glo30_sources(lat, lon), stats), path)
        if got:
            glo30[(lat, lon)] = got
    low = []
    for lat, lon in plan.low_cells:
        if (lat, lon) in glo30:
            low.append(glo30[(lat, lon)])
            continue
        if (lat, lon) in plan.glo30_cells:
            continue                           # sea
        p30 = _glo30_path(dem_dir, lat, lon)
        if _dem_tif_is_usable(p30):            # fetched for another region
            stats.cached += 1
            low.append(p30)
            continue
        if os.path.exists(p30 + ".nodata"):    # 404 on GLO-30 and GLO-90
            stats.sea += 1
            continue
        ns, alat, ew, alon = _cell_name(lat, lon)
        url = COPERNICUS_DEM_URL_GLO90.format(ns=ns, lat=alat, ew=ew, lon=alon)
        p90 = _glo90_path(dem_dir, lat, lon)
        got = check(_resolve_cell(p90, [(url, "GLO-90")], stats), p90)
        if got:
            low.append(got)
    print(f"    {stats.summary()}", flush=True)
    return [glo30[c] for c in plan.glo30_cells if c in glo30], low


def _write_vrt(tif_paths, out_path, res=None, want_bbox=None):
    """A mosaic VRT of `tif_paths` (gdalbuildvrt when installed, else the
    same XML written directly). `res`: the grid, in degrees; None keeps
    gdalbuildvrt's default (and 1 arc-second without it)."""
    import tempfile as _tmpfile
    with _tmpfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as flist:
        flist.write('\n'.join(tif_paths))
        flist_path = flist.name
    cmd = ["gdalbuildvrt", "-overwrite"]
    if res:
        cmd += ["-resolution", "user", "-tr", repr(res), repr(res)]
    try:
        # -input_file_list avoids "Argument list too long" with 24K+ files.
        subprocess.run(cmd + ["-input_file_list", flist_path, out_path],
                       check=True, capture_output=True, text=True)
        return out_path
    except FileNotFoundError:
        # gdalbuildvrt not on PATH — build the same VRT XML ourselves.
        #
        # NEVER fall back to a shared single-file mosaic here. The previous
        # code did, keyed on a fixed `mosaic_4326.tif` with no bbox check,
        # and silently rasterised every region against a Hispaniola-only DEM
        # for six months. A per-bbox VRT is cheap (XML over the tiles
        # already on disk) and cannot be reused for the wrong region.
        print("    gdalbuildvrt not found; building VRT directly")
        kw = {"res": res} if res else {}
        return _build_dem_vrt(tif_paths, out_path, want_bbox=want_bbox, **kw)
    finally:
        # Must be in a finally: gdalbuildvrt is absent on this host, so a
        # raise skipped the unlink and every build leaked a /tmp file.
        os.unlink(flist_path)


def plan_vrts(plan, dem_dir, stats=None):
    """Fetch the plan's DEM cells and write its mosaics.

    Returns (glo30_vrt, low_vrt); either is None when the plan has no such
    cells, or when every one of them is sea."""
    import rasterio  # noqa: F401 -- fail before building the VRT if missing
    glo30_paths, low_paths = fetch_plan_dems(plan, dem_dir, stats)
    glo30_vrt = low_vrt = None
    if glo30_paths:
        # Use a UNIQUE VRT path per bbox to avoid race conditions when two
        # builds run in parallel and overwrite each other's VRT.
        print("    Building VRT from DEM tiles...")
        glo30_vrt = _write_vrt(glo30_paths, os.path.join(dem_dir, f"mosaic_{plan.key}.vrt"),
                               want_bbox=plan.bbox)
    if low_paths:
        print(f"    Building low-zoom VRT (z{plan.min_zoom}-z{LOWRES_MAX_ZOOM}, "
              f"full squares of the z{plan.low_zoom} tiles)...")
        low_vrt = _write_vrt(low_paths, os.path.join(
            dem_dir, f"lowzoom_{plan.key}_z{plan.low_zoom}.vrt"), res=LOWRES_RES)
    return glo30_vrt, low_vrt


def generate_terrain_tiles(bbox_str, dest_dir, max_zoom=12,
                           low_zoom_world_vrt=None, min_zoom=None):
    """Download Copernicus DEM and generate terrain-RGB tiles.

    Downloads 1-degree GeoTIFF tiles from AWS, mosaics them, then generates
    Mapbox terrain-RGB tiles as lossless WebP using rasterio + mercantile.
    Tiles are stored as {dest_dir}/{z}/{x}/{y}.webp, for z = min_zoom ..
    max_zoom (min_zoom None: terrain_min_zoom). See TerrainPlan for which
    DEM each zoom reads.

    ``low_zoom_world_vrt`` (optional): if provided, z=0-7 tiles are
    generated from that DEM instead of the region-bbox mosaic. Prevents
    the bbox-edge stripe bug at low zooms where a tile's footprint
    extends past the region and zero-fills outside. z=8+ still use the
    regional mosaic (fine-grained detail, no stripe risk since each
    tile is small).

    An area across the antimeridian is generated one side at a time.
    """
    from streetzim import area

    if min_zoom is None:
        # From the whole area: the viewer's bounds are the whole area.
        min_zoom = terrain_min_zoom(parse_bbox(bbox_str), max_zoom, low_zoom_world_vrt)
    if area.crosses(parse_bbox(bbox_str)):
        return sum(generate_terrain_tiles(area.to_str(part), dest_dir, max_zoom=max_zoom,
                                          low_zoom_world_vrt=low_zoom_world_vrt,
                                          min_zoom=min_zoom) or 0
                   for part in area.split(parse_bbox(bbox_str)))

    bbox = parse_bbox(bbox_str)
    minlon, minlat, maxlon, maxlat = bbox
    plan = TerrainPlan(bbox, max_zoom, min_zoom, low_zoom_world_vrt)

    os.makedirs(dest_dir, exist_ok=True)
    # Always use the shared DEM sources directory (large raw files, ~547 GB total)
    dem_dir = dem_sources_dir()

    # Check if terrain generation is already complete for THIS SPECIFIC bbox.
    # The marker encodes the bbox so a Europe build can't fool a US build.
    import mercantile
    bbox_key = plan.key
    completed_marker = os.path.join(dest_dir, plan.marker_name)
    if plan.fresh:
        print(f"    Terrain z{plan.min_zoom}-z{max_zoom} (the viewer shows this area "
              f"from map zoom {viewer_min_map_zoom(bbox):.1f}); DEM: "
              f"{len(plan.glo30_cells)} GLO-30 cells, {len(plan.low_cells)} cells "
              f"for z<={LOWRES_MAX_ZOOM}", flush=True)

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
        for z in range(max_zoom, plan.min_zoom - 1, -1):
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
            total = _bbox_tile_total(minlon, minlat, maxlon, maxlat, max_zoom,
                                     min_zoom=plan.min_zoom)
            print(f"    Using ~{total} cached terrain tiles "
                  f"(generation complete for {bbox_key})", flush=True)
            return total
        print(f"    Cached terrain for {bbox_key} is marked complete but {blank} "
              f"— regenerating missing/undersized tiles", flush=True)

    # Fallback: sample z-max tiles at the CORNERS AND CENTER of this bbox
    # to check if they're cached. More robust than just first/last.
    # Production layout only: a fresh layout's tiles differ from the old
    # ones at low zoom, so a cache of old tiles must not be taken for it.
    z_max_tiles = (list(mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=max_zoom))
                   if not plan.fresh else [])
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

    stats = _DemStats()
    glo30_vrt, low_vrt = plan_vrts(plan, dem_dir, stats)
    from streetzim import source_report
    source_report.note("Copernicus DEM", stats.summary().replace("DEM cells: ", ""))
    if not glo30_vrt and not low_vrt:
        # Open sea only (every cell 404s): nothing to draw.
        print("    No DEM tiles downloaded, skipping terrain")
        return 0

    # Generate terrain-RGB tiles using multiprocessing.
    # Each process opens its own handle to the VRT file — GDAL reads only the
    # pixels needed per tile from the underlying GeoTIFFs. No shared state.
    # Uses a streaming generator so workers start immediately without building
    # a multi-million element list in memory (world z12 = 16.7M tiles).
    print(f"    Generating terrain-RGB tiles (z{plan.min_zoom}-{max_zoom})...")
    count = 0
    cached = 0
    import multiprocessing

    num_workers = min(os.cpu_count() or 4, 16)  # cap at 16 to limit I/O contention

    for z in plan.zooms():
        # For z=0-7, prefer the world-coverage VRT if supplied — those
        # tiles span regions past the bbox, so a regional mosaic would
        # zero-fill outside and produce the bbox-edge stripe bug
        # (Iran 33°N, Butte MT, east-Iran 65°E). z=8+ stays on the
        # regional mosaic (small tiles, full DEM resolution, no stripe).
        # Without one, TerrainPlan's low-zoom mosaic covers them.
        vrt_for_z = plan.vrt_for_zoom(z, glo30_vrt, low_vrt)

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
            y_min = int((1 - math.log(math.tan(math.radians(min(maxlat, 85.0511))) + 1/math.cos(math.radians(min(maxlat, 85.0511)))) / math.pi) / 2 * n)
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


# ---------------------------------------------------------------- the audit


def _decode_terrain(path):
    """Elevation (m) of a terrain-RGB tile, as a 256x256 float array."""
    import numpy as np
    from PIL import Image
    im = np.array(Image.open(path).convert("RGB")).astype(np.uint32)
    return -10000.0 + ((im[:, :, 0] << 16) | (im[:, :, 1] << 8) | im[:, :, 2]) * 0.1


# Pixel rows/columns sampled per tile, and what counts as land the tile lost:
# the DEM reads more than LAND_M from 0 (above, or below sea level like the
# Caspian's -28 m) where the tile says exactly 0 m, at MIN_MISSES or more of
# the 25 points. The tile is quantised to 10 m, so anything within 5 m of 0
# legitimately reads 0; 15 m leaves 10 m for resampling, and 3 points keep
# single coastal points out (a tile pixel averages a neighbourhood).
_AUDIT_PIXELS = (25, 76, 128, 179, 230)
_AUDIT_LAND_M = 15.0
_AUDIT_MIN_MISSES = 3


def _tile_points(t):
    """(lon, lat) of the audited pixel centres of tile `t`, row by row."""
    import mercantile
    xb = mercantile.xy_bounds(t)
    pts = []
    for py in _AUDIT_PIXELS:
        for px in _AUDIT_PIXELS:
            x = xb.left + (px + 0.5) / 256.0 * (xb.right - xb.left)
            y = xb.top - (py + 0.5) / 256.0 * (xb.top - xb.bottom)
            pts.append(mercantile.lnglat(x, y))
    return pts


def tile_lost_land(path, t, dem):
    """True if tile `t` reads 0 m where the DEM it was made from has land.

    That is the signature of every terrain bug so far: a DEM cell missing
    from the mosaic (the Hispaniola mosaic, the VRT race), or a low-zoom
    tile whose square reaches past the DEM (the bbox-edge stripe). It
    compares the tile with the same mosaic the generator read, so a tile
    that is legitimately 0 m (sea, or past a capped low-zoom ring) passes,
    and one that is not fails whatever its file size."""
    elev = _decode_terrain(path)
    pts = _tile_points(t)
    samples = dem.sample([(p.lng, p.lat) for p in pts], indexes=1)
    pixels = [(py, px) for py in _AUDIT_PIXELS for px in _AUDIT_PIXELS]
    lost = sum(1 for (py, px), v in zip(pixels, samples)
               if len(v) and abs(float(v[0])) > _AUDIT_LAND_M
               and abs(float(elev[py, px])) < 0.05)
    return lost >= _AUDIT_MIN_MISSES


def _blank_over_land(path, t, dem):
    """The size-based rule of the production audit (create_osm_zim's
    _verify_terrain): a tile under 500 bytes, nearly all 0 m, over a square
    the DEM says is mostly land (6 of 9 points above 5 m)."""
    import mercantile
    if os.path.getsize(path) >= 500:
        return False
    elev = _decode_terrain(path)
    if abs(float(elev[128, 128])) >= 10 or (abs(elev) >= 0.05).mean() > 0.05:
        return False
    b = mercantile.bounds(t)
    pts = [(b.west + (b.east - b.west) * fx, b.south + (b.north - b.south) * fy)
           for fx in (0.25, 0.5, 0.75) for fy in (0.25, 0.5, 0.75)]
    land = sum(1 for v in dem.sample(pts, indexes=1) if len(v) and abs(float(v[0])) > 5)
    return land >= 6


def _cell_resolved(dem_dir, lat, lon, low):
    """The cell was fetched (GLO-30, or GLO-90 when `low`) or is known sea
    (a 404 from every source)."""
    paths = [_glo30_path(dem_dir, lat, lon)] + ([_glo90_path(dem_dir, lat, lon)] if low else [])
    return any(_dem_tif_is_usable(p) or os.path.exists(p + ".nodata") for p in paths)


def missing_dem_cells(plan, dem_dir, t, z, _seen=None):
    """1-degree cells under tile `t` that should be on disk but are not.

    Independent of the plan's cell lists: it asks the disk about every cell
    the tile's square touches. Zooms below a capped low-zoom mosaic
    (plan.low_zoom) only need the part of their square the mosaic covers."""
    import mercantile
    b = mercantile.bounds(t)
    box = (b.west, b.south, b.east, b.north)
    low = z <= LOWRES_MAX_ZOOM and plan.low_zoom is not None
    if low and z < plan.low_zoom:
        f = _tiles_footprint(plan.bbox, plan.low_zoom)
        box = (max(box[0], f[0]), max(box[1], f[1]), min(box[2], f[2]), min(box[3], f[3]))
        if box[0] >= box[2] or box[1] >= box[3]:
            return []
    seen = {} if _seen is None else _seen
    out = []
    for lat, lon in _cells(box):
        key = (lat, lon, low)
        if key not in seen:
            seen[key] = _cell_resolved(dem_dir, lat, lon, low)
        if not seen[key]:
            out.append((lat, lon))
    return out


def audit_terrain(plan, dest_dir):
    """Repair and check every tile of a fresh plan (no world DEM).

    Two checks that do not trust each other: every 1-degree cell under every
    tile's square must be on disk or known sea (missing_dem_cells, asked of
    the disk, not of the plan), and z <= LOWRES_MAX_ZOOM and bbox-edge tiles
    are compared with the low-zoom (resp. GLO-30) mosaic at 25 points, chosen
    here by zoom and not by the generator's vrt_for_zoom: nothing may be 0 m
    where the mosaic has land or sea floor below sea level.
    Missing tiles are made first; a tile still wrong after that fails the
    build (TERRAIN_BLANK_TOLERATE=N, the operator's escape hatch for DEM
    gaps, still applies)."""
    import mercantile
    import rasterio
    minlon, minlat, maxlon, maxlat = plan.bbox
    glo30_vrt, low_vrt = plan_vrts(plan, dem_sources_dir())
    if not glo30_vrt and not low_vrt:
        print("    No DEM for this area (open sea): nothing to audit")
        return
    print(f"    Verifying terrain tiles z{plan.min_zoom}-z{plan.max_zoom} "
          "against the DEM they were made from...")
    repair = []
    for z in plan.zooms():
        vrt = plan.vrt_for_zoom(z, glo30_vrt, low_vrt)
        for t in mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=z):
            fp = os.path.join(dest_dir, str(z), str(t.x), f"{t.y}.webp")
            if not _terrain_tile_usable(fp):
                b = mercantile.bounds(t)
                repair.append((vrt, t.x, t.y, z, dest_dir, b.west, b.south, b.east, b.north))
    if repair:
        print(f"    Making {len(repair)} missing terrain tiles...")
        import multiprocessing as _mp
        with _mp.get_context("spawn").Pool(min(4, os.cpu_count() or 4)) as pool:
            pool.map(_generate_one_terrain_tile, repair)

    broken = []
    checked = 0
    handles = {}
    seen = {}
    dem_dir = dem_sources_dir()
    try:
        for z in plan.zooms():
            vrt = (low_vrt if z <= LOWRES_MAX_ZOOM and low_vrt else glo30_vrt or low_vrt)
            dem = handles.get(vrt) or handles.setdefault(vrt, rasterio.open(vrt))
            for t in mercantile.tiles(minlon, minlat, maxlon, maxlat, zooms=z):
                fp = os.path.join(dest_dir, str(z), str(t.x), f"{t.y}.webp")
                gone = missing_dem_cells(plan, dem_dir, t, z, seen)
                if gone:
                    broken.append((z, t.x, t.y, "DEM cell(s) not fetched: "
                                   + ", ".join(f"{la},{lo}" for la, lo in gone[:3])))
                    continue
                if not os.path.isfile(fp):
                    broken.append((z, t.x, t.y, "missing"))
                    continue
                b = mercantile.bounds(t)
                # Every tile at z <= LOWRES_MAX_ZOOM and every tile on the
                # bbox edge: those are the ones whose square reaches past the
                # region. Interior tiles at higher zoom get the size rule.
                edge = (b.west < minlon or b.east > maxlon
                        or b.south < minlat or b.north > maxlat)
                if z <= LOWRES_MAX_ZOOM or edge:
                    checked += 1
                    if tile_lost_land(fp, t, dem):
                        broken.append((z, t.x, t.y, "0 m over land"))
                        continue
                if _blank_over_land(fp, t, dem):
                    broken.append((z, t.x, t.y, "blank over land"))
    finally:
        for h in handles.values():
            h.close()
    if not broken:
        print(f"    Terrain audit passed — every tile's DEM cells are on disk, and "
              f"{checked} low-zoom/edge tiles match their mosaic at 25 points")
        return
    tolerate = int(os.environ.get("TERRAIN_BLANK_TOLERATE", "0") or 0)
    sample = "\n  ".join(f"z={z} x={x} y={y} ({why})" for z, x, y, why in broken[:5])
    if len(broken) <= tolerate:
        print(f"    [WARN] {len(broken)} terrain tile(s) wrong after repair, within "
              f"TERRAIN_BLANK_TOLERATE={tolerate}. Sample:\n  {sample}\n    Continuing.")
        return
    raise RuntimeError(
        f"Terrain build unhealthy: {len(broken)} tile(s) wrong after the repair "
        f"pass. Sample:\n  {sample}\nEither a DEM cell under them was never "
        "fetched, or the DEM has land where they read 0 m. Delete them and "
        f"rerun, or set TERRAIN_BLANK_TOLERATE=N to accept up to N (currently "
        f"{tolerate}). Aborting.")
