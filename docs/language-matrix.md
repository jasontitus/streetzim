# The language matrix: every map, any language, any mix of Wikipedias

Status: design, 2026-10-13, revised after review (see the next section). It builds on `docs/language-packs.md` (multilingual base, packs, merge) and `docs/language-packs-browser.md` (assembly in the browser). Not built yet.

## Revised after review (2026-10-13): the plan of record

An adversarial review, run against the measurements, found three parts of the design below unworkable. This section replaces those parts; the rest stands.

1. **The base comes from the visitor's disk, not the network.**
   - Measured from the build host, archive.org serves 0.2-2 MB/s per request through the Worker, so a whole US base would take 5-19 h in a browser tab.
   - Instead, the visitor downloads the region's normal published ZIM (Download or Torrent). The builder opens that local file and fetches only the packs over the network.
   - The prefix layout keeps a base's bytes `[first cluster, end of dirents)` at the same offsets in every merged file. So the **published default ZIM is the base**, and no separate bare base is uploaded.
   - Chunk hashes cover that range, so the browser can check the visitor's file.
   - Small regions may still stream everything.
   - The Worker resolves archive.org's two storage nodes once per item and pins the faster one.
2. **A bounded menu with recipes made in advance, instead of free-form mixes and an in-browser planner.**
   - Menu: map language L; Wikipedia = the region's own languages ∪ {L} ∪ {en}. This still covers "a Thai map of the US with Thai and English Wikipedia".
   - The build host plans every menu entry (0.3-1.3 s each) and gates a sample of them. It publishes each recipe with a SHA-256 for every generated segment.
   - The browser splices the segments and verifies every byte against the recipe and the chunk hashes, then writes the MD5. A buggy planner can't produce a silently wrong file, because there is no planner in the browser.
3. **Kiwix search stays.**
   - The published defaults (the bases) keep their Xapian title and full-text indexes.
   - Kiwix place pages are titled "native · English", as `kiwix_page_title` does.
   - Each base is gated on its v1 listing holding its front pages. Without a title index, libzim only does exact, accent-sensitive prefix matches, and a base with an empty v1 suggests nothing.

**Whom the map is for (the user, 2026-10-13):** "the map a Thai person would want if they were going to the USA". The map language is the *traveller's* language, and that sets five things:

1. **The interface** is in the traveller's language. This needs a UI table per language: machine-translated for the prototype and marked as such.
2. **Labels are bilingual, not replaced.** The local name comes first, because it is what the signs say. The traveller's name goes underneath, smaller, where OSM has one that differs. MapLibre `format` makes this a two-line label.
3. **Search finds both:** local names as today, plus the traveller's-language names where they exist (`ln`, brought forward from "deferred").
4. **Wikipedia follows the traveller's order:** the traveller's language first, then English, then the region's own languages. The Wiki panel and popups follow that order.
5. **Short descriptions** come from Wikidata labels and descriptions in the traveller's language (the lang-text pack, brought forward). Wikidata covers far more places than that language's Wikipedia does.

The prototype's showcase entry is "California → ไทย": a Thai visitor's California, standing in for the whole US, whose base is 35 GB.

**Other review points adopted:**
- **Viewer.** Multi-language Wikipedia means:
  - a Q-ID-based article lookup;
  - per-language geo-indexes under `~w/xx/`;
  - `sw.js` recognising `~w/` articles for the back-to-map bar;
  - the bar handling a prefix path depth.
- **PWA reader.** The PWA's ZIM reader must decode zstd clusters as streams to the end of the frame. In a merged file the base's last cluster is followed by old tables, not by a cluster.
- **Naming and metadata.**
  - Names keep the `osm_<lang>_<region>` lineage, so Kiwix sees updates.
  - The Wikipedia set goes in Flavour.
  - Title ≤ 30 is counted in UTF-16 code units.
- **Pack sources.** A standalone pack builder works from local Kiwix Wikipedia ZIMs; the top languages are being downloaded to `/storage/streetzim/wiki-src`. Wikidata text comes from a dump, not the API under maxlag.
- **Thai** is in the prototype even though it is not in the measured top 10.

**Deferred past the prototype:**
- the free-form planner
- the full 70-region matrix

Brought forward for the traveller (see above): lang-text packs, `ln` search, bilingual labels, and UI tables for the prototype's languages.

Multilingual tiles are built for the prototype regions anyway, since that is cheap.

