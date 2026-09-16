# Search: give hot prefix chunks locality (proposal v4, 2026-09-16)

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
delete. `rec["n"]` is the string the writer consumed and `_norm`/`_word_re`
are pure, so the word set reconstructs exactly — but only words whose
`_prefix_key` equals the prefix being re-split may be used, or records migrate
between prefixes. Flatten the US/EU `sub_chunks` trees while re-splitting.

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
