# DS4 sweep review (perf focus) — streetzim

Exhaustive per-file pass: 118 code files across 13 batches.

## Findings

# Batch-1 performance review findings

Reviewed every listed file (build/maintenance scripts and Python tools for the
streetzim map-data pipeline). No hot request paths in this batch — all files are
build jobs, audit tools, or smoke tests — so findings are constant-factor /
batch-job waste, not request-latency incidents.

## Findings

- [medium] cloud/build_torrent.py:92 — O(n) bytearray front-slice deletion inside the per-piece hashing loop — `del buf[:piece_size]` shifts the whole remaining tail left on every piece (CPython `memmove` of `len(buf)-piece_size` bytes), even though the buffer is already drained to `< piece_size`. Each piece moves ~1 MiB (the last read chunk), so total memmove ≈ `file_size/piece_size × 1 MiB`; for small files where `auto_piece_size` yields ~1 MiB pieces this is an extra full pass (~file size) of memory traffic added to the hash stream, and for the 20+ GB ZIMs it's ~2.5 GB of extra copying — on the single hottest loop of the tool (docstring: "generating torrents for all regions... O(transfer_time)"). Smallest safe fix: keep a `start` offset and only `del buf[:start]`/reset once per drain, or read `piece_size`-aligned chunks so `del` happens once per file, or hash the slice via a `memoryview` and `buf = bytearray()` per piece.
- [low] cloud/fix_stale_terrain_tiles.py:57-62,181-185 — loop-invariant bbox-corner recomputation per tile per region — `tile_in_bbox` calls `ll_to_tile` (3 trig ops each, `math.log`/`tan`/`cos`) 4 times per tile per region, but those 4 values depend only on `z` and the region bbox, never on `x,y`. Inside the scan loop over every z0-8 tile in up to 7 regions (hundreds of thousands of tiles) this recomputes millions of trig calls that are identical for all tiles of a given zoom/region — constant-factor waste dominating the scan pass. Smallest safe fix: compute the per-region corner tiles `(xmin,xmax,ymin,ymax)` once outside the tile loop (they only vary by `z`), and pass them into `tile_in_bbox`.

## Coverage

- .smoke_viewer_playwright.py — clean
- build-and-upload-queue.sh — clean
- build-california-2026-05-10.sh — clean
- build-coasts.sh — clean
- build-region-and-upload.sh — clean
- build-small-region.sh — clean
- build-ukraine.sh — clean
- build_world_and_us.sh — clean
- cloud/audit_dem_cache.py — clean
- cloud/build-vm-startup.sh — clean
- cloud/build_region.sh — clean
- cloud/build_torrent.py — findings: 1
- cloud/chip-retrofit-d.sh — clean
- cloud/chip_rules.py — clean
- cloud/cleanup_old_zims.py — clean
- cloud/decode_check.py — clean
- cloud/deploy_pwa.sh — clean
- cloud/diff_zim.py — clean
- cloud/fix_stale_terrain_tiles.py — findings: 1
- cloud/fix_terrain_seams.py — clean

## Notes

- Files are build/maintenance scripts and batch audit tools; none are request
  paths, so no N+1/query/pagination findings apply. The shell polling loops
  (`sleep 30/60/900` in build-coasts.sh and build-vm-startup.sh) are low-frequency
  waits on build jobs, not tight polling — dismissed.
- Dismissed: audit_dem_cache.py and diff_zim.py whole-entry reads (`src.read`,
  `bytes(e.get_item().content)`) are inherent to computing vmin/vmax / content
  hashes and run once per bounded file — not hot paths.
- Dismissed: chip_rules.py `split_records_by_chip` scans each chip's category
  list once per chip; the 11 chips are a constant (bounded, not growing input),
  so it's O(c·n) with c constant, not O(n²).
# Batch-2 performance review findings

All files are cloud build/maintenance/batch tools (Python + shell) — no
request-path servers, so findings are batch-job waste / redundant work,
not request-latency incidents. The heaviest file is `repackage_zim.py`,
the hot path that re-emits every entry of multi-GB ZIMs.

## Findings

