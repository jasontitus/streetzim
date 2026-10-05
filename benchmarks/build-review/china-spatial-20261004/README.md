# China-scale spatial and regional Wikidata memory measurements

See the October follow-up in [the build review](../../../docs/build-review.md#china-memory-follow-up--2026-10-04)
for interpretation, production observations and limitations. These graphs/cache
facts are synthetic; no production host or active country run was modified.

- `summary.json`: spatial dimensions, metrics and caveats.
- `china-size-*.json`: raw 100 ms samples, Linux process RSS/anonymous/PSS,
  cgroup current/anonymous/file/kernel, limits/events, input preparation and
  output hashes. `china-size-current.json` is the 9 GiB capacity run; the
  `*-16g` files are the equal-limit comparison. Each retains 1,600 MiB of
  synthetic parent state. File-cache peaks can include both copied and original
  input. Preparation snapshots were added before the 16 GiB pair; the 9 GiB
  run has copy time/flag but no pre/post-copy snapshots.
- `4m-*.json`: three final small pairs, without synthetic parent reservation.
  Shared bind-mounted input makes their cgroup file-cache ownership unsuitable
  for before/after comparisons. Repeat 2's updated timing is retained.
- `spatial-measured-source.json`: hashes of measured spatial source, tool and
  exact frozen baseline fixture. `source-final.json` records the first tested
  implementation, including its cache/fault tests. These historical hashes
  are retained unchanged. The later adversarial pass added MBTiles selection,
  shared-file group/lock handling, failed-retry invalidation and benchmark
  preflight/reporting fixes; it did not change the measured spatial algorithm
  or regional cache loader. See `adversarial/` for the new source hashes and
  verification, without relabeling old measurements as fresh runs.
- `golden-*`: libzim Monaco builds before/control/after and entry comparison.
  The comparison omits no content differences; two Xapian entries vary normally.
- `wikidata/`: six final fresh-process runs and medians, source hashes,
  independent runtime provenance, focused-test result, and benchmark recipe.

In the historical `validation.json`, `full_profile.wikidata_entries: 207`
counts facts loaded for the writer. The resulting archive contains 96 facts
after tile-reference filtering (`wikidata/manifest.json`), plus seven cached
Wikipedia articles. The later review corrects that distinction in the prose.

Spatial reproduction commands are in the build review. Generate inputs outside
measurement, use fresh output directories and Docker containers, and preserve
Docker exit/OOM state alongside JSON. The reference is frozen `29ec7da` code
in `tests/fixtures/spatial_build_reference.py`. The SHA-256 covers sorted output
filenames, a NUL separator, and every output byte, after conversion timing stops.
No large graphs, cache buckets, ZIMs or output cells belong in this directory.

## Wikidata reproduction

Use Linux and a fresh task-owned scratch directory. Prepare source snapshots
there (`wikidata_current_final.py` must match the hash in `wikidata/summary.json`
for exact reproduction):

```sh
mkdir -p /path/to/new-evidence /path/to/new-results
git show 29ec7da:wikidata_cache.py > /path/to/new-evidence/wikidata_baseline_29ec7da.py
cp wikidata_cache.py /path/to/new-evidence/wikidata_current_final.py
cp benchmarks/build-review/china-spatial-20261004/wikidata/benchmark.py /path/to/new-evidence/benchmark.py
```

Using image `sha256:dfeddd2a65ccfcf6cd72665cae0e3e4a6497cf81248f512db14307e41aab1893`,
mount the checkout at `/review-repo:ro`, evidence at `/evidence` and results at
`/results`. Run `python /evidence/benchmark.py --prepare` once; it refuses to
replace an existing fixture cache. Then mount `/evidence:ro` and run
`python /evidence/benchmark.py` in a new container with `--memory=2g
--memory-swap=2g --cpus=2 --network=none --read-only --tmpfs
/tmp:rw,nosuid,size=64m`. Leave only `/results` writable. The corrected helper
reads actual cgroup limits and records an optional `--image-label` as an
unverified operator label. Independent `runtime-provenance.json` records the
actual settings of the original runs, whose JSON remains unchanged. See
[`wikidata/README.md`](wikidata/README.md) for the historical-helper distinction.
The fixture needs about 190 MB of disk; selected compact output digests must
match across all six processes. The main memory reduction is independent of
cache warmness, while elapsed time can vary with page-cache/host activity.
