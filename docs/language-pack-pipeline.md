# Language-pack pipeline: packs, gates, site files

How a region's base ZIM and its Wikipedia packs become merged ZIMs: one per
map language, offered on the builder page as recipes. The interfaces (base,
pack and merged-file layout) are in
[language-matrix-prototype.md](language-matrix-prototype.md). The code is in
`tools/langpack/` and the tests are in `tests/langpack/`.

The pipeline does not care how a base was built. It reads only the ZIM, its
`map-config.json` and its metadata, plus an optional lineage sidecar (see
below).

## Configuration

Everything is set by arguments and environment variables.

| variable | used by | meaning |
|---|---|---|
| `LANGPACK_WORK` | all | Working directory; default `./langpack-work`. It holds `recipes/<base>/` (gated recipes), `verdicts/<base>/`, `merged/` (the merged file being checked: one at a time), `gates/<base>/` (gate logs), `sha-cache/`, `locks/` and `tmp/`; ship_packs.sh adds `packs/<base>/`, `site/<region>/`, `regions/`, `wpcache/`, `ia-cache/` and `logs/`. **Production:** a disk with room for twice the base, and the same directory for every step of a round, since make_site reads the verdicts and gated recipes run_gates wrote. On StreetZim's build host that is `/storage/streetzim/langpack-work` (on `/storage`, never `/home`); `ops/langpack/host-env.sh` sets it. |
| `PACK_GATE_ZIMCHECK` | pack_gate, build_packs, run_gates, mutation_test | The **official** zimcheck (zim-tools, e.g. 3.8.0). The default is `zimcheck` on PATH. Any other zimcheck is refused: zimru's misreads hrefs that contain `&amp;` or `&#39;`, and it prints the same "Overall Test Status" line. build_packs.sh and run_gates.sh refuse to start, and pack_gate.py fails the pack. build_packs.sh also passes it to the builder as `ZIMCHECK_BIN`, so the builder's own check uses it too. |
| `PYTHON` | shell scripts | Default `python3`. |
| `WIKI_SRC` | build_packs.sh | The Kiwix Wikipedia ZIMs, passed as `--source`. Required. |
| `WP_CACHE`, `PACK_DATE` | build_packs.sh | The builder's `--cache` (default: its own); the date in pack file names (default: today). |
| `KIWIX_SERVE`, `NODE` | run_gates.sh | Defaults `kiwix-serve` and `node`. |
| `PLAYWRIGHT_CORE`, `CHROME_PATH`, `BROWSER_LIBS` | the browser gates | Path to playwright-core's `index.mjs` (default: the installed package); the Chromium to drive; extra `LD_LIBRARY_PATH` for that Chromium. |
| `LANGPACK_MIN_FREE_GB` | shell scripts | Pause while MemAvailable is below this; default 0. On the shared build host it is 40 (`ops/langpack/host-env.sh`). |
| `LANGPACK_DISK_MARGIN_GB`, `KEEP_MERGED`, `RESUME` | run_gates.sh | Free disk asked beyond twice the base (default 20); 1 keeps every merged file; 1 skips what already passed (see §2). |
| `LANGPACK_WEBSEED` | ship_packs.sh, site_check.py | The build worker's origin (`build-proxy/`, e.g. `https://streetzim-build-proxy.<account>.workers.dev`). Set: a webseeded torrent per language entry (see "Language torrents"), and site_check.py requires every torrent's webseed to use it. Empty (the default): none. |
| `LANGPACK_WEBSEED_SUBREQ_LIMIT`, `LANGPACK_WEBSEED_BLOB_BUDGET` | lang_torrents.py, site_check.py | The most worker subrequests a 16 MiB request of a language version may need (default 40; the free plan allows 50); the most blob bytes per entry (default 64 MiB). |
| `SHIP_TORRENT_TIMEOUT`, `SHIP_LANG_TORRENTS_ARGS` | ship_packs.sh | lang_torrents.py's time limit (default 7200 s; on expiry: a WARNING, published without torrents); extra arguments for it. |
| `LANGPACK_RESHIP_AFTER_PATCH` | upload_validated.sh, rollout_viewer_patch.sh | 1 lifts the freeze on re-uploading a base whose recipes are published; the region is queued in langpack-reship.tsv for ops/langpack/reship_queue.sh. |

