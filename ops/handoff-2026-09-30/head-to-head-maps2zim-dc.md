# maps2zim 0.2.1 and StreetZim (d92b187, 82def4f) on Zimfarm: Washington, D.C.

**Who wrote this.** We wrote StreetZim, one of the two tools compared, so we are an interested
party.
- We wrote the plan (`dc-comparison-plan.md`, v2) and fixed it before the runs. It was
  internal: openZIM did not see it, the queries or the metrics beforehand, and nothing external
  proves when they were fixed.
- Everything is published with this report: the plan, every script, the pinned inputs, the raw
  runs and the logs (see Appendix D).

## Summary (one screen)

**What was run.** Local Zimfarm builds of D.C. on a shared 4-core VM, set against openZIM's own
production records for maps2zim (scraper time over 196 regions 48.7–474 min, median 77 min;
the smallest areas 51–119 min; §2). **M-dcseed** is maps2zim 0.2.1 as a *lab control*: its image
is pre-seeded with the 200-tile D.C. cut, so it skips the planet download and scan (§1).
- **First pass** (n = 3, every run **contended**): **S-basic**, StreetZim `d92b187`, default build
  (and **S-ofm**, a lab control on the same tiles; §4).
- **S-full phase** (n = 2, uncontended; §8): StreetZim `82def4f`, `profile=full` (**S-full**:
  Overture, Wikidata, Wikipedia articles, terrain, POI pages; satellite off) and `profile=basic`
  (**S-basic-new**), with M-dcseed rerun. `82def4f` was written after the query set was frozen (§8.3).

Median [min–max]. Two-value M-dcseed cells are "first pass / S-full phase".

| | M-dcseed (non-planet phases only) | S-basic `d92b187` (contended) | S-basic-new | S-full |
|---|---|---|---|---|
| CPU time (primary speed metric) | 68 s / 65 s [63–67] | 153 s | 174 s | **506 s [497–514]** |
| **peak PSS (anon + shmem)** | 0.50 GB | 3.97 GB | 3.94 GB | **4.06 GB** |
| scraper wall | 96 s / 85 s | 168 s | 146 s | **39.1 min [35.5–42.7]** |
| ZIM size | 143.1 MB | 19.9 MB | 18.9 MB | 94.9 MB |
| Kiwix search box, hits of 79 frozen queries: ADM / nbhd / POI / street / address | **4/4** / – / – / – / – (claims ADM only) | 0/4 / 0/15 / 0/25 / 0/20 / 0/20 | 0/4 / 12/15 / 1/25 / 0/20 / 0/20 | **0/4 type-aware** (4/4 by rule) / 13/15 / 25/25 / 1/20 / 0/20 |
| StreetZim in-map search (no maps2zim equivalent) | n/a | 0/4 type-aware / 13/15 / 24/25 / 20/20 / 20/20 | not run | 0/4 type-aware / 12/15 / 25/25 / 20/20 / 20/20 |
| zimcheck (Zimfarm) | fails (4 dangling favicon links) | pass | pass | pass |

**Calls** (plan §8.4: ranges do not overlap and medians differ ≥ 1.5×; at n = 2 non-overlap
arises by chance with probability 1/3, so only the large ratios carry weight):
- **Memory:** StreetZim needs about 8× maps2zim's non-planet phases (S-full 8.1×, S-basic-new 7.9×).
- **CPU:** S-full uses 7.8× M-dcseed's non-planet CPU and 2.9× S-basic-new's.
- **Wall time:** S-full takes 39 min, 28× M-dcseed and 16× S-basic-new, and held its worker
  slot for 37–44 min. 78–85 % of it is idle time in the Wikidata/Wikipedia steps. At least
  21.8 min of that is StreetZim's own fixed 1 s pause after each of 1,308 article requests;
  Wikimedia's responses, through the proxy, took about 0.2 s per article. The pause grows with the number of
  articles. It is a StreetZim design choice, so this time is StreetZim's cost.
- **ZIM size:** S-full is smaller than maps2zim's (143 MB, of which ~110 MB is the same for any
  region). About 55 of S-full's 95 MB (estimate) serves the 103,737 POI pages in Kiwix's search (§8.2).
- S-basic `d92b187` and S-basic-new are **not ranked**: image and host load (PSI 38 vs 1) differ.

**Caveats.** D.C. is StreetZim's most favourable case: maps2zim's production cost is a
per-task planet download and scan, while StreetZim's grows with the region (crossover about
Switzerland for the basic build, indicative; Appendix C). S-full's type-aware ADM score is 0/4:
its "hits" are POIs named after the areas. This report does not show maps2zim's cost or memory
for D.C., significance, larger regions, satellite, or code quality (§9).

---

## 1. Configurations

| id | what | runs on Zimfarm? |
|---|---|---|
| **M-prod** | maps2zim 0.2.1 on production Zimfarm: openZIM's own task records (§2). No D.C. recipe exists. | yes (production) |
| **S-basic** | StreetZim default build at `d92b187`: tilemaker tiles, search, routing; no terrain, Wikidata or satellite. Built through the local Zimfarm. | on a Zimfarm with the streetzim offliner registered (a 7-line patch to `917d7bc`, `repro/zimfarm-vs-917d7bc.diff`); not on production Zimfarm today |
| **M-dcseed** | maps2zim 0.2.1 (pinned digest), **lab control**. The image is pre-seeded with the exact 200-tile D.C. cut of the OpenFreeMap planet at `/tmp/dl/planet.mbtiles`, so it skips the planet download and the 275.7 M-row scan. | no |
| **S-ofm** | StreetZim with `--mbtiles` = the same 200-tile cut. **Lab control**, via a locally edited offliner definition; the definition at `d92b187` has no `mbtiles` flag. | no |
| **S-full** (S-full phase) | StreetZim `next` @ `82def4f`, `profile=full`: tilemaker tiles, search, routing, Overture addresses and places, Wikidata, Wikipedia articles, Copernicus terrain (z8–12), POI pages in Kiwix's search; satellite off (flag omitted). Same area, poly, PBF and resources as S-basic. | on a Zimfarm with the streetzim offliner registered (definition version `82def4f`) |
| **S-basic-new** (S-full phase) | The same `82def4f` image with `profile=basic`: tiles, search and routing only. | as S-full |

**How M-dcseed departs from a real maps2zim task:**
- no planet download;
- a 200-row scan instead of 275.7 M rows;
- `count(*)` over 200 rows;
- an extra image layer (the cut), plus the sandbox CA layer.

Everything else is 0.2.1 unchanged: poly, fonts, Natural Earth, sprites, live GeoNames, styles
and libzim settings. **Its resource numbers are not maps2zim's cost.** They cover its output and
its non-planet phases only.

**Changed in StreetZim since d92b187 (not measured in the first pass).** On `next`, after the tested image:
- search pages are front articles, so Kiwix title suggestions return them;
- `--kiwix-poi-pages` puts every named POI in Kiwix's search (on in the `full` profile, off in
  `basic`);
- `--profile full|basic`, with `full` the default (so "default build" means something else on
  `next` than in the image measured here);
- terrain is on in the `full` profile;
- satellite imagery is opt-in;
- `--mbtiles-url` gives a Zimfarm-runnable tile source.

(The `monaco` area preset is not new: it is already in the d92b187 definition.)

None of these is in the first-pass image, and no first-pass result reflects them. The S-full
phase (§8) measures `82def4f`, which has all of them; satellite stayed off and `--mbtiles-url`
was not used.

## 2. Production records: what maps2zim costs on Zimfarm (M-prod)

These come from `api.farm.openzim.org/v2` (raw: `inputs/prod-maps-durations.json`,
`inputs/prodtask-lux.json`). They are openZIM's own measurements, on its workers.

**Recipes and scraper duration.**
- 207 maps recipes exist; 196 have a succeeded task on `0.2.1` or `dev`.
- Scraper duration (`scraper_started` → `scraper_completed`) over those 196:
  - minimum **48.7 min** (`maps_en_thailand`, `93c21ba2`);
  - median **77.0 min**;
  - maximum **474.1 min** (`maps_en_all`).
- The smallest areas take about an hour or more:
  - Nauru 51.4 min (`ae14a91c`);
  - Andorra 63.4 (`057a3794`);
  - Luxembourg 75.0 (`46f2ea98`);
  - Liechtenstein 119.0 (`04c1e20f`);
  - Monaco 297.6 (`76a6c819`; an outlier, cause unknown).

