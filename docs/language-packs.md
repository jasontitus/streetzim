# Maps in any language without a copy per language

Status: design, 2026-10-13. Not built yet. The measurements behind it are in
`/storage/experiments/langpack/{inventory,merge,multiling}/REPORT.md` on the
build host, with their scripts.

## The goal

Someone should be able to ask for "Armenian Armenia" or "French USA" and get it
within a minute or two. We should not build, store and upload a full ZIM for
every region in every language. Today a language is a whole build: German
Switzerland is a second 3.5 GB file next to the English 2.9 GB one.

## What the measurements say

**What the language changes.** Switzerland was built twice, English and German, and the
two ZIMs were compared entry by entry:

- **The same in every build:** routing (413 MB), satellite, terrain, fonts
  and the viewer. The viewer already carries every UI language.
- **What the language really changes on the map is small.**
  - `name:de` appears on 44,144 of 3.78 M named tile features (+0.04% of
    tile bytes).
  - The search data has about 56,000 renamed record copies.
  - Together that is under 1 MB compressed.
- **What is per language by nature:**
  - Wikipedia: articles plus the images the English build lacks, 859 MB.
  - Kiwix's own search indexes: Xapian title + full text, 597 MB.
  - Wikidata text: about 2 MB.
- **Why a German "pack" over the English build would be 2.46 GB today**
  (84% of the English file):
  - Builds are not reproducible: two English runs differ on 99.6% of the
    tiles, because tilemaker's feature order varies.
  - Kiwix page paths carry a running number, so they renumber.
  - A single renamed record rewrites its whole search shard.

**Carrying many languages at once is cheap.** OSM translations sit on
prominent objects only: 1.6% of named objects in the Swiss box have one. With
about 20 curated languages in every tile and search record, a ZIM grows:

