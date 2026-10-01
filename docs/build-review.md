# ZIM build review — 2026-09-30

Reviewed the build pipeline after initially pulling `main` from `557f971` to
`d5c32b6045604d19584f9d9280cedc33d7c8d01a`, then pulling/rebasing onto
`3679aa1844b5494360c2e964e654550e629c4f83`, and again onto
`4e115c05621cdc57650c779fd92c59c409db83dd`. The changes below are local
and preserve the checkout's pre-existing deletions and untracked work.
Binary and JSON format versions are unchanged.

## Fixes made

| Area | Problem and resulting behavior |
| --- | --- |
| Output publication | Python, the public CLI and standalone Rust packer now finalize private outputs on the destination filesystem before publishing. Failure or interruption preserves a previous archive. Concurrent CLI runs have separate scratch and staging; without `--overwrite`, a late-arriving destination is preserved. |
| Tiles | Regional MBTiles scans previously materialized every coordinate just to compute bounds. Bounds and progress counts now use constant-space ranges, including antimeridian deduplication. SQLite readers close on early termination and errors; normalized views and `WITHOUT ROWID` tables work without a `rowid`. |
| Satellite | Coordinates and futures now flow through a bounded queue of at most twice the worker count. Missing source tiles remain retryable; partial 512-pixel mosaics are not cached. Cached JPEGs are decoded before reuse, corrupt sources are refetched, and network images are validated before atomic publication. Unavailable AVIF encoding fails with install/WebP guidance before downloading. |
| Downloads | HTTP and local source downloads share per-file locks, atomic publication and metadata. HTTP headers are case-insensitive; HEAD and GET sizes are checked. Unstamped generic PBF/poly downloads are refreshed rather than accepted by size alone. Local URLs use decoded paths and nanosecond timestamps. Lock files retain their inode so existing waiters cannot race a newly created lock. |
| Terrain | DEM and VRT writers use unique sibling staging files. Short HTTP bodies and VRT errors preserve earlier files. A VRT with no readable sources remains absent rather than becoming an empty file. Exact bbox and source-zoom cache keys prevent neighboring regions or fresh/production layouts from sharing incomplete mosaics. |
| Search | Location grids and executor initializer references are released before sorting, including error paths. Worker scratch is owned and cleaned after the pool exits. Location workers explicitly use spawn: Python 3.14's default forkserver failed on Docker Desktop bind-mounted scratch when setting socket permissions. Streaming extraction falls back to the actual highest stored zoom when z14 is absent. Unicode street/POI identities and combining marks survive conflation; valid spaced/escaped JSON records are accepted. Viewer application text no longer pollutes libzim full-text search. |
| Wikidata | Invalid Q-ID tags are filtered before batching, including old extraction caches. Two semicolon-separated tags in the Netherlands PBF caused entire SPARQL batches to fail, dropping 78 valid neighboring IDs. The ambiguous tags are skipped with a bounded warning; valid IDs can be retried on the next build. |
| Docker packaging | Generated country outputs, local virtual environments, development Rust targets, test/lint caches and the operational URL validation cache are excluded from the build context. This prevents subsequent image builds from uploading country data and embedding hundreds of megabytes of unrelated cache. User files remain on the host. |
| Image publication | The incoming publication workflow rebuilt the image after CI, let old releases overwrite `latest`, and allowed stale reruns to replace current main's pending publish. CI now exports its validated image; publication verifies its checksum, identity, architecture and commit. A shared queued publisher keeps pending runs, and read-only metadata checks approve only current main or matching stable tags. Version files are parsed as restricted literal data; the package-write job runs no release source or container code. |
| Routing | Builds no longer remove another run's node store solely because it is six hours old. Failed store construction cleans only owned scratch. Geometry serialization writes its bytearray directly, removing a full-size copy. Monaco graph bytes remain identical. |
| Rust packaging | Large in-memory bodies are staged as files instead of base64/JSON copies. Raw bodies use the actual chunked API. Compressed bodies retain the established pipeline after an adversarial benchmark exposed excess concurrent encoder memory. Main-page selection follows the manifest config; front-article hints no longer replace it. Explicit workers reach Rayon, and unsupported indexer modes fail clearly. |
| Usability and instrumentation | Invalid worker/indexer choices and unavailable accelerator executables fail before build work. Compression logs distinguish an explicit Rust level from libzim's unknown effective default. Tile watchdogs stop on failures. Interrupted external indexing terminates/reaps launched processes and removes their partial databases; reused databases survive. `measure_build.py` measures Linux RSS/PSS and macOS process-tree RSS, normalizes units, reports unavailable values/errors, bounds sample storage, limits disk scans and terminates descendants on cancellation. |

