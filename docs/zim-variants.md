# ZIM variants: deriving light builds in minutes, not hours

Status: design note (2026-09-20). Nothing here is implemented yet except
where a file is named.

## What the viewer slot actually bought us, and what it did not

`docs/viewer-slots.md` describes one mechanism: three entries padded into
fixed-size **uncompressed** slots so a same-or-smaller replacement is a seek,
a write and an MD5. It turned a re-pack (8-22 min for a 3-7 GB region, hours
for europe) into 3-10 s.

It generalises along exactly one axis: **replace a known entry with bytes
that fit**. It does not generalise to **removing** content. Dropping
satellite tiles or a zoom level changes the entry count, the URL pointer
list, the cluster table and every offset after the first removed cluster.
There is no slot for that. So "light variants" need a second mechanism, and
the two compose: derive the variant once (minutes), then keep patching its
viewer in place forever (seconds).

There are four cost tiers. Today we have tiers 0, 2 and 3. The gap is tier 1,
and tier 2 exists as three separate tools that each re-implement the same
walk.

| tier | operation | cost | exists today |
|---|---|---|---|
| 0 | in-place slot patch | seconds | `patch_viewer_inplace.py` (viewer only) |
| 1 | cluster-level subset copy, no recompression | I/O bound, ~minutes | **no** |
| 2 | entry-level walk + filter/transform + re-pack | 5-22 min per 2-7 GB; hours for europe | `swap_viewer_rust.py`, `repackage_zim.py`, `upgrade_spatial_zim.py` |
| 3 | full rebuild from planet/mbtiles/search cache | hours | `create_osm_zim.py` |

The important observation is that **tier 2 already delivers "light in
minutes" for every region except the continents**, using only code we have.
Switzerland re-packs in ~5 min. The work is consolidation and a recipe
format, not new infrastructure. Tier 1 is what makes continents cheap and it
needs a small ordering contract in the builder first.

## What is in a ZIM, and how each part can be trimmed

Every component the viewer uses is discovered through `map-config.json`
(`hasSatellite`, `satelliteMaxZoom`, `hasTerrain`, `terrainMaxZoom`,
`maxZoom`, `hasRouting`, `hasWikidata`) or through a per-component
manifest. Nothing is hard-wired to paths in `index.html`. That is what makes
derivation safe: **drop the entries and rewrite one small JSON file** and the
viewer adapts.

| component | paths | drop by prefix? | what else must change |
|---|---|---|---|
| satellite | `satellite/{z}/{x}/{y}.avif` | yes, whole or `z > N` | `hasSatellite`, `satelliteMaxZoom` |
| terrain | `terrain/{z}/{x}/{y}.png` | yes, whole or `z > N` | `hasTerrain`, `terrainMaxZoom`; terrain coverage gate must be skipped |
| vector tiles, deep zooms | `tiles/14/...` | yes, `z > N` | `maxZoom` (viewer falls back to 14 if absent, so it MUST be written) |
| routing | `routing-data/*`, `routing-worker.js` | yes | `hasRouting`; routing gate skipped; the worker slot can stay (68 KB) |
| Wikipedia articles | `wiki-article/*`, `wiki-geo-index.json` | yes | filter `wiki-geo-index.json` (the filter already exists in `swap_viewer_rust.py`); Xapian fulltext keeps dead docs, see below |
| addresses in search | inside `search-data/*.json` leaves, records with `"type":"addr"` | **no** | every leaf re-serialised without addr records; both manifests rewritten; Xapian title/fulltext rebuilt if addresses were indexed |
| Find chips | `category-index/chip-*.json` | per chip | `category-index/manifest.json` |
| fonts, MapLibre, CSS | small | never worth it | |

Two components are always regenerated whatever the tier, and both are cheap:
the title listing (`listing/titleOrdered/*`, an array of entry indexes that
is garbage after any entry removal; zimru rebuilds it on write) and the
16-byte MD5 trailer.

**Where the bytes are is not where intuition says.** Measured on Switzerland
(STATUS-2026-09-18.md): satellite is 6.5% of the file, search-data is 53%
and 58% of that is addresses. The current `switzerland-light` recipe
(no satellite, cap at z13) drops the cheap part and keeps the expensive part.
The first deliverable below is therefore an inventory tool, so recipes are
chosen from numbers rather than guesses. The z14 share is unmeasured; the
1.40 GB figure quoted for the z13 build came from a defective lineage and is
void.

## Tier 2 now: one derive tool with recipes

