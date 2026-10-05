# Wikidata selection benchmark

The dated JSON reports are unchanged historical evidence. Those runs used
Linux Python 3.14, the stated QA image, 2 GiB memory, no swap and two CPUs.
[runtime-provenance.json](runtime-provenance.json) records the actual Linux
runtime and cgroup limits. The original helper printed prescribed limits and
an operator-selected image identity; those fields alone were not verification.

The corrected helper requires Linux, reads the actual cgroup limits, reports
unavailable counters as null with errors, and distinguishes unlimited limits
from zero. `--image-label` is an optional operator label; the helper cannot
verify which Docker image launched it. The correction does not change the
cache workload, compact-fact comparison or historical measurements.

To reproduce, create a separate evidence directory with source snapshots:

```bash
git show 29ec7da539a8611472420903f800d477a83ec610:wikidata_cache.py \
  > /evidence/wikidata_baseline_29ec7da.py
cp wikidata_cache.py /evidence/wikidata_current_final.py
python benchmarks/build-review/china-spatial-20261004/wikidata/benchmark.py \
  --repo "$PWD" --evidence /evidence --cache /evidence/cache \
  --results /results --prepare
python benchmarks/build-review/china-spatial-20261004/wikidata/benchmark.py \
  --repo "$PWD" --evidence /evidence --cache /evidence/cache \
  --results /results
```

Run inside a fresh Linux container with `--memory=2g --memory-swap=2g
--cpus=2 --network=none` for the historical limits. Use fresh cache and empty
results directories. Preparation refuses an existing cache or generated
evidence file; measurement refuses a nonempty results directory. Exclusive
creation also protects evidence from a path created after these checks. An
empty mounted results directory is allowed. Source snapshots and generated
fixture details are required; the script never reads a production cache.
Compare the snapshots' hashes with the summary for the revision being tested.

The fixture has 500,000 facts and selects 30,000 across 90 prefix buckets.
Its explicitly synthetic field lengths and frequencies are not the China
cache distribution. Loader timing excludes startup and selected-fact hashing;
whole-process peaks include later comparison work. No result establishes the
memory limit or completeness of a full China build.