**One task in full: Luxembourg `46f2ea98-8200-4984-81b0-ccfddbf3acee`.**
- Worker `badger2`, image `ghcr.io/openzim/maps:0.2.1`, `--include-poly`, no `--area`, so it is
  a planet task.
- Scraper 75.0 min. Reserved 3 CPU, 16 GiB, 200 GiB.
- Zimfarm stats:
  - `memory.max` 17,180,577,792 B. That is the limit, reached *including page cache*.
  - CPU max 132 %, average 68.8 %.
- The scan over `tiles_shallow` processed **275,733,728 rows** at about **218 k rows/s**, which
  is about 21 min. It wrote 2,741 tiles.
- ZIM 176,649,821 B, zimcheck 0. Contents: 3,399 `image/webp` (Natural Earth), 2,741 tiles, 768
  glyph files, 354 HTML; no full-text index.

**Not available from production:**
- the phase split (the API keeps only the last 5,000 stdout lines);
- the full logs (S3, 60-day expiry).

**Request to openZIM:** full logs of 3–5 recent maps tasks (small, medium, continent), to split
download, `count(*)`, scan and write time. The local D.C. numbers below never stand in for these,
and **local memory is never compared with production memory.**

## 3. Local D.C. runs: setup

**Zimfarm and worker.**
- openzim/zimfarm `917d7bc` plus the 7-line streetzim patch, dev compose stack (`repro/`).
- Worker offering 4 CPUs, 12 GiB and 2 GiB of disk.

**Recipes.** Every recipe follows `recipesauto/maps.py` (title, description, publisher,
`include-poly`, `monitor: false`, `platform: maps`).
- All variants get the **same resources**: cpu 3 (shares, no quota), memory 12 GiB, disk 2 GiB.
  The disk reservation is not enforced (Appendix B).
- S-basic adds `pbf-url` = the saved Geofabrik `district-of-columbia-260922.osm.pbf` (sha256
  `86d73daf…`), served on the compose network.
- S-ofm also adds `mbtiles`.

**Order.**
- Latin square: r1 M, S, S-ofm; r2 S, S-ofm, M; r3 S-ofm, M, S.
- The first round 1 was voided and rerun after round 3.
- Every task starts network-cold.
- No `drop_caches`.

**Sampler.** `scripts/dc_sampler.py` is external and identical for all variants:
- cgroup v1 `memory.stat` at about 10 Hz;
- CPU and per-process PSS at 1 Hz;
- `rx_bytes` over a 5-min idle baseline;
- disk (workdir + `SizeRw`) every 5 s;
- host PSI, steal and load.

**Contention: every run is contended.** Other jobs shared the 4-core VM throughout: test suites,
type checkers, headless browsers and another StreetZim build.
- Host load reached 20–40, and PSI cpu `some` avg60 was mostly 25–90 %.
- No run met the gate (PSI < 10 % and ≥ 3 idle cores). The gate wait was cut from 20 to 5 min
  after the first task.
- **Every timing is therefore indicative only.** CPU time is the primary speed metric.
- All downloads went through the sandbox's TLS-intercepting egress proxy.

**The tile cut.**
- Source: OpenFreeMap planet `tiles.mbtiles`, build `20260927_080001_pt` (OSM 2026-09-21), read
  through an HTTP-range SQLite VFS.
- Selection: exactly the 200 tiles that maps2zim 0.2.1's own `TileFilter` accepts for the poly
  bbox, computed inside the pinned image, with no margin.
- Copied verbatim, with the original `tile_data_id`s.
- All 200 tiles were verified byte-equal (after gunzip) against OpenFreeMap's tile server.
- `dc-ofm.mbtiles` sha256 `038400d5…8d0b`. M-dcseed and S-ofm have the same decoded MVT tiles.

**Code identity.** `maps2zim/` extracted from `ghcr.io/openzim/maps@sha256:71c9474b…`:
- equals tag `v0.2.1` (`025f879`), except for one build-time download;
- against main `707fc44`, differs only in `__about__` and two favicon hrefs.

## 4. Results

Cells show the median [min–max] over three complete rounds.

The planned rule: a difference is called only when the ranges do not overlap **and** the medians
differ by ≥ 1.5×. With 3 runs against 3, non-overlapping ranges occur by chance with probability
0.10.

**Read M-dcseed as a lab control.** Where the rule says "M-dcseed lower", it compares StreetZim's
whole D.C. build with maps2zim's non-planet phases only.

| metric | M-dcseed (lab control) | S-basic | S-ofm (lab control) | S-basic vs M-dcseed |
|---|---|---|---|---|
| CPU time (primary speed metric) | 68 s [68–69] | 153 s [145–154] | 137 s [133–138] | M-dcseed lower (2.24×); robust (least-contended S-basic run: 2.12×) |
| of which user / sys | 63 / 5 s | 129 / 20 s | 120 / 14 s | M-dcseed lower (2.04× / 4.32×) |
| **peak Pss_Anon + Pss_Shmem** | **0.50 GB [0.50–0.50]** | **3.97 GB [3.96–3.97]** | 3.99 GB [3.99–4.00] | **M-dcseed lower (7.96×)** |
| peak working set (usage − inactive_file) | 0.97 GB [0.97–1.02] | 4.07 GB [4.07–4.08] | 4.07 GB [4.05–4.12] | M-dcseed lower (4.18×) |
| cgroup max_usage (incl. page cache) | 2.95 GB [2.95–3.23] | 5.42 GB [5.33–5.44] | 4.17 GB [4.10–4.37] | M-dcseed lower (1.84×) |
| peak Pss_File | 65.7 MB | 88.9 MB | 89.2 MB | no clear difference (1.35) |
| scraper wall | 96 s [95–99] | 168 s [121–211] | 97 s [80–115] | M-dcseed lower (1.75×) by the rule; **not robust**: the least-contended S-basic run (PSI 6) is 1.26× |
| build time = wall − download | 58 s [56–64] | 120 s [79–152] | 95 s [78–112] | M-dcseed lower (2.06×) by the rule; **not robust**: least-contended 1.36× |
| task wall (requested → succeeded) | 189 s [187–194] | 256 s [254–314] | 186 s [184–252] | no clear difference (1.35) |
| download time | 37 s [35–40] | 48 s [42–58] | 2 s [2–3] | no clear difference (1.32) |
| bytes downloaded | 818 MB | 949 MB | 21 MB | no clear difference (1.16) |
| peak disk (workdir + SizeRw) | 2.34 GB [2.34–2.36] | 1.94 GB [1.80–2.21] | 0.21 GB | no clear difference (1.21) |
| ZIM size | 143.1 MB | 19.9 MB | 32.8 MB | S-basic lower (7.19×); see §5 for what each contains |
| memory.failcnt, OOM | 0 | 0 | 0 | equal |
| host PSI cpu some avg10, run mean (%) | 49.7 [33.8–65.2] | 38.0 [6.1–58.0] | 38.7 [22.6–43.9] | context |
| coverage | tiles: whole poly bbox (incl. parts of MD/VA) | Geofabrik extract clipped to the D.C. polygon | tiles: bbox; search/routing: clipped PBF | |

Notes on the metrics.

**Why wall time is not robust.**
- S-basic's wall tracks host load: 121 s at PSI 6, 168 s at PSI 38, 211 s at PSI 58.
- M-dcseed's is flat (95–99 s at PSI 34–65). It is mostly serial, at 71–88 % of one core.
- Contention therefore penalises the multi-process build, and the wall-time calls rest on it.
- r2 S-basic, the slowest run, was also sampled by three samplers at once (Appendix B).

**Memory: StreetZim needs about 8× more.**
- S-basic peaked at 3.97 GB PSS, against 0.50 GB for maps2zim's non-planet phases.
- The peak comes in short spikes, 5–24 s above 3 GB per run (1 Hz PSS samples). In every
  S-basic run (r1, r2, r3) the spikes fall in the same three phases: OSM data acquisition
  (peak 3.85–3.91 GB), search indexing (3.89–3.97 GB) and routing extraction (3.96–3.97 GB);
  tile generation stays at 1.35 GB and ZIM packing under 0.7 GB. S-ofm, which skips the OSM
  acquisition step, spikes in the last two phases only, in all three rounds
  (`scripts/memory_phases.py`, output in `reader/memory-phases.txt`).
