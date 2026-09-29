# Golden builds: proving a refactor changes no output

A refactor of the builder (moving code, splitting functions, renaming,
changing how something is computed but not what) should produce the same
ZIM. This page is the procedure for showing that: build the same small
region from the code before and after the change, with identical inputs
and caches, and compare the two ZIMs entry by entry. It writes down how the
`create_osm_zim.py` split was checked ([adoption-plan.md](adoption-plan.md))
and, with production flags, the Washington, D.C. head-to-head
([head-to-head-dc.md](head-to-head-dc.md)).

Two tools do it:
- `tools/golden_builds.sh` builds Monaco three times (before, a second
  "control" build of before, and after) and runs the comparison;
- `tools/golden_diff.py` compares any two ZIMs, and is what to use for
  builds you run yourself (other regions, other flags).

## One command

From a checkout with `requirements.txt` installed (plus tilemaker 3 for the
tilemaker mode):

```bash
tools/golden_builds.sh main WORKTREE ~/golden/run1          # OpenFreeMap tiles
tools/golden_builds.sh --tilemaker main WORKTREE ~/golden/run2
```

- The first two arguments are what to compare: any git ref, or `WORKTREE`
  for the checkout on disk with its uncommitted changes. A ref is exported
  with `git archive` into `WORKDIR/src-<sha>`; the repository is not
  touched.
- Inputs are downloaded into `WORKDIR/inputs` on the first run and reused
  after that: `monaco.osm.pbf` (Geofabrik) and `monaco.mbtiles`
  (OpenFreeMap). To pin them across runs or machines, copy the same files
  into each new `WORKDIR/inputs` first.
- All three builds share one download cache, `WORKDIR/cache`. The "before"
  build runs first and fills it, so the other two fetch nothing.
