# Python manifest ZIM packer

The manifest writer is implemented in `streetzim/pack.py`. It replaces the
Rust `streetzim-pack` writer without requiring Cargo or a sibling zimru checkout.
The normal `streetzim` command still uses stock python-libzim by default.

```sh
pip install -e .
streetzim-pack manifest.jsonl.zst output.zim --threads 4 --verbose
# Equivalent from a checkout:
python -m streetzim.pack manifest.jsonl output.zim --threads 4 --verbose

# The Docker image includes this backend and its dependencies:
streetzim --area monaco --name osm_en_monaco --title Monaco \
  --description 'Offline map of Monaco' --zim-builder manifest
# Advanced core command with native search built by the external indexer:
python create_osm_zim.py --area monaco --zim-builder manifest --xapian builder
```

`--zim-builder manifest` runs this interpreter's installed Python module, unless
`STREETZIM_PACK_BIN=/absolute/executable` explicitly selects another packer.
`--zim-builder rust` (in `create_osm_zim.py`) writes the same manifest but packs
it with a built Rust `streetzim-pack`: `STREETZIM_PACK_BIN`, else
`rust/streetzim-pack/target/release/streetzim-pack`, else the `debug` build. If
none exists the build stops with an error before it starts; it never falls back
to the Python packer. See [the Rust guide](zim-builder-rust.md) for what that
binary needs.

## Features and native search

