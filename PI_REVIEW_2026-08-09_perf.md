# Pi sweep review (perf focus) — streetzim-94939a0c

Exhaustive per-file pass: 118 code files across 16 batches.

## Findings

# Pi perf review — batch-1

## Summary

Reviewed 20 files, all of which are one-off build-orchestration shell scripts
(build-and-upload-queue.sh, build-california-2026-05-10.sh, build-coasts.sh,
build-region-and-upload.sh, build-small-region.sh, build-ukraine.sh,
build_world_and_us.sh, cloud/build-vm-startup.sh, cloud/build_region.sh,
cloud/chip-retrofit-d.sh, cloud/deploy_pwa.sh) and one-off cloud maintenance
Python tools (cloud/audit_dem_cache.py, cloud/build_torrent.py, cloud/chip_rules.py,
cloud/cleanup_old_zims.py, cloud/decode_check.py, cloud/diff_zim.py,
cloud/fix_stale_terrain_tiles.py, cloud/fix_terrain_seams.py), plus a one-off
smoke test (.smoke_viewer_playwright.py). None of these sit on a request path or
any loop over user-growing data; the I/O-heavy Python tools all parallelize with
ProcessPoolExecutor and their loops are bounded by physical tile/file counts with
O(N) total work. No defensible performance findings.

## Findings

(no findings)

## Coverage
.smoke_viewer_playwright.py — clean
build-and-upload-queue.sh — clean
build-california-2026-05-10.sh — clean
build-coasts.sh — clean
build-region-and-upload.sh — clean
build-small-region.sh — clean
build-ukraine.sh — clean
build_world_and_us.sh — clean
cloud/audit_dem_cache.py — clean
cloud/build-vm-startup.sh — clean
cloud/build_region.sh — clean
cloud/build_torrent.py — clean
cloud/chip-retrofit-d.sh — clean
cloud/chip_rules.py — clean
cloud/cleanup_old_zims.py — clean
cloud/decode_check.py — clean
cloud/deploy_pwa.sh — clean
cloud/diff_zim.py — clean
cloud/fix_stale_terrain_tiles.py — clean
cloud/fix_terrain_seams.py — clean
# Perf sweep — batch-2 (cloud/ tooling)

Reviewed each listed file for performance only, applying performance-review + shell/python
filters. The overwhelming majority of these are one-off / batch CLI orchestration scripts
(build-gates, repackagers, reroll/rollout wrappers, VM launchers, a LAN file server) with
small bounded inputs (region count, tile count, ZIM entry count) — not request paths, so per
the skill's "one-off scripts / CLI admin tools / batch tools are not hot paths" rule the
normal hot-path findings (N+1, per-item HTTP, connection churn) are not applicable here.
The batch tooling that does heavy work (manifest_writer, preflight, repackage_zim, regen_*)
is already heavily optimized (lazy providers, disk spill, streaming, module-level clients,
thread/process pools). Two genuine redundancies found in repackage_zim.py's split/bucket
loops, both low severity because they are single-shot batch work.

## Findings
- [low] cloud/repackage_zim.py:1128-1145 — chip sub-bucketing `while True: n_sub *= 2` loop re-hashes every record into n_sub buckets AND re-serializes the full records set to JSON (`bucket_blobs`) on every doubling iteration — for a 164 MB (Japan) / 137 MB (Canada) chip, n_sub doubles 2→4→…→256, so the entire dataset is re-hashed and re-serialized ~8× just to discover the largest bucket size. — adds minutes of redundant CPU + transient allocation on the biggest regions on a multi-10-minute repackage; scales with records × log(n_sub). — compute the terminating largest-bucket size from per-bucket record counts (or serialize only the largest bucket) and drop the full re-serialization except for the final n_sub; or determine n_sub once from an estimate before serializing.
- [low] cloud/repackage_zim.py:265 — in `_emit_split_search`, after `_split_records_recursive` has already partitioned records into bucket lists, each emitted leaf is re-parsed with `len(json.loads(sub_bytes.decode("utf-8")))` purely to recover its record count. — one full extra JSON parse of the entire hot chunk (up to ~500 MB for CJK prefixes) that adds nothing the recursion didn't already know. — have `_split_records_recursive` return the per-leaf count alongside the bytes so the count is free; at minimum only deserialize when the leaf reveal-a-count is actually needed.

