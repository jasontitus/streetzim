# Search: give hot prefix chunks locality (proposal v4, 2026-09-16)

> Design record. Implemented in `aab3334` (2026-09-16): `cloud/search_shards.py`, the viewer's `search-shards` blocks, `--reshard-search`. The current layout is in [search-records.md](search-records.md#search-data).

Status: design, revised after two adversarial reviews and four measurement
passes over real ZIMs. Implement → retrofit in the same pass as the Find-chip
shards (`docs/find-chip-shards.md`).

## The problem, measured

`search-data/` is keyed by a 2-character prefix. A prefix whose chunk exceeds
`--split-hot-search-chunks-mb` (10 MB in every build wrapper) is fanned out by
**FNV-1a hash of the record name**, recursively to depth 5
(`_split_records_recursive`, `cloud/repackage_zim.py`). Hash buckets bear no
relation to what the user typed, so a query fetches **every** leaf under its
prefix and filters client-side.

Measured on shipped ZIMs (headless Chromium, real service worker):

| region | query | leaves fetched | bytes | time |
|---|---|---|---|---|
| south-america | `Caracas` | 736 | 320 MB | 99 s |
| brazil (pre-retrofit) | `Rio de Janeiro` | 768 | 3.2 GB | 182 s |

Both return the right answer eventually; nobody waits. Worst prefix per region:
4096 leaves — united-states `av` (2.93 GB), europe `de` (3.65 GB),
south-america `de` (2.73 GB); 256 leaves in 19 more regions; ≤ 16 elsewhere,
where search is already fine.

South-america `ca` (736 leaves, 19.6 M records, 2511 MB): addresses 86.2 %,
POIs 8.3 %, streets 4.7 %, place-like 0.8 %. `Caracas` is a `place` record —
every place-like record under `ca` is 20 MB of that 2511 MB.

## Layout

**1. Character paths, not hashes.** A record is in `ca` because a word of its
normalised name starts with `ca` (`_prefixes_for`), or because the whole name
does (`nn[:2]`, spaces folded to `_`). The path is that word's 3rd character,
then 4th, 5th…, **while the leaf exceeds the threshold, to depth 4**. Depth is
adaptive: most prefixes stop at 1, skewed ones (`av` → "aven…") keep going.

**2. Tiers, fetched in order.** Types come from a fixed 8-value vocabulary
(`addr, poi, street, water, place, park, peak, airport`; `t` is never absent —
0 of 412,510 sampled records). `highway` does not occur: roads are
`t="street"` with the class in `s`.

| tier | types | fetched |
|---|---|---|
| `c` | place, airport, peak, park, water | always first, in full |
| `p` | poi (and any unknown future `t`) | next, under the budget |
| `s` | street | only when tier c+p give few matches |
| `a` | addr | only when the query has a digit **and** ≥ 4 word characters |

**3. Hash split inside a character leaf** when characters stop dividing it
("Carrera 7" repeats a million times). Those children keep today's `-0…-f`
names, so the name says which scheme produced it, and `expandPrefix` already
resolves them.

Leaf name: `prefix ~ path ~ tier` (`ca~r~c`, `ca~ra~p`, `ca~rr~a-3`).
Non-ASCII prefixes are `u<hex>` codepoint buckets; path characters there are
further code points (`Array.from(word)` in JS), emitted as hex
(`u627~u628~c`) so names stay ASCII.

### Measured result for `ca` (threshold 4 MB, depth ≤ 4)

| tier | leaves | biggest leaf | over 16 MB |
|---|---|---|---|
| c | 87 | 2.7 MB | 0 |
| p | 871 | 10.4 MB | 0 |
| s | 243 | 39.0 MB | 2 |
| a | 1655 | 162.9 MB | 21 |

Tiers c and p — everything a name query reads — are fully bounded. The 23
oversized leaves are all street/address tiers where characters stop dividing;
they get the hash fallback. What a query costs:

