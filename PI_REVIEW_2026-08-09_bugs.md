# Pi sweep review — streetzim-94939a0c

Exhaustive per-file pass: 118 code files across 16 batches.

## Findings

# Pi sweep review — batch-1

## Findings

- [medium] build-region-and-upload.sh:64 — `... "$RAW" | read HAS_GRAPH_BIN HAS_POI` runs `read` in a pipeline subshell, so the assignments never reach the parent shell. Both vars stay at their `=0` initializers regardless of the actual ZIM contents, so `if [ "$HAS_GRAPH_BIN" = "1" ]` (line 70) and `if [ "$HAS_POI" = "1" ]` (line 81) are ALWAYS false. — Monolithic `routing-data/graph.bin` sources are never converted with `--spatial-chunk-scale 10` (the exact iOS-WebView routing failure this step exists to prevent), and `--split-find-chips` is never passed even when `poi.json` is present, so fat chips that OOM the Find page on large ZIMs are left unsplit. — Bind `read` to the current shell with process substitution: `read HAS_GRAPH_BIN HAS_POI < <(./venv312/bin/python3 -c "..." "$RAW")`.
- [low] build-and-upload-queue.sh:86,99,112,125,138,151 — `set -e` without `pipefail` masks build failures: `create_osm_zim.py ... 2>&1 | tee <log>` returns tee's status (0) even if the build dies. — If a stale dated `.zim` exists from an earlier run, `upload_zim` sees it via `[ -f ]` and uploads an old/broken ZIM as if it were the new build; if no file exists, `return 1` under `set -e` aborts the remainder of the 6-region queue with no clear cause. — Add `set -o pipefail` (and/or gate the upload on the ZIM mtime being newer than the build start).
- [low] build-coasts.sh:23,49 — same `set -e`-without-`pipefail` + `| tee` masking of `create_osm_zim.py` failures. — A silent build failure followed by `ia upload ... osm-west-coast-us.zim` / `osm-east-coast-us.zim` can ship a stale ZIM from a previous run, then `web/generate.py --deploy` publishes it as current. — Add `set -o pipefail`, or check/clear the output ZIM before each build.

## Coverage
.smoke_viewer_playwright.py — clean
build-and-upload-queue.sh — findings: 1
build-california-2026-05-10.sh — clean
build-coasts.sh — findings: 1
build-region-and-upload.sh — findings: 1
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
# Pi sweep — batch-2 (cloud/ dev + build tooling)

## Findings

