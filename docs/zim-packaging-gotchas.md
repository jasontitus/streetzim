# ZIM packaging & upload gotchas

Hard-won lessons that aren't obvious from the code or the libzim
docs. Each one cost real user time before being run down. Worth
re-reading before changing anything in `cloud/repackage_zim.py`,
`cloud/upload_validated.sh`, or the `places.html` chunk-loading path.

---

## 1. `routing-data/graph-cells-index.bin` ≥ 200 MB **must** be raw

**Symptom.** Open the ZIM in Kiwix Desktop (Mac/iOS), click Directions
to here. The dialog hangs on "Loading routing data…" and eventually
errors "could not load routing data". The PWA on `streetzim.web.app`
behaves the same way.

**Cause.** The cells-index is parsed in one shot — the viewer fetches
the whole file into a single ArrayBuffer before any cell can be
looked up. When the cluster is zstd-22 compressed, decompression on
the WebView thread takes longer than the watchdog will tolerate.
Storing the file raw lets Kiwix's HTTP server hand the bytes through
unmodified.

**Where the rule lives.**
- Fresh build (`_emit_spatial_graph`):
  ```python
  compress_idx = idx_mb < 200
  compress_cell = len(data) < 200 * 1024 * 1024
  ```
- Repack passthrough (`cloud/repackage_zim.py`): same threshold
  applied to `graph-cells-index.bin` and any individual
  `graph-cell-*.bin`. Added 2026-04-28 (commit `23e0cfc`) after a
  Midwest repack regressed: source ZIM stored the index raw, repack
  default re-compressed it, and Directions hung.

**Field check.** Compare the cells-index sha between source and
repack. If bytes match but the repack ZIM is smaller overall, the
index probably ended up in a compressed cluster:

```sh
./venv312/bin/python3 - <<'PY'
from libzim.reader import Archive
import hashlib
for p in ("osm-x.zim", "osm-x-fixed.zim"):
    a = Archive(p)
    b = bytes(a.get_entry_by_path("routing-data/graph-cells-index.bin").get_item().content)
    print(p, len(b), hashlib.sha256(b).hexdigest()[:12])
PY
```

**Region sizes seen so far** (cells-index, 2026-04-28 builds):
Hispaniola 5 MB, Colorado 17 MB, Baltics 32 MB, California 67 MB,
Japan 144 MB (borderline), **Midwest 212 MB (over the line)**.
Anything over ~150 MB compressed in a cluster will likely fail on
iOS / Mac Kiwix.

**Validator gap.** `cloud/validate_zim.py`'s `routing_kiwix_compat`
check only looks at *layout* (monolithic vs spatial vs chunked), not
storage compression. A future check should fail any ZIM whose
cells-index lives in a compressed cluster.

---

## 2. `places.html` must follow three search-data manifest layouts

`search-data/manifest.json` lists each prefix in one of three shapes.
The typeahead and name search must handle all of them — early
versions only handled the first and silently returned no results for
hot-split prefixes.

1. **Unsplit.** `chunks['de']` exists; `de.json` is the single file.
2. **Hot-split.** No `chunks['de']`; `sub_chunks['de'] = ['de-0',
   'de-1', …, 'de-f']`. Each child appears in `chunks`. Triggered by
   `--split-hot-search-chunks-mb`.
3. **Recursively-split.** Hot-split children themselves split, leaves
   are 4-character names (`de-0-0-0`, …). Manifest *should* chain
   `sub_chunks['de'] = ['de-0', …]` then `sub_chunks['de-0'] =
   ['de-0-0']` etc. **Current builds sometimes ship
   `sub_chunks['de'] = []`** — a build-side bug worth fixing in the
   prefix splitter. Until then, the client falls back to a chunks-map
   scan for `<prefix>-*` keys.

**Helper.** `expandPrefix(prefix)` in `resources/viewer/places.html`
resolves a prefix to its leaf chunk filenames covering all three
shapes. `loadChunk(prefix)` parallel-fetches the leaves and
concatenates.

**Spot-check.** The smoke harness's `near typeahead` step prints
`near-candidates[0..3]`. If it's `["No matches."]` for a city that
obviously exists in the region, the prefix lookup is the suspect.

---

## 3. `ia metadata` is eventually consistent — wait before pruning

`ia upload` returns success once the file is stored, but the metadata
API the cleanup step reads can lag behind by seconds-to-minutes. A
naive `sleep 30` is not enough.

**Symptom.** Per-item keep-2 cleanup leaves stale ZIMs in place. The
archive.org item swells to N times the actual current ZIM size. The
2026-04-28 DC upload caught this — item ended up with four dated
ZIMs (Apr 20 @ 176 MB, 22, 28, plus an earlier stub) and the site
rendered as ~700 MB.

**Fix landed (commit `5aeb5d8`).** `cloud/upload_validated.sh` now
polls `ia metadata` for the just-uploaded filename in a 10-second
loop with a 3-minute deadline before invoking
`cloud/cleanup_old_zims.py`. Loud WARN if the deadline expires;
cleanup still runs (best-effort) so we don't make outages worse.

**Manual recovery if you discover a stale item right now.**

```sh
PATH="$PWD/venv312/bin:$PATH" ./venv312/bin/python3 \
    cloud/cleanup_old_zims.py streetzim-<id> --keep 2
./venv312/bin/python3 web/generate.py --deploy
```