| typed | tier c | tier p |
|---|---|---|
| `car` | 2.6 MB | 51 MB (streamed, capped) |
| `cara` | 0.7 MB | 3–6 MB |

## Manifest

Measured, not estimated. Today: united-states 1887 KB, europe 1364 KB,
south-america 1003 KB. For `ca`, the new layout costs ~56 KB against today's
~14 KB — leaf count rises from 736 to 2856, mostly tier a.

Budget control: **tier a is character-split only to depth 2**, then hash-split.
It is read only for digit queries, so its locality matters least, and it is
75 % of the new leaf count. With that cap the whole-manifest growth stays
under ~1 MB on the worst region; the implementation must print the resulting
manifest size per region, and the validator must fail a manifest over 4 MB.

```json
{"total": N,
 "chunks": {"ca~r~c": 23755, "ca~ra~p": 14902, ...},
 "sub_chunks": {"ca": ["ca~_~c", ..., "ca~r~a"]},
 "char_split": {"ca": ["_", "a", ..., "r", "ra", "rl"]}}
```

* `sub_chunks[prefix]` stays the exact leaf union: old clients (iOS, in-ZIM
  apps — `docs/in-zim-apps.md`) resolve through it and fetch everything — as
  slow as today, never wrong. It cannot be derived away, because their
  `expandPrefix` treats a name as a leaf only when it appears in `chunks`.
* **Existing US/EU manifests are trees, not flat lists** — 143 (US) and 134
  (EU) hot prefixes ship `sub_chunks[prefix] = []` with the real relationship
  one level down. Those regions survive today only through the client's
  scan for `chunks` keys starting `prefix + "-"`, which **cannot match `~`
  names**. So: both clients must scan for `prefix + "~"` as well, and that fix
  must ship before any `~` ZIM does. The retrofit flattens those trees.
* New clients resolve a leaf **through `expandPrefix`**, never by direct
  fetch: a later `repackage_zim.py` hash-split renames `ca~r~p` to
  `ca~r~p-0…` and copies `char_split` through verbatim.

## Client changes

`resources/viewer/index.html`, `places.html`, and the identical copies under
`web/drive/viewer/`:

* ≥ 3 characters: longest matching `char_split` path; tier c, then p, then s;
  a only on a digit query of ≥ 4 word characters.
* **A typed 3rd character with no path entry means no records** — render "no
  results" immediately. Never degrade to the whole prefix (2.5 GB).
* Tier c is drained **in full** before any cap: its worst leaf is 2.7 MB, and
  it carries the `placeSubBonus` city/county records that `scoreResults`
  (index.html:4692-4776) relies on to rank a distant city above nearby noise.
  Capping tier c would drop the obviously-correct answer.
* Cap tiers p/s/a at ~300 matches **after** dedup (`scoreResults` dedups on
  `n|a|o`; duplication is 1.12 leaves/record) and at a record budget read from
  `chunks` counts — a byte budget is not measurable client-side
  (`fetchChunk` parses JSON; the manifest stores counts, not bytes).
* Fall back to the full leaf set when the targeted leaves yield fewer than ~5
  matches, restoring mid-word hits ("Caiçara" for `car`, ~2 % of hits).
* 2 characters: unchanged (all leaves, streamed, capped).
* `places.html` additionally needs the map page's streaming fetcher and its
  addresses-only-on-a-digit rule; today it `Promise.all`s every leaf.

### Text folding

Every key and path above is computed from the **folded** name, and the viewer
must fold exactly as the writer did or it asks for leaves the writer never
wrote. The writer's fold is `cloud/search_shards.py` `norm` (used by
`prefixes_for`, which `streetzim/zim_writer.py` calls): NFKD, drop every character
whose **canonical combining class** is not 0 (`unicodedata.combining`), then
lower-case. The viewers use `SEARCH_SHARDS.fold` (the search-shards block of
`300-search.js` and `places.html`; `normalizeText` and `foldText` delegate to
it) for names, queries, keys and paths alike.

