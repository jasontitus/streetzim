# Polar maps

Web Mercator ends at 85° S, so a Mercator map of Antarctica has no South
Pole, no Amundsen–Scott station and a continent stretched beyond
recognition. The map `antarctica-polar` ("Antarctica with the South Pole")
draws all of it in Antarctic polar stereographic (EPSG:3031).

## How it works

Neither MapLibre nor the ZIM's tile, search and routing formats know any
projection but Web Mercator. So the *data* is moved instead: every position
is projected to EPSG:3031 metres, and those metres are read as Web Mercator
metres. The result is an ordinary Mercator map of a place that does not
exist: the pole is at "map" position (0, 0), 0° E points up, 90° E points
right, and the 60° S circle lies about 30° out.

- `streetzim/polar.py` is the projection (`to_map`, `to_true`), tested
  against pyproj's EPSG:3031.
- `create_osm_zim.py --projection antarctic-polar` writes
  `"projection": "antarctic-polar"` into `map-config.json`, masks everything
  north of 60° S, and measures routing edges between the real places their
  points stand for.
- The viewer (`resources/viewer/src/index/122-polar.js`) keeps everything
  in map positions and converts only what crosses to the real world: a GPS
  fix and its heading, coordinates shown or shared, distances, the scale
  bar. It draws meridians and parallels with their names, because north is
  not at the top.

## Building it

The registry row of `antarctica-polar` (`cloud/regions.tsv`) holds a box
and smoke points in **map positions**. A build by that box would cut a piece
of the Gulf of Guinea from the planet, so the queue refuses the id (its row
in `cloud/region-build-flags.tsv` carries `--projection`), and the map has a
script of its own:

    osmium extract --bbox -180,-90,180,-60 --strategy smart planet.osm.pbf -o real.osm.pbf
    ops/build-antarctica-polar.py --extract real.osm.pbf --work DIR --output osm-antarctica-polar-DATE.zim
    ops/cloud/parallel_rebuild.sh --regate antarctica-polar osm-antarctica-polar-DATE.zim

The script moves the extract (`tools/polar_data.py move`), derives the sea
from the extract's own coastline (`ocean`: the world water polygons the tile
profile reads are in real positions), runs tilemaker from the same image as
any other map with a layer file that reads no real-world file
(`tile-config`), makes the terrain (`terrain`) and the imagery
(`satellite`), and builds the ZIM. `--regate` runs the usual gates and
uploads.

## Terrain

The DEM is in real positions, so a terrain tile cannot be cut from it the
way any other map's is. `streetzim/polar_terrain.py` goes the other way:
for every pixel of a map tile it finds the real place the pixel stands for
and reads the elevation there, between the four nearest samples of the
Copernicus cells the terrain cache holds (`terrain_cache/dem_sources`; they
reach the pole). Zoom 12, about 38 m a pixel, is made from the DEM; each
zoom below it is the mean of the four tiles under it. A tile with no land
is not written, at any zoom, and `create_osm_zim.py --projection --terrain
--terrain-dir DIR` takes the folder as it is.

The first run reads some 7,000 cells and takes a few hours; a stopped run
goes on where it was, and a rerun of the script reuses the tiles while the
cells are unchanged. The validator knows such a map by its `projection`:
it asks the DEM about a tile's empty side at the real place, and does not
expect tiles over the sea or take a small tile of flat ice for a blank.

## Imagery

The Sentinel-2 mosaics other maps use are Web Mercator tiles: they end at
85° S, and over the ice sheet they are a plain white fill. This map has
NASA's Blue Marble instead, which NASA's Global Imagery Browse Services
(GIBS) serve in EPSG:3031, the positions the map draws in. So nothing is
reprojected: `streetzim/polar_satellite.py` fetches the picture once (1,024
tiles, 512 m a pixel, kept beside the other imagery caches), and every map
tile is a piece of it, cut out and scaled. Zoom 9 keeps all the picture
has; the imagery layer stops there while the map zooms on. Every tile of the
map's box is written, the sea too, and `create_osm_zim.py --projection
--satellite --satellite-zoom 9 --satellite-dir DIR` takes the folder as it
is.

The source is `satellite_sources.POLAR`. It is not a choice of
`--satellite-source` (no Web Mercator map can be built from it); a polar
map has it whatever source is named. NASA does not copyright the picture and
asks for a credit: the map shows "Blue Marble, NASA (GIBS)", and the About
panel and the ZIM's License metadata carry the acknowledgement GIBS asks
for.

A new viewer reaches this map the same way: the viewer rollout
(`ops/langpack/fresh_all.py`) skips it, because the rollout's gates open a
Wikipedia article, and a rerun of the script reuses the moved extract, the
sea, the tiles, the terrain and the imagery while what they were made from
is unchanged.

## What is not there yet

Wikipedia articles: their coordinates are real positions and nothing moves
them yet. Administrative areas are left out for the same reason. A reader
far from Antarctica who presses the locate button is told the position is
not on this map.

Between 60° S and 71° S the route search's estimate of the distance left
can be up to 4% too long, so a route there may be slightly longer than the
shortest one. The distances shown are real ones everywhere.