- Anything after `--` is passed to all three builds, for example
  `-- --wikidata` (see [Covering more of the builder](#covering-more-of-the-builder)).
- `PYTHON=/path/to/venv/bin/python` selects the interpreter. Both sides
  run in it, so they use the same libzim, osmium and tilemaker.
- Both sides get the same `create_osm_zim.py` flags. A ref too old for
  one of them (`--split-find-chips`, `--spatial-chunk-scale`) fails to
  build; compare such refs by hand with `tools/golden_diff.py`.
- The exit status is 0 when the only differences are the expected ones,
  1 otherwise, and 2 when an archive cannot be read or has no content.

Each Monaco build takes under a minute, and a run needs about 100 MB of
disk (mostly the exported source trees), plus about 1.2 GB for the
shapefiles in tilemaker mode.

### The two modes

**Default: OpenFreeMap tiles (`--mbtiles`).** The vector tiles are a fixed
input, so everything derived from them (tiles, search, Find chips, places)
must match exactly. This is the strict check, and the one to run for any
change outside tile generation. Expected result: `changed`, `only-before`,
`only-after` and `noise` all 0.

**`--tilemaker`.** Builds the tiles with tilemaker from the extract, as a
default build does, so it also covers `resources/tilemaker/` and the tile
pipeline. It needs tilemaker 3 on `PATH` and the coastline and Natural
Earth shapefiles in `WORKDIR/inputs` (`scripts/fetch-shapefiles.sh
WORKDIR/inputs`). tilemaker writes features in a different order on each
run, which changes tile bytes and, through them, where a street's search
record is placed (a few metres either way) and, rarely, which of two
near-duplicate records is kept. So this mode compares tiles by their
decoded features (`--decode-tiles`) and accepts search records whose
coordinates moved by up to 0.0001° (`--coord-tolerance 0.0001`, about
11 m; the largest shift measured between two builds of one commit was
0.00005°, and the tool prints the largest it accepted). Expected
result: `changed`, `only-before` and `only-after` 0.

## What the comparison reports

`tools/golden_diff.py before.zim after.zim [--control control.zim]
[--decode-tiles] [--coord-tolerance DEG]` reads every entry of both
archives (content, metadata, and libzim's own listings and indexes) and
compares namespace and path, title, MIME type and content (a redirect by
its target). Paths are printed with their namespace: `C/` content, `M/`
metadata, `W/` libzim's `mainPage` redirect, `X/` libzim's listings and
Xapian indexes. Each path lands in one class:

| class | meaning | fails? |
|---|---|---|
| `identical` | same title, MIME type and bytes (or the same redirect target) | no |
| `volatile` | differs in every build, by design: see the table below | no |
| `reordered` | a list of search records (`search-data/`, `category-index/`) with the same records whose sequence of (type, name) is unchanged: only records with the same type and name swapped places. The viewer lists results in list order, so any other move is `changed`. (This is what the check requires; it is not a claim that chunks are sorted by (type, name) — many are not.) Also the same JSON written differently (string escapes), and Kiwix search pages (`C/search/*.html`) whose entries (title, MIME type or redirect target, content) moved between page numbers | no |
| `tiles-equal` | (`--decode-tiles`) a vector tile with the same features in another order | no |
| `moved` | (`--coord-tolerance`) as `reordered`, except that records' coordinates (`a`, `o`) may also have moved by at most DEG degrees. A coordinate that is not a finite number (NaN, a string) is `changed`, with a note | no |
| `noise` | (`--control`) the control differs from before too, and after's entry is the control's variant: identical to it, or equal up to reordering. No coordinate tolerance applies here, so noise cannot carry after further from before than `moved` allows | no, but read it |
| `changed` | different content, title, MIME type or redirect target | **yes** |
| `only-before`, `only-after` | an entry dropped or added | **yes** |

It lists up to `--show` (default 20) paths per class.

### Expected differences

These differ between any two builds of the same code and inputs. They
were measured on 29 September 2026 by building Monaco twice from the same
commit.

| what | why |
|---|---|
| archive UUID | libzim makes a new one per file; printed, not compared |
| `M/Date` | the build date (must be `YYYY-MM-DD` on both sides) |
| `buildDate` in `map-config.json` and `streetzim-meta.json` | the build month/date; the key must be present on both sides and is the only thing ignored; every other value is compared type-strictly (`true` is not `1`, `13` is not `13.0`) |
| `X/fulltext/xapian`, `X/title/xapian` | libzim's Xapian indexes are not written reproducibly |
| order of records with the same type and name in `search-data/*.json` and `category-index/*.json` | e.g. two places called "Monaco" (the country and the commune) come out in either order. Such records can differ in other displayed fields: across five same-commit pairs, the swapped records differed in label (`l`), subtype (`s`), coordinates, Wikidata ID or Wikipedia link, so requiring the whole record sequence to match would fail every run |
| page numbers of Kiwix search pages | `C/search/monaco-6.html` and `C/search/monaco-7.html` can swap contents |
| tilemaker mode only: tile bytes | feature order (reported as `tiles-equal`) |
| tilemaker mode only: street records in `search-data/` and `category-index/street.json` | the record's point moves in the fifth decimal, at most 0.00005° measured (reported as `moved`) |
| tilemaker mode only, rarely: a different near-duplicate record kept | e.g. an empty vs a filled field; `noise` when after matches the control, otherwise `changed` (then judge it as below) |

Measured results for comparison:

| run | identical | volatile | reordered | tiles-equal | moved | changed |
|---|---|---|---|---|---|---|
| OpenFreeMap tiles, same commit twice (two pairs) | 1,330–1,361 of 1,367 | 2 | 4–35 | – | – | 0 |
| tilemaker, same commit twice (four pairs) | 1,098–1,134 of 1,168 | 2 | 14–46 | 18–20 | 0–9, largest shift 0.00005° | 0 |
| tilemaker, same commit twice, without `--coord-tolerance` | 1,098 of 1,168 | 2 | 40 | 19 | – | 9 |

The counts depend on the extract and the code; what matters is that a
refactor's run looks like its own control. As a check that the procedure
catches real changes: the commit that removed the Leaflet variant also
reworded one message in the viewer, and both modes reported exactly one
`changed` entry, `index.html`.

### Judging any other difference

Every `changed`, `only-before` or `only-after` entry needs an explanation
before the change is merged. Look at the entry in both ZIMs (content
paths without their `C/` prefix):

```bash
python - before/monaco.zim after/monaco.zim map-config.json <<'EOF'
import sys
from libzim.reader import Archive
for f in sys.argv[1:3]:
    item = Archive(f).get_entry_by_path(sys.argv[3]).get_item()
    print(f, item.mimetype, len(bytes(item.content)))
    print(bytes(item.content)[:2000].decode("utf-8", "replace"))
EOF
```

(`zimdump show --url=<path> file.zim` from zim-tools does the same.) Then
decide which it is:

- **Deliberate.** The change was meant to alter this entry (a new field, a
  fixed bug). Say so in the commit message, entry by entry, as
  [head-to-head-dc.md](head-to-head-dc.md#differences-main-vs-branch)
  does. A refactor that is meant to change nothing has no deliberate
  differences.
- **A new source of nondeterminism.** The entry also differs between two
  builds of the *after* code. Check by running the tool with `after` as
  both sides (`tools/golden_builds.sh WORKTREE WORKTREE DIR`). That is
  still a regression to fix (output should be reproducible), unless it is
  of a kind listed above.
- **A bug.** Anything else.

`noise` means after produced one of the variants the control produced, so
it is run-to-run variation, not the change under test. It should be rare
(none in the measured runs); `python tools/golden_diff.py before.zim
control.zim` shows the control's own differences, and search records
should differ only in near-duplicates, as described in
[head-to-head-dc.md](head-to-head-dc.md#main-vs-branch). An entry that
varies between runs but matches neither before nor the control is
reported as `changed`: rerun, or judge it as above.

## Covering more of the builder

The Monaco run covers tiles, search, Find chips, places, the viewer, fonts,
metadata and routing (the spatial layout; add `-- --chunk-graph-mb 1
--split-hot-search-chunks-mb 1` for the chunked graph and split search
chunks). A change in some other feature needs that feature's flags added
to all three builds:

- **Network-backed features** (`--wikidata`, `--resolve-wikidata-titles`,
  `--terrain`, `--satellite`): all builds share `WORKDIR/cache`, so the
  inputs match once it is warm. Services rate-limit (Wikidata answers
  HTTP 429), and a build that got fewer answers differs for that reason
  alone. Run the same command again until the logs show no new fetches,
  and compare only that last run; [head-to-head-dc.md](head-to-head-dc.md#shared-inputs-and-caches)
  describes this.
- **Overture addresses and places**: download the parquet files once
  (`download_overture_data.py`) and pass the same files to every build.
- **Not covered by this procedure:** `--zim-builder rust` (needs the Rust
  packer; run a small region on a machine that has it), and bundled
  Wikipedia articles (need a Wikipedia ZIM as `--wiki-articles-source`).

### Optionally: Washington, D.C.

D.C. is larger (a few thousand entries, minutes per build) and is
a published region, so it exercises more of the search and routing code.
OpenFreeMap publishes only Monaco and the planet, so D.C. uses tilemaker.
Run the builds by hand with `tools/golden_diff.py`:

```bash
W=~/golden/dc && mkdir -p "$W/inputs" && cd "$W"
curl -fL -o inputs/dc.osm.pbf \
  https://download.geofabrik.de/north-america/us/district-of-columbia-latest.osm.pbf
/path/to/streetzim/scripts/fetch-shapefiles.sh "$W/inputs"
git -C /path/to/streetzim archive main | (mkdir -p src-before && tar -x -C src-before)
for side in before control after; do
  src=$W/src-before; [ "$side" = after ] && src=/path/to/streetzim
  mkdir -p "$W/$side" && cd "$W/$side"
  ln -sfn "$W/inputs/coastline" coastline; ln -sfn "$W/inputs/landcover" landcover
  STREETZIM_CACHE_DIR=$W/cache python "$src/create_osm_zim.py" \
    --pbf "$W/inputs/dc.osm.pbf" --bbox=-77.12,38.79,-76.91,38.99 \
    --name "Washington, D.C." --routing --spatial-chunk-scale 10 \
    --split-hot-search-chunks-mb 10 --split-find-chips -o dc.zim > build.log 2>&1
done
python /path/to/streetzim/tools/golden_diff.py "$W/before/dc.zim" "$W/after/dc.zim" \
  --control "$W/control/dc.zim" --decode-tiles --coord-tolerance 0.0001
```

The D.C. control run of 28 September 2026 (production flags, see
[head-to-head-dc.md](head-to-head-dc.md#main-vs-branch)) had 179 tiles,
31 Kiwix search pages and 20 `search-data/` entries with different bytes;
after decoding, every tile matched and the search records differed only
in 22 near-duplicates.