Terrain keys require a one-time refresh of affected completion markers/VRTs;
downloaded DEMs and encoded tiles remain reusable. Failed Rust manifests are
preserved for inspection, but external file references still require their
original files. SIGKILL can leave hidden scratch; atomic publication does not
make that scratch safe to delete while another build is using it.
For SIGTERM cleanup, use the `streetzim` CLI or `measure_build.py` supervisor.
Direct SIGTERM to raw `create_osm_zim.py` retains its default process-exit
behavior and bypasses Python cleanup; hidden scratch and external indexers
can survive it.

Current main also supplies CPU budgeting (`streetzim/cpus.py`, `--cpus`) and
reuse of the initial complete-way PBF cut for addresses, wiki tags and routing.
Those are upstream changes retained during integration, not improvements
attributed to this review. The affinity fallback now also supports macOS,
which has no `os.sched_getaffinity`.

## Measurements

Raw measurements live in [`benchmarks/build-review/`](../benchmarks/build-review/).
The original before/after measurements below use `d5c32b6` as their baseline.
RSS below uses decimal MB. The setup/serialization cases isolate specific
allocations and are not estimates of a continent build's peak or throughput.
An 80–90% reduction of one allocation does not imply the same reduction of
the full build. Native tilemaker and osmium allocations remain; satellite
and Rust transport improvements are not exercised by a vector-only libzim build.
Most are medians of three fresh processes on macOS arm64/Python 3.12; each
JSON specifies its scope, worker settings and method.

| Workload | Before | After | Interpretation |
| --- | ---: | ---: | --- |
| Complete warm-cache Monaco build | 3,255.7 MB / 3.047 s | 3,265.9 MB / 3.075 s | Whole-build peak and time are essentially unchanged on this small workload. |
| US bbox z14 bounds setup, empty indexed MBTiles | 540.7 MB / 3.049 s | 28.3 MB / 0.0013 s | 94.8% lower process peak RSS; avoids coordinate enumeration. |
| 79,053 synthetic satellite cache hits, 4 workers | 188.7 MB / 0.741 s | 31.9 MB / 0.876 s | 83.1% lower peak RSS; 18.3% slower trivial scheduling. Encoding/network throughput was not measured. |
| 128 MiB manifest serialization | 691.9 MB / 0.818 s | 155.1 MB / 0.169 s | Removes large transport copies; caller still owns the original body. |
| 128 MiB raw Rust packing | 410.0 MB / 0.325 s | 141.6 MB / 0.741 s | 65.5% lower peak RSS; extra staging/splicing/fsync costs time on this short synthetic job. |
| Four 64 MiB compressed bodies, level 22, 1 worker | 947.7 MB / 2.760 s | 947.8 MB / 2.784 s | Final compressed path retains baseline memory behavior. |
| 100,000-place annotation, 2 spawn workers | 215.1 MB | 139.0 MB | Parent RSS after completion; aggregate process-tree peak was not measured in this case. Output digest matches. |
| 64 MiB geometry serialization | 67.11 MB | 0.0054 MB | Traced allocation peak of serialization alone. Process peak RSS falls 162.6 → 93.3 MB; output checksum matches. |

The complete build uses fixed PBF/MBTiles inputs, a warm verified viewer cache,
routing/spatial cells and Find chips. Three before/after runs were interleaved;
the JSON records input hashes, commands, CPU, disk and 50 ms memory samples.
The MBTiles were made from the fixture PBF with local tilemaker; coastline
and Natural Earth files were unavailable. This fixture checks pipeline and
archive parity rather than cartographic completeness.
An additional 1/2/4/8-worker sweep left the peak at 3.25–3.27 GB, so the
Python/libzim default is now `min(compression_cpus(), 20)`: an explicit
`--cpus` or CPU quota affects compression; the automatic memory rule affects
tilemaker/search/terrain workers rather than compression. An explicit
`--zim-workers` overrides the compression worker count.

