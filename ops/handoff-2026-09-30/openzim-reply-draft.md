> **DRAFT v6. Not committed and not posted.** To be posted directly to
> openZIM (a reply on the closed issue), not committed to the repository.
> Remove this note before posting. Before posting also:
> - replace relative links with absolute GitHub links pinned to a commit,
>   and check CI is green on that commit (it is on `-next` 82def4f);
> - line figures in §0 are recounted on `-next` 82def4f (`fc/cnt.py`,
>   `fc/grp2.py`); recount if much lands after it;
> - the maps2zim D.C. comparison (§0) is still only in the scratchpad
>   (`dc-comparison-results/head-to-head-maps2zim-dc.md`). Publish it with
>   its Appendix D package and link it, or drop the link;
> - the full-profile comparison (§0, §3) is *in progress*: add its result if
>   it has finished, or keep the wording;
> - the "small PR" sentence (§0) assumes the user has filed the maps2zim
>   `area`-pattern PR. Link it, or drop the sentence;
> - "Where the work is" describes the branches as of 29 September, late.
>   If the merges have landed when this is posted, reword it;
> - the repo docs state the Option B proposal (on `-next`), and the first
>   response is marked superseded.

# Response to "openzim/maps vs. streetzim" (second update)

We know the issue is closed. We are writing anyway because the review's
case against Option B rests on seven prerequisites, and most of them are
now met. We think that changes the answer, and we would rather say so now
than after openZIM has committed to a port.