- The same ~4 GB was measured for Luxembourg and Rhode Island (`docs/zimfarm.md`). It is a floor
  for small regions and grows with the region.
- maps2zim's memory during the planet scan was not measured.

**Download.**
- M-dcseed: GeoNames `allCountries.zip` (422 MB), Natural Earth (328 MB) and fonts (61 MB), all
  fetched live on every task.
- S-basic: the water polygons (906 MB), the PBF (21 MB) and Natural Earth (15 MB).
- S-ofm skips the shapefiles.

**Where the time goes.**
- S-basic, r2, by its own step timer: search index 47 s, ZIM packing 42 s, routing 29 s,
  tilemaker 17 s, PBF 16 s.
- M-dcseed, r2, 96 s in all:
  - fonts 4 s;
  - Natural Earth 11 s;
  - GeoNames download 18 s, unzip 10 s and scan 42 s;
  - `count(*)` and the tile write 6 s;
  - finalising 4 s.

  In production, the planet download, `count(*)` and the scan come on top.

Every raw run and Zimfarm's own figures (which include page cache and the image, and are never
ranked) are in Appendix A.

## 5. What each ZIM contains

| feature | M-dcseed (maps2zim 0.2.1) | S-basic | S-ofm |
|---|---|---|---|
| vector tiles | OpenFreeMap, 200 (z0–14), ~30 MB stored | tilemaker, 139 stored (61 empty skipped), ~8 MB | OpenFreeMap, the same 200 decoded MVT, ~20 MB |
| fonts (glyph PBFs) | Noto Sans, 3 stacks, 104 MB uncompressed (~80 MB stored, est.); broad script coverage | Open Sans, 3 stacks, 1.2 MB; narrower script coverage likely (not tested) | as S-basic |
| Natural Earth raster (world, fixed) | yes: 5,461 WebP (~32 MB stored, est.); the ZIM Counter reports 3,399, as for production Luxembourg | no | no |
| sprites / POI icons, dark style | yes | no | no |
| Kiwix title-index search | GeoNames ADM1–4 with a point in the bbox: 4 entries (2 in Virginia) | **none in the tested image** (search pages not front articles; fixed since, not measured) | none |
| full-text index | no | yes, 482 search pages | yes, 1,849 pages |
| in-map search / chips / `places.html` | no | yes: 156,830 features | yes: 173,341 features |
| routing | no | yes (12 MB graph, ~5 MB stored) | yes |
| zimcheck `--all` (local dev Zimfarm) and local `-A` | fails on four dangling `..//favicon.ico` links in the four `search/` pages (cosmetic; the href is fixed on main `707fc44`); production Zimfarm reports check_result 0 for 0.2.1 builds (e.g. Luxembourg), which presumably have the same href, so production likely runs zimcheck with different options than the dev stack's `--all` (not verified) | pass (1 "redundant data" warning) | pass (same warning) |

**Fixed vs region-dependent size.**
- In maps2zim's ZIM, about 110 of 143 MB is the same for any region: glyphs (~80 MB), Natural
  Earth (~32 MB), assets and sprites. Only the tiles (~30 MB) and 4 search pages depend on D.C.
- In StreetZim's ZIM, about 1 MB is fixed (viewer, glyphs, routing code), and about 19 MB depends
  on the region (tiles, search data, routing graph).
- The stored-size split is an estimate: zstd-19 recompression per path segment, scaled to the
  file size. Read it as ±30 %.
- Neither tool's ZIM is byte-reproducible across rounds.

**Tile source (S-basic vs S-ofm).** With OpenFreeMap's 200 tiles instead of tilemaker's:
- the ZIM grows from 19.9 to 32.8 MB;
- the search index grows from 156,830 to 173,341 features.

The comparison is confounded by coverage (the OFM tiles cover the whole bbox) and by date.
`tools/compare_tile_sources.py` was not run.

## 6. Search

79 queries were built by rule before any ZIM was read (`scripts/query_build.py`, seed 20260929).
The query set was frozen in `reader/queries.json` at 19:26 UTC. The parsers were written later,
during round 2 (Appendix B).

| stratum | n | source of query and expected coordinate | tolerance |
|---|---|---|---|
| ADM | 4 (all in bbox) | GeoNames ADM1–4; coordinate from Wikidata P625 via P1566 | max(2 km, area-equivalent radius from P2046): 9.1, 9.1, 4.6, 3.6 km |
| neighbourhood | 15 | Wikidata P31/P279* Q123705 in the bbox | 1.5 km |
| POI | 25 | Wikidata, stratified over 7 types | 250 m (500 m for parks and universities) |
| street | 20 | DC Open Data (DDOT Roadway Block), "Name Type" | 100 m from the block geometry |
| address | 20 | DC Master Address Repository, "Number Name Type" | 75 m |

- Every item had to exist in both inputs: the PBF, and either the OFM cut or GeoNames.
- 248 candidates were examined and dropped; they are listed with the reason.
- Of the dropped addresses, 177 were in the PBF but had no OFM house number within 100 m. The
  address results therefore apply to addresses present in both inputs.
- A hit is any of the top 5 results within tolerance.

| stratum | maps2zim claims it? | M-dcseed Kiwix suggest | S-basic Kiwix suggest | S-basic Kiwix full-text (extra, post hoc) | S-basic in-map (StreetZim only) | S-ofm in-map |
|---|---|---|---|---|---|---|
| ADM | yes | **4/4** | **0/4** | 1/4 | 2/4 by the rule, **0/4 type-aware** | 3/4 by the rule, 0/4 type-aware |
| neighbourhood | no | n/a | 0/15 | 12/15 | 13/15 | 15/15 |
| POI | no | n/a | 0/25 | 2/25 | 24/25 | 25/25 |
| street | no | n/a | 0/20 | 0/20 | 20/20 | 19/20 |
| address | no | n/a | 0/20 | 0/20 | 20/20 | 20/20 |

maps2zim returned nothing in the unclaimed strata (0 of 75). Those strata are outside its design,
not failures.

**Kiwix title suggestions (the planned measure for both tools).**
- maps2zim answers its claimed stratum, ADM, 4/4. The points are 29–2,071 m from Wikidata's.
- **StreetZim d92b187 returned no location for any of the 79 queries through Kiwix's own search
  box.** The only suggestion was the main page, for "District of Columbia".
- Cause: its 482 `search/` pages are not front articles, so libzim's title index does not return
  them.
- This is a StreetZim defect in the image tested. It is fixed on `next` since, and that fix is
  not measured here.

**Kiwix full-text (an extra, added after seeing the above).** StreetZim's full-text index returns:
- 12/15 neighbourhoods;
- 2/25 POIs (two metro stations, matched by the neighbourhood pages of the same name);
- 1/4 ADM (the place "Washington", 27 m).

It covers only the 482 "notable" features that have pages, and no streets or addresses.

**StreetZim in-map search (StreetZim-only; n/a for maps2zim).**
- Scores: 20/20 streets, 20/20 addresses, 24/25 POIs, 13/15 neighbourhoods. Median latency from
  the last keystroke to a settled list was about 215 ms.
- **StreetZim d92b187 has no admin-area search.** Its two ADM "hits" under the rule are POIs whose
  names contain the query: "First Baptist Church of the City of Washington", 1.5 km from the
  expected point, and a POI named "District of Columbia", 4.1 km away.
- All four ADM entities are in its input PBF as boundary relations, but d92b187 does not index
  boundaries. Type-aware, it scores 0/4.
- A tighter distance tolerance would not fix the rule. At a flat 2 km, StreetZim still scores 1/4
  (the church), and maps2zim drops to 3/4, because GeoNames' "City of Washington" point is 2.07 km
  from Wikidata's.
- The next version will require an ADM hit to be an administrative or place result.

Other misses:
- "Greater U Street Historic District": no result.
- Fort Dupont: found 1.86 km away, against a 1.5 km tolerance.
- Grace Episcopal Church: a different church of that name, 7.4 km away.
- S-ofm: Varnum Street, nearest result 189 m away.

## 7. Rendering

**Setup.** The same view (38.8951, −77.0364) at z14, rendered with:
- kiwix-serve 3.8.2;
- Chrome 131 headless with swiftshader, 1280×800;
- a fresh incognito context per load;
- 5 loads per ZIM, interleaved.