Two independent probes found the peak in **`osmium extract`**, before packing:
isolated address/wiki bbox cuts each used about 3.185 GB. Their tag filters
used 647 MB/1,478 MB; export used only 5 MB/12 MB. The PBF has 68,512 nodes
but its maximum node ID is 14,219,141,884. Osmium's documented extraction
memory scales with that ID space; `complete_ways` needs twice the simple
strategy's bitmap memory. Switching strategies would change boundary-way
completeness, so this review preserves the existing geometry behavior.
See the [official extraction manual](https://docs.osmcode.org/osmium/latest/osmium-extract.html#memory-usage)
and `osmium-phases.json`. Current main reuses the initial PBF cut, eliminating
the repeated cuts in the normal downloaded-PBF flow. The initial native
ID-space allocation still remains.

The rejected compressed-streaming candidate reached **3.23 GB** on the
four-body level-22 case despite a 1 MiB pipeline budget. Encoder tasks were
outside that budget and remained live until archive finalization. That
candidate is not enabled; its JSON is explicitly labeled rejected. A future
compressed streaming implementation needs an upstream wait/drain API and
multi-item production-level benchmarks.

Even raw streaming does not bound total RSS: final verification maps and
touches output pages. Compressed bodies still need one full-body buffer;
encoder state, indexes and producer buckets are additional. Avoid treating
`max_in_flight_bytes` as a whole-process memory cap.

Reusable measurement tools:

```bash
python tools/benchmark_tile_pipeline.py --baseline-ref d5c32b6 --repetitions 3 --output tile.json
python tools/benchmark_search_memory.py --baseline-ref d5c32b6 --rows 100000 --output search.json
python tools/benchmark_routing_serialization.py --mib 64 --output routing.json
python tools/benchmark_zim_pack.py --json pack.json --mib 128 --pack-bin /path/to/streetzim-pack
python tools/measure_build.py --json build.json --watch /path/to/output --log build.log -- streetzim ...
```

`measure_build.py` records sampled process-tree memory, kernel-reported command high-water RSS,
wall/CPU time, output/scratch disk use, sample columns and errors. Linux PSS
accounts for shared pages; macOS PSS is null and tree RSS double-counts shared
pages. Short peaks between samples can be missed. Disk scans exclude symlink
targets, so watch shared caches explicitly when measuring their growth.

## Verification and adversarial review

Three reviewers covered memory/search/routing, reliability/CLI/downloads,
and Rust packaging. They cross-reviewed changes and exercised errors,
interruption, concurrency, malformed manifests, size disagreement, main-page
redirects, Unicode collisions and real archive reads. Findings corrected
during review included empty VRT publication, erased recovery manifests,
orphaned descendants on cancellation and concurrent encoder memory growth.
The following original counts/golden measurements predate the final upstream
integration; current-main verification is recorded separately below.

- Complete Python/ops suites: macOS Python 3.12 **1,192 passed / 303 skipped**;
  Linux Python 3.12 **1,184 passed / 311 skipped**; Linux Python 3.14
  **1,184 passed / 311 skipped**. The macOS run includes optional real Rust
  round trips; Linux uses GDAL's VRT builder. Some production-region tests
  require large local ZIMs and skip without them.
- Rust: **8 tests**, clippy clean; **19** adapter/real libzim integration cases
  and **12** independently constructed adversarial packer scenarios pass.
  External indexing has **9** actual-process lifecycle/cache cleanup cases,
  including termination resistance, partial writes, retry and reused outputs.
- Monaco golden comparison: **1,167 identical entries**, two volatile Xapian
  indexes, no unexpected changed/missing/reordered entries. Actual routing
  extraction remains exactly **474,563 bytes**, SHA256
  `50d8ddfba81d055780f6fd8dcadd205e0b81e89a10673e8c3010344772c8635a`.
- Python and Rust Monaco builds pass the repository validator and current
  stock `zimcheck` **3.8.0**. Rust's external full-text index passes native
  search checks. The generated Python archive passes **17** headless Chrome
  viewer smoke assertions.
- All seven Node suites and viewer ESLint pass; generated viewer, pinned
  assets and offliner checks pass. Ruff is clean for reviewed source; pyright
  has no new findings and its baseline shrank from 18 to 17.

The host's Homebrew GDAL and stock zimcheck have broken dynamic-library
dependencies; tests used the supported Python VRT fallback locally and
isolated Linux/current stock tools for native checks. The root boundary gate
cannot read a pre-existing deleted tracked file, and an unrelated untracked
script has an existing Ruff finding. Both gates pass in an isolated tracked
checkout with the reviewed changes overlaid; those user-owned files were
left untouched. Python 3.14 dependencies emit shutdown diagnostics after
the passing suite; the process exits successfully.

### Current-main integration and Docker validation

The merged checkout and repository Docker image use current main `3679aa1`
plus the review fixes. Linux Python 3.14 completed **1,533 passed / 48 skipped**;
the macOS critical suite completed **204 passed / 6 skipped**. All seven Node
suites, generated viewer ESLint, reviewed-source Ruff and the type gate pass
(17 baseline findings, none new). A separate final review exercised the CPU
fallbacks and **75** focused CPU/CLI tests.

A Monaco archive built with this exact Docker image's python-libzim passes
the repository metadata/progress/routing checks, structural validator,
official openZIM `zim-tools` 3.8.0 Docker `zimcheck`, and **17** in-archive
headless Chrome assertions. Its fixture scope is still Monaco; it does not
establish an upper memory bound for the Netherlands build.

The first Netherlands run exposed the forkserver scratch/socket failure;
both location executors now request spawn without changing the process-wide
default. Regression tests force forkserver globally and use a scratch path
too long for a Unix socket; both the streaming and in-memory paths pass on
macOS and Linux Python 3.14. Six pinned-image Linux probes (100,000 records,
2 workers, three fresh processes per context) produced identical output
digests and correctly assigned all 90,000 POIs. Median forkserver → spawn:
2.0033 → 2.0560 seconds (+2.6%), PSS 212.0 → 214.1 MB (+1.0%), RSS
267.1 → 251.3 MB (−5.9%). These are small-workload context measurements.
`wait4` CPU values are not comparable because the forkserver owns/reaps its
workers. Raw results and provenance are in
`out/netherlands-full-libzim-20260930/spawn-benchmark/`.

The Wikidata validation regression suite includes malformed cached and fresh
IDs, Unicode, injection-like values, non-string values and valid neighbors.
The focused Wikimedia suites pass **99 tests**. The subsequent Linux Python
3.14 core suite exits successfully with **1,456 passed / 46 skipped**;
the separately rerun ops suite passes **89 tests / 2 skipped**. Together,
these are **1,545 passed / 48 skipped**, including the 12 new QID tests.
The earlier 1,533/48 integration run covered core plus ops before those tests.
An independent comparison confirms the exact progress sequences explain the
counts. Logs are `/tmp/streetzim-netherlands-8g-linux314-tests.log` and
`/tmp/streetzim-repeat-main-ops314-tests.log`.

### Netherlands native worker benchmark

Six real tilemaker runs use the same frozen clipped Netherlands PBF,
shapefiles, config/Lua and pinned Docker image. The interleaved worker order
is 20, 4, 4, 20, 20, 4. Four workers are selected automatically by the
8 GiB container limit; its memory-swap limit equals its memory limit, disabling
swap. The 20-worker containers have no memory/swap limit. Workers and container
limits therefore change together. A separate full build and unrelated host
workloads run concurrently; the timing comparison is indicative.

| Median of three native runs | 20 workers, unlimited | Auto 4 workers, 8 GiB |
| --- | ---: | ---: |
| Sampled process-tree peak PSS | 8.068 GB | 3.970 GB |
| Container peak memory, including file cache | 10.094 GB | 4.474 GB |
| Tile generation time | 65.675 s | 210.900 s |

Process-tree PSS is 50.8% lower; the native phase takes 3.21 times as long.
All six runs exit successfully without swap, OOM or other cgroup memory
events. Linux PSS/RSS is sampled every 250 ms; cgroup counters every 2 seconds
and `memory.peak` retains the exact container high-water value. GB is decimal;
the Docker limit is binary GiB. These are native-phase measurements, not a
whole-ZIM peak or an 80–90% country-build reduction.

All runs contain the same 63,126 tile coordinates and per-zoom counts.
Exact content parity does **not** hold: even two other 20-worker runs have
7,110 and 7,954 strict protobuf content mismatches against the first.
The four-worker runs have 11,853, 11,259 and 11,709 mismatches. Canonicalization
retains typed attributes, feature/dictionary multiplicity, layer metadata
and exact geometry command arrays; it only ignores compression, dictionary/
feature ordering and the order of distinct property keys. A separate decoder
checks 9,705 features from 25 selected tiles. Some samples contain actual
geometry changes, including between 20-worker runs, so no global displacement
bound, visual equivalence or full semantic identity is claimed.

Raw measurements, input digests, coordinate/blob hashes, complete strict
comparison reports, geometry audits and the chart are in
`out/netherlands-tilemaker-benchmark-20260930/`. The six generated MBTiles were
removed after these reports were saved; their reproduction commands remain.

### Repeat pull and publication review

The second pull integrates `e478160` and `4e115c0` without conflicts. It changes
only CI scheduling, Docker publication and Zimfarm documentation; the tracked
local patch is byte-for-byte identical before and after the pull. No rebase
state, unmerged paths or stash remains. Existing user deletions are preserved.

Two independent adversarial reviewers accepted the local publication fixes.
**37 policy tests pass on both macOS Python 3.12 and Linux Python 3.14**.
Tests cover malformed/excluded refs without execution, version mutation,
annotated tags, a tag named `origin/main` shadowing a stale branch, newer and
older releases, paginated artifacts, failed-job reruns, nullable provenance,
expired/future/wrong-run artifacts and ambiguous IDs. A real 420 MB compressed
artifact of the already-published amd64 image passes Linux checksum and Docker
save/load image-ID/platform/revision checks. Independent actual-container probes
reject corrupt checksums and wrong IDs, revisions and architectures. Save/load
pipelines use `pipefail`. Ruff and the type gate pass (17 baseline, none new).

The installed actionlint 1.7.12 does not recognize GitHub's newly documented
`queue: max`; the publication workflow passes its other checks with that exact
compatibility diagnostic ignored. Current primary
[GitHub concurrency documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
supports this setting, with up to 100 pending runs. The six existing ShellCheck
diagnostics in CI are unchanged from the pre-pull workflow. The pulled upstream
commit's original CI and publisher have succeeded on GitHub; the local fixes
have not been pushed or run there.

Publication artifacts expire after 14 days. Rerunning all CI jobs restores them
only for commits containing image-export support; older workflow definitions
cannot produce them retroactively. Keep stable release tags immutable. Only the
highest stable Git tag promotes `latest`; a higher failing/unpublished tag can
therefore suppress a lower version's promotion, while its version tag remains
publishable. These policies are documented in `docs/zimfarm.md`.

## Remaining proposals, in priority order

1. **Budget buffers across the build.** Upstream CPU budgeting now accounts
   for CPU quotas and applies a memory-based worker limit to native/search/
   terrain steps. Libzim/Rust encoders, search workers and DuckDB still do
   not share a RAM/byte budget. Measure Linux PSS,
   cgroup peak memory and anonymous/file-backed pages on actual large
   regions, then expose a memory budget and byte-based queue limits. Keep
   explicit worker overrides for throughput tuning.
2. **Reduce the initial native PBF extraction peak.** Current main reuses
   one complete-way cut in the downloaded-PBF flow. MBTiles builds supplied
   with an uncropped external PBF still need their fallback cuts. Reuse
   removes repeated reads/allocations but leaves the initial ID-space peak. An upstream sparse
   ID tracker or compatible extraction replacement is needed for lower RAM;
   prove boundary-way, relation and antimeridian semantics before replacing it.
3. **Bound global search memory.** Global digest and partition tuple sets
   grow with unique features. The full place grid is copied into spawn
   workers, with up to twice the worker count of 50,000-line batches queued.
   Use compact shared/mapped place storage and an external dedup index;
   verify first-record semantics, digest collisions, output identity and I/O
   costs on millions of unique/boundary-duplicate records.
4. **Reduce routing's concurrent working set.** Junction dictionaries,
   geometry dedup keys, edge arrays and sorting copies coexist. Release
   obsolete lookup tables before sorting, then consider disk-backed geometry
   and partitioned adjacency construction. Preserve graph bytes and route
   identity on representative connected graphs.
5. **Bound Overture/address joins.** POI keys, enrichment dictionaries,
   applied sets and OSM cell/name dedup sets scale with input size. Evaluate
   disk-backed joins and compact spatial indexes with deterministic matching
   and international/boundary tests.
6. **Improve completion and recovery reporting.** Existing nonempty encoded
   satellite tiles are still trusted without decoding every cache hit.
   Consider a versioned validated-cache manifest and explicit missing-layer
   counts in build summaries. Scratch reclamation needs ownership/liveness
   checks rather than age alone; locks must remain stable for waiters.

No planet/continent benchmark was run. The measured improvements and
small-region archive checks support these changes; they do not establish
an upper bound for large production builds.


## Python replacement for the manifest packer (2026-09-30)

The custom manifest packer now runs `streetzim.pack`, using Python SQLite,
lzma and python-zstandard. `--zim-builder manifest` selects it; the advanced
core command retains `rust` as an alias. Stock python-libzim remains the normal
Docker/CLI default, and the external xapianbuilder remains optional. No Cargo
installation or sibling zimru checkout is needed to pack a manifest.

Directory sorting, redirect traversal and pointer construction use a private
SQLite database instead of retaining the full directory in memory. Both raw and
compressed file bodies stream in 4 MiB chunks. Compression jobs, bucket count,
blob count and pending logical body bytes are bounded; one oversized item can
progress alone. Default compression workers now follow container memory/CPU
limits, while explicit worker choices remain overrides. Verbose JSON reports
logical pending bytes separately from actual process peak RSS. Parent build
metrics use the packer's own wait4 usage rather than cumulative child usage or
a sampling-only estimate.

Independent adversarial review found and fixed signed Zstd-level compatibility,
cluster target overflow, small-frame window sizing, truncated manifest frames,
Counter ordering, partial-constructor/reader descriptor cleanup, changed manifest
rejection, foreign-cwd module lookup, exception masking and child monitor startup
cleanup. Serialized header/MIME/directory/title/cluster/raw-blob/redirect checks
run before atomic publication; native tests decode compressed payloads. The
adapter preserves prior output and staged recovery inputs on failure, including
partial executable overrides, missing output despite exit zero, interruption,
codec failures and failed publication.

The benchmark compares a frozen reviewed Rust executable (`86df102c…`, built
from the preceding review fixes) with a frozen Python implementation (`cafd534d…`).
Older checked-in target binaries were excluded after provenance checks showed
they predated the earlier fixes. Three fresh processes per backend/case measure
process RSS/CPU/wall time on macOS; content/hash/native validation happens outside
timing. Inputs are synthetic and compressible, with two workers and a 1 MiB
logical queue budget. Concurrent host work and warm filesystem cache limit
throughput comparisons. These are packer measurements, not country-build RAM.

| Input | Rust peak RSS | Python peak RSS | Python/Rust wall time |
|---|---:|---:|---:|
| One raw 128 MiB file | 141.6 MB | 39.3 MB | 0.16× |
| Four 64 MiB files, Zstd 22 | 947.9 MB | 845.3 MB | 1.51× |
| 10,000 small items, single grouping | 26.0 MB | 43.5 MB | 9.05× |
| 10,000 small items, 72 cycling MIME groups | 36.0 MB | 41.4 MB | 12.90× |
| 10,000 small items, 72 cycling extension groups | 36.4 MB | 41.0 MB | 10.63× |
| 10,000 small items, 72 cycling path groups | 38.8 MB | 41.6 MB | 15.22× |

Raw-file RSS fell 72.3%; level-22 compressed-file RSS fell 10.8%, with 1.51×
wall time. Codec context memory remains significant. Python is 9–15× slower
and uses 7–67% more RSS on the small-item fixtures; bounded grouped output is
16.1% larger. The single-group archive size differs by less than 0.1%. These
tradeoffs make libzim the appropriate unchanged default while the Python backend
provides the custom format/compression controls requested here.

The first bounded bucket policy caused equal-size inputs cycling through more
than 64 groups to evict every useful bucket: roughly 10,000 tiny clusters.
Key pressure now evicts the smallest recent bucket; byte pressure flushes the
fullest one. This reduced the measured grouped fixtures to 1,314–1,322 clusters
and cut their archive size 47.6%, preserving grouping and memory limits. Timing
improved across benchmark runs, but those runs do not isolate eviction from
other changes/cache conditions. A permanent equal-size cycling regression
prevents oldest-first tie behavior from returning.

All 36 final benchmark archives passed item hashes, MIME/title/metadata/cover,
main page and checksum readback. All 12 final archive pairs passed stock official
openZIM `zimcheck --all`. A real Monaco archive repack preserved all 1,164 entries
and both existing Xapian databases; native search/suggestion results matched.
A fresh Monaco Docker build using the Python backend in an 8 GiB/no-swap container
selected four packer workers, generated a 2.89 MB ZIM, and passed official zimcheck,
metadata/progress, routing/structure validation and all 17 Chrome viewer checks.
Its whole-build process-tree peak remains about 3.25 GB PSS, dominated by the
earlier native osmium phase; the packer itself used 193.8 MB RSS.

The full Linux Python 3.14 core suite passed 1,655 tests with 40 skips; operations
tests passed 90 with one skip. Final scoped runs subsequently passed 266 cases on Linux and 214 on macOS,
including the added exact-RSS, interruption and mutation regressions. Installed-wheel checks from outside the checkout
passed, including the `streetzim-pack` entrypoint and default ManifestCreator
with native readback. Ruff and the existing Pyright gate pass without adding
baseline findings. A broad host run exposed the already-broken Homebrew GDAL CLI
and unrelated pre-existing dated archive search failures; native Linux checks
provide the authoritative runtime result.

Evidence, frozen sources, benchmark drivers, review reports, XML, Docker logs
and the fresh archive are under `out/python-packer-review-20260930/`. The packer
guide is `docs/zim-builder-python.md`. The Netherlands full libzim builds are
reported separately below; these packer-only figures do not establish a
Netherlands whole-build reduction from the Python rewrite.

## Completed Netherlands full libzim builds (2026-09-30)

Both full country builds completed all nine stages and passed metadata/progress,
full-profile, archive structure, official openZIM `zimcheck`, memory recording and
all 18 Chrome viewer checks. They include routing, terrain, Overture enrichment,
Wikidata, cached Wikipedia pages and the native search index. These archives use
stock libzim, not the Python manifest packer.

| Measurement | Original run | 8 GiB run |
|---|---:|---:|
| Heavy workers | 20 | 4, selected automatically |
| Container lifetime peak | 8.94 GB | 7.07 GB |
| Sampled process-tree peak PSS | 8.33 GB | 5.15 GB |
| Swap / OOM events | 0 / 0 | 0 / 0 |
| Wall time | 5 h 06 min 55 s | 1 h 27 min 51 s |
| Archive size | 2,670,258,955 bytes | 2,670,375,627 bytes |

Memory is decimal GB. The 8 GiB run had an 8,589,934,592-byte container limit,
with swap disabled. Its measured container peak was 20.9% lower and its sampled
process-tree PSS peak was 38.2% lower. Container measurements include file cache
and kernel memory. Process samples were taken every two seconds and can miss
short spikes. The runs used different pinned runtime revisions and cache states;
the follow-up copied caches after the original completed. The wall-time difference
does not establish a speed improvement from worker scaling, and the whole-build
comparison does not isolate the effect of any individual change.

The original build used image `9a1c7495…`; its first custom-validator invocations
failed because that image was no longer locally available. Its official zimcheck
and memory-report checks had already passed. The remaining checks were rerun
successfully with pinned image `16d461a4…`, also used for the 8 GiB build. Original
failure reports/logs remain preserved; the recovery report records both image
identities and confirms the original archive was unchanged.

The 8 GiB archive is
`out/netherlands-full-libzim-8g-20260930/osm_en_netherlands_full_libzim_8g_2026-09.zim`
(SHA-256 `1ac7c5b853ac02f2e4f6536b750af6b18043d05406a0ee40bf1715c83ee7cb85`).
Both output directories contain `memory-report.json`, `memory.png`, `memory.svg`,
`validation.json` and the per-check logs. The supervisor state is now `completed`.