Not `/\p{M}/gu`: that drops every mark, including Indic vowel signs and Thai
vowels, which are marks with class 0. Until 2026-10 the viewers did, so
"कोलकाता" folded to "कलकत" in the viewer and stayed "कोलकाता" in the index —
different keys and, where a prefix is split into character-path leaves,
different leaves: the viewer read the wrong ones (on southeast-asia
2026-09-28, "พัทยา" found 8 matching records instead of 5,369, and
"เชียงใหม่" only street names). Prefixes not split by character (Myanmar,
Khmer and Lao there) were read whole and still matched, since both sides of
the in-viewer filter used the same fold. The writer's rule is the fixed point: every
published index was written with it and new viewers are patched into old ZIMs.

JavaScript has no combining-class API, so `tools/gen_combining_marks.py`
writes the class-≠-0 code points as ranges into both viewers (between
`// BEGIN combining-marks` and `// END combining-marks`), from the newest
Python in use (3.14, Unicode 16.0.0 — e.g. in the Docker image) and records
that Unicode version. `--check` (CI) regenerates with the running Python: an
exact match on the same version; on an older one (3.12, Unicode 15.0.0) the
table must be a superset whose extras are unassigned there (combining classes
never change once assigned); a newer Python fails until the table is
regenerated with it. `tests/search_fold_js.test.mjs` compares the fold, the
prefix keys and the leaf paths against the Python writer.

### Word rule

A name is indexed under the key of each of its **words** (plus its first two
characters), and a hot prefix is split by the characters that follow the key
in that word. So the reader must cut a query into words exactly where the
writer cut the name. The manifest says how: `"word_rule"` in
`search-data/manifest.json`, **absent = 1**.

* **Rule 1** (every ZIM written before 2026-10): a word is a run of
  alphanumerics, `[^\W_]+`. A mark is not alphanumeric, so a mark the fold
  keeps — canonical combining class 0: Indic vowel signs (mostly `Mc`), Thai
  vowels such as U+0E31 and U+0E34–0E37, Khmer, Myanmar, Lao, Sinhala and
  Tibetan vowel signs — **ended the word**: कोलकाता → क, लक, त; เชียงใหม่ →
  เช, ยงใหม; พัทยา → พ, ทยา. A 1–2 character fragment matches a whole
  prefix subtree, and once the viewer folded like the writer it requested
  exactly those fragments: on southeast-asia 2026-09-28, พัทยา read 70 leaves
  (7.1 MB) and เชียงใหม่ 40 leaves (8.4 MB) per keystroke, 0.5–2 s on a phone.
* **Rule 2** (`"word_rule": 2`, what every writer records now): a word is a
  maximal run of characters that are alphanumeric (`str.isalnum`) **or a
  mark** (category `Mn`, `Mc`, `Me`), never `_`. Marks continue a word but
  **never start one**: a mark at the start of a run (after a space, or a name
  that begins with a stray vowel sign) is skipped and the word begins at the
  first alphanumeric, so a run of marks alone is no word. `cloud/search_shards.py`
  `words(nn, rule)` and `prefixes_for(name, rule)` are the one implementation
  the build (`streetzim/zim_writer.py` pass 1, the planner through
  `record_paths`/`Aggregator`/`leaf_for`) and the retrofit use.

