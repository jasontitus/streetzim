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
(`tile-config`), and builds the ZIM. `--regate` runs the usual gates and
uploads.

## What is not there yet

Satellite imagery, terrain and Wikipedia articles: their sources are in
real positions and nothing moves them yet. Administrative areas are left
out for the same reason. A reader far from Antarctica who presses the
locate button is told the position is not on this map.

Between 60° S and 71° S the route search's estimate of the distance left
can be up to 4% too long, so a route there may be slightly longer than the
shortest one. The distances shown are real ones everywhere.
