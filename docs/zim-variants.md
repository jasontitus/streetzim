# ZIM variants: deriving light builds in minutes, not hours

Status: design note (2026-09-20), with a first implementation of tiers 0-1
measured on real files. See **What exists now** at the end for the tools,
the numbers and what is still open.

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

## What exists now (2026-09-20, measured)

Files: `cloud/zimfmt.py` (raw format), `cloud/zim_inventory.py`,
`cloud/derive_zim.py` (the `streetzim-derive` CLI), `cloud/verify_derived.py`,
`cloud/zim_access_sim.py`, `tests/test_derive_zim.py`. None of them need
zimru or the libzim Creator; the verifier and the tests use python-libzim as
the independent reader.

### Inventory: where the bytes are

`zim_inventory.py` reads every dirent and inflates every cluster once. 2 s on
washington-dc, 10 s on switzerland.

switzerland 2026-09-20d, 2.20 GB, 159,936 entries, 903 clusters, 19 mixed:

| component | entries | on disk | share |
|---|---|---|---|
| tiles/14 | 44,520 | 489 MB | 22.2% |
| search-data | 13,047 | 480 MB | 21.8% |
| routing-data | 1,620 | 408 MB | 18.5% |
| tiles/13 | 11,305 | 203 MB | 9.2% |
| wiki articles + images | 20,963 | 124 MB | 5.6% |
| wikidata | 91 | 109 MB | 5.0% |
| satellite (all zooms) | 59,730 | 123 MB | 5.6% |
| xapian | 2 | 70 MB | 3.2% |
| terrain (all zooms) | 3,904 | 65 MB | 3.0% |
| tiles z0-12 | 3,903 | 95 MB | 4.3% |

Two things the table settles. First, the shipped "light" recipe (no
satellite, z13 cap) removes 28% and z14 is four fifths of that; satellite is
a rounding error next to search-data and routing. Second, the source is
**already component-major and zoom-major**: only 19 of 903 clusters mix
components, all at run boundaries. The ordering-contract concern above holds
for the sub-5 GB SQL path in principle, but this build did not exhibit it, so
a cluster-copy derive re-encodes a handful of boundary clusters, not the file.

washington-dc for contrast is 48% wikidata and 28% wiki, with tiles at 7.7%.
A recipe that helps one region can be irrelevant to another; run the
inventory first.

### Derive: tier 1 on real files

```
python3 cloud/derive_zim.py SRC.zim DST.zim --light            # = --no-satellite --max-tile-zoom 13
python3 cloud/derive_zim.py SRC.zim DST.zim --no-terrain --no-routing --no-wiki
python3 cloud/derive_zim.py SRC.zim DST.zim --satellite-max-zoom 12 --drop-prefix wiki-image/
python3 cloud/derive_zim.py SRC.zim DST.zim --light --title "... (Light)" --name osm_x_light
python3 cloud/derive_zim.py SRC.zim --light --dry-run          # cluster plan only
python3 cloud/derive_zim.py SRC.zim DST.zim --regroup-tiles --tile-order hilbert --cluster-target 8388608
python3 cloud/verify_derived.py SRC.zim DST.zim --expect-dropped satellite/ tiles/14/
```

| run | source | plan | time | output |
|---|---|---|---|---|
| washington-dc `--light` | 226 MB | copy 313 / re-encode 5 / drop 13 clusters | 4.6 s | 213 MB |
| switzerland `--light` | 2.20 GB | copy 769 / re-encode 7 / drop 127 | **31 s** | 1.578 GB |
| switzerland `--regroup-tiles hilbert` | 2.20 GB | copy 708 / re-encode 194 (1.6 GB of tiles) | 185 s, 4 cores | 2.214 GB (+0.6%) |

The switzerland light derive produces a file the same size, to the megabyte,
as the shipped `osm-switzerland-light-2026-09-20.zim` (1.578 GB), which took
a rebuild plus a viewer re-pack. 31 s against ~5 min for the re-pack path and
hours for the rebuild. The work is `sendfile`-shaped: 769 clusters copied
through the source mmap, 7 clusters inflated and re-deflated (map-config,
metadata, the two boundary clusters, the raw satellite/xapian cluster), one
title listing regenerated, one MD5.

What `verify_derived.py` proved for each output, with python-libzim:
`Archive.check()` true, MD5 trailer valid, main page resolves, fulltext and
title indexes present and answering queries, every kept entry byte-identical
(55,667 on switzerland light), every expected-dropped entry absent (104,249),
redirects to dropped targets dropped, `Counter` metadata recomputed, UUID
fresh unless `--keep-uuid`.