On StreetZim's build host, source `ops/langpack/host-env.sh` first. It sets these variables for that host; values already set win.

## 1. Build packs

```bash
WIKI_SRC=/path/to/wiki-src tools/langpack/build_packs.sh california BASE.zim PACKS_DIR "de th zh es ja ko fr"
```

Each pack is built into `PACKS_DIR/.staging/` by `tools/build_wiki_pack.py`.
It reaches `PACKS_DIR` only when both of these pass:
- **`builder_ok.py`.** The builder's exit code counts. A non-zero exit is excused only when every problem it reports is a zimru `url_internal` finding. The stats file must also be this run's: it is deleted before the build, and it must be newer than the run marker, no older than the staged pack, and its `zim_bytes` must equal the pack's size.
- **`pack_gate.py PACK --base BASE`.** It writes `PACK.gate.json`.

A second run for the same region and language is refused (exit 2) while
one is building, whatever the output directory (`flock` on
`$LANGPACK_WORK/locks/`).

**Pack gate rule.** A pack is usable only when `PACK.gate.json` says
`"ok": true` and its `pack_md5` equals the pack's current MD5. The gate
requires all of the following:
- **zimcheck.** The official zimcheck `-A` finished, so its "Overall Test Status" line is present. Its only findings are dangling back-to-map links, exactly one per pack article, each resolving to `index.html`. Every block must parse, titles with quotes included, and any other `[ERROR]` or `[WARNING]` line fails the gate.
- **Integrity.** libzim's `Archive.check()` passes.
- **Layout.** There is one `~w/<xx>/` prefix, and `pack.json` has a matching `lang`, a `base_uuid` and `articles`.
- **Metadata.** Title is at most 30 UTF-16 units, Description at most 80 characters, Language is ISO 639-3, and Name is `osm_<region>_wiki-<xx>`.
- **Lineage.** With `--base`, `pack.json`'s `base_uuid` is in the base's lineage.

## 2. Gate the menu

```bash
tools/langpack/run_gates.sh BASE.zim PACKS_DIR de,th,zh,es,ja,ko,fr,en en california \
    tools/langpack/regions/california.json 8991
```

`run_gates.sh` takes the menu for every map language L from `recipe2.py menu`,
which uses only packs with a passing gate. It writes
`$LANGPACK_WORK/recipes/<base>/<L>.json` and runs these checks:

