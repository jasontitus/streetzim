# TODO: open on the whole region, not the middle of its bbox

> **Status 2026-09-30.** New builds: done. `create_osm_zim.py` opens on the
> region's anchor city from `cloud/regions.tsv` (`registry_anchor`, `423f65f`),
> else the median place position (`center_from_places`, `e789a52`). The zoom
> was never fixed at 6: `get_center_and_zoom` buckets it by extent (4 for the
> US, 11 for washington-dc). Viewer side (fit the whole region, city rescue):
> tried on 2026-09-25 and reverted, reasons in the comment in
> `resources/viewer/src/index/120-map-init-and-style.js`; still open.

Reported 2026-09-25: *"I sometimes see zims open to random fairly empty
areas. Ideally it shows the whole region but if it can't — then center on an
anchor city."*

Confirmed, and it is the same root cause as the render-gate bug fixed in
`5e3fe72`.

## What happened on 2026-09-25

The diagnosis at the time (before the fixes in the status note above,
which also corrects the zoom: it was always extent-based) was that
`create_osm_zim.py` wrote `map-config.json` with the bbox **centre** and a
**fixed zoom 6**, for every region regardless of size:

| region | opening centre | what is there |
|---|---|---|
| north-africa | 4.5, 28.25 | open Sahara |
| hawaii | -166.5, 23.5 | open Pacific, west of the islands |
| nordics | 18.05, 62.85 | Gulf of Bothnia |

The viewer applies it directly (`resources/viewer/src/index/120-map-init-and-style.js`,
built into `index.html`: the `new maplibregl.Map({ center: openCam.center,
zoom: openCam.zoom, ... })` call, where `_szOpeningCamera` gives
`config.center`/`config.zoom` unless the reader's last view is restored).
`config.bounds` is already written and used for `maxBounds`, so the geometry
needed to do better is present in every ZIM already shipped.

Zoom 6 is also wrong in both directions: too far in for a continent (you see
a fraction of it), too far out for washington-dc or silicon-valley.

## What it should do

1. **Fit the whole region** — `fitBounds(config.bounds)` on load. The user
   sees what they downloaded, at whatever zoom that implies.
2. **Fall back to the anchor city** when fitting is not useful — a region
   whose bbox is mostly empty (east-polynesia spans 3,000 km of ocean for a
   few atolls), or where the fitted view renders almost nothing. The anchor
   is `cloud/regions.tsv` column 5, the same field the render gate now
   probes, so the two stay consistent by construction.

A reasonable rule for "not useful": fit the bounds, then count rendered
features the way `tmp/map-health.mjs` does; under the threshold, jump to the
anchor at z11. That reuses the check we already trust.

## Two places, and they must agree

- **New builds**: write a better `center`/`zoom` (or an explicit
  `initialView`) in `_build_map_config` in `create_osm_zim.py`. (Done for
  the centre: see the status note.)
- **Every shipped ZIM**: the opening view is read from `map-config.json`,
  which is **not** in a viewer slot — so a viewer-only patch cannot change
  it unless the viewer ignores `config.center`/`config.zoom` and derives the
  opening view from `config.bounds` itself. Doing it viewer-side means the
  fix rides the ordinary viewer rollout and reaches all ~65 regions without
  a re-pack. That is the cheaper path and should be preferred.

## Why it was not done on 2026-09-25

The 490 GB viewer rollout was mid-flight. Changing the viewer while it runs
would ship two different opening behaviours across the set — the regions
patched before the change and those after — for an untested change. Do it
as its own rollout, or fold it into the next one.