The results are relative only.

| ZIM | animation (nav → camera at target), ms | render (camera → idle, all tiles loaded), ms |
|---|---|---|
| M-dcseed | 3189 [2866–3529] | 2679 [2240–3721] |
| S-basic | 2990 [2731–3350] | 2705 [2431–3234] |
| S-ofm | 3374 [3300–4300] | 2761 [2516–4732] |

- No clear difference under the rule.
- **z12 is withheld.** Both viewers clamp the view near z12 (maps2zim also clamps the zoom to
  12.06), so the planned end point, the camera within ε, never occurs. A relaxed end point gave
  inconsistent results in two attempts.
- **The stable-screenshot time is withheld.** For S-basic, two identical frames occur (median 1.3 s)
  before its tiles are idle (2.7 s). The metric does not measure a finished render. By the rule it
  would have favoured StreetZim.
- The raw data for both is in `reader/`, and the screenshots are in `screenshots/`.

## 8. S-full phase: StreetZim `82def4f`

This phase measures the current StreetZim (`next` after the first pass), in both profiles, with
maps2zim's lab control rerun alongside. Its results are in `sfull/`.

### 8.1 Setup

**Image.**
- Built from StreetZim `next` @ `82def4f489d810e95c2447ac74d352c2b0cd3a52` (2026-09-29 21:47 UTC).
  `df07556` adds only runbook documents on top; `82def4f` was built.
- The repository's Dockerfile was used unchanged, on the same CA-shadowed base images as the
  first pass (`repro/cashim.Dockerfile`). Build log: `sfull/build-82def4f.log`.
- Tag `localhost:5000/openzim/streetzim:82def4f`, manifest digest
  `sha256:a60ef98bdd976f3990f0aba880b65cf4b3b40a4bd0e06332987bd6cf00885527`, 1.68 GB on disk
  (422 MB content).

**Offliner definition.** Registered as version `82def4f`
(`repro/streetzim-offliner-definition-82def4f.json`).
- `profile` is required (`full` | `basic`).
- `wikidata`, `wikipedia`, `overture` and `terrain` are `on`/`off` enums; unset means the
  profile's default.
- `satellite` is a boolean, not an enum. It was omitted, so it is off; the logs never mention
  satellite.

**Recipes.** Identical to S-basic's except for the name, the image tag and `profile`:
- the same poly, the same PBF from `dc_pbfhost`, and the same resources (cpu 3, 12 GiB, 2 GiB
  disk);
- Zimfarm generated `streetzim … --include-poly=… --pbf-url=… --profile=full` (or `basic`);
- M-dcseed: the recipe and image are unchanged from the first pass.

**Order and measurement.**
- r1: M-dcseed, S-full, S-basic-new. r2: S-full, S-basic-new, M-dcseed.
- These are two rows of the 3×3 Latin square. The third row was not run, so positions are not
  balanced.
- The driver, sampler, gate and summariser are the first pass's. `dc_task.py` carries the sampler
  fix (Appendix B).
- Every task started network-cold, including S-full's Wikimedia, Overture and DEM caches.

**Load.**
- Every run passed the gate at its first check (PSI avg60 0.09–0.87 %, 3.86–3.94 idle cores).
- The run-mean PSI avg10 was 0.2–1.3 %, and steal was 0.1–0.6 %. The only excursions (avg10
  up to 21 % for about 60 s) came during S-full's own ZIM write (step 9b), when the scraper
  itself used more than two cores.
- The first pass's other jobs were not running.
- **These runs are uncontended, so their wall times are not comparable with the first pass's.**
- Zimfarm's own containers used 129–148 s of CPU during each S-full run and 7–8 s during each
  S-basic-new run, mostly the backend (92–107 s), at about 0.04 core for as long as a task runs.
  This is not counted in the variants' CPU.

### 8.2 Results

Median [min–max] over n = 2. Calls use the same rule as §4. With 2 runs against 2, non-overlapping
ranges occur by chance with probability 1/3, so a call here is weaker evidence than at n = 3.

| metric | M-dcseed (lab control) | S-basic-new | S-full | S-full vs M-dcseed | S-full vs S-basic-new | S-basic-new vs M-dcseed |
|---|---|---|---|---|---|---|
| CPU time (primary) | 65 s [63–67] | 174 s [166–182] | 506 s [497–514] | M-dcseed lower (7.76×) | S-basic-new lower (2.91×) | M-dcseed lower (2.67×) |
| of which user / sys | 60 / 5 s | 131 / 43 s | 425 / 80 s | 7.05× / 16.7× | 3.24× / 1.88× | 2.18× / 8.86× |
| peak Pss_Anon + Pss_Shmem | 0.50 GB [0.50–0.50] | 3.94 GB [3.93–3.94] | 4.06 GB [4.06–4.06] | M-dcseed lower (8.14×) | no clear difference (1.03) | M-dcseed lower (7.90×) |
| peak working set | 0.97 GB | 4.07 GB | 4.17 GB [4.16–4.17] | M-dcseed lower (4.28×) | no clear difference (1.02) | M-dcseed lower (4.17×) |
| cgroup max_usage (incl. page cache) | 2.95 GB | 5.42 GB | 5.59 GB [5.57–5.61] | M-dcseed lower (1.89×) | no clear difference (1.03) | M-dcseed lower (1.84×) |
| peak Pss_File | 66 MB | 90 MB | 178 MB | M-dcseed lower (2.69×) | S-basic-new lower (1.99×) | no clear difference (1.36) |
| scraper wall | 85 s [84–85] | 146 s [141–151] | 2,348 s [2,132–2,564] | M-dcseed lower (27.7×) | S-basic-new lower (16.1×) | M-dcseed lower (1.72×) |
| task wall | 192 s [191–192] | 254 s [253–254] | 2,450 s [2,240–2,659] | M-dcseed lower (12.8×) | S-basic-new lower (9.66×) | no clear difference (1.32) |
| bytes downloaded | 818 MB | 950 MB | 1,408 MB [1,407–1,410] | M-dcseed lower (1.72×) | no clear difference (1.48) | no clear difference (1.16) |
| peak disk (workdir + SizeRw) | 2.47 GB [2.39–2.56] | 1.85 GB [1.82–1.88] | 2.17 GB [2.16–2.17] | no clear difference (1.14) | no clear difference (1.17) | no clear difference (1.34) |
| ZIM size | 143.1 MB | 18.9 MB | 94.9 MB | S-full lower (1.51×) | S-basic-new lower (5.02×) | S-basic-new lower (7.57×) |
| memory.failcnt, OOM | 0 | 0 | 0 | equal | equal | equal |
| host PSI avg10, run mean (%) | 0.2 | 1.2 [1.0–1.3] | 0.6 | context | context | context |

The control reproduced its first-pass numbers: CPU 65 s against 68 s, and the same 0.50 GB PSS
and 143.1 MB ZIM. Its wall was 85 s against 96 s. The two phases differ in host load (PSI 0.2
against 50), the likely cause, but this is a cross-phase comparison and is not tested.

The first pass's "download time" and "build time" rows are not reported for this phase. The
metric counts every second of network traffic above the idle baseline, so S-full's Wikimedia
requests make it 1,880–1,925 s of "download" and it no longer separates download from build.

**Where S-full's time goes** (StreetZim's step markers in the scraper log; wall and CPU per
step from the sampler). Step 9a runs from `bundle-wiki-articles: 1308 distinct titles` to
`stored 1290 articles`. `sfull/phases.txt` ended it at the last progress line (1,250/1,308)
instead, which moved about 70 s of fetching into 9b; the figures below are corrected.

| step | r1 wall / CPU | r2 wall / CPU |
|---|---|---|
| 0: downloads before step 1 (poly, PBF, Overture, water polygons, Natural Earth) | 65 / 24 s | 145 / 26 s |
| 1–4: OSM, tiles, search index | 90 / 103 s | 84 / 97 s |
| 5: Wikidata properties (1,991 Q-IDs) | 83 / 7 s | 84 / 8 s |
| 5: Wikipedia extracts (1,597 titles, 80 requests) | **526 / 0 s** | 46 / 0 s |
| 6–8: routing, terrain, MapLibre check | 40 / 26 s | 38 / 25 s |
| 9: ZIM build, before the article fetch | 11 / 17 s | 13 / 19 s |
| 9a: Wikipedia article fetch (1,308 titles) | **1,614 / 43 s** | **1,592 / 46 s** |
| 9b: ZIM build after the articles | 136 / 295 s | 131 / 275 s |