| region | growth |
|---|---|
| Switzerland | +0.15-0.4% |
| Japan | about +1.1% (Tokyo's search records +22%) |
| Egypt | about +0.1-0.3% |

**A ZIM can be assembled from a base and a pack in seconds, and streamed.**
`merge_zim.py` copies every compressed cluster byte for byte and generates
only the tables: header, MIME list, dirents, pointers, the title listing,
`M/Counter`, a derived UUID and the MD5.

- **Correctness.** Merged files pass:
  - `Archive.check()`
  - official zimcheck 3.8 and zimru's zimcheck
  - kiwix-serve, including `/suggest` and `/search`
  - the in-ZIM viewer smoke (17/17)

  English Liechtenstein plus a German pack reproduced the German build
  entry for entry. A deliberately broken merger was caught 9 times out of 9,
  but zimcheck alone missed 4 of the 9.
- **Speed.**
  - Merging the 1.2 GB Switzerland: 1.4 s warm, 11 s cold, under 80 MB of memory.
  - Planning a merge, which gives the download size: 0.3 s for the US,
    about 1 s for Europe.
- **Streaming.** With the right layout, the merged file is the base's bytes
  at their original offsets, the pack's clusters and about 3 KB of generated
  bytes: 23-34 byte ranges in all. A server can stream it with constant
  memory, a known Content-Length and Range support.
- **The one full pass.** The MD5 covers the whole file and the header
  (with the UUID) comes first, so each combination needs one hashing pass at
  about 0.8 GB/s: Switzerland 1.5 s, the US about 55 s, Europe about 90 s.
  It can run during the first download and then be cached.

## The design

### 1. One multilingual base per region (the map in about 20 languages)

The base is today's ZIM with these changes:

- **Tiles carry `name:xx`** for a curated list of about 20 languages
  (`STREETZIM_TILE_LANGUAGES`). The tile profile already supports this.
  Proposed list: ar de el es fa fr he hi it ja ko nl pl pt ru th tr uk vi zh,
  plus English (already present as `name:latin`) and the region's own
  languages. Dialects, transliterations and mass-tagged imports are left
  out on purpose.
- **Search records carry `ln: {lang: name}`**, indexed under those names,
  so "Genf" finds Geneva and shows it as Genf in a German view. In East
  Asia, search translations are capped at 5-10 languages; tiles carry all 20.
- **The viewer switches language at runtime:**
  - Label expressions already read `name:<lang>`, so a switch re-sets
    `text-field` on the symbol layers, as the theme switch does.
  - A picker defaults to the browser's languages that the map supports, or
    to the pack's language, and the choice is remembered.
  - Search display, popups and the UI strings follow the chosen language.
  - The UI needs a translation table per language (only German exists).
  - Map-config gets a `languages` list.
- **Pack-ready layout:**
  - no Xapian in the base;
  - a raw, front-articles-only `X/listing/titleOrdered/v1`;
  - a MIME list that covers what packs use;
  - no paths at or after `~`.

**This alone answers "French USA" for the map:** labels, search and UI in
French from the same file everyone downloads. It needs no packs and no
assembly. For map-only builds (Ultralight), the work is done at this point.
The ZIM's `Language` metadata lists the languages it carries.

### 2. Language packs for what is per language by nature

A pack is a small ZIM, built per (region, language) against a published base:

- **Metadata:** Title (at most 30 characters), Language, a per-language
  Name, Description and the illustration. It also overrides
  `map-config.json` to set the default language and point to the pack's
  content.
- **Wikipedia in that language:** articles, the images the base lacks, and
  the geo-index. Wikidata labels and descriptions in that language.
- **Kiwix pages titled in that language,** and Kiwix's Xapian title and
  full-text indexes over every page searchable in that language. A ZIM can
  have only one index of each kind, so the pack's indexes must cover the
  base pages too.
- **Paths:** every new path sorts after the base's (`~xx/…`). The pack
  deletes nothing, and the base's indices do not move.

Expected sizes, Switzerland German:

| pack | size | share of the base |
|---|---|---|
| map-only (metadata, default language, Kiwix page titles) | 3-5 MB | under 0.5% |
| Wikipedia, no Kiwix search | about 0.9 GB | about 30% |
| Wikipedia and Kiwix search | about 1.5 GB | about 50% |

Dropping Kiwix's own full-text search from packs is the biggest size lever
we control. The map's own search works without it.

### 3. Assembly and delivery

- **Merger.** `streetzim merge BASE PACK -o OUT`, productionised from the
  prototype, is the one assembly tool. Every published combination passes:
  - the merger's entry-level verification;
  - official zimcheck;
  - kiwix-serve `/suggest` and `/search`;
  - the in-ZIM viewer smoke.

  The UUID is derived from the base, the pack and the merger version: every
  combination is its own Kiwix book, and a rebuild of either part counts as
  an update.
- **Prebuilt languages.** The top languages (English, and others by demand)
  are merged at publish time and uploaded like any ZIM today. A reflink
  clone on the build host's XFS makes the merged file in under a second
  plus one MD5 pass.
- **On-demand languages.** Everything else is assembled on request:
  - Base and packs sit as static files, on archive.org or object storage.
  - A small range-splicing server streams the merged ZIM from a cached
    per-combination recipe of 23-34 byte ranges plus a few KB.
  - The MD5 is computed during the first stream and cached; a request for
    the last 16 bytes before then waits for it.
  - Time to first byte is under a second or two; after that it runs at
    download speed.
- **The /drive web app needs no assembly.** Its reader can open the base
  and the pack together and overlay them, so any language works instantly
  in the browser.

## Changes to today's builds (prerequisites)

1. **Stable Kiwix page paths,** derived from the feature's identity rather
   than a running number. This also stops the 400k path moves between two
   builds of the same language.
2. **Search shards whose boundaries do not depend on display names,** and
   deterministic duplicate and street-merge tie-breaks. Without these, a
   pack would have to carry whole shards.
3. **The base carries no Xapian.** Each pack carries combined indexes.
   Builds without a pack (`--xapian none`, the Ultralights) are unaffected.
4. **The Rust packer (zimru) writes a raw, front-only v1 listing.** Today
   it compresses the listing and lists every entry, so libzim ignores it.
   Merging such a base makes Kiwix count 10.7 M articles on the US.
5. **The world tiles are rebuilt once with the language list,** at about
   +0.5-1.5 GB on 115 GB, and the search caches re-extracted with it.
6. **UI translation tables for the listed languages.** 297 strings, chip
   names and about 260 place types per language. Machine translation would
   need review, and the UI would say where a translation is machine-made.

## Phases

1. **Multilingual map (ships with normal rebuilds).**
   - Viewer: runtime language picker, label switch, `ln` in search and
     popups.
   - Builder: `ln` records, map-config `languages`, the tile language list.
   - Regional tilemaker builds (the `streetzim` command) get it at once.
     Production regions get it at the next world-tile rebuild.
2. **Packs and merging.**
   - Builder `--pack LANG --base BASE.zim`.
   - Prerequisites 1-4.
   - `streetzim merge` with its gates.
   - The /drive app opens base + pack.
3. **On-demand delivery.**
   - The range-splicing server.
   - Language choices on each region's card.
   - Optionally an OPDS catalog, so the Kiwix apps can list the
     combinations.

## Open decisions

1. **Where on-demand assembly runs.**
   - (a) The build host, behind a tunnel: no cost, but it serves traffic
     from a shared machine.
   - (b) A small VPS or container reading byte ranges from archive.org:
     archive.org is slow per connection.
   - (c) A Cloudflare Worker over object storage: paid Workers plan for
     the MD5 CPU, plus storage costs.
2. **Whether packs carry Kiwix's own full-text and title search.** It is
   about 40% of a full pack.
3. **The language list,** and whether UI translations may be
   machine-made.

## Risks and limits

- **Not every place gets a translated name.** Translations exist only
  where OSM has them: a French USA map labels states, cities and big
  features in French, and streets keep their English names. That matches
  what a French paper map does.
- **Kiwix desktop and Android download through aria2** with metalinks that
  carry a checksum. For a combination not yet hashed, the metalink can't
  carry the MD5 until the first pass is done.
- **Readers that only know the old v0 title list** (libzim < 7, old
  kiwix-js) were not tested on merged files.
- **A merged ZIM needs both parts.** A new base invalidates every pack
  built against it. Packs therefore record the base's UUID, and the merger
  refuses a mismatch.