What the derive rewrites: `map-config.json` (flags, `maxZoom`, a `derived`
block naming the recipe), `streetzim-meta.json` (`derivedFrom`: source UUID,
filename, recipe, date), `M/Name`, `M/Title`, `M/Description`, `M/Flavour`,
`M/Counter`, and `wiki-geo-index.json` under `--no-wiki`.

### Known limits of the current tool

- **Not yet a search-data or Xapian transform.** `--no-wiki` leaves dead
  documents in the fulltext index, as predicted above. The address-stripping
  recipe (the 53% lever on switzerland) needs the leaf rewrite and an
  `xapianbuilder` rerun; that is tier 2 work the tool does not do yet.
- **Regroup memory.** The first switzerland regroup peaked at 3.95 GB RSS;
  blobs now spill to bucketed files (1/256 of a zoom per bucket) so memory
  is bounded, but a continent regroup is still a 4-core re-encode of every
  tile at zstd-22 (~3 MB/s per core), i.e. hours for europe. Regroup is an
  experiment knob, not the shipping path; the builder should emit the layout
  directly.
- **Viewer slots are not re-padded.** A pre-slot source stays pre-slot; run
  `swap_viewer_rust.py` once as today. A slotted source copies its slot
  cluster verbatim, so `patch_viewer_inplace.py` keeps working on the output.
- **Peak RSS figures include mmap'd file pages** and overstate heap use.

## Sparse regions (2026-09-20, second pass)

Switzerland is the wrong file to size a light recipe on: it is a small dense
country where search and routing dominate. `zim_inventory.py` now runs
against a URL, reading only the tables (header, dirents, cluster pointers,
raw-cluster offset tables) over HTTP range requests: 191 MB fetched for
argentina, 872 MB for south-america, no download.

| region | size | tiles | of which z14 | satellite | terrain | search-data | routing |
|---|---|---|---|---|---|---|---|
| switzerland | 2.2 GB | 36% | 22% | 5.6% | 3.0% | 22% | 18% |
| argentina | 3.4 GB | 25% | 13% | 9.6% (z12: 7.0%) | **20%** (z12: 12.8%, z11: 4.9%) | 21% | 10% |
| australia-nz | 7.2 GB | 18% | 10% | **29%** (z13: 1.4 GB) | 12% (z12: 575 MB) | 14% | 7% |
| south-america | 21 GB | 18% | 9.5% | 4.5% | 15% (z12: 2.0 GB) | **47%** | 8.6% |

So the lever differs per region and the inventory has to come first:

- **argentina**: terrain z12 alone is 13%; satellite z12 another 7%; z14
  tiles 13%. A light recipe that only drops z14 vectors and satellite
  leaves the biggest raster component untouched.
- **australia-nz**: satellite is the file. Its z13 satellite is 1.4 GB, so
  `--satellite-max-zoom 12` alone saves 20%.
- **south-america**: search-data is 10 GB of 21. Addresses, not imagery.
  That is the tier-2 leaf rewrite, still not implemented here.

### Argentina: the sparse-light recipe

```
python3 cloud/derive_zim.py osm-argentina.zim ar-light.zim \
    --max-tile-zoom 13 --terrain-max-zoom 11 --satellite-max-zoom 11 \
    --title "OSM - Argentina (Light)" --name osm_argentina_light
```

| recipe (dry-run) | copy / re-encode / drop clusters | dropped |
|---|---|---|
| `--light` (no sat, z13) | 1181 / 7 / 170 | 769 MB |
| z13 + terrain ≤ z11 + satellite ≤ z11 | 1139 / 9 / 210 | **1113 MB** |
| `--no-terrain --no-satellite` | 1224 / 6 / 128 | 1016 MB |
| `--terrain-max-zoom 11` | 1300 / 5 / 53 | 434 MB |

The second recipe ran in **47 s**: 3.43 GB to 2.17 GB (63%), 736,142 of
2,599,702 entries kept, verified identical, all `tiles/14/`, `terrain/12/`,
`satellite/12/` entries absent, search and suggestions working.

The re-encoded clusters are the same nine every time: map-config, metadata,
the raw satellite/terrain/xapian cluster at the component boundary, and the
six clusters where two zooms meet. Everything else is copied.

### Does zoom-major clustering slow down zooming? Measured.