- [medium] cloud/repackage_zim.py:1129-1140 — chip sub-bucketing `while True: n_sub *= 2` re-buckets and re-serializes every record at every doubling step — `buckets = [[] for _ in range(n_sub)]` + `for r in dst_records: buckets[_sub_bucket_for_name(name, n_sub)].append(r)` + `bucket_blobs = [json.dumps(b, ...) for b in buckets]` runs once per iteration until `biggest <= threshold_b or n_sub >= 256`. For a fat chip (Japan restaurants 164 MB, millions of records) the worst case doubles 1→256 (8 iterations), each doing a full FNV hash of every record AND a full JSON serialization of the entire dataset — ~8× serialization of a 164 MB dataset (~1.3 GB of `json.dumps` work) plus 8 full hash passes. The doubling is also O(n_sub) bucket-list allocation per iteration. Smallest safe fix: bucket once directly into 256 (the cap) and only keep the levels actually needed, or estimate leaf sizes without re-serializing each doubling.
- [medium] cloud/repackage_zim.py:151-183 — `_split_records_recursive` re-serializes the full `records` list at every recursion node (`serialized = json.dumps(records, ...)` at the top of each call) before bucketing and recursing, so the total serialization work is ~(1 + max_depth)× dataset size — for a 500 MB hot search chunk split to 1 M leaves at max_depth 5 that's ~3 GB of redundant `json.dumps`. The leaf-count accounting then re-parses every emitted leaf (`leaf_count = len(json.loads(sub_bytes.decode("utf-8")))` at line 265) — another full parse pass over the dataset. This is the exact hot path `--split-hot-search-chunks-mb` exists for (dense-prefix regions). Smallest safe fix: return the record count alongside the leaf bytes from the recursion (it's known at each leaf) and drop the per-leaf `json.loads`, and check threshold against a cached serialized length rather than re-dumping at each node.
- [low] cloud/repackage_zim.py:803 — loop-invariant `src_mtime = os.path.getmtime(src_path)` recomputed per terrain entry — the statement sits inside the per-entry passthrough loop (`for i in range(src.entry_count)`, line 748) under `if refresh_terrain_dir and path.startswith("terrain/")`. A US-scale ZIM with hundreds of thousands of terrain/*.webp entries pays a `stat` syscall on the same source file per tile; the source mtime never changes during the loop. Smallest safe fix: hoist `src_mtime = os.path.getmtime(src_path)` once before the entry loop.
- [low] cloud/generate_all_torrents.py:57,64 — search API call is not paginated — the query hard-codes `&rows=100` with no `page` parameter, and `list_streetzim_items` returns only `data["response"]["docs"]`. Archive.org advancedsearch caps a page at 100 docs, so any streetzim-* item beyond the first 100 (latent today at ~2 dozen regions, but grows as regions ship) silently never gets a torrent generated. Smallest safe fix: loop over `page` until `response["numFound"]` is fully covered.
- [low] cloud/preflight.py:338-344 — the `seen` set in `check_terrain_cache` is dead weight — `key = (z, t.x, t.y)` always includes `z`, so tiles from different zooms never collide and `mercantile.tiles` yields each tile once, making `if key in seen: continue` never skip. For a continent bbox at z12 (~1.3 M tiles) this keeps an extra set of ~1.3 M tuples in memory plus a per-tile dict membership check. Smallest safe fix: delete the `seen` set and the `continue` guard.
- [low] cloud/regen_all_low_zoom.py:124 — `pool.map(regen, jobs, chunksize=1)` forces one IPC/pickle round-trip per tile across the 12 workers — with z0-z7 that's ~21.8 K jobs, each individually pickled and returned. The per-tile rasterio reproject dominates, but chunksize=1 adds ~21 K pickle+IPC transactions. Smallest safe fix: drop `chunksize=1` (use the default ~2 K chunk) so args/results are batched per IPC.
- [low] cloud/route_cli.py:71-84,410-411 — `nearest_node` does a full numpy pass over every node (~20 M) allocating ~5 float64 arrays (~160 MB each, ~800 MB transient) per call, and `find_route_two_pass` calls `nearest_node_filtered` for hw_src and hw_dst on top of `main()` already calling `nearest_node` for src and dst — the same O(n) distance computation over all nodes is done 4× per route. Smallest safe fix: compute the full distance array once and reuse it across the src/dst/hw lookups, and short-circuit the highway-only scan by precomputing which node ids have a highway edge.

## Coverage

- cloud/generate_all_torrents.py — findings: 1
- cloud/launch-build-vm.sh — clean
- cloud/manifest_writer.py — clean
- cloud/preflight.py — findings: 1
- cloud/rebuild-all.sh — clean
- cloud/rebuild_overture_regions.sh — clean
- cloud/regen_all_low_zoom.py — findings: 1
- cloud/regen_low_zoom.py — clean
- cloud/repackage_zim.py — findings: 3
- cloud/reroll-sv-iran.sh — clean
- cloud/reroll_viewer.sh — clean
- cloud/restart-build.sh — clean
- cloud/rollout_viewer_swap.sh — clean
- cloud/route_cli.py — findings: 1
- cloud/serve_zims.py — clean
- cloud/spot-to-ondemand-watcher.sh — clean
- cloud/stamp_item_metadata.py — clean
# Performance review — batch 3 (cloud/ pipeline & build scripts)

## Findings

- [high] cloud/verify_terrain_freshness.py:321 — the full `dem_index` dict (~26K DEM files) is embedded in **every** job tuple, so ProcessPoolExecutor pickles it once per tile — `jobs.append((z, x, y, p, dem_index, newest, args.check_content))` then `pool.map(_check_tile, jobs, chunksize=256)` — a 26K-entry dict re-serialized per job. A z0-12 audit over all regions enumerates ~20K+ tiles per region, so each pass re-pickles the whole index across the process boundary (tens of GB of IPC/CPU) — the audit slows to a crawl or worse as DEM count × tile count grows. — Pass the immutable index once (worker `initializer`/module-global, or `fork` before spawning) instead of carrying it in every job arg.
- [high] cloud/validate_zim.py:1248 — `size = len(bytes(e.get_item().content))` in `_audit_tiles` decompresses **every** tile entry's content just to measure size, despite the docstring claiming "No content reads — just metadata iteration". On a Japan-scale ZIM (millions of tiles) this decompresses the whole tile set per validation — the audit is minutes-to-tens-of-minutes, not the ~20 s the docstring promises. — Use `e.get_item().size` (libzim reports the decompressed size without decompressing), which is how `_chk_satellite_coverage`/`_chk_vector_coverage` already measure tiles.
- [medium] cloud/validate_zim.py:1020 — `size = len(bytes(e.get_item().content))` in `_chk_search_data_sizes` fully reads + decompresses **every** search-data chunk (some are 100+ MB) to compare against size thresholds. — Use `e.get_item().size` instead of reading content; the manifest's stored per-chunk sizes are also available.
- [medium] cloud/validate_zim.py:166 — roughly ten check functions (`_chk_fonts:166`, `_chk_satellite_coverage:508`, `_chk_vector_coverage:544`, `_chk_terrain_edge_stripe:256`, `_chk_overture_fields:588`, `_chk_tile_corners:963`, `_chk_routing:1082`, `_chk_satellite:774`, `_chk_terrain:794`, `_audit_tiles:1222`) each do a full `for i in range(arc.entry_count)` archive walk, so one validate repeats the entire entry enumeration ~10×; on a 35M-entry Canada-scale ZIM that is ~10 full archive iterations per upload. — Coalesce the metadata/path bucketing into a single pass (bucket paths by namespace once) and have each check consume the bucket.
- [medium] cloud/validate_overture_urls.py:566 — `_save_checkpoint` (called synchronously from the async `bounded()` task at line 444 every `checkpoint_every` URLs) does `tmp.write_text(json.dumps(cache, indent=2, sort_keys=True))` on the **whole** ever-growing cache, blocking the asyncio event loop during a large serialize+write; with 100K+ cached URLs each checkpoint stalls the whole crawl. — Serialize only the new `partial` entries (merge incrementally), stream the write, and drop `indent=2`/use compact separators; or move the write off the event loop.
- [medium] cloud/upgrade_spatial_zim.py:565 — inside the `for sub_prefix, sub_bytes in leaves` loop, `leaf_count = len(json.loads(sub_bytes.decode("utf-8")))` re-parses every leaf's JSON (the very bytes `_split_records_recursive` just serialized) just to count records — redundant full parse of all split content. — Have the splitter return per-leaf record counts, or count records once before serializing.
- [medium] cloud/upgrade_spatial_zim.py:663 — `LazyPassthroughItem`/`LazyZimEntryProvider.gen_blob` (lines 107-109) call `self._src.get_entry_by_path(self._path)` once **per** passthrough entry at cluster-compress time (a binary search over the path index for every entry of a multi-million-entry ZIM). — Build a path→entry-id map during the existing Phase-3e enumeration (line 631 already iterates by id) and have gen_blob use `_get_entry_by_id` instead of a path lookup.
- [medium] cloud/swap_viewer_rust.py:180-181 — the geo-index filter loop calls `src.has_entry_by_path("wiki-article/" + t)` once per geo entry `t` (an archive path lookup per title) to decide whether to keep it; with thousands of places this is thousands of per-item archive searches during an already long repackage. — Collect the set of bundled `wiki-article/` paths in one pass during the main entry walk (line 132 already visits every entry) and replace the per-title archive lookup with `t in article_paths` set membership.

## Coverage
- cloud/swap_viewer_rust.py — findings: 1
- cloud/upgrade_spatial_zim.py — findings: 2
- cloud/upload-caches.sh — clean
- cloud/upload_shipped.sh — clean
- cloud/upload_url_cache.sh — clean
- cloud/upload_validated.sh — clean
- cloud/url_cache_filter.py — clean
- cloud/validate_overture_urls.py — findings: 1
- cloud/validate_platforms.py — clean
- cloud/validate_zim.py — findings: 3
- cloud/verify_terrain_freshness.py — findings: 1
- cloud/vm-health-cron.sh — clean
- cloud/wait-and-launch.sh — clean
- cloud/wiki_articles.py — clean
# Batch 4 — Performance review findings

- [high] cloud_terrain_gen.py:99 — Per-tile S3 DEM re-fetch with no local cache — each z12 tile calls `with rasterio.open(dem_url) as src` on `/vsis3/{bucket}/{key}` for each DEM its bounds touch; the same 1°x1° Copernicus DEM is shared by ~130 neighbouring z12 tiles (a z12 tile is 360/4096 ≈ 0.088°, so ~11.4 tiles per DEM dimension), so every DEM is re-downloaded from S3 once per tile instead of once — at 16.7M world z12 tiles this is tens of millions of redundant S3 range requests and is the dominant cost/latency of the whole job — smallest safe fix: cache each DEM dataset (or the downloaded .tif) once per worker in a per-worker dict keyed by the DEM key so all ~130 tiles sharing it reuse the same open.

- [high] cloud_terrain_gen.py:159-164 — Full world task list materialized + stat per tile before the pool starts — `for tile in mercantile.tiles(...)` over the whole world bbox at z12 generates ~16.7M tile objects, does `os.path.isfile(tile_path)` (one stat syscall per tile, line 161) and appends every tuple to `tasks` (line 164), so the main process holds a multi-GB list (~16.7M 4-tuples ≈ 1–2 GB) and issues ~16.7M stat syscalls before any worker runs; on restart with most tiles cached this startup scan dominates and risks OOM on smaller instances — smallest safe fix: feed the pool lazily from the tile generator (chunked) instead of materializing the whole list, and/or do the cached-skip `os.path.isfile` check inside workers rather than in the main process.

- [low] cloud_terrain_gen.py:195-197 — Full-tree `os.walk(args.output)` recount of every .webp at completion — walks the whole ~16.7M-file output tree a third time (after the task-list build) to write the COMPLETED marker, adding a long I/O pass over 16.7M files at the end of each run — smallest safe fix: maintain a running counter incremented per generated tile in the pool and write that into COMPLETED instead of re-walking the tree.

- [medium] cloud_rsync_loop.sh:25 — `LOCAL_COUNT=$(find "${LOCAL_DIR}/12" -name "*.webp" | wc -l)` re-scans the entire local terrain_cache/12 tree (target ~16,728,064 tiles) on every 20-minute loop iteration — each iteration walks ~16.7M small files (minutes of I/O per pass) forever, so the loop's own bookkeeping grows with total tile count — smallest safe fix: track the local count incrementally (e.g. count newly rsync'd files per iteration and add to a persistent counter) instead of re-`find`-ing the whole tree each cycle.

- [low] cloud_terrain_collect.sh:108 — `LOCAL_TOTAL=$(find "${LOCAL_DIR}/12" -name "*.webp" | wc -l)` does one full-tree scan of the ~16.7M-file local tile tree at the end of each collection run, a multi-minute I/O walk whose cost scales with total tiles — smallest safe fix: derive the total from the per-instance remote counts already gathered (or an incremental counter) rather than re-walking the whole local tree.

- [low] cloud/wikidata_titles.py:229-243 — `_measure` reads each search-data chunk fully into memory (`raw` returns all bytes via `get_item().content`), scans it (`b'"q":' not in b`) and `json.loads(b)` per chunk, so the measurement CLI's peak memory scales with the largest chunk (and per-chunk bytes scan) rather than streaming — smallest safe fix: iterate the chunk as a stream / parse with `ijson` or stream-read instead of loading whole chunks when measuring a full ZIM.

## Coverage

- cloud/wikidata_titles.py — findings: 1
- cloud_rsync_loop.sh — findings: 1
- cloud_terrain_collect.sh — findings: 1
- cloud_terrain_gen.py — findings: 3
- cloud_terrain_launch.sh — clean
- cloud_terrain_monitor.sh — clean
# Batch 5 — performance review of create_osm_zim.py

Findings for `create_osm_zim.py` (OSM → ZIM batch pipeline). This is a batch/offline build script, so findings are calibrated to data-pipeline cost (memory, CPU, redundant recomputation) rather than request-path latency. Applied: performance-review checklist + python-performance-review checklist.

- [high] create_osm_zim.py:4822 — reverse_geocoder KNN search fired once per search-feature record that lacks a `location` — `feat["location"] = loc_lookup(feat["lat"], feat["lon"])` inside the streaming search-bucketing pass, where `loc_lookup` (build_location_index) calls `_rg.search([(lat, lon)], mode=1)` (line 1444) — a KNN search over the ~30 MB GeoNames dataset per call. Every address record from `extract_addresses_pbf` and every Overture-added record (millions on continent-scale Europe builds) has no `location`, so each one triggers a single-point KNN lookup inside an otherwise cheap JSON-parse/write loop. — Consequence: tens of millions of KNN searches add hours to continent builds and can dominate the pass; the loop is otherwise linear JSON work. — Smallest safe fix: batch coordinates through reverse_geocoder (`_rg.search([...], mode=1)` accepts a list and returns all in one tree walk) by collecting features lacking `location` and resolving in chunks, or precompute a coarse (lat,lon)→label grid once and reuse.

- [medium] create_osm_zim.py:1280 — non-streaming MBTiles path materializes every tile blob into an in-memory dict — `tiles[(z, x, y)] = data` — for MBTiles up to 5 GB (the streaming gate at line 5935 is `use_streaming = mbtiles_size_gb > 5.0`), and create_zim then builds a second full list of every tile with `tile_source = iter([(z, x, y, data) for (z, x, y), data in sorted(tiles.items())])` (line 4222). — Consequence: a ~5 GB MBTiles with millions of z14 tiles is held as a Python dict of blobs (bytes + dict overhead) plus a fully sorted copy of all items, which can exceed available RAM and OOM the build on typical build boxes at the 5 GB boundary. — Smallest safe fix: lower the threshold or always route through the streaming path (`iter_tiles_from_mbtiles`), which already yields tiles one at a time from SQLite.

- [medium] create_osm_zim.py:5004 — after an oversized search chunk is split, `leaf_count = len(json.loads(sub_bytes.decode("utf-8")))` re-parses the entire serialized leaf (potentially tens/hundreds of MB for continent hotspots like Japan's `u5927` at 514 MB) just to count records for the manifest. — Consequence: a second full JSON parse of each leaf doubles the serialization cost of the hottest search chunks. — Smallest safe fix: have `_split_records_recursive` return per-leaf record counts (it already holds the records), or track `len` during the split instead of re-loading.

- [medium] create_osm_zim.py:4781-4791 — `_prefixes_for(name)` calls `_prefix_key(name[:2])` once plus `_prefix_key(m)` once per word from `_word_re.findall(name)`, and every `_prefix_key` call runs `_norm(word)` (NFKD normalize + combining-filter + lowercase, line 4752). A multi-word name is therefore Unicode-normalized once per word plus once for the prefix — e.g. a 3-word name is re-normalized ~4× on overlapping substrings, and the same words recur across millions of feature names. — Consequence: redundant per-feature Unicode normalization dominates the search-bucketing pass for large corpora. — Smallest safe fix: `functools.lru_cache` the `_norm` result (module-level, keyed by word), or normalize the whole name once and then split/reuse the normalized form.

- [medium] create_osm_zim.py:2418, 2502, 2576 — `_normalize_street` (NFKD fold + regex tokenize + suffix-expansion join) is recomputed for the same OSM POI names in all three passes of `merge_overture_places` (Pass A index line 2418, Pass B key line 2502, Pass C lookup line 2576) and for every Overture row; street/city names repeat heavily across records. — Consequence: millions of redundant per-record normalizations across passes (same string normalized 2-3×). — Smallest safe fix: module-level `functools.lru_cache(maxsize=...)` on `_normalize_street` (it is idempotent), collapsing repeated street/city normalizations.

- [low] create_osm_zim.py:4455-4457 — after each Wikidata chunk is already serialized once to add it as a ZIM item (`chunk_json = json.dumps(chunk_entries, ...)` at line 4446), `total_bytes = sum(len(json.dumps(v, separators=(",", ":"), ensure_ascii=False).encode()) for v in wd_chunks.values())` re-serializes every chunk a second time just to compute a size stat for the log. — Consequence: double serialization of all Wikidata chunks. — Smallest safe fix: accumulate `len(chunk_json)` during the emit loop instead of re-serializing.

## Coverage
- create_osm_zim.py — findings: 6
# Batch 6 — performance review

Files are mostly batch/offline build + deploy scripts plus the browser-side routing worker, service worker, and the Rust ZIM packer. Calibrated to data-pipeline cost (RAM, CPU, redundant I/O) for the scripts/Python/Rust and to hot-path cost (routing engine, cell cache) for the JS. Applied: performance-review + python-performance-review + js-ts-performance-review + rust-performance-review.

## Findings

- [medium] create_osm_zim_leaflet.py:269-273 — `extract_tiles_from_mbtiles` materializes every tile blob into an in-memory dict (`tiles[(z, x, y)] = data` for `for z, x, tms_y, data in cursor`), and `render_all_tiles` (line 648) then iterates `sorted(vector_tiles.items())`, building a full sorted copy of all keys. — Consequence: a multi-GB MBTiles (e.g. Virginia at z14) is held as a Python dict of compressed blobs plus a second sorted list of every item, which can exceed RAM on the build box and OOM before any tile is rendered. — Smallest safe fix: stream tiles directly from the SQLite cursor into `render_all_tiles` (one tile at a time, no dict, no full sort); MBTiles rows are already in (z,x,y) order.
- [medium] create_osm_zim_leaflet.py:181-206 — `download_satellite_tiles` fetches every satellite tile serially (`for x in range(...)` / `for y in range(...)` with one `urllib.request.urlopen` + full body read + WebP encode per tile) with no concurrency. — Consequence: at z14 a region can require 10^4–10^5 tiles; at ~50 ms+ per HTTP round-trip the serial loop adds hours to the build, and retries multiply it. — Smallest safe fix: run the per-tile download/encode through a `ThreadPoolExecutor` (I/O + Pillow are GIL-releasing) bounded to e.g. 8–16 workers, keeping the existing cache-skip check.
- [low] create_osm_zim_leaflet.py:659 — `img.save(tile_path, "PNG", optimize=True)` runs PIL's optimize/color-quantization second pass on every RGB tile, roughly doubling the PNG encode cost per tile. — Consequence: constant-factor CPU waste multiplied by millions of raster tiles (z14 region-scale). — Smallest safe fix: drop `optimize=True` for these fixed-size RGB tiles (encode once); reserve optimize for palette-indexed or one-off saves.
- [low] create_osm_zim_leaflet.py:522-525 — `_draw_label`'s overlap check scans the whole `labels_drawn` list per label (`for existing in labels_drawn:`), an O(n²) scan inside each tile render. — Consequence: dense city tiles (hundreds of place/road labels) pay quadratic overlap comparisons per tile; bounded per-tile but multiplied across millions of tiles. — Smallest safe fix: keep labels in a small grid/quadtree keyed by pixel cell (e.g. 64 px buckets) so each label only checks its neighborhood, or cap the per-tile label count.
- [medium] fix_boundary_chunk.py:57 — inside the per-tile lat/lon loop, `with rasterio.open(dem_path) as src:` re-opens the same 1-degree DEM GeoTIFF (tens of MB) for every tile that touches it; a chunk of ~10^5 tiles re-opens the same DEM file ~10^5 times. — Consequence: GDAL open/header-parse per tile per adjacent cell dominates the regeneration pass; adjacent tiles re-open identical files. — Smallest safe fix: keep the opened DEM dataset open per (lat,lon) in a small per-process dict (they are only ~26k distinct files) instead of `with rasterio.open(...)` per tile.
- [medium] fix_boundary_terrain.py:68 — `_regen_tile` opens `rasterio.open(dem_path)` per tile per overlapping DEM cell (lines 61-73) in every worker, re-opening the same DEM file for every adjacent boundary tile. — Consequence: tens of thousands of boundary tiles each re-open identical multi-tens-of-MB DEM files, adding GDAL open cost per tile across the whole boundary set. — Smallest safe fix: open each DEM dataset once per worker and reuse the handle (key by path), like the dem_cache pattern already used in verify_tile_cache.py.
- [medium] resources/viewer/routing-worker.js:710-721 — `snapNearestNode` scans ALL cells (`for (var cid = 0; cid < this._index.numCells; cid++)`) building a distance array and then `candidates.sort(...)` sorts every cell, per snap. — Consequence: every origin/destination click does an O(numCells log numCells) scan+sort over the whole graph; on a world-scale graph this makes snapping visibly laggy. — Smallest safe fix: only consider cells whose (lat,lon) bounding box is within a few cells of the query point (compute the cell for the point, check neighbors), avoiding the global scan and sort.
- [medium] resources/viewer/routing-worker.js:611-615 — `_touch` does `this._lru.indexOf(cid)` + `splice` on an array (O(lru-size) each), and `compact`/`_evictToBudget` use `this._lru.shift()` (O(n) each, lines 604/618). — Consequence: during a long route touching thousands of cells, LRU bookkeeping is O(n²) in cell touches; `compact` runs repeatedly in the two-pass path. — Smallest safe fix: replace the array LRU with a doubly-linked list (move-to-front O(1)) or a `Map`-keyed order, and evict from the tail instead of `shift()`.
- [medium] resources/viewer/routing-worker.js:909 — inside the A* edge loop, `var targetCoords = await graph.nodeCoordsE7(target);` awaits a promise (and for v3 does a binary-search `cellForNode`) per successful relaxation; up to 200k–400k pops × ~8 edges = millions of promise/await + binary searches per route. — Consequence: the hot A* loop spends most of its time on per-edge async dispatch and cell-index lookup even when the cell is already resident. — Smallest safe fix: resolve node coordinates in bulk per cell (load the cell's `nodesScaled`/shard slice once and index locally) and keep the per-cell coords in the A* `g`/`prev` maps instead of a per-edge async call.
- [medium] resources/viewer/routing-worker.js:958 — path reconstruction does `path.unshift(segment)` per edge, shifting the whole array each time (O(n) per unshift). — Consequence: a long route with thousands of segments costs O(n²) array moves to build the path. — Smallest safe fix: push segments onto the array while walking `prev`, then `reverse()` once before returning.
- [medium] resources/viewer/routing-worker.js:999 — `findNearestHighwayNode` uses `var current = queue.shift()` on a plain array (O(n) per shift) in its BFS over up to `maxPops` (5000) nodes. — Consequence: BFS dequeue is O(n²) in queue size, repeated for start and end nodes in two-pass mode. — Smallest safe fix: use a ring-buffer index cursor or a `Deque`-style implementation instead of `shift()`.
- [medium] rust/streetzim-pack/src/main.rs:356-366 — the entire JSONL manifest is deserialized into `Vec<Record>` (line 356 `let mut records: Vec<Record> = Vec::new();`), holding every record — including `body_b64` strings for every base64-inlined tile — fully in RAM, then processed in two passes (lines 371-406). — Consequence: a region-scale manifest with millions of tile records (each base64 body ≈ 1.33× file size) can be many GB in memory before `start_writing`, risking OOM on the build box. — Smallest safe fix: process records streaming as they parse (apply config inline, handle each item immediately) instead of collecting the whole manifest, or re-read the manifest for the second pass.

## Coverage
- create_osm_zim_leaflet.py — findings: 4
- download_dem.py — clean
- download_overture_data.py — clean
- drive-rollout.sh — clean
- fix_boundary_chunk.py — findings: 1
- fix_boundary_terrain.py — findings: 1
- fix_terrain_tiles.py — clean
- overture-rollout-redo.sh — clean
- rebuild-all-final.sh — clean
- rebuild-queue.sh — clean
- recompress_avif.py — clean
- resources/viewer/routing-worker.js — findings: 5
- rust/streetzim-pack/Cargo.toml — clean
- rust/streetzim-pack/src/main.rs — findings: 1
- scripts/sync-drive-viewer.sh — clean
- upload-to-archive.sh — clean
- verify_tile_cache.py — clean
- web/drive/build-info.js — clean
- web/drive/fzstd.js — clean
- web/drive/sw.js — clean
# batch-7 perf review

Scope: `web/drive/viewer/maplibre-gl.js` (single file in this batch).

## Finding

- [medium] web/drive/viewer/maplibre-gl.js:5 — monolithic 1MB (275KB gzip) minified vendored MapLibre GL bundle shipped and loaded wholesale — the file is a single un-split minified build of the entire MapLibre GL v5.23.0 engine (module loader `define` at line 15 wraps `modules.shared` + `modules.index` + `modules.worker` into one bundle; it is git-tracked and loaded by `web/drive/viewer/index.html:12` via a synchronous `<script src="maplibre-gl.js">` with no `defer`/`async`). Loading clock: the viewer needs only raster/vector sources, markers, popups, navigation/scale/geolocate controls, but every load parses and executes the full engine — vector/DEM/geojson/video/image source pipelines, symbol-collision, terrain/3D fill-extrusion, heatmap, expression/spec runtime — ~275KB gzipped transfer plus main-thread parse+eval that blocks first paint on a mobile PWA. Consequence: each viewer cold start pays ~275KB transfer and a long parse task for code the page never invokes; INP/LCP on the map page are gated by this single synchronous bundle. Smallest safe fix: since this is a committed upstream build artifact (not team-maintained code), replace the vendored monolithic bundle with a tree-shaken ESM build from npm (`import` only the used `maplibregl` exports) and/or load it with `async`/`module` + initialize the map lazily on first interaction, and serve it with an immutable-cache header (it is versioned via `web/drive/viewer/.version`).

## Coverage

web/drive/viewer/maplibre-gl.js — findings: 1
# Batch 8 — performance findings

Files reviewed: web/drive/viewer/routing-worker.js, web/drive/zim-reader.js, web/generate.py, web/stats.py, web/watch-and-deploy.sh, wikidata_cache.py

- [high] web/drive/viewer/routing-worker.js:909 — in the A* inner relaxation loop `for (var k = 0; k < nodeEdges.length; k++)` (lines 893-915) each improved edge does `var targetCoords = await graph.nodeCoordsE7(target);` (line 909) plus a `haversine(...)` (line 912) with `Math.sin/cos/atan2`. For a 400k-pop route (POP_LIMIT) with several relaxed edges per pop, this is a promise chain + `[lat,lon]` allocation + full trig heuristic per edge relaxation — the dominant per-pop cost in the routing hot path, and every `_ensureCell` inside `nodeCoordsE7` also pays a `_touch` (see below). Consequence: route latency scales with edges×pops; the worker spends most of its A* wall-clock fetching/recomputing coordinates instead of expanding the heap. Smallest safe fix: read target coords directly from the already-resident cell (target is usually in the same/adjacent cell as `current`, already loaded) and use a cheaper linear-distance heuristic (e.g. `dx+dy` or `sqrt(dx²+dy²)` on the E7 coords) instead of haversine per relaxation.
- [medium] web/drive/viewer/routing-worker.js:611-615 — `_touch(cid)` implements the LRU as an array: `this._lru.indexOf(cid)` + `splice(idx,1)` + `push`, all O(resident-cell-count), and is called on every cell cache hit (`_ensureCell` line 670) and every load (line 653). `_evictToBudget` also uses `this._lru.shift()` (line 618), O(n) per eviction. Over a route that expands thousands of cells (California 7.9M nodes), each A* pop/relaxation does an indexOf+splice scan of the whole LRU — O(cells²) total. Consequence: LRU bookkeeping becomes a measurable fraction of route CPU as the resident cell set grows. Smallest safe fix: use a doubly-linked list / `Map` iteration-order LRU (re-`set` on touch, `keys().next()` on evict) so touch/evict are O(1).
- [medium] web/drive/viewer/routing-worker.js:710-721 — `snapNearestNode` builds `candidates` by looping over ALL `numCells` cells (computing per-cell lat/lon bounds) and then `candidates.sort(...)` the whole array, per snap request. The graph's cell count scales with the loaded region (dense ZIMs like California have many cells), so every pin-drop/snap does an O(numCells log numCells) scan+sort. Consequence: snapping latency grows with region size; unnecessary since only the cells near the query point can win. Smallest safe fix: use `cellOf(latE7, lonE7)` to find the containing cell and iterate only its 3×3 neighborhood, dropping the full scan and sort.
- [medium] web/drive/viewer/routing-worker.js:998 — in `findNearestHighwayNode`, the BFS drain uses `var current = queue.shift();` — O(queue-length) per pop because each `shift()` copies the whole array. The queue can grow to thousands of nodes (each expanded node pushes all its neighbors) over up to `maxPops` (5000) pops, so the drain is O(pops × queue-length). Consequence: the two highway-leg BFSes in `findRouteSpatialTwoPass` degrade quadratically on dense graphs. Smallest safe fix: keep an index cursor (`var head=0; current = queue[head++]`) and slice once at the end, or use a two-pointer array.
- [low] web/drive/viewer/routing-worker.js:958 — path reconstruction uses `path.unshift(segment)` per segment; each unshift is O(segments) and shifts the whole array, plus two `await graph.nodeCoordsE7` per segment (lines 946-947). Consequence: O(segments²) copying plus per-segment async on the reconstructed route (hundreds of segments on long routes). Smallest safe fix: push segments into a local array and `reverse()` once at the end instead of unshifting per segment.

- [medium] web/drive/zim-reader.js:171-206 — `findEntry` runs a binary search over `articleCount` and each probe does two async `Blob.slice().arrayBuffer()` reads: `_readUrlPointer(mid)` (line 174) and `_readDirEntryAt(off)` (line 175), each allocating a fresh `Uint8Array` + TextDecoder decode (up to 1024 B per probe). For a ZIM with ~1M articles, an uncached path costs ~log2(1M)=20 probes × 2 reads ≈ 40 async file reads per content lookup, repeated for every uncached URL. Consequence: page/article fetch latency is dominated by per-probe async reads even though the pointer table (articleCount × 8 B) is small enough to load once. Smallest safe fix: read/cache the whole URL-pointer table once (`_readRange(urlPtrPos, articleCount*8)`) and keep a small dir-entry cache, turning lookups into in-memory binary search with O(1) file reads.
- [medium] web/drive/zim-reader.js:227 — on a cluster miss `_loadCluster` decompresses the ENTIRE cluster with `payload = global.fzstd.decompress(raw.subarray(1))` to serve a single small blob, and `clusterCache` holds only 8 clusters. For image-heavy ZIMs with multi-MB clusters and many distinct clusters, each cold blob read decompresses tens of MB. Consequence: memory spikes and decompress CPU per request scale with cluster size × distinct-cluster count. Smallest safe fix: since fzstd has no random-access decompress, keep a larger cluster cache or persist decompressed clusters (e.g. a sidecar per-cluster cache) so repeated blob reads in the same cluster don't re-decompress.

- [medium] wikidata_cache.py:513-514 — `fetch_wikidata_batch` calls `save_cache(cache_dir, results)` every `save_interval` (10000) Q-IDs, and `save_cache` (lines 663-683) reads every existing bucket file (`bucket_path.exists()` → `json.load`) and re-writes every bucket file it touches, each `json.dump` of the whole bucket. As `results` accumulates across a large fetch (millions of Q-IDs), each incremental save re-reads and re-writes the entire cache — O(cache-size) I/O per save, i.e. O(n²) total over the fetch. Consequence: the build job's save phase becomes quadratic; hours-long fetches spend more time serializing buckets than fetching. Smallest safe fix: only write buckets whose contents changed since the last save (track dirty bucket keys), or accumulate results in memory and call `save_cache` once at the end (keep the periodic save only for crash-resume).
- [medium] wikidata_cache.py:215 — `extract_qids_from_mbtiles` does `rows = conn.execute("SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level = 14").fetchall()` — materializing every z14 tile blob into RAM before decoding. On a large MBTiles (millions of z14 tiles), this buffers the whole tile set in memory. Consequence: memory footprint scales with the tile dataset; can OOM on big regions. Smallest safe fix: iterate with a server-side cursor (`conn.execute(...)` + `fetchone`/chunked fetch) and decode tiles incrementally instead of `.fetchall()`.

## Coverage
- web/drive/viewer/routing-worker.js — findings: 5
- web/drive/zim-reader.js — findings: 2
- web/generate.py — clean
- web/stats.py — clean
- web/watch-and-deploy.sh — clean
- wikidata_cache.py — findings: 2
# Batch 9 — performance findings

Files reviewed: test_satellite_compression.py, test_terrain_compression.py, test_zim_perf.py, tests/_regen_headers.sh, tests/diff_corpora.py, tests/functional_search_test.py, tests/generate_cell_diverse_corpus.py, tests/generate_golden_corpus.py, tests/mem_compare.py, tests/run_identity_suite.sh, tests/smoke_japan_spatial.py, tests/szrg_astar.py, tests/szrg_reader.py, tests/szrg_spatial.py, tests/szrg_spatial_astar.py, tests/test_chip_rules.py, tests/test_chunked_zim_roundtrip.py, tests/test_empty_tile_skip.py, tests/test_graph_chunking.py, tests/test_hot_chunk_split_native.py

Most of these are one-off CLI/test/benchmark scripts (not production request paths), so most have no defensible perf finding. The few reported below are real inefficiencies in batch jobs or in the spatial-graph support code that the differential suite runs at scale.

- [medium] tests/szrg_spatial.py:736 — `_ensure_cell` implements the LRU touch as `self._lru.remove(cell_id)` + `self._lru.append(cell_id)` on every cell access, including cache hits; `_lru` is a plain list, so `remove` is a linear scan O(resident-cell-count). `_ensure_cell` is called once per A* pop (via `edges_of_node`) and once per relaxed edge (via `node_coords_e7`) in `find_route_spatial` (tests/szrg_spatial_astar.py:113,126), and the default `cache_limit=None` (load_spatial_from_zim / spatial_graph_from_memory, lines 898/885) means the list grows to every cell ever loaded. Over a route that loads hundreds of cells, each node expansion pays O(cells_loaded) — O(cells²) total for the route. Consequence: routing wall-clock grows quadratically with the number of distinct cells a route crosses. Smallest safe fix: use a `collections.OrderedDict` (re-`move_to_end` on touch, pop oldest on evict) or skip the touch entirely when `cache_limit is None`, making hits O(1).
- [medium] tests/generate_golden_corpus.py:121-122 — in `_run_parallel`, every progress checkpoint runs `reach = sum(1 for r in results if r and not r.get("unreachable"))` and `unreach = sum(1 for r in results if r and r.get("unreachable"))`, each scanning the entire `results` list (size = pairs completed so far). With progress_every=500 and a large corpus (200k pairs), this is ~400 checkpoints × up to 200k-element scans ≈ 80M+ comparisons (×2 for the two sums) on top of the route work. Consequence: the progress-reporting loop is O(n²/p) in the batch job and grows with corpus size. Smallest safe fix: maintain running `found`/`unreach` counters and print those, instead of re-summing the whole result list each checkpoint.
- [low] tests/generate_golden_corpus.py:176,182-184 — the serial path (`args.workers <= 1`) does `del g` at line 176 then re-parses/loads the graph a second time at lines 182-184 (`g = load_from_file(args.graph_bin)` / `load_from_zim(args.zim)`) right after freeing it. The `del`+reload is only meaningful for the forked-worker path; in serial mode it is a redundant full re-parse of a graph that can be tens of millions of nodes/edges. Consequence: serial corpus generation pays a second full graph load (seconds for large ZIMs) for no benefit. Smallest safe fix: only `del g` when `args.workers > 1`; keep the already-loaded graph in the serial path.
- [low] tests/generate_cell_diverse_corpus.py:191 — `main` calls `_bucket_nodes_by_cell(g, args.cell_scale)` again at line 191, but `pick_cell_diverse_pairs` already ran the same O(num_nodes) cell-bucketing pass at line 74 (`buckets = _bucket_nodes_by_cell(g, cell_scale)`). The second pass re-scans every node + adjacency entry of the whole graph just to report cell counts. Consequence: one redundant full-graph scan per corpus run (millions of nodes). Smallest safe fix: have `pick_cell_diverse_pairs` return/also expose `buckets` (or the per-cell counts) so `main` reuses it instead of recomputing.
- [low] test_terrain_compression.py:139-146 — `get_elevation_for_tile` opens the mosaic rasterio source (`with rasterio.open(mosaic_path) as src:`) on every call, and it is called once per tile inside the per-tile loop (line 366). Each tile pays a file open + reproject setup; with 15 strategies × 3 regions × all zoom-level tiles (~46+ tiles at max zoom), the same mosaic file is opened hundreds of times. Consequence: per-item file-open overhead in the compression benchmark loop (a batch job). Smallest safe fix: open `mosaic_path` once and pass the open `src` (or a cached band handle) into `get_elevation_for_tile`, reusing it across tiles.

## Coverage
- test_satellite_compression.py — clean
- test_terrain_compression.py — findings: 1
- test_zim_perf.py — clean
- tests/_regen_headers.sh — clean
- tests/diff_corpora.py — clean
- tests/functional_search_test.py — clean
- tests/generate_cell_diverse_corpus.py — findings: 1
- tests/generate_golden_corpus.py — findings: 2
- tests/mem_compare.py — clean
- tests/run_identity_suite.sh — clean
- tests/smoke_japan_spatial.py — clean
- tests/szrg_astar.py — clean
- tests/szrg_reader.py — clean
- tests/szrg_spatial.py — findings: 1
- tests/szrg_spatial_astar.py — clean
- tests/test_chip_rules.py — clean
- tests/test_chunked_zim_roundtrip.py — clean
- tests/test_empty_tile_skip.py — clean
- tests/test_graph_chunking.py — clean
- tests/test_hot_chunk_split_native.py — clean
# batch-10 performance review — tests/* (13 files)

All listed files are test/support modules. Per the performance-review skill, test
setup and one-off test logic are not app hot paths; I read every file end to end
and applied the checklist (N+1, O(n^2), allocation churn, per-item I/O, unbounded
caches, main-thread blocking). No defensible performance finding was found in this
batch; the only real cost (build_spatial in-memory accumulation) lives in the
out-of-scope `tests/szrg_spatial.py` and is already mitigated/documented.

Dismissed candidates (with evidence):
- `tests/test_szrg_parser.py:62` and `tests/test_szrg_v5_split.py:51`:
  `name_bytes += nb` string accumulation in a loop — over bounded `names` tuples
  (≤7 entries, e.g. `("", "", "", "", "", "Main St")`), test helpers. Not a hot
  path; dismissed per "String += a handful of times ... only accumulation across
  loop iterations [over unbounded data] counts".
- `tests/test_overture.py:211/420`: per-row `con.execute("INSERT INTO ...")` in
  `_write_parquet` / `_write_places_parquet` — test setup with ≤3 rows per test;
  dismissed per "Queries inside loops in ... test setup — not hot paths".
- `tests/test_spatial_chunking.py:408-410,424`: in-memory `build_spatial` on a
  real region ZIM. The 30-min/20-GB figure in the comment describes the legacy
  Python-list materialization that `tests/szrg_spatial.py:146-148` confirms is
  already fixed (numpy views, no `.tolist()`); the test's 3-GB skip is an
  intentional guard, and the remaining in-memory accumulation cost is in the
  out-of-scope builder. Not reported per CODE-NOT-COMMENTS.
- `tests/test_route_identity.py:80-84,104`: regenerates a 2000-pair golden corpus
  via subprocess when the source ZIM hash changed — intentional integration-test
  behavior, bounded by the skip on unchanged ZIMs.
- `tests/test_v5_end_to_end.py:98-116`, `tests/test_spatial_chunking.py:430-457`:
  replay loops capped at 50/30 pairs with `find_route` (A*) per pair — bounded,
  deliberate test-time caps.
- `tests/test_validator_regression.py:156,347`: 201 MB / 501 MB blob allocations
  are intentional threshold-check fixtures, test-only.
- `tests/test_wikidata_titles.py:39-53`: 120 qids → 3 batched URL calls (50/50/20),
  bounded by the batching contract under test.

## Coverage
tests/test_native_flags.py — clean
tests/test_overture.py — clean
tests/test_route_identity.py — clean
tests/test_routing_worker_v3.py — clean
tests/test_spatial_chunking.py — clean
tests/test_szrg_parser.py — clean
tests/test_szrg_v5_split.py — clean
tests/test_upgrade_spatial_zim.py — clean
tests/test_v5_end_to_end.py — clean
tests/test_validator_regression.py — clean
tests/test_wiki_articles.py — clean
tests/test_wikidata_titles.py — clean
tests/v4_to_v5_convert.py — clean
# Batch 11 — performance review findings

- [low] web/generate.py:590 — loop-invariant `import re as _re` inside the region loop (`for kind, payload in flat_iter:`), executed once per live region — each region iteration pays a module-lookup/import check instead of one hoisted import; negligible alone but it's a textbook hoist miss on a per-region path — hoist `import re` to module top (the module already imports nothing from `re`), then the loop uses the shared cached module.
- [low] web/generate.py:566 — per-region serial HTTP round trip: `details = fetch_item_details(f"streetzim-{region['id']}")` fires one blocking `urlopen` per live region with no concurrency — wall clock is ~N_regions × (network latency + up to 3 retry backoffs), so a regen of ~35 live regions takes tens of seconds dominated by latency, not throughput; archive.org's metadata API is per-item so this can't be batched into one call — wrap the loop's detail fetches in `concurrent.futures.ThreadPoolExecutor` to overlap latencies (bounded fan-out, ~35 tasks).
- [medium] web/drive/sw.js:279 — the range-request path loads the entire ZIM entry before honoring a `Range` header: `serveFromZim` calls `await reader.read(lookupPath)` (which materializes the whole entry via `_readBlob`, decompressing the full cluster in `zim-reader.js`), then `rangeResponse(entry.data, ...)` only slices it — so a client asking for a small byte range of a large binary entry (e.g. MapLibre tile ranges or a probe into a >500 MB routing-graph chunk, the sizes `cloud/manifest_writer.py` documents) pays for a full decompress+hold of the entry in SW memory on first access, and the subarray view keeps the parent buffer alive for the response's lifetime; the reader exposes no range-aware read — add a `readRange(path, start, end)` API to `zim-reader.js` that `_readBlob`-slices before decompressing the full cluster, and call it from `serveFromZim` when a `Range` header is present.
- [medium] cloud/wiki_articles.py:275 — serial per-title network fetch in a batch job: `raw = src.html(title_us) if src else _fetch_online(title_us, cache_dir, user_agent)` issues one blocking Wikipedia API `urlopen` per distinct title (line 203) inside the `for i, title_us in enumerate(norm, 1)` loop, with no concurrency — with the linkable set scaling to tens of thousands of OSM `wikipedia=`/wikidata titles, wall clock is ~N_titles × (latency + 0.1 s sleep), i.e. hours at realistic scale; disk cache only helps rebuilds, not the first crawl — batch page lookups (Wikipedia API supports multiple `titles` per `parse` call) or use a small bounded `ThreadPoolExecutor` while keeping the 429/503 backoff (the sleep is deliberate politeness, so preserve throttling).

## Coverage
web/generate.py — findings: 2
cloud/manifest_writer.py — clean
web/drive/sw.js — findings: 1
cloud/wiki_articles.py — findings: 1
# Batch 12 — performance review findings

- [low] cloud/validate_platforms.py:118 — whole-entry read to check 8 bytes: `head = bytes(e.get_item().content)[:8]` decompresses the ENTIRE `graph-cells-index.bin` into a bytearray just to read the spatial-version header — `Item.content` in libzim materializes/decompresses the full entry, so a large spatial index (the per-cell coordinate table, tens of MB for a continent/world ZIM) is fully decompressed once per validator run for a single int; one-shot CLI so bounded, but the index can be large and this is a pure whole-file-read-where-a-slice-would-do — read only the first cluster bytes (seek 4 bytes at offset 4) or use a range-aware read instead of `content` — smallest safe fix: replace `bytes(e.get_item().content)[:8]` with a read of just the needed header bytes.
- [low] cloud/build-vm-startup.sh:181 — SPOT handoff watcher re-greps the whole growing build log every 30 s: `if grep -q "Building ZIM file" /var/log/streetzim-build.log` inside `while true; do sleep 30` — before the packaging marker appears, each poll re-reads the full log file, which grows unbounded over a multi-hour build (verbose `create_osm_zim.py` with `--keep-temp` plus download progress); grep -q stops at first match so it's cheap after the marker exists, but pre-marker polls pay O(log size) per tick on a watcher that only needs the newest tail — smallest safe fix: check a marker file (touch a sentinel when packaging starts) or use `tail -f`/`tail -n +N`-style incremental read instead of re-grepping the whole log.

## Coverage
cloud/build-vm-startup.sh — findings: 1
cloud/validate_platforms.py — findings: 1
build-and-upload-queue.sh — clean
cloud/launch-build-vm.sh — clean
# batch-13 findings

- [low] overture-rollout-redo.sh:60 — `cp "$src" "$dated"` performs a full read+write copy of a multi-GB ZIM just to create the dated artifact name before upload — for continent-scale builds (Europe/Africa/United States, tens of GB) this doubles disk I/O and adds seconds-to-minutes of copy time per region right before the (already large) upload — `ln "$src" "$dated"` (hardlink; both paths are on the same filesystem under /Users/jasontitus/experiments/streetzim) creates the dated name in O(1) with no data copy.

## Coverage
- web/drive/fzstd.js — clean
- overture-rollout-redo.sh — findings: 1

## Run stats

Engine throughput (weighted across batches): prefill 184661 tok @ 1449 t/s, generated 51852 tok @ 22.4 t/s (13 batches)