(`cleanup_old_zims.py` shells out to bare `ia` via `subprocess.run`,
so PATH must include the venv `bin/` or it raises `FileNotFoundError`
silently before listing items.)

---

## 4. Embedded viewer must understand whatever SZCI version we ship

**Symptom.** Validator passes. Smoke against the deployed PWA may
pass too (depending on what's deployed). Open the ZIM in iOS Kiwix
or Kiwix Desktop and Directions stays at "Loading routing data…",
console error: `Failed to load routing graph: HTTP absent`.

**Cause.** `repackage_zim.py` emits **SZCI v1** (nodes inline in
`graph-cells-index.bin`) for small graphs and **SZCI v2** (nodes
sharded into `routing-data/nodes-scaled-NNN.bin` files) once node
count crosses the inline-threshold. Continent-scale builds and
mid-size regions like Ukraine (10 M+ nodes) trip v2 every time.

The viewer's `parseRoutingCellsIndex` had `if (version !== 1) throw`
for months. v2 ZIMs fell through to the chunked-graph fallback path,
which doesn't exist for spatial layouts, surfacing as the cryptic
`HTTP absent` message above. Silicon Valley masked the bug —
small enough to stay v1.

**The non-obvious bit.** The viewer is *bundled inside the ZIM*.
Kiwix iOS / Android / Desktop all read JS from `index.html` inside
the ZIM, not from `streetzim.web.app/drive/viewer/`. Fixing
`resources/viewer/index.html` and deploying the PWA fixes nothing
for native Kiwix users until the ZIM itself is repacked with the
new viewer baked in (`cloud/repackage_zim.py` swaps the viewer by
default; `--no-swap-viewer` to opt out).

**Fix landed (commit `b4e1000`).** `parseRoutingCellsIndex` accepts
v1 *and* v2; for v2, `loadNodeShards(idx)` fetches every
`nodes-scaled-NNN.bin` in parallel before constructing the
`SpatialGraph`. Re-pack and re-upload Ukraine done in commit
`46ccf05` (`-b` suffix for the same-day reroll).

**Validator gap.** `_chk_routing` only verifies the SZCI buffer
parses on the Python side (which has had v2 support for months) — it
does *not* replay the JS reader path. Worth adding a check that
the major-version of the shipped viewer JS supports the SZCI
version the ZIM emits.

**Smoke harness rule.** Pre-upload smoke against `localhost:8765`
serving the freshly-built ZIM is the load-bearing gate here. The
PWA viewer is downstream — it's a deployment artifact, not the
contract surface for native Kiwix consumers.

---

## 5. Repackage flag traps when the source is already-repacked

`build_region.sh` runs `cloud/repackage_zim.py --spatial-chunk-scale 1`
internally whenever the create_osm_zim graph exceeds 500 MB (Iran,
California, Ukraine, Canada, …). That internal repack uses the
default `drop_llm_bundle=True`, so by the time the wrapper's
*second* repack (LLM-drop, viewer-swap, search splits, chip splits)
runs, the source ZIM is already spatial AND already has no
`poi.json`. Two flags fail silently in this state.

### `--spatial-chunk-scale` on an already-spatial source: WIPES routing

```
warning: no routing-data/graph.bin in source — nothing to upgrade.
Output will have no routing.
```

The repack proceeds anyway, emitting a routing-less ZIM that
validator will reject (`hasRouting=True but no graph.bin or spatial
index found`). On the California rebuild this cost a 14-minute
repack run before the validator caught it.

**How to avoid.** Probe for `routing-data/graph.bin` first; only
pass `--spatial-chunk-scale` when the source is monolithic.

```sh
./venv312/bin/python3 -c "
from libzim.reader import Archive
import sys
a = Archive(sys.argv[1])
try:    a.get_entry_by_path('routing-data/graph.bin');  sys.exit(0)
except Exception:                                       sys.exit(1)
" "$RAW" && SCALE_FLAG=(--spatial-chunk-scale 10) || SCALE_FLAG=()
```

### `--split-find-chips` on a source without `poi.json`: DELETES existing chips

`--split-find-chips` does two things:

1. SKIP passthrough of any `category-index/chip-*.json` from the
   source (so newly-derived chips don't collide).
2. Re-derive chips from `category-index/poi.json` +
   `category-index/park.json`.

If `poi.json` was already dropped (by the prior internal repack's
LLM-drop), step 2 produces zero records and step 1 still discards
the chips that were validly passed through from the original
create_osm_zim build. End result: **manifest still lists chips, but
the per-chip `chip-{id}.json` files are missing**. Find page
falls back to `poi.json` (also gone) and OOMs the browser.

**How to avoid.** Probe for `category-index/poi.json`; only pass
`--split-find-chips` when poi is still present. When it's absent,
let chips passthrough untouched.

### Both checks live in `build-region-and-upload.sh`

The wrapper now detects both conditions and conditionally drops the
problematic flags. Replicate the same probe if you write a one-off
repack script.

**Validator gap.** `_chk_find_chips` registers `error`-severity but
only fires when the manifest declares `chips`. A source that came
through this trap has `manifest.chips = {}` (empty), so the chips
check is `[SKIP]` and the lethal-on-mobile fallback to `poi.json`
ships unflagged. A future check should also verify chips exist
when `poi.json` was supposed to be the source data.
