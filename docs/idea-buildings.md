# Idea: more buildings, and real heights

> **Status 2026-11-16: an area of potential, not started.** Notes only; no
> code, no data source added.

## Where the maps are today

Every building comes from OpenStreetMap. The tile profile
(`resources/tilemaker/process-openmaptiles.lua`, `SetBuildingHeightAttributes`)
gives a building its OSM `height`, else `building:levels` × 3.66 m, else one
storey (3.66 m). Most buildings carry neither tag, so in 3D most towns are
flat one-storey blocks. Where OSM has few buildings mapped (much of Africa,
South Asia, Southeast Asia) the map shows few buildings at all.

## Looked at: GlobalBuildingAtlas

<https://github.com/zhu-xlab/GlobalBuildingAtlas> (ESSD 2025): global
footprints, a height for every building, LoD1 block models. Machine-learned
("errors may occur"); tiles on Hugging Face and mediaTUM.

| Part | Licence | For us |
|---|---|---|
| `GBA.ODbLPolygon`: footprints from OSM + Microsoft Global ML Building Footprints | ODbL | **Duplicative**: the same sources Overture's buildings theme already merges, with Overture's own conflation. |
| `GBA.Height`, `GBA.Polygon`, `GBA.LoD1`: heights and 3D models | **CC BY-NC 4.0** | The only new part, and the one that would look best. Not usable as is: non-commercial data would make every map non-commercial (no redistribution on sold devices or bundles), unlike the rest of the map. Their code is MIT + Commons Clause (no commercial use). |

Verdict: do not use; revisit only if its heights are ever released under an
open licence.

## Better routes, when this is picked up

1. **Overture buildings** (`buildings` theme). ODbL-compatible, merges OSM,
   Microsoft and Google Open Buildings footprints and others, some with
   heights. It fills the regions where OSM is thin, and the build already
   downloads Overture (places, addresses: `download_overture_data.py`,
   `streetzim/overture.py`). To try: one map where OSM is thin (Lagos or
   Dhaka); compare building counts, tile size, the 3D view, and the source
   credits the About panel needs (Google's footprints carry their own
   attribution terms).
2. **Better guesses for buildings without a height.** Use the building type
   instead of one storey for all: `house`/`detached` 1–2 storeys,
   `apartments` and `residential` in a city several, `office`/`commercial`
   more, `garage`/`shed` low. A tile-profile change only: no new data, no
   licence question, every map gains a more believable skyline. Mark guessed
   heights so the viewer could draw them more softly than measured ones.
