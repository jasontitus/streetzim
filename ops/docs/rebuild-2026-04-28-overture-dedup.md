# Remote-box rebuild queue — 2026-04-28 Overture dedup fix

Commit `e4c388e` ("overture: dedup attr_key now includes normalized
city") landed today. **It only takes effect on a fresh rebuild** — the
collision happens in `create_osm_zim.py`'s Pass 2 Overture merge,
before the ZIM is emitted. Repacking shipped ZIMs won't pick it up.

This doc lists which regions to rebuild on the big remote box tonight,
ordered by how many addresses the bug was silently dropping.

The actual build commands haven't changed — see
`docs/remote-rebuild.md` Path B (FULL REBUILD). This file is just the
prioritized queue and the survey numbers behind it.

---

## What the bug did

`Pass 2` of the Overture address merge deduped against OSM using
`attr_key = (housenumber, normalized_street)`. If OSM had
"1029 Ramona Street" in Ramona, CA, the Overture row for "1029 Ramona
St, Palo Alto" got dropped as a spatial duplicate — even though the
two locations are 600 km apart. Surfaced when the user noticed his
own street address was missing from the California ZIM.

The fix is `attr_key = (housenumber, normalized_street,
normalized_city)`; coord_key (~1 m grid) still catches the
same-building-different-geocode case.

## Per-region impact (counts, not estimates)

Scan ran against today's 7 region Overture parquets (`addresses-{id}-2026-04-15.0.parquet`). "Multi keys" = `(housenumber, normalized_street)` values that span ≥2 distinct cities; "max rows dropped" = sum of all-but-one row per such key, the worst case the old dedup could swallow.

| Region | Parquet rows | Multi-city keys | Rows in collisions | Max rows dropped | Worst-case key |
|---|---:|---:|---:|---:|---|
| **midwest-us** | 31.2 M | 1,231,452 | 4.95 M | **3,717,300** | "101 north main street" × 328 cities |
| **baltics** | 2.7 M | 154,386 | 1.43 M | **1,271,319** | "4 liepu g" × 613 cities |
| **california** | 14.3 M | 272,269 | 895 K | **622,731** | "201 main street" × 26 cities |
| silicon-valley | 1.8 M | 17,714 | 59 K | 41,257 | "1340 el camino real" × 9 |
| colorado | 2.5 M | 17,746 | 53 K | 35,587 | "101 main street" × 19 |
| washington-dc | 623 K | 247 | 497 | 250 | "3100 richmond hwy" × 2 |
| hispaniola | 0 | — | — | — | (empty parquet — no Overture for region) |

The other shipped regions weren't in today's scan but every region
built before `e4c388e` carries the same bug. Address-rich regions
(europe, united-states, africa) are presumed worst-affected.

## Tonight's queue (ordered by impact)

These all use the canonical full-rebuild flow from
`docs/remote-rebuild.md` Path B — same flags, same post-processing.
Estimated runtimes from that doc; assume world MBTiles + terrain cache
+ satellite cache are still warm from the world build.

| # | ID | Bbox | Est runtime | Why now |
|---|---|---|---:|---|
| 1 | `midwest-us` | `-97.5,36.0,-80.0,49.4` | ~1 h | 3.7 M addresses dropped — biggest survey impact, also oldest shipped (2026-04-15) |
| 2 | `baltics` | `19.0,53.0,28.5,60.0` | ~30 min | 1.27 M dropped — small region, finishes fast |
| 3 | `california` | `-125.0,32.0,-114.0,42.0` | ~30 min | 622 K dropped — was the user-visible repro |
| 4 | `europe` | `-25.0,34.0,50.5,72.0` | ~4 h | Biggest addressable population; last shipped 2026-04-17 |
| 5 | `united-states` | `-125.0,24.5,-66.9,49.4` | ~3 h | Country-wide rollup; last shipped 2026-04-13, staler than any regional |
| 6 | `africa` | `-18.0,-35.0,52.0,38.0` | ~3 h | Last shipped 2026-04-17; less Overture address density but still buggy |

**Total ≈ 12 h serially** — fits an overnight run.

If the queue completes before morning, the second tier (lower impact
but still buggy) is:

| # | ID | Bbox | Est runtime | Notes |
|---|---|---|---:|---|
| 7 | `canada` | `-141.0,41.0,-52.0,84.0` | ~2 h | Last shipped 2026-04-26c |
| 8 | `east-coast-us` | `-84.0,24.5,-66.9,49.4` | ~1.5 h | Last shipped 2026-04-26c |
| 9 | `central-us` | `-114.0,30.0,-94.0,49.0` | ~1 h | Last shipped 2026-04-26c |
| 10 | `west-coast-us` | `-125.0,32.0,-114.0,49.0` | ~1 h | Last shipped 2026-04-26c (overlaps california) |
| 11 | `texas` | `-107.0,25.5,-93.0,37.0` | ~45 min | Last shipped 2026-04-26c |

Skip on this pass:
- `hispaniola` — Overture parquet was empty for this region; dedup is
  moot. The shipped 04-22 ZIM is unaffected by this bug.
- `silicon-valley`, `colorado`, `washington-dc` — fits-in-30-min
  regions where survey impact is low (≤ 41 K). Run from local Mac
  next time the user wants a fresh roll, no need to burn remote-box
  hours on them tonight.
- `japan`, `iran`, `central-asia`, `west-asia`, `egypt`,
  `australia-nz` — non-English address regions, unscanned. They have
  the bug but expected impact is much lower because cross-city
  number/street collisions are rarer when street names aren't
  formulaic ("101 main"). Defer until the high-impact regions land.

## Run command

Use the existing wrapper at the bottom of `docs/remote-rebuild.md`
("Wrapper script for the queue") and replace its inline list with:

```sh
for row in \
  "midwest-us    -97.5,36.0,-80.0,49.4 'Midwest US'"      \
  "baltics       19.0,53.0,28.5,60.0 'Baltics'"           \
  "california    -125.0,32.0,-114.0,42.0 'California'"    \
  "europe        -25.0,34.0,50.5,72.0 'Europe'"           \
  "united-states -125.0,24.5,-66.9,49.4 'United States'"  \
  "africa        -18.0,-35.0,52.0,38.0 'Africa'"          \
; do
  read -r id bbox name <<< "$row"
  # … rest of the wrapper from docs/remote-rebuild.md …
done
```

Order is impact-first — if the box dies overnight, the most
impactful regions are already done.

## Pre-flight checklist before kicking off

1. Confirm the remote box is on the commit with the fix:
   ```sh
   git fetch origin && git log --oneline -1 origin/main
   # expect: e4c388e overture: dedup attr_key now includes normalized city …
   git rev-parse HEAD  # should equal e4c388e (or newer)
   ```
2. Verify the Overture parquets for the queued regions exist locally:
   ```sh
   for id in midwest-us baltics california europe united-states africa; do
     ls -lh overture_cache/addresses-${id}-2026-04-15.0.parquet 2>&1 | head -1
   done
   ```
   If any are missing, scp them from the local Mac (~1–3 GB each).
3. Disk: each `--keep-temp` run leaves the working dir intact. Plan
   for ~30 GB extra per region (terrain + tilemaker store + ZIM
   pre-spatial). The 14 TB box has plenty of room but check there
   isn't a stale `--keep-temp` from the world build hogging the
   tilemaker store dir.
4. World build status: if the world ZIM build is still running,
   **don't kick this queue off concurrently** — they share the
   tilemaker store and terrain cache and will fight for filesystem
   locks. Wait for it to finish first.

## What to verify after each region uploads

Within a few minutes of `cloud/upload_validated.sh` finishing for
each region, the public site at `streetzim.web.app` should list the
new dated ZIM. If the listing still shows yesterday's date after
~5 min, archive.org's metadata API is lagging — re-run
`./venv312/bin/python3 web/generate.py --deploy` from anywhere with
the venv.

For the user-visible 1029-Ramona-Palo-Alto repro, after the
California rebuild lands and downloads:

```sh
./venv312/bin/python3 - <<'PY'
from libzim.reader import Archive
import json
a = Archive("osm-california-$(date +%Y-%m-%d).zim")
manifest = json.loads(bytes(a.get_entry_by_path("search-data/manifest.json").get_item().content))
# 1029 Ramona — sub_chunks may be hot-split, but '10' or '1029' should resolve
PY
```

(Or just open in Kiwix and type "1029 Ramona" in Find — should hit
both Palo Alto and the Ramona, CA original.)
