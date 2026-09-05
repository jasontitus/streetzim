# 2026-09 data refresh + full rebuild round — prep notes

Written 2026-09-04 on the Linux build host. Everything below was measured
or read from the host that day; nothing has been launched yet.

## 1. What is stale in the shipped ZIMs

| Dataset | In shipped ZIMs | Latest available | What it feeds | Refresh cost |
|---|---|---|---|---|
| OSM planet PBF | `planet-2026-03-10` | `planet-260831` (weekly, 94.6 GB) | routing graph, OSM addresses, Wikidata Q-ID scan, regional extracts | ~40 min download (measured 43 MB/s) + 1 planet pass for all regional extracts |
| World vector tiles `world-tiles-v2.mbtiles` | from the 2026-03-10 planet (built on the Mac with tilemaker v3) | must be regenerated from the new planet | map rendering **and** the search/find name index (see next row) | tilemaker planet run: days; needs tilemaker v3 built on this host (apt's 2.4 is too old for `resources/tilemaker/*`; cmake not installed) |
| World search cache `search_cache/world.jsonl` | extracted from world-tiles-v2 (Mar 15) | regenerate after tiles | every street / POI / place name in search + find | hours (`cloud/build_search_cache.py`, one z14 scan of the MBTiles) |
| Overture places + addresses | `2026-04-15.0` (most regions), `2026-05-20.0` (CA, SV), `2026-06-17.0` (Carolinas) | **`2026-08-19.0`** (monthly; a September release usually lands mid-month) | business info (names, categories, phone, website, socials, brand), address gap-fill | minutes per region via DuckDB → S3 (current cache: 42 GB) |
| URL liveness cache `url_validation_cache.json` | crawled 2026-05-10 (1.29 M URLs, 64.5 % alive) | recrawl; max-age is 30 days so everything is due | which Overture businesses ship at all (`--url-cache-policy drop-record`) | one crawl of the new URL set (~1.3 M+ URLs) |
| Wikidata cache | per-Q-ID, incremental | new Q-IDs from the new PBF are fetched automatically | place info panels | small |
| Terrain (Copernicus GLO-30), satellite (Sentinel-2 cloudless) | static | static | — | none |
| Viewer + routing builder code | pre-bit-9 (no "no motor vehicles" access bit) | branch `claude/web-views-routing-review-akztkq` (under review 2026-09-04) | routing quality | merge before building so the new round carries it |

**Overture schema check (done):** a 2026-08-19.0 sample (Monaco bbox) has the
same columns as our 2026-06-17 parquets; the only change is three additive
fields inside `sources[]` (`provider`, `resource`, `version`). The merge code
only reads `dataset`/`license`/`record_id`, so `download_overture_data.py`
and `merge_overture_*` need no changes.

**Business-data quality fix (done, opt-in):** the liveness cache marks 87 k
URLs dead on HTTP 403, 35 k on 429, 62 k on timeout and ~11 k on 5xx — those
are overwhelmingly bot-blocked live sites (Starbucks, BevMo, AAA fell out of
California). `STREETZIM_URL_DEAD_STATUSES=404,410,dns` (now honoured by
`create_osm_zim._is_url_dead` and `cloud/url_cache_filter.is_url_dead`, test
`tests/test_url_dead_statuses.py`) narrows drop/scrub to real dead sites.
`build-refresh-queue.sh` sets it by default; unset it to get the old rule.

## 2. Two refresh tiers

**A. Business-data + PBF-derived refresh (keep the March tiles).**
Overture 2026-08-19.0, new planet for routing/addresses/Q-IDs, new URL
policy. Map rendering and the search name index stay at March. Cheapest;
everything is scripted and can start as soon as the planet is downloaded.

**B. Full refresh (A + new world tiles + new world.jsonl).** Adds the
tilemaker planet run and the search-cache extraction. Recommended: six
months of OSM edits, and A leaves tiles/search inconsistent with routing.
Building A now and B later means building every region twice.

Recommendation: do the prerequisites and start the planet download + tile
build immediately; run the region queue against the new tiles. If a quick
business-data-only refresh of a few high-traffic regions is wanted first,
run the queue with `--only` and the v2 tiles (that is tier A for those
regions).

## 3. Tooling prepared (all in the repo root / cloud/)

| Script | Purpose |
|---|---|
| `download-planet.sh [YYMMDD]` | resumable planet download + md5 verify → `world-data/planet-2026-08-31.osm.pbf` |
| `build-world-tiles.sh [planet] [out]` | tilemaker planet run with `resources/tilemaker/*` + coastline/landcover, then `cloud/build_search_cache.py` → `search_cache/world-<date>.jsonl` |
| `cloud/build_search_cache.py` | standalone world search-cache extraction (same code path as the world build) |
| `cloud/regions.tsv` | **canonical registry**: 49 regions (48 on archive.org + East Africa) with the bbox each last build actually used, tier, routing smoke pair, search term |
| `extract-region-pbfs.sh` | one osmium pass over the planet producing every regional PBF (`--only`, `--force`; skips extracts newer than the planet) |
| `build-refresh-queue.sh` | per-region: symlink world files → extract PBF if stale → download Overture `$OVERTURE_RELEASE` → `build-region-fast.sh` → gates (terrain coverage, validator, live routing, search, find, browser smoke) → `upload_validated.sh`; `--only/--skip/--tier/--no-upload/--dry-run/--continue`; results in `queue-refresh-<date>.tsv` |
| `build-region-fast.sh` | now honours `OVERTURE_RELEASE` (default unchanged, `2026-04-15.0`) |
| `cloud/pwa_smoke_test.mjs` | `SMOKE_ROUTE="lat,lon;lat,lon"` override so every region gets an in-bbox route pair |

Dry run verified: `PLANET=world-data/planet-2026-03-10.osm.pbf ./build-refresh-queue.sh --dry-run --only silicon-valley,hawaii,himalayas`.

## 4. Prerequisites that need the user (sudo)

```sh
# post-reboot (host rebooted 2026-09-04; only the 16 GB LVM swap is active)
sudo swapon /storage/swapfile && sudo sysctl vm.swappiness=100

# tilemaker v3 build deps (only for tier B)
sudo apt install cmake libboost-all-dev liblua5.1-0-dev librapidjson-dev libshp-dev libsqlite3-dev zlib1g-dev
git clone https://github.com/systemed/tilemaker.git /storage/streetzim/tmp/tilemaker-src
cd /storage/streetzim/tmp/tilemaker-src && mkdir build && cd build && cmake .. && make -j36
# then: TILEMAKER=/storage/streetzim/tmp/tilemaker-src/build/tilemaker ./build-world-tiles.sh

# optional but big win for tilemaker: an NVMe store dir (/data is root-only)
sudo mkdir -p /data/tilemaker-store && sudo chown ot:ot /data/tilemaker-store
#   → STORE=/data/tilemaker-store ./build-world-tiles.sh
```

Disk: 11 TB free on /storage. `tmp/` holds **1.2 TB** of `--keep-temp`
leftovers (`osm_zim_*`) from the May round — delete once confirmed nothing
is running (`rm -rf /storage/streetzim/tmp/osm_zim_*`).

## 5. Time budget

Measured wall-clock from the May round (`*-build.out`, same host, fast path):

| Region | min | Region | min | Region | min |
|---|---|---|---|---|---|
| russia | 1583 | alaska | 196 | florida | 80 |
| south-america | 884 | carolinas | 176 | southern-africa | 60 |
| brazil | 741 | argentina | 136 | turkey | 57 |
| china | 346 | new-york-state | 120 | greater-la | 47 |
| mexico | 286 | east-africa | 102 | nyc-metro / hawaii / south-korea / iceland / caucasus / chicago-metro | 25–33 each |
| indian-subcontinent | 235 | korea-mongolia / pacific-islands / west-africa | 82–89 | | |

Those 24 sum to ~91 h. The other 25 (Europe ~2 days and US ~3 days last
time via the salvage path; Canada, Africa, Australia-NZ, West/Central Asia,
Japan, Southeast Asia, the four US regions, Texas, California, and the small
ones) were built in April on the Mac or on cloud VMs, so no host timings
exist. Expect **2–3 weeks serial** for all 49 with uploads, plus the tile
build. Small regions can run two at a time (125 GB RAM); continents need the
machine to themselves.

Europe and the US previously reused `salvage/*.search-with-overture.jsonl`
to skip the ~24 h Overture merge. Those salvage files carry April Overture
data, so a business-data refresh cannot use them: budget the full merge.

## 6. Suggested sequence

1. Land the routing/build-pipeline branch (bit 9 access flag) so the round carries it.
2. `./download-planet.sh 260831` (≈40 min). In parallel: tilemaker prerequisites.
3. Tier B: `./build-world-tiles.sh` (days). Tier A can skip this.
4. `./extract-region-pbfs.sh` (one planet pass; ≈1–2 h).
5. Pipeline check on small regions without uploading:
   `./build-refresh-queue.sh --tier local --only silicon-valley,washington-dc,hawaii --no-upload`
   (for tier B add `WORLD_MBTILES=… WORLD_SEARCH=…`).
6. Recrawl the URL cache from the first new ZIM:
   `venv-linux/bin/python3 cloud/validate_overture_urls.py --zim osm-silicon-valley-<date>.zim` then `bash cloud/upload_url_cache.sh`.
7. Full run, detached:
   `setsid nohup ./build-refresh-queue.sh --continue > queue-refresh.out 2>&1 < /dev/null &`
   Check progress with `cat queue-refresh-<date>.tsv` and the per-region `*-rebuild-<date>.log`.
8. Site: add `mexico`, `pacific-islands`, `west-africa`, `southern-africa` (live on archive.org but absent from `web/generate.py` REGIONS) and `east-africa` once uploaded; regenerate on the Mac with `--deploy`.

## 7. Decisions needed

- **Himalayas and Central America & Caribbean bboxes are reconstructed** (built on cloud VMs in April; no local record survives). Confirm or correct them in `cloud/regions.tsv` before those two build.
- **East Africa** (`osm-east-africa-2026-05-12.zim`, 4.6 GB) was built but never uploaded — include in this round?
- Tier A vs B (§2), and whether to give tilemaker an NVMe store dir.
- Delete the 1.2 TB of `tmp/osm_zim_*` leftovers.
- Whether to wait for the September Overture release (typically mid-month) before starting the region queue.

## 8. SSD scratch policy (`/mnt/data`)

`/mnt/data` is a 390 GB NVMe volume **shared with another project**
(`st-bridge-models`, 16 GB). `/storage` is a single spinning 15 TB disk,
so the SSD is the only place where the tilemaker store's random I/O is
affordable — but nothing of ours may rest there.

**Rule: the SSD holds exactly one thing, the tilemaker node/way store,
and only while a tile build is running.** Everything else — planet PBFs,
regional extracts, MBTiles, ZIMs, build temp dirs — stays on `/storage`.

Four mechanisms enforce it, covering each other's gaps:

| Mechanism | Covers | Gap it leaves |
|---|---|---|
| `build-world-tiles.sh` EXIT/TERM/INT trap | normal end, failure, `docker stop`, Ctrl-C | not SIGKILL or reboot |
| `flock` on `tmp/.world-tiles.lock` | a second run sharing or deleting the store | — |
| `scripts/nvme-guard.sh` (40 GB reserve) | the store filling the volume under the other project | only while it is running |
| `scripts/ssd-reap.sh` + reap-on-start | SIGKILL, OOM kill, reboot | run it after any hard stop |

Verified 2026-09-05: stopping the build mid-run freed 104 GB and left
`st-bridge-models` untouched; the reaper refuses to delete a live run's
store (exit 1).

**Sizing.** tilemaker reports a *sparse* "Store size" (259–286 GB) that
is much larger than real allocation. Measured real use on this planet:
~73–107 GB during the store-building phases, growing ~4.5 GB/min while a
phase runs. Budget ~150 GB and keep the 40 GB reserve; the guard stops
the build rather than letting the volume fill.

**If the footprint ever needs to be smaller**, in increasing order of
cost:
1. Lower `nvme-guard.sh`'s reserve only if the other project shrinks —
   it is the last line of defence, not a tuning knob.
2. Tile continent-by-continent into one MBTiles with tilemaker's
   `--merge`, so the store only ever holds one continent (~30–60 GB).
   Costs more wall time and needs a per-continent bbox list, but caps
   SSD use and makes a failure cheap to resume.
3. Put `STORE` on `/storage` (`STORE=/storage/streetzim/tmp/tilemaker-store`).
   No SSD use at all, but the store is random-I/O heavy and the HDD is
   already saturated — expect the planet run to take well over a week.

**Housekeeping owed on `/storage`** (not SSD, but the same hygiene): the
May round left **1.2 TB** of `--keep-temp` leftovers in
`tmp/osm_zim_*`. Delete once no build is running:
`rm -rf /storage/streetzim/tmp/osm_zim_*`.

## 9. Operational notes learned during the round (2026-09-05)

**Never edit a shell script that is currently running.** Bash reads a
script incrementally and keeps a byte offset, so rewriting the file under
it makes execution resume at a shifted position — usually a syntax error
somewhere unrelated. `extract-region-pbfs.sh` died with
`line 81: unexpected EOF while looking for matching '"'` after an in-place
patch, though by then all four passes had finished, so nothing was lost.
When a running script must change, write the new content to a temporary
file and `mv` it over: the rename swaps the directory entry while the
running shell keeps reading the old inode.

**Never shrink a running container's memory cap.** See the header of
`build-world-tiles.sh` — `docker update --memory` below current usage
OOM-killed a tile build at 99.6% of its store phases.

**Extraction results.** All 49 regional PBFs were extracted from
`planet-2026-08-31` in four osmium passes, ~5 hours total. Every region
is now a real extract; the previous round left several
(`carolinas`, `alaska`, `hawaii`, `florida`, `mexico`, `russia`, `china`,
and others) as symlinks to a parent region or the planet, which makes
every PBF-touching phase walk the parent — the wikidata Q-ID scan alone
went from minutes to 9+ hours on Brazil in May. Sizes grew as expected
against April (europe 40.67 → 41.81 GB, us 14.52 → 14.98, china 6.16 →
6.48), and `osmium fileinfo` reads cleanly on spot checks.
