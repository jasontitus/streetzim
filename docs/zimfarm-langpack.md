# Language packs on Zimfarm: `streetzim-langpack`

`streetzim-langpack` (`streetzim/langpack_cli.py`) makes the parts of a
StreetZim region's language versions ([proposal-kiwix-language-assembly.md](proposal-kiwix-language-assembly.md)):
Wikipedia packs, search packs and `streetzim-recipe/2` recipes. It is the
second StreetZim offliner, beside `streetzim` ([zimfarm.md](zimfarm.md)),
in the same Docker image, and `offliner-definition-langpack.json`
describes its flags for Zimfarm. It runs the tools StreetZim's own
pipeline runs ([language-pack-pipeline.md](language-pack-pipeline.md)),
without the parts that are StreetZim's hosting: archive.org, the builder
page, torrents, the browser checks.

A region's parts take three kinds of task:

1. **The base**: `streetzim --multilingual` ([zimfarm.md, "Multilingual
   maps"](zimfarm.md)), published like any ZIM. The packs and recipes are
   for that very file: a rebuilt base needs new packs and recipes.
2. **One pack task per language**: `--mode pack`, a Wikipedia pack for
   the base in one language.
3. **One menu task per region**, once its packs are published:
   `--mode menu`, the search packs and the recipes.

```sh
# a pack (one per language)
docker run --rm -v "$PWD/out:/output" streetzim \
  streetzim-langpack --mode pack \
    --base-url https://download.kiwix.org/zim/maps/osm_mul_monaco_2026-10.zim \
    --language fr --output /output --stats-filename /output/task_progress.json

# the menu, once the packs are published
docker run --rm -v "$PWD/out:/output" streetzim \
  streetzim-langpack --mode menu \
    --base-url https://download.kiwix.org/zim/maps/osm_mul_monaco_2026-10.zim \
    --pack-url https://download.kiwix.org/zim/maps/osm_monaco_wiki-fr_2026-10-23_6e3ea481-c4fc1fd8331a.zim \
    --pack-url https://download.kiwix.org/zim/maps/osm_monaco_wiki-de_2026-10-23_6e3ea481-8005ec3bab41.zim \
    --pack-url https://download.kiwix.org/zim/maps/osm_monaco_wiki-en_2026-10-23_6e3ea481-287664a5b212.zim \
    --url-template 'https://download.kiwix.org/zim/maps/{file}' \
    --output /output --stats-filename /output/task_progress.json
```

(The URLs are examples of where Zimfarm might publish the parts.) On the
command line `streetzim-langpack pack ...` and `streetzim-langpack menu ...`
are the same as `--mode`; Zimfarm runs one command with flags, so the mode
is a flag. `--base-url`, `--wikipedia-zim-url` and `--pack-url` also take
local files on the command line, read where they are (a Zimfarm task sees
only its own `/output`, so its recipes give URLs).

## Flags

| Zimfarm key | flag | mode | |
|---|---|---|---|
| `mode` | `--mode` | both, required | `pack` or `menu`. Each choice lists its own flags as dependents, so Zimfarm's form shows them with their mode and clears the other mode's when a recipe is saved |
| `base_url` | `--base-url` | both, required | the multilingual base |
| `region` | `--region` | both | the region id in the parts' names; default from the packs' names, else the base's file name or Name (`osm_mul_monaco`: `monaco`). Every name that gives one must agree |
| `jobs` | `--jobs` | both | processes: articles read and cleaned at a time (pack), search packs built at a time (menu); default 4 |
| `language` | `--language` | pack, needed | the Wikipedia language, as Kiwix names its Wikipedia ZIMs (`de`, `fr`, `ja`) |
| `wikipedia_zim_url` | `--wikipedia-zim-url` | pack | default: the newest `wikipedia_<xx>_all_maxi` ([below](#the-wikipedia-zim)) |
| `images` | `--images` | pack | `none`, `lead` (default, as StreetZim's own packs) or `all` (up to 12 an article) |
| `no_wikidata_text` | `--no-wikidata-text` | pack | leave out the place names and descriptions in the language from Wikidata |
| `pack_url` | `--pack-url` | menu, at least one | a pack of this base, named `osm_<region>_wiki-<xx>_...zim` as `--mode pack` names it; one per language; a `list-of-url` on Zimfarm, one flag per pack |
| `languages` | `--languages` | menu | the map languages to offer; default the core set ([below](#which-versions)) |
| `url_template` | `--url-template` | menu | each part's URL in the recipes, `{file}` its file name; default `{file}`, relative to wherever the recipes are published |
| `output`, `stats_filename` | `--output`, `--stats-filename` | both | `/output` and `/output/task_progress.json`, as for `streetzim` |

Command line only (not in the definition): `--tmp`, `--dl`
([below](#temporary-files-downloads-and-caches)), `--zimcheck`,
`--overwrite` (replace an earlier `recipes/` folder), `--keep-temp`.

These are usage errors (exit 2), found before anything is downloaded: a
flag of the other mode, or a mode without its needed flag; region ids that
disagree (`--region`, the packs' names, the base's file name); two
`--pack-url` for one language, or one not named like a pack; a Wikipedia
ZIM URL with a query or fragment, or without a `/zim/` path; `--tmp` or
`--dl` inside `--output`. A zimcheck that is not zim-tools' (`--zimcheck`,
else `$PACK_GATE_ZIMCHECK`, else `zimcheck` on PATH; the image has
zim-tools 3.8.0's) stops the task before any download too (exit 1). After
the download, the base's Name must give the same region, and each pack's
articles the language its name says.

## `--mode pack`

1. Fetch the base, and the language's Wikipedia ZIM.
2. `tools/langpack/prefetch.py`: the language's `page_props`, `page` and
   `redirect` SQL dumps from dumps.wikimedia.org (the Q-ID to title map)
   and the labels and descriptions of the base's Q-IDs from Wikidata
   (`wbgetentities`, at most a request a second, `maxlag=5`, `Retry-After`
   honoured, StreetZim's user agent: `cloud/wikimedia_http.py`), in up to
   three rounds a minute apart. Wikidata text still incomplete after them
   is said and the pack is built (its statistics say what it lacks); a
   title map that could not be made, or a prefetch that stopped, ends the
   task with the cause.
3. `tools/build_wiki_pack.py` (`streetzim/wiki_pack.py`) offline, under
   the production time limit (3 h per started 3 GB of base, at least 3 h).
4. `tools/langpack/builder_ok.py` accepts the builder's result (this
   run's statistics and the pack's size; of the builder's own checks, only
   escaped links its zimcheck misreads may fail, and the next step checks
   every link again), then `tools/langpack/pack_gate.py`: zim-tools'
   `zimcheck -A` (where each article's link back to the base's map is the
   only dangling link allowed), libzim's `Archive.check()`, the metadata,
   `pack.json`, and that the pack was built for this base.
5. Only a pack that passes reaches `/output`:
   `osm_<region>_wiki-<xx>_<YYYY-MM-DD>_<first 8 hex of the base's UUID>-<first 12 hex of its SHA-256>.zim`,
   with its gate and the builder's statistics beside it
   (`.zim.gate.json`, `.zim.stats.json`) ([names](#names)).

### The Wikipedia ZIM

Without `--wikipedia-zim-url`, the newest full Wikipedia with pictures in
the language: the `maxi` entry of `wikipedia_<xx>_all` in Kiwix's OPDS
catalog (`library.kiwix.org/catalog/v2/entries?name=wikipedia_<xx>_all`,
which lists the current file of each flavour), else the newest
`wikipedia_<xx>_all_maxi_<YYYY-MM>.zim` in the download.kiwix.org listing.
Whichever server the bytes come from (download.kiwix.org redirects to a
mirror, or the URL names one), the file is checked against the SHA-256 in
the `.meta4` that download.kiwix.org gives for the same path from `/zim/`
on, before use; a URL with a query or fragment, without a `/zim/` path, or
whose `.meta4` gives no SHA-256, fails. A local file (command line only)
is read where it is. A full ZIM is needed (not `top` or `mini`): every
article the region's places link to is looked up in it.

## `--mode menu`

1. Fetch the base and every `--pack-url` (one on download.kiwix.org is
   checked against its `.meta4` SHA-256); gate each pack again
   (`pack_gate.py`). A pack that fails, or that was built for another
   base, is not used. The disk is checked for the search packs and the
   largest version's checks ([below](#disk)).
2. Plan the menu (`tools/langpack/region_meta.py`, [below](#which-versions)).
   The versions' Titles are the region's name in each language from
   Wikidata where the base's country or region record names it (one
   request, at most 2 minutes of throttling), else the base's Title.
3. `tools/langpack/search_packs.py`: a search pack for each version
   that carries packs (libzim's title and full-text indexes over the
   base's pages and the version's packs), indexed in the version's own
   language (`recipe2.search_lang`: its stemmer and stopwords, so a German
   speaker's "Kirchen" finds "Kirche"). Two versions share one only with
   the same packs and the same language, so in practice there is one per
   version: Monaco's de, en and fr versions get three, and en and fr,
   which carry the same packs, one each (`search-en-fr`, `search-en-fr.fr`).
   A version that lacks a pack it must carry gets none (make_site.py
   refuses it). Each is then renamed by its content ([names](#names))
   before any recipe names it.
4. `recipe2.py menu`: each version's recipe.
5. Each version is merged and checked ([below](#what-is-checked)).
6. `make_site.py` writes the recipes with their URLs and `index.json`;
   `site_check.py --no-archive` checks them as a whole: each recipe is the
   checked one, has a passing verdict for its exact bytes (MD5, UUID,
   size), the base's and every pack's chunk SHA-256s match, the segments
   add up, the per-version download stays within 2 GiB of packs and the
   recipe within 32 MiB, and every URL ends in its file's name.
7. Published to `/output`: the search packs the published versions use
   (`osm_<region>_search-<langs>[.<L>]_<date>_<uuid8>-<sha12>.zim`, `.<L>`
   unless the set's one language beyond the region's is the version's own;
   the only ZIMs this mode writes) and `recipes/`:

| file | |
|---|---|
| `recipes/index.json`, `recipes/region.json` | the region and its menu: per version the language, recipe path, size, MD5, packs and download bytes (`streetzim-build-index/1`); paths are relative to the folder holding `recipes/` |
| `recipes/<L>.json` | each version's recipe (`streetzim-recipe/2`) |
| `recipes/refused.json` | versions not published, and why |
| `recipes/verdicts/<base>/<L>.json` | each published version's checks |
| `recipes/gates/<pack>.gate.json` | each pack's and search pack's gate |
| `recipes/menu.json` | the run: languages, region languages, published and refused versions, languages left out and why, packs used |

Zimfarm uploads ZIMs; the `recipes/` folder needs the recipe's
`artifacts_globs` (`["recipes/**/*"]`) or another place in the warehouse
([below](#what-zimfarm-needs-on-its-side)).

### Which versions

- **The languages**: `--languages`, default StreetZim's core set (the 21
  label languages and English: `ar de el en es fa fr he hi id it ja ko nl
  pl pt ru th tr uk vi zh`), plus the base's region languages
  (`regionLanguages` in its `map-config.json`, which
  `streetzim --multilingual --region-languages` records) and the base's
  own language.
- **The packs a version carries**: its language's, every region
  language's, and English, less the Wikipedia the base already has. A
  language is offered only when all of them were given and passed their
  gate; the others are listed in `menu.json` with the reason.
- **Region languages**: a map of any tier but country, local and
  us-region (StreetZim's `cloud/regions.tsv`; a region it does not list
  counts as capped) keeps its three most spoken with a pack, since every
  version carries them.

### What is checked

Per version, before its recipe is published:

| check | tool | |
|---|---|---|
| recipe = merge | `merge_zim.py`, `recipe2.materialize` | the file the recipe builds is byte for byte the merged file |
| verify | `verify_merge.py` | every base and pack entry in the merged file, its metadata, map-config and UUID, the search pack's indexes |
| zimcheck | zim-tools `zimcheck -A --threads=1` | on the merged file: exit 0, and its last `Overall Test Status:` line is the Pass line |
| search | `search_gate.py` | Kiwix's search (libzim's suggestions and full-text index) finds pack-only article titles, and the base's own pages as well as the base does |

StreetZim's own pipeline also checks each version in kiwix-serve and in
Chromium (the viewer's Wikipedia packs, the interface language, labels,
the smoke test) and routes on it. The image has neither kiwix-serve nor
Chromium, so this mode does not; each verdict lists those checks under
`not_run`.

## Names, temporary files and the disk

### Names

Every ZIM the offliner publishes ends its name with the first 12 hex
digits of its SHA-256, so a re-run with the same inputs that makes the
same bytes gives the same name, and new bytes get a new name. A name
already in `/output` is never given other bytes, `--overwrite` or not: a
recipe made earlier names its parts by file name and checks their chunk
hashes, and it keeps building from them. A file of the same name and the
same bytes is kept as it is. `--overwrite` replaces only `recipes/`.

### Temporary files, downloads and caches

| what | where |
|---|---|
| the work folder (staged packs, search packs being built, merged files, logs) | `--tmp`, else a private folder of its own (`mkdtemp`), removed at the end |
| downloads: the base, the packs, the Wikipedia ZIM | `--dl`, reused by later runs; else inside the work folder, removed at the end, so nothing is reused that was not checked in this run |
| caches that hold no ZIM: the SQL dumps, title maps, Wikidata text, the bases' scans | `$STREETZIM_CACHE_DIR` where set (the image sets `/output/cache`, as for `streetzim`), else beside the downloads |

On Zimfarm, then, the work folder and the downloads are in the
container's writable layer (`/tmp`), which Zimfarm counts toward the
task's disk with `/output`, and the caches in `/output/cache`. No ZIM
other than a published one is ever under `/output`: `--tmp` and `--dl`
inside `--output` are refused, a ZIM is copied there under a hidden name
that does not end in `.zim` and renamed when complete, and the caches
hold no ZIM. Zimfarm's uploader, which takes ZIMs by name, sees only
finished outputs.

Each step runs in a process group of its own. When a step runs out of
time or the task is stopped (SIGTERM), the step and every process under
it are stopped and killed, found through `/proc` (GNU `timeout`, which
the gates run zimcheck under, starts a process group of its own); when a
step ends, what is left in its group is killed.

### Disk

Before the search packs, the menu asks for the search packs (one per
version that carries packs, each estimated at 15% of the base and half
the version's packs) plus the largest version's merged file and its copy
from the recipe (base + packs + search pack, twice). Before each version's checks it asks again with the real
sizes, and before publishing it asks for the search packs and recipes on
`/output`'s disk. Short of any of these, the task stops with the sizes.

## Resources

Measured on Monaco (a 6 MB multilingual base, `regionLanguages` `[fr]`,
no Wikipedia of its own), from local Wikipedia ZIMs on a busy hard disk,
on StreetZim's build host:

| task | result | wall time | peak memory |
|---|---|---|---|
| pack fr (55.4 GB source) | 50 articles, 0.96 MB | 149 s | 216 MB |
| pack de (52.4 GB source) | 28 articles, 0.32 MB | 36 s | 230 MB |
| pack en (124 GB source) | 53 articles, 0.86 MB | 56 s | 560 MB |
| menu (the 3 packs, base and packs downloaded) | versions de, en, fr; 3 search packs, one per version (2.7, 2.6 and 2.6 MB); recipes 2.0 MB each | 139 s, 120 of them waiting on Wikidata's maxlag for the Titles | 146 MB |

Switzerland (2.28 GB base, 68,000 Q-IDs) on StreetZim's pipeline, the
same builder with `--images lead`, 2026-10-23: de 23,367 articles, 229 MB
in 9 min (peak 836 MB); it 15,175 articles, 135 MB in 12 min; fr 14,267
articles, 185 MB in 50 min (under disk contention); ja 17 MB in 6 min;
rm 7 MB in 48 s; all peaks under 1 GB. Its search packs are 210-225 MB
each and took 6-25 min each to build, one at a time; with one per version
that carries packs, Switzerland's 23 versions have 23.

### Per pack task

- **Download**: the whole Wikipedia ZIM, every task. Current maxi sizes
  (Kiwix's catalog, 2026-10-23), GB: en 127.4, fr 55.4, de 48.6, ru 37.1,
  es 36.5, it 31.8, ja 30.8, uk 28.2, zh 26.6, nl 23.5, pl 22.7, pt 20.6,
  ar 19.1, fa 13.8, vi 12.4, ko 11.4, id 10.4, tr 8.9, he 8.7, el 5.2,
  th 3.6, hi 1.9, rm 0.1. A region's 22 core languages download 585 GB.
  Plus the SQL dumps (en 3.1 GB; fr 0.62, de 0.45, es 0.39, ru, it and
  pt 0.3-0.4, the rest under 0.3) and the base.
- **Disk**: the Wikipedia ZIM + the dumps + their title map (en 0.22 GB,
  the others under 0.1) + the base + the pack twice (staged and
  published) + the image (about 1.7 GB). German Switzerland: about
  48.6 + 0.45 + 0.1 + 2.3 + 0.5 + 1.7 = 54 GB.
- **Memory**: under 1 GB measured (the English title map is the
  largest); give 2 GiB. StreetZim's pipeline caps the builder at 8 GB of
  address space.
- **Time**: the download (35 min for German at 25 MB/s, 85 min for
  English), the dumps, then minutes to an hour of build, which reads the
  Wikipedia ZIM at random (an SSD helps more than CPUs).

### Per menu task

- **Download**: the base and the packs (a few hundred MB for a country).
- **Disk**: the base, the packs, a search pack per version (each about a
  tenth of the base: Switzerland 0.21 GB), and while a version is checked its
  merged file and its copy from the recipe (two times base + packs +
  search pack), checked as [above](#disk).
- **Memory**: the search pack builds dominate (libzim's indexer); 4 GiB
  for a country-sized base.
- **Time**: search packs (minutes each for a country, one per version;
  `--jobs` builds several at once), then per version a merge, a
  recipe build, verify, zimcheck and the search check (Switzerland's 23
  versions: about 1.7 h of checks with the browser ones included).

## What Zimfarm needs on its side

- **The offliner**: `streetzim-langpack` in the same image as
  `streetzim`, command `streetzim-langpack`, definition
  `offliner-definition-langpack.json` (validated against Zimfarm's models
  in CI, as `streetzim`'s is). The image carries zim-tools' static
  `zimcheck`, which the gates require.
- **Recipes and other non-ZIM outputs**: the menu's `recipes/` folder
  through `artifacts_globs`, or a warehouse path for recipes, and the
  catalog generator reading them (the proposal's "Catalog").
- **The packs on the mirrors but off the catalog**: packs and search
  packs are ZIMs whose articles link back into the base; Zimfarm's own
  zimcheck run on a pack reports those links.
- **Order**: a region's menu task runs after its pack tasks, and the
  base, packs and search packs stay published while recipes name them.
- **Publisher**: packs and search packs carry Publisher `StreetZim`;
  there is no flag for it yet.
- **The Wikipedia download per pack task** is most of the cost. A pack
  task for one language and several bases would download it once for
  all of them; not written.