The fold runs first and already drops every mark of class ≠ 0, so what rule 2
keeps inside words is the class-0 marks. Latin, Cyrillic, Greek, CJK, Hangul,
Georgian, Armenian, Ethiopic and pointed Arabic/Hebrew have none left after the
fold, so their keys, paths and leaves are **byte-identical** under both rules —
measured by running the writer's search passes (`_search_bucket`,
`_search_emit_chunks`) at `8ece2cf` and with rule 2 over the real feature
files of washington-dc, iceland, baltics, caucasus, egypt and hawaii: every
`search-data/*.json` identical, the manifests identical but for `word_rule`.
Switzerland differs in exactly 6 prefixes (`ue04`, `ue25`, `ue27`, `uba9`,
`ubb3`, `ubb5`): its 2 Thai/Tamil names. Over all 5,935,467 southeast-asia
feature names, the 68,383 whose keys or paths change all contain a class-0
mark after the fold (Thai 49,863, Myanmar 14,131, Khmer 2,536, Bengali 873,
Lao 950, a few Javanese/Balinese/Tibetan). Some Thai names never
fragmented (กรุงเทพ: its only mark, sara u, has class 103 and is folded
away); the ones with a class-0 vowel did. A name like "ที่10" did too, and
put itself under the digit prefix `10` — those records leave `10` under
rule 2, so ASCII digit prefixes of such a region change a little.

