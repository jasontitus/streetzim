# Head-to-head: Washington, D.C., `main` vs the refactor branch

On 28 September 2026 Washington, D.C. was built from `main` and from the
refactor branch with production-like flags and identical inputs, and the
two ZIMs were compared entry by entry. Both were also compared with the
published D.C. ZIM. This page records what was run, the numbers, every
difference, and the one feature the run did not test: bundled Wikipedia
articles.

The working files (build script, comparison script, outputs) were in a
scratch directory that has since been deleted. The numbers below come from
the session's recorded tool output. Where something was not measured, or
the explanation was not verified, it says so. The comparison can now be
rerun from the repository: see
[golden-builds.md](golden-builds.md#optionally-washington-dc).

**Result:** apart from three deliberate changes, `main` and the branch
differed only in the same ways that two `main` builds differ from each
other. One of the deliberate changes turned out to be a bug: the branch
claimed Wikipedia content in a ZIM with no articles. It was fixed later
(see [Differences](#differences-main-vs-branch)).

A follow-up on 29 September bundled real D.C. article text through the
Wikipedia API: 1,561 articles, against 1,551 in the published ZIM, with
1,544 titles in common. `main` and the branch clean every article to
identical bytes. Images are still not compared (see
[Why no Wikipedia articles](#why-no-wikipedia-articles)).

## What was compared

| side | commit |
|---|---|
| `main` | `cd4b7ea` (Merge #19: maintainability pass) |
| branch | `9e168ae` (Review fixes: validator reports SZRG versions; stricter pyright gate), on `claude/adoring-dijkstra-i2vge7` |
| published | `osm-washington-dc-2026-09-26.zim` from archive.org (115,494,153 bytes, 9,046 entries), linked from the StreetZim site |

The branch worktree was first created at `66635aa` and moved to `9e168ae`
before any build that completed. All completed D.C. builds used `9e168ae`,
so they predate the later `main()` split, the Python 3.14/zimscraperlib
work and the `hasWikiArticles` fix (`684dae9`).

D.C. was chosen because it is published, small and a builder preset.
Its extract was available here: BBBike's `WashingtonDC.osm.pbf`, last
modified 26 Sep 2026, 64,659,545 bytes. The openstreetmap.fr mirror has no
D.C. extract, and Geofabrik was blocked from this sandbox. An Iceland
comparison was tried first and dropped for the same reason.

## How

### Build flags

Both sides ran the same script, one after the other, with the flags of
`build-region-fast.sh`:

```
create_osm_zim.py --pbf WashingtonDC.osm.pbf --bbox=-77.12,38.79,-76.91,38.99 --name "Washington, D.C."
  --satellite --satellite-download-zoom 12 --terrain
  --wikidata --wikidata-cache <shared>
  --routing --spatial-chunk-scale 10 --split-hot-search-chunks-mb 10 --split-find-chips
  --no-llm-bundle --low-zoom-world-vrt <world.vrt>
  --resolve-wikidata-titles --wikidata-title-cache <shared>
  --bundle-wiki-articles --wiki-articles-source <Monaco test ZIM> --wiki-images all --wiki-image-max-kb 128
  --overture-addresses dc-addresses.parquet --overture-places dc-places.parquet
  -o dc.zim
```

It differed from production in these ways:

- **No `--zim-builder=rust`.** The Rust packer isn't available in the
  sandbox, so both sides used libzim. The branch's changes on the Rust path
  were reviewed but never run. A small region through `build-region-fast.sh`
  on the build host would cover that.
- **No `--mbtiles`.** Vector tiles came from tilemaker on the D.C. extract,
  not from production's tile build.
- **No `--terrain-dir`, `--search-cache`, `--keep-temp` or `--url-cache`.**
- **The Wikipedia article source was a small Monaco test ZIM,** not
  production's English Wikipedia ZIM. See
  [Why no Wikipedia articles](#why-no-wikipedia-articles).
- **`TERRAIN_BLANK_TOLERATE=2`.** The first attempt failed the same way on
  both sides. Two z7 terrain tiles cover far more than D.C., the sandbox
  lacked their elevation data, and the terrain health check aborted.
  Both sides were rerun with this setting.
- **Newer Overture releases.** Production pins 2026-04-15.0 (read from
  the published ZIM's `overture-sources.json`), which Overture has since
  deleted. Instead:
  - addresses came from 2026-09-23.1 (623,225 rows);
  - places came from 2026-08-19.0 (74,177 rows), because 2026-09-23.1
    dropped the `categories` column and `download_overture_data.py places`
    errors on it.

  Both sides used the same two files.

### Shared inputs and caches

Both sides read the same inputs:
- the extract, Overture parquet files and low-zoom `world.vrt`;
- the coastline and landcover files;
- one cache directory (`STREETZIM_CACHE_DIR`): satellite, terrain,
  Wikidata entities and resolved Wikidata titles.

Wikidata was rate-limited during the first builds, so the two sides at
first got different Wikidata inputs:
- first pair: `wikidataEntries` 1,582 on `main`, 1,724 on the branch;
- Wikidata title resolution on `main` stopped on an HTTP 429 at 405 of
  1,072 Q-IDs, against 618 on the branch.

The builds were repeated until both sides fetched zero new Q-IDs and
resolved the same 618 of 1,072 titles in the same round. Only that final
pair is compared below.

A second `main` build from the same inputs served as a control, to show
which differences are ordinary run-to-run variation.

### What was diffed

- **Every entry:** path, MIME type, size and content hash.
- **Vector tiles:** decoded, compared feature by feature (layer, geometry,
  properties).
- **Search records:** every record in `search-data/`, compared as a
  multiset by (name, type, place) and exactly.
- **Kiwix search pages (`search/`):** compared as a multiset, with page
  numbers removed.
- **Summary figures for each side:**
  - entries per top-level folder;
  - metadata;
  - search record counts by type;
  - Find chips;
  - routing graph size;
  - `streetzim-meta.json` counts;
  - `map-config.json` flags.
- **Quality:**
  - `cloud/validate_zim.py` with zimcheck;
  - a route from Georgetown (38.9076,-77.0723) to Union Station
    (38.8973,-77.0063);
  - the browser smoke test `cloud/zim_viewer_smoke.mjs`.
- **Size and time:** from `measure_build.py`.

## Results

### `main` vs branch

| | `main` vs `main` (control) | `main` vs branch |
|---|---|---|
| Entries | 3,330 vs 3,330 | 3,330 vs 3,330 |
| Vector tiles, decoded | 181/181 identical | 181/181 identical |
| Search records | 1,769,118 each; keys identical | 1,769,118 each; keys identical |
| Records differing exactly (a different near-duplicate kept) | 22 | 22 |
| Kiwix search pages, as a set | identical | identical |
| Entries with different bytes, by folder | tiles 179, search 31, search-data 20 | tiles 181, search 99, search-data 20, plus `License`, `M/License`, `index.html`, `map-config.json` |
| Routing, satellite, terrain, fonts, Wikidata, chips, other metadata | identical | identical |

Tile bytes differ because tilemaker writes features in a different order
on each run. That order changes which of two near-duplicate search records
is kept: a coordinate in the fifth decimal, or an empty vs a filled
subtype. It also changes the numbering of the Kiwix search pages. The
control build shows all of this without any code change. The branch had
more search pages with different bytes (99 against 31), but compared as a
set they were identical in both pairs.

Address search was also identical: 288,059 distinct addresses on both
sides, the same list at the same positions.

### Differences, `main` vs branch

| entry | change | deliberate? |
|---|---|---|
| `License` (and `M/License`) | Wikipedia text credited as CC BY-SA 4.0 instead of 3.0 | yes |
| `index.html` | the branch's viewer: the credits dialog shows only the sections for what the ZIM contains (`attr-satellite-section`, `attr-terrain-section`, `attr-wiki-section`) | yes |
| `map-config.json` | gains `hasWikiArticles` | intended, but **a bug here**: this ZIM stored 0 articles |

`hasWikiArticles` and the Wikipedia credit were set whenever article
bundling was requested and there were Wikipedia links, even when no article
was stored. Commit `684dae9` fixed this: `create_zim` now writes
`map-config.json` and `License` after the articles, and sets both from what
was actually stored. A test covers 0 and 1 stored articles. The D.C.
comparison was not rerun after the fix.

### Three-way summary

| | published | `main` | branch |
|---|---|---|---|
| entries (`all_entry_count`)¹ | 9,046 | 3,319 | 3,319 |
| `tiles/` | 185 | 181 | 181 |
| `satellite/` | 185 | 35 | 35 |
| `terrain/` | 35 | 35 | 35 |
| `search-data/` | 1,115 | 1,115 | 1,115 |
| `search/` (Kiwix search pages) | 0 | 1,044 | 1,044 |
| `wikidata/` | 89 | 89 | 89 |
| `wiki-article/` | 1,551 | 0 | 0 |
| `wiki-image/` | 5,065 | 0 | 0 |
| search records (unique) | 416,292 | 395,515 | 395,515 |
| … addr | 289,613 | 289,622 | 289,622 |
| … poi | 91,051 | 94,080 | 94,080 |
| … street | 34,612 | 10,769 | 10,769 |
| … water / place / park / peak / airport | 438 / 375 / 181 / 20 / 2 | 456 / 375 / 191 / 20 / 2 | same as `main` |
| Find chips | 10 | 10 | 10 |
| … food / shops / landmarks | 8,516 / 6,594 / 1,996 | 8,736 / 6,804 / 2,012 | same as `main` |
| routing nodes / edges | 270,455 / 738,795 | 272,049 / 742,694 | 272,049 / 742,694 |
| `wikidataEntries` | 1,825 | 1,800 | 1,800 |
| `wikiCrossRefs` | 800 | 732 | 732 |
| `addresses` | 330,088 | 329,919 | 329,919 |
| wiki geo-index places | 523 | 0 | 0 |

¹ The comparison script counts `all_entry_count`; the entry-by-entry diff
above counts 3,330 because it also lists metadata. Both scripts were run on
the same files.

The published ZIM differs from both new builds because of inputs and code
already on `main`, not because of the branch:

- **Streets 34,612 → 10,769.** The street-piece merge from PR #19 is on
  `main`, and the published ZIM predates it.
  - Run on the published ZIM's own street pieces, the merge gives
    34,612 → 10,719 records.
  - All 5,986 street names are kept, and no place label is lost.
  - The farthest any piece ends up from its kept record is 2.04 km.
  - `16th Street Northwest` goes from 226 pieces to 13 records, one per
    place.
  - All 34 street names with a Wikipedia link keep it.
- **About 3% more POIs and chip entries:** newer Overture data (August
  and September releases instead of April).
- **Addresses:** 288,016 of the published ZIM's 288,052 are found in the
  branch build (99.99%). Of those found, 3 sit more than 200 m from the
  published position. The 36 missing are most likely addresses edited
  upstream between the two datasets (not verified one by one).
- **Satellite 185 → 35:** attributed to the sandbox's smaller local
  satellite inputs. Not investigated further.
- **Two place labels missing from the branch build** (for example "Silver
  Spring" on East-West Highway). The merge keeps every label when run on
  production's pieces, so the likely cause is tile input: production's tiles
  include place names just outside the D.C. box. **Not verified.**
- **No Wikipedia articles or images:** see below.

### Quality checks

- **Validator:** all 25 `[ OK ]` checks and zimcheck pass on the branch
  build.
- **Validators compared:** `main`'s and the branch's `validate_zim.py`
  gave the same 27 verdicts on each of four ZIMs (published, repackaged,
  both D.C. builds).
- **Route:** Georgetown → Union Station is 7.3 km with A\* (0.2–0.3 s) and
  8.3 km with `hwy2` on the published ZIM, `main` and the branch.
- **Browser smoke test** (15 checks):

  | ZIM | result | failures |
  |---|---|---|
  | published | 12/15 | the three new credits-section checks; its viewer predates those section ids |
  | published, branch viewer patched in | 15/15, zimcheck passes | none |
  | branch build | 14/15 | "wiki geo-index loaded or loading", because no articles were bundled |
  | `main` build | 11/15 | the same geo-index check, plus the three credits checks |

  Under real `kiwix-serve`, the branch build again passed 14/15, and the
  branch's repackage of the published ZIM passed 15/15.

### Repackaging a published ZIM

The published D.C. ZIM was repackaged with `main`'s
`cloud/repackage_zim.py` and with the branch's, using
`--split-find-chips --split-hot-search-chunks-mb 10`. Both outputs had
9,057 entries. The only difference was `index.html`, the viewer the tool
swaps in.

This is the only part of the comparison that covered real Wikipedia
articles and images. It shows the branch's repackaging keeps them. It does
not test bundling them.

### Sizes and timings

The same second pair of builds, which used `TERRAIN_BLANK_TOLERATE=2`:

| | `main` | branch |
|---|---|---|
| `dc.zim` size | 46,361,872 B | 46,364,161 B |
| wall time | 541.5 s | 373.1 s |
| CPU time | 455.3 s | 457.5 s |
| peak memory (PSS) | 4.09 GB | 4.11 GB |

The wall times are not a fair speed comparison. The builds ran one after
the other with a shared cache, so the second build fetched fewer Wikidata
entities. CPU time and peak memory are about the same. The failed first
pair took 22.5 and 13.1 minutes of wall time for the same reason. Sizes and
times of the final pair were not recorded. The published ZIM is
115,494,153 B, mostly because of its articles, images and satellite tiles.

## Why no Wikipedia articles

The D.C. builds contain no Wikipedia articles because they had no real
article source. Running out of time to download them was not the reason.

1. **Wikipedia was rate-limiting the sandbox.** Just before the builds
   started, a request to `https://en.wikipedia.org/w/api.php` returned
   **HTTP 429**. Wikidata and the EOX satellite tiles returned 200.
   Wikidata was also throttled during the builds: the logs show
   repeated "Rate limited, waiting" lines. Title resolution on `main`'s
   first build stopped with "HTTP Error 429: Too Many Requests".
2. **So `--wiki-articles-source` pointed at a small test ZIM.**
   - It was the Monaco fixture from the golden-build inputs, with 35
     entries, and was used only to exercise the code path.
   - Without `--wiki-articles-source`, the builder fetches articles from
     the public Wikipedia API, which was returning 429.
   - `--wiki-images` needs an offline source anyway.
3. **Nothing matched.** Both builds looked up 1,580 D.C. article titles and
   found none in the Monaco ZIM:

   ```
   bundle-wiki-articles: 1580 distinct titles from wiki.zim
   bundle-wiki-articles: stored 0 articles (0 KB), 1580 unavailable; 0 images (0 MB, mode=all)
   ```

   Both sides stored 0 articles and 0 images, and wrote no wiki geo-index.
   The published ZIM has 1,551 articles, 5,065 images and 523 places in its
   geo-index.

What this means:

- `main` and the branch got the same empty source, so the comparison
  between them is still fair.
- **Article bundling was not compared on real D.C. content.** It is the one
  production feature this head-to-head did not test.
- Golden build F on Monaco bundles real articles from that same test ZIM,
  and it matches between `main` and the branch apart from the intended
  changes. The repackage test above keeps the published ZIM's real articles
  and images intact.
- The empty source exposed the `hasWikiArticles` bug above. The same false
  label would appear in production whenever the article source yields
  nothing.

### Follow-up: real article text, 29 September

A day later the article text was compared on real D.C. content. Images
still were not.

**Which source could be used.**

| source | result |
|---|---|
| Wikipedia API (`action=parse`, the builder's online path) | Reachable, but heavily throttled. It worked with `Retry-After` honoured, 1.5 s between requests, and a User-Agent with a contact address. Fetching 563 pages drew 182 HTTP 429 responses. |
| Full English Wikipedia ZIM (production's source) | Not possible here: the `maxi` ZIM is 119 GB and `nopic` is 49 GB. |
| `wikipedia_en_top_maxi_2026-09` (6.5 GB, the only subset with images and broad coverage) | Not downloaded: the disk had 3.1–10 GB free while other builds were running, and this ZIM would have left less than the 3 GB floor. `top_nopic` (2.6 GB) and `top_mini` (283 MB) have no images, so they add nothing over the API. |
| Kiwix's online library (`browse.library.kiwix.org`, full `maxi` 2026-08) | Returns an "Access confirmation" page aimed at AI crawlers. It was not bypassed. |

**What was run.** The build used this branch at `ce2c161`, the `-next`
head. The input was the same BBBike `WashingtonDC.osm.pbf` as in the
original run (26 Sep, 64,659,545 bytes). Geofabrik still reset the
connection. The flags were `--wikidata --resolve-wikidata-titles
--bundle-wiki-articles --wiki-images all --wiki-image-max-kb 128
--wiki-articles-cache <dir>`, with no `--wiki-articles-source`. To save
time and disk, satellite, terrain, Overture and the world VRT were left
out; they do not feed article bundling. The steps:

1. A first build had cold caches. Wikidata returned 429 during title
   resolution, which stopped at 600 of 1,072 Q-IDs. That left 1,203 titles,
   and 1,009 articles were stored.
2. The Wikidata title cache was then completed slowly, with one batch every
   5 s and a 60 s pause after each 429. With the cache complete, the build
   requests **1,580 distinct titles, the same number as the original run**.
3. The 563 articles missing from the cache were fetched politely into the
   builder's cache format. This took 2.5 hours.
4. A final build ran from the warm caches and made no Wikipedia requests.

**Numbers.**

| | published (Feb 2026 enwiki ZIM) | branch, API text |
|---|---|---|
| titles requested | n/a | 1,580 |
| `wiki-article/` | **1,551** | **1,561** |
| … unavailable | n/a | 19 (12 enwiki disambiguation pages, 7 pages the API returns no text for) |
| article bytes | 16.97 MB | 16.10 MB |
| `wiki-image/` | **5,065** (in 1,474 articles, 6,377 `<img>` refs) | **0**: the online path is text only by design |
| image candidates in the fetched HTML (≤ 12 per article) | n/a | 7,839 (6,322 unique `src`), an upper bound before the 128 KB cap |
| wiki geo-index places | 523 | 495 |
| `hasWikiArticles` | absent (older viewer) | `true` |
| `License` credits Wikipedia | yes (CC BY-SA 3.0) | yes (CC BY-SA 4.0) |

- **1,544 titles are in both ZIMs.**
- **7 are only in the published ZIM:**
  - 4 are no longer requested, because the OSM or Wikidata link changed.
    For example, `Saint_Anselm's_Abbey_(Washington,_D.C.)` is now
    `St._Anselm's_Abbey_…`.
  - `Aurora_(sculpture)` is now a disambiguation page.
  - 2 embassy pages now return no text.
- **17 are only in the new build.** These are newer links or pages, for
  example `New_Stadium_at_RFK_Campus`, `National_Mall` and
  `National_Academy_of_Sciences`.
- The geo-index gap (495 vs 523) is not explained. The Wikidata place
  cache was still partial (1,360 of 1,511 Q-IDs fetched), and the geo-index
  draws on it. This was not verified.
- **`main` vs branch:** `main`'s `cloud/wiki_articles.py` differs only in
  type annotations. Run over the same 1,561 cached pages, `main`'s
  `clean_article_html` produced byte-identical output. The stored
  `wiki-article/` entries match a fresh recompute byte for byte.

**Rendering.** The final ZIM was served with `kiwix-serve` and checked in
headless Chromium:
- The viewer loaded `wiki-geo-index.json` (495 places), with no page
  errors. This was the smoke-test check that failed in the original run.
- `_wikiArticlePath(null, "en:Lincoln Memorial")` resolves.
- Nine sampled articles returned HTTP 200 with the right `<h1>`, body text,
  the "Back to map" bar, no failed requests and no page errors. They
  included landmarks, a `/` title (`Bellevue_/_William_O._Lockridge_Library`),
  an en-dash title and a `#` title (`McKeldin_Mall#The_Peace_Garden`).

**Found on the way** (in `main` too):
- `_fetch_online` caches a 429 that survives its retries as a permanent
  "no article" marker. It uses a 1/2/4 s backoff and ignores `Retry-After`.
  The cold build left 184 empty cache files. When they were refetched
  politely, 177 turned out to be real articles. A rebuild would have
  skipped them for good without saying so.
- `cloud/wikidata_titles.py` also ignores `Retry-After`.
- Both default User-Agents lack a contact address, which Wikimedia's
  User-Agent policy asks for.
- One stored title contains a fragment (`McKeldin_Mall#The_Peace_Garden`),
  so the page is the whole McKeldin Mall article under that name.

### What is still not compared

Images, and the offline-ZIM HTML path, on real D.C. content. On the build
host, which has the production ZIM:

```
# same warm caches for both sides; build main, then the branch
create_osm_zim.py --pbf WashingtonDC.osm.pbf --bbox=-77.12,38.79,-76.91,38.99 --name "Washington, D.C." \
  --wikidata --wikidata-cache <shared> --resolve-wikidata-titles --wikidata-title-cache <shared> \
  --bundle-wiki-articles --wiki-articles-source /storage/streetzim/wiki-src/wikipedia_en_all_maxi_2026-02.zim \
  --wiki-images all --wiki-image-max-kb 128 -o dc-<side>.zim
```

Then:
- diff `wiki-article/` and `wiki-image/` entry by entry between the two
  sides;
- compare the counts with the published ZIM's 1,551 articles and 5,065
  images, which came from that same source ZIM.

This needs no network for articles. In a sandbox, `wikipedia_en_top_maxi`
(6.5 GB) would give a partial image comparison once about 10 GB is free.

### Still to do

- [x] Rerun D.C. with a real article source at the current branch head.
  Done for article text over the API; see the
  [follow-up](#follow-up-real-article-text-29-september).
- [ ] Compare images and the offline-ZIM path on the build host, as above.
- [ ] Make `_fetch_online` stop caching 429s as misses and honour
  `Retry-After`, in both the article fetcher and the Wikidata title
  resolver.
- [ ] Build one small region through `build-region-fast.sh` on the build
  host, to exercise `--zim-builder=rust`.
- [x] Make the golden and D.C. comparison scripts (entry fingerprint diff,
  decoded-tile diff, search-record multiset diff) a documented,
  rerunnable procedure. They lived in the deleted scratch directory.
  Done: `tools/golden_builds.sh` and `tools/golden_diff.py` replace them,
  and [golden-builds.md](golden-builds.md#optionally-washington-dc) has a
  D.C. recipe. The recipe uses fewer flags than the run above; add the
  others as that page's "Covering more of the builder" describes.
- [ ] Optional: confirm the two missing place labels come from production's
  tile input.

Found on the way; these affect production whatever happens to the branch:
- [x] Overture release 2026-04-15.0 has been deleted. The ops wrappers pin
  2026-08-19.0 (`OVERTURE_RELEASE=latest` opts in to the newest complete
  release) and `build-region-fast.sh` refuses to run without a release.
- [x] `download_overture_data.py places` failed on 2026-09-23.1, which
  dropped `categories`. It now reads `taxonomy` and maps it to the old
  category names (`streetzim/overture_taxonomy.py`).
- [x] Low-zoom terrain tiles need elevation data far outside a small
  region's bbox. On a fresh machine this trips the terrain health check
  unless `TERRAIN_BLANK_TOLERATE` is set. The D.C. run tripped because the
  `--low-zoom-world-vrt` it was given lacked those cells; without one,
  the z0-z7 tiles came out 0 m past bbox + 1 degree and the check, which
  read the same short DEM, let them through. Fixed without a world DEM
  (every `streetzim` build): terrain starts at the lowest zoom the viewer
  can show, every tile is filled over its whole square (GLO-90 for
  z <= 9), and the audit compares each tile with that DEM. See
  [zimfarm.md](zimfarm.md#terrain-cost).
