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