**The viewer** (`SEARCH_SHARDS.wordRule(manifest)` / `SEARCH_SHARDS.words(folded,
manifest)` in the search-shards block, used by `getPrefixes` in
`300-search.js` and by `prefixesFor`/`searchLeavesFor` in `places.html`):
rule 2 splits on `/[^\p{L}\p{M}\p{N}]+/u`, strips leading `\p{M}`, and keeps
words of ≥ 2 **code points** (Python's `len`); anything but `word_rule === 2`
is rule 1, the old split byte for byte (`/[^\p{L}\p{N}]+/u`, ≥ 2 UTF-16
units). `[\p{L}\p{M}\p{N}]` is the same set as Python's isalnum-or-mark minus
`_` on every code point assigned in both engines (Python's `\w` is exactly
`isalnum` + `_`; `ª`, superscripts and other-script digits agree;
`tests/search_word_rule_js.test.mjs` checks all 0x110000).
Matching and scoring are unchanged: they compare folded substrings and
whitespace-separated query words, not index words.

Measured on southeast-asia 2026-09-28 (23,113,590 records), rebuilt with
`--rebuild-search` (1 h 24 min, peak RSS 13.9 GB, during the pack): search
leaves 113,576 → 108,781 and 11.59 → 10.94 GB uncompressed — the non-ASCII
leaves 3.49 → 2.85 GB, the fragments' duplicates gone; ASCII leaves 8.100 →
8.099 GB. Headless Chromium on kiwix-serve, the new viewer on both (patched
over the old ZIM's), one query from an empty cache — leaves read / JSON
bytes, top 15 results identical in every row:

| query | rule 1 ZIM | rule 2 ZIM |
|---|---|---|
| พัทยา | 70 / 7.13 MB | 3 / 2.42 MB |
| เชียงใหม่ | 40 / 8.43 MB | 3 / 2.87 MB |
| วัดพระแก้ว | 6 / 2.71 MB | 3 / 0.92 MB |
| กรุงเทพ (no class-0 mark) | 3 / 1.08 MB | 3 / 0.92 MB |
| ภูเก็ต | 1 / 4.39 MB | 1 / 2.98 MB |
| ရန်ကုန် / ភ្នំពេញ / ວຽງຈັນ | 1 / 0.76, 0.48, 0.47 MB | 1 / 0.84, 0.35, 0.27 MB |
| Bangkok, Orchard Road (controls) | 3 / 6.01 MB, 34 / 48.2 MB | the same |

Typed one character at a time, the whole word: พัทยา 115 leaves / 8.0 MB →
3 / 2.4 MB; เชียงใหม่ 100 / 15.2 MB → 38 / 4.0 MB (the 38 are the two-character
step, which reads its whole prefix under either rule).

Compatibility:

* **Old ZIM + new viewer**: no `word_rule` → rule 1 → the viewer asks for the
  fragments the old writer indexed, exactly as before. Nothing to do.
* **New ZIM + old viewer**: an old viewer always splits with rule 1, so on a
  rule-2 ZIM a query such as พัทยา also asks for the fragment keys (`ue17`
  for "ทยา"), which a rule-2 writer no longer fills with that name. It still
  finds it: the viewer always adds the whole query as a word too, and for a
  query that starts at a word of the name, that is the rule-2 word (or its
  path runs past the planned depth) — measured, the old viewer on the
  rebuilt southeast-asia: พัทยา 4 leaves with the same top results,
  เชียงใหม่ 39 leaves / 9.0 MB (as slow as before, not wrong). The fragment
  keys are wasted reads, not the only route. Every viewer copy in this repo
  (`resources/viewer/index.html`, `places.html`, and the site-served PWA
  copies in `web/drive/viewer/`) is updated in the same change, and a
  retrofit swaps the new viewer into the ZIM it rewrites. Readers outside
  the repo — mcpzim, the Swift `Geocoder.normalizePrefix` — must read
  `word_rule` and split the same way to get the benefit.
* **Which ZIMs benefit**: only regions with names in scripts that have
  class-0 marks — Thai, Lao, Khmer, Myanmar (southeast-asia), Devanagari,
  Bengali, Tamil, Telugu, Kannada, Malayalam, Gujarati, Gurmukhi, Odia,
  Sinhala (indian-subcontinent, himalayas), Tibetan (china, himalayas),
  Javanese and Balinese (southeast-asia) — and
  small pockets elsewhere (switzerland holds a few Thai/Tamil names). They
  need a rebuild, or `cloud/swap_viewer_rust.py --rebuild-search` (below). A
  Latin/CJK region gains nothing and needs neither.

## Validation

`cloud/validate_zim.py`:

* `sub_chunks[prefix]` equals the leaf union exactly and is never empty;
* every path in `char_split[prefix]` resolves to leaves present in `chunks`;
* sampled records in `ca~r~c` have a qualifying word whose 3rd character is
  `r`; tier leaves hold only their tier's types; every `t` value maps to a
  tier (so a new type can never silently vanish from targeted fetches);
* fail a tier c/p leaf over 16 MB; tiers s/a are exempt (hash children);
* fail a search manifest over 4 MB;
* the Overture-fields sampler (validate_zim.py:694-712) must skip `~a`/`~s`
  leaves, or it warns about address-only chunks.

## Build path

`_emit_split_search` currently does `json.loads` on a whole prefix
(repackage_zim.py:224) — 2.93 GB for `av`, well over 10 GB of Python objects,
on a host with a documented pack/swap-stall history. Stream the per-prefix
JSONL line by line into per-leaf temp files instead, as the retrofit does.

## Retrofit

`cloud/swap_viewer_rust.py --reshard-search`, alongside `--reshard-chips`, so
each region is rewritten once. Per hot prefix: stream its existing leaves,
append each record to a temp JSONL per target leaf (LRU over open fds), emit,
delete. `rec["n"]` is the string the writer consumed and `norm`/`words`
are pure, so the word set reconstructs exactly — but only words whose
`prefix_key` equals the prefix being re-split may be used, or records migrate
between prefixes. Flatten the US/EU `sub_chunks` trees while re-splitting.
It keeps the source's prefixes, so it plans paths with the **source's** word
rule (`word_rule_of(manifest)`): planning a rule-1 ZIM's prefixes with rule 2
would leave records that only a rule-1 fragment put in a prefix with no word
there.

`cloud/swap_viewer_rust.py --rebuild-search` changes the word rule: it
recovers every record once per feature from its **home** prefix — the key of
its whole name's first two characters — taking, per record, the most copies
any one home leaf holds; it re-keys it with `prefixes_for` under the current
rule and runs the build's own emit pass (`_search_emit_chunks`, hot prefixes
split at 10 MB). Every other source manifest key (`addresses_stripped`, …)
is kept.

The home key has not always been computed the same way. Since `6223071`
(2026-09-03) it is `prefix_key(norm(name)[:2])`; before, the writer took
`_prefix_key(name[:2])` on the **raw** name (and split words on it). The two
differ when the raw 2nd character is a mark the fold drops — decomposed
Vietnamese "Ủy ban…" (`u_` raw, `uy` folded), NFD "Écouen" (`e_` / `ec`), a
name opening with a Thai tone mark (`__` / `_p`). So a record is accepted in
either home (`_homes`), and one whose two homes differ is counted through a
single region-wide table so it is never taken twice. Taking only today's
home lost 156 records of southeast-asia 2026-05-09 (26,732,128 of
26,732,284). The rebuild refuses when the recovered count differs from
the source manifest's `total`, or the manifest has no `total`
(`--allow-total-mismatch` overrides, e.g. for a ZIM whose addresses
`derive_zim --strip-addresses` removed), and when a leaf the manifest
declares cannot be read. The records are recovered into the spill directory
right after the source manifest is read, before the packer's stage
(`DST.pack-stage-*`) exists, so these checks fail in minutes, not after the
hours-long entry walk; the rebuild later reads that spool (and checks it
still holds `total` records).

The spill directory holds the whole index while it is re-bucketed (several
GB on a continent), so `--reshard-search` and `--rebuild-search` require
`--tmp DIR` or `$TMPDIR`, and refuse a directory on the root filesystem or
in memory (tmpfs/ramfs, from `/proc/mounts`). A directory named with `--tmp`
is held to the same rule without a search option (large entries are staged
there too). Everything is checked before the source is opened. Wrappers
point it at `/storage` (`retrofit-chips-queue.sh` exports
`TMPDIR=/storage/streetzim/tmp`). Nothing falls back to `/tmp`.

### Administrative areas

`--rebuild-search --add-admin-areas REGION.osm.pbf` gives a ZIM built before
administrative-area search (docs/search-records.md, `admin`) the areas a
build would add, without a rebuild:

* **Extraction**: the builder's own `admin_areas.append_admin_areas` on the
  PBF, for the ZIM's box (`map-config.json` `bounds`, the box the build
  passed it; `--bbox W,S,E,N` overrides, parsed as the build parses its
  `--bbox`, so an RFC 7946 box across the antimeridian, W > E, works). It
  needs the osmium CLI and pyosmium, and runs in a child Python that writes
  the areas to the spill directory (its scratch too) and exits: its 4.2 GB on
  china would otherwise stay the retrofit's high-water mark while the packer
  runs. No area in the box is an error (`--allow-no-admin-areas`). GeoNames
  (`reverse_geocoder`'s bundled `rg_cities1000.csv`, offline; a builder
  dependency, in the image and the host venv) places areas the extract
  clips and names regions; without it those clipped areas are left out and
  a region falls back to the country.