The writer emits ZIM 6.3 with MD5, UUID, MIME list, URL/title/cluster tables,
metadata, illustrations, redirects, main page and the title listing
(`listing/titleOrdered/v1`). Metadata defaults to libzim's MIME type,
`text/plain;charset=UTF-8`. The listing holds front articles only, as libzim's
`FRONT_ARTICLE` hint decides: an item's `front` flag when the record has one
(the adapter always records it for HTML, so `places.html` with `is_front=False`
is not listed), otherwise libzim's default of true for `text/html` items; a
redirect is listed only with `"front": true` (the adapter's `add_redirection`
takes libzim's hints argument). Raw,
Zstandard and XZ clusters can coexist. Per-item `compress=false` and every X
namespace item use raw clusters, including external Xapian databases.

The stock libzim backend owns automatic native search indexing. The manifest
backend supports `--xapian builder` (the existing external `xapianbuilder`
executable, over the same `search/` pages and redirects a libzim build writes,
so Kiwix's results open; before 2026-10-02 it indexed pages that were never
written, docs/search-prefix-locality.md "Kiwix's own search"), or
`--xapian none` (the viewer's own search remains available).
The public CLI defaults to `none` when `--zim-builder manifest` is selected;
it rejects `--xapian libzim` for this backend.

**With `--xapian none` the ZIM has no Xapian indexes at all**: no
`X/fulltext/xapian` (no Kiwix full-text search) and no `X/title/xapian`, so
Kiwix has no Xapian title suggestions. libzim then falls back to prefix
matches on the titles of the listed front articles (a title must start with
what was typed). Use `--xapian builder` for Kiwix's native search, or the
default libzim backend. The external indexer is still an
optional separate dependency; rewriting the packer does not rewrite that indexer.

The first optional JSONL record configures `compression` (`none`, `zstd`, `xz`),
`compression_level`, `cluster_size_target`, `max_in_flight_bytes`, `main_path`, and
`cluster_strategy` (`single`, `by_mime`, `by_extension`, `by_first_path_segment`).
Subsequent records are `item`, `metadata`, `illustration`, `redirect` or
`cluster_break`.
Inline UTF-8 text, strict base64 and regular-file bodies are supported. The adapter
stages in-memory bodies at 64 MiB; file bodies stream through 4 MiB chunks for
both raw and compressed output. Plain and concatenated Zstandard manifests are
detected by magic; incomplete frames/checksums and input file identity, size or timestamp changes fail.

Direct packer Zstandard defaults to level 19 (`ZSTD_CLEVEL` may override it),
including negative fast levels and zero. The map-build adapter retains level 22
unless configured otherwise. XZ defaults to 3 and clamps to 0–9. Cluster targets
apply to body bytes: an oversized single item occupies its own cluster, and offset
tables add a small amount beyond the target. The writer selects 64-bit offsets
for clusters exceeding the 32-bit format boundary.

`{"kind":"cluster_break"}` closes every active cluster bucket, keeping later
items in separate clusters even when they share a MIME, extension or path group.
An optional `cluster_size_target` changes the body-byte target after the break;
it accepts an unsigned 64-bit integer, treats zero as one byte, and leaves the
target unchanged when omitted or null. Leading and consecutive breaks create
no empty clusters. The initial queue budget stays fixed when the target changes.
This supports zoom boundaries for whole-cluster copying or trimming with
`cloud/derive_zim.py` ([variant tooling](zim-variants.md)); the map builder's
planned `--tile-order zoom-hilbert` flow has not yet been ported. Python supports
breaks directly; an explicit legacy Rust executable needs the feature and zimru
patch described in [the legacy guide](zim-builder-rust.md). Older packers reject
the new record.

## Memory and instrumentation

Directory entries, redirect traversal and pointer tables use SQLite on disk,
with an 8 MiB page cache and file-backed sorting. At most 64 cluster buckets
remain open, each with at most 4096 blobs. Key pressure evicts the
smallest recent bucket; byte pressure flushes the fullest bucket, avoiding
oldest-first churn when inputs cycle through more than 64 groups. Open buckets are bounded by the smaller
of the configured queue budget and 64 MiB. Compression has at most two queued jobs
per worker and observes the logical body-byte budget. One oversized cluster is
admitted alone, so a small budget cannot deadlock a large file.

Default queue budget is three target-sized clusters per worker. File recipes count
as logical bytes even though their bodies are streamed. Codec contexts, JSON parsing,
SQLite and interpreter overhead are additional resident memory. A byte budget is
therefore not a total RSS limit. Level-22 compression of a large individual item
can still require a substantial Zstandard context; increasing worker count can
increase memory even with streaming input.

Default workers respect CPU affinity, CPU quota and container memory (one worker
per 2 GiB, capped at 20). `--threads`, `--zim-workers` or the legacy
`RAYON_NUM_THREADS` explicitly override this default. Libzim retains its existing
separate worker policy. Small Zstandard clusters use a size-appropriate window and
a known payload length rather than declaring a large decoder window.

`--verbose` prints JSON (and writes it to `STREETZIM_PACK_STATS_FILE` when that
is set) with wall time, process peak RSS, validation time,
worker count, queue budget, output size, cluster/entry counts and simultaneous
logical pending-body peak. `peak_in_flight_bytes` and `peak_bucket_bytes` are
separate logical peaks; `peak_pending_body_bytes` measures their simultaneous
sum. These metrics exclude codec workspaces; `peak_rss_bytes` includes them.
On Linux `peak_rss_bytes` is the packer process's own `VmHWM`, read from
`/proc/self/status` as it finishes; `VmHWM` starts afresh at `exec`, so it
does not include the parent's memory. (Elsewhere it is `getrusage`'s
`ru_maxrss`.) API calls report the calling process's lifetime high-water mark.
The map-build adapter (`cloud/manifest_writer.py`) logs the figure the packer
wrote to `STREETZIM_PACK_STATS_FILE`. It does not use `wait4`'s `ru_maxrss`,
which on Linux carries the parent's RSS from before `exec` and so reported a
large builder's footprint as the packer's. An executable override that writes
no stats file is measured by polling `/proc/<pid>/status`, ignoring samples
taken before the child has exec'd. Use process-tree and cgroup telemetry for
the complete build.

```sh
python tools/benchmark_zim_pack.py --python-packer --mib 128 \
  --threads 2 --repetitions 3 --json python-pack.json
python tools/benchmark_zim_pack.py --pack-bin /path/to/reviewed/rust/packer \
  --mib 128 --threads 2 --repetitions 3 --json rust-pack.json
```

## Reliability and verification

Archive output and cluster spools use a unique private directory on the output
filesystem. Workers are canceled and joined before cleanup. The writer completes
and fsyncs the archive, then independently checks serialized header, MIME,
directory/title/cluster tables, redirects and raw blob offsets before atomic
publication. Compressed payload decoding and native search are checked in the
native integration tests. Failures preserve the prior destination and the caller's
manifest. A failed adapter attempt (a producer error, a packer failure, or a
successful child exit that did not create an archive) removes its pack stage
(manifest, staged input bodies, partial archive) unless
`STREETZIM_KEEP_PACK_STAGE=1` is set, in which case the error names the kept
manifest. SIGTERM/keyboard interruption cancel the writer: an interrupted build
sends the packer SIGTERM, so it removes its scratch, and SIGKILL only if it is
still running after a 5 s grace; an uncatchable kill can leave private scratch.

Validation rejects duplicate entries/fields, invalid Unicode/control characters,
malformed MIME/base64, missing redirect targets/cycles and invalid integer/boolean
fields. Duplicate unknown JSON fields are deliberately rejected more strictly than
the old serde parser. Each manifest line is capped at 128 MiB.

The regression suite has independent raw-format inspection and libzim readback,
all codecs/strategies, namespace ordering, Unicode, HTML title-listing aliases,
large files, malformed/truncated input, mutation, interruption and injected I/O
failures. The unit tests read archives back with python-libzim; they run
`zimcheck` only where it is installed. CI's `monaco-e2e` job builds Monaco with
`--zim-builder manifest --xapian none` and checks it with `cloud/validate_zim.py`
with `STREETZIM_REQUIRE_ZIMCHECK=1`, so a current `zimcheck` must pass it.
Comparisons with the Rust binary and browser checks were made in review, not in CI.
Measured results and limitations are recorded in `docs/build-review.md`.