**Prototype (California and Greece), in order:**
1. **Builder page skeleton with analytics.** GA4 events: `build_probe`, `build_start`, `build_progress`, `build_end`, with throughput, TTFB, retries, resets and time hidden. They are not sent for webdriver, localhost or `sz_dev`. Plus Worker node pinning. This proves real users' speeds.
2. **Rebuilt bases:** multilingual tiles, place pages, raw front-only v1, Xapian.
3. **`~w/xx` packs** from the local Wikipedia ZIMs: for Greece el, de, fr, th; for California es, zh, ja, th.
4. **Viewer support for several Wikipedias,** tested first in the PWA with base and packs mounted side by side.
5. **The Python N-pack merge** with menu recipes and gates: verify_merge, zimcheck, kiwix-serve, the Kiwix viewer gate, and the PWA opening the merged file.
6. **The builder at `/lab/build/`:** local base, recipe splicing, SHA-256 and MD5, and File System Access / service-worker sinks. Deployed outside the `/drive/` service-worker scope, from a clean tree.

## What we want

Someone picks a region on streetzim.web.app, then makes two choices:
- **Map language:** the language for labels, search and the interface.
- **Wikipedia languages:** which Wikipedias to bundle. The default is the region's own languages, and any others can be ticked.

For example: a Thai map of the United States, with about 500 Thai Wikipedia articles and about 30,000 English ones.

The site builds that ZIM in the visitor's browser from parts stored on archive.org. The visitor saves it and opens it in Kiwix. We rebuild the parts every data round, starting with 10 languages.

## The parts (static files on archive.org)

Per region, per data round:

| part | what it holds | how many |
|---|---|---|
| **base** | the multilingual map: tiles and search with ~20 languages' names, routing, terrain, satellite as today; Kiwix place pages titled by the place's own name; the viewer with every UI language; no Wikipedia; no Xapian | 1 |
| **lang-text/xx** | Wikidata labels and descriptions in xx, used by popups and place panels | 1 per language |
| **wiki/xx** | xx Wikipedia: the articles about the region's places, their images and the geo-index | 1 per language that has articles |
| **manifest.json** | the files, their sizes, SHA-256 per 64 MB chunk, article counts per language, the region's own languages, the base's sidecar files and the merger version | 1 |
| **base sidecars** | the titles of the base's front articles in title order (for merging the title listing), plus the base's table offsets | 1 |

Each pack is a small ZIM whose paths sit under its own prefix, after everything in the base:
- `~t/xx/…` for lang-text
- `~w/xx/…` for wiki

Packs never override or delete base entries, so the base's bytes and offsets stay as they are in the merged file. The only file that has to change is `map-config.json`, which says which packs are present. The merger generates it as part of the inline bytes, so no pack needs to override it.

**Images are stored per language.** An image used by both the English and the Thai article is stored once in each pack. A mixed file can hold some duplicates, since removing them would mean splitting clusters, so the language with fewer articles pays a little.

## Merging N packs

The prototype merges one pack. Merging N packs with disjoint prefixes is the same algorithm. The merged file is laid out as:

```
generated head          header, MIME list (the base's list is a superset of every pack's)
base bytes              verbatim, at their original offsets
pack 1..N clusters      verbatim
generated tail          merge cluster (map-config.json, M/ metadata, M/Counter, v1 listing),
                        path pointers  = base pointers + pack pointers in prefix order
                        cluster pointers = base + packs, offset-shifted
                        pack dirents, re-encoded with shifted cluster numbers
MD5                     16 bytes
```

**v1 title listing.** This is the only step that is not a concatenation. The base's front articles (the place pages) and every pack's front articles (the Wikipedia articles) must be listed in title order. The listing is a k-way merge of streams that are each already sorted:
- the base's titles come from the base-fronts sidecar;
- each pack's titles come from its own small dirent table.

Memory stays constant.

**Kiwix search: no Xapian.** No combined Xapian index can be precomputed for arbitrary language sets, so assembled files carry none. Kiwix then suggests by title prefix over the v1 listing, which covers place pages and Wikipedia articles in every bundled language. Kiwix's full-text search inside article text is lost. The map's own search, which is the main way to find places, is unaffected.

**Metadata,** generated from the choices:

| field | value |
|---|---|
| Title | the region's name in the map language (from the manifest), at most 30 characters |
| Language | the map language plus the Wikipedia languages, ISO 639-3, comma-separated |
| Name | `streetzim_<region>_<maplang>[_w-<langs>]`, so Kiwix groups updates of the same choice |
| Description | in the map language |
| UUID | derived from the base UUID, the sorted pack UUIDs, the map language and the merger version: every choice is its own book, the same bytes every time, and a new round gives a new UUID, which Kiwix treats as an update |

## Integrity without a precomputed checksum