- [low] cloud/serve_zims.py:126-128 (translate_path) & 139-146 (do_GET) — path
  traversal: `translate_path` joins `ROOT / path` with no `..` normalization and
  `do_GET` only checks that the (unquoted, lstrip'd) request path *starts with*
  `osm-` and *ends with* `.zim` before delegating to `SimpleHTTPRequestHandler`.
  A request like `/osm-x.zim%2f..%2f..%2f..%2fetc%2fsome.zim` survives the
  prefix/suffix guards (starts with `osm-`, ends with `.zim`), and after
  unquoting the `..` segments climb out of ROOT, so on this 0.0.0.0-bound,
  unauthenticated LAN server an arbitrary file ending in `.zim` outside the repo
  can be read/downloaded. — Restrict the served path to the repo root: resolve
  `(ROOT / path).resolve()` and bail (404) if the resolved path is not under
  `ROOT` (and RFC 3986-range it by stripping `..` before the `osm-`/`.zim`
  check).

## Coverage
cloud/generate_all_torrents.py — clean
cloud/launch-build-vm.sh — clean
cloud/manifest_writer.py — clean
cloud/preflight.py — clean
cloud/rebuild-all.sh — clean
cloud/rebuild_overture_regions.sh — clean
cloud/regen_all_low_zoom.py — clean
cloud/regen_low_zoom.py — clean
cloud/repackage_zim.py — clean
cloud/reroll-sv-iran.sh — clean
cloud/reroll_viewer.sh — clean
cloud/restart-build.sh — clean
cloud/rollout_viewer_swap.sh — clean
cloud/route_cli.py — clean
cloud/serve_zims.py — findings: 1
cloud/spot-to-ondemand-watcher.sh — clean
cloud/stamp_item_metadata.py — clean
# Pi sweep — batch-3

## Findings

- [high] cloud/vm-health-cron.sh:43 — `pgrep -f "create_osm_zim\|apt-get\|pip install\|git clone\|gcloud storage\|tar "` uses `\|` for alternation, but `pgrep -f` treats its pattern as an ERE where `\|` is a literal pipe (bare `|` is the alternation operator). No real build process command line contains a literal `|`, so pgrep never matches; BUILD_ALIVE is always "NO", so every RUNNING VM is deemed "build DEAD" and is deleted + relaunched on each 5-minute cron tick (destroying live builds). Fix: use a single ERE with `|` alternation (e.g. `pgrep -f '([c]reate_osm_zim|apt-get|pip install|git clone|gcloud storage|tar )'`), or match a single unique token that cannot self-match the `pgrep`/`gcloud ssh` command line.

- [medium] cloud/wiki_articles.py:215-227 — `except HTTPError: if e.code in (429,503) and attempt<3: retry; else cacheable_miss=True; break` treats any status other than 429/503 as a permanent miss. A transient 5xx (500/502/504) therefore falls into the `cacheable_miss` branch and writes an empty file as a known-miss marker, so the article is never re-fetched on subsequent builds and is silently lost from the bundle. Fix: only treat real 4xx (and cache 404 specifically) as a permanent miss; retry on all 5xx and skip caching on transient server errors.

- [low] cloud/validate_overture_urls.py:400 — `TCPConnector(ssl=False)` disables TLS certificate verification for every URL crawled. The results are written into `url_validation_cache.json`, which the build consumes to drop dead `ws` links from POI records; an on-path attacker (or a parking host) can therefore poison liveness for an arbitrary POI domain (marking a real business dead → dropped from the map, or marking dead alive). It is a deliberate tradeoff, but the cache has no integrity/provenance check. Fix: validate certs (or at least pin/record the verified chain) and only fall back to report alive=False on ssl error, preserving verifiability of the verdict.

## Coverage
cloud/swap_viewer_rust.py — clean
cloud/upgrade_spatial_zim.py — clean
cloud/upload-caches.sh — clean
cloud/upload_shipped.sh — clean
cloud/upload_url_cache.sh — clean
cloud/upload_validated.sh — clean
cloud/url_cache_filter.py — clean
cloud/validate_overture_urls.py — findings: 1
cloud/validate_platforms.py — clean
cloud/validate_zim.py — clean
cloud/verify_terrain_freshness.py — clean
cloud/vm-health-cron.sh — findings: 1
cloud/wait-and-launch.sh — clean
cloud/wiki_articles.py — findings: 1
# Pi sweep — batch-4

## Summary
Reviewed 6 files: one Wikidata Q-ID resolver module, one standalone terrain tile generator, and four bash orchestration scripts (rsync loop, collect, launch, monitor). The Python is largely sound (retries with backoff, atomic cache writes via `os.replace`, validated bounds). The material issues are in the shell orchestration layer: cloud_terrain_collect.sh runs under `set -e` yet holds an unguarded `completed=$(ssh ...)` whose non-zero exit on a transient connection failure aborts the entire collection run after already terminating prior instances, and cloud_terrain_monitor.sh terminates instances after only rsync's exit code with no local tile verification (unlike collect.sh's documented spot-check safeguard), risking permanent data loss on a stale rsync. Dead unused provisioning code in the launch script is a minor maintainability issue.