## Coverage
cloud/generate_all_torrents.py — clean
cloud/launch-build-vm.sh — clean
cloud/manifest_writer.py — clean
cloud/preflight.py — clean
cloud/rebuild-all.sh — clean
cloud/rebuild_overture_regions.sh — clean
cloud/regen_all_low_zoom.py — clean
cloud/regen_low_zoom.py — clean
cloud/repackage_zim.py — findings: 2
cloud/reroll-sv-iran.sh — clean
cloud/reroll_viewer.sh — clean
cloud/restart-build.sh — clean
cloud/rollout_viewer_swap.sh — clean
cloud/route_cli.py — clean
cloud/serve_zims.py — clean
cloud/spot-to-ondemand-watcher.sh — clean
cloud/stamp_item_metadata.py — clean
# Batch 3 — performance findings (cloud/*)

## Findings

- [medium] cloud/validate_zim.py:1089 — `_chk_routing` sizes every `routing-data/` entry with `len(bytes(e.get_item().content))`, i.e. it fully decompresses each routing cell/cluster just to compare its size against `MAX_ROUTING_ENTRY_MB`. On a large spatial ZIM with many cells whose content is tens of MB of graph each, this reads+decompresses gigabytes that a pure metadata read (`e.get_item().size`) already provides. Runs once per pre-upload validation, so every rollout pays the decompression of the whole routing graph before upload. Fix: use `e.get_item().size` (or `e.get_item().size`) instead of materializing the content bytes.
- [medium] cloud/validate_zim.py:1020 — `_chk_search_data_sizes` sizes each search chunk with `len(bytes(e.get_item().content))`, fully decompressing every chunk (including oversized 50–200 MB ones whose size is exactly what we want) to compare against the crash thresholds. Use `e.get_item().size` (already exposed by the same entry/item API) so the guard is metadata-only and doesn't allocate each chunk's decompressed buffer. Fix: replace `len(bytes(e.get_item().content))` with `e.get_item().size`.
- [medium] cloud/validate_zim.py:166,256,508,544,744,774,794,910,1082,1222 — the validator performs ~10 independent full scans of the entire entry table (`for i in range(arc.entry_count)`: `_chk_fonts`, `_chk_terrain_edge_stripe`, `_chk_satellite_coverage`, `_chk_vector_coverage`, `_chk_tile_corners`, `_chk_routing`, `_audit_tiles`, plus `_chk_satellite`/`_chk_terrain`). Each re-walks the same 30M+ entry index via `_get_entry_by_id`, so a full validation is O(k·N) entry lookups (and the size probes fire another decompression per matching entry in several of them). For a Canada-scale (35M-entry) ZIM this serializes seconds-to-minutes of redundant scanning on every upload gate. Fix: enumerate the entry table once into a single pass, or at least batch the per-check scans (each check currently can't share the iteration); hoist the size lookup so decompression happens once per entry, not once per check.
- [medium] cloud/validate_overture_urls.py:572 — the mid-crawl checkpoint callback re-serializes and rewrites the *entire* growing cache (`json.dumps(cache, indent=2, sort_keys=True)` + full file write) every `--checkpoint-every` (default 2000) URLs. Over a crawl of U distinct URLs the total bytes written is O(U²/2000): a 500k-URL crawl rewrites a multi-MB JSON hundreds of times, and each write is a full pretty-printed dump of everything accumulated so far. The growing input is the cache itself. Fix: append only the newly-checked delta to a separate incremental log (or write JSON-lines per batch) and fold into the cache once at the end; avoid re-dumping the whole dict per checkpoint.
- [low] cloud/wiki_articles.py:208 — `_fetch_online` opens a fresh `urllib.request.urlopen` connection per article to the same `en.wikipedia.org` host, so every bundled title pays a new TCP/TLS handshake with no keep-alive reuse across the loop in `bundle_wiki_articles`. For thousands of titles this adds a handshake per item on top of the deliberate 0.1 s rate-limit sleep. Fix: reuse a single `http.client.HTTPConnection`/open opener (kept-alive) for the crawl, or a `requests.Session`.

## Coverage
- cloud/swap_viewer_rust.py — clean
- cloud/upgrade_spatial_zim.py — clean
- cloud/upload-caches.sh — clean
- cloud/upload_shipped.sh — clean
- cloud/upload_url_cache.sh — clean
- cloud/upload_validated.sh — clean
- cloud/url_cache_filter.py — clean
- cloud/validate_overture_urls.py — findings: 1
- cloud/validate_platforms.py — clean
- cloud/validate_zim.py — findings: 3
- cloud/verify_terrain_freshness.py — clean
- cloud/vm-health-cron.sh — clean
- cloud/wait-and-launch.sh — clean
- cloud/wiki_articles.py — findings: 1
# Pi performance review — batch-4

Performance-only review of the six listed cloud-orchestration files. These are batch / build / admin
scripts, not request-handling servers, so severity is calibrated to their role (long-running generator,
persistent rsync loop). Findings below.

## Findings
- [medium] cloud_terrain_gen.py:97-105 — DEM source tiles are re-opened and re-downloaded from S3 for every output tile, with no caching — each z12 output tile reads the 1-4 Copernicus DEM tiles it overlaps (`for dem_url in dem_urls: with rasterio.open(dem_url) as src: reproject(...)`); the same ~1° DEM tile is shared by hundreds of overlapping output tiles, so it is fetched over `/vsis3` from scratch for every one of them, across all 64 workers and across adjacent tasks — S3 GET count and wall-clock scale with (output tiles × overlaps) instead of (distinct DEM tiles), multiplying total network I/O several-fold on a 16M-tile world run. Fix: keep a small per-worker LRU cache of recently-read DEM source band arrays (keyed by dem_url, e.g. last 2-4 tiles, ~50MB each) so adjacent tiles reuse the in-memory band instead of re-fetching.
- [low] cloud_terrain_gen.py:74-75,115-116,128-129 — per-tile global `multiprocessing.Lock` around `counter.value += 1` — the progress counter is incremented while holding a cross-process lock for every single one of ~16.7M tiles, and the main process reads `c.value` (lock-free but on the same shared cell) for every imap result; contended lock/unlock ~16.7M times across 64 workers adds serialization overhead and cache-line ping-pong to the hot loop — the tile-processing throughput is a few %-lower than it would be without the shared counter. Fix: drop the shared counter entirely and derive progress from the `imap_unordered` results themselves (count tiles as they are yielded), or have each worker keep a local count and only flush to the shared Value every N tiles.
- [low] cloud_rsync_loop.sh:27 — full-tree `find "${LOCAL_DIR}/12" -name "*.webp" | wc -l` walks all ~16.7M local files on every 20-minute loop iteration just to print a count — each pass re-walks the entire accumulated output tree (minutes of directory traversal) and is recomputed from scratch each cycle even though it exceeds the rsync transfer itself; at full build the loop spends more wall-clock in the count than in syncing. Fix: maintain the count incrementally (tally tiles added per rsync batch, or use a cheap `ls | wc -l` per changed subdir) instead of recounting the whole tree, or print the current target delta only occasionally.

## Coverage
- cloud/wikidata_titles.py — clean
- cloud_rsync_loop.sh — findings: 1
- cloud_terrain_collect.sh — clean
- cloud_terrain_gen.py — findings: 2
- cloud_terrain_launch.sh — clean
- cloud_terrain_monitor.sh — clean
# Batch 5 — performance review

File: create_osm_zim.py (6578 lines, Python build/pipeline tool)

## Findings

- [medium] create_osm_zim.py:4249 — a fresh `ThreadPoolExecutor(max_workers=os.cpu_count())` is created and torn down inside the `while True` tile-ingestion loop, once per 1000-tile batch, instead of hoisting one pool for the whole ZIM pack phase — for region builds with millions of tiles (multi-GB / world builds run to hundreds of millions of z≤14 tiles) this creates/tears down tens-of-thousands of pools, each spawning `os.cpu_count()` threads, adding measurable thread-spawn/teardown churn and pool contention to the dominant packaging pass — hoist a single `ThreadPoolExecutor` above the `while` loop and reuse it for every batch (decompress_tile is stateless).
- [medium] create_osm_zim.py:679 — `_generate_one_terrain_tile` opens the mosaic VRT (`rasterio.open(mosaic_file)`) fresh on every tile task, even though the workers run inside a persistent multiprocessing Pool; for a large region the z0–12 pass generates hundreds of thousands to millions of tiles, so each worker re-opens the VRT (which references many GeoTIFFs) per tile, repeating dataset setup that an `initializer=`-stored per-worker handle would do once — open the mosaic once per worker (multiprocessing `initializer` setting a global handle) and reuse it across all tiles that worker processes.
- [low] create_osm_zim.py:1444 — the reverse_geocoder location `lookup` issues `_rg.search([(lat, lon)], mode=1)` as a separate single-point KNN call per feature; in the streaming search-bucketing path this fires once per feature (millions of features on continent builds), accumulating per-call Python/KD-tree query overhead instead of batching all feature coordinates into a single `_rg.search(points, mode=1)` call.
- [low] create_osm_zim.py:5004 — in the hot-search-chunk splitter, `leaf_count` is computed by re-parsing each just-serialized leaf with `json.loads(sub_bytes.decode("utf-8"))`, undoing the serialize it already did; for multi-hundred-MB hot chunks (e.g. Japan's u5927 at 514 MB) this doubles the parse cost just to get a count — have the splitter return record counts alongside `sub_bytes` instead of re-deserializing.

## Coverage
create_osm_zim.py — findings: 4
# Pi perf review — batch-6

- [medium] resources/viewer/routing-worker.js:710-721 — `snapNearestNode` enumerates **all** cells in the routing index (`for cid = 0; cid < this._index.numCells; cid++`), builds a `candidates` array of every cell, sorts it O(numCells·log numCells), then walks the sorted list awaiting `_ensureCell` (a network fetch) for each. `numCells` grows with region size (dense graphs like United States/Europe have tens of thousands of cells). Every origin/destination snap pays this full-index scan+sort even though the owning cell is computable in O(1) from lat/lon via `_index.cellOf` — and a strictly increasing `bestDist` guard can still cause multiple remote cell fetches per snap on a cold cache. — Compute the target cell with `cellOf(latE7, lonE7)` and check only that cell plus a small (3x3) neighbor ring instead of scanning and sorting the whole cell table.
- [low] resources/viewer/routing-worker.js:958 — Path reconstruction uses `path.unshift(segment)` per edge, which is O(n) per insert, making the reconstruction O(n²) in route segment count. On long routes (thousands of segments) this is wasted array shifting during an already-heavy computation. — Build the path in forward order with `path.push(segment)` and `reverse()` once at the end, or use an index cursor.
- [low] rust/streetzim-pack/src/main.rs:356-365 — The manifest is fully buffered into `records: Vec<Record>` (holding every item's inline `content`/`body_b64` string) before `start_writing`, then iterated twice (line 371 for config, line 386 to write). For the largest regions the manifest has millions of tile items whose base64 bodies are all resident simultaneously, inflating peak memory to multiple GB on top of the streamed decoded bytes — a spike that can OOM where a single pass would not. — Stream records in one pass (apply the config record when it is encountered, and require it to precede items as the docstring already states) instead of buffering the entire file.

## Coverage
create_osm_zim_leaflet.py — clean
download_dem.py — clean
download_overture_data.py — clean
drive-rollout.sh — clean
fix_boundary_chunk.py — clean
fix_boundary_terrain.py — clean
fix_terrain_tiles.py — clean
overture-rollout-redo.sh — clean
rebuild-all-final.sh — clean
rebuild-queue.sh — clean
recompress_avif.py — clean
resources/viewer/routing-worker.js — findings: 2
rust/streetzim-pack/Cargo.toml — clean
rust/streetzim-pack/src/main.rs — findings: 1
scripts/sync-drive-viewer.sh — clean
upload-to-archive.sh — clean
verify_tile_cache.py — clean
web/drive/build-info.js — clean
web/drive/fzstd.js — clean
web/drive/sw.js — clean
# Pi sweep perf — batch 7

Reviewed for PERFORMANCE ONLY (performance-review checklist applied). No security/style findings.

## Findings

(none)

The single listed file, `web/drive/viewer/maplibre-gl.js`, is a vendored, minified copy of the
upstream third-party bundler output for MapLibre GL JS v5.23.0, committed once in history as a
static dependency. The full library body sits on one minified line (line 42, ~458KB). It is not
project-maintained code the team edits, and the minified form is the unmodified upstream bundle;
there is no project-specific, defensible performance defect I can exhibit in the actual code.
Any perf characteristic of the library itself lives in upstream MapLibre and is not fixable here.

## Coverage

web/drive/viewer/maplibre-gl.js — clean
# Pi perf review — batch-8

## Findings

- [medium] web/drive/viewer/routing-worker.js:612 — O(n) LRU touch on every A* node expansion — `_touch` does `this._lru.indexOf(cid)` + `splice` on an array that can hold thousands of resident cells (budgeted up to 64 MB). It is called from `_ensureCell` on every cache hit, and the A* hot loop (up to 200k pops, or 400k in the greedy pass) calls `_ensureCell`→`_touch` for almost every popped node, so total work is O(pops × residentCells) — hundreds of millions of array-scan/splice steps on a cold-ish long route. — Replace the array-based LRU with an insertion-ordered `Map` (delete+set on touch, first key = LRU) so touch is O(1).
- [medium] web/drive/viewer/routing-worker.js:958 — O(n²) path reconstruction via `path.unshift(segment)` — each unshift shifts every already-inserted segment, so rebuilding an N-segment route is O(N²) array moves. Long routes (thousands of segments) pay a quadratic cost on top of the A* search. — Build with `.push()` and `.reverse()` at the end, or index in reverse order.
- [medium] web/drive/viewer/routing-worker.js:710 — full-cell-index scan on every snap request — `snapNearestNode` iterates every cell in `this._index.numCells` building a candidate array with distance, then sorts it, for each point snapped (every map click to set origin/destination/search). For continent-scale regions with tens of thousands of cells this is O(numCells log numCells) per interaction. — Restrict to cells within a bounding box around the latE7/lonE7 point (only push cells whose cell bounds are within a few km), then expand only if bestDist is still unreached (heavy hitters rarely win).
- [low] web/drive/viewer/routing-worker.js:998 — `queue.shift()` array dequeue in BFS — `findNearestHighwayNode` drains its frontier with `array.shift()`, which is O(frontier length) per pop, turning the BFS into O(n²) on the frontier. Bounded by `maxPops` (5000) and only on the two-pass long-route path, so cold/warm — switch to an index cursor or `deque` if the frontier exceeds a few hundred.

## Coverage
web/drive/viewer/routing-worker.js — findings: 4
web/drive/zim-reader.js — clean
web/generate.py — clean
web/stats.py — clean
web/watch-and-deploy.sh — clean
wikidata_cache.py — clean
# Pi sweep — perf — batch-9

Findings for the 20 listed files (all test/benchmark scripts and the spatial
routing test-harness). Ran the mandatory inventories against the batch:
loop-dense files, cache/registry sites, per-item client construction, and
route/handler hot paths; every candidate was read in full. The batch is
entirely test tooling — most files are bounded benchmark drivers (fixed
sample areas, fixed strategy lists, pair counts of 2000–5000) whose
loops are the deliberate workload, so the only findings kept are genuine
O(n) scans over data that grows with ZIM/corpus size that would slow down
the tests themselves on large inputs.

- [low] tests/functional_search_test.py:132-146 — geocode probe fully reads each target sub-chunk JSON and does a per-record substring scan `if q.lower() in name.lower()` — reading and JSON-parsing entire possibly-large sub-chunk blobs (the module/sibling code documents buckets up to 349 MB, e.g. the `__` CJK bucket) once per probe (5 probes) and scanning all records of each sub until `hits>20`; on a large hot-split ZIM this loads and parses hundreds of MB of JSON per probe and does O(records) lowercase-substring work. Impact: the integrity test takes many seconds and spikes memory on real Japan-scale ZIMs. Smallest safe fix: stop probing after the first sub-chunk that yields a hit (early-return `first`), and/or check the suffix/index rather than scanning every record for a substring match; at minimum reuse one loaded sub-chunk across probes instead of re-reading per probe.
- [low] tests/smoke_japan_spatial.py:230-245,269-282,286-299 — probe_tiles/probe_satellite/probe_terrain each do a full `for i in range(arc.entry_count): arc._get_entry_by_id(i)` scan of every archive entry to find a sample tile path, and each of the three probes repeats that full O(entry_count) pass. On a large ZIM (millions of entries) this materializes every entry object 3× just to find one sample path, adding seconds-to-minutes to an otherwise quick smoke test; cost grows linearly with entry count. Impact: smoke test latency scales with archive size. Smallest safe fix: do a single pass that collects one `tiles/14/`, one `satellite/`, and one `terrain/` sample simultaneously, or use a direct path/ZXID lookup instead of a brute-force linear scan.
- [low] tests/szrg_spatial.py:454-475 — nearest_node builds a candidates list by iterating every cell in the index (`for cid in range(self._index.num_cells)`) and then, for each candidate whose lower bound beats the best, iterates every node in that cell — O(num_cells) per call just to seed the scan, growing with region size (a continent at 0.1° scale is tens of thousands of cells). Every geocode/nearest lookup pays a full-cell-index sweep. Impact: each nearest-node query is O(cells) instead of O(plausible cells); slow on large-region ZIMs. Smallest safe fix: only consider cells whose (dlat²+dlon²) bound beats the running best before expanding them (iterate cells near the query first, e.g. via the existing `cell_id_by_key` or a small spatial fan-out), rather than scanning the entire cell table per call.

## Coverage
test_satellite_compression.py — clean (bounded benchmark: 24 fixed tiles × fixed strategy list; downloads/compressions are the deliberate workload)
test_terrain_compression.py — clean (bounded benchmark over fixed regions/zooms/strategies; per-tile rasterio open + encode is the measured workload; avifenc subprocess is deliberate)
test_zim_perf.py — clean (benchmark by design; 500k tile generation and ZIM insert is the measured subject)
tests/_regen_headers.sh — clean (shell driver, 5 fixed regions, no hot loop)
tests/diff_corpora.py — clean (bounded corpus diff; corpus size ~2000–5000 records)
tests/functional_search_test.py — findings: 1
tests/generate_cell_diverse_corpus.py — clean (one O(nodes) cell-bucket pass; pair-picking loops capped at fixed quota/tries; not per-item hot work)
tests/generate_golden_corpus.py — clean (progress-tick re-scan of results is O(n²/progress_every) but n≈2–5k → ~27k ops, negligible; A* is the deliberate workload)
tests/mem_compare.py — clean (benchmark by design; pair count default 3)
tests/run_identity_suite.sh — clean (shell driver over 5 fixed regions)
tests/smoke_japan_spatial.py — findings: 1
tests/szrg_astar.py — clean (already hand-optimized hot A*; list views hoisted, math aliases, set-free membership; operating on explicit edges is the test's purpose)
tests/szrg_reader.py — clean (one-shot parse; chunk reassembly loops over manifest chunk count, bounded)
tests/szrg_spatial.py — findings: 1
tests/szrg_spatial_astar.py — clean (mirrors szrg_astar; per-node edge gather via edges_of_node triggers lazy cell load — intended)
tests/test_chip_rules.py — clean (tiny synthetic corpora, no unbounded loops)
tests/test_chunked_zim_roundtrip.py — clean (tiny synthetic graphs, chunk sizes bounded)
tests/test_empty_tile_skip.py — clean (tiny synthetic streams)
tests/test_graph_chunking.py — clean (tiny fixtures + one real-graph roundtrip, bounded)
tests/test_hot_chunk_split_native.py — clean (16k synthetic names, bounded)

Note: ruff/staticcheck/eslint/madge not run (not confirmed installed); the checklist greps + full-file reads were the fallback and all mandatory inventories were completed.
# Pi perf sweep — batch-10

All 13 files in this batch are test files (unit/pytest regression suites, an in-memory
v4→v5 converter test helper). Each exercises small, bounded synthetic fixtures or
deliberate oversized inputs used to trigger validator thresholds. None sit on a
production hot path; the loops present iterate over bounded test data, and the large
memory allocations (201 MB/501 MB blobs in test_validator_regression.py, whole-graph
load in test_v5_end_to_end.py / v4_to_v5_convert.py) are intentional test-input or
test-setup behavior, not unbounded application logic. No defensible performance
findings to report.

## Coverage
- tests/test_native_flags.py — clean
- tests/test_overture.py — clean
- tests/test_route_identity.py — clean
- tests/test_routing_worker_v3.py — clean
- tests/test_spatial_chunking.py — clean
- tests/test_szrg_parser.py — clean
- tests/test_szrg_v5_split.py — clean
- tests/test_upgrade_spatial_zim.py — clean
- tests/test_v5_end_to_end.py — clean
- tests/test_validator_regression.py — clean
- tests/test_wiki_articles.py — clean
- tests/test_wikidata_titles.py — clean
- tests/v4_to_v5_convert.py — clean
# Pi perf review — batch-11

## Summary
Reviewed web/drive/viewer/maplibre-gl.js for performance-only findings. This file is a 1,054,454-byte vendored build artifact of the upstream open-source MapLibre GL JS v5.23.0 library (single UMD wrapper + minified bundle with very long lines up to 63,886 chars). It contains no project-authored logic — only the upstream license header and the generated minified bundle. There are no in-repo performance defects to fix; patching a vendored upstream build bundle is not a maintainable remedy, and no hot-path code belonging to this project lives here.

## Findings
(none)

## Coverage
web/drive/viewer/maplibre-gl.js — clean
# Pi perf review — batch-12

## Findings

- [medium] create_osm_zim_leaflet.py:269 — `extract_tiles_from_mbtiles` loads every tile's raw bytes into an in-memory dict `tiles[(z,x,y)]` and only releases them after `render_all_tiles` finishes — for a country/continent bbox at z14 (e.g. "virginia" or a whole continent, hundreds of thousands to millions of tiles) the working set is the entire MBTiles tile payload (multi-GB), blowing the process RAM instead of streaming one tile at a time — iterate the sqlite cursor row-by-row and decode/render/discard each tile as it is read, writing the PNG and dropping the bytes within the same pass.
- [medium] create_osm_zim_leaflet.py:193 — `download_satellite_tiles` calls `urllib.request.urlopen` once per tile in the nested z/x/y loop with no connection reuse, so every tile pays a fresh TCP+TLS handshake against `tiles.maps.eox.at`; at z14 a single area is tens of thousands of tiles → tens of minutes of pure handshake latency plus server connection churn — open one pooled client (`http.client.HTTPSConnection` reused across requests, or `urllib3`/`requests.Session`) once before the loop and reuse it for all tiles.
- [low] create_osm_zim_leaflet.py:522 — `_draw_label` checks each new label against every label in `labels_drawn` (`for existing in labels_drawn`), an O(n²) overlap scan per rendered tile that dominates in dense city labels at high zoom — spatial check only against a coarse grid (bucket labels by tile, compare within the neighborhood) or cap the scan once a label-density bound is hit.
- [low] cloud/upgrade_spatial_zim.py:565 — after `_split_records_recursive` serializes each leaf, the code re-parses every leaf with `len(json.loads(sub_bytes.decode(...)))` just to count records, doubling the deserialize cost of the multi-MB hot search chunks — have `_split_records_recursive` return record counts alongside bytes (it already has the list) instead of re-parsing.
- [medium] wikidata_cache.py:636 — `save_cache` rewrites the *entire* cache on every incremental call: each bucket that exists is read, merged, and rewritten; `fetch_wikidata_batch` invokes it every `save_interval` (10000) Q-IDs, so total work is O(num_saves × total_entries) and grows quadratically as the cache accumulates across a long run — only read-merge-write the buckets that actually gained new entries on incremental saves, and do the full merge once at the end.
- [low] wikidata_cache.py:213 — `extract_qids_from_mbtiles` does `.fetchall()` on all z14 tile blobs, holding every tile's compressed + decompressed data and decoded feature dicts in RAM simultaneously; for a large region at z14 this is a large memory spike — stream the sqlite cursor and decode/lookup one tile at a time, discarding each before the next.

## Coverage
- create_osm_zim_leaflet.py — findings: 3
- cloud/upgrade_spatial_zim.py — findings: 1
- wikidata_cache.py — findings: 2
- web/generate.py — clean
# Batch 13 — performance review

## Findings

- [low] cloud/verify_terrain_freshness.py:315 — all-region tile jobs accumulated
  into one in-memory list + one global `seen` tuple-set *before* any checking
  starts, and `pool.map` queues the whole list onto the pool task queue. With
  `--region all --zooms 0-12` the iteration covers (after dedup) roughly the
  planet at z12 (~10^7 tiles); each job is an 8-element tuple (~150-200 B) and
  each `(z,x,y)` key another tuple+set node, so peak RSS is multi-GB of
  cold-start memory that yields no work until every tile is enumerated — an
  OOM/memory-spike risk on a modest build machine during the pre-build gate.
  Fix: stream the generator through `pool.imap_unordered(_check_tile, ...,
  chunksize=256)` so jobs are created lazily, or process one region/zoom at a
  time, instead of hydrating the full `jobs`+`seen` structures up front.
- [low] cloud/preflight.py:337 — same full-materialization pattern inside
  `check_terrain_cache`: a large region at z0-12 (`europe`, `africa`) yields
  millions of 8-tuples plus a tuple-key `seen` set, and `pool.map`
  materializes both the jobs list and the results before the first check
  runs. Consequence: multi-GB peak RSS at the cold-start of the prebuild gate
  for big regions / `--region all`. Fix: consume `mercantile.tiles` lazily via
  `pool.imap_unordered` with a chunksize, avoiding the giant upfront list.
- [low] cloud/route_cli.py:71-113 — brute-force `nearest_node` /
  `nearest_node_filtered` recompute a full O(N) haversine over all ~20M nodes
  (allocating ~200 MB of float64/radians/sin/arcsin arrays and, for the
  highway path, a full `np.argsort`) on *every* call, and nothing reuses the
  result. In a single `hwy2` run `main()` calls `nearest_node` twice and
  `find_route_two_pass` calls `nearest_node_filtered(highway_only=True)`
  twice, i.e. ~4 redundant full-graph passes (each "a few seconds") plus
  repeated multi-hundred-MB transient allocations *before* routing even
  starts — and the tool's whole purpose is to time routing, so this setup
  overhead distorts the measurement and adds ~10s+ per query at 20M nodes.
  Fix: compute the float64 distance array once per graph-load and reuse it for
  both endpoints (and pass the `d` array into the filtered variant), or
  restrict the scan to the nearest spatial cells the graph already indexes.

## Coverage
- cloud/preflight.py — findings: 1
- cloud/route_cli.py — findings: 1
- cloud/manifest_writer.py — clean
- cloud/verify_terrain_freshness.py — findings: 1
# Pi sweep perf review — batch-14

## Findings

- [medium] verify_tile_cache.py:174 — `dem_cache` dict grows unboundedly, holding one open rasterio/GDAL dataset handle per DEM cell touched, and it is kept alive for the entire zoom-level scan (no eviction, no close). In `--accurate` terrain mode over a large bbox (e.g. africa/europe) or at higher zoom, a tile overlaps several 1° cells, so this can open hundreds-to-thousands of DEM `.tif` handles per worker, multiplied across the multiprocessing pool → file-descriptor exhaustion and memory pressure that slows/aborts the verification. — While the handle cache is the intended way to avoid re-opening per tile, it never bounds retained datasets; each retained handle holds file FD + metadata. Open can grow past ulimit and leave GDAL buffers resident. — Replace the per-worker dict with a bounded/evicting cache (e.g. close handles once the active cell set moves on, or an LRU keyed by `dem_path`) so only the cells overlapping the current tile set stay resident; call `ds.close()` on eviction.

- [low] cloud/validate_platforms.py:118 — `head = bytes(e.get_item().content)[:8]` materializes the *entire* `graph-cells-index.bin` item into memory just to read an 8-byte version header. For large regions the index is tens of MB, so `_scan_routing` allocates the full buffer (some held on the heap during the rest of the scan) for no reason. — Reads the whole index once per run just to grab a u32 offset; on a big ZIM this is a wasted multi-MB allocation and read on the (already O(entry_count)) scan path. — Read only the header bytes via a ranged access (e.g. `item.read(offset=0, length=8)` if libzim supports it, else parse the version from the first small chunk) instead of `bytes(item.content)`.

- [low] cloud/build-vm-startup.sh:153-155 — The periodic progress-save loop runs a full `gcloud storage rsync --recursive` of the entire `satellite_cache_avif_256/` and `terrain_cache/` trees (hundreds of GB, millions of tiles) every 15 minutes for the whole build. Each cycle re-lists and diffs the complete source + destination object sets even though only a handful of newly-downloaded tiles changed, and this runs on a spot-preemptible handoff plus the EXIT trap plus the explicit `push_caches` — several redundant full-tree comparisons. — Every 15 min a multi-hundred-GB tree is fully listed/compared against GCS; over a multi-hour build this repeats dozens of times, adding sustained GCS LIST/bandwidth and CPU on the build VM that slows the actual download/build work. — Reduce cadence (e.g. every 60 min) and/or restrict the periodic push to a delta — only rsync the subdirectories/z-levels currently being written rather than the whole cache each cycle.

## Coverage
web/drive/sw.js — clean
verify_tile_cache.py — findings: 1
cloud/build-vm-startup.sh — findings: 1
cloud/validate_platforms.py — findings: 1
# Batch 15 — performance review

- [medium] web/drive/zim-reader.js:171-187 (findEntry binary search) — Every binary-search step issues two separate async Blob reads (`_readUrlPointer(mid)` = one 8-byte `_readRange`, then `_readDirEntryAt(off)` = another `_readRange`). On a cache miss the lookup performs ~2·log2(articleCount) individual `File.slice().arrayBuffer()` I/O operations instead of reading the contiguous URL-pointer block once. — For a large ZIM (millions of articles, log2 ≈ 20+ steps) each uncached lookup (each distinct tile/article path) incurs ~40+ separate slice reads; over the many distinct tile lookups a map render performs this makes the reader latency-bound on the blob device, and in a service-worker/IndexedDB-backed blob each slice read is an asynchronous round-trip. — Read the whole `urlPtrPos..urlPtrPos+articleCount*8` block once into a cached `Uint8Array`/`DataView` and binary search over it in-memory (single bulk read per open, or lazily cached on first lookup), leaving only the DirEntry fetch as per-step I/O.

## Coverage
cloud/wikidata_titles.py — clean
web/drive/zim-reader.js — findings: 1
cloud/swap_viewer_rust.py — clean
build-and-upload-queue.sh — clean
# Pi sweep perf — batch-16

No defensible performance findings.

- cloud/launch-build-vm.sh: one-off GCP provisioning script; single pass over a fixed 10-zone array, one `gcloud` create per zone, no unbounded collections, no hot path.
- cloud/fix_stale_terrain_tiles.py: offline batch maintenance job; `seen` set de-duplicates tiles across regions, `ProcessPoolExecutor` with chunksize, `tile_in_bbox` is O(1) math, PIL/numpy imports are worker-cached. No N+1, no O(n²), appropriate parallelism.
- web/drive/fzstd.js: vendored optimized zstd decompressor; `decompress` (B) accumulates chunks in an array and `S` allocates the destination once then copies each chunk — amortized O(n), not O(n²). No spread/concat accumulator in a linear path.
- overture-rollout-redo.sh: build orchestration that deliberately parallelizes waves with `&`/`wait`; `wait_for_parquet` polls with `sleep 30` backoff. No hot-path or unbounded-growth issue.

## Coverage
cloud/launch-build-vm.sh — clean
cloud/fix_stale_terrain_tiles.py — clean
web/drive/fzstd.js — clean
overture-rollout-redo.sh — clean

## Run stats

input 1004710 tok (+4275456 cached), output 94441 tok, cost $0.18 — 139 files in 7m (1092.6 files/h, 0.5 min/batch)
