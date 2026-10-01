# Rust packer (`--zim-builder rust`)

The [Python manifest packer](zim-builder-python.md) (`--zim-builder manifest`)
is the maintained manifest backend; see its guide for the build flow, controls,
memory instrumentation and validation. The default libzim build flow is
unchanged.

`create_osm_zim.py --zim-builder rust` writes the same manifest and packs it
with a built Rust `streetzim-pack`, looked for in this order:

1. `STREETZIM_PACK_BIN` (an executable file);
2. `rust/streetzim-pack/target/release/streetzim-pack`;
3. `rust/streetzim-pack/target/debug/streetzim-pack`.

If none exists the build stops before it starts, with an error naming these
places. It does not fall back to the Python packer: choose
`--zim-builder manifest` for that. Like the manifest backend it needs
`--xapian builder` or `--xapian none`. (`STREETZIM_PACK_BIN` also overrides
the packer for `--zim-builder manifest`.)

## Building it: the zimru it needs is not in this repository

The source under `rust/streetzim-pack` builds against a sibling `zimru`
checkout (`Cargo.toml`: `zimru = { path = "../../../zimru" }`). **The zimru
revision it was last built with, and any zimru changes beyond
`patches/zimru-flush-cluster.patch`, are not recorded or vendored here.**
`patches/` holds only that flush patch and two libzim patches; it does not
bring a stock zimru up to what `src/main.rs` calls. This repository's CI and
tests do not build the crate, so whether a given zimru revision compiles it is
untested here.

`src/main.rs` uses these zimru APIs:

- `zimru::writer::{Creator, Item, ClusterStrategy}` and `zimru::Compression`
  (`None`, `Zstd`, `Xz`); `ClusterStrategy::{Single, ByMime, ByExtension,
  ByFirstPathSegment}`.
- `Creator::new`, `set_compression`, `set_compression_level`,
  `set_cluster_strategy`, `set_cluster_size_target`,
  `set_max_in_flight_bytes`, `set_streaming_encode_threshold`,
  `try_set_main_path`, `start_writing(path)`, `finish_writing()`.
- Adding entries: `try_add_metadata`, `try_add_metadata_with_mimetype`,
  `try_add_illustration`, `try_add_redirection`, `try_add_item`;
  `Item::new`, `Item::in_namespace` and a public `Item::compress` field.
- Streaming: `Creator::begin_item(path, title, mime, namespace, size)`
  returning a builder with `set_compress`, `write_chunk` and `finish`, and
  `Creator::begin_chunked_item(namespace, …)` for raw file bodies.
- `Creator::flush_cluster()` only with `--features cluster_break` (added by
  `patches/zimru-flush-cluster.patch`).
- Tests: `zimru::Archive::open(path)` and `Archive::main_path()`.

Use a zimru that has all of these and the adaptive Zstandard window fix
(`64e76c7` or later). An older encoder can declare a 128 MiB decoder window
for small clusters at high levels, increasing browser memory; see
[packaging gotcha 6](zim-packaging-gotchas.md).

```sh
# zimru checked out next to this repository (../zimru), at a suitable revision
cd rust/streetzim-pack
cargo build --release
cd ../..
python create_osm_zim.py --area monaco --zim-builder rust --xapian none
```

## Differences from the Python packer

- The Rust packer ignores the manifest's `front` flags (zimru builds its own
  title listing), so its listing can differ from the Python packer's, which
  follows libzim's `FRONT_ARTICLE` hint.
- It writes no `STREETZIM_PACK_STATS_FILE`, so the build logs its peak RSS
  from polling `/proc/<pid>/status` (Linux only) instead of the packer's own
  report.
- Its metadata MIME type is whatever the zimru revision writes.

## cluster_break records

Manifests may contain `cluster_break` records. To make the Rust executable
close all active buckets at each break, apply
[`patches/zimru-flush-cluster.patch`](../patches/zimru-flush-cluster.patch) to
the sibling zimru checkout (or use a version exposing `Creator::flush_cluster()`
and updating the running streamer's target), then build with:

```sh
cargo build --release --features cluster_break
```

The record's optional `cluster_size_target` changes the target after the break.
A build without the feature accepts the record but warns once and does not split
clusters; its target changes reach the streamer only with the zimru patch.
Older packers reject the record. The Python packer supports these boundaries
directly; see [the manifest contract](zim-builder-python.md#features-and-native-search)
and [variant tooling](zim-variants.md).

The optional external `xapianbuilder` remains a separate native-search indexer;
its use is independent of the packer implementation.
