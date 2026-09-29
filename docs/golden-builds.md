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
- The exit status is 0 when the only differences are the expected ones.

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
record is placed (a few metres either way) and which of two
near-duplicate records is kept. So this mode compares tiles by their
decoded features (`--decode-tiles`), accepts search records whose
coordinates moved by up to 0.001° (`--coord-tolerance 0.001`, about
100 m), and uses the control build for the rest. Expected result:
`changed`, `only-before` and `only-after` 0; a few `noise` entries are
possible (see below).

## What the comparison reports

`tools/golden_diff.py before.zim after.zim [--control control.zim]
[--decode-tiles] [--coord-tolerance DEG]` reads every entry of both archives (content, metadata,
and libzim's own listings and indexes) and puts each path in one class:

| class | meaning | fails? |
|---|---|---|
| `identical` | same MIME type and bytes (or the same redirect target) | no |
| `volatile` | differs in every build, by design: see the table below | no |
| `reordered` | a JSON list with the same items in another order, or Kiwix search pages (`search/*.html`) whose contents moved between page numbers | no |
| `tiles-equal` | (`--decode-tiles`) a vector tile with the same features in another order | no |
| `moved` | (`--coord-tolerance`) a JSON list of search records that match apart from coordinates (`a`, `o`) that moved by at most DEG degrees | no |
| `noise` | (`--control`) also differs between before and the control build | no, but read it |
| `changed` | different content, MIME type or redirect target | **yes** |
| `only-before`, `only-after` | an entry dropped or added | **yes** |

It lists up to `--show` (default 20) paths per class.

### Expected differences

These differ between any two builds of the same code and inputs. They
were measured on 29 September 2026 by building Monaco twice from the same
commit.

| what | why |
|---|---|
| archive UUID | libzim makes a new one per file; printed, not compared |
| `Date` metadata | the build date |
| `buildDate` in `map-config.json`, dates in `streetzim-meta.json` | the build month/date; compared with dates masked |
| `fulltext/xapian`, `title/xapian` | libzim's Xapian indexes are not written reproducibly |
| order of tied records in `search-data/*.json` and `category-index/*.json` | records that sort equal (e.g. "Monaco" the country and "Commune de Monaco") come out in either order |
| page numbers of Kiwix search pages | `search/monaco-6.html` and `search/monaco-7.html` can swap contents |
| tilemaker mode only: tile bytes | feature order (reported as `tiles-equal`) |
| tilemaker mode only: street records in `search-data/` and `category-index/street.json` | the record's point moves in the fifth decimal (reported as `moved`) |
| tilemaker mode only, rarely: a different near-duplicate record kept | e.g. an empty vs a filled field; `noise` when the control shows it too, otherwise `changed` (then judge it as below) |

Measured results for comparison:

| run | identical | volatile | reordered | tiles-equal | moved | noise | changed |
|---|---|---|---|---|---|---|---|
| OpenFreeMap tiles, same commit twice | 1,361 of 1,367 | 2 | 4 | – | – | – | 0 |
| tilemaker, same commit twice | 1,098 of 1,168 | 2 | 40 | 19 | 9 | – | 0 |
| tilemaker, same commit twice, without `--coord-tolerance` | 1,098 of 1,168 | 2 | 40 | 19 | – | – | 9 |

The counts depend on the extract and the code; what matters is that a
refactor's run looks like its own control. As a check that the procedure
catches real changes: the commit that removed the Leaflet variant also
reworded one message in the viewer, and both modes reported exactly one
`changed` entry, `index.html`.

### Judging any other difference

Every `changed`, `only-before` or `only-after` entry needs an explanation
before the change is merged. Look at the entry in both ZIMs:

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

`noise` needs a look too. An entry is `noise` when the control build also
changed it, which says nothing about *how* after changed it. In the
tilemaker mode, compare the count with the control's and check the
records: `python tools/golden_diff.py before.zim control.zim` shows the
control's own differences, and the search records should differ only in
near-duplicates, as described in
[head-to-head-dc.md](head-to-head-dc.md#main-vs-branch). In the default
mode there should be no noise at all.

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
  --control "$W/control/dc.zim" --decode-tiles --coord-tolerance 0.001
```

The D.C. control run of 28 September 2026 (production flags, see
[head-to-head-dc.md](head-to-head-dc.md#main-vs-branch)) had 179 tiles,
31 Kiwix search pages and 20 `search-data/` entries with different bytes;
after decoding, every tile matched and the search records differed only
in 22 near-duplicates.
