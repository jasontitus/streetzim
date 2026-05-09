# Rust ZIM builder (Path D)

`create_osm_zim.py` ships two ZIM emit backends:

- `--zim-builder=python` (default) — the original `python-libzim`
  `Creator` path. Stable, widely used, requires the libzim C++ stack.
- `--zim-builder=rust` — captures every Creator call to a JSONL
  manifest, then shells out to `streetzim-pack` (a small Rust binary
  backed by [zimru](https://github.com/jasontitus/zimru)) to emit
  the actual `.zim`.

Both paths are wire-compatible — they emit ZIMs that pass `zimcheck`
and read identically in Kiwix. The Rust path exists to break specific
limits of `python-libzim` (peak RSS, GIL-bound emit loop, no per-item
compression knob) on multi-GB builds.

## When to use which

| symptom on a build                                       | recommendation       |
|----------------------------------------------------------|----------------------|
| <1 GB ZIM, no routing graph                              | `python` (simplest)  |
| Large multi-GB ZIM, peak RSS pressure                    | `rust`               |
| Routing graph >500 MB needs `compress=False`             | `rust`               |
| Need a build that the libzim C++ stack already supports  | `python` is fine     |

The Rust path supports per-item compression natively (the patched
`zimru` ships an `Item::compress: Option<bool>` field that routes
`compress=False` items to dedicated raw clusters — see
[zimru's per-item-compression doc](https://github.com/jasontitus/zimru/blob/main/docs/per-item-compression.md)).

## Building

The Rust binary lives under `rust/streetzim-pack/` with `zimru` as a
path dependency at `../../../zimru`. Build once per machine:

```sh
cd rust/streetzim-pack
cargo build --release
# → rust/streetzim-pack/target/release/streetzim-pack
```

The Python side resolves the binary via:
1. `STREETZIM_PACK_BIN` env var if set.
2. `rust/streetzim-pack/target/release/streetzim-pack` (preferred).
3. `rust/streetzim-pack/target/debug/streetzim-pack` (fallback).

If neither is built, `ManifestCreator` raises a clear `RuntimeError`
pointing at the missing binary.

## Using it

Identical CLI to the existing path; just add the flag:

```sh
python3 create_osm_zim.py --area silicon-valley --zim-builder=rust
```

Internally, `create_zim()` swaps the libzim `Creator` for
`cloud.manifest_writer.ManifestCreator`, which:

1. Captures every libzim Creator call (`add_metadata`, `add_item`,
   `add_redirection`, `add_illustration`, `set_mainpath`, `config_*`)
   as a JSONL record into `<output>.zim.pack-stage/manifest.jsonl`.
2. Stages any in-memory bytes (e.g. inline HTML, generated PNGs) into
   that same directory and references them from the manifest by
   absolute path — keeping the Rust side uniform.
3. At `__exit__`, runs `streetzim-pack <manifest> <output.zim>`,
   propagates errors, and (on success) deletes the stage dir unless
   `keep_stage=True` is set on `ManifestCreator`.

## Manifest schema

JSONL, one record per line. Field types are JSON-native; absolute paths
are recommended for any `file:` field.

```jsonl
{"kind":"config","compression":"zstd","compression_level":3,"cluster_strategy":"by_mime","cluster_size_target":2097152,"max_in_flight_bytes":536870912,"main_path":"index.html"}
{"kind":"metadata","name":"Title","value":"OSM Silicon Valley"}
{"kind":"metadata","name":"CustomBlob","mimetype":"application/octet-stream","file":"/abs/blob.bin"}
{"kind":"illustration","size":48,"file":"/abs/icon48.png"}
{"kind":"item","path":"index.html","title":"Map","mime":"text/html","content":"<html>…</html>","front":true}
{"kind":"item","path":"maplibre-gl.js","title":"MapLibre","mime":"application/javascript","file":"/abs/maplibre-gl.js"}
{"kind":"item","path":"routing-data/graph-chunk-0001.bin","title":"","mime":"application/octet-stream","file":"/abs/chunk.bin","streaming":true,"size":104857600,"compress":false}
{"kind":"redirect","path":"home","title":"Home","target":"index.html"}
```

### Records

- **`config`** (zero or one, must precede other records)
  - `compression`: `"none" | "zstd" | "xz"` (default `zstd`)
  - `compression_level`: int (zstd 1–22, xz 0–9)
  - `cluster_strategy`: `"single" | "by_mime" | "by_extension" | "by_first_path_segment"`
  - `cluster_size_target`: bytes (default 2 MiB)
  - `max_in_flight_bytes`: bytes (`0` = unbounded)
  - `main_path`: ZIM path of the front article

- **`metadata`** — `name` + (`value` string OR `file` path), optional `mimetype`.

- **`illustration`** — `size` (side length) + `file` (PNG path).

- **`item`**
  - `path`, `title`, `mime` (required; `title` may be empty)
  - `content` (UTF-8 string) **OR** `file` (absolute path) — exactly one
  - `front`: `true` marks as main page (also sets `main_path` if the
    config record didn't)
  - `streaming`: `true` routes through zimru's chunked path. Required
    for items >256 MiB to keep peak RSS bounded; optional below that.
    The Python ManifestCreator auto-enables it for file-backed items
    >64 MiB.
  - `size`: required when `streaming: true` is set on a file-backed
    item — must match the on-disk size; `streetzim-pack` errors out
    otherwise.
  - `namespace`: optional `u8` (rare; defaults to `'C'`)
  - `compress`: optional `bool`. `false` forces a raw cluster
    regardless of the build's default; `true` or omitted honours the
    default. Mixed values within a manifest are supported (zimru
    routes them to separate clusters).

- **`redirect`** — `path` (alias) + `target` (existing path), optional `title`.

## Differences vs. the Python path

| dimension                | Python (`libzim`)                         | Rust (`streetzim-pack` + `zimru`)              |
|--------------------------|-------------------------------------------|------------------------------------------------|
| Peak RSS for emit        | tied to libzim's queue + worker count     | bounded by `max_in_flight_bytes`               |
| Parallelism              | C++ workers (config_nbworkers); GIL-bound feed | `rayon` work-stealing across cores         |
| Per-item compression     | only via the `Hint.COMPRESS` ItemHint     | native `Item::compress` (mixed builds OK)      |
| FT index (xapian)        | yes (libzim auto-indexer at finalize)     | yes via `xapianbuilder` subprocess; see "Xapian on the rust path" below |
| Streaming for huge items | `FileProvider` (libzim cluster heuristic) | dedicated streaming-encode (zstd or raw)       |
| Cluster strategy         | `single` / `by_mime` (libzim flag)        | `single` / `by_mime` / `by_extension` / `by_first_path_segment` |

Both paths preserve the same ZIM filename + size pattern; output ZIMs
are mutually readable in Kiwix and `zimcheck`.

## Troubleshooting

- **`streetzim-pack binary not found`** — build it (see "Building" above).
- **`item …: declared size N != on-disk size M`** — the manifest's `size`
  hint disagrees with the file. Don't pre-stage a streaming file then
  let it grow; `streetzim-pack` validates byte counts.
- **`begin_item requires start_writing first`** — internal error; means
  the dispatcher didn't open the output for streaming. File a bug.

## Provenance

Path D is an externalisation of the ZIM emit phase only — the
upstream OSM PBF + tile + satellite + terrain + wikidata + overture
pipeline is untouched and still runs in Python. Only the final
`Creator` calls move to Rust.

## Xapian on the rust path

zimru itself is GPL-incompatible so it doesn't link Xapian, but it
*does* accept arbitrary items into the `X/` namespace. We exploit
this by:

1. Building the fulltext + title Xapian glass DBs externally via
   `../xapianbuilder/target/release/xapianbuilder` (a separate Rust
   binary that's GPL-clean for ZIM output and licence-bound to its
   own tree).
2. Adding the resulting `*.glass` files to the manifest as items at
   `namespace=88` (`b'X'`), `path=fulltext/xapian` and `path=title/xapian`,
   `mime=application/octet-stream+xapian`, `compress=False` (required
   — see *Gotcha #6: zimru zstd `windowLog`* in
   `zim-packaging-gotchas.md`; the X namespace is uncompressed by
   Kiwix convention so libzim's reader picks them up via
   `has_fulltext_index`).
3. libzim's reader on Kiwix Desktop / iOS / kiwix-serve treats the
   resulting ZIM identically to one libzim emitted itself.

Wired up by `create_osm_zim.py --xapian=builder` (see
`docs/remote-rebuild.md` for the full flag set). Roughly **40-60×
faster** than libzim's auto-indexer on continent-scale corpora
(California: 252,947 docs in ~3 s vs. ~hours for libzim's
HTML-stub-feed flow).

Caveats:

- `--xapian=builder` requires `--zim-builder=rust`. libzim's
  Creator's public Python API doesn't accept items at the X
  namespace, so we can't inject pre-built glass DBs there.
- `xapianbuilder` is a separate `cargo build --release` in
  `../xapianbuilder/`. The Python side resolves the binary via
  `--xapianbuilder-bin` flag, then `$XAPIANBUILDER_BIN`, then
  `../xapianbuilder/target/{release,debug}/xapianbuilder`.
- Both `xapianbuilder fulltext` and `xapianbuilder title` run in
  parallel; the wall-clock recorded in the build summary is
  `max(fulltext_dur, title_dur)`.

## zimru zstd window-log: don't ship without it

zimru's `encode_cluster` previously used zstd defaults, which set
`windowLog=27` (128 MiB) at high levels. fzstd in browsers
allocates a buffer of windowLog size per decompression — fatal for
the streetzim PWA viewer's 256-parallel-fetch typeahead workload at
continent scale.

Fixed in zimru `64e76c7` (2026-05-08): `windowLog` is pinned to
`ceil(log2(payload.len()))`, clamped `[10, 27]`. See
`docs/zim-packaging-gotchas.md` Gotcha #6 for the full numbers.

If you build streetzim against an older `../zimru/` checkout, you'll
see continent-scale ZIMs that pass validate but fail the smoke
harness's typeahead test. `cd ../zimru && git pull && cargo build
--release` and rebuild streetzim-pack.
