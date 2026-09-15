# Find chips: geographic shards

The Find chips (Restaurants, Cafés, Shops, …) read pre-filtered record
lists from `category-index/chip-{id}.json`; the rules live in
`cloud/chip_rules.py`. This page covers how those lists are laid out in
the ZIM and how the viewers load them.

## Why

Until 2026-09-15 a chip over 10 MB was split into FNV-1a *name*-hash
buckets. A name hash has no locality, so both viewers fetched every
bucket and concatenated them. A Shops tap parsed the whole chip:

| ZIM | Shops chip | Restaurants chip |
|---|---|---|
| east-coast-us | 147.5 MB, 758,849 records | 97.6 MB |
| brazil | 217.1 MB, 1,041,447 records | 98.1 MB |

Continents are several times larger. `JSON.parse` of that much text costs
roughly 4–5× its size in JS heap. That is past what iOS allows a WebView
content process (about 1.5 GB soft) before it is killed, and the user
only ever sees 300 results near one point.

## Layout

A chip whose JSON array is at most 2 MiB stays a single
`chip-{id}.json`. A bigger chip is cut by `cloud/chip_shards.py` with a
byte-weighted k-d split, alternating on the longer axis. The result is a
set of spatially compact shards of 1–2 MiB each, named
`chip-{id}-g000.json`, `-g001`, and so on. Records keep their original
order inside a shard. The manifest entry is a superset of the old one:

```json
"shops": {
  "label": "Shops", "count": 758849, "bytes": 154659011,
  "sub_chunks": ["g000", "g001", "..."],
  "layout": "geo",
  "shards": [[s, w, n, e, count, bytes], "..."]
}
```

- **`count` and `bytes`** still describe the whole chip.
- **`sub_chunks`** is still the complete file list, so a reader that knows only the old format (older viewers, mcpzim) still gets every record.
- **`n_sub_buckets`** is gone for geo chips, because it meant "route by name hash".
- **`shards[i]`** lines up with `sub_chunks[i]`. The bbox is rounded outward to 1e-5°. `w > e` means the shard crosses ±180; its longitude range is the smallest arc covering its points, not a box spanning the whole world. Records without usable coordinates go in trailing shards whose bbox values are `null`.

Measured on east-coast-us: Shops becomes 128 shards of 1.15 MB each, and
Manhattan falls in a single shard. Planning the whole chip takes 4.4 s.

## Loading (viewer)

`resources/viewer/index.html` and `places.html` carry the same ES5 block
between `// BEGIN chip-shards` and `// END chip-shards`.
`tests/chip_shards_js.test.mjs` fails if the copies differ.
`CHIP_SHARDS.load(meta, opts)` returns the **exact k nearest** matching
records (k = 300) around a point:

1. **Rank.** Candidate shards are the ones that overlap the viewport, when
   there is one. They are ranked by a great-circle *lower bound* from the
   point to the box: `hav(d) ≥ hav(Δφmin) + cos φp · min(cos s, cos n) ·
   hav(Δλmin)`. Each term is at its minimum over the box, so the bound
   holds at any latitude and across ±180.
2. **Load.** Shards are fetched in batches of up to 4 until the k-th
   nearest record is closer than the next shard's bound. Results are
   then exact.
3. **Stop on budget.** Loading also stops at a byte budget:
   `navigator.deviceMemory` × 4 MiB, clamped to 12–32 MiB. Browsers without
   `deviceMemory` (WebKit/iOS) get 16 MiB. A stop here, or a failed fetch,
   marks the result `partial` with `radiusKm`. Everything within that
   radius is exact.
4. **Filter inside the search.** The name query and the sub-type filter
   are applied while searching. Cafés + "Starbucks" returns the nearest
   Starbucks, not the Starbucks among the nearest 300 cafés.
5. **Cache and cancel.** Shards sit in an LRU keyed `chipId/suffix`,
   capped at one budget. Failed fetches are never cached, and the cache is
   cleared on `pagehide`. A newer tap cancels an older load before its
   next batch.

- **Map (`index.html`):** the point is the map centre and the scope is the
  viewport. When nothing is in view, the first tap falls back to the
  nearest records anywhere; "Search this area" does not fall back.
- **Find page (`places.html`):** the point is the GPS fix or the chosen
  place. Without one it is the map-area centre, then the region centre
  from `map-config.json`. The scope is the map area when "Limit to map
  area" is on.
- **Status line:** it says "300 nearest matches" rather than a region-wide
  total. Sub-filter chips show the histogram of the chip's own nearest set.

ZIMs with name-hash buckets or single files keep the old whole-chip path.

## Producing and checking

- **Build:** `create_osm_zim.py --split-find-chips` emits geo shards.
  `build-region-fast.sh` passes that flag.
- **Retrofit:** `cloud/repackage_zim.py SRC DST --split-find-chips`
  re-shards an existing ZIM. When the source has no `poi.json`/`park.json`
  (every `--no-llm-bundle` build), it rebuilds each chip from the source's
  own chip files, whatever their layout, so re-running it on a geo ZIM is
  idempotent. `--chip-shard-mb N` sets the shard size.
  `--chip-split-threshold-mb` is deprecated: 0 still disables splitting,
  and any other value is ignored.
- **Validate:** `cloud/validate_zim.py` (`find_chips`) checks each shard
  file against its manifest row (count, bytes, every record inside the
  bbox) and that the rows line up with `sub_chunks`. It warns on a geo
  shard over 16 MB, and on name-hash chips over 50 MB, which still load
  whole on phones.
- **Smoke:** `cloud/pwa_smoke_test.mjs` logs how many chip files and bytes
  the Find step fetched. It fails if a geo chip pulls more than 64 MB.
