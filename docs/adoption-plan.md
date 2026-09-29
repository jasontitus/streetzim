# What it would take for openZIM to adopt StreetZim directly

openZIM's review (§6.1, "Why not Option B") listed what StreetZim would need
before openZIM could use it as its maps scraper, rather than porting features
into openzim/maps. This page tracks each item: done, in progress, or left,
with the reasoning. It complements [openzim-integration.md](openzim-integration.md),
which covers the other route: porting features into maps2zim.

| # | the review asked for | status |
|---|---|---|
| 1 | Split the builder and viewer into modules, with a proper front-end build | **Done for the builder and `index.html`**; no bundler (see below). |
| 2 | Remove the hosting/operations layer | **Separated in the tree (stage 1); a separate repository is stage 2.** Operations files live in [`ops/`](../ops/README.md) with symlinks at their old paths for the build host; `web/`, `preview-proxy/` and host-edited lists follow in stage 2. `tools/check_boundary.py` (CI) keeps the builder independent of `ops/`. The 31 dead scripts are deleted (tag `pre-ops-split`). |
| 3 | Replace zimru/xapianbuilder with libzim, or bring them under openZIM | **Done.** libzim is the default; CI builds, rewrites and serves ZIMs with libzim, zim-tools and kiwix-tools only. The Rust packer is an optional speed-up. |
| 4 | Freeze and document the binary formats; drop legacy versions | **Documented and frozen** ([formats.md](formats.md), [search-records.md](search-records.md)). Dropping readers waits on rebuilding the pre-June continent ZIMs (retirement table in formats.md). |
| 5 | openZIM conventions: CLI, metadata, illustration, Zimfarm progress file, `offliner-definition.json`, Docker image, typing, linting, CI | **Partly done.** CI and the Docker image are done. Left: progress file, Zimfarm definition, `--title`/`--description`/`--creator`-style flags, type checking, wider lint rules. |
| 6 | tilemaker/osmium/GDAL in the image; fit Zimfarm resources | **Mostly done.** tilemaker 3 and osmium are in the image, and GDAL arrives as rasterio wheels. Zimfarm sizing still needs measuring on mid-size regions (see [tile-sources.md](tile-sources.md): OpenFreeMap tiles avoid tilemaker entirely). |
| 7 | Drop or replace the non-commercial satellite layer | **Opt-in and documented.** `--satellite` is off by default; a Zimfarm definition would simply not offer it. |

## 1. Splitting the large files

Rule for every split: **behaviour-neutral and verified mechanically.**

| file | before | now | how it's verified |
|---|---|---|---|
| `resources/viewer/index.html` | 11,007 lines, edited by hand | 24 parts in `resources/viewer/src/index/` (largest 976 lines), joined by `tools/build_viewer.py` | the joined output is byte-identical to the file; CI and a test fail if they drift |
| search extraction | inside `create_osm_zim.py` | `streetzim/search_extract.py` | golden-build diff |
| routing formats | under `tests/` | `streetzim/routing/` | golden-build diff |
| `create_osm_zim.py` | 7,188 lines | a 1,478-line CLI; the work happens in `streetzim/{common,tiles,satellite,terrain,addresses,zim_writer}.py` and `streetzim/routing/build.py`. `create_zim` is still one 1,900-line function | golden-build diff (fixed inputs, every entry compared) and an independent review that ran every production flag on old and new code |
| `places.html`, `routing-worker.js` | 2.5k / 1.7k lines | unchanged | small enough for now |

"A proper front-end build" in openZIM's sense (ES modules, bundler, npm) is
a larger step. The viewer must still ship as the same three slot files,
because published ZIMs are updated in place and can't gain files. The part
split keeps that constraint and adds no tooling. A bundler can come later,
as long as it emits the same three files.

## 2. Separating the operations layer

The live operations scripts (build host, gates, uploads, catalogue, PWA)
are StreetZim's own distribution. openZIM would not use them: Zimfarm
replaces them. The clean end state is a separate `streetzim-ops` repository
(or an `ops/` directory) that depends on the builder, not the other way
round.

Order, because the build host runs these by absolute path:
1. **Done:** move them under `ops/` with symlinks at the old paths (stage 1,
   [ops/README.md](../ops/README.md)).
2. Finish the phases in [scripts.md](../ops/docs/scripts.md) (optional,
   either side of the split).
3. Split the repository (stage 2: steps in ops/README.md, which need the
   build host).

## 5. openZIM conventions still to add

- **The repository split, stage 2** (item 2): the operations files are in
  `ops/`; moving them to their own repository needs checks and a switch on
  the production host, which runs them by path.
- **Zimfarm progress file** (`--stats-filename`). The builder already
  announces phases ("[3/7] …"); writing `{"done": 2, "total": 7}` from that
  hook is small.
- **Metadata flags**: `--title`, `--description`, `--long-description`,
  `--creator`, `--publisher`, `--tags`, `--illustration`, following
  maps2zim's names.
- **`offliner-definition.json`**, derived from the argparse flags (maps2zim
  generates theirs in CI).
- **Type checking** (pyright) on `streetzim/`, starting with the pure modules.
  Also widen `ruff.toml` one rule family at a time.

## Would it be worth it?

What openZIM would get by adopting StreetZim rather than porting into
maps2zim:
- search over every named feature, not just admin areas;
- category browsing;
- offline routing and navigation;
- terrain and Wikipedia;
- richer search from its own tiles (+31% street names, about 2× named places:
  [tile-sources.md](tile-sources.md)).

What it would cost:
- taking on a larger codebase (about 95k lines, most of it viewer and
  operations);
- custom formats to maintain;
- one author's history.

The items above lower that cost, but openZIM decides whether it is low
enough. Either way, the same work makes the porting route (Option A)
cheaper too.
