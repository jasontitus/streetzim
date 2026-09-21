# Trails: what we ship today, and a plan

Status 2026-09-21. Pilot regions: **switzerland** and **california**.

## What is actually in the ZIMs today

Measured, not assumed — one z14 tile over Zermatt
(`tiles/14/8544/5827.pbf`, `osm-switzerland-2026-09-20c.zim`):

```
transportation classes: path=533  track=15  rail=73  aerialway=4  service=6  minor=3
```

The trail data is **already there and dense**. Attributes riding along on those
533 path/track features:

| attribute | present on |
|---|---|
| `subclass` (footway / path / track) | 533 |
| `surface` (paved, gravel, dirt…) | 206 |
| `bicycle` | 50 |
| `foot` | 36 |
| `access` | 19 |
| `horse` | 4 |

A `transportation_name` layer exists in the same tiles.

So the gap is **not** data acquisition. It is that we barely draw it, cannot
route on it, and cannot search it.

## The four layers, honestly assessed

| layer | state | verdict |
|---|---|---|
| **Vector tiles** | `path` + `track` present with `subclass`/`surface` | ✅ already good |
| **Style** | one rule: `class == path`, `minzoom 14`, 1 px dashed `#cba090`. `track` never drawn. `subclass`/`surface` never consulted | ❌ the visible gap |
| **Routing graph** | `path`/`footway`/`steps`/`cycleway`/`bridleway`/`track` kept, with speeds (path 5 km/h, track 15) and class ordinals, flagged bit-9 "no motor vehicle" | ✅ data present, unusable |
| **Routing UI** | car profile only; every consumer skips bit-9 edges (`routing-worker.js`, `index.html`, `tests/szrg_astar.py`, `cloud/route_cli.py`) | ❌ no foot/hike mode |
| **Search** | named trails effectively absent — 400k switzerland rows yielded 5 trail-ish hits, all coincidental place names | ❌ |
| **Route relations** | `create_osm_zim.py` has only `_Pass1`/`_Pass2` node+way handlers, **no relation handler**. PCT / AT / CDT / GR routes do not exist as entities | ❌ |
| **Trail metadata** | `sac_scale`, `mtb:scale`, `trail_visibility` never read from the PBF (`surface` reaches the tiles but nothing else) | ❌ |

## Plan, cheapest first

### Phase 1 — draw what we already have (viewer only, ~10 s/region)
No rebuild. Ships through the slot patch (`cloud/patch_viewer_inplace.py`).

- style `class=track` (currently invisible)
- lower `road-path` minzoom 14 → 12; trails are the *reason* to zoom out in
  back-country
- differentiate by `subclass`: footway vs path vs track
- use `surface` (present on 40 % of features) for dash pattern — paved solid,
  gravel/dirt dashed
- render `transportation_name` labels along trails

**Verification:** the device matrix already fails on overlap and unreachable
controls; add a check that a known alpine tile renders ≥ N path features.

### Phase 2 — foot/hiking routing (viewer + worker, no rebuild)
The graph already holds the edges and speeds. Today bit-9 is a hard exclude;
a foot profile inverts it — use `path`/`footway`/`steps`/`track`, exclude
`motorway`/`trunk`. Needs a mode toggle in the UI and matching changes in
`routing-worker.js`, `cloud/route_cli.py`, `tests/szrg_astar.py` so the CLI,
tests and viewer agree.

**Risk:** the car profile's speed table gives `steps` 3 km/h; a naive foot
profile will happily route up a 400-step staircase. Needs a foot-specific
cost function, not just an inverted filter.

### Phase 3 — trails in search (rebuild per region)
Named ways are in the PBF and never emitted into `search.jsonl`. This is an
extractor change plus a rebuild — no new data source.

### Phase 4 — route relations (rebuild, real work)
Add an `osmium` relation handler for `route=hiking` / `route=bicycle`, emit
each named long-distance route as a searchable entity with member geometry.
This is what makes "Pacific Crest Trail" or "Haute Route" a thing you can
find rather than a series of anonymous segments.

### Phase 5 — difficulty metadata (rebuild)
Carry `sac_scale` / `mtb:scale` / `trail_visibility` into the tiles or a
sidecar so the viewer can colour by difficulty and warn on T4+.

## Pilot: switzerland + california

Chosen deliberately:

- **switzerland** — densest signed trail network in the world (Wanderwege),
  2.2 GB so the whole loop is fast, and it already has slots so Phase 1 costs
  ~10 s. Alpine `sac_scale` coverage is the best anywhere, which makes it the
  right place to prove Phase 5.
- **california** — PCT, JMT, and a mix of desert/alpine/coastal surface types;
  3.4 GB; exercises US tagging conventions and USFS/NPS-adjacent terrain that
  Switzerland does not.

Run Phase 1 on both, compare against the current build side by side, then
decide whether Phase 2 is worth the profile work before touching rebuilds.

## On the government datasets (USGS / NPS / USFS / BLM)

Deferred, deliberately. They add authoritative US coverage, but each is a
second conflated source with its own licensing, projection and dedupe
problems against OSM geometry, and they help only the US. OSM alone carries
Phases 1-5 globally. Revisit once Phase 1-3 ship and we can measure what is
actually missing rather than guessing.

## What this does not need

No new downloads, no Overpass, no Geofabrik, no tile regeneration for
Phases 1-2. The planet PBF we already parse and the tiles we already ship
contain everything those two phases need.
