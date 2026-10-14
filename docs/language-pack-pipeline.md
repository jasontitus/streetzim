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
| `LANGPACK_WORK` | all | Working directory; default `./langpack-work`. It holds `recipes/<base>/` (gated recipes), `verdicts/<base>/`, `merged/` (one full-size ZIM per gated entry), `gates/<base>/` (gate logs), `sha-cache/`, `locks/` and `tmp/`. **Production:** a disk with room for the merged files, and the same directory for every step of a round, since make_site reads the verdicts and gated recipes run_gates wrote. On StreetZim's build host that is `/storage/streetzim/langpack-work` (on `/storage`, never `/home`); `ops/langpack/host-env.sh` sets it. |
| `PACK_GATE_ZIMCHECK` | pack_gate, build_packs, run_gates, mutation_test | The **official** zimcheck (zim-tools, e.g. 3.8.0). The default is `zimcheck` on PATH. Any other zimcheck is refused: zimru's misreads hrefs that contain `&amp;` or `&#39;`, and it prints the same "Overall Test Status" line. build_packs.sh and run_gates.sh refuse to start, and pack_gate.py fails the pack. build_packs.sh also passes it to the builder as `ZIMCHECK_BIN`, so the builder's own check uses it too. |
| `PYTHON` | shell scripts | Default `python3`. |
| `WIKI_SRC` | build_packs.sh | The Kiwix Wikipedia ZIMs, passed as `--source`. Required. |
| `WP_CACHE`, `PACK_DATE` | build_packs.sh | The builder's `--cache` (default: its own); the date in pack file names (default: today). |
| `KIWIX_SERVE`, `NODE` | run_gates.sh | Defaults `kiwix-serve` and `node`. |
| `PLAYWRIGHT_CORE`, `CHROME_PATH`, `BROWSER_LIBS` | the browser gates | Path to playwright-core's `index.mjs` (default: the installed package); the Chromium to drive; extra `LD_LIBRARY_PATH` for that Chromium. |
| `LANGPACK_MIN_FREE_GB` | shell scripts | Pause while MemAvailable is below this; default 0. On the shared build host it is 40 (`ops/langpack/host-env.sh`). |

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
| ui | `ui_gate.mjs`: html lang, labels, popup → pack article → images → back to the map, the map's search |
| smoke | `cloud/zim_viewer_smoke.mjs` |

The viewer, UI and smoke gates also run on the base alone. kiwix-serve
serves at most 3 merged files at a time.

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