- **Idle time in steps 5 and 9a (wall − CPU):** 2,172 s in r1 and 1,667 s in r2, that is 85 %
  and 78 % of the scraper wall.
- **Most of it is StreetZim's own pause, not Wikimedia.** The article fetch is serial, and
  `cloud/wikimedia_http.py`'s `Pacer` (called from `cloud/wiki_articles.py`) waits a fixed 1.0 s
  after every response before the next request. For 1,308 requests that is a floor of 1,307 s
  (21.8 min). Step 5 adds fixed pauses of about 1 min (1.0 s per Wikidata batch of 40, 0.2 s per
  extracts batch of 20). The rest of step 9a's idle time, 264 s (r1) and 239 s (r2), about 0.2 s
  per article, is Wikimedia's response time plus the network and the sandbox's proxy.
- The floor grows linearly with the number of articles: 1 s per title, whatever the area.
- **The r1 extracts time is unexplained.** The extracts took 526 s in r1 and 46 s in r2, for
  the same 80 batch requests and the same 1,527 of 1,597 extracts. The code logs a warning when
  a request fails or passes its 30 s timeout, and none was logged, so every request was answered,
  at about 6.6 s per request in r1 against 0.6 s in r2. Whether the delay was at Wikimedia, in
  the sandbox's TLS-intercepting proxy or elsewhere was not measured.
- Without steps 5 and 9a, S-full's wall is 341 s (r1) and 410 s (r2). r2's higher figure is its
  slower water-polygon download (step 0: 145 s against 65 s).
- **No request was rate-limited.** Both runs report `0 rate-limited` for the articles, no
  `Warning:` lines for Wikidata or the extracts, and no 429 anywhere in the logs. The expected
  rate limiting of this IP did not show in these two runs.
- CPU is the robust figure. Step 9b (295 / 275 s of CPU in about 133 s, about 2.1–2.2 cores) is
  where the 103,737 POI pages, the articles and the terrain are written. It accounts for most of
  S-full's extra CPU over S-basic-new.

**Memory.** S-full's peaks fall in the same three steps as S-basic's, with similar values; the
routing step is about 0.1 GB higher (4.06 GB against 3.96–3.97 GB) (`sfull/memory-phases.txt`):
- OSM acquisition 3.87–3.94 GB;
- search index 3.96–4.00 GB;
- routing 4.06 GB.

The Wikidata, terrain and ZIM steps stay under 1 GB. The full profile adds about 0.1 GB to the
peak.

**The `streetzim: sources:` line, per run.**

| run | sources |
|---|---|
| r1-S-full, r2-S-full (identical) | Overture addresses 634364 rows (250477 added); Overture places 811 enriched, 89277 added; Wikipedia titles 490/804 Q-IDs resolved; Wikidata 1991 entries; Copernicus DEM 4 GLO-30 + 5 GLO-90 downloaded (193.3 MB), 0 cached, 0 sea; Wikipedia articles 1290/1308 (0 not fetched, 0 rate-limited) |
| r1-S-basic-new, r2-S-basic-new | none besides OSM |

**Coverage per run** (the same in r1 and r2):

| source | covered | not covered |
|---|---|---|
| Wikidata → enwiki title | 490 of 804 Q-IDs (61 %) | 314. Deterministic across the runs, which suggests missing enwiki sitelinks rather than failures, but this is not verified |
| Wikidata properties | 1,991 Q-IDs; 1,395 inside the bbox were packed | — |
| Wikipedia extracts | 1,527 of 1,597 (95.6 %) | 70, with no warning logged |
| Wikipedia articles | 1,290 of 1,308 (98.6 %), 12.8 MB, no images | 18 unavailable (11 enwiki disambiguation pages); 0 unfetched, 0 rate-limited |
| Overture | 634,364 address rows (250,477 added); 811 places enriched, 89,277 added | — |
| Copernicus DEM | 9 cells: 4 GLO-30 and 5 GLO-90, **193.3 MB (184.4 MiB) downloaded per run**, 0 cached | — |

The terrain was audited against the DEM ("Terrain audit passed"), and 26 terrain tiles
(z8–12) were stored.

**What the ZIM contains** (r2, `sfull/inventory/`):

| path | S-basic-new | S-full |
|---|---|---|
| `search/` pages (Kiwix-searchable) | 482 | 103,737 (POI pages on in `full`) |
| `wiki-article/` | 0 | 1,290 |
| `wikidata/` chunks | 0 | 89 |
| `terrain/` | 0 | 26 |
| `tiles/` | 139 | 139 |
| entries in all | 1,938 | 107,221 |

**Where S-full's 94.9 MB goes** (r2; S-basic-new's 18.9 MB in brackets). Uncompressed clusters
and the directory are measured from the ZIM's own cluster table; compressed segments are split
by entry count within each cluster, so those are approximate:
- **Kiwix search for the POI pages, about 55 MB:** libzim's title index 22.9 MB [0.2] and
  full-text index 16.6 MB [0.2] (Xapian, stored uncompressed), the 103,737 `search/` pages about
  7.8 MB [0.1], and the directory and pointer lists for 107,221 entries 9.3 MB [0.2]. The
  full-text index also covers the articles.
- **In-map search data, about 17–21 MB [4–5]:** it grows with Overture's 250,477 added addresses and
  89,277 added places (496,584 search features).
- **Unchanged from `basic`:** tiles 7.0 MB, routing 3.9 MB, fonts, viewer and other files about
  2 MB.
- **Wikipedia articles** about 2.6–2.9 MB (1,290 pages, text only), **Wikidata** 0.1 MB, **terrain**
  0.3 MB (26 WebP tiles).

So terrain and Wikidata cost almost nothing in size; the POI pages in Kiwix's search cost most of
it. S-full is still smaller than maps2zim's ZIM because about 110 of maps2zim's 143 MB is the
same for any region (glyphs and Natural Earth, §5). Method and raw output: the review's
`inv-*.json` and `zimclusters.py`, to be added to `sfull/inventory/`.

Both ZIMs passed Zimfarm's zimcheck (result 0). M-dcseed failed it again (result 1) on the
same dangling `favicon.ico` links.

### 8.3 Reader check (S-full, final round)

**This check is not blind.** The query set was frozen at 19:26 UTC on 2026-09-29. The StreetZim
changes it measures were committed afterwards by the same party: `--kiwix-poi-pages` at 19:44,
"search pages are front articles" at 19:54, and `82def4f` at 21:47. The first-pass reader test runs
started at 20:09, so these changes came before the first-pass search results but after the
queries existed. No query string from the set appears in the `d92b187..82def4f` diff, except
"District of Columbia" as the area's title. A reader should still treat S-full's search scores
as those of a tool whose authors knew the test.

The same frozen 79 queries (`reader/queries.json`), parsers and tolerances as §6, on r2's ZIMs:
- **S-full:** sha256 `455b8679…`. The recorded run was repeated after the container restart
  and gave identical hits (`sfull/reader/verify/`).
- **S-basic-new:** Kiwix search only.
- **M-dcseed:** Kiwix search, rerun as a control.

| stratum | M-dcseed Kiwix suggest | S-basic `d92b187` Kiwix suggest (historical) | S-basic-new Kiwix suggest | S-full Kiwix suggest | S-full Kiwix full-text | S-full in-map | S-basic `d92b187` in-map (historical) |
|---|---|---|---|---|---|---|---|
| ADM | 4/4 | 0/4 | 0/4 | **0/4 type-aware** (4/4 by the rule) | **0/4 type-aware** (4/4 by the rule) | **0/4 type-aware** (4/4 by the rule) | **0/4 type-aware** (2/4 by the rule) |
| neighbourhood | n/a | 0/15 | 12/15 | 13/15 | 13/15 | 12/15 | 13/15 |
| POI | n/a | 0/25 | 1/25 | **25/25** | 24/25 | 25/25 | 24/25 |
| street | n/a | 0/20 | 0/20 | 1/20 | 1/20 | 20/20 | 20/20 |
| address | n/a | 0/20 | 0/20 | 0/20 | 0/20 | 20/20 | 20/20 |