## Findings
- [medium] cloud_terrain_collect.sh:9,42 — the script runs under `set -e`, and line 42 is `completed=$(ssh $SSH_OPTS ubuntu@$ip 'cat terrain_tiles/COMPLETED 2>/dev/null || echo NOT_DONE' 2>/dev/null)` with no `||` guard on the ssh command substitution: if the ssh connection fails to come up (ConnectTimeout=30, 2>/dev/null swallows the error) the substitution returns non-zero and `set -e` aborts the whole script mid-run — consequence: one transiently-unreachable instance stops collection for every not-yet-processed healthy instance, so tiles from the remaining instances are neither synced nor their instances terminated (run silently stops short); if invoked with `terminate`, instances already processed are gone and the rest leak — smallest safe fix: make the check non-fatal and self-consistent, e.g. `completed=$(ssh ... 2>/dev/null || echo NOT_DONE)` (add `|| echo NOT_DONE` on the *local* side) or drop `set -e` for that call, so a hiccup is treated as still-running and `continue`s rather than aborting the loop.
- [medium] cloud_terrain_monitor.sh:37-42 — after `rsync -az ... 2>/dev/null` the instance is immediately `aws ec2 terminate-instances`'d on a bare `$? -eq 0` check with no verification that the tiles actually made it to LOCAL_DIR, and rsync's own error output is discarded with `2>/dev/null` — consequence: a partially-stale or failed rsync (e.g. transient network drop or the instance finishing writing tiles after the one-shot sync) reports exit 0, the one-time spot instance is terminated, and its generated tiles are permanently lost, which is exactly the data-loss outcome cloud_terrain_collect.sh was written to guard against with its "spot-check 5 random tiles / keep alive on failure" logic — smallest safe fix: before terminating, spot-verify a sample of remote tiles exist locally (as collect.sh does) and only terminate on success; also drop `2>/dev/null` so real rsync errors are visible.
- [low] cloud_terrain_launch.sh:59-62 — `SETUP_SCRIPT` is defined (a `yum install ...` string for Amazon-Linux-style setup) but never referenced anywhere; all real provisioning is done inline in the `USERDATA` heredoc using `apt-get` for the Ubuntu 24.04 AMI — consequence: dead, misleading code that would break if someone wired it into the launch (yum doesn't exist on Ubuntu), and its presence suggests an unused provisioning path — smallest safe fix: delete the `SETUP_SCRIPT` variable block entirely.

## Coverage
- cloud/wikidata_titles.py — clean
- cloud_rsync_loop.sh — clean
- cloud_terrain_collect.sh — findings: 1
- cloud_terrain_gen.py — clean
- cloud_terrain_launch.sh — findings: 1
- cloud_terrain_monitor.sh — findings: 1
# Sweep batch 5 — create_osm_zim.py

## Findings

- [low] create_osm_zim.py:2233, create_osm_zim.py:2429 — the Overture parquet path is interpolated directly into a DuckDB SQL string (`FROM read_parquet('{overture_parquet}')`) without any escaping or parameterization. A parquet file path containing a single quote (legal on many filesystems) raises a SQL syntax error and aborts the build; a hostile path string would be executed as SQL. Since the path is operator-supplied this is robustness + latent injection, not remote RCE. Smallest safe fix: pass via a SQL parameter / `make_copy` or escape with `overture_parquet.replace("'", "''")` before interpolation.
- [low] create_osm_zim.py:1460 — the `build_location_index` MVT fallback (used whenever `reverse_geocoder` is not installed) does `conn.execute(...zoom_level in 0..8).fetchall()` for every low-zoom level and decodes every tile, materializing all low-zoom tile blobs in memory at once. This path is invoked for large/streaming (>5 GB MBTiles) builds precisely to avoid holding tiles in memory, so on a world/continent build it can OOM or spend minutes decoding tiles that contribute only a handful of place labels. Smallest safe fix: stream the rows per zoom (iterate the cursor instead of `.fetchall()`) and only decode the `place` layer.

## Coverage
create_osm_zim.py — findings: 2
# Pi batch review — batch-6

Findings for the listed files. Anchored to this batch, cross-file reachability considered.

## Findings

- [low] create_osm_zim_leaflet.py:765-771 (and same pattern in water/buildings landcover loops) — polygon rendering fills only the outer ring (`ring = projected[0]`) and ignores all inner rings (holes). MultiPolygon/Polygon coordinates from mapbox_vector_tile carry holes as subsequent rings, so a lake with an island / a building with an atrium is painted as a solid filled shape: the island gets the surrounding fill color and the hole is not punched out. Concrete consequence: visibly wrong raster maps (e.g. islands incorrectly rendered as water, building courtyards filled in). Fix: draw the outer ring then over-draw each inner ring with the layer's underlying color (or use ImageDraw polygon with holes via a background pass) rather than using only `projected[0]`.

- [medium] download_dem.py:113-121 — tiles are streamed straight to the final path `fpath` with no temp-file + atomic rename and no validity check; the cache guard only requires `os.path.getsize(fpath) > 1000`. A failed/interrupted download that leaves a partial file larger than 1000 bytes is treated as a valid cached DEM on the next run and is silently reused by `fix_boundary_*` / terrain generation. Concrete consequence: corrupt elevation data baked into terrain tiles with no error. Fix: write to `{fpath}.part` and `os.replace()` only on a completed full read, or verify the header is a valid GeoTIFF before treating it as cached.

- [nit] create_osm_zim_leaflet.py:731 — `creator.add_metadata("Date", "2026-03-10")` hardcodes the ZIM date; the stamp will be wrong for any run on a different day. Concrete consequence: incorrect metadata date on produced ZIMs. Fix: use the run date (e.g. `datetime.date.today().isoformat()`).

- [low] recompress_avif.py:164-165 — after the pool loop, the final summary computes `f"{total/elapsed:.0f}"` where `elapsed = time.time() - start`; if the scan found zero JPEG files (empty source dir) `total` is 0 and a near-zero `elapsed` causes a `ZeroDivisionError`, so the tool crashes instead of reporting "nothing to do". Concrete consequence: crash on empty source cache. Fix: guard `elapsed`/`total` (report 0 and return when `total == 0`).

## Coverage

- create_osm_zim_leaflet.py — findings: 3
- download_dem.py — findings: 1
- download_overture_data.py — clean
- drive-rollout.sh — clean
- fix_boundary_chunk.py — clean
- fix_boundary_terrain.py — clean
- fix_terrain_tiles.py — clean
- overture-rollout-redo.sh — clean
- rebuild-all-final.sh — clean
- rebuild-queue.sh — clean
- recompress_avif.py — findings: 1
- resources/viewer/routing-worker.js — clean
- rust/streetzim-pack/Cargo.toml — clean
- rust/streetzim-pack/src/main.rs — clean
- scripts/sync-drive-viewer.sh — clean
- upload-to-archive.sh — clean
- verify_tile_cache.py — clean
- web/drive/build-info.js — clean
- web/drive/fzstd.js — clean
- web/drive/sw.js — clean
# Pi sweep — batch-7

Findings from reviewing the listed files.

## Findings

(no findings)

## Coverage
web/drive/viewer/maplibre-gl.js — clean
# Pi sweep — batch 8

## Findings

- [low] web/drive/viewer/routing-worker.js:262 — `haversine` computes `Math.sqrt(1 - a)` with no clamp; for near-antipodal points rounding can push `a` slightly above 1, making `1 - a` negative and the distance NaN. That NaN propagates into the A* heuristic `h` (and the crow-distance gating in `findRoute`), so routes spanning near-global opposites can yield NaN costs/heuristics and degrade or break routing. Smallest safe fix: clamp `a = Math.min(1, a)` before the square root.

- [low] web/drive/zim-reader.js:159 — `findEntry`'s binary search issues one separate `file.slice().arrayBuffer()` (via `_readUrlPointer`/`_readDirEntryAt`) per pivot, i.e. O(log N) random Blob boundary crossings for every content-path lookup. On large ZIMs (millions of entries) and pages that trigger many lookups this is a measurable per-request I/O cost that could be one contiguous read of the pointer table. Smallest safe fix: read the whole urlPtr table once (or cache pointer pages in `blobCache`) instead of per-pivot range reads.

- [medium] web/generate.py:487 — `zim_file` (when overridden at line 616-617 with a name read from Archive.org's file listing at line 610) is interpolated unescaped into the `href` attribute, whereas `title_attr` on the same line is HTML-escaped. A hostile/compromised Archive.org item filename containing `"`/`>`/`<` would inject markup or attributes into the generated index.html (reflected into every visitor), and currently even benign special chars can corrupt the download URL. Smallest safe fix: `escape(zim_file, quote=True)` and use the escaped value in the href.

- [medium] web/watch-and-deploy.sh:24 — `if python3 web/generate.py --deploy 2>&1 | tail -5; then last_count=$count; fi` makes the pipeline exit status that of `tail` (always 0), not `generate.py`. A failed generate/deploy is treated as success, so `last_count` is advanced and the watcher will never regenerate the site on subsequent polls even after the transient failure clears (stale site shipped silently). Smallest safe fix: add `set -o pipefail` (with `set -e`) or drop the pipe and check the python exit status directly.

- [medium] web/watch-and-deploy.sh:10 — `EXPECTED=10` disclaims to be "Total number of regions defined in REGIONS", but `web/generate.py`'s REGIONS list defines 45 entries. The watcher therefore exits (line 29) once only 10 items are live, long before all configured regions are deployed, contradicting its own header comment and leaving the remaining regions unreconciled. Smallest safe fix: compute EXPECTED from the REGIONS count in generate.py (or import/parse it) so the watcher truly waits for all configured regions.

- [medium] wikidata_cache.py:318-326 — feature names extracted from OSM data are interpolated into the SPARQL `VALUES` tuple with incomplete escaping (only `"` is replaced; backslashes are not, and the string is split on `"` inside a `( "..." @en ... )` row), and the surrounding query has no parameterization. A crafted name (e.g. one containing a backslash before the quote, or a newline) breaks the statement or injects arbitrary SPARQL against the public endpoint. Even though this is a local build tool, the input crosses a trust boundary (untrusted OSM tile/name data feeding a network query builder). Smallest safe fix: escape both backslash and quote (`name.replace("\\", "\\\\").replace('"', '\\"')`) and reject/quote control characters.

## Coverage

web/drive/viewer/routing-worker.js — findings: 1
web/drive/zim-reader.js — findings: 1
web/generate.py — findings: 1
web/stats.py — clean
web/watch-and-deploy.sh — findings: 2
wikidata_cache.py — findings: 1
# Pi sweep — batch-9

## Summary
Reviewed 20 files, all in the offline routing-differential test/benchmark
suite (corpus generation, A* differential harnesses, spatial-chunked graph
reader/loader, chip/search/chunk contract tests and compression/perf
benchmarks). The cross-references these tests depend on
(`chunk_graph_file` in create_osm_zim.py, `_sub_bucket_for_name` in
cloud/repackage_zim.py, `cloud.chip_rules`) all resolve and match the
contracts the tests assert. Code is overwhelmingly well-formed; one
defensible defect found (an unbounded, un-capped pair-picking loop that can
hang forever). The remainder are benchmark/test harnesses with no concrete
failing behavior worth reporting.

## Findings
- [low] tests/mem_compare.py:214 — `pick_pairs` runs an uncapped `while len(pairs) < n:` loop with a hard 20–80 km haversine filter and no attempt cap; if the loaded graph contains no node pair in that straight-line band (small/isolated graphs, or graphs whose nodes are all clustered or all >80 km apart), the process spins forever instead of erroring, hanging a memory-benchmark run. — Concrete consequence: unbounded CPU hang on graphs that can't satisfy the distance filter. — Smallest safe fix: add a retry cap (e.g. `attempts = 0; cap = n * 20000`) and raise/return what was found when the cap is hit, mirroring `pick_valid_pairs` in generate_golden_corpus.py.

## Coverage
test_satellite_compression.py — clean
test_terrain_compression.py — clean
test_zim_perf.py — clean
tests/_regen_headers.sh — clean
tests/diff_corpora.py — clean
tests/functional_search_test.py — clean
tests/generate_cell_diverse_corpus.py — clean
tests/generate_golden_corpus.py — clean
tests/mem_compare.py — findings: 1
tests/run_identity_suite.sh — clean
tests/smoke_japan_spatial.py — clean
tests/szrg_astar.py — clean
tests/szrg_reader.py — clean
tests/szrg_spatial.py — clean
tests/szrg_spatial_astar.py — clean
tests/test_chip_rules.py — clean
tests/test_chunked_zim_roundtrip.py — clean
tests/test_empty_tile_skip.py — clean
tests/test_graph_chunking.py — clean
tests/test_hot_chunk_split_native.py — clean
# Pi sweep review — batch-10 (tests)

## Summary
Reviewed the 13 test/support files in this batch. Most are well-constructed unit,
integration, and regression suites. The notable defect is a tautological test in
`test_native_flags.py` that claims to verify the low-zoom VRT selection logic but never
invokes the production function and compares a value to a value derived from the identical
expression, so it cannot fail and the real `os.path.isfile()` branch in
`generate_terrain_tiles` is untested. One additional weak assertion in
`test_szrg_parser.py` compares a re-parse to itself instead of the original fingerprint.

## Findings
- [medium] tests/test_native_flags.py:87-95 — `test_vrt_path_selection_is_zoom_conditional` is a tautology: it never calls `generate_terrain_tiles`/any production code; it re-implements the expected `z<=7` branch inline and asserts `vrt == expected` where both sides are derived from the equivalent expression (`low_flag if (z<=7 and low_flag) else mosaic` vs `world if (z<=7 and low_flag==world) else mosaic`), so the assertion holds for every input by construction and can never fail. It also omits the production branch's `os.path.isfile(low_zoom_world_vrt)` guard (create_osm_zim.py:905-907), so the actual VRT-selection path is completely untested and the suite gives false confidence in the critical z<=7 world-VRT routing. — smallest safe fix: import/monkeypatch `generate_terrain_tiles` (stubbing rasterio/merge/mercantile deps), call it with `low_zoom_world_vrt` set to an existing temp file, and assert the emitted worker args' VRT path is `world` for z<=7 and `mosaic` for z>=8 (and that a non-existent path falls back to mosaic).
- [low] tests/test_szrg_parser.py:189-196 — `test_fingerprint_is_json_safe` asserts `back == json.loads(s)` where the RHS is a second fresh parse of the same string; this compares the value to itself and only proves JSON-serializability (raised only if `dumps`/`loads` throws), not that the round-trip preserves the original fingerprint. — smallest safe fix: capture `fp` once and assert `json.loads(s) == fp` (and `json.dumps(fp, ...)` key order stable) so a fingerprint that mutates/drops fields on round-trip is actually caught.

## Coverage
tests/test_native_flags.py — findings: 1
tests/test_overture.py — clean
tests/test_route_identity.py — clean
tests/test_routing_worker_v3.py — clean
tests/test_spatial_chunking.py — clean
tests/test_szrg_parser.py — findings: 1
tests/test_szrg_v5_split.py — clean
tests/test_upgrade_spatial_zim.py — clean
tests/test_v5_end_to_end.py — clean
tests/test_validator_regression.py — clean
tests/test_wiki_articles.py — clean
tests/test_wikidata_titles.py — clean
tests/v4_to_v5_convert.py — clean
# Pi sweep — batch-11

## Summary
`web/drive/viewer/maplibre-gl.js` is a vendored, git-tracked, minified distribution bundle of the third-party MapLibre GL JS v5.23.0 library (BSD 3-Clause, UMD wrapper + single ~458KB minified line). It contains no hand-written application code; all project logic lives in `index.html`, which is outside this batch. There are no defensible findings for this file — reviewing a compiled upstream library bundle as if it were project code would produce only noise, and there is no project-specific key, secret, or modified logic to flag.

## Findings

## Coverage
web/drive/viewer/maplibre-gl.js — clean
# Sweep batch-12 findings

- [medium] cloud/validate_zim.py:1286 — `_audit_tiles` Gate 2 fails any deep zoom (z>8) whose tile count is <5% of the bbox-expected count (`ZERO_COVERAGE_FAIL=0.05`), even though the same function's own docstring and the header comment state "Low absolute coverage alone is NOT a fail" and cite Japan at 7.5% land as fine. Any region whose land fraction inside its bbox is below ~5% (e.g. the ocean-heavy Hawaii bbox the code itself discusses for `--map-center`, which spans the uninhabited NW Hawaiian islands so land is a tiny slice of the bbox) will be hard-failed and block upload even though the build is healthy — a false negative on a correct ZIM. Fix: drop the absolute-`ZERO_COVERAGE_FAIL` gate and rely only on the relative cliff-drop (child/parent) and blank-tile gates, or scale the absolute threshold against the region's actual land fraction derived from an early zoom.

- [low] cloud/validate_zim.py:558 — `_chk_vector_coverage` populates `empty_by_zoom` (tiles with `size < 50`, i.e. ~35B empty MVT) but never uses it for any gate; only the count-based cliff-drop between zooms is enforced. Its docstring (lines 539-540) states the purpose is to catch "an empty zoom [that] silently breaks rendering," but an entire zoom composed of empty tiles passes. Consequence: a zoom where tilemaker emitted only empty MVT tiles ships undetected. Fix: mirror `_chk_satellite_coverage` (line 529) and fail/warn when any zoom's empty fraction exceeds a threshold (e.g. >50%).

- [low] resources/viewer/routing-worker.js:1090-1093 — a cancelled route still triggers the expensive fallback path. When `findRouteSpatial` returns null because the route was cancelled (`ctx.cancelled()` at line 811/871), `findRoute` then unconditionally runs `findRouteSpatialTwoPass`, which runs three more full optimal A* passes (up to 200k pops each, with cancellation only re-checked on 50ms yields) before the worker honors the abort. Consequence: cancelling a long route still burns significant CPU and re-fetches cells the user has already aborted, delaying the route-done/cancelled response. Fix: guard the fallback — `if (!routeResult && useTwoPass && !(ctx.cancelled && ctx.cancelled()))`.

## Coverage
cloud/validate_zim.py — findings: 2
cloud/repackage_zim.py — clean
resources/viewer/routing-worker.js — findings: 1
# Pi sweep — batch-13

## Findings

- [low] cloud/upgrade_spatial_zim.py:523,532 — If the source ZIM has oversized search chunks but no `search-data/manifest.json` entry (`captured_search_manifest is None`), every oversized chunk was already added to `replaced_search_paths` (line 366) and collected in `captured_search_chunks`, but the only place they are re-emitted is the `for prefix, raw in captured_search_chunks.items()` loop nested inside `if captured_search_manifest is not None:`. When the manifest is absent those chunks are neither passed through nor re-emitted, silently dropping search data from the output ZIM (search results missing after upgrade). Fix: split/emit `captured_search_chunks` outside the manifest guard (only the manifest-bookkeeping needs the manifest), or fall back to a synthesized empty manifest so the loop always runs.

- [low] cloud/preflight.py:264 — The fast path (default; `--audit-content` off) flags every missing terrain tile as a hard `fail`, because the VRT ocean-skip is gated behind `if audit_content:`. In a coastal/maritime region the default quick run (which the module docstring recommends as the seconds-fast gate, e.g. `python cloud/preflight.py --bbox=...`) reports every legitimately-absent ocean tile as `missing`, producing a large false FAIL list and a hard build stop. The ocean check is only needed for the small set of missing tiles, so it doesn't need to be tied to the expensive full content audit. Fix: always consult the VRT for missing tiles (skip only missing pins as ocean) regardless of `audit_content`, keeping `audit_content` solely for the per-tile byte/pixel content audit.

- [low] cloud/route_cli.py:108 — `nearest_node_filtered(..., highway_only=True)` builds the full haversine distance array for all ~20M nodes, then runs `np.argsort(d)` over the entire array even though it only needs the nearest 50k candidates — a full O(n log n) sort plus a ~160 MB index allocation, repeated once per src and once per dst (lines 410-411) on every `hwy2`/`all` run. This contradicts the function docstring ("walks concentric rings rather than scanning all 20M nodes") and adds several seconds to the very timing this CLI exists to measure, skewing the `hwy2` wall-clock comparison. Fix: don't sort the whole array — use `np.argpartition(d, 50_000)` (or `np.argpartition(d, kth=min(g.num_nodes,50_000)-1)` so only the nearest k are ordered), or compute the radius-rings incrementally as the docstring describes.

## Coverage
cloud/upgrade_spatial_zim.py — findings: 1
cloud/preflight.py — findings: 1
cloud/route_cli.py — findings: 1
cloud/manifest_writer.py — clean
# Pi sweep — batch-14

## Findings

- [medium] verify_tile_cache.py:180-188 — In `--type terrain` mode with no usable DEM mask (DEM_SOURCES missing/empty, or all `.tif` < 1000 bytes), `load_land_cells()` returns `{}`, but the code passes that non-None dict into `check_zoom`'s coarse path. There every tile fails `(floor(lat_c), floor(lon)) not in land_cells` (empty dict ⇒ always True), so EVERY tile is counted as `skipped_ocean` and never tallied as expected — the scan reports `Expected: 0 / Present: 0 / 100%` as if the cache were fine. The code even prints "no DEM sources … cannot determine land vs ocean" (line 252) but then proceeds down the ocean-skipping branch instead of falling back to counting all tiles. Consequence: a false "all present / nothing missing" clean signal for terrain verification, silently masking real missing-tile coverage. Smallest safe fix: when `not land_cells`, set `land_cells = None` (so `check_zoom` skips the ocean filter entirely and counts every tile in the bbox), or short-circuit and abort terrain verification when no land mask can be built.

- [low] verify_tile_cache.py:143-151 — Rasterio datasets opened in `tile_has_land` are cached in `dem_cache` and never closed (`ds` has no `ds.close()` anywhere in the worker). In `--accurate` terrain mode each `check_zoom` worker holds one open GDAL file handle per distinct 1° DEM it touches for the entire zoom scan; across N workers and a wide region (asia, south-america, oceania span thousands of cells) this can exhaust the process file-descriptor limit. Consequence: scan fails mid-run with EMFILE for large regions. Smallest safe fix: after the per-zoom loop in `check_zoom`, close all cached datasets (`for ds in dem_cache.values(): ds.close()`), or set a bounded LRU on the cache.

- [low] cloud/verify_terrain_freshness.py:106,280 — The `_check_tile` docstring and the `--check-content` help text both claim the zero-fill check is ">20% zero pixels AND max elev > 100m", but the implementation (lines 155-166) actually flags consecutive full-height zero **columns** at the left or right tile edge (≥10 columns). The documented criterion and the implemented criterion are different, so the flag's contract is misleading to anyone relying on the help. Separate caveat: `zero_fill`/`decode_error` are only computed when `--check-content` is passed (off by default), so the default mtime-only audit (and whatever preflight wrapper calls it without the flag) does not catch truncated/corrupt tiles that happen to have a fresh mtime. Consequence: a builder may ship a content-corrupt tile the audit reports as "fresh". Smallest safe fix: make the help/docstring describe the column-block heuristic, and have `cloud/preflight_build.sh` (not in this batch) pass `--check-content` so the structural guarantee covers corrupt content, or default the flag on.

## Coverage
cloud/verify_terrain_freshness.py — findings: 1
rust/streetzim-pack/src/main.rs — clean
web/drive/sw.js — clean
verify_tile_cache.py — findings: 2
# Pi sweep — batch-15

## Findings

- [low] cloud/swap_viewer_rust.py:171 — every source entry is materialized in Python as `data = bytes(item.content)` and passed to `_Item` as `_data`, so `ManifestCreator._item_record` base64-inlines it (`body_b64`) regardless of size. This bypasses the streaming path (`_file_path` + >=64 MiB threshold) that `cloud/manifest_writer.py` explicitly exists to provide, and that file's own docstring warns base64-inlining a 1 GB routing chunk holds ~1.4 GB of UTF-8 string plus the source bytes. On rust-built ZIMs (Japan-scale routing chunks >500 MB) this drives peak RSS to multiple GB per large entry. — concrete consequence: memory spikes / OOM risk on the very ZIMs this tool targets. — smallest safe fix: stage items whose `len(data) >= 64 MiB` to a temp file first and pass `_file_path=` on the `_Item` so `_item_record` takes the `streaming: true` path, keeping RSS bounded.

- [low] cloud/build-vm-startup.sh:99 — the 85 GB planet PBF is faked with `truncate -s 91646574601` (a zero-filled sparse file) and the build's cache hit depends on the `qids_planet-*.json` name+size+mtime key matching. The script's own comment (line 110) confirms that if the mtime is not restored the build "re-scans the full 85 GB PBF". On any cache miss (missing/renamed cache file, or a failed mtime restore) `create_osm_zim.py` will parse the all-zeros sparse PBF and silently emit an empty/broken POI set rather than erroring. — concrete consequence: a build that ships with no/few OSM features, unbeknownst to the operator. — smallest safe fix: after restoring the mtime, verify the expected `qids_planet-*` cache file exists (and matches the key) before proceeding, and fail loudly if it does not instead of relying on a silent re-scan of zero-filled data.

## Coverage
cloud/build-vm-startup.sh — findings: 1
cloud/validate_platforms.py — clean
cloud/wikidata_titles.py — clean
cloud/swap_viewer_rust.py — findings: 1
# Sweep findings — batch-16

## Findings

- [low] cloud/launch-build-vm.sh:36-58 — `--fast`/`USE_FAST` is accepted from argv but never consulted anywhere; machine type is chosen solely by `USE_BIG`. Its documented behavior ("--fast Use c3-standard-8, ~50% cheaper than n2-standard-16") is never honored, and the help/comments keep referencing a "c3 default" while the actual default is `n2-standard-8`. Users invoking `--fast` get silently different hardware/pricing than advertised — dead option.
- [low] cloud/launch-build-vm.sh:118-124 — Archive.org `IA_ACCESS`/`IA_SECRET` (long-lived static credentials) are written into GCE instance metadata via `--metadata-from-file`, where they persist on the instance record and are readable by any project member with compute view/admin roles (and via the instance metadata server). The builder SA has `roles/compute.instanceAdmin.v1` and `roles/storage.objectAdmin` plus a `cloud-platform` scope, so it could fetch from Secret Manager instead. Prefer storing a reference (secretmanager secret id) in metadata and resolving at startup.
- [medium] overture-rollout-redo.sh:99-123 (call sites ~135-200) — Each wave launches 2-3 `build_and_ship "..." &` in the background, and every build runs `create_osm_zim.py --terrain`, which calls `generate_terrain_tiles(bbox, terrain_dir=.../terrain_cache, ...)` writing regenerated `.webp` tiles into the same shared `terrain_cache/{z}/{x}/{y}.webp` tree (and `satellite_cache*` for downloads). Concurrent processes writing the same tile files (overlapping regions, and low-zoom world-spanners all regions share) race: interleaved/partial WEBP writes and lost updates, producing corrupted or clobbered terrain tiles that then pass straight into the packaged ZIM. Smallest safe fix: serialize builds (drop the `&`/one at a time) or give each build an isolated `--terrain-dir`/satellite cache, or gate the shared cache writes with a per-tile lock.
- [low] overture-rollout-redo.sh:31-40 — `wait_for_parquet` spins on an unconditional `while :` loop with no timeout or retry cap. If the `download_overture_data.py` worker crashes (or the expected `2026-04-15` filename is never produced), the `-s` check and `pgrep` both stay false forever and the whole wave blocks indefinitely with only "Waiting for parquet" log spam and no failure. Add a deadline/count and fail the build with an error when it is exceeded.

## Coverage

- cloud/launch-build-vm.sh — findings: 2
- cloud/fix_stale_terrain_tiles.py — clean
- web/drive/fzstd.js — clean
- overture-rollout-redo.sh — findings: 2

## Run stats

input 943284 tok (+5494784 cached), output 177194 tok, cost $0.22 — 138 files in 11m (704.7 files/h, 0.7 min/batch)