* **Records**: `zim_writer.search_record` (the writer's own record shape:
  `al`, `bb`, `alt`, `osm`, `w`/`q`), serialised as the writer serialises,
  appended to the recovered records and keyed with them under the current
  rule, by the name and every other name (as `_search_bucket` keys them).
  `total` becomes recovered + added. Wiki keys as a production build
  (`--resolve-wikidata-titles`) gives them, offline: the relation's own
  tags (`add_admin_wiki_refs`), then `augment_wiki_cross_refs` with a Q-ID
  -> English title map from the source's `wiki-geo-index.json` (the
  articles it bundles) and the build's `--wikidata-title-cache` JSON when
  given, so a Q-ID-only or non-English tag becomes `w: "en:…"`, `wsrc:
  "wd"`. No Wikimedia request. The map's Wikipedia button appears only for
  an article the ZIM bundles (the geo-index, left untouched); the Find
  list's 📖 links to the public site, as for every record.
* **Kiwix pages** only with `--rebuild-xapian`, below: a `--xapian=builder`
  build writes none, and a page is a document of Kiwix's own search.
* **Already there**: a source with any `t: "admin"` record is left as it is
  (the option is skipped, with a message); nothing is added or replaced.
* **Unchanged**: the Find chips and category index (no `category-index/
  admin.json`, no `streetzim-meta.json` type count) and the wiki geo-index.