The openZIM team compared the two tools in
[`Streetzim vs Maps.md`](https://github.com/openzim/maps/blob/763a9bea611e2a64da90146636ce821c5e40d253/Streetzim%20vs%20Maps.md),
reviewing streetzim at `abdb891` (26 Sep). It recommends Option A: keep
openzim/maps and port streetzim's features into it. It argues that Option B,
adapting streetzim, needs seven changes (its §6.1). After those, it says,
"what is left is essentially new code… Option A done the hard way", and
maintainers would still have to learn a large codebase that one person
knows.

Since then we have made most of those seven changes, by restructuring the
code rather than rewriting it. Every restructuring was checked by building
the same small regions before and after and comparing the ZIMs ("golden
builds"). They differed only as two builds of the same code differ, plus
deliberate changes listed in [head-to-head-dc.md](head-to-head-dc.md) (one
of them a bug, since fixed); see [golden-builds.md](golden-builds.md) and
[adoption-plan.md](adoption-plan.md). This note:
1. goes through the seven prerequisites (§1);
2. answers every other point the review raises (§2);
3. lists what is still open (§3);
4. adds two clarifications (§4);
5. maps the Option A roadmap to our code (§5).

## Where the work is

| | what it holds | head | state |
|---|---|---|---|
| `main` | the first round: builder modules, the formats spec, the viewer build, chip-rule sync, libzim-default CI (merge #19) | `37403b8` | merged |
| `claude/adoring-dijkstra-i2vge7` | the operations split, stage 1 (`ops/`, symlinks, the boundary check), with `main` merged in; it changes no build code | `bd09db9` | CI green; next to merge, by PR |
| `claude/adoring-dijkstra-i2vge7-builder` | the openZIM-shaped builder, on top of the operations split: the `streetzim` command, `offliner-definition.json`, the Python 3.14 image with zimscraperlib, pyright, current-only format writers, `create_zim` and `main()` as phase functions, the Zimfarm measurements | `dbfe993` | merges after the operations split |
| `claude/adoring-dijkstra-i2vge7-next` | on top of the builder branch, everything since: the viewer work of the last round (dark style, POI icons, RTL text, pinned MapLibre and fonts, ESLint, tile aliasing, the antimeridian); and, new since our last update, `--profile full\|basic`, terrain for Zimfarm, opt-in satellite, `--mbtiles-url`, Kiwix title suggestions and `--kiwix-poi-pages`, viewer polish, Wikimedia rate-limit handling, the local Zimfarm run, the offline full-profile CI build, and the build host's runbook | `82def4f` | CI green; merges after the builder branch |

The branches merge to `main` in that order, each by a PR with a merge
commit. The build host is in the middle of a rebuild round, so it moves
separately. [ops/TESTING-NEXT.md](../ops/TESTING-NEXT.md) is the runbook
for that. The host takes the operations split now, which changes no code.
It moves to the builder and `-next` code only after the round ends, and
then runs the tests only it can run: golden builds, a production smoke
build, D.C. with an offline Wikipedia, Alaska across the antimeridian,
Massachusetts, and a planet-scale comparison with maps2zim.

**We expect all of it on `main` by the end of 29 September (Pacific
time).** The merges don't wait for the host. Until then, links below point
to files as they are on the branches.

## 0. Our position: we propose Option B

We would like openZIM to adopt StreetZim as its maps scraper (Option B),
after a short Zimfarm pilot, rather than port its features into
openzim/maps one by one.

Our reason is the roadmap itself. Its first items are small, but the ones
that make an offline map useful (POI and street search, category browsing,
routing, terrain) are rated M to L. Routing alone is a multi-PR project
with its own binary format, a web-worker router and mobile memory limits.
Ports like that tend to land their first few items and then stall. Until
they finish, Kiwix users get a map with admin-area search. Option B gives
them the whole feature set now, from code whose features already run in
production, restructured and tested in CI.

**What openZIM would run.** One offliner, `streetzim`, whose recipes turn
features on and off:
- `profile` is a required recipe setting. `full`, the command's default,
  turns on:
  - Wikidata facts;
  - Wikipedia article text;
  - Overture addresses and places (the latest release, found through
    Overture's STAC catalogue);
  - terrain (hillshade and 3D);
  - every named POI in Kiwix's own search.
- `basic` turns all of them off and fetches nothing but the OSM extract
  and the shapefiles.
- Search, category chips, the places page, routing, the dark style and
  icons are on in both profiles.
- Each feature is also its own `on`/`off` setting, so a recipe can be
  `full` without Overture, or `basic` with Wikidata
  ([zimfarm.md](zimfarm.md#profiles)).
- **Satellite imagery is outside both profiles: off by default in the main
  distribution, and one setting to turn on.** See #7 for how it works and
  why.

What made B affordable is the work since the review:
- the operations layer is moving out;
- the builder has openZIM's shape;
- the outputs are verified unchanged.

It is still a sizeable codebase. After the operations split, openZIM would
own about 46k lines of code (tests, docs, data and generated files not
counted):
- 16.7k of viewer source: the 29 parts `index.html` is built from,
  `places.html` and the routing worker;
- 15.4k of builder Python (`create_osm_zim.py` and `streetzim/`), plus a
  2.6k-line Overture category table generated from Overture's data;
- 8.5k of `cloud/` modules the builder imports, and ZIM tools;
- about 5.6k of tools, resources, small scripts and one 506-line Rust
  crate (an optional packer). The 1.2k-line Leaflet variant has been
  removed.

Tests add 18.0k lines of code, plus golden corpora and a small Monaco
fixture for CI. The review counted 119k tracked lines of every kind at
`abdb891`. Counted the same way, the tree is about 168k today, and 54k of
it (operations and the website) leaves in stage 2. The growth since our
last update (about 145k) is this round's features, their tests and the CI
fixture, the generated table, and the host runbook.

The pilot we propose:
- run the `streetzim` command on Zimfarm, from our image, for a few recipes
  next to maps2zim's: a western-hemisphere region, a multi-part country and
  a dense city;
- compare the ZIMs, the build costs and the reader behaviour side by side;
- if they hold up, switch openZIM's maps recipes to `streetzim`. maps2zim
  can keep running until then. Four of maps2zim's strengths the review
  lists (tile aliasing, the dark style, icons, RTL) are now in StreetZim
  too (§2).

If openZIM still prefers Option A, §5 maps every roadmap item to our code.

**A first pass at that comparison.** We ran Washington, D.C. through a
local Zimfarm: StreetZim at `d92b187`, the profile now called `basic`,
against maps2zim 0.2.1, three runs each. The write-up
([head-to-head-maps2zim-dc.md](head-to-head-maps2zim-dc.md)) has been
through an adversarial review. We wrote StreetZim, so we are an interested
party; the plan, scripts, inputs and raw runs are published with it. Its
main caveats:
- **The machine was shared and contended**, so every timing is indicative
  only. CPU time, memory and ZIM size held up despite the load.
- **maps2zim ran as a lab control.** Its image was pre-seeded with the
  200 D.C. tiles, so it skipped the planet download and scan. Its local
  numbers cover only its other phases and are not its real cost.
- **maps2zim's real cost comes from openZIM's production records:** 49
  to 474 minutes of scraper time over 196 regions (median 77 min), and
  about an hour or more even for the smallest areas.
- **StreetZim needs about 8× more memory:** 3.97 GB PSS against 0.50 GB
  for maps2zim's non-planet phases. It also used 2.2× the CPU time of
  those phases. StreetZim's memory grows with the region (10.8 GB for the
  Netherlands).
- **ZIM sizes:** StreetZim's was 19.9 MB, with search and routing.
  maps2zim's was 143 MB, about 110 MB of it the same for any region
  (glyphs and Natural Earth).
- **Search.** maps2zim found all 4 admin areas through Kiwix's search box,
  the only kind of place it claims to find. StreetZim's in-map search
  found 20/20 streets, 20/20 addresses, 24/25 POIs and 13/15
  neighbourhoods, but no admin areas, because it does not index
  boundaries. Through Kiwix's own search box the tested StreetZim image
  found nothing: its search pages were not front articles then. That is
  fixed on `-next` and not yet measured.
- **Size of the region matters.** D.C. is StreetZim's most favourable
  case. An indicative cross-reference puts the break-even in build time
  at about Switzerland's size.

A comparison with the `full` profile is in progress; we will post it when
it is done. A planet-scale comparison on the build host, with maps2zim on
the real planet, is in the host runbook. While setting up the recipes we
also noticed that maps2zim's `area` pattern accepts more than `planet` and
`monaco`; we'll send a small PR.

What Zimfarm needs from openZIM, checked by running a Monaco recipe end to
end on a local Zimfarm (backend, worker manager and task worker at zimfarm
`917d7bc`, our image from a local registry, the profile now called
`basic`). The task succeeded, Zimfarm showed our progress numbers, and the
ZIM reached the receiver and passed zimcheck
([zimfarm.md](zimfarm.md#tested-on-a-local-zimfarm)). The changes it took:
- the image name in `DockerImageName`;
- `streetzim` in the worker's `ALL_OFFLINERS` (without it the worker never
  takes the task) and in `PROGRESS_CAPABLE_OFFLINERS`;
- the definition upload ([zimfarm.md](zimfarm.md));
- recipes with at least 4 GiB of disk when tiles are built with tilemaker:
  each task downloads the coastline and Natural Earth shapefiles until we
  bake them into the image.

We publish the image to `ghcr.io/openzim/streetzim` (or wherever openZIM
prefers) before the pilot.

What we offer: a named co-maintainer arrangement, and moving the repository
under `openzim` if B is chosen.

## 1. The review's seven prerequisites for Option B (its §6.1)

| # | prerequisite | status | evidence |
|---|---|---|---|
| 1 | Split the builder and viewer into modules; a proper front-end build | **Builder: done. Viewer: split into parts, linted, pinned; no bundler, on purpose.** | See the notes below this table. |
| 2 | Remove the hosting and operations layer | **Separated in the tree; removal is stage 2.** | See the notes below this table. |
| 3 | Replace zimru / xapianbuilder with zimscraperlib / libzim | **Done for everything openZIM would run.** | python-libzim is the default writer. CI builds, validates (`zimcheck` 3.8), rewrites and serves ZIMs with stock libzim, zim-tools and kiwix-tools only. The Rust packer and xapianbuilder are opt-in, used by the author's production wrappers (`ops/build-region-fast.sh`), and still built from the author's checkouts. |
| 4 | Freeze and document the routing formats; drop legacy versions | **Done for writing; readers retire by condition.** | [formats.md](formats.md) specifies every format byte for byte. The builder writes only SZRG v4 and SZCI v3 + SZRC v2; the v5 / SZGM writer and the v2 upgrader are retired. Legacy readers stay while published ZIMs carry them, each listed with its removal condition. |
| 5 | openZIM conventions (CLI, metadata, illustration, progress file, `offliner-definition.json`, Docker, typing, linting, CI) | **Done, except strict typing everywhere** (strict on 24 files, basic with 18 baselined findings elsewhere; §3). | See the notes below this table. |
| 6 | tilemaker, osmium and GDAL in the image; fit Zimfarm resources | **Mostly.** | tilemaker 3 and osmium are in the image, GDAL comes as rasterio wheels, and the image carries the pinned font glyphs. The coastline and Natural Earth shapefiles (~900 MB) are still fetched per task, not baked in. `basic` is measured on four regions (one of them also in the image, one in the US) and on D.C. through a local Zimfarm. `full` is measured on Monaco only, and terrain on Monaco and Luxembourg. The `full` resources for countries are estimates. Massachusetts is to be rerun (see notes). |
| 7 | Drop or replace the non-commercial satellite layer | **Done: opt-in, with a freely licensed default and labelled restricted variants.** | See the notes below this table. |

**Notes on #1 (the split).**
- The builder was 8.2k lines in `create_osm_zim.py` at `abdb891`
  ([adoption-plan.md](adoption-plan.md) uses an earlier baseline). It is
  now a 1.8k-line command plus `streetzim/`: the `streetzim` command,
  tiles, MBTiles input, terrain, satellite, addresses, Overture, search
  extraction, routing and the ZIM writer.
- `create_zim` and `main()` were one function each (1,935 and 1,109 lines
  at `abdb891`). They are now sequences of phase functions: `create_zim` is
  256 lines, `main()` 123.
- `index.html` is edited as 29 parts and joined byte-identically by
  `tools/build_viewer.py`; CI fails if the built file and its parts differ.
- ESLint runs on the viewer's scripts (as built), in CI and pre-commit.
- MapLibre GL JS 5.23.0 and the RTL text plugin are vendored. They and the
  768 Open Sans glyph ranges (plus 16 Noto ranges) are pinned by SHA-256
  in a lock file, checked on every build
  ([viewer-supply-chain.md](viewer-supply-chain.md)).
- No bundler, on purpose: published ZIMs can only swap three fixed-size
  viewer files, and the byte-identical build is what lets us prove a move
  changed nothing. The same page says what a bundler would have to
  guarantee before we add one.
- `places.html` (2.5k) and `routing-worker.js` (1.7k) are not split.
- Every move was done by script and checked with golden-build diffs, plus a
  production-flag head-to-head on Washington, D.C. The procedure is written
  up so anyone can rerun it, D.C. included:
  [golden-builds.md](golden-builds.md) (with `tools/golden_builds.sh` and
  `tools/golden_diff.py`). The D.C. results are in
  [head-to-head-dc.md](head-to-head-dc.md).

**Notes on #2 (operations).** Everything operational lives in
[`ops/`](../ops/README.md):
- the build host's queues and wrappers;
- uploads, torrents and archive.org;
- rollouts and gates;
- VM scripts and host hygiene.

It stays in this repository for now (stage 1), with symlinks at the old
paths for the build host. A CI check (`tools/check_boundary.py`) fails if
the builder depends on any of it. Stage 2 moves `ops/`, the website
(`web/`), the preview proxy and the host-edited lists into a separate
repository. It needs a switch on the build host.

**Notes on #5 (conventions).**
- The `streetzim` command takes maps2zim's flag names, `{name}_{period}`
  file names and `--stats-filename`.
- The image is Python 3.14, where zimscraperlib provides:
  - the metadata rules;
  - the illustration (SVG too);
  - downloads;
  - the output-folder check.
  A tested copy of the metadata rules serves Python 3.12.
- `offliner-definition.json` is generated from the parser, and CI validates
  it with Zimfarm's own `OfflinerSpecSchema`. `profile` is a required
  string-enum and each feature an optional `on`/`off` enum. A test runs
  recipes through Zimfarm's own models and `compute_flags` and checks
  which features `streetzim` then builds.
- pyright covers the builder, its `cloud/` modules and the tools: strict
  on 24 files, basic mode on the rest with 18 baselined findings (down
  from 39). New findings fail CI.
- ruff runs pyflakes, bugbear, pyupgrade, pylint's error and warning
  sets, ruff's own rules and a few more on everything except `ops/`, which
  stays on the original syntax-plus-pyflakes gate until it leaves
  (`ruff.toml` lists the rules and why the rest are off). Findings that
  earlier per-file ignores had hidden were fixed.
- pre-commit runs ruff, the pyright gate, the viewer build and asset
  checks, ESLint, the offliner-definition check and the boundary check.
- CI measures coverage (pytest-cov) and uploads it to Codecov once a
  token is set.
- A wheel (`python -m build`) carries the runtime files; tilemaker and
  osmium still have to be on PATH. CI installs it into a fresh environment
  and, with downloads blocked and from outside the checkout, checks that
  it finds every file a build reads. A manual workflow publishes to PyPI
  with trusted publishing; nothing is published yet
  ([packaging.md](packaging.md)).
- There is no task runner.

**Notes on #6 (resources).** `basic` profile (search, chips, routing),
measured with Python 3.11 outside Docker on a 4-core, 15 GB machine:

| region | extract | wall | peak memory (PSS) | peak disk | ZIM |
|---|---|---|---|---|---|
| Luxembourg | 56 MB | 3 min | 4.0 GB | 0.35 GB | 55 MB |
| Switzerland | 679 MB | 70 min* | 4.6 GB | 4.4 GB | 654 MB |
| Netherlands | 1.63 GB | 92 min | 10.8 GB | 11.0 GB | 1.22 GB |

\* The Switzerland run shared the machine with other builds, so its time
is an upper bound.

In the Netherlands, memory and disk peak while the ZIM is written (the
search index goes in last). Two runs with less free disk failed there.
For a country that size a task needs about 12 GB of RAM and 15 GB of disk,
plus the extract and shapefiles. Details are in [zimfarm.md](zimfarm.md).
Since then, on a shared machine (so wall times are upper bounds):
- Luxembourg in the Docker image (Python 3.14) matched the run outside it
  (4.1 GB against 4.0 GB, the same ZIM).
- Rhode Island, a US state of Luxembourg's size, cost the same 4.0 GB.
- A Massachusetts run (310 MB extract) was stopped in the ZIM step when
  the shared machine ran low on disk, and has no result yet.

For `full` and terrain:
- **The `full` profile** on Monaco peaked at 2.8 to 3.4 GB, against
  2.1 GB for `basic`. Its extra time is waiting on the Wikimedia APIs, not
  computing: they rate limit, and each source waits within a budget.
  zimfarm.md gives the recipe resources for `full` at country scale as
  estimates. A large-region `full` recipe should allow hours on top of
  `basic`'s time.
- **Terrain** added 0.3 GB of disk and 1.5 minutes of wall time to
  Luxembourg, most of it the DEM download, and 5% to its ZIM. Peak memory
  did not change. For Switzerland and the Netherlands the cost is
  extrapolated, not built: a DEM download of 0.6 to 0.8 GB, about 1.5 to
  2 minutes of CPU, and 0.6 to 7% of the ZIM
  ([zimfarm.md](zimfarm.md#terrain-cost)).

**Notes on #7 (satellite).**
- **Off by default in `streetzim`, and outside both profiles.** One recipe
  setting (`satellite: true`) adds EOX's Sentinel-2 cloudless mosaic for
  2016, which EOX licenses under **CC BY 4.0**. This is the "earlier EOX
  vintage" the review suggested checking.
- **The 2021 mosaic is CC BY-NC-SA 4.0.** It needs an explicit
  acknowledgement (`--satellite-source s2cloudless-2021
  --satellite-accept-noncommercial`). Without it the task fails before any
  download, with a message naming the licence.
- **A 2021 build is labelled as restricted:**
  - Flavour `satellite-nc`, so it is a separate book from the openly
    licensed one;
  - the tag `non-commercial`;
  - a "Restricted" note at the end of the LongDescription;
  - a `License` that opens with "Non-commercial use only";
  - a "Restricted" notice in the viewer's About panel.

  A library filter can pick restricted variants out by Flavour or tag.
- **Credits.** `License` and the viewer's credits list only the layers a
  ZIM contains. With satellite, EOX's own attribution also shows on the
  map while the imagery is shown.
- **Why off by default.** Keeping satellite out of the default is a choice
  about openZIM's main distribution, not a vague licensing worry:
  - each source's licence is known and stated, and is checked against
    EOX's own pages ([zimfarm.md](zimfarm.md#satellite-imagery));
  - the 2021 imagery is non-commercial, and openZIM's ZIMs are mirrored,
    bundled and resold by others, so it is kept out of the default and
    clearly labelled when included;
  - the 2016 imagery has no such restriction.
- **One concrete step.** We recommend openZIM get written confirmation from
  EOX (cloudless@eox.at) before publishing 2016 satellite ZIMs widely. The
  licence page and the WMTS both say CC BY 4.0 for 2016, but EOX's License
  Summary page does not carve 2016 out of its general terms, and a short
  written answer settles it.
- **StreetZim's own builds keep the 2021 layer.**

## 2. Every other point the review raises

| review point (section) | answer |
|---|---|
| **No CI** (§2, §5) | Six CI jobs run on every push and weekly, and a seventh runs the `full` profile against the live sources weekly; see the notes below this table. |
| **Hardcoded local paths, e.g. `/Users/jasontitus/...`** (§5) | See the notes below this table. |
| **Personal forks (`zimru`, `xapianbuilder`)** (§2, §5) | Optional; see #3. |
| **"More than 70 shell scripts"; "about 50 `cloud/` helpers"; one-off scripts, queues, rollout lists** (§3.2, §5) | See the notes below this table. |
| **Monolithic files** (§5) | Builder done, viewer partly split; see #1. The largest remaining functions are `repackage` (`cloud/repackage_zim.py`, 919 lines, a tool for published ZIMs) and `extract_routing_graph` (663). |
| **Category chip rules in three places** (§5) | Still three places, but now kept in sync by CI; see the notes below this table. |
| **Custom binary formats, several versions** (§5) | See #4 and §4. |
| **Heavy builds (up to ~100 GB RAM; tilemaker `--fast` 32 GB+)** (§5) | True for continent-scale builds with every layer. `basic` measured 4.0–4.6 GB PSS on Luxembourg and Switzerland and 10.8 GB on the Netherlands (1.6 GB extract), where writing the ZIM is the peak. Memory does not follow extract size closely, so we can't extrapolate yet. `full` adds a little memory and a lot of waiting on the Wikimedia APIs (#6). Planet or continent recipes would need large workers, or regional recipes instead. |
| **Outside openZIM scope** (§5) | Agreed; see #2. |
| **Satellite is non-commercial** (§4, §5) | Addressed: opt-in, a CC BY 4.0 default source, and the non-commercial source gated and labelled; see #7. |
| **Stale docs (no coastline, placeholder fonts)** (§5) | Fixed; see the notes below this table. |
| **Bus factor** (§5, §6.1) | Improved, not solved; see the notes below this table. |
| **Size and learning cost** (TL;DR, §6.1) | About 46k lines of code after stage 2, plus 18k of tests; see §0. |
| **Region definition** (§4) and **the antimeridian** (§3.1) | See the notes below this table. |
| **Coastline not downloaded** (§3.2) | Fixed. `scripts/fetch-shapefiles.sh` fetches it, the `streetzim` command fetches it when missing, and the builder warns (or fails, with `STREETZIM_REQUIRE_SHAPEFILES=1`). |
| **Python 3.12** (§2) | Runs on 3.10 and later. The image and CI's 3.14 job use 3.14; the other CI jobs and the author's build host use 3.12 until it moves. |
| **MapLibre from unpkg at build time** (§2) | Fixed. MapLibre GL JS 5.23.0 is vendored, checked against the npm registry's hash when pinned, and against the lock file on every build ([viewer-supply-chain.md](viewer-supply-chain.md)). |
| **Fonts from the openmaptiles font CDN; no sprites** (§4) | See the notes below this table. |
| **Python dependencies** (§2) | Split into runtime, dev and ops requirements; see the notes below this table. |
| **Distribution: Docker, PyPI** (§2) | A Dockerfile that CI builds and runs, but the image is not yet published to a registry (Zimfarm needs it on ghcr.io). A wheel that CI checks from a clean install. A manual workflow publishes it to PyPI with trusted publishing; nothing is published yet ([packaging.md](packaging.md)). |
| **Overture licences** (§4) | Each ZIM built with Overture data carries `overture-sources.json`, the dataset list, and its `License` metadata points to it. |
| **Tiles only from OpenFreeMap, whole planet per task** (§3.1, about maps2zim) | StreetZim builds its own tiles from the regional extract. It can also build from an OpenMapTiles MBTiles such as OpenFreeMap's (`--mbtiles-url`), cut to the area by maps2zim's own `TileFilter` rule, reading only the area's tiles. On Zimfarm today that works only with a regional file downloaded per task: sharing a planet between tasks needs a worker volume that Zimfarm doesn't have ([zimfarm.md](zimfarm.md#building-from-ready-made-tiles---mbtiles-url)). |
| **Experimental Leaflet raster variant** (§1) | Removed. |
| **Light style only; no POI icons** (§1) | Fixed, with right-to-left text too; see the notes below this table. |
| **Low-zoom background** (§1) | Different by design: Natural Earth lakes and OSM water, where maps2zim uses shaded relief. Terrain (on in `full`) now also covers the low zooms. Its tiles start two levels below the lowest zoom the viewer can show, each filled over its whole square. |
| **Other UI: remembered view, home button, About page, error page** (§1) | Added; see the notes below this table. |
| **Language fixed to `eng`** (§3.1, about maps2zim) | The same here: the metadata language is fixed and the viewer is not translated. |

**Notes on CI.** Six jobs run on every push, pull request and weekly:
- **checks:**
  - lint and the pyright gate;
  - the viewer-build, viewer-asset lock, ESLint and offliner-definition
    checks, including Zimfarm's schema;
  - the core/ops boundary check;
  - the Python unit tests and the ops tests, with coverage;
  - seven Node test files (two of them test the website's ZIM reader and
    move with it in stage 2);
- **wheel:** the sdist and wheel built, installed into a fresh
  environment, and checked from outside the checkout;
- **python-3-14:** the unit tests with zimscraperlib installed;
- **monaco-e2e:**
  - a real Monaco build from Geofabrik with tilemaker and production
    routing;
  - `validate_zim.py` with `zimcheck` 3.8;
  - a headless-browser test of the viewer, served from the ZIM directly
    and through kiwix-serve;
  - the rewrite tools (repackage, in-place viewer patch);
  - a build through the `streetzim` command, `basic` plus terrain from
    the Copernicus buckets;
- **openfreemap-tiles:** a build from maps2zim's own OpenFreeMap tiles;
- **docker:**
  - the image built;
  - `streetzim --profile full` run in it the Zimfarm way on Monaco, as a
    non-root user and with `--network none`;
  - every input comes from a committed fixture: the extract, Overture
    files of a pinned release, and Wikidata and Wikipedia caches;
  - then `zimcheck`, and a check that the Overture, Wikidata and Wikipedia
    data landed in the ZIM;
  - it leaves terrain out, since the DEM is not in the fixture (§3).

A seventh job, **live-full**, runs weekly and on demand. It builds the same
`full` Monaco in the image against the live sources, terrain included.
Overture must work; Wikidata and Wikipedia only warn, since their APIs
rate limit.

**Notes on hardcoded paths.**
- The builder has no machine paths.
- The Mac-only scripts were deleted. `/Users/...` remains in two `ops/`
  files and `/home/ot/...` in several, all leaving in stage 2.
- `/storage/streetzim` (the author's build host) appears in:
  - `ops/`, and the files that move with it;
  - fallback interpreter paths in three core JS tests;
  - MAINTAINING.md.
- `patches/README.md` (libzim patch notes) now uses generic example paths.

**Notes on the scripts.**
- The dead scripts (31 in the inventory) were deleted.
- Of the live shell scripts:
  - 38 moved to `ops/` (plus two new helpers there);
  - 2 are core: the shapefile fetcher and the route identity suite
    (joined since by the golden-build runner);
  - 2 move with `web/` in stage 2.
- `cloud/` now holds what the builder imports and the ZIM tools (validate,
  repackage, viewer patch, serve for tests), plus a few developer tools
  (e.g. `route_cli.py`). The PWA's deploy script and smoke tests move with
  `web/` in stage 2. The rest is in `ops/cloud/`.
- The inventory is [ops/docs/scripts.md](../ops/docs/scripts.md).

**Notes on the chip rules.**
- The places are:
  - `cloud/chip_rules.py` (build side);
  - `places.html` `CATEGORIES`;
  - `index.html` `EXPLORE_CHIPS`.
- They are still three, because published ZIMs can only swap their three
  viewer files, so the viewer copies stay inline.
- `tests/chip_rules_js.test.mjs` now checks `places.html`'s copy field by
  field against `chip_rules.py`, and `index.html`'s copy by id and label.
  It also checks that JS and Python classify a shared corpus the same way.
- A mismatch fails CI.
- The same change fixed `repackage_zim --split-find-chips` dropping the
  food chips.
- The viewer now hides chips that a ZIM cannot serve, rather than showing
  chips that load nothing. Tilemaker builds keep a POI's real type (a fuel
  station had been stored as "amenity", so the Gas chip was empty).

**Notes on fonts.**
- The 768 Open Sans ranges still come from the openmaptiles font CDN, and
  16 Noto Sans ranges come from protomaps' basemaps-assets on GitHub.
- Every range is pinned by SHA-256, cached by content, and carried in the
  Docker image, so a Zimfarm task downloads none. A mismatch stops the
  build.
- The Noto glyphs (Arabic, Hebrew, Armenian, Georgian, Thai and Lao, which
  Open Sans lacks) are merged into the Open Sans ranges.
- Sprites: none, on purpose. The POI icons are drawn in the viewer (see
  the notes on the other UI).

**Notes on stale docs.**
- The README was rewritten.
- `MAINTAINING.md` covers core vs. operations, releases and a debt list.
- `formats.md`, `zimfarm.md` and `adoption-plan.md` were added, and since
  then `golden-builds.md`, `head-to-head-dc.md`, `viewer-supply-chain.md`,
  `tile-aliases.md` and `packaging.md`. `zimfarm.md` now covers the
  profiles, the failure policy, terrain and satellite costs and licences,
  `--mbtiles-url`, and the local Zimfarm run.

**Notes on the bus factor.**
- There is CI, a maintainers' guide, a formats spec, and the golden-build
  procedure and D.C. head-to-head written up so someone else can rerun
  them.
- There is still one author.

**Notes on the region definition and the antimeridian.**
- `streetzim --include-poly` takes `.poly` URLs, as maps2zim does, and a
  Geofabrik one selects its extract. Like maps2zim, the cut is by bounding
  box.
- A polygon whose parts are far apart (the Netherlands and its Caribbean
  islands) is built from the part with the most land, and the log names
  the parts left out.
- **Areas across the antimeridian are supported** (Fiji, Chukotka,
  Kiribati; `--bbox` with minlon > maxlon), in the builder and in the
  author's own region scripts, which are now set up to build Alaska with
  the western Aleutians. A box wider than 180° is still refused, with a
  clear error: it would be a band round the world, and a planet build is
  not a bounding-box cut
  ([formats.md](formats.md#areas-across-the-antimeridian)).

**Notes on dependencies.**
- `requirements.txt` is the runtime set only; `requirements-dev.txt` adds
  tests and lint, `requirements-ops.txt` adds `internetarchive` for `ops/`.
- The image leaves out `web/` and other non-build files, but still copies
  `ops/` until it leaves in stage 2.

**Notes on the other UI.**
- **Dark style:** follows the reader's colour scheme (`?theme=` forces
  one), and now the UI chrome follows it too. Switching live keeps search
  results, the route and satellite mode.
- **POI icons:** Maki (CC0) icons drawn in the viewer on category-coloured
  discs, rather than a sprite file, so updating an existing ZIM's viewer
  brings them too.
- **Right-to-left text:** MapLibre's RTL plugin, vendored and pinned,
  loaded only when a label needs it. With the merged Noto glyphs, Arabic
  and Hebrew labels render shaped, offline.
- **Remembered view, Home button, About panel**, and clear pages when
  WebGL or Fetch is missing or start-up fails. `#map=` / `#dest=` deep
  links work as before.
- **Map bounds:** the view is held to the area's box (maxBounds), as in
  maps2zim.
- **Units:** the default distance unit follows the reader's locale, and
  every distance follows the scale bar's units. Results now print "1 mi"
  rather than "1 Mi".
- **Kiwix's own search:**
  - the search pages are now front articles, so Kiwix's title suggestions
    return them;
  - the D.C. comparison found that they did not before;
  - `--kiwix-poi-pages` (on in `full`) gives every named POI a page, so
    Kiwix's full-text search and suggestions find POIs too. It costs about
    12 to 19% of a country's ZIM.
- **Search before and after.** We checked search on every surface (the
  map's search box, category chips, the places page, Kiwix title and
  full-text search) on Monaco served by kiwix-serve, in light and dark.
  The results were the same before and after the restructuring.

## 3. Still open

**Viewer and user-facing:**
- **No bundler**, by choice for now; see the notes on #1.
- **Tile aliasing is in, but it is not the size saving we expected.**
  - Identical tiles are now stored once, as ZIM aliases, as maps2zim does.
  - On Monaco it saved 0.1–0.25% of a tiles-only ZIM. An alias still
    needs a directory entry the size of the item it replaces, zstd had
    already squeezed out most of the repetition, and a duplicate vector
    tile is only about 56 bytes.
  - For a sea-heavy country we estimate well under 1%.
  - It also silences zimcheck's "Redundant Data" warnings (139 to 1 on
    Monaco).
  - It may matter more for raster tiles, which sit in uncompressed
    clusters; that is not measured yet ([tile-aliases.md](tile-aliases.md)).
- **Areas are bounding boxes.** Polygon filtering, the review's item 2,
  would help both tools.
- **No admin-area search.** Boundaries are not indexed, so a search for a
  district or county finds only places and POIs of that name (0 of 4 in
  the D.C. comparison, where maps2zim found 4 of 4).
- **Search finds places by name, not by type:** searching "pharmacy"
  finds only pharmacies with the word in their name. The category chips
  cover types.

**QA and dependencies:**
- **Typing depth:** 18 baselined pyright findings in basic-mode files.
- The routing reference-router tests need local ZIMs, so they don't run
  in CI.
- **Terrain in CI's offline build.** The `docker` job builds `full`
  without network access and leaves terrain out, because Monaco's DEM
  (about 18 MB) is not in the fixture. Terrain is built from the live
  buckets on every push (`monaco-e2e`) and weekly in the image
  (`live-full`), but not offline.
- **Image:**
  - publish it to ghcr.io; there is no workflow for that yet;
  - bake in, or cache, the shapefiles;
  - drop `ops/` from it after stage 2.

**Evidence and resources:**
- **Build cost at country scale.**
  - In `basic`, the search step is 70% of Switzerland's time and 43% of
    the Netherlands'.
  - Writing the ZIM is 37% of the Netherlands' time, and its memory and
    disk peak.
  - Neither is profiled below the phase level yet.
  - `full` is measured only on Monaco, so its country rows in zimfarm.md
    are estimates.
  - The US region measured so far (Rhode Island) is small; Massachusetts
    is to be rerun with enough disk.
- **The comparison with maps2zim.** The D.C. first pass is done (§0). A
  comparison with StreetZim's `full` profile is *in progress*. A
  planet-scale comparison on the build host is in the host runbook.
- **Wikipedia images not yet compared.** The D.C. rerun with real article
  text matched the published ZIM (1,561 articles against 1,551; the
  differences come from changed Wikidata links). Images need an offline
  Wikipedia ZIM, and that run is on the build host's list
  ([head-to-head-dc.md](head-to-head-dc.md)). The rerun also found that a
  rate-limited request could be cached as a missing article. That is
  fixed: rate limits are never cached, `Retry-After` is honoured, and
  each source has a wait budget.
- **Wikipedia on Zimfarm is text only by default.** A Zimfarm task has no
  persistent storage, so `full` takes article text from the Wikipedia API.
  Images need a Wikipedia ZIM downloaded per task (6.5 to 119 GB).
- **A shared planet on Zimfarm.** `--mbtiles-url` works on Zimfarm today
  only with a regional MBTiles downloaded per task. Sharing one planet
  file between tasks would need an openZIM change: a read-only worker
  volume mounted into scrapers, or a cache the worker manager keeps
  across tasks. Neither exists at `917d7bc`.
- **Overture's dead-website filter** has no public source, so Zimfarm
  builds keep Overture websites that may be dead.
- **EOX's written confirmation** for the 2016 imagery (#7).

**Structure and people:**
- **Merging:** the operations split, then the builder branch, then
  `-next`, each by PR. The build host moves to the new code after its
  rebuild round ends, and then runs its own tests
  ([ops/TESTING-NEXT.md](../ops/TESTING-NEXT.md)).
- **Operations split, stage 2:** a separate repository, with a switch on
  the build host.
- **The ZIM writer on zimscraperlib's `Creator`:** blocked on the
  author's build host moving to Python 3.14.
- **A second maintainer.**

## 4. Two clarifications

These support the review's points rather than correct them. They matter to
whoever does the port.
- **Why the chip rules live in three places.** The build side had one
  source (`cloud/chip_rules.py`). The viewer keeps two inline copies
  because in-place updates of published ZIMs can only rewrite the three
  existing viewer files, so a new JSON entry can't be added. For
  openzim/maps, which has no such constraint, the review's build-time JSON
  is the better design.
- **Format versions.** The review says the viewer baked into a ZIM must
  understand whichever version that ZIM ships. The constraint is wider:
  the `/drive/` PWA serves the current viewer to any ZIM, so its reader
  must handle every version still in circulation. That is why readers
  retire by condition, not by date.

## 5. If openZIM prefers Option A

All the code below is MIT, reusable in GPL-3.0 openzim/maps with
attribution:

| roadmap item | where to look |
|---|---|
| 1. Populated places in search | nothing specific: our search indexes OSM `place=*` features from the tiles, so cities and towns are in it |
| 3. Retry/concurrency wrapper for tile fetches | the `zimtile://` protocol (`resources/viewer/src/index/110-debug-and-zimtile-protocol.js`) and `docs/zim-packaging-gotchas.md` |
| 4. POI/street search from vector tiles, sharded | `streetzim/search_extract.py`, `cloud/search_shards.py` plus the viewer's `search-shards` block, `docs/search-prefix-locality.md` |
| 5. Category chips | `cloud/chip_rules.py`, `cloud/chip_shards.py`, `docs/find-chip-shards.md` |
| 6. Regional tiles without the planet download | our tilemaker pipeline on a Geofabrik extract (`streetzim/tiles.py`, `resources/tilemaker/`); or `streetzim/mbtiles.py`, which cuts an OpenFreeMap-layout MBTiles to an area with one index search per tile column and keeps its deduplication; the measured costs are in [zimfarm.md](zimfarm.md) |
| 7. Terrain | `streetzim/terrain.py` (whole-square fill, strict coverage audit, fail-fast download), `docs/elevation-compression-analysis.md`, and the costs in [zimfarm.md](zimfarm.md#terrain-cost) |
| 8. Routing | `streetzim/routing/build.py` (graph), `streetzim/routing/spatial.py` (cells), `resources/viewer/routing-worker.js`, `docs/formats.md`, `docs/routing.md`; the reference router `streetzim/routing/astar.py` and `tests/test_route_identity.py` (needs local ZIMs) |
| 9. Wikidata / Wikipedia | `wikidata_cache.py`, `cloud/wikidata_titles.py`, `cloud/wiki_articles.py` (reads articles from a local Wikipedia ZIM, or the API), `cloud/wikimedia_http.py` (rate limits) |
| 10. Satellite imagery | `streetzim/satellite_sources.py`: the 2016 EOX mosaic is CC BY 4.0, as the review suggested checking; see #7 |

For item 2, polygon filtering, we have nothing to offer yet: our `.poly`
parser keeps only each ring's bounding box.

StreetZim's search and chips also build from maps2zim's own OpenFreeMap
tiles, in CI ([tile-sources.md](tile-sources.md),
[openzim-integration.md](openzim-integration.md)).

We are happy to review ports, especially routing and search sharding, and
to upstream the Kiwix reader findings in `docs/zim-packaging-gotchas.md`.
Either way, StreetZim will keep publishing its own fuller ZIMs (satellite,
terrain, Wikipedia), as the review's §6.4 suggests.