| check | what it does |
|---|---|
| recipe = merge | `merge_zim.py` output equals the materialized recipe, byte for byte |
| verify | `verify_merge.py`: 22 entry-level checks, including the UUID and lineage rules below |
| zimcheck | zimcheck `-A` on the merged file |
| route | `cloud/route_cli.py` |
| kiwix | kiwix-serve suggest/search, one article and `pack.json` per pack, and the catalog Title and Language |
| viewer | `viewer_gate.mjs`: `WIKI_PACKS`, source order, the article offered |
| ui | `ui_gate.mjs`: the UI in the map language (a language with no UI table FAILS; read through the viewer's runtime, any table format; RTL needs `dir="rtl"`), labels, popup → pack article → images → back to the map, the map's search |
| search | `search_gate.py`: Kiwix's search finds 8 pack-only article titles by suggestion (first 10) and by full text (first 25), and base parity: of 12 base front pages and 12 base front redirects, each the base's own suggestions find, the merged file's find too; the kiwix-serve check asks `/suggest` and `/search` for three pack titles too. Required (v1 does not launch without it) |
| smoke | `cloud/zim_viewer_smoke.mjs` |

The viewer, UI and smoke gates also run on the base alone, first. Then one
language at a time: its merged file is made, checked with kiwix-serve
serving it alone, and deleted (`KEEP_MERGED=1` keeps it), so the disk holds
one merged file and, during the recipe = merge check, its copy. The run
refuses to start, and stops before the next language, while the merged
folder's disk has less than twice the base plus `LANGPACK_DISK_MARGIN_GB`
free. `RESUME=1` keeps the last run's logs and skips an entry whose verdict
already passes for the recipe this run makes, and the base's checks when
they passed for a base of the same size and mtime. Every language's logs
are stamped with its recipe's md5 and uuid (`<L>.stamp`); a verdict needs
this run's stamp (`logs_for_recipe`) and base logs for this base
(`base_logs_current`), and after a disk break the languages not reached
lose their logs, so a resumed run can never pass old logs for a new
recipe. A zimcheck that ends
without its verdict line (killed, not a finding) runs once more with
`--threads=1`; a Fail is never retried.

The region file (`REGION_JSON`) names the city the UI gate clicks, the
searches and the route. `tools/langpack/regions/<region>.json` holds the
hand-written ones; `region_json.py BASE` writes one for any base (a city or
town near map-config's center whose article the base offers, inside its
outline). The UI gate looks for the city's label at the file's zoom, then
at 7, 8, 6, 9, 10 and 5, and clicks it at the zoom that showed it.

`compose_verdicts.py` then writes `$LANGPACK_WORK/verdicts/<base>/<L>.json`.
Each verdict records the zimcheck version (`zimcheck_version`). The zimcheck
check passes only when the log says "Overall Test Status: Pass" and that
version is `zim-tools …`.

**Verdict rule.** An entry may be published only with a verdict that has
`all_pass` and that names this exact recipe: its `recipe_md5`, `recipe_uuid`
and `recipe_size`. The verdict also records `recipe_file_md5` and the MD5 of
every pack used.

**Two MD5s.** A recipe's `md5` is the ZIM's internal checksum, the last 16
bytes of the file. Its `file_md5` is the MD5 of the whole file, which is what
`md5sum` prints. Reports should say which one they mean.

## 3. Site files

```bash
tools/langpack/make_site.py BASE.zim --packs-dir PACKS_DIR --region california \
    --region-title California --region-langs en --langs de,th,... --titles '{"de": "Kalifornien"}' \
    --language-meta map --url-template 'https://PROXY/ITEM/{file}' \
    --url-override 'fr=https://PROXY/ITEM-fr/{file}' --base-download URL --base-url URL -o SITE/recipes
```

This writes the builder page's files:
- `index.json` and `region.json`;
- `<L>.json`: a `streetzim-recipe/2` recipe per language;
- `refused.json`.

An entry is refused when it has no all-PASS verdict for its recipe, or when
it lacks a pack because that pack has no passing gate.
`ops/langpack/california_site.sh` is StreetZim's own California setup, with
its archive.org items and range proxy.

`--language-meta map` makes M/Language the map language alone, because
kiwix-serve refuses `/search` on a book that lists several languages.

## 4. Check before deploying

```bash
tools/langpack/site_check.py SITE --base BASE.zim --packs-dir PACKS_DIR \
    --ia-cache DIR --ia-fetch --probe-urls
```

`site_check.py` exits 0 only when all of these hold:
- **Site against gates.** Every site recipe equals its gated recipe, ignoring pack URLs, and its verdict passes. The index fields match the recipes.
- **Inputs.** The base and pack sizes, UUIDs and 16 MiB chunk SHA-256s match the local files. Segments add up and inline segments match their hashes.
- **Published files.** Every URL's item lists the file with the local size and MD5. This uses archive.org's metadata API: `--ia-fetch` downloads it into `--ia-cache`; without the flag, the cache is used.
- **Served bytes.** With `--probe-urls`, the first and last 64 KiB of each URL match the local bytes.

Deploying the site is an operations step and is not covered here.

## Lineage and UUIDs

**Merged UUID.** A merged file's UUID is
`uuid5(base UUID, sorted pack UUIDs, L, merger version, SHA-256 of the generated map-config + metadata)`.
The same inputs always give the same file, and anything generated differently
gets a different UUID, which Kiwix needs because it dedupes books by UUID.

**Lineage.** A base's lineage is its own UUID plus, when
`<base>.parent.json` checks out, the parents that sidecar records. The
sidecar checks out only when both of these hold:
- its `child_uuid` equals the base's header UUID;
- its `child_md5` equals the file's MD5.

So a sidecar copied beside another file, or left over from an edited file,
adds nothing. The merger, `verify_merge.py` and `pack_gate.py` all apply this
rule. A pack fits a base when its `pack.json` `base_uuid` is in the base's
lineage.

**Viewer swaps.** `swap_base_viewer.py SRC DST VIEWER_EXPORT REV` (pass the
repository root to use this checkout's viewer) gives the base a new viewer
and a new UUID. It refuses a DST that is SRC (same path, symlink or hard
link) before touching anything. DST starts as a reflink of SRC. Where the filesystem cannot
reflink, the swap stops unless `--allow-full-copy` is given; then it warns
and copies every byte. It writes DST's sidecar only when every check passes, and
otherwise deletes DST. The sidecar records SRC's UUID as the parent. It
carries SRC's own parents forward only from a sidecar that checks out, so a
pack built against the unswapped base still fits.

## Tests

`pytest tests/langpack` writes small ZIMs with python-libzim: a base and
de/fr/th packs. They cover:
- merge, verify, and recipe = merge;
- recipe and input corruption, the 18 merger mutants (`mutation_test.py`), and lineage;
- the pack gate, with a stand-in for the official zimcheck, plus the gate's own source mutants;
- `builder_ok`, `build_packs.sh` (fake builder, stale stats, locking), verdicts, site files, and the site-check mutants.

Tests that need more skip themselves and say why:
- the official zimcheck (`PACK_GATE_ZIMCHECK`);
- a real base for viewer swaps (`LANGPACK_TEST_SWAP_BASE`);
- the full browser gate run (`LANGPACK_TEST_GATES`).

`tests/langpack/test_real_data.py` lists those variables.

## Kiwix search: search packs

Kiwix searches a ZIM through libzim: title suggestions from
`X/title/xapian` (without one, a case-sensitive prefix walk of the title
listing) and full text from `X/fulltext/xapian`. Every Kiwix app (desktop,
Android, iOS, kiwix-serve, the PWA) goes through these two. A merged file
that kept the base's indexes found none of its pack articles.

A **search pack** (`tools/langpack/index_pack.py`) is a small ZIM written by
libzim itself: one empty item per page of the merged file, at the merged
path, with the page's title and its text as index data, and every front
redirect of the base and the packs at its own path (a place's other names:
Greece's base has 2,566, "Δήμος Παλλήνης" → the Pallini page). The index
data is folded exactly as libzim folds every query and its own HTML
indexer's text (`zim::removeAccents`, ICU "Lower; NFD; [:M:] remove;
NFC": lowercase, every Unicode mark out, the Thai and Devanagari vowel
signs too); libzim indexes custom index data as given. Pages stream from
the base and the packs into libzim's writer (nothing holds every page's
text); the base's are cached per base and text version
(`<cache>/index-text/<uuid>.v2.jsonl.gz`; an older version's cache is not
read). `merge_zim.py --index` puts its two indexes in place of the
base's; its clusters are copied verbatim like a pack's, and the recipe
names it as one more source (`packs.search`, `"kind": "search"`), so the
browser streams it and builds nothing. Kiwix then finds the base's pages
and the pack articles alike, with libzim's own matching (case, accents,
words inside titles), by suggestion and by full text. The base's own index
bytes stay in the file, unused.