The worry: if every zoom level lives in its own clusters, a zoom-in sequence
touches a new cluster at every step instead of finding neighbouring zooms in
one. `zim_access_sim.py` replays three interactions (first view, zoom 4→14,
six-screen pan at z14) on a phone viewport, counting distinct clusters read
and their compressed bytes, and with `--measure` timing the same fetches
through python-libzim (warm page cache, so this is dirent lookup plus
inflate, which is the part a phone pays in CPU).

Four layouts of the same content: the shipped file (component-major, zoom
runs with mixed boundary clusters, ~8 MiB uncompressed clusters), a Hilbert
zoom-major regroup at 8 MiB, the same at 2 MiB, and the sparse-light derive.

**argentina, Buenos Aires, tiles + satellite + terrain, zoom 4→14:**

| layout | clusters read | MB inflated | libzim ms |
|---|---|---|---|
| shipped | 23 | 141 | 715 |
| hilbert, 8 MiB | 30 | 121 | 289 |
| hilbert, 2 MiB | 35 | **44** | **109** |
| sparse-light (shipped layout) | 18 | 98 | 435 |

**argentina, El Calafate (Patagonia), same:** shipped 23 reads / 152 MB /
816 ms; hilbert 8 MiB 27 / 110 / 116 ms; hilbert 2 MiB 30 / 40 / 48 ms.

**first view (startup, z6):** shipped 6 reads / 33.9 MB / 116 ms; regrouped
5 reads / 9.0 MB / 11-15 ms, both cluster sizes. The shipped file's low-zoom
tiles share clusters with unrelated bulk, so the first paint inflates 34 MB
to draw 84 tiles.

**pan at z14, six screens, Calafate:** shipped 6 reads / 37 MB / 434 ms;
hilbert 8 MiB 2 / 12 MB / 21 ms; 2 MiB 3 / 4 MB / 12 ms.

**switzerland, Zurich, tiles only, zoom 4→14:** shipped 13 reads / 63 MB /
540 ms; hilbert 8 MiB 15 / 50 MB / 338 ms. Zermatt with all layers: 22 / 135
MB / 692 ms against 32 / 112 MB / 262 ms.

Reading of the numbers:

1. **Zoom-major does add cluster reads on a zoom-in**, 15-40% more, exactly
   as feared: each zoom step lands in its own cluster. But each of those
   clusters is smaller and contains nothing but that zoom, so bytes inflated
   fall 15-30% and measured time falls 2-7x. The reads were never the cost;
   inflating megabytes of unrelated tiles to get at one was.
2. **Cluster size is the bigger knob.** 2 MiB clusters cut inflated bytes
   another 2.7x over 8 MiB at the cost of 1.8% file size (argentina; 0.6% on
   switzerland). MapLibre fetches 12-20 tiles per view; a 2 MiB cluster
   holds ~100 z14 tiles, so most views still resolve in 1-3 clusters.
3. **Hilbert order within a zoom** is what keeps the pan cheap: six screens
   east at z14 cost 2-3 cluster reads total because neighbouring tiles are
   neighbouring blobs.
4. The light derive inherits the shipped layout and so inherits its startup
   and zoom costs; a variant should be derived from a well-laid-out source,
   or regrouped once.

Caveat: `--measure` times python-libzim on this container with the file in
page cache and libzim's own cluster cache in play, so absolute ms are not a
phone's, and the per-step numbers wobble by tens of ms. The ratios between
layouts on the same file are the result.

### What this asks of the builder

Emit tiles, satellite and terrain **zoom-major with a cluster break per
zoom and Hilbert order within a zoom**, and use a smaller cluster target for
those three components (2 MiB) than for search and routing (8 MiB, where the
typeahead measurements in gotcha #7 want big clusters). That is an insertion
order and two config values, no format change, and it makes every later
derive a pure cluster copy. `--regroup-tiles` exists to measure this on
shipped files, not to be the production path: it re-encodes every tile
(argentina: 2.5 M tiles, 285 s on 4 cores; a continent is hours).

### Costs observed

| run | source | time | peak RSS |
|---|---|---|---|
| argentina sparse-light | 3.43 GB, 2.6 M entries | 47 s | 3.9 GB |
| argentina regroup hilbert 8 MiB | 3.43 GB | 285 s | 5.5 GB |
| argentina regroup hilbert 2 MiB | 3.43 GB | 218 s | 7.3 GB |

RSS counts the mmap'd source pages the run touched, so it scales with bytes
read rather than heap; the real heap cost is the in-memory dirent list (2.6 M
Dirent objects for argentina). A continent with 12 M entries (south-america)
will want dirents parsed into arrays instead of objects before this tool is
run on one. Open item.