There are too many choices to merge and gate each one in advance (2^N per region), so the browser computes the plan itself. Integrity comes from three things:

1. **Inputs.** Every fetched chunk of the base and the packs is checked against the manifest's SHA-256 before it is written. archive.org, the proxy or the network corrupting a byte gets caught. Torrent piece hashes would work too, but SHA-256 is in WebCrypto.
2. **Generated bytes.** The planner is one JavaScript implementation, and it must produce bytes identical to the Python merger's. CI merges a set of combinations both ways and compares them byte for byte. Every published round also gets gated merges on the build host for each region, using its own languages plus English plus one small pack:
   - entry-level verification
   - zimcheck
   - kiwix-serve
   - the viewer smoke
3. **Output.** The browser computes the ZIM's MD5 while writing and appends it. The ZIM is then valid for `zimcheck` and `Archive.check()`.

## The viewer, in every merged file

- **Map language** comes from `map-config.json`'s `language` and can be changed at runtime with a picker. It sets labels and search display through `name:xx`, the UI strings, and the popup and place text, which is read from `~t/xx/` when present and falls back to English.
- **Wikipedia** comes from the bundled languages listed in `map-config.json` `wiki: ["th","en"]`. Each language has its own geo-index at `~w/xx/wiki-geo-index.json`. A place with articles in several bundled languages links to the map language's article first and offers "also in English". The Wiki panel lists articles in view across all bundled languages, map language first.
- **Old viewers** do not know `~w/` and simply show no Wikipedia. Merged files always carry the current viewer, because it is in the base.

## The site builder

On each region's card, "Build in another language…" opens `/drive/build?region=…`. The page shows:
- The map language: the browser's language if supported, else the region's.
- Wikipedia: a checklist with article count and size per language, for example "ไทย 512 articles · 14 MB" and "English 31,240 · 1.1 GB". The region's own languages are ticked by default.
- The total size, an estimated time, and "Save as…".

Then it plans, fetches through the range proxy, verifies, writes and hashes, and offers "Verified · open in the web app".

**Default downloads stay prebuilt.** The plain Download and Torrent buttons, and Kiwix's own catalog, keep offering prebuilt files for the region's own language and for English. These are assembled on the build host and uploaded like today's ZIMs.

## Rebuilding it all

**Per round:**
1. **Bases.** As today, plus:
   - multilingual tiles: the world tiles are rebuilt once with the language list, about +0.5-1.5 GB on 115 GB;
   - `ln` search records;
   - base layout rules: no Xapian, a raw front-only v1, the MIME superset, and nothing at or after `~`.
2. **Language parts.** For each region, every matrix language with at least one article:
   - `lang-text/xx`: Wikidata labels and descriptions, from the Wikidata API under the shared pacer, or from a dump;
   - `wiki/xx`: articles from a local Kiwix Wikipedia ZIM in xx (`wikipedia_xx_all_maxi`), which avoids API rate limits, plus WebP thumbnails as today;
   - the pack's dirent table and its titles for the manifest.

   The top 10 languages' Wikipedia ZIMs are a one-time download of a few hundred GB; /storage has room.
3. **Manifest and gates:** chunk hashes, the gated merges, and CI's comparison of the JavaScript and Python planners.
4. **Upload to archive.org:**
   - `streetzim-<region>`: the base and the prebuilt default files, as today;
   - `streetzim-<region>-lang`: the packs, the manifest and the sidecars;
   - the usual prune, keeping two rounds.

**Sizes.** Measured in `/storage/experiments/langpack/matrix/`. Most region and language combinations are small, because foreign Wikipedias cover few places; the largest are each region's own languages and English.

## Phases

1. **Multilingual base and viewer language picker.** Useful on its own: one file, maps in 20 languages.
2. **Viewer support for several Wikipedias.** Plus the pack builder, the multi-pack Python merger with its gates, and prebuilt defaults.
3. **Browser builder:**
   - the JS planner with tests identical to the Python merger's;
   - the assembler with sinks;
   - the builder page;
   - manifests and chunk hashes.
4. **Rebuild every region × the top 10 languages,** upload, and publish the builder on the site.

## Open decisions

1. **The first 10 languages.** The matrix measurement proposes some. Each region's own languages are always included.
2. **Losing Kiwix full-text search in custom builds.** Title suggestions and the map's search stay. The prebuilt files could keep full text: build the combined index on the host for those two combinations only.
3. **Whether the plain Download stays the prebuilt own-language + English file,** or becomes the builder's default.
4. **Where parts live:** archive.org behind our range proxy (free; Workers free plan ≈ 100k requests/day), or Hugging Face / R2 for native CORS and speed.