**Kiwix's search box now returns places.**
- `search/` pages are front articles in `82def4f`, so libzim's title index returns them.
- S-full's suggestions for "Lincoln" are Lincoln, Lincoln, Lincoln Building, Lincoln Cleaners,
  … Lincoln Memorial (tenth) (`S-full-kiwix-search-Lincoln.png`).
- Median suggest latency was 0.8–4.9 ms per stratum.
- In S-basic-new, only the 482 notable-place pages are searchable. That gives 12/15
  neighbourhoods and 1/25 POIs.
- In S-full, every named POI has a page, which gives 25/25 POIs (24/25 at the first suggestion).
  The POI queries are exact Wikidata labels of places chosen to be present in the PBF (§6), so
  this is recall for known names, not general search quality. In 8 of the 25, the top five also
  hold a same-named page more than 1 km away (duplicates from OSM and Overture), for example two
  of the four "National Museum of African American History and Culture" pages.
- Neither profile makes streets or addresses searchable in Kiwix. S-full's one street hit is
  Dalecarlia Parkway, which has a POI page of the same name (16 m).

**ADM is still not an admin-area search: 0/4 type-aware.** All four of S-full's ADM "hits" under
the rule are POIs named after the area. "City of Washington, D C" is a "public service and
government" POI, 8.1 km from Wikidata's point. "City of Alexandria" is a town-hall POI.
"Arlington County" is two government-office POIs (the first suggestion, 4.6 km away, misses).
"District of Columbia" is a generic POI, a "topic publisher" (a social-media account) and a
government office, all within tolerance. None is a boundary, so type-aware it scores 0/4, as in
the first pass. maps2zim answers 4/4 from GeoNames, and the control rerun gave the
same result.

**In-map search.**
- Scores: 20/20 streets, 20/20 addresses, 25/25 POIs, 12/15 neighbourhoods.
- Median latency, last keystroke to a settled list: 213–307 ms per stratum, and 463 ms for ADM
  (n = 4).
- Neighbourhood misses:
  - Greater U Street Historic District: no result, as in `d92b187`.
  - Fort Dupont: 1.86 km, against a 1.5 km tolerance, as in `d92b187`.
  - Woodland: the "Woodland" place is 10.3 km from the expected point in both builds.
    `d92b187` scored a hit only through the street "Woodland Drive Northwest" (315 m). In S-full,
    added Overture POIs push that street out of the top 5.
- S-basic-new's in-map search was not run.

