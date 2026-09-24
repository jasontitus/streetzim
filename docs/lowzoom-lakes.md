# Low-zoom lakes: why Baikal was not blue

## The symptom

Zoomed out past z6, every inland lake rendered as land. Lake Baikal in the
russia ZIM, Superior in canada/us, Victoria in east-africa, Ladoga in
baltics — all of them. Oceans and seas were unaffected, which is why it went
unreported for six months: it only shows on inland water, and only when
zoomed out.

## The cause

`resources/tilemaker/config-openmaptiles.json`:

```json
"water":  { "minzoom": 6, "maxzoom": 14, ... },
"ocean":  { "minzoom": 0, "maxzoom": 14, "source": "coastline/water_polygons.shp",
            "write_to": "water", ... },
```

Lakes come from the OSM `water` layer, which starts at **z6**. Below that the
only thing written into the `water` layer is the ocean coastline shapefile.
So z0–z5 tiles contain sea but no lakes, and the viewer's `water` fill layer
has nothing to draw.

This was **not a regression**. The line is unchanged since the project's
first commit (`ea5bdc4`, 2026-03-10); the only edit it ever received was the
tilemaker v3 API update (`2c68ac0`), which did not touch the zoom. Confirmed
in the data across four build vintages of the same region — Lake Peipus in
baltics 2026-04-22, 05-03, 09-07 and 09-20 all show `ocean` only at z3–z5 and
`lake` from z6.

It is inherited from tilemaker's stock OpenMapTiles config. Upstream
OpenMapTiles basemaps do not look broken at low zoom because the full
pipeline separately imports **Natural Earth** lakes for z0–5. Tilemaker's
bundled config brings in only the coastline shapefile, so the lakes are
simply absent.

## The fix

Fill exactly that gap, from the same source upstream uses, in the viewer
rather than the tiles: regenerating the 113 GB world MBTiles is not possible
on the build host (no tilemaker) and would require rebuilding every region.

`resources/viewer/index.html` carries `_SZ_LAKES`: Natural Earth 1:50m lakes
(public domain), 409 polygons, simplified to 0.01° (~1 km, under one pixel at
z5) and polyline-encoded. 36 KB — the viewer goes 463 → 499 KB against its
1 MB slot. A GeoJSON source `lowzoom-lakes` feeds one fill layer
`water-lowzoom` with `maxzoom: 6`, so it stops exactly where the tiles start.

Each lake fades in at its Natural Earth `min_zoom` via a `step` on zoom, so a
z2 view is not speckled with Finland's small lakes. The layer is listed in
`hiddenInSatellite` alongside `water`, so satellite mode is unchanged.

### Verification

Blue coverage over Baikal on the live russia ZIM, same viewport:

| zoom | before | after |
|---|---|---|
| z3 | 0.4% | 0.9% |
| z4 | 0.6% | 2.3% |
| z5 | 0.5% | 7.2% |
| z5.9 | 0.3% | 15.0% |
| **z6** | **16.4%** | **16.4%** |

z6 identical is the important row: the tile water takes over with no
double-draw and no seam.

The encoded data round-trips — all 409 rings closed, all coordinates in
range, no entry with `min_zoom >= 6` (which would never draw), and Baikal's
bbox matches the source to 0.01° (124 points vs 180 before simplification).

## Regenerating

```sh
curl -fL -o cache/natural-earth/ne_50m_lakes.geojson \
  https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_lakes.geojson
python3 tools/natural_earth/build_lowzoom_lakes.py \
  cache/natural-earth/ne_50m_lakes.geojson cache/natural-earth/lowzoom-lakes.json
```

Then paste the JSON as `_SZ_LAKES` in `resources/viewer/index.html` and copy
the file to `web/drive/viewer/index.html` (the two must stay byte-identical).
`cache/` is gitignored; the source GeoJSON is not committed.

## If you ever can rebuild tiles

The better fix is upstream: add a Natural Earth lakes source to
`config-openmaptiles.json` for z0–5, writing to the `water` layer, and drop
`_SZ_LAKES` from the viewer. That needs tilemaker on the build host and a
full world re-tile, which is why it was not the fix taken here.