`swap_viewer_rust.py`, `repackage_zim.py` and `upgrade_spatial_zim.py` all do
the same thing: open the source with `libzim.reader.Archive`, walk
`all_entry_count`, carry Xapian into namespace X raw, keep big routing
entries raw, filter or rewrite a few paths, emit through `ManifestCreator`.
Each grew its own flags (`--reshard-chips`, `--reshard-search`,
`--split-find-chips`, `--spatial-chunk-scale`) and each has a documented
trap when run against the wrong source (gotchas #5). A fourth copy for
"light" would be the wrong move.

Proposal: `cloud/derive_zim.py SRC.zim DST.zim --recipe light` where a
recipe is a small declarative file:

```yaml
# cloud/recipes/light.yaml
name: light
title_suffix: " (Light)"
drop_prefixes: [satellite/]
max_tile_zoom: 13
map_config:
  hasSatellite: false
  satelliteMaxZoom: null
  maxZoom: 13
gates_skip: []
```

```yaml
# cloud/recipes/no-addresses.yaml
name: noaddr
drop_search_record_types: [addr]     # forces a search-data rewrite
gates_skip: [address-search]
```

Operations the walk needs, all already written somewhere in the three tools:

- `drop_prefix` / `max_zoom` for a raster or vector prefix
- `rewrite_json(path, patch)` for `map-config.json` and the two manifests
- `filter_records(prefix, predicate)` for search leaves, streaming one leaf
  at a time (the `av` prefix on united-states is 2.9 GB of JSON; the
  streaming aggregator in `swap_viewer_rust.py --reshard-search` already
  handles this)
- the existing viewer swap into slots, so a derived ZIM is always born with
  the current viewer and patchable slots
- metadata: `Name`, `Title`, `Description` from the recipe; a **fresh UUID**
  (a variant is a different book to Kiwix, so this is the right default,
  unlike a viewer update to a shipped file)

Cost is the tier 2 re-pack: ~5 min for Switzerland, ~20 min for mexico,
hours for europe. Fine for everything but continents; and recipes that only
drop content re-pack fewer bytes than the source, so they are faster than the
viewer swap numbers above.

## Tier 1: cluster-level copy, and the ordering contract it needs

A ZIM is a header, a MIME list, a URL-sorted dirent table, a title index, a
cluster pointer table, the clusters, and an MD5. A dirent points at
(cluster number, blob number). **A cluster that contains only kept entries
can be copied byte for byte**: no decompression, no recompression, no
gotcha #6 window-log concerns because the bytes are unchanged. The derive
tool then only re-encodes clusters that straddle a keep/drop boundary or
whose entries are being transformed (search leaves under `noaddr`). The
rest is `sendfile`. For europe that is the difference between hours and
however long it takes to stream 71 GB off the disk.

For this to work, droppable components must not share clusters with kept
ones, which is an **ordering contract** on the builder:

1. **Component-major emit order**, already true in practice: viewer,
   libs, config, tiles, satellite, terrain, fonts, wikidata, routing,
   search, Xapian (`create_osm_zim.py` around lines 5072-6690).
2. **Zoom-major within a raster or vector prefix.** True for the streaming
   path (`ORDER BY rowid`, zoom-major from tilemaker) but **not** for regions
   whose mbtiles is under 5 GB: `extract_tiles_from_mbtiles()` uses
   `ORDER BY tile_column, tile_row` (line 1675), so z14 tiles are interleaved
   with every other zoom in every cluster. Change to
   `ORDER BY zoom_level, tile_column, tile_row`. Same content, same
   `tiles/{z}/{x}/{y}` URLs, so no reader-visible change; compressed size
   may move by a fraction of a percent either way.
3. **A cluster break at each component and zoom boundary.** Cheapest
   implementation: a `{"kind":"cluster_break"}` manifest record that makes
   `streetzim-pack` close the current cluster (a few lines in
   `rust/streetzim-pack/src/main.rs` plus a `flush()` on zimru's `Creator`
   if it lacks one). Cost is one under-filled 8 MiB cluster per boundary,
   roughly 25 boundaries per ZIM, so well under 200 MB of slack in the worst
   case and typically far less since the last cluster of a run is partial
   anyway. The alternative, `cluster_strategy: by_first_path_segment`, gets
   component grouping for free but has to be re-measured against the 15 s
   typeahead smoke that killed `by_mime` (gotcha #7) and gives no zoom
   grouping.

With the contract in place, tier 1 needs a raw-format subset writer:
read dirents and cluster offsets (the in-place patcher and
`verify_slot_integrity.py` already work at this level in Python), decide
keep/drop/re-encode per cluster, write a new dirent table and cluster table,
copy kept clusters, regenerate the title listing, write the MD5. This is
either a `subset` subcommand in zimru (preferred, since it owns the writer
and the title-listing code) or ~400 lines of Python against the format spec.
Entries dropped from a *kept* boundary cluster can simply be left as
unreferenced blobs; libzim and zimcheck do not walk blobs, only dirents.

Until the contract lands, every shipped ZIM is "pre-contract" in the same
way ~40 regions were "pre-slot": the first derive is a tier 2 re-pack, and
from then on the full build carries the layout and every variant is tier 1.
The gate in `.allzims-v2.sh` that refuses a build without slot markers
should grow a sibling that refuses a build whose clusters mix components.

## Extending slots: config and provenance

Two more entries deserve slots, both tiny:

- **`map-config.json` (4 KB slot).** Lets a viewer change that needs a new
  config key ship in the same in-place patch. Also lets a variant be flipped
  without a re-pack (`hasSatellite: false` with the satellite bytes still in
  the file), which is useless for size but useful for A/B testing a viewer
  behaviour on a device against the same file. JSON has no comment syntax, so
  the padding goes in a trailing `"_slot": "SZVSLOT1:..."` key rather than a
  comment; `viewer_slots.py` already parameterises the delimiters per type.
- **`build-info.json` (4 KB slot), new.** Provenance the tooling can read
  back out of the file without guessing from markers: source ZIM UUID and
  filename, recipe name and hash, viewer build stamp, build date. Today
  `.allzims-v2.sh` decides "already has the newest fix" by grepping for a
  fix-specific marker string in `index.html`; a stamp it can compare is the
  general form of that. Kiwix-visible metadata (`M/` namespace) lives in a
  compressed cluster and cannot be patched in place, which is exactly why
  this belongs in a slot.

`cloud/viewer_slots.py` becomes a registry of slotted paths with sizes and
delimiter styles, and the patcher takes `--set path=file` for any of them.
The known gap about the patch being destructive and non-atomic applies more
as more things become patchable; patching a copy (or a reflink where the
filesystem allows) and renaming over the original is a one-afternoon fix
and should land with the registry.

## The pipeline this gives us

```
full rebuild (hours, only when inputs change)
   └─ derive variants: light, noaddr, no-routing ... (tier 1: minutes; tier 2 until the contract ships)
        └─ patch viewer / config / build-info in place (seconds, forever after)
             └─ gate (recipe-aware) → upload (the remaining cost)
```

Variants are always derived from the **full** build, never from another
variant, so there is one canonical source per region and every variant is
a pure function of (source UUID, recipe, viewer). That is also what makes
the catalog row for a variant derivable: `web/generate.py` and
`cloud/regions.tsv` currently carry hand-copied duplicates of the parent's
bbox, smoke coordinates and search term for `switzerland-light`; a recipe
can generate the row.

Gates need to know the recipe. `ship-switzerland-light.sh` runs the terrain
coverage and routing gates unconditionally; a `no-terrain` recipe would fail
its own gate. Recipes carry `gates_skip` for that reason.

## Xapian and dropped content

Xapian indexes store the entry **path** per document, not the dirent index,
so dropping tiles, satellite, terrain or routing leaves the indexes correct.
Dropping `wiki-article/*` or address records leaves documents whose target
no longer exists, so Kiwix's own search can offer a dead result. Two
options: rebuild the glass DBs with `xapianbuilder` from what is left (3 s
for California's 250k docs, so cheap, but it needs the search features,
which a tier 2 walk can stream from `search-data/`), or accept the dead
results as the existing "Xapian goes stale" gap already does for the app
shell. Recipes that touch indexed content should rebuild; recipes that only
drop tiles need not.

## Order of work

1. **`cloud/zim_inventory.py`** (half a day). One pass over dirents and
   cluster offsets, reporting compressed bytes per component and per zoom,
   plus how many clusters are mixed. Run it on switzerland, japan and europe
   before choosing any recipe. Also proves the ordering-contract violation
   on a sub-5 GB region.
2. **`cloud/derive_zim.py` + `cloud/recipes/`** (one to two days). Tier 2,
   consolidating the three walk tools behind recipes. Delivers light
   variants in minutes for every non-continent region immediately. Tests
   follow `tests/test_upgrade_spatial_zim.py`: build a small fixture, derive,
   assert entry set, `map-config.json`, fresh UUID, `Archive.check()`.
3. **Ordering contract** in the builder (zoom-major SQL, `cluster_break`
   records) and the matching gate (one day, small zimru change).
4. **Tier 1 cluster-copy path** in `derive_zim.py` (three to five days,
   mostly the raw writer). Measure on a continent; expect I/O bound.
5. **Slot registry**: `map-config.json` and `build-info.json` slots, patcher
   `--set`, non-destructive patch via copy-and-rename (half a day).
6. Recipe-aware gates and recipe-generated catalog rows (half a day).

Steps 1, 2 and 5 are independent of each other and of zimru. Step 4 depends
on 3.