`tools/langpack/search_packs.py` builds one per distinct pack set of the
menu (entries with the same packs share one: Greece's el and en entries),
named `osm_<region>_search-<langs>_<date>_<base uuid8>.zim`, with a sidecar
(the base UUID, the packs' files and MD5s) and a gate (both indexes; a page
of the base, a front redirect of the base and a page of each pack found by
suggestion, the packs' by full text too). `recipe2.find_search_pack`
takes one only for exactly the entry's packs and base and with a passing
gate. The index's stemmer is the visitor's language, else the base's.

Greece (1.05 GB base, 64,457 pages): 57–60 MB per search pack (the base's
own indexes are 28 MB; a pack's full text adds most of the rest), 67–76 s
each once the base's text is cached (21 s to read it the first time).

## Viewer patches and recipes

`cloud/patch_viewer_inplace.py` rewrites a base's three viewer slots (and
its checksum) and keeps its name, size and UUID. A recipe carries those
slots inline (from the base it was made from), lists their ranges and the
base's checksum as `base.mask`, hashes the base's chunks with them zeroed
and never reads them from the base. So:

- a visitor's copy one viewer patch older (or newer) still builds the new
  file: the page's check of the copy and the worker's chunk checks zero the
  mask too;
- the merged UUID follows the inputs' MD5s (`derive_uuid`), so a new viewer
  is a new book for Kiwix;
- a patched base keeps its recipes' `fingerprint` (everything but the slots
  and the UUID/checksum they change: the base's masked chunk hashes, the
  packs' chunk hashes, the metadata, map-config, the merger version and a
  hash of every byte the merger generates, `generated_sha256`), and
  `run_gates.sh` with `RESUME=1` gives such an entry a **light gate** when
  the last passing verdict has that fingerprint and checked another base
  MD5 (a base really patched): recipe = merge, verify, zimcheck (its link
  checks read the viewer's HTML; 28 s on Greece) and the browser checks
  again, route, kiwix-serve and Kiwix search carried from that verdict
  (`evidence.light_gate`; compose_verdicts.py checks both conditions again).
  A merger change, a pack or a base byte outside the slots changed: the
  full gate. `ship_packs.sh` on a
  patched base therefore rebuilds no pack, sends nothing, and only
  regenerates, light-gates and re-seals the recipes; until it has, the
  live-MD5 rule keeps the region off `/build/`.

## The browser holds no pack whole

Before Build the page downloads and checks the first chunk the build needs
from each pack (a missing or replaced pack stops the visitor before anything
is saved) and keeps it, one chunk per pack, until Build moves it to the
worker; the worker streams the rest of every pack, the search pack included,
in verified 16 MiB chunks through the range proxy during the build, like the
base, with the same sliding window and buffer pool. No chunk is downloaded
twice, so a visitor downloads exactly the entry's `download_bytes` of packs. `make_site.py` refuses, and `site_check.py`
fails, an entry over the per-entry budget: more than `LANGPACK_ENTRY_BUDGET`
(2 GiB) of packs to download, or a recipe over `LANGPACK_RECIPE_BUDGET`
(32 MiB).

**Continent maps get no language entries in v1.** A search pack indexes
every page of the base, so it grows with the map: Greece 56 MB, Italy
~175 MB, Iberia ~238 MB, Europe ~2.76 GB of indexes alone. A continent's
entries are over the 2 GiB budget, so `ship_packs.sh` refuses a continent
(and any base over `SHIP_MAX_BASE_GB`, 8) in its first step, before any
download or build (exit 4); maps up to about 5 GB come first, continents
after a measured design. Indexing the base's pages by title only is no way out: on
Greece it saves 14% (the base's share of a search pack 36.8 → 28.7 MB;
most of it is the title index of its 64,457 pages, which the base's own
suggestions need).

## Language torrents

`tools/langpack/lang_torrents.py OUT_DIR --base BASE --packs-dir DIR --webseed URL
[--base-torrent T] [--ia-cache DIR]` gives every entry of a region's site
files a torrent of its language version: a single-file torrent of exactly
the file the page builds (so a visitor who built it can seed it; same
infohash), with the piece length `ops/cloud/build_torrent.py` picks for that
size, the trackers in `LANG_TRACKERS` (archive.org's two, which count peers
of any infohash, and open UDP trackers that answered; DHT works too) and one
BEP 19 webseed, `URL/merged/<region>/<L>/<recipe uuid>/<file>.zim`. Its
creation date is the newest archive.org mtime among its sources (left out
when `--ia-cache` has none), so a rerun on the same published files writes
the same torrent bytes. That URL
is the build worker (`build-proxy/`, Cloudflare Workers), which serves the
language version virtually: no merged file is stored anywhere, and nothing
is served from the build host.

Beside each recipe it writes `<L>.torrent`, `<L>.ws.json` (the worker's
segment index, streetzim-webseed/1: the merged file as parts, each a byte
range of one archive.org file, with its archive.org ETag when `--ia-cache`
has the file's mtime, or of a blob; a few KB) and `ws-<h>.bin` blobs: the
recipe's inline bytes, the MD5 and base or pack segments shorter than the
remote threshold, coalesced into blobs of at most 4 MiB, named by content
(the viewer slots are stored once per region). It adds `torrent` and
`infohash` to the entry in `region.json`/`index.json`; the page shows a
"Torrent" link.

Subrequests: the worker spends 1 on the index, 1 per blob and 1 per
archive.org part (2 for a file's first: archive.org's redirect), and the
free plan allows 50 per request. Every 16 MiB window (libtorrent's largest
web seed request) must need at most `LANGPACK_WEBSEED_SUBREQ_LIMIT` (40);
the threshold starts at 256 KiB and rises (1, 4, 16, 64 MiB) for an entry
that would need more; local runs under 4 KiB share one pool blob per entry;
an entry that cannot fit, or whose blobs would pass
`LANGPACK_WEBSEED_BLOB_BUDGET` (64 MiB), gets no torrent. The index records
the threshold and the counts (worst 16 MiB window, whole file). The worker
cuts a bigger range to what fits and answers a whole-file GET that does not
fit with 416.

Every byte is read through the recipe's chunk check, and the merged
stream's MD5 must equal the recipe's `file_md5`, always. With
`--base-torrent` (the base's own torrent, `web/torrents/<region>.torrent`),
pieces lying wholly inside a base range at its own offset take the base
torrent's hashes, but only after every piece of the base torrent has been
checked against the local base (once per run, for all entries); a base
torrent that differs anywhere (one made before a viewer patch, say) is not
used. On Greece: 997 of 1,104 pieces reused, identical infohashes. Exit 0,
3 (some entries failed: they have no torrent, the rest do), 1 (run failed;
`--strip` removes every torrent file and field).

`ship_packs.sh` runs it (under timeout and prlimit) after `make_site.py`
when `LANGPACK_WEBSEED` is set, first fetching the base's and the packs'
archive.org metadata for their ETags. `site_check.py` rebuilds each index
from its recipe and compares (ETags from the metadata cache included),
checks the subrequest bound, the blobs' SHA-256, the webseed origin against
`LANGPACK_WEBSEED`, the torrent's name/length/piece length/webseed/infohash
and every piece and the MD5, and fails on torrent files no entry names;
torrent failures start "FAIL TORRENT", and when they are the only ones,
ship_packs.sh strips the torrent files and publishes the recipes without
them. Otherwise they are sealed and published with the recipes. When a map
or its packs change, the recipe's UUID changes, so the old torrent's webseed
URL answers 404 instead of other bytes and the new torrent replaces it; a
base re-uploaded under the same name (a viewer re-patch) changes its
archive.org ETag, so the worker answers "out of date" until ship_packs.sh
has run again for the region — which is why such re-uploads are frozen for
bases with published recipes: upload_validated.sh refuses them (exit 7,
only on a positive match and only when bytes would be sent) and
rollout_viewer_patch.sh skips them before patching, unless
`LANGPACK_RESHIP_AFTER_PATCH=1`, which queues the region in
`langpack-reship.tsv` for `ops/langpack/reship_queue.sh` (run it after the
rollout; ship_packs.sh takes hours per region). A region's blobs, all
entries together, are capped too (`LANGPACK_WEBSEED_REGION_BLOB_BUDGET`,
256 MiB). A
lang_torrents.py failure or timeout (`SHIP_TORRENT_TIMEOUT`, default 2 h)
is logged as a WARNING and the recipes are published without torrents.

Measured on Greece's 22 entries (tools/langpack/webseed_e2e.py: archive.org
and site stand-ins and the worker on 127.0.0.1): every piece of every
torrent fetched as a webseed client does matched its SHA-1, every stream's
MD5 its recipe's, and libtorrent (webseed only, no peers, uploads off)
downloaded all 22 byte-exact; 25,520 webseed requests, at most 5 parts and 5
subrequests each. After deploying, `tools/langpack/webseed_smoke.py` checks
the published torrents' pieces through the live worker. Deploying the worker,
its request budget and switches: `build-proxy/README.md`.

## In production

Every published map gets packs for the **core languages**, the tiles'
languages plus English: ar de el en es fa fr he hi id it ja ko nl pl pt ru
th tr uk vi zh (`CORE_LANGS` in `tools/langpack/region_meta.py`), and for
the map's own `regionLanguages` (hy, sv, ...) that have a Kiwix Wikipedia
ZIM in `WIKI_SRC`. A language without one is skipped and logged; a region
language without one is left out of the region's languages, since every
menu entry would need its pack. A continent or multi-country map (its tier
in `cloud/regions.tsv`; any tier but country, local and us-region, and a
region the registry does not list; a variant it does not list takes its
parent's tier: `switzerland-de` is switzerland's) keeps its `REGION_PACKS_MAX` (3) most spoken
languages with a Wikipedia ZIM, the base's own not counted
(`cloud/region-languages.tsv` lists them by speakers: Europe 38, Africa
31), so its entries stay within a browser's memory; the others stay on the
menu when they are core languages. A country map keeps all of them:
Switzerland carries de, fr, it and rm in every entry; Greece el.

### Host setup (once)

`ops/langpack/install.sh` (dry run; `--apply` does it) and
[ops/langpack/INSTALL.md](../ops/langpack/INSTALL.md).

`ops/langpack/host-env.sh` sets the paths; `langpack_env_check` says what is
missing. It needs zim-tools 3.8.0 in
`/storage/streetzim/tools/zim-tools_linux-x86_64-3.8.0/` (the zimcheck on
PATH there is zimru's), playwright-core in `ops/langpack/node_modules`
(`cd ops/langpack && npm ci`; never npm at the repository root),
puppeteer in the repository's own `node_modules` (the smoke), Chromium 1217
in `~/.cache/ms-playwright` with `.browser-libs`, and the Kiwix Wikipedia
ZIMs in `/storage/streetzim/wiki-src`.

### After a map ships

```bash
. ops/langpack/host-env.sh
ops/langpack/ship_packs.sh greece /storage/streetzim/osm-greece-2026-10-15.zim
ops/langpack/publish_site.sh greece          # once ship_packs.sh exited 0
python3 web/generate.py --deploy             # the usual site deploy
```

`ship_packs.sh REGION BASE` (the base exactly as `streetzim-<REGION>` lists
it on archive.org) logs each step in PDT to
`$LANGPACK_WORK/logs/ship-<REGION>-<base>.log`:

1. `region_meta.py`: the menu (core + region languages, the base's own), the
   packs it needs, and each merged file's Title (the region's name in that
   language from the Wikidata cache, else the base's Title).
2. `prefetch.py`: the SQL dumps, title maps and Wikidata text those packs
   read (network, no timeout of its own; the builds then run `--offline`).
3. `build_packs.sh` for each pack language without a gated pack for this
   base in `$LANGPACK_WORK/packs/<base>/`. Packs are named
   `osm_<region>_wiki-<L>_<date>_<first 8 hex of the base's UUID>.zim`, so two
   bases of a region never share a pack name. Then `search_packs.py`: the
   search packs the menu needs (above).
4. `run_gates.sh` with `RESUME=1`, streamed. The region file is
   `tools/langpack/regions/<REGION>.json`, else `region_json.py`'s.
5. archive.org: the packs the passing entries use go to
   `streetzim-<REGION>-lang` (made on the first upload: mediatype data,
   collection opensource_media like the bases, licence CC BY-SA 4.0 for
   Wikipedia's text, title, one language per pack), under the host's upload lock.
   A pack listed with its MD5 is not sent again, and neither is one sent
   before and not listed yet (`$LANGPACK_WORK/uploads/<item>.tsv`; after 72
   hours unlisted the run stops and says so); a name the item holds with
   another MD5 stops the run. Until archive.org lists every pack with its
   MD5: exit 6, run it again later; nothing is redone.
6. `make_site.py` into a staging folder, URLs `{proxy}/<item>/<file>`, the
   base's MD5 in region.json; `site_check.py --recipes-dir` there
   (archive.org listings, the served bytes through the proxy: a 206 answer
   for every range). Only when it passes is the folder sealed
   (`checked.py write`: checked.json with every file's SHA-256) and swapped
   in as `$LANGPACK_WORK/site/<REGION>/recipes/<REGION>/`; a failed check
   leaves the previous files.

Exits: 0 ready to publish; 6 listing pending; 2 another run holds the
region's lock or a pack build lock; 3 `SHIP_SKIP_UPLOAD=1` stopped after the
gates (nothing published); 4 a map v1 gives no packs: a continent
(`cloud/regions.tsv`) or a base over `SHIP_MAX_BASE_GB` (8), refused in step
1 before any download, pack build or gate; 5 nothing publishable: every
entry with a Wikipedia pack that passed its gates is over the per-entry
budget (each refusal logged as `REFUSED <lang>: ...` before anything is
sent; the previous site files stay); 1 anything else. Light and ultralight
maps are refused (no Wikipedia in them).

`publish_site.sh REGION` refuses a folder that is not exactly the sealed
one, puts it at `web/build/recipes/<REGION>/` with one atomic exchange and
rewrites `web/build/recipes/index.json` (`site_index.py`), putting the
previous folder back if that fails. The recipes are site data, not source
(gitignored). Every site build (`web/generate.py`) rewrites the page's
index with only the regions whose base is archive.org's live ZIM (name and
MD5), and gives exactly those cards their "Build in another language"
link (`/build/?region=<id>`): a pruned or re-uploaded base leaves the page
with the next deploy.

### During a rebuild round: the pack queue

`ops/langpack/pack_queue.sh` follows a round: when a map's latest row in
`rebuild-old.tsv` (written after the queue first started) says `uploaded`,
or `upload-pending` and archive.org now lists the file with its MD5
(`ops/cloud/archive_has_file.py`; nothing appends `uploaded` to such a row
later), it runs `ship_packs.sh` for it with production's items
(`streetzim-<region>`, `streetzim-<region>-lang`), `LANGPACK_WORK=/storage/streetzim/langpack-work`
and the build worker as `LANGPACK_WEBSEED`, then publishes the region into a
**preview** clone of production (`/storage/experiments/build-preview/streetzim`,
moved to production's commit first) and redeploys the preview channel
(`firebase hosting:channel:deploy swiss-languages --expires 30d --project
streetzim`). Never the live site: production's `web/build/recipes` stays
empty until the page goes live. A new preview clone starts with the Swiss
test recipes the channel shows now (`QUEUE_SEED`, from
`/storage/experiments/lang-torrents/swiss-site2`); production's Switzerland
replaces them when it ships. The preview's home page is not regenerated.

- Eligible: not a light map, at most 8 GB, multilingual tiles
  (`tileLanguages`); `ship_packs.sh` refuses continents itself (exit 4).
  The log says so for each: France, Germany, the Nordics and Italy (8.1 GB)
  get no packs in v1.
- In the order the maps shipped, one run at a time while a round driver
  builds (the disk is saturated and its scheduler ignores ionice), up to 2
  (`QUEUE_JOBS`) once none does: a second one starts only with 24 GB of
  memory available, no memory pressure and room on the disk for its merged
  files. `ship_packs.sh` takes turns on the shared Wikipedia cache (its
  prefetch holds a lock).
- Exit 6 (archive.org listing pending) or 2 (a lock held): tried again after
  30 min; 4 and 5: logged, skipped for that base; anything else: a loud
  failure (`queue.log`, stderr), not retried until the queue restarts.
- Resumable: a map published for the same base UUID is not shipped again; a
  new file is. State in `$LANGPACK_WORK/pack-queue/` (`status.tsv`,
  `published.tsv`, `queue.log` in PDT); a file named `stop` there ends it
  after the current pass. The round's files are only read.

```bash
cd /storage/streetzim                # QUEUE_SINCE: count maps the round shipped before this start
QUEUE_SINCE='2026-10-23 00:00' setsid nohup ops/langpack/pack_queue.sh > langpack-work/pack-queue.nohup 2>&1 < /dev/null &
```

### The builder page

`/build/` (`web/build/`; the prototype's `/lab/build/` redirects there)
lists every region in `recipes/index.json`. The language picker shows each
language's own name and its name in the visitor's language ("Ελληνικά
(Griechisch)" in a German browser). Recipes name files on the range proxy
as `{proxy}/<archive.org item>/<file>`; the page puts in the proxy the
`/drive/` viewer uses, set in one place: `web/drive/preview-config.js`.
`site_check.py --proxy` (default: that file) probes through the same one.

`tools/langpack/builder_e2e.mjs` runs the page in headless Chromium against
local files (a stand-in for the site and one for the proxy) and checks that
each saved file is byte-identical to the gated merge (the recipe's
`file_md5`).

### Sizes

A recipe is the generated bytes (the packs' dirents and pointers, the merged
v1 listing's pack rows) plus the base's chunk hashes. They grow with the
packs' entries, not the base's.

Greece end to end on the build host (2026-10-21, production base
osm-greece-2026-10-15.zim, 1.05 GB, all 22 core languages):

| step | time | result |
|---|---|---|
| prefetch.py, 21 pack languages (13 new to the cache: 3.2 GB of dumps) | 15 min | complete |
| build_packs.sh, 21 packs | 19 min | all gated; 0.4 MB (hi, 34 articles) to 55 MB (el, 12,769) |
| run_gates.sh, 22 entries + the base, streamed | 1 h 23 min | 22/22 every gate PASS; merged folder peak 3.2 GB |
| make_site.py + site_check.py | 2 min | 722/722 checks (archive.org listings synthetic: nothing uploaded) |
| builder_e2e.mjs: el, ar, ja one-click, de from a local copy | 17-23 s each (local servers) | each file byte-identical to its gated merge |

Recipes: 2.7-3.8 MB each, 63 MB for the region's 22.