### Kiwix's own search (`--rebuild-xapian`)

A `--xapian=builder` ZIM -- all four retrofit targets, and every region the
build wrappers build where xapianbuilder is installed
(`build-region-fast.sh`) -- has Xapian title and full-text indexes whose
documents point at `s/<n>`, and nothing was ever written there:
`_streetzim_to_xapianbuilder_jsonl` named them so "until follow-up work
adds a tiny redirect entry per record" (f38cfb4, 2026-05-08, which moved
the build to xapianbuilder and stopped writing the pages), and that work
never came. On himalayas 2026-09-27, kiwix-serve `/suggest?term=Nepal`
returns `s/85122`, a 404. A `--xapian=libzim` build writes a front-article
page `search/<slug>-<i>.html` for every record of a page type
(`KIWIX_PAGE_TYPES`: place, airport, park, peak, water, admin; POIs too
with `--kiwix-poi-pages`) and the admin areas' other titles as redirects,
and libzim indexes those.

`--rebuild-search --rebuild-xapian` makes the retrofitted ZIM that: every
page-type record gets its page (`search_page` on the record read back as a
feature, `_feature_of`; numbered in record order, the areas
`--add-admin-areas` adds last, as a build appends them), the admin areas
their `~<k>` redirects, and the source's two indexes are replaced by new
ones from the build's own xapianbuilder (`_build_xapian_via_xapianbuilder`,
the corpus `xapianbuilder_doc` writes for a builder build) whose documents
are those pages; the title index also holds every redirect title at its
redirect. Every result opens. Pages, redirects and both indexes are made
before the packer starts; an entry that would be replaced stops the run.
A source that already has `search/` pages (a `--xapian=libzim` ZIM) is
refused. Page titles lose control characters (`zim_writer._title_text`:
"Tunda\nBhuj" -- both packers refuse them).

So the Kiwix A-Z list and random article are the pages, as in a
`--xapian=libzim` ZIM; without `--rebuild-xapian` no page is written, as in
a `--xapian=builder` one. (Fresh `--xapian=builder` builds still point at
`s/<n>`: fixing the build means writing these pages there too.)

Measured 2026-10-02 on himalayas 2026-09-27 (5.34 GB, rule 1, built with
`--xapian=builder`), region PBF of 2026-09-05: 2,452,785 records recovered
(= `total`), 15,890 areas added (16,224 relations read; levels 2: 2, 4: 40,
5: 305, 6: 2,013, 7: 994, 8: 1,888, 9: 9,086, 10: 1,562), `total`
2,468,675; 15,890 pages and 7,148 redirects; 13 min 55 s wall (recovery
~1 min, extraction ~1.5 min, pack 2.5 min), peak RSS 11.1 GB (the Rust
packer; Python 0.7 GB); output 5.09 GB. Country names in `location` come from
admin_areas' short table, so Indian states read "IN".

Use a packer built from this tree (`STREETZIM_PACK_BIN`): the
`streetzim-pack` binary of 2026-09-14 took every front article for the main
page, so the ZIM opened on the last area's page instead of the map.

Budget the largest prefix (`av`, `de`): ~4-6 GB of temp space; re-serialising
~41 k hot leaves roughly doubles the chip-retrofit wall time.

`retrofit-chips-queue.sh` gains a search equivalence check: a per-prefix hash
over sorted record JSON, old versus new. Nothing in the current gates would
catch a record dropped during a re-split.

## Accepted losses

* Mid-word matches the targeted leaves miss (~2 % of hits for `car`),
  mitigated by the few-results fallback.
* ~12 % more bytes across a prefix's leaves: a record with two qualifying
  words appears in two leaves (1.12 leaves/record measured). Indexing only the
  first qualifying word would save that and lose real hits — rejected.
* Address queries ("123 Carrera") still read hash-split leaves, since
  characters cannot divide repeated street names. They are gated behind a
  digit and ≥ 4 characters.