**Screenshots** (`sfull/screenshots-full/` holds five of them; the Kiwix-search, in-map search,
museums-chip and z12 shots must be copied there from the build host before publication):
- the z12 and z14 views;
- hillshade and **3D terrain** (pitch 60, `S-full-3d-terrain.png`);
- the Wiki panel (`S-full-wiki-panel.png`: "Washington, D.C. — capital city of the United
  States", artworks and memorials near the Mall);
- a stored Wikipedia article (`S-full-wikipedia-article.png`: "Lincoln Memorial", with no
  images);
- the museums chip (230 cards);
- in-map and Kiwix searches.

## 9. What this does not show

- **Not maps2zim's cost for D.C.** M-dcseed skips the planet download and scan, which dominate the
  real cost. The real cost is the production record (§2).
- **Not maps2zim's memory during the planet scan**, locally or in production.
- **No local-vs-production memory comparison**, and no ranking on Zimfarm's own resource figures.
- **No significance.** n = 3 per variant on a contended machine (first pass); n = 2 on an
  uncontended one (S-full phase). The timing calls are indicative, and the two phases' wall times
  are not comparable.
- **Not Wikimedia's behaviour in general.** No request was rate-limited in the two S-full runs.
  A rate-limited run would take longer and cover less; no wait-budget message was logged. The
  fixed 1 s pause per article applies whatever Wikimedia does.
- **Nothing about larger regions**, beyond the indicative table in Appendix C. Nothing about
  continents or the planet for StreetZim.
- **Not StreetZim after `82def4f`.** The first pass (§4–§7) covers `d92b187` only. The S-full
  phase (§8) covers `82def4f` in both profiles, with n = 2, satellite off, and no render timing.
- **Not code quality, maintainability, or fit with openZIM's stack.** Not user preference: styling
  (icons, dark mode, glyph coverage) differs and is not scored.
- **Not search quality in general.** The check is one rule-built set of 79 queries. maps2zim
  claims admin-area search only.

---

## Appendix A. Raw runs and Zimfarm's figures

| run | scraper wall | task wall | CPU | download | build | peak PSS (anon+shmem) | peak working set | peak disk | ZIM | Zimfarm check | PSI avg10 mean | steal % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| r1-M-dcseed | 95 s | 189 s | 68 s | 37 s | 58 s | 0.50 GB | 0.97 GB | 2.34 GB | 143.1 MB | 1 | 49.7 | 1.49 |
| r1-S-basic | 168 s | 254 s | 153 s | 48 s | 120 s | 3.97 GB | 4.07 GB | 1.80 GB | 19.9 MB | 0 | 38.0 | 1.24 |
| r1-S-ofm | 80 s | 186 s | 133 s | 2 s | 78 s | 4.00 GB | 4.05 GB | 0.19 GB | 32.8 MB | 0 | 22.6 | 1.08 |
| r2-M-dcseed | 99 s | 194 s | 69 s | 35 s | 64 s | 0.50 GB | 1.02 GB | 2.34 GB | 143.1 MB | 1 | 65.2 | 1.41 |
| r2-S-basic † | 211 s | 314 s | 154 s | 58 s | 152 s | 3.97 GB | 4.08 GB | 2.21 GB | 19.9 MB | 0 | 58.0 | 1.08 |
| r2-S-ofm | 97 s | 184 s | 137 s | 2 s | 95 s | 3.99 GB | 4.07 GB | 0.21 GB | 32.8 MB | 0 | 38.7 | 1.24 |
| r3-M-dcseed | 96 s | 187 s | 68 s | 40 s | 56 s | 0.50 GB | 0.97 GB | 2.36 GB | 143.1 MB | 1 | 33.8 | 1.38 |
| r3-S-basic | 121 s | 256 s | 145 s | 42 s | 79 s | 3.96 GB | 4.07 GB | 1.94 GB | 19.9 MB | 0 | 6.1 | 1.01 |
| r3-S-ofm | 115 s | 252 s | 138 s | 3 s | 112 s | 3.99 GB | 4.12 GB | 0.21 GB | 32.8 MB | 0 | 43.9 | 1.17 |

† Sampled by three samplers at once (Appendix B).

**Zimfarm's own figures.** These are never ranked: memory includes page cache, and disk adds the
image. The coarse sampler also misses StreetZim's short memory spikes; r3 S-basic shows 1.72 GB.

| run | memory max | CPU max % | CPU avg % | disk max (incl. image) |
|---|---|---|---|---|
| r1-M-dcseed | 2.53 GB | 97 | 71 | 6.03 GB |
| r1-S-basic | 2.72 GB | 214 | 98 | 4.00 GB |
| r1-S-ofm | 0.82 GB | 170 | 98 | 1.52 GB |
| r2-M-dcseed | 2.81 GB | 97 | 72 | 6.03 GB |
| r2-S-basic | 4.72 GB | 96 | 94 | 4.09 GB |
| r2-S-ofm | 0.81 GB | 151 | 94 | 1.38 GB |
| r3-M-dcseed | 2.53 GB | 98 | 88 | 6.03 GB |
| r3-S-basic | 1.72 GB | 281 | 100 | 3.87 GB |
| r3-S-ofm | 0.98 GB | 88 | 81 | 1.42 GB |

The per-path-segment ZIM inventories (items, uncompressed and estimated stored size) are in
`tables.md` and `inventory/*.json`.

**S-full phase (§8; uncontended; in run order).**

| run | scraper wall | task wall | CPU | peak PSS (anon+shmem) | peak working set | peak disk | ZIM | Zimfarm check | PSI avg10 mean | steal % | min host free |
|---|---|---|---|---|---|---|---|---|---|---|---|
| r1-M-dcseed | 84 s | 191 s | 67 s | 0.50 GB | 0.97 GB | 2.56 GB | 143.1 MB | 1 | 0.2 | 0.44 | **2.67 GB** |
| r1-S-full | 2564 s | 2659 s | 514 s | 4.06 GB | 4.17 GB | 2.16 GB | 94.9 MB | 0 | 0.6 | 0.3 | 3.21 GB |
| r1-S-basic-new | 151 s | 254 s | 182 s | 3.93 GB | 4.07 GB | 1.88 GB | 18.9 MB | 0 | 1.3 | 0.6 | 4.66 GB |
| r2-S-full | 2132 s | 2240 s | 497 s | 4.06 GB | 4.16 GB | 2.17 GB | 94.9 MB | 0 | 0.6 | 0.3 | 4.31 GB |
| r2-S-basic-new | 141 s | 253 s | 166 s | 3.94 GB | 4.07 GB | 1.82 GB | 18.9 MB | 0 | 1.0 | 0.23 | 4.60 GB |
| r2-M-dcseed | 85 s | 192 s | 63 s | 0.50 GB | 0.98 GB | 2.39 GB | 143.1 MB | 1 | 0.2 | 0.12 | 4.01 GB |

Task ids: r1 `81ead9bd`, `1c5f235b`, `9d439732`; r2 `12a6a7fd`, `3c5f2c36`, `5f22c2a8`.
ZIM sha256 values are in each run's `meta.json`; the S-full ZIM kept is r2's, `455b8679…`.

| run | Zimfarm memory max | CPU max % | CPU avg % | disk max (incl. image) |
|---|---|---|---|---|
| r1-M-dcseed | 2.53 GB | 99 | 98 | 6.03 GB |
| r1-S-full | 2.95 GB | 335 | 72 | 5.52 GB |
| r1-S-basic-new | 2.74 GB | 370 | 101 | 4.12 GB |
| r2-S-full | 4.84 GB | 317 | 77 | 5.37 GB |
| r2-S-basic-new | 2.74 GB | 232 | 101 | 4.25 GB |
| r2-M-dcseed | 2.53 GB | 101 | 99 | 6.03 GB |

## Appendix B. Deviations from the plan

| item | plan | done | why |
|---|---|---|---|
| sharing | share the plan with openZIM first | not shared | our decision for this first pass |
| `mbtiles` flag | optional | not added; S-ofm is a lab control via a local definition version `d92b187-ofm` | our decision |
| rounds | n = 3, complete rounds only | 3 complete rounds, 9 tasks | — |
| first round 1 | — | **voided**. S-basic's request got HTTP 400 twice: the recipe's disk reservation exceeded the worker's offer, which we had just lowered. The round's **successful** M-dcseed task (`0cfbf87e`) was discarded with it; its ZIM hash is in `runs/deleted-zims-SHA256SUMS`. Round 1 was rerun after round 3, in the same order. | disk |
| **three samplers on r2 S-basic** | one sampler per task | Each failed round-1 S-basic attempt had already started its sampler, and the driver did not stop it. Both orphans attached to the next scraper container, r2 S-basic, which was sampled three times over (`smaps_rollup` 1 Hz, `memory.stat` 10 Hz, ×3; evidence in `runs/void-round1/`). r2 S-basic is the slowest S-basic run and also had the highest host load; the two effects cannot be separated. `dc_task.py` now kills its sampler when the request fails (changed after the rounds, before S-full). | driver bug |
| load gate | wait up to 20 min, then flag `contended` | 20 min for the first task, then 5 min (a `dc_task.py` change at 19:28, after the 19:26 freeze); no run met the gate | other jobs kept the host at load 20–40 |
| recipe disk | 8 GiB | 2 GiB for every variant; worker `ZIMFARM_DISK` 8 → 4 → 2 GiB | the worker manager exits when host free disk < `ZIMFARM_DISK`; the reservation is not enforced |
| free-disk floor | images + 4 GB, and ≥ 3 GB free at all times (user's floor) | about 5–6 GB free between tasks. **Below 3 GB in three runs**, from the sampler (every 5 s) and the driver (about every 60 s): r1 S-basic 2.39 GB (sampler; 6 samples below 3 GB, 21:07:00–21:08:46) and 2.76 GB (driver); r3 M-dcseed 2.62 and 2.47 GB (driver, 20:42–20:43, as its scraper ended); r1 M-dcseed 2.92 GB (sampler; 2 samples, 20:59:28–33) and 2.81 GB (driver). No other run, including the voided round, went below 3 GB. The 1.5 GB abort threshold was never hit. | shared disk; other jobs writing |
| render script | frozen with the queries (P7) | `reader_render.mjs` was written at 19:51 (after the freeze) and its end point changed twice after the ZIMs existed (a `LOADS` override for a dry run; the clamped-camera rule at 21:34, whose second attempt led to z12 being withheld) | found during the check |
| analysis scripts | frozen (P7) | `summarize.py` and `make_tables.py` were written and edited after the freeze; the rule they apply (§8.4) is unchanged, and the review recomputed every table number independently | time |
| parsers and success rule | frozen with the queries (P7) | the query set was frozen at 19:26; `reader_search.py` and `inmap_search.mjs` were written during round 2, after round-2 ZIMs existed, and test runs of them (kiwix-serve from 20:09) overlapped the round-2 S-basic and S-ofm builds | time |
| GeoNames hashes | per run | at session start and end (identical); again at the S-full phase's start and end (`sfull/inputs/`, identical) | — |
| GeoNames source for the ADM queries | allCountries | `US.zip` from the same export | size; same records |
| streets | "DC Open Data street centerlines" | DDOT Roadway Block layer | no layer of that name |
| sampling | seeded sample of N | seeded shuffle, walked in order, keeping the first N that pass; all drops listed | so each stratum reaches N |
| address existence | in both inputs | PBF `addr:*` and an OFM house number within 100 m | the MVT has no street name on house numbers |
| render end point | camera within ε at z12 and z14; also stable screenshots | z14 only; z12 and stable screenshots withheld (§7) | found during the check |
| Chrome | fresh profile per load | fresh incognito context per load | — |
| search full-text | not planned | reported as an extra, post hoc column | StreetZim's Kiwix suggestions returned nothing |
| reader ZIMs | round-1 ZIMs | the rerun round 1's ZIMs | the first round 1 was void |
| `compare_tile_sources.py` | in the main report | not run | time |
| S-ofm-nr, §7 scan cross-check | optional | not run | time |

**S-full phase (§8).**

| item | plan / request | done | why |
|---|---|---|---|
| rounds | n = 3, Latin square | **n = 2**, two of the square's three rows (r1 M, S-full, S-basic-new; r2 S-full, S-basic-new, M); positions not balanced. The §8.4 rule is applied unchanged, but at n = 2 non-overlapping ranges occur by chance with probability 1/3 | time (an S-full task takes about 40 min) and disk |
| S-basic-new | requested | run, both rounds | — |
| load gate | as the first pass | every run passed at the first check; the phase is uncontended, the first pass was not | the other jobs had stopped |
| free-disk floor (≥ 3 GB) | at all times | **below 3 GB once**: r1 M-dcseed, 2.67 GB minimum, 9 sampler samples 22:15:25–22:16:05, and 2.74–2.90 GB by the driver; 3.2 GB or more in every other run, and 5.8 GB after the phase | the new image (1.68 GB) was on disk |
| reader ZIM | round 1 (§8.5) | the final round's (r2) ZIMs, as requested for this phase | request |
| S-basic-new reader | — | Kiwix search only; in-map search not run | time |
| `memory_phases.py` | as frozen | its step regex was widened from `[n/7]` to `[n/d]` to read the full profile's `[n/9]` markers; its first-pass output is byte-identical | the full profile has 9 steps |
| `sfull_extract.py`, `sfull_phases.py`, `shots_full.mjs` | — | written for this phase: `sfull_extract.py` and `shots_full.mjs` at 22:08, before the first task (22:13); `sfull_phases.py` at 23:56, after the last. The query set, parsers and tolerances were not changed | new metrics (waits, DEM bytes, per-step CPU) and screenshots |
| Wikimedia | expected to rate-limit this IP | no 429, no rate-limited article, no wait-budget message in either run. The waits are the code's fixed pacing plus upstream response time | as observed |
| satellite | "off" as an on/off enum | `satellite` is a boolean in the `82def4f` definition; it was omitted (off) | definition |
| container restart | — | the container restarted after all six runs, during the reader screenshots. dockerd, the registry, postgres and the compose stack came back from the persisted `/var/lib/docker` with no rebuild (18 tasks and 4 definition versions intact). The Wiki-panel and article screenshots, and the terrain and z14 shots with them, were retaken from r2's ZIM; the S-full Kiwix search was rerun on r2's ZIM and matched | sandbox |
| other containers' CPU | — | Zimfarm's own containers used 129–148 s of CPU per S-full run (a 40-min task), recorded and not counted | — |
| render timing (§7) | per variant | **not run** for S-full or S-basic-new; screenshots only | time |
| download / build time | per run | not reported: the rx-based download metric counts S-full's Wikimedia traffic (1,880–1,925 s) | metric does not apply |
| `sfull_phases.py` | — | ended step 9a at the last progress line (1,250/1,308), about 70 s early; §8.2 uses corrected boundaries (`stored 1290 articles`) and adds step 0 | found in review |
| query blinding | queries frozen before the tool under test | StreetZim's Kiwix-search changes were committed 18–28 min after the 19:26 freeze, and `82def4f` at 21:47, by the same party (§8.3) | the S-full phase was added after the first pass |
| screenshots | in the package | four of the nine S-full screenshots are only on the build host | disk |

## Appendix C. Scale cross-reference (indicative; different machines)

| region | StreetZim (`docs/zimfarm.md`, 4-core shared machine, outside Zimfarm) | maps2zim production (scraper) |
|---|---|---|
| D.C. | 2.8 min wall on the local Zimfarm, 3.97 GB PSS, 19.9 MB ZIM | no recipe; the smallest areas take 51–119 min |
| Luxembourg | 3.2 min, 4.0 GB PSS, 55 MB ZIM | 75.0 min (`46f2ea98`), 176.6 MB ZIM |
| Switzerland | 70 min (shared machine; an upper bound), 4.6 GB PSS | 72.4 min (`625d6b2d`) |
| Netherlands | 92 min, **10.8 GB PSS**, 11.0 GB disk, 1.22 GB ZIM | 80.0 min (`1a8d7dbc`) |
| USA | not measured | 212.7 min (`8014997b`) |

- Build times cross at about Switzerland's size (a 0.7 GB extract). Above it, StreetZim's
  region-dependent build takes longer than maps2zim's mostly fixed planet cost.
- StreetZim's memory grows with the region. maps2zim's memory during the planet scan is unknown.
- StreetZim is not measured for continents.

## Appendix D. Reproducibility

The package is the `dc-comparison-results/` folder. The review's gaps are closed:
- the PBF, the poly and their `SHA256SUMS` are in `inputs/dc-inputs/`, and the screenshots in
  `screenshots/`;
- `recipes/*.stored.json` were re-exported from the backend after the runs and show the 2 GiB
  disk actually used;
- the `.mjs` scripts no longer hard-code paths: puppeteer is resolved from `PUPPETEER_FROM`
  (default: the working directory) and Chrome from `CHROME_PATH` (default: puppeteer's own).
  Measured with puppeteer 23.11.1 and Chrome 131.0.6778.204. `reader_search.py` takes
  `KIWIX_SERVE`;
- **the images were not saved** (`docker save`). About 5.5 GB was free, and a save of the four
  images (about 0.8 GB compressed) would have left too little for the S-full phase's image build
  and runs while keeping 3 GB free. Instead `repro/build-images.sh` records the exact build
  commands and the recorded ids. **A rebuild will have different digests**: the sandbox CA layer,
  moving base images and build-time apt/pip resolution all change them. The images can still be
  saved from the local registry later if disk allows.

**S-full phase additions** (`sfull/`):
- the image built from `82def4f489d810e95c2447ac74d352c2b0cd3a52`, manifest-list digest (equal
  to the local image id the runs report) `sha256:a60ef98bdd976f3990f0aba880b65cf4b3b40a4bd0e06332987bd6cf00885527`
  (build log `sfull/build-82def4f.log`), and the definition
  `repro/streetzim-offliner-definition-82def4f.json`. **`repro/build-images.sh` and
  `repro/images.txt` do not yet record this build**; add its clone, checkout and `docker build`
  commands and the id before publication. The build log does not record the source commit; the
  viewer files the runs log (605,851 and 103,513 B) match `82def4f`;
- `recipes/S-full.json`, `recipes/S-basic-new.json` and their `.stored.json` exports;
- `sfull/runs/`, `sfull/summary.json`, `sfull/phases.txt`, `sfull/sources-waits.json`,
  `sfull/memory-phases.txt`, `sfull/inventory/`, `sfull/inputs/`, and `sfull/reader/`
  (including the post-restart recheck in `verify/`);
- `scripts/run_matrix_full.sh`, `sfull_extract.py`, `sfull_phases.py` and `shots_full.mjs`;
- S-full's ZIMs (r1 and r2) are in `sfull/runs/`; five screenshots are in `sfull/screenshots-full/`
  and the other four must be copied in (Appendix B).
- Copernicus DEM, Overture and Wikimedia were fetched live and not archived. A rerun will fetch
  whatever those services serve then.

**Notes for anyone rerunning.**
- **`WEB_API_URI`.** The worker-manager image defaults `WEB_API_URI` to production
  (`https://api.farm.openzim.org/v2`, visible in `repro/worker_mgr-env.json`). The compose
  command's `--webapi-uri http://backend:80/v2` overrides it, and the manager passes that URI to
  each task worker. Without the flag, a local worker would poll production.
- **TLS-intercepting proxy.** All egress from this sandbox, containers included, went through a
  TLS-intercepting proxy. The base images were shadowed with its CA (`repro/cashim/Dockerfile`),
  so the measured images carry an extra CA layer and `SSL_CERT_FILE`-style env vars; the CA
  certificates are sandbox-specific and not published. Download times include the proxy.
  Geofabrik was not reachable through the host's proxy, only from containers.
- **GeoNames** republishes `allCountries.zip` daily. The file used (sha256 `b932491d…`,
  Last-Modified 2026-09-29 01:58 GMT) was hashed only at session start and end, not per task;
  the two hashes match, so every M-dcseed task in the session most likely read the same file.
  A rerun will fetch a different file unless it is archived; M-dcseed's search output
  (4 ADM places) is unlikely to change, but its download and parse time can.

**Contents.**
- **Inputs:**
  - `prep/dc-ofm.mbtiles` (sha256 `038400d5…`), `prep/make_ofm_cut.report.json`, `prep/accept_set.json`;
  - the D.C. PBF (sha256 `86d73daf…`) and poly (`41147312…`);
  - `inputs/input_hashes-{start,end}.json`: the live inputs, identical at start and end.
    GeoNames `allCountries.zip` is republished daily, so a rerun will fetch a different file
    unless it is archived.
- **Images:**
  - maps2zim: `ghcr.io/openzim/maps:0.2.1@sha256:71c9474b…` (public), plus the CA layer and the
    cut (`repro/M-dcseed.Dockerfile`).
  - StreetZim: built from `d92b1878d71c8b7965f050c58530e44649ffb411` with the repository's
    Dockerfile, on base images shadowed with a sandbox proxy CA (`repro/cashim.Dockerfile`).
    The local digest is `sha256:1b3f4ea2…`; S-ofm's is `sha256:90b171d9…`. A rebuild without the
    CA layer will have a different digest.
- **Zimfarm:**
  - `917d7bc` plus `repro/zimfarm-vs-917d7bc.diff` (7 lines, 4 files);
  - the compose file, before and after;
  - the worker and backend env (see the `WEB_API_URI` note above).
- **Recipes and scripts:** `recipes/`, the offliner definitions, and every script in `scripts/`,
  with `reader/SCRIPTS-SHA256SUMS-final` and the 19:26 `reader/FROZEN-SHA256SUMS`.
- **Runs:** `runs/r<round>-<variant>/`:
  - the task JSON;
  - Zimfarm's log and its zimcheck;
  - `docker logs`;
  - the sampler JSONL;
  - the gate record;
  - the ZIM sha256.

  `runs/void-round1/` keeps the voided round, including the orphaned samplers' files.
- **Summary:** `summary.json`.
- **Versions:**
  - zim-tools 3.8.0;
  - kiwix-serve 3.8.2;
  - Chrome 131.0.6778.204;
  - python-libzim 3.13.0 (host and StreetZim image), 3.10.0 (maps image);
  - Docker 29.3.1, cgroup v1.
