# Legacy Rust packer

The Rust packer has been replaced by the Python manifest writer. See
[Python manifest packer](zim-builder-python.md) for the build flow, controls,
memory instrumentation and validation.

`create_osm_zim.py --zim-builder rust` is retained as an alias for
`--zim-builder manifest` and runs Python. Cargo is no longer needed for that
backend. The default libzim build flow is unchanged.

The source under `rust/streetzim-pack` remains available for comparison and an
explicit executable override. Building it requires a sibling `zimru` checkout:

```sh
cd rust/streetzim-pack
cargo build --release
# From the repository root, opt in to that particular executable:
STREETZIM_PACK_BIN="$PWD/rust/streetzim-pack/target/release/streetzim-pack" \
  python create_osm_zim.py --area monaco --zim-builder manifest --xapian none
```

Legacy manifests may contain `cluster_break` records. To make that Rust
executable close all active buckets at each break, apply
[`patches/zimru-flush-cluster.patch`](../patches/zimru-flush-cluster.patch) to the
sibling zimru checkout (or use a version exposing `Creator::flush_cluster()` and
updating the running streamer's target), then build with:

```sh
cargo build --release --features cluster_break
```

The record's optional `cluster_size_target` changes the target after the break.
A build without the feature accepts the record but warns once and does not split
clusters; its target changes reach the streamer only with the zimru patch.
Older packers reject the record. The Python replacement supports these boundaries
directly; see [the manifest contract](zim-builder-python.md#features-and-native-search)
and [variant tooling](zim-variants.md).

Use a zimru checkout with the adaptive Zstandard window fix (`64e76c7` or later).
An older encoder can declare a 128 MiB decoder window for small clusters at high
levels, increasing browser memory; see
[packaging gotcha 6](zim-packaging-gotchas.md).

The optional external `xapianbuilder` remains a separate native-search indexer;
its use is independent of the packer implementation.
