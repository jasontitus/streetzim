# Building on rented machines (plan, 2026-11-07)

A plan, not yet carried out: back up the build caches to object storage and
build maps on rented Hetzner machines beside the build host. It was reviewed
adversarially; what the review changed is folded in below.

## The caches

Measured 2026-11-07 on the build host (`/storage/streetzim`):

| Cache | Size | Backed up | Why |
|---|---:|---|---|
| `world-data` (planet pbfs; per-region pbf, mbtiles, multilingual tiles `.ml.mbtiles`) | 1.02 TB | yes, but the older planet; region pbfs only for the regions a rented machine builds | multilingual tiles take hours (Europe 12–48 h); a machine without the planet cannot cut its region |
| `overture_cache` | 111 GB | yes | Overture removes old releases: the backup is their only copy |
| `search_cache` | 58 GB | yes | |
| `wikidata_cache` | 2.5 GB | yes | |
| `terrain_cache` | 22.5 M files (size not measured: `du` ran past 2 h) | yes, as tar shards by region box | millions of small objects are slow and costly |
| `terrain_cache_10m` | 4.8 GB, 0.86 M files | yes, as tar shards | |
| `satellite_cache_avif_256`, `satellite_cache_sources` | 26.6 M and 8.0 M files (size not measured) | the encoded tiles, as tar shards; sources only if re-encoding matters | refetching is slow and rate-limited |
| `coastline`, `landcover`, `wiki_articles_cache` | 2.1 GB, 38 MB, 114 MB | yes | |
| `wiki-src` (Wikipedia ZIMs; English 124 GB) | 768 GB | the dated files builds pin | Kiwix drops old dumps |
| `langpack-work` | 1.0 TB | `wpcache` only | the rest is derived |
| operational state (`rebuild-par/env`, queue lists, `cloud/*.tsv`, pending and reship tables) | small | yes | not in git |

About 2.5–3 TB plus terrain and satellite tiles; all of `/storage` holds 9.3 TB, so those are at most a few TB more. Size them with a sampled count before the first upload. Bucket `streetzimcache` (Hetzner Object Storage); its key
lives only in `~/.config/rclone/rclone.conf` on the build host (mode 600),
never in the repo. The bucket wants versioning or object lock, or a project
of its own: a key copied to a build machine can otherwise delete it.
Layout: `regions/<id>/…`, `planet/`, `overture/<release>/`,
`terrain/<shard>.tar`, `wiki/`, `wikidata/`, `manifests/<date>.json` (every
object's sha256, written last). `rclone --checksum`; a restore is checked
against the manifest. The upload crosses the home uplink once (measure it
first: 3 TB is a day at 300 Mbit/s); machines in the bucket's location then
pull from it without traffic charges.

## Where maps build

The build host keeps the large maps (Europe, the United States, Russia,
South America, China, Australia–New Zealand, Canada, West Asia, Africa).
For the rest, the review favoured **one or two dedicated machines from
Hetzner's server auction** (64–128 GB RAM, ≥2 TB NVMe, no setup fee) set up
like the build host (`/storage/streetzim` at the same path,
`ops/cloud/parallel_rebuild.sh` unchanged, English Wikipedia copied once)
over a pool of small cloud instances: compute for ~75 maps is about €50–100
either way, and what costs is setup and failed runs. A cloud pool stays an
option once the two changes below exist.

Before anything rebuilds: sort the live maps into those that need a rebuild
(stale extract, new Overture, code that changes the output, MapLibre 6) and
those a viewer swap brings up to date (docs/fresh-viewer.md); and re-cut the
region extracts that lack the `--strategy smart` mark (a build refuses them).

## Rules for any machine but the build host

- **It builds and stages; the build host publishes.** A machine copies its
  ZIM and MD5 to a staging prefix and takes the next map. The build host
  alone checks it overwrites no published language base, uploads to
  archive.org (two or three at a time), waits for the listing and MD5,
  makes the torrent, prunes, deploys the site and queues the language
  versions. So archive.org's keys stay on the build host, nothing is lost
  when a machine goes, and no machine is paid to wait for a listing.
- **Inputs complete or no build.** Today a missing Overture parquet, URL
  cache or xapianbuilder quietly gives another map, and the gates pass. A
  machine's builds need a mode that refuses to start unless every input is
  there and matches the manifest, and its image carries xapianbuilder.
- **No shared cache write-back.** A machine copies what it refreshed to a
  prefix of its own; the build host merges, one at a time.
- **Wikidata and Wikipedia lookups at home.** The build host warms the
  caches first; machines build from them. Spreading lookups over cloud
  addresses dodges a per-address limit, and Wikimedia may block them.
- **No tracker announces, seeding or peers** from any machine, as on the
  build host.
- **One frozen commit for the round.** No merges mid-round.

## Cost (2026 prices, third-party figures: check Hetzner's)

| Item | Estimate |
|---|---:|
| Object storage, 2.5–3+ TB (€6.49 with 1 TB, ~€8.70 per further TB) | €20–30 / month |
| Auction machine, 64–128 GB | ~€40–80 / month each |
| Cloud instead: CPX42 (8 vCPU, 16 GB) ~€0.10/h, CCX33 (8 dedicated vCPU, 32 GB) ~€0.19/h | €40–100 per round, failed runs included |

Hetzner's prices rose sharply in 2026 and differ between sources; the
setup fees of new dedicated servers did too.

## Open decisions

- Auction machines or a cloud pool (and how many).
- How much of `wiki-src` to keep in the bucket.
- The bucket's location (the machines' should match).
